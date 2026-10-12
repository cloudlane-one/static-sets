"""The on-node installer runtime, including what it does when the API is gone."""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from static_sets.errors import DefinitionError, NotOfflineReadyError
from static_sets.install import PlacementLayout, read_intent, read_record
from static_sets.installer import (
    ApiUnavailableError,
    Assignment,
    intent_from_payload,
    reconcile,
)
from static_sets.models import StaticSet
from static_sets.naming import placement_configmap_name, placement_identity

CONFIGMAP = {"web-conf": {"app.conf": b"greeting=hello\n"}}
NOW = "2026-10-12T10:00:00Z"
UID = "0f9a4b6c-1111-2222-3333-444455556666"


class FakeApi:
    """An in-memory stand-in for the slice of the API an installer uses."""

    def __init__(
        self,
        static_set: StaticSet,
        *,
        placement: Mapping[str, str] | None = None,
        unavailable: set[str] | None = None,
    ) -> None:
        """Build a fake API.

        Args:
            static_set: The StaticSet every read returns.
            placement: The payload the operator published, if any.
            unavailable: Method names that should report the API as unreachable.
        """
        self.static_set = static_set
        self.placement = placement
        self.unavailable = unavailable or set()
        self.published: dict[str, str] | None = None
        self.reads: list[str] = []

    def _guard(self, method: str) -> None:
        """Raise when a method is configured to be unreachable.

        Args:
            method: The method name.

        Raises:
            ApiUnavailableError: If the method is in the unavailable set.
        """
        if method in self.unavailable:
            raise ApiUnavailableError("connection refused")

    def get_static_set(self, namespace: str, name: str) -> StaticSet:
        """Return the configured StaticSet."""
        self._guard("get_static_set")
        self.reads.append("staticset")
        return self.static_set

    def get_config_map(self, namespace: str, name: str) -> Mapping[str, bytes] | None:
        """Return a ConfigMap from the fixed data set."""
        self._guard("get_config_map")
        self.reads.append(f"configmap:{name}")
        return CONFIGMAP.get(name)

    def get_secret(self, namespace: str, name: str) -> Mapping[str, bytes] | None:
        """Return no Secret; this fake does not model Secret data."""
        self._guard("get_secret")
        return None

    def publish_placement(
        self, namespace: str, name: str, payload: Mapping[str, str], *, static_set: str
    ) -> None:
        """Record the published acknowledgment payload and the set it names."""
        self._guard("publish_placement")
        self.published = dict(payload)
        self.published_for = static_set

    def get_placement(self, namespace: str, name: str) -> Mapping[str, str] | None:
        """Return the configured placement payload."""
        self._guard("get_placement")
        return self.placement


@pytest.fixture()
def assignment(temp_roots: tuple[Path, Path], make_static_set: Any) -> Assignment:
    """Return an installer assignment rooted in the test's temporary directories.

    Args:
        temp_roots: The (manifests, bundle) pair from the fixture.
        make_static_set: The fixture factory for validated StaticSets.

    Returns:
        The assignment.
    """
    del make_static_set
    manifests, bundle = temp_roots
    return Assignment(
        static_set="web",
        namespace="edge",
        uid=UID,
        generation=3,
        node="node-a",
        instance="web-installer-abc",
        static_pod_path=str(manifests),
        bundle_dir=str(bundle),
    )


def api(assignment: Assignment, make_static_set: Any, **kwargs: Any) -> FakeApi:
    """Return a fake API serving a StaticSet that matches the installer's mounts.

    Args:
        assignment: The installer's assignment.
        make_static_set: The fixture factory for validated StaticSets.
        **kwargs: Extra fake-API options.

    Returns:
        The fake API.
    """
    resource = make_static_set(
        spec={
            "installer": {
                "staticPodPath": assignment.static_pod_path,
                "bundleDir": assignment.bundle_dir,
            }
        }
    )
    return FakeApi(resource, **kwargs)


def static_set_for(assignment: Assignment, make_static_set: Any, **overrides: Any) -> StaticSet:
    """Return a StaticSet whose declared installer paths match the assignment.

    Args:
        assignment: The installer's assignment.
        make_static_set: The fixture factory for validated StaticSets.
        **overrides: Spec fields to merge in.

    Returns:
        The validated resource.
    """
    spec = {
        "installer": {
            "staticPodPath": assignment.static_pod_path,
            "bundleDir": assignment.bundle_dir,
        },
        **overrides,
    }
    return make_static_set(spec=spec)


def test_reconcile_installs_and_acknowledges(assignment: Assignment, make_static_set: Any) -> None:
    """A successful reconcile installs the revision and reports it as acknowledged."""
    fake = api(assignment, make_static_set)

    report = reconcile(assignment, api=fake, now=NOW)

    assert report.state == "Installed"
    assert report.acknowledged is True
    assert report.offline is False
    assert report.revision is not None
    layout = assignment.layout(f"web-{assignment.identity}")
    assert Path(layout.manifest_path).is_file()
    record = read_record(layout)
    assert record is not None
    assert record.owner == assignment.instance
    assert record.revision == report.revision


def test_reconcile_keeps_the_revision_when_the_api_is_unreachable(
    assignment: Assignment, make_static_set: Any
) -> None:
    """An unreachable API is never read as an empty desired state."""
    good = api(assignment, make_static_set)
    first = reconcile(assignment, api=good, now=NOW)
    offline = api(assignment, make_static_set, unavailable={"get_static_set"})

    report = reconcile(assignment, api=offline, now=NOW)

    assert report.offline is True
    assert report.revision == first.revision
    assert report.acknowledged is True
    assert "retained" in (report.message or "")
    layout = assignment.layout(f"web-{assignment.identity}")
    assert Path(layout.manifest_path).is_file()


def test_reconcile_reports_offline_without_installing_anything(
    assignment: Assignment, make_static_set: Any
) -> None:
    """Nothing is written locally when the StaticSet cannot be read."""
    offline = api(assignment, make_static_set, unavailable={"get_static_set"})

    report = reconcile(assignment, api=offline, now=NOW)

    assert report.offline is True
    assert report.revision is None
    assert report.acknowledged is False
    layout = assignment.layout(f"web-{assignment.identity}")
    assert not Path(layout.manifest_path).exists()


def test_reconcile_acts_on_a_cleanup_intent_from_the_api(
    assignment: Assignment, make_static_set: Any
) -> None:
    """A cleanup intent published by the operator is persisted before it is acted on."""
    good = api(assignment, make_static_set)
    reconcile(assignment, api=good, now=NOW)
    layout = assignment.layout(f"web-{assignment.identity}")
    payload = {
        "cleanup": "true",
        "placement": assignment.identity,
        "podName": f"web-{assignment.identity}",
        "staticSet": "web",
        "uid": UID,
        "namespace": "edge",
        "node": "node-a",
        "generation": "3",
        "reason": "ScaledDown",
        "removeConfiguration": "true",
        "requestedAt": NOW,
    }
    with_intent = api(assignment, make_static_set, placement=payload)

    report = reconcile(assignment, api=with_intent, now=NOW)

    assert report.state == "Removed"
    assert report.acknowledged is True
    assert not Path(layout.manifest_path).exists()
    intent = read_intent(layout)
    assert intent is not None
    assert intent.reason == "ScaledDown"


def test_cleanup_placement_without_an_intent_is_a_no_op(
    assignment: Assignment, make_static_set: Any
) -> None:
    """A cleanup worker that finds no intent does nothing and says so."""
    from static_sets.installer import cleanup_placement

    fake = api(assignment, make_static_set)

    report = cleanup_placement(assignment, api=fake, now=NOW)

    assert report.acknowledged is True
    assert "no cleanup was requested" in (report.message or "")


def test_cleanup_placement_reports_offline_instead_of_removing(
    assignment: Assignment, make_static_set: Any
) -> None:
    """Without an authoritative intent nothing is removed."""
    from static_sets.installer import cleanup_placement

    good = api(assignment, make_static_set)
    reconcile(assignment, api=good, now=NOW)
    offline = api(assignment, make_static_set, unavailable={"get_placement"})

    report = cleanup_placement(assignment, api=offline, now=NOW)

    assert report.offline is True
    layout = assignment.layout(f"web-{assignment.identity}")
    assert Path(layout.manifest_path).is_file()


def test_image_verification_fails_closed_without_a_checker(
    assignment: Assignment, make_static_set: Any
) -> None:
    """VerifiedBeforeInstall without a checker refuses to publish."""
    resource = static_set_for(
        assignment, make_static_set, offline={"imageProvisioning": "VerifiedBeforeInstall"}
    )
    fake = FakeApi(resource)

    with pytest.raises(NotOfflineReadyError) as error:
        reconcile(assignment, api=fake, now=NOW)

    assert "image checker" in str(error.value)


def test_image_verification_reports_a_missing_image(
    assignment: Assignment, make_static_set: Any
) -> None:
    """A checker that reports a missing image prevents the publish."""
    resource = static_set_for(
        assignment, make_static_set, offline={"imageProvisioning": "VerifiedBeforeInstall"}
    )
    fake = FakeApi(resource)

    with pytest.raises(NotOfflineReadyError) as error:
        reconcile(assignment, api=fake, now=NOW, image_checker=lambda images: False)

    assert "not all images" in str(error.value)


def test_image_verification_installs_when_confirmed(
    assignment: Assignment, make_static_set: Any
) -> None:
    """A confirmed image set lets the revision proceed."""
    resource = static_set_for(
        assignment, make_static_set, offline={"imageProvisioning": "VerifiedBeforeInstall"}
    )
    fake = FakeApi(resource)
    seen: list[list[str]] = []

    def checker(images: list[str]) -> bool:
        seen.append(images)
        return True

    report = reconcile(assignment, api=fake, now=NOW, image_checker=checker)

    assert report.acknowledged is True
    assert seen and seen[0] == ["registry.example.com/web:1.27"]


def test_declared_paths_must_match_the_mounts(assignment: Assignment, make_static_set: Any) -> None:
    """An installer mounted somewhere the StaticSet did not declare refuses to write."""
    resource = static_set_for(
        assignment,
        make_static_set,
        installer={
            "staticPodPath": "/other/manifests",
            "bundleDir": assignment.bundle_dir,
        },
    )
    fake = FakeApi(resource)

    with pytest.raises(DefinitionError) as error:
        reconcile(assignment, api=fake, now=NOW)

    assert "staticPodPath" in str(error.value)


def test_report_payload_is_string_only(assignment: Assignment, make_static_set: Any) -> None:
    """The acknowledgment payload is a ConfigMap-shaped string mapping."""
    report = reconcile(assignment, api=api(assignment, make_static_set), now=NOW)

    payload = report.payload()

    assert all(isinstance(value, str) for value in payload.values())
    assert payload["acknowledged"] == "true"
    assert payload["state"] == "Installed"


def test_intent_payload_absent_means_no_cleanup() -> None:
    """No payload, or a payload without the cleanup flag, means no cleanup."""
    assert intent_from_payload(None) is None
    assert intent_from_payload({"placement": "abc"}) is None
    assert intent_from_payload({"cleanup": "false"}) is None


def test_assignment_derives_a_stable_placement_identity() -> None:
    """The identity comes from the StaticSet UID and the node, never from the Pod."""
    first = Assignment(
        static_set="web",
        namespace="edge",
        uid=UID,
        generation=1,
        node="node-a",
        instance="pod-1",
        static_pod_path="/m",
        bundle_dir="/b",
    )
    second = Assignment(
        static_set="web",
        namespace="edge",
        uid=UID,
        generation=2,
        node="node-a",
        instance="pod-2",
        static_pod_path="/m",
        bundle_dir="/b",
    )

    assert first.identity == second.identity == placement_identity(UID, "node-a")


def test_assignment_honours_a_supplied_placement_identity(temp_roots: tuple[Path, Path]) -> None:
    """A cleanup worker is given the placement explicitly and must use it."""
    manifests, bundle = temp_roots
    given = Assignment(
        static_set="web",
        namespace="edge",
        uid=UID,
        generation=1,
        node="node-a",
        instance="pod-1",
        static_pod_path=str(manifests),
        bundle_dir=str(bundle),
        placement="explicit-identity",
    )

    assert given.identity == "explicit-identity"


def test_placement_configmap_name_is_derived_from_the_placement() -> None:
    """The acknowledgment object is named after the StaticSet and the placement."""
    manifests = Path("/m")
    layout = PlacementLayout(
        bundle_dir="/b", static_pod_path=str(manifests), identity="id1", pod_name="web-id1"
    )

    assert placement_configmap_name("web", "id1") == "web-placement-id1"
    assert layout.manifest_path.endswith("web-id1.yaml")


def test_published_acknowledgment_can_be_serialized(
    assignment: Assignment, make_static_set: Any
) -> None:
    """The payload round-trips through JSON, as a ConfigMap value must."""
    report = reconcile(assignment, api=api(assignment, make_static_set), now=NOW)

    assert json.loads(json.dumps(report.payload())) == report.payload()
