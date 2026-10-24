"""The node manager: installing, rolling out and removing one placement."""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from static_sets.errors import (
    ApiUnavailableError,
    DefinitionError,
    NotOfflineReadyError,
    OwnershipError,
    StaticSetsError,
)
from static_sets.install import claim_placement, read_record
from static_sets.manager import (
    Assignment,
    ManagerConfig,
    MirrorPod,
    PlacementReport,
    cleanup,
    reconcile,
)
from static_sets.models import ImageProvisioning, PlacementPhase, StaticSet, UpdateStrategy
from static_sets.naming import placement_identity, static_pod_name

UID = "0f9a4b6c-1111-2222-3333-444455556666"
NODE = "worker-1"
NOW = "2026-10-24T10:00:00+00:00"


class FakeApi:
    """An in-memory stand-in for the slice of the API the manager uses."""

    def __init__(
        self,
        static_set: StaticSet | None = None,
        *,
        config_maps: Mapping[str, Mapping[str, bytes]] | None = None,
        secrets: Mapping[str, Mapping[str, bytes]] | None = None,
    ) -> None:
        """Bind the fake to a StaticSet and its configuration.

        Args:
            static_set: The StaticSet the placements belong to.
            config_maps: The ConfigMaps the API can serve.
            secrets: The Secrets the API can serve.
        """
        self.static_set = static_set
        self.config_maps = dict(config_maps or {"web-conf": {"app.conf": b"greeting=hello\n"}})
        self.secrets: dict[str, Mapping[str, bytes]] = dict(secrets or {})
        self.pods: dict[str, MirrorPod] = {}
        self.offline = False

    def get_static_set(self, namespace: str, name: str) -> StaticSet | None:
        """Return the StaticSet, or ``None`` once it has been deleted."""
        self._check()
        if self.static_set is None:
            return None
        assert self.static_set.metadata.namespace == namespace
        return self.static_set

    def get_config_map(self, namespace: str, name: str) -> Mapping[str, bytes] | None:
        """Return a ConfigMap's data."""
        self._check()
        return self.config_maps.get(name)

    def get_secret(self, namespace: str, name: str) -> Mapping[str, bytes] | None:
        """Return a Secret's data."""
        self._check()
        return self.secrets.get(name)

    def get_pod(self, namespace: str, name: str) -> MirrorPod | None:
        """Return the mirror Pod kubelet published, if any."""
        self._check()
        return self.pods.get(name)

    def _check(self) -> None:
        """Fail the way the client fails when the API is unreachable."""
        if self.offline:
            raise ApiUnavailableError("connection refused")


def assignment(manifests: Path, bundle: Path, *, generation: int = 3) -> Assignment:
    """Build the assignment of the base StaticSet's placement on ``NODE``.

    Args:
        manifests: kubelet's static Pod path.
        bundle: The bundle directory.
        generation: The placement's generation.

    Returns:
        The assignment.
    """
    return Assignment.for_placement(
        namespace="edge",
        static_set="web",
        uid=UID,
        node=NODE,
        generation=generation,
        instance="manager-1",
        static_pod_path=str(manifests),
        bundle_dir=str(bundle),
    )


def manifest_files(manifests: Path) -> list[str]:
    """Return the manifests currently in kubelet's directory.

    Args:
        manifests: kubelet's static Pod path.

    Returns:
        The file names, sorted.
    """
    return sorted(path.name for path in manifests.glob("*.yaml"))


def test_the_manager_installs_and_acknowledges(
    temp_roots: tuple[Path, Path], make_static_set: Any
) -> None:
    """A pass writes the manifest, records the revision and reports it as installed."""
    manifests, bundle = temp_roots
    api = FakeApi(make_static_set())
    assignment_ = assignment(manifests, bundle)

    report = reconcile(assignment_, api=api, now=NOW)

    record = read_record(assignment_.layout(report.static_pod_name or ""))
    assert report.phase is PlacementPhase.INSTALLED
    assert report.acknowledged is True
    assert report.revision is not None
    assert report.static_pod_name == static_pod_name("web", assignment_.identity, report.revision)
    assert manifest_files(manifests) == [f"{report.static_pod_name}.yaml"]
    assert record is not None
    assert record.revision == report.revision
    assert record.state == "Installed"


def test_the_static_pod_name_carries_the_revision(
    temp_roots: tuple[Path, Path], make_static_set: Any
) -> None:
    """Two revisions of one placement are two different static Pods."""
    manifests, bundle = temp_roots
    api = FakeApi(make_static_set())

    report = reconcile(assignment(manifests, bundle), api=api, now=NOW)

    assert report.static_pod_name is not None
    assert report.static_pod_name.endswith(str(report.revision))
    assert assignment(manifests, bundle).identity in report.static_pod_name


def test_an_unreachable_api_changes_nothing(
    temp_roots: tuple[Path, Path], make_static_set: Any
) -> None:
    """An unreachable API is not an empty desired state."""
    manifests, bundle = temp_roots
    api = FakeApi(make_static_set())
    assignment_ = assignment(manifests, bundle)
    reconcile(assignment_, api=api, now=NOW)
    installed = manifest_files(manifests)

    api.offline = True
    report = reconcile(assignment_, api=api, now=NOW)

    assert report.offline is True
    assert report.revision is not None
    assert report.acknowledged is True
    assert manifest_files(manifests) == installed


def test_a_configuration_change_is_a_new_revision(
    temp_roots: tuple[Path, Path], make_static_set: Any
) -> None:
    """The manager recompiles every pass, so drift in a ConfigMap is noticed."""
    manifests, bundle = temp_roots
    api = FakeApi(make_static_set())
    assignment_ = assignment(manifests, bundle)

    first = reconcile(assignment_, api=api, now=NOW)
    api.config_maps["web-conf"] = {"app.conf": b"greeting=goodbye\n"}
    second = reconcile(assignment_, api=api, now=NOW)

    assert first.revision != second.revision
    # Recreate is the default: the old manifest is gone before the new one is written.
    assert manifest_files(manifests) == [f"{second.static_pod_name}.yaml"]


def test_a_recreate_removes_the_old_manifest_before_the_new_one(
    temp_roots: tuple[Path, Path], make_static_set: Any
) -> None:
    """At most one revision of a placement exists on a node under Recreate."""
    manifests, bundle = temp_roots
    api = FakeApi(make_static_set())
    assignment_ = assignment(manifests, bundle)
    first = reconcile(assignment_, api=api, now=NOW)
    assert first.static_pod_name is not None
    api.pods[first.static_pod_name] = MirrorPod(phase="Running", ready=True)

    api.config_maps["web-conf"] = {"app.conf": b"greeting=goodbye\n"}
    second = reconcile(assignment_, api=api, now=NOW)

    assert manifest_files(manifests) == [f"{second.static_pod_name}.yaml"]
    record = read_record(assignment_.layout(second.static_pod_name or ""))
    assert record is not None
    assert record.retiring is None


def test_surge_keeps_the_previous_revision_until_the_new_one_runs(
    temp_roots: tuple[Path, Path], make_static_set: Any
) -> None:
    """Surge trades a moment of two revisions for no gap at all."""
    manifests, bundle = temp_roots
    static_set = make_static_set(
        spec={"update": {"strategy": UpdateStrategy.SURGE.value, "maxSurge": 1}}
    )
    api = FakeApi(static_set)
    assignment_ = assignment(manifests, bundle)
    first = reconcile(assignment_, api=api, now=NOW)
    assert first.static_pod_name is not None
    api.pods[first.static_pod_name] = MirrorPod(phase="Running", ready=True)
    api.config_maps["web-conf"] = {"app.conf": b"greeting=goodbye\n"}

    second = reconcile(assignment_, api=api, now=NOW)

    assert second.static_pod_name != first.static_pod_name
    assert manifest_files(manifests) == sorted(
        [f"{first.static_pod_name}.yaml", f"{second.static_pod_name}.yaml"]
    )
    assert "still installed" in (second.message or "")

    # Once kubelet reports the new Pod running, the previous manifest is retired.
    assert second.static_pod_name is not None
    api.pods[second.static_pod_name] = MirrorPod(phase="Running", ready=True)
    third = reconcile(assignment_, api=api, now=NOW)

    assert manifest_files(manifests) == [f"{second.static_pod_name}.yaml"]
    assert "retired" in (third.message or "")


def test_a_surge_never_retires_a_revision_that_does_not_run(
    temp_roots: tuple[Path, Path], make_static_set: Any
) -> None:
    """If the new Pod never comes up, the old one stays — and status says so."""
    manifests, bundle = temp_roots
    static_set = make_static_set(
        spec={"update": {"strategy": UpdateStrategy.SURGE.value, "maxSurge": 1}}
    )
    api = FakeApi(static_set)
    assignment_ = assignment(manifests, bundle)
    first = reconcile(assignment_, api=api, now=NOW)
    api.config_maps["web-conf"] = {"app.conf": b"greeting=goodbye\n"}
    reconcile(assignment_, api=api, now=NOW)
    api.config_maps["web-conf"] = {"app.conf": b"greeting=again\n"}

    third = reconcile(assignment_, api=api, now=NOW)

    assert first.static_pod_name is not None
    assert len(manifest_files(manifests)) == 2
    assert "still installed" in (third.message or "")


def test_an_idle_pass_leaves_the_record_installed(
    temp_roots: tuple[Path, Path], make_static_set: Any
) -> None:
    """A pass that finds the wanted revision installed must not downgrade its record.

    The pass claims the placement before it looks at it, which records "installing"; a
    record that stayed that way would make a converged placement look half-done.
    """
    manifests, bundle = temp_roots
    api = FakeApi(make_static_set())
    assignment_ = assignment(manifests, bundle)
    first = reconcile(assignment_, api=api, now=NOW)

    second = reconcile(assignment_, api=api, now="2026-10-24T10:05:00+00:00")

    record = read_record(assignment_.layout(second.static_pod_name or ""))
    assert second.revision == first.revision
    assert "already current" in (second.message or "")
    assert record is not None
    assert record.state == "Installed"
    assert record.updated_at == "2026-10-24T10:05:00+00:00"


def test_an_idle_pass_finishes_a_leftover_retirement(
    temp_roots: tuple[Path, Path], make_static_set: Any
) -> None:
    """A revision left behind by a Surge is removed even after the strategy changed."""
    manifests, bundle = temp_roots
    static_set = make_static_set(
        spec={"update": {"strategy": UpdateStrategy.SURGE.value, "maxSurge": 1}}
    )
    api = FakeApi(static_set)
    assignment_ = assignment(manifests, bundle)
    first = reconcile(assignment_, api=api, now=NOW)
    assert first.static_pod_name is not None
    api.pods[first.static_pod_name] = MirrorPod(phase="Running", ready=True)
    api.config_maps["web-conf"] = {"app.conf": b"greeting=goodbye"}
    second = reconcile(assignment_, api=api, now=NOW)
    assert second.static_pod_name is not None

    # kubelet reports the new Pod running, so the next idle pass retires the old one.
    api.pods[second.static_pod_name] = MirrorPod(phase="Running", ready=True)
    third = reconcile(assignment_, api=api, now=NOW)

    assert manifest_files(manifests) == [f"{second.static_pod_name}.yaml"]
    assert "retired" in (third.message or "")


def test_image_verification_fails_closed_without_a_checker(
    temp_roots: tuple[Path, Path], make_static_set: Any
) -> None:
    """``VerifiedBeforeInstall`` refuses to publish what it cannot confirm."""
    manifests, bundle = temp_roots
    static_set = make_static_set(
        spec={"offline": {"imageProvisioning": ImageProvisioning.VERIFIED_BEFORE_INSTALL.value}}
    )

    with pytest.raises(NotOfflineReadyError) as error:
        reconcile(assignment(manifests, bundle), api=FakeApi(static_set), now=NOW)

    assert "no image checker was supplied" in str(error.value)
    assert manifest_files(manifests) == []


def test_image_verification_reports_a_missing_image(
    temp_roots: tuple[Path, Path], make_static_set: Any
) -> None:
    """A checker that reports a missing image refuses the installation."""
    manifests, bundle = temp_roots
    static_set = make_static_set(
        spec={"offline": {"imageProvisioning": ImageProvisioning.VERIFIED_BEFORE_INSTALL.value}}
    )

    with pytest.raises(NotOfflineReadyError) as error:
        reconcile(
            assignment(manifests, bundle),
            api=FakeApi(static_set),
            now=NOW,
            image_checker=lambda images: False,
        )

    assert "are present on" in str(error.value)


def test_image_verification_installs_when_confirmed(
    temp_roots: tuple[Path, Path], make_static_set: Any
) -> None:
    """A confirmed image set is installed normally."""
    manifests, bundle = temp_roots
    static_set = make_static_set(
        spec={"offline": {"imageProvisioning": ImageProvisioning.VERIFIED_BEFORE_INSTALL.value}}
    )

    report = reconcile(
        assignment(manifests, bundle),
        api=FakeApi(static_set),
        now=NOW,
        image_checker=lambda images: "registry.example.com/web:1.27" in images,
    )

    assert report.phase is PlacementPhase.INSTALLED


def test_cleanup_removes_the_manifest_and_acknowledges(
    temp_roots: tuple[Path, Path], make_static_set: Any
) -> None:
    """A removal is acknowledged once kubelet has dropped the mirror Pod."""
    manifests, bundle = temp_roots
    api = FakeApi(make_static_set())
    assignment_ = assignment(manifests, bundle)
    installed = reconcile(assignment_, api=api, now=NOW)

    report = cleanup(
        assignment_,
        api=api,
        now=NOW,
        reason="ScaledDown",
        remove_configuration=True,
    )

    assert report.phase is PlacementPhase.CLEANED
    assert report.acknowledged is True
    assert manifest_files(manifests) == []
    assert installed.revision is not None
    assert not (bundle / "placements" / assignment_.identity / "revisions").exists()


def test_cleanup_waits_while_the_workload_may_still_run(
    temp_roots: tuple[Path, Path], make_static_set: Any
) -> None:
    """Deleting the files a still-running workload mounts would break it."""
    manifests, bundle = temp_roots
    api = FakeApi(make_static_set())
    assignment_ = assignment(manifests, bundle)
    installed = reconcile(assignment_, api=api, now=NOW)
    assert installed.static_pod_name is not None
    api.pods[installed.static_pod_name] = MirrorPod(phase="Running", ready=True)

    first = cleanup(assignment_, api=api, now=NOW, reason="ScaledDown", remove_configuration=True)

    assert first.acknowledged is False
    assert first.phase is PlacementPhase.CLEANING
    assert manifest_files(manifests) == []
    assert (bundle / "placements" / assignment_.identity / "revisions").exists()

    del api.pods[installed.static_pod_name]
    second = cleanup(assignment_, api=api, now=NOW, reason="ScaledDown", remove_configuration=True)

    assert second.acknowledged is True
    assert not (bundle / "placements" / assignment_.identity / "revisions").exists()


def test_cleanup_without_a_local_record_acknowledges_immediately(
    temp_roots: tuple[Path, Path], make_static_set: Any
) -> None:
    """A placement that was never installed has nothing to remove."""
    manifests, bundle = temp_roots

    report = cleanup(
        assignment(manifests, bundle),
        api=FakeApi(make_static_set()),
        now=NOW,
        reason="Relocated",
        remove_configuration=True,
    )

    assert report.acknowledged is True
    assert "never installed" in (report.message or "")


def test_cleanup_keeps_configuration_when_the_policy_says_so(
    temp_roots: tuple[Path, Path], make_static_set: Any
) -> None:
    """``removeConfiguration`` is the placement's decision, not the manager's."""
    manifests, bundle = temp_roots
    api = FakeApi(make_static_set())
    assignment_ = assignment(manifests, bundle)
    reconcile(assignment_, api=api, now=NOW)

    report = cleanup(assignment_, api=api, now=NOW, reason="ScaledDown", remove_configuration=False)

    assert report.acknowledged is True
    assert "retained by policy" in (report.message or "")
    assert (bundle / "placements" / assignment_.identity / "revisions").exists()


def test_a_placement_whose_set_is_gone_waits_for_the_controller(
    temp_roots: tuple[Path, Path], make_static_set: Any
) -> None:
    """A missing StaticSet is not a removal request: only the record may decide that."""
    manifests, bundle = temp_roots
    api = FakeApi(make_static_set())
    assignment_ = assignment(manifests, bundle)
    reconcile(assignment_, api=api, now=NOW)
    api.static_set = None

    report = reconcile(assignment_, api=api, now=NOW)

    assert report.phase is PlacementPhase.CLEANING
    assert report.acknowledged is False
    assert len(manifest_files(manifests)) == 1


def test_a_newer_generation_cannot_be_reclaimed(
    temp_roots: tuple[Path, Path], make_static_set: Any
) -> None:
    """A stale manager cannot overwrite a placement that has moved on."""
    manifests, bundle = temp_roots
    newer = read_claim = assignment(manifests, bundle, generation=5)
    claim_placement(
        newer.layout("web-x"),
        static_set="web",
        static_set_uid=UID,
        namespace="edge",
        node=NODE,
        generation=5,
        owner="manager-1",
        now=NOW,
    )

    with pytest.raises(OwnershipError):
        reconcile(
            assignment(manifests, bundle, generation=4), api=FakeApi(make_static_set()), now=NOW
        )

    assert read_claim.generation == 5


def test_a_placement_identity_is_per_node() -> None:
    """The identity is derived from the UID and the node, never from a Pod name."""
    assert placement_identity(UID, NODE) == placement_identity(UID, NODE)
    assert placement_identity(UID, NODE) != placement_identity(UID, "worker-2")


def test_the_manager_paths_must_be_absolute_and_disjoint() -> None:
    """The manager owns two host paths, and they may not overlap."""
    with pytest.raises(DefinitionError) as error:
        Assignment.for_placement(
            namespace="edge",
            static_set="web",
            uid=UID,
            node=NODE,
            generation=1,
            instance="manager-1",
            static_pod_path="etc/kubernetes/manifests",
            bundle_dir="/var/lib/static-sets",
        )
    assert "must be an absolute path" in str(error.value)

    with pytest.raises(DefinitionError) as overlap:
        Assignment.for_placement(
            namespace="edge",
            static_set="web",
            uid=UID,
            node=NODE,
            generation=1,
            instance="manager-1",
            static_pod_path="/etc/kubernetes/manifests",
            bundle_dir="/etc/kubernetes/manifests/tmp",
        )
    assert "must be disjoint" in str(overlap.value)


def test_the_runtime_class_is_compiled_into_the_manifest(
    temp_roots: tuple[Path, Path], make_static_set: Any
) -> None:
    """A declared runtime class reaches the static Pod, unlike the probe's placeholder."""
    manifests, bundle = temp_roots
    api = FakeApi(
        make_static_set(spec={"template": {"spec": {"runtimeClassName": "nvidia.com/gpu"}}})
    )

    report = reconcile(assignment(manifests, bundle), api=api, now=NOW)

    assert report.static_pod_name is not None
    manifest = json.loads(
        (manifests / f"{report.static_pod_name}.yaml").read_text(encoding="utf-8")
    )
    assert manifest["spec"]["runtimeClassName"] == "nvidia.com/gpu"


def test_the_report_payload_is_string_only() -> None:
    """A status payload crosses the API as strings and booleans only."""
    payload = PlacementReport(
        revision="r-1",
        static_pod_name="web-a-r-1",
        phase=PlacementPhase.INSTALLED,
        acknowledged=True,
    ).payload()

    assert payload["acknowledged"] == "true"
    assert payload["phase"] == "Installed"
    assert all(isinstance(value, str) for value in payload.values())


def test_the_manager_configuration_comes_from_the_environment() -> None:
    """The node and the paths are configuration of the node, not of a StaticSet."""
    config = ManagerConfig.from_environment(
        {
            "STATIC_SETS_NODE": "worker-1",
            "STATIC_SETS_MANAGER_POD": "manager-abc",
            "STATIC_SETS_STATIC_POD_PATH": "/var/lib/kubelet/manifests",
            "STATIC_SETS_BUNDLE_DIR": "/var/lib/static-sets",
        }
    )

    assert config.node == "worker-1"
    assert config.instance == "manager-abc"
    assert config.static_pod_path == "/var/lib/kubelet/manifests"


def test_a_manager_without_a_node_cannot_start() -> None:
    """Without a node the process would not know which host it is responsible for."""
    with pytest.raises(StaticSetsError) as error:
        ManagerConfig.from_environment({})

    assert "names no node" in str(error.value)


def test_a_manager_refuses_a_placement_of_another_node(make_placement: Any) -> None:
    """A placement is only ever written by the manager of the node it names."""
    config = ManagerConfig(node=NODE, instance="manager-1")

    with pytest.raises(DefinitionError) as error:
        config.assignment(make_placement(spec={"node": "worker-2"}))

    assert "belongs to node" in str(error.value)


def test_the_manager_configuration_builds_an_assignment(make_placement: Any) -> None:
    """An assignment is the placement plus the node's own configuration."""
    config = ManagerConfig(node=NODE, instance="manager-1")

    assignment_ = config.assignment(make_placement())

    assert assignment_.identity == placement_identity(UID, NODE)
    assert assignment_.namespace == "edge"
    assert assignment_.generation == 3
