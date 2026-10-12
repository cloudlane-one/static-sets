"""The installer command: environment handling, API calls and end-to-end runs."""

from __future__ import annotations

import base64
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from static_sets.errors import StaticSetsError
from static_sets_installer import (
    ClusterApi,
    assignment_from_environment,
    main,
)

UID = "0f9a4b6c-1111-2222-3333-444455556666"
NAMESPACE = "edge"
SET_NAME = "web"


class FakeJsonClient:
    """A JSON client that answers from a path-keyed table and records calls."""

    def __init__(self, answers: Mapping[str, Any] | None = None) -> None:
        """Build the client.

        Args:
            answers: Path to response mapping; a missing path answers ``{}``.
        """
        self.answers = dict(answers or {})
        self.calls: list[tuple[str, str, Any]] = []

    def json(
        self,
        method: str,
        path: str,
        *,
        body: Any = None,
        params: Mapping[str, str] | None = None,
        missing: Any = None,
    ) -> Any:
        """Answer one call, honouring the caller's ``missing`` sentinel.

        Args:
            method: The HTTP method.
            path: The API path.
            body: The request body.
            params: Query parameters.
            missing: The value to return when the path is unknown.

        Returns:
            The recorded answer.
        """
        self.calls.append((method, path, body))
        if method == "POST":
            created = f"{path.rstrip('/')}/{body['metadata']['name']}"
            self.answers[created] = body
            return body
        if path in self.answers:
            return self.answers[path]
        return missing


def environment(temp_roots: tuple[Path, Path], **overrides: str) -> dict[str, str]:
    """Return an installer environment rooted in the test's temporary directories.

    Args:
        temp_roots: The (manifests, bundle) pair from the fixture.
        **overrides: Environment values to replace.

    Returns:
        The environment.
    """
    manifests, bundle = temp_roots
    values = {
        "KUBERNETES_SERVICE_HOST": "10.0.0.1",
        "STATIC_SETS_SET": SET_NAME,
        "STATIC_SETS_NAMESPACE": NAMESPACE,
        "STATIC_SETS_UID": UID,
        "STATIC_SETS_GENERATION": "3",
        "STATIC_SETS_NODE": "node-a",
        "STATIC_SETS_POD_NAME": "web-installer-abc",
        "STATIC_SETS_STATIC_POD_PATH": str(manifests),
        "STATIC_SETS_BUNDLE_DIR": str(bundle),
    }
    values.update(overrides)
    return values


def static_set_body(temp_roots: tuple[Path, Path], **spec_overrides: Any) -> dict[str, Any]:
    """Return the StaticSet body the fake API serves.

    Args:
        temp_roots: The (manifests, bundle) pair from the fixture.
        **spec_overrides: Spec fields to merge in.

    Returns:
        The wire-shaped body.
    """
    from static_sets.models import StaticSet

    manifests, bundle = temp_roots
    installer = {
        "image": "registry.test/static-sets:1",
        "staticPodPath": str(manifests),
        "bundleDir": str(bundle),
        **dict(spec_overrides.pop("installer", {})),
    }
    spec = {"installer": installer, **spec_overrides}
    resource = StaticSet.model_validate(
        {
            "apiVersion": "staticsets.cloudlane.dev/v1",
            "kind": "StaticSet",
            "metadata": {"name": SET_NAME, "namespace": NAMESPACE, "uid": UID, "generation": 3},
            "spec": {
                "mode": "Deployment",
                "replicas": 1,
                "template": {
                    "spec": {
                        "containers": [{"name": "app", "image": "registry.test/web:1"}],
                    }
                },
                **spec,
            },
        }
    )
    return resource.model_dump(mode="json", by_alias=True)


def assignment_environment(temp_roots: tuple[Path, Path]) -> dict[str, str]:
    """Return the environment as the operator would inject it.

    Args:
        temp_roots: The (manifests, bundle) pair from the fixture.

    Returns:
        The environment.
    """
    return environment(temp_roots)


def test_assignment_reads_every_required_variable(temp_roots: tuple[Path, Path]) -> None:
    """The assignment is built from the Pod's environment alone."""
    assignment = assignment_from_environment(environment(temp_roots))

    assert assignment.static_set == SET_NAME
    assert assignment.uid == UID
    assert assignment.generation == 3
    assert assignment.node == "node-a"
    assert assignment.instance == "web-installer-abc"
    assert assignment.placement is None


def test_assignment_lists_what_is_missing(temp_roots: tuple[Path, Path]) -> None:
    """A missing variable is named rather than silently defaulted."""
    incomplete = environment(temp_roots)
    del incomplete["STATIC_SETS_NODE"]

    with pytest.raises(StaticSetsError) as error:
        assignment_from_environment(incomplete)

    assert "STATIC_SETS_NODE" in str(error.value)


def test_assignment_honours_an_explicit_placement(temp_roots: tuple[Path, Path]) -> None:
    """A cleanup worker is told which placement it owns."""
    assignment = assignment_from_environment(
        environment(temp_roots, STATIC_SETS_PLACEMENT="explicit")
    )

    assert assignment.identity == "explicit"


def derived_identity() -> str:
    """Return the placement identity the installer derives for its node.

    Returns:
        The placement identity.
    """
    from static_sets.naming import placement_identity

    return placement_identity(UID, "node-a")


def static_set_path() -> str:
    """Return the REST path of the test StaticSet.

    Returns:
        The path.
    """
    return f"/apis/staticsets.cloudlane.dev/v1/namespaces/{NAMESPACE}/staticsets/{SET_NAME}"


def config_map_path(name: str) -> str:
    """Return the REST path of a ConfigMap.

    Args:
        name: The ConfigMap's name.

    Returns:
        The path.
    """
    return f"/api/v1/namespaces/{NAMESPACE}/configmaps/{name}"


def test_cluster_api_reads_and_validates_the_static_set() -> None:
    """The StaticSet is validated into the typed model before use."""
    client = FakeJsonClient({static_set_path(): static_set_body_fixture()})

    resource = ClusterApi(client).get_static_set(NAMESPACE, SET_NAME)

    assert resource.spec.replicas == 1
    assert resource.spec.mode.value == "Deployment"


def static_set_body_fixture() -> dict[str, Any]:
    """Return a minimal valid StaticSet body for the API tests.

    Returns:
        The wire-shaped body.
    """
    return {
        "apiVersion": "staticsets.cloudlane.dev/v1",
        "kind": "StaticSet",
        "metadata": {"name": SET_NAME, "namespace": NAMESPACE, "uid": UID},
        "spec": {
            "mode": "Deployment",
            "replicas": 1,
            "template": {"spec": {"containers": [{"name": "app", "image": "img:1"}]}},
            "installer": {"image": "img:1"},
        },
    }


def test_cluster_api_decodes_config_map_data_and_binary_data() -> None:
    """Text and base64 entries of a ConfigMap become bytes."""
    client = FakeJsonClient(
        {
            config_map_path("cfg"): {
                "data": {"a": "text"},
                "binaryData": {"b": base64.b64encode(b"\x00\x01").decode()},
            }
        }
    )

    data = ClusterApi(client).get_config_map(NAMESPACE, "cfg")

    assert data == {"a": b"text", "b": b"\x00\x01"}


def test_cluster_api_reports_an_absent_config_map() -> None:
    """A 404 becomes ``None`` rather than an empty mapping."""
    client = FakeJsonClient()

    assert ClusterApi(client).get_config_map(NAMESPACE, "cfg") is None


def test_cluster_api_decodes_secret_data() -> None:
    """Secret values are base64 and are decoded to bytes."""
    client = FakeJsonClient(
        {f"/api/v1/namespaces/{NAMESPACE}/secrets/sec": {"data": {"token": "czNjcjN0"}}}
    )

    data = ClusterApi(client).get_secret(NAMESPACE, "sec")

    assert data == {"token": b"s3cr3t"}


def test_cluster_api_creates_then_updates_a_placement() -> None:
    """The acknowledgment object is created on first publish and replaced later."""
    client = FakeJsonClient()
    api = ClusterApi(client)

    api.publish_placement(NAMESPACE, "web-placement-id1", {"placement": "id1"})
    api.publish_placement(NAMESPACE, "web-placement-id1", {"placement": "id1", "state": "Removed"})

    methods = [method for method, path, _ in client.calls]
    assert methods == ["GET", "POST", "GET", "PUT"]
    created = client.calls[1][1]
    assert created == f"/api/v1/namespaces/{NAMESPACE}/configmaps"
    assert client.calls[3][1] == config_map_path("web-placement-id1")


def test_cluster_api_reads_a_published_placement() -> None:
    """The payload the operator published is returned as a plain mapping."""
    client = FakeJsonClient({config_map_path("web-placement-id1"): {"data": {"cleanup": "true"}}})

    payload = ClusterApi(client).get_placement(NAMESPACE, "web-placement-id1")

    assert payload == {"cleanup": "true"}


def test_release_self_clears_the_finalizer_and_deletes_the_pod() -> None:
    """A finished cleanup worker removes its own finalizer and itself."""
    client = FakeJsonClient()
    api = ClusterApi(client)

    api.release_self(NAMESPACE, "web-cleanup-id1")

    assert [method for method, _, _ in client.calls] == ["PATCH", "DELETE"]
    assert client.calls[0][2] == {"metadata": {"finalizers": []}}


def test_release_self_tolerates_a_failure() -> None:
    """Failing to release itself must not fail the cleanup."""
    from static_sets.installer import ApiUnavailableError

    class Broken(FakeJsonClient):
        def json(self, method: str, path: str, **kwargs: Any) -> Any:
            self.calls.append((method, path, kwargs.get("body")))
            raise ApiUnavailableError("gone")

    ClusterApi(Broken()).release_self(NAMESPACE, "pod")


def serving_transport(
    temp_roots: tuple[Path, Path],
    *,
    spec_overrides: Mapping[str, Any] | None = None,
) -> tuple[Any, list[tuple[str, str]]]:
    """Return a transport serving the installer's reads, and its call log.

    Args:
        temp_roots: The (manifests, bundle) pair from the fixture.
        spec_overrides: Extra spec fields for the served StaticSet.

    Returns:
        The transport and the list it records calls into.
    """
    body = static_set_body(temp_roots, **dict(spec_overrides or {}))
    config_map = {"data": {"app.conf": "greeting=hello\n"}}
    calls: list[tuple[str, str]] = []

    def transport(method: str, url: str, payload: bytes | None, headers: Any) -> tuple[int, bytes]:
        del payload, headers
        path = url.split("?")[0].replace("https://10.0.0.1:443", "")
        calls.append((method, path))
        if path == static_set_path():
            return 200, json.dumps(body).encode()
        if path == config_map_path("web-conf"):
            return 200, json.dumps(config_map).encode()
        if path.startswith(config_map_path("")):
            if method == "POST":
                return 201, b"{}"
            return 404, b"{}"
        return 200, b"{}"

    return transport, calls


def test_main_reconciles_a_placement_end_to_end(temp_roots: tuple[Path, Path]) -> None:
    """The command installs the static Pod and acknowledges it."""
    transport, calls = serving_transport(temp_roots)

    status = main(
        [],
        environ=environment(temp_roots),
        transport=transport,
        now="2026-10-12T10:00:00+00:00",
    )

    assert status == 0
    manifests, _ = temp_roots
    written = list(manifests.glob("*.yaml"))
    assert len(written) == 1
    assert written[0].name.startswith("web-")
    assert any(method == "POST" for method, _ in calls)


def test_main_treats_an_unreachable_api_as_a_non_failure(
    temp_roots: tuple[Path, Path],
) -> None:
    """Being offline is a state to report, not a crash: the local state is kept."""

    def broken(*_: Any) -> tuple[int, bytes]:
        raise OSError("no route to host")

    status = main([], environ=environment(temp_roots), transport=broken)

    assert status == 0
    manifests, _ = temp_roots
    assert list(manifests.glob("*.yaml")) == []


def test_main_reports_an_incomplete_environment(temp_roots: tuple[Path, Path]) -> None:
    """A missing environment variable is a configuration error, not a reconcile."""
    incomplete = environment(temp_roots)
    del incomplete["STATIC_SETS_UID"]

    assert main([], environ=incomplete) == 2


def test_main_fails_closed_when_images_cannot_be_verified(
    temp_roots: tuple[Path, Path],
) -> None:
    """A revision whose images are unconfirmed must not be published."""
    transport, _ = serving_transport(
        temp_roots, spec_overrides={"offline": {"imageProvisioning": "VerifiedBeforeInstall"}}
    )

    status = main(
        [],
        environ=environment(temp_roots),
        transport=transport,
        now="2026-10-12T10:00:00+00:00",
    )

    assert status == 1
    manifests, _ = temp_roots
    assert list(manifests.glob("*.yaml")) == []


def test_main_runs_a_cleanup_and_releases_itself(temp_roots: tuple[Path, Path]) -> None:
    """A cleanup worker removes the workload, acknowledges, and clears its finalizer."""
    transport, _ = serving_transport(temp_roots)
    main([], environ=environment(temp_roots), transport=transport, now="2026-10-12T10:00:00+00:00")
    manifests, _ = temp_roots
    assert list(manifests.glob("*.yaml"))

    serving, calls = serving_transport(temp_roots)
    identity = derived_identity()

    def cleanup_transport(
        method: str, url: str, payload: bytes | None, headers: Any
    ) -> tuple[int, bytes]:
        path = url.split("?")[0].replace("https://10.0.0.1:443", "")
        if path.startswith(config_map_path("web-placement-")):
            if method == "POST":
                return 201, b"{}"
            return 200, json.dumps(
                {
                    "data": {
                        "cleanup": "true",
                        "placement": identity,
                        "podName": f"web-{identity}",
                        "staticSet": SET_NAME,
                        "uid": UID,
                        "namespace": NAMESPACE,
                        "node": "node-a",
                        "generation": "3",
                        "reason": "ScaledDown",
                        "removeConfiguration": "true",
                        "requestedAt": "2026-10-12T10:00:00Z",
                    }
                }
            ).encode()
        return serving(method, url, payload, headers)

    status = main(
        ["cleanup"],
        environ=environment(temp_roots, STATIC_SETS_PLACEMENT=identity),
        transport=cleanup_transport,
        now="2026-10-12T10:00:00+00:00",
    )

    assert status == 0
    assert list(manifests.glob("*.yaml")) == []
    assert ("PATCH", f"/api/v1/namespaces/{NAMESPACE}/pods/web-installer-abc") in calls


def test_main_with_a_running_workload_keeps_its_configuration(
    temp_roots: tuple[Path, Path],
) -> None:
    """A worker told the workload is still running does not delete its files."""
    transport, _ = serving_transport(temp_roots)
    main([], environ=environment(temp_roots), transport=transport, now="2026-10-12T10:00:00+00:00")
    manifests, bundle = temp_roots
    revisions = list((bundle / "placements").glob("*/revisions/*"))
    assert revisions

    identity = derived_identity()
    serving, _ = serving_transport(temp_roots)

    def cleanup_transport(
        method: str, url: str, payload: bytes | None, headers: Any
    ) -> tuple[int, bytes]:
        path = url.split("?")[0].replace("https://10.0.0.1:443", "")
        if path.startswith(config_map_path("web-placement-")):
            return 200, json.dumps(
                {
                    "data": {
                        "cleanup": "true",
                        "placement": identity,
                        "podName": "web-id1",
                        "staticSet": SET_NAME,
                        "uid": UID,
                        "namespace": NAMESPACE,
                        "node": "node-a",
                        "generation": "3",
                        "reason": "ScaledDown",
                        "removeConfiguration": "true",
                        "requestedAt": "2026-10-12T10:00:00Z",
                    }
                }
            ).encode()
        return serving(method, url, payload, headers)

    status = main(
        ["cleanup", "--workload-running"],
        environ=environment(temp_roots, STATIC_SETS_PLACEMENT=identity),
        transport=cleanup_transport,
        now="2026-10-12T10:00:00+00:00",
    )

    assert status == 0
    assert list(manifests.glob("*.yaml")) == []
    assert list((bundle / "placements").glob("*/revisions/*"))


def test_the_module_entry_point_runs_the_command(tmp_path: Path) -> None:
    """``python -m static_sets_installer`` is the documented way to start it."""
    import os
    import subprocess
    import sys

    environment = {
        "PATH": os.environ.get("PATH", ""),
        "SYSTEMROOT": os.environ.get("SYSTEMROOT", ""),
    }
    result = subprocess.run(
        [sys.executable, "-m", "static_sets_installer"],
        capture_output=True,
        text=True,
        env=environment,
        cwd=tmp_path,
        timeout=120,
    )

    assert result.returncode == 2
    assert "invalid-environment" in result.stdout
