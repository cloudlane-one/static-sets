"""The installer command: environment handling, API calls and end-to-end runs."""

from __future__ import annotations

import base64
import os
from pathlib import Path
from typing import Any, cast

import pytest
from kubernetes.client import V1ConfigMap, V1ObjectMeta, V1Pod, V1Secret
from typer.testing import CliRunner

from static_sets.api import ApiRequestError, KubernetesClient
from static_sets.errors import StaticSetsError
from static_sets.installer import ApiUnavailableError
from static_sets.models import StaticSet
from static_sets.naming import COMPONENT_LABEL, PLACEMENT_COMPONENT, PLACEMENT_LABEL, SET_LABEL
from static_sets.naming import placement_configmap_name, placement_identity
import static_sets_installer as installer_cli
from static_sets_installer import (
    Action,
    ClusterApi,
    app,
    assignment_from_environment,
    run,
)

UID = "0f9a4b6c-1111-2222-3333-444455556666"
NAMESPACE = "edge"
SET_NAME = "web"


class FakeCore:
    """The core API group, answering from the cluster's typed collections."""

    def __init__(self, cluster: FakeCluster) -> None:
        self.cluster = cluster

    def read_namespaced_config_map(self, name: str, namespace: str, **kwargs: Any) -> V1ConfigMap:
        """Read a ConfigMap.

        Args:
            name: The ConfigMap's name.
            namespace: The namespace.
            **kwargs: Further client arguments.

        Returns:
            The ConfigMap.

        Raises:
            ApiUnavailableError: If the fake is configured as unreachable.
            ApiRequestError: If it does not exist.
        """
        del namespace, kwargs
        if self.cluster.unavailable:
            raise ApiUnavailableError("connection refused")
        record = self.cluster.config_maps.get(name)
        if record is None:
            raise ApiRequestError(f"ConfigMap {name} does not exist", status=404)
        return record

    def read_namespaced_secret(self, name: str, namespace: str, **kwargs: Any) -> V1Secret:
        """Read a Secret.

        Args:
            name: The Secret's name.
            namespace: The namespace.
            **kwargs: Further client arguments.

        Returns:
            The Secret.

        Raises:
            ApiUnavailableError: If the fake is configured as unreachable.
            ApiRequestError: If it does not exist.
        """
        del namespace, kwargs
        if self.cluster.unavailable:
            raise ApiUnavailableError("connection refused")
        record = self.cluster.secrets.get(name)
        if record is None:
            raise ApiRequestError(f"Secret {name} does not exist", status=404)
        return record

    def create_namespaced_config_map(
        self, namespace: str, body: V1ConfigMap, **kwargs: Any
    ) -> None:
        """Record a ConfigMap creation.

        Args:
            namespace: The namespace.
            body: The object.
            **kwargs: Further client arguments.
        """
        del namespace, kwargs
        self.cluster.record(body)

    def replace_namespaced_config_map(
        self, name: str, namespace: str, body: V1ConfigMap, **kwargs: Any
    ) -> None:
        """Record a ConfigMap replacement.

        Args:
            name: The ConfigMap's name, which the generated signature puts first.
            namespace: The namespace.
            body: The object.
            **kwargs: Further client arguments.
        """
        del namespace, kwargs
        assert body.metadata is not None and body.metadata.name == name
        self.cluster.record(body)

    def patch_namespaced_pod(self, name: str, namespace: str, body: V1Pod, **kwargs: Any) -> None:
        """Record a Pod patch.

        Args:
            name: The Pod's name.
            namespace: The namespace.
            body: The patch.
            **kwargs: Further client arguments.

        Raises:
            ApiUnavailableError: If the fake is configured as unreachable.
        """
        del kwargs
        if self.cluster.unavailable:
            raise ApiUnavailableError("connection refused")
        self.cluster.patched.append((namespace, name, body))

    def delete_namespaced_pod(self, name: str, namespace: str, **kwargs: Any) -> None:
        """Record a Pod deletion.

        Args:
            name: The Pod's name.
            namespace: The namespace.
            **kwargs: Further client arguments.
        """
        self.cluster.deleted.append((namespace, name, kwargs.get("grace_period_seconds", 0)))


class FakeCustom:
    """The custom-objects API group, serving the StaticSet under test."""

    def __init__(self, cluster: FakeCluster) -> None:
        self.cluster = cluster

    def get_namespaced_custom_object(
        self,
        group: str,
        version: str,
        namespace: str,
        plural: str,
        name: str,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Return the served StaticSet's wire form.

        Args:
            group: The API group.
            version: The API version.
            namespace: The namespace.
            plural: The collection name.
            name: The object's name.
            **kwargs: Further client arguments.

        Returns:
            The wire-shaped resource.

        Raises:
            ApiUnavailableError: If the fake is configured as unreachable.
        """
        del group, version, namespace, plural, name, kwargs
        if self.cluster.unavailable:
            raise ApiUnavailableError("connection refused")
        assert self.cluster.static_set is not None, "the fake has no StaticSet to serve"
        return self.cluster.static_set


class FakeCluster:
    """A cluster that serves typed objects and records what the installer writes.

    It stands in for :class:`~static_sets.api.KubernetesClient`, exposing the same
    ``core`` and ``custom`` API groups, so a test describes the objects the API
    would return rather than an HTTP conversation.
    """

    def __init__(
        self,
        static_set: dict[str, Any] | None = None,
        *,
        config_maps: dict[str, V1ConfigMap] | None = None,
        secrets: dict[str, V1Secret] | None = None,
    ) -> None:
        """Build the fake.

        Args:
            static_set: The wire-shaped StaticSet to serve, or ``None`` for a 404.
            config_maps: ConfigMaps by name.
            secrets: Secrets by name.
        """
        self.static_set = static_set
        self.config_maps = dict(config_maps or {})
        self.secrets = dict(secrets or {})
        self.applied: list[Any] = []
        self.patched: list[tuple[str, str, Any]] = []
        self.deleted: list[tuple[str, str, int]] = []
        self.unavailable = False
        self.core = FakeCore(self)
        self.custom = FakeCustom(self)

    def record(self, obj: Any) -> None:
        """Record an applied object.

        Args:
            obj: The object that was written.
        """
        self.applied.append(obj)
        if isinstance(obj, V1ConfigMap) and obj.metadata and obj.metadata.name:
            self.config_maps[obj.metadata.name] = obj


def as_client(cluster: FakeCluster) -> KubernetesClient:
    """Type a fake cluster as the client the installer takes.

    Args:
        cluster: The fake.

    Returns:
        The same object, seen as a client.
    """
    return cast(KubernetesClient, cluster)


def cluster_api(cluster: FakeCluster) -> ClusterApi:
    """Wrap a fake cluster in the byte-oriented placement API.

    Args:
        cluster: The fake.

    Returns:
        The adapter under test.
    """
    return ClusterApi(as_client(cluster))


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
    return placement_identity(UID, "node-a")


def test_cluster_api_reads_and_validates_the_static_set(temp_roots: tuple[Path, Path]) -> None:
    """The StaticSet is validated into the typed model before use."""
    cluster = FakeCluster(static_set_body(temp_roots))

    resource = cluster_api(cluster).get_static_set(NAMESPACE, SET_NAME)

    assert resource.spec.replicas == 1
    assert resource.spec.mode.value == "Deployment"


def test_cluster_api_decodes_config_map_data_and_binary_data() -> None:
    """Text and base64 entries of a ConfigMap become bytes."""
    cluster = FakeCluster(
        config_maps={
            "cfg": V1ConfigMap(
                data={"a": "text"},
                binary_data={"b": base64.b64encode(b"\x00\x01").decode()},
            )
        }
    )

    data = cluster_api(cluster).get_config_map(NAMESPACE, "cfg")

    assert data == {"a": b"text", "b": b"\x00\x01"}


def test_cluster_api_reports_an_absent_config_map() -> None:
    """A 404 becomes ``None`` rather than an empty mapping."""
    assert cluster_api(FakeCluster()).get_config_map(NAMESPACE, "cfg") is None


def test_cluster_api_decodes_secret_data() -> None:
    """Secret values are base64 and are decoded to bytes."""
    cluster = FakeCluster(secrets={"sec": V1Secret(data={"token": "czNjcjN0"})})

    data = cluster_api(cluster).get_secret(NAMESPACE, "sec")

    assert data == {"token": b"s3cr3t"}


def test_cluster_api_creates_then_updates_a_placement() -> None:
    """The acknowledgment object is created on first publish and updated later."""
    cluster = FakeCluster()
    api = cluster_api(cluster)

    api.publish_placement(NAMESPACE, "web-placement-id1", {"placement": "id1"}, static_set="web")
    api.publish_placement(
        NAMESPACE,
        "web-placement-id1",
        {"placement": "id1", "state": "Removed"},
        static_set="web",
    )

    assert len(cluster.applied) == 2
    assert cluster.config_maps["web-placement-id1"].data == {"placement": "id1", "state": "Removed"}


def test_cluster_api_reads_a_published_placement() -> None:
    """The payload the operator published is returned as a plain mapping."""
    cluster = FakeCluster(config_maps={"rec": V1ConfigMap(data={"cleanup": "true"})})

    payload = cluster_api(cluster).get_placement(NAMESPACE, "rec")

    assert payload == {"cleanup": "true"}


def test_release_self_clears_the_finalizer_and_deletes_the_pod() -> None:
    """A finished cleanup worker removes its own finalizer and itself."""
    cluster = FakeCluster()

    cluster_api(cluster).release_self(NAMESPACE, "web-cleanup-id1")

    assert [name for _, name, _ in cluster.patched] == ["web-cleanup-id1"]
    assert cluster.patched[0][2] == V1Pod(metadata=V1ObjectMeta(finalizers=[]))
    assert cluster.deleted == [(NAMESPACE, "web-cleanup-id1", 0)]


def test_release_self_tolerates_a_failure() -> None:
    """Failing to release itself must not fail the cleanup."""
    cluster = FakeCluster()
    cluster.unavailable = True

    cluster_api(cluster).release_self(NAMESPACE, "pod")


def test_the_command_line_parses_the_documented_options(monkeypatch: pytest.MonkeyPatch) -> None:
    """The Typer declaration is the CLI: defaults and flags are what it says."""
    captured: dict[str, Any] = {}

    def fake_run(action: Action, **kwargs: Any) -> int:
        captured.update(action=action, **kwargs)
        return 0

    monkeypatch.setattr(installer_cli, "run", fake_run)

    result = CliRunner().invoke(
        app,
        ["cleanup", "--timeout", "2", "--interval", "5", "--workload-running"],
    )

    assert result.exit_code == 0
    assert captured == {
        "action": Action.CLEANUP,
        "timeout": 2.0,
        "interval": 5.0,
        "workload_running": True,
    }


def test_the_command_line_rejects_an_unknown_action() -> None:
    """An action outside the two the installer has is refused by the parser."""
    result = CliRunner().invoke(app, ["install"])

    assert result.exit_code == 2


def serving_cluster(temp_roots: tuple[Path, Path], **spec_overrides: Any) -> FakeCluster:
    """Return a cluster serving the installer's reads.

    Args:
        temp_roots: The (manifests, bundle) pair from the fixture.
        **spec_overrides: Extra spec fields for the served StaticSet.

    Returns:
        The fake cluster.
    """
    return FakeCluster(
        static_set_body(temp_roots, **dict(spec_overrides)),
        config_maps={"web-conf": V1ConfigMap(data={"app.conf": "greeting=hello\n"})},
    )


def test_main_reconciles_a_placement_end_to_end(temp_roots: tuple[Path, Path]) -> None:
    """The command installs the static Pod and acknowledges it."""
    cluster = serving_cluster(temp_roots)

    status = run(
        Action.RECONCILE,
        environ=environment(temp_roots),
        client=as_client(cluster),
        now="2026-10-12T10:00:00+00:00",
        interval=0.0,
    )

    assert status == 0
    manifests, _ = temp_roots
    written = list(manifests.glob("*.yaml"))
    assert len(written) == 1
    assert written[0].name.startswith("web-")
    acknowledgments = [item for item in cluster.applied if isinstance(item, V1ConfigMap)]
    assert acknowledgments
    assert (acknowledgments[0].data or {})["state"] == "Installed"


def test_main_treats_an_unreachable_api_as_a_non_failure(
    temp_roots: tuple[Path, Path],
) -> None:
    """Being offline is a state to report, not a crash: the local state is kept."""
    cluster = serving_cluster(temp_roots)
    cluster.unavailable = True

    status = run(
        Action.RECONCILE,
        environ=environment(temp_roots),
        client=as_client(cluster),
        interval=0.0,
    )

    assert status == 0
    manifests, _ = temp_roots
    assert list(manifests.glob("*.yaml")) == []


def test_main_reports_an_incomplete_environment(temp_roots: tuple[Path, Path]) -> None:
    """A missing environment variable is a configuration error, not a reconcile."""
    incomplete = environment(temp_roots)
    del incomplete["STATIC_SETS_UID"]

    assert run(Action.RECONCILE, environ=incomplete) == 2


def test_main_fails_closed_when_images_cannot_be_verified(
    temp_roots: tuple[Path, Path],
) -> None:
    """A revision whose images are unconfirmed must not be published."""
    cluster = serving_cluster(temp_roots, offline={"imageProvisioning": "VerifiedBeforeInstall"})

    status = run(
        Action.RECONCILE,
        environ=environment(temp_roots),
        client=as_client(cluster),
        now="2026-10-12T10:00:00+00:00",
        interval=0.0,
    )

    assert status == 1
    manifests, _ = temp_roots
    assert list(manifests.glob("*.yaml")) == []


def cleanup_payload() -> dict[str, str]:
    """Return the cleanup intent the operator would publish.

    Returns:
        The intent's data.
    """
    identity = derived_identity()
    return {
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


def test_main_runs_a_cleanup_and_releases_itself(temp_roots: tuple[Path, Path]) -> None:
    """A cleanup worker removes the workload, acknowledges, and clears its finalizer."""
    cluster = serving_cluster(temp_roots)
    run(
        Action.RECONCILE,
        environ=environment(temp_roots),
        client=as_client(cluster),
        now="2026-10-12T10:00:00+00:00",
        interval=0.0,
    )
    manifests, _ = temp_roots
    assert list(manifests.glob("*.yaml"))

    identity = derived_identity()
    record = cluster.config_maps[placement_configmap_name(SET_NAME, identity)]
    record.data = cleanup_payload()

    status = run(
        Action.CLEANUP,
        environ=environment(temp_roots, STATIC_SETS_PLACEMENT=identity),
        client=as_client(cluster),
        now="2026-10-12T10:00:00+00:00",
    )

    assert status == 0
    assert list(manifests.glob("*.yaml")) == []
    assert [name for _, name, _ in cluster.patched] == ["web-installer-abc"]
    assert cluster.deleted == [(NAMESPACE, "web-installer-abc", 0)]


def test_main_with_a_running_workload_keeps_its_configuration(
    temp_roots: tuple[Path, Path],
) -> None:
    """A worker told the workload is still running does not delete its files."""
    cluster = serving_cluster(temp_roots)
    run(
        Action.RECONCILE,
        environ=environment(temp_roots),
        client=as_client(cluster),
        now="2026-10-12T10:00:00+00:00",
        interval=0.0,
    )
    manifests, bundle = temp_roots
    revisions = list((bundle / "placements").glob("*/revisions/*"))
    assert revisions

    identity = derived_identity()
    record = cluster.config_maps[placement_configmap_name(SET_NAME, identity)]
    record.data = cleanup_payload()

    status = run(
        Action.CLEANUP,
        environ=environment(temp_roots, STATIC_SETS_PLACEMENT=identity),
        client=as_client(cluster),
        now="2026-10-12T10:00:00+00:00",
        workload_running=True,
    )

    assert status == 0
    assert list(manifests.glob("*.yaml")) == []
    assert list((bundle / "placements").glob("*/revisions/*"))


def test_the_module_entry_point_runs_the_command(tmp_path: Path) -> None:
    """``python -m static_sets_installer`` is the documented way to start it."""
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


def test_a_published_placement_carries_the_labels_the_operator_selects_by() -> None:
    """The operator finds acknowledgments by label, so the labels have to agree.

    They did not: the installer wrote a label set of its own, the operator selected on
    one the installer never wrote, and no acknowledgment was ever observed. Both sides
    now take the keys from the same module, and this pins that the record really is
    written with them.
    """
    cluster = FakeCluster()

    cluster_api(cluster).publish_placement(
        NAMESPACE, "web-placement-id1", {"placement": "id1"}, static_set="web"
    )

    written = cluster.applied[0]
    labels = written.metadata.labels
    assert labels[SET_LABEL] == "web"
    assert labels[COMPONENT_LABEL] == PLACEMENT_COMPONENT
    assert labels[PLACEMENT_LABEL] == "id1"


def test_a_republished_placement_keeps_the_labels_the_operator_wrote() -> None:
    """An acknowledgment merges into the record instead of replacing its labels."""
    existing = V1ConfigMap(
        metadata=V1ObjectMeta(
            name="web-placement-id1",
            labels={"staticsets.cloudlane.dev/set": "web", "kept": "yes"},
            annotations={"note": "from the operator"},
        )
    )
    cluster = FakeCluster(config_maps={"web-placement-id1": existing})
    api = cluster_api(cluster)

    api.publish_placement(NAMESPACE, "web-placement-id1", {"placement": "id1"}, static_set="web")

    updated = cluster.applied[0]
    assert updated.metadata.labels["kept"] == "yes"
    assert updated.metadata.annotations == {"note": "from the operator"}


def test_the_installer_keeps_reconciling_until_it_is_stopped(
    monkeypatch: pytest.MonkeyPatch, temp_roots: tuple[Path, Path]
) -> None:
    """A placement has to notice a cleanup intent published after its last pass.

    Running one pass and exiting would leave that discovery to kubelet's container
    restart backoff, which grows to five minutes — long enough for a pending cleanup
    to look like a stuck one. The pass runs, then the loop waits the interval, and
    being stopped is a clean exit rather than a failure.
    """
    cluster = serving_cluster(temp_roots)
    waited: list[float] = []

    def stop_after_the_wait(seconds: float) -> None:
        waited.append(seconds)
        raise KeyboardInterrupt

    monkeypatch.setattr(installer_cli.time, "sleep", stop_after_the_wait)

    status = run(
        Action.RECONCILE,
        environ=environment(temp_roots),
        client=as_client(cluster),
        now="2026-10-12T10:00:00+00:00",
        interval=7.0,
    )

    assert status == 0
    assert waited == [7.0]
    manifests, _ = temp_roots
    assert list(manifests.glob("*.yaml")), "the pass ran before the loop waited"
