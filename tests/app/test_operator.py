"""The controller adapter: snapshot reading, plan application and admission.

The entry point is loaded the way ``kopf run`` loads it and the way
``based-operators scaffold`` loads it, which proves in one step that the file really
is a deployable operator entry point: the framework finds exactly the StaticSet
resource it declares.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any, cast

import kopf
import pytest
from based_operators.scaffold import discover_resources, load_operator
from kubernetes.client import (
    V1ConfigMap,
    V1ConfigMapList,
    V1DaemonSet,
    V1DaemonSetList,
    V1Deployment,
    V1DeploymentList,
    V1Node,
    V1NodeList,
    V1Pod,
    V1PodList,
)

import static_sets_operator as operator
from static_sets.api import ApiRequestError, KubernetesClient

REPO_ROOT = Path(__file__).resolve().parents[2]
ENTRY_POINT = REPO_ROOT / "app" / "static_sets_operator.py"
NAMESPACE = "edge"
SET_NAME = "web"

LOGGER = logging.getLogger("static-sets-test")


class FakeCore:
    """The core API group, answering from the adapter's typed collections."""

    def __init__(self, cluster: FakeCluster) -> None:
        self.cluster = cluster

    def list_namespaced_pod(self, namespace: str, **kwargs: Any) -> V1PodList:
        """List the configured Pods.

        Args:
            namespace: The namespace.
            **kwargs: The selector and further client arguments.

        Returns:
            The Pods.
        """
        del namespace
        self.cluster.selectors.append(kwargs.get("label_selector"))
        return V1PodList(items=self.cluster.pods)

    def list_node(self, **kwargs: Any) -> V1NodeList:
        """List the configured Nodes.

        Args:
            **kwargs: Further client arguments.

        Returns:
            The Nodes.
        """
        del kwargs
        return V1NodeList(items=self.cluster.nodes)

    def list_namespaced_config_map(self, namespace: str, **kwargs: Any) -> V1ConfigMapList:
        """List the configured ConfigMaps.

        Args:
            namespace: The namespace.
            **kwargs: The selector and further client arguments.

        Returns:
            The ConfigMaps.
        """
        del namespace
        self.cluster.selectors.append(kwargs.get("label_selector"))
        return V1ConfigMapList(items=self.cluster.config_maps)

    def read_namespaced_config_map(self, name: str, namespace: str, **kwargs: Any) -> V1ConfigMap:
        """Read a ConfigMap.

        Args:
            name: The ConfigMap's name.
            namespace: The namespace.
            **kwargs: Further client arguments.

        Returns:
            The ConfigMap.

        Raises:
            ApiRequestError: If it does not exist.
        """
        del namespace, kwargs
        for item in self.cluster.config_maps:
            if item.metadata is not None and item.metadata.name == name:
                return item
        raise ApiRequestError(f"ConfigMap {name} does not exist", status=404)

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
        self.cluster.applied.append(body)
        self.cluster.config_maps.append(body)

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
        self.cluster.applied.append(body)
        self.cluster.replaced.append(body)

    def read_namespaced_pod(self, name: str, namespace: str, **kwargs: Any) -> V1Pod:
        """Read a Pod.

        Args:
            name: The Pod's name.
            namespace: The namespace.
            **kwargs: Further client arguments.

        Returns:
            The Pod.

        Raises:
            ApiRequestError: If it does not exist.
        """
        del namespace, kwargs
        for item in self.cluster.pods:
            if item.metadata is not None and item.metadata.name == name:
                return item
        raise ApiRequestError(f"Pod {name} does not exist", status=404)

    def create_namespaced_pod(self, namespace: str, body: V1Pod, **kwargs: Any) -> None:
        """Record a Pod creation.

        Args:
            namespace: The namespace.
            body: The object.
            **kwargs: Further client arguments.
        """
        del namespace, kwargs
        self.cluster.applied.append(body)
        self.cluster.pods.append(body)

    def replace_namespaced_pod(self, name: str, namespace: str, body: V1Pod, **kwargs: Any) -> None:
        """Record a Pod replacement.

        Args:
            name: The Pod's name, which the generated signature puts first.
            namespace: The namespace.
            body: The object.
            **kwargs: Further client arguments.
        """
        del namespace, kwargs
        assert body.metadata is not None and body.metadata.name == name
        self.cluster.applied.append(body)
        self.cluster.replaced.append(body)


class FakeApps:
    """The apps API group, answering from the adapter's typed collections."""

    def __init__(self, cluster: FakeCluster) -> None:
        self.cluster = cluster

    def list_namespaced_deployment(self, namespace: str, **kwargs: Any) -> V1DeploymentList:
        """List the configured Deployments.

        Args:
            namespace: The namespace.
            **kwargs: The selector and further client arguments.

        Returns:
            The Deployments.
        """
        del namespace
        self.cluster.selectors.append(kwargs.get("label_selector"))
        return V1DeploymentList(items=self.cluster.deployments)

    def list_namespaced_daemon_set(self, namespace: str, **kwargs: Any) -> V1DaemonSetList:
        """List the configured DaemonSets.

        Args:
            namespace: The namespace.
            **kwargs: The selector and further client arguments.

        Returns:
            The DaemonSets.
        """
        del namespace
        self.cluster.selectors.append(kwargs.get("label_selector"))
        return V1DaemonSetList(items=self.cluster.daemon_sets)

    def read_namespaced_deployment(self, name: str, namespace: str, **kwargs: Any) -> V1Deployment:
        """Read a Deployment.

        Args:
            name: The Deployment's name.
            namespace: The namespace.
            **kwargs: Further client arguments.

        Returns:
            The Deployment.

        Raises:
            ApiRequestError: If it does not exist.
        """
        del namespace, kwargs
        for item in self.cluster.deployments:
            if item.metadata is not None and item.metadata.name == name:
                return item
        raise ApiRequestError(f"Deployment {name} does not exist", status=404)

    def create_namespaced_deployment(
        self, namespace: str, body: V1Deployment, **kwargs: Any
    ) -> None:
        """Record a Deployment creation.

        Args:
            namespace: The namespace.
            body: The object.
            **kwargs: Further client arguments.
        """
        del namespace, kwargs
        self.cluster.applied.append(body)
        self.cluster.deployments.append(body)

    def replace_namespaced_deployment(
        self, name: str, namespace: str, body: V1Deployment, **kwargs: Any
    ) -> None:
        """Record a Deployment replacement.

        Args:
            name: The Deployment's name, which the generated signature puts first.
            namespace: The namespace.
            body: The object.
            **kwargs: Further client arguments.
        """
        del namespace, kwargs
        assert body.metadata is not None and body.metadata.name == name
        self.cluster.applied.append(body)
        self.cluster.replaced.append(body)

    def read_namespaced_daemon_set(self, name: str, namespace: str, **kwargs: Any) -> V1DaemonSet:
        """Read a DaemonSet.

        Args:
            name: The DaemonSet's name.
            namespace: The namespace.
            **kwargs: Further client arguments.

        Returns:
            The DaemonSet.

        Raises:
            ApiRequestError: If it does not exist.
        """
        del namespace, kwargs
        for item in self.cluster.daemon_sets:
            if item.metadata is not None and item.metadata.name == name:
                return item
        raise ApiRequestError(f"DaemonSet {name} does not exist", status=404)

    def create_namespaced_daemon_set(
        self, namespace: str, body: V1DaemonSet, **kwargs: Any
    ) -> None:
        """Record a DaemonSet creation.

        Args:
            namespace: The namespace.
            body: The object.
            **kwargs: Further client arguments.
        """
        del namespace, kwargs
        self.cluster.applied.append(body)
        self.cluster.daemon_sets.append(body)

    def replace_namespaced_daemon_set(
        self, name: str, namespace: str, body: V1DaemonSet, **kwargs: Any
    ) -> None:
        """Record a DaemonSet replacement.

        Args:
            name: The DaemonSet's name, which the generated signature puts first.
            namespace: The namespace.
            body: The object.
            **kwargs: Further client arguments.
        """
        del namespace, kwargs
        assert body.metadata is not None and body.metadata.name == name
        self.cluster.applied.append(body)
        self.cluster.replaced.append(body)

    def delete_namespaced_deployment(self, name: str, namespace: str, **kwargs: Any) -> None:
        """Record a Deployment deletion.

        Args:
            name: The Deployment's name.
            namespace: The namespace.
            **kwargs: Further client arguments.
        """
        del namespace, kwargs
        self.cluster.deleted.append(("Deployment", name))

    def delete_namespaced_daemon_set(self, name: str, namespace: str, **kwargs: Any) -> None:
        """Record a DaemonSet deletion.

        Args:
            name: The DaemonSet's name.
            namespace: The namespace.
            **kwargs: Further client arguments.
        """
        del namespace, kwargs
        self.cluster.deleted.append(("DaemonSet", name))


class FakeCluster:
    """A cluster that answers from typed collections and records what it is told.

    It stands in for :class:`~static_sets.api.KubernetesClient`, exposing the same
    ``core`` and ``apps`` API groups, so a test describes the objects the API would
    return and asserts on the models the adapter applies.
    """

    def __init__(
        self,
        *,
        pods: Iterable[Mapping[str, Any]] = (),
        nodes: Iterable[Mapping[str, Any]] = (),
        config_maps: Iterable[Mapping[str, Any]] = (),
        deployments: Iterable[Mapping[str, Any]] = (),
        daemon_sets: Iterable[Mapping[str, Any]] = (),
    ) -> None:
        """Build the fake.

        Args:
            pods: Wire-shaped Pods the lists return.
            nodes: Wire-shaped Nodes the lists return.
            config_maps: Wire-shaped ConfigMaps the lists return.
            deployments: Wire-shaped Deployments the lists return.
            daemon_sets: Wire-shaped DaemonSets the lists return.
        """
        self.pods = [V1Pod.model_validate(item) for item in pods]
        self.nodes = [V1Node.model_validate(item) for item in nodes]
        self.config_maps = [V1ConfigMap.model_validate(item) for item in config_maps]
        self.deployments = [V1Deployment.model_validate(item) for item in deployments]
        self.daemon_sets = [V1DaemonSet.model_validate(item) for item in daemon_sets]
        self.applied: list[Any] = []
        self.replaced: list[Any] = []
        self.deleted: list[tuple[str, str]] = []
        self.selectors: list[str | None] = []
        self.core = FakeCore(self)
        self.apps = FakeApps(self)


def as_client(cluster: FakeCluster) -> KubernetesClient:
    """Type a fake cluster as the client the adapter takes.

    Args:
        cluster: The fake.

    Returns:
        The same object, seen as a client.
    """
    return cast(KubernetesClient, cluster)


def test_entry_point_is_a_deployable_operator() -> None:
    """The scaffolder finds exactly the StaticSet the entry point declares."""
    module = load_operator(ENTRY_POINT)

    resources = discover_resources(module)

    assert [resource.__name__ for resource in resources] == ["StaticSet"]
    assert resources[0]._require_resource_config().crd_name == (
        "staticsets.staticsets.cloudlane.dev"
    )


def test_selector_renders_a_stable_label_selector() -> None:
    """Selectors are sorted, so a reconcile is comparable between runs."""
    assert operator.selector(b="2", a="1") == "a=1,b=2"


def test_read_snapshot_maps_every_observed_kind(make_static_set: Any) -> None:
    """Pods, nodes and placement ConfigMaps become the planner's input types."""
    identity = "abc123"
    client = FakeCluster(
        nodes=[
            {
                "metadata": {"name": "node-a", "labels": {"role": "edge"}},
                "spec": {},
                "status": {"conditions": [{"type": "Ready", "status": "True"}]},
            },
            {
                "metadata": {"name": "node-b", "labels": {}},
                "spec": {"unschedulable": True},
                "status": {"conditions": [{"type": "Ready", "status": "False"}]},
            },
        ],
        pods=[
            {
                "metadata": {
                    "name": "web-installer-1",
                    "uid": "u1",
                    "labels": {"app.kubernetes.io/component": "installer"},
                },
                "spec": {"nodeName": "node-a", "containers": [{"name": "c", "image": "i"}]},
                "status": {"phase": "Running"},
            },
            {
                "metadata": {
                    "name": f"web-{identity}",
                    "labels": {"app.kubernetes.io/component": "workload"},
                    "annotations": {"kubernetes.io/config.mirror": "hash"},
                },
                "spec": {"nodeName": "node-a", "containers": [{"name": "c", "image": "i"}]},
                "status": {
                    "phase": "Running",
                    "containerStatuses": [
                        {
                            "name": "app",
                            "image": "i",
                            "imageID": "sha256:0",
                            "ready": True,
                            "restartCount": 2,
                        }
                    ],
                },
            },
        ],
        config_maps=[
            {
                "data": {
                    "placement": identity,
                    "node": "node-a",
                    "revision": "r-1",
                    "state": "Installed",
                    "acknowledged": "true",
                }
            }
        ],
    )

    state = operator.read_snapshot(as_client(client), make_static_set(spec={"replicas": 1}))

    assert state.synced is True
    assert [(node.name, node.ready, node.unschedulable) for node in state.nodes] == [
        ("node-a", True, False),
        ("node-b", False, True),
    ]
    assert [pod.node for pod in state.installer_pods] == ["node-a"]
    assert state.placements[0].identity == identity
    assert state.placements[0].acknowledged is True
    assert state.workloads[0].ready is True
    assert state.workloads[0].restart_count == 2


def test_read_snapshot_skips_a_mirror_pod_as_an_installer(make_static_set: Any) -> None:
    """A mirror Pod is never mistaken for a live installer."""
    client = FakeCluster(
        pods=[
            {
                "metadata": {
                    "name": "web-abc",
                    "labels": {"app.kubernetes.io/component": "installer"},
                    "annotations": {"kubernetes.io/config.mirror": "hash"},
                },
                "spec": {"nodeName": "node-a", "containers": [{"name": "c", "image": "i"}]},
            }
        ]
    )

    state = operator.read_snapshot(as_client(client), make_static_set())

    assert state.installer_pods == ()
    assert len(state.workloads) == 1


def test_apply_plan_creates_the_installer_workload(make_static_set: Any) -> None:
    """A first reconcile applies the installer Deployment as the client's model."""
    client = FakeCluster()
    resource = make_static_set(spec={"replicas": 1})

    result = operator.apply_plan(
        as_client(client),
        resource,
        operator.ObservedState(observed_at="2026-10-12T10:00:00Z"),
        NAMESPACE,
    )

    assert result.workload is not None
    assert len(client.applied) == 1
    deployment = client.applied[0]
    assert isinstance(deployment, V1Deployment)
    assert deployment.spec.replicas == 1


def test_apply_plan_replaces_an_existing_workload(make_static_set: Any) -> None:
    """A second reconcile replaces the Deployment, carrying its resource version.

    The generated client's replace method takes the name before the namespace. A fake
    that mirrored the wrong order hid that until the end-to-end suite caught it.
    """
    name = "web-installer-deployment"
    client = FakeCluster(
        deployments=[
            {
                "metadata": {"name": name, "resourceVersion": "42"},
                "spec": {
                    "selector": {"matchLabels": {}},
                    "template": {"spec": {"containers": [{"name": "c", "image": "i"}]}},
                },
            }
        ]
    )
    resource = make_static_set(spec={"replicas": 1})

    operator.apply_plan(
        as_client(client),
        resource,
        operator.ObservedState(observed_at="x"),
        NAMESPACE,
    )

    assert len(client.applied) == 1
    replaced = client.replaced[0]
    assert isinstance(replaced, V1Deployment)
    assert replaced.metadata is not None
    assert replaced.metadata.name == name
    assert replaced.metadata.resource_version == "42"


def test_apply_plan_publishes_a_cleanup_intent_and_a_worker(make_static_set: Any) -> None:
    """An orphaned placement gets an intent ConfigMap and a pinned worker Pod."""
    from static_sets.placement import ObservedPlacement

    client = FakeCluster()
    resource = make_static_set(spec={"replicas": 1})
    state = operator.ObservedState(
        nodes=(operator.NodeInfo(name="node-a", labels={}, ready=True),),
        placements=(
            ObservedPlacement(
                identity="abc123",
                node="node-a",
                revision="r-1",
                state="Installed",
                acknowledged=True,
            ),
        ),
        observed_at="2026-10-12T10:00:00Z",
    )

    operator.apply_plan(as_client(client), resource, state, NAMESPACE)

    kinds = sorted(type(item).__name__ for item in client.applied)
    assert kinds == ["V1ConfigMap", "V1Deployment", "V1Pod"]
    config_map = next(item for item in client.applied if isinstance(item, V1ConfigMap))
    assert (config_map.data or {})["cleanup"] == "true"
    worker = next(item for item in client.applied if isinstance(item, V1Pod))
    assert worker.spec is not None and worker.spec.node_name == "node-a"


def test_apply_plan_deletes_both_installer_kinds(make_static_set: Any) -> None:
    """Deletion clears a Deployment and a DaemonSet, so a mode switch leaves nothing."""
    workload_spec = {
        "selector": {"matchLabels": {}},
        "template": {"spec": {"containers": [{"name": "c", "image": "i"}]}},
    }
    client = FakeCluster(
        deployments=[{"metadata": {"name": "web-installer-deployment"}, "spec": workload_spec}],
        daemon_sets=[{"metadata": {"name": "web-installer-daemonset"}, "spec": workload_spec}],
    )
    resource = make_static_set(
        spec={"replicas": 1}, metadata={"deletionTimestamp": "2026-10-12T11:00:00Z"}
    )

    operator.apply_plan(
        as_client(client),
        resource,
        operator.ObservedState(observed_at="x"),
        NAMESPACE,
    )

    assert sorted(client.deleted) == [
        ("DaemonSet", "web-installer-daemonset"),
        ("Deployment", "web-installer-deployment"),
    ]


def test_publish_status_writes_every_reported_field(make_static_set: Any) -> None:
    """Status is published field by field, so a merge patch never drops a key."""
    from static_sets.controller import ObservedState, plan

    resource = make_static_set(spec={"replicas": 1})
    result = plan(resource, ObservedState(observed_at="2026-10-12T10:00:00Z"), namespace=NAMESPACE)
    patch = kopf.Patch()

    operator.publish_status(patch, result)

    written = dict(patch.status)
    assert set(written) >= {
        "observedGeneration",
        "placements",
        "health",
        "cleanup",
        "conditions",
    }
    assert written["observedGeneration"] == 3
    assert isinstance(written["conditions"], list)


def test_ensure_finalizer_adds_it_once(make_static_set: Any) -> None:
    """The finalizer is added, and an object that already has it is left alone."""
    patch = kopf.Patch()

    operator.ensure_finalizer(patch, make_static_set())

    assert dict(patch.metadata)["finalizers"] == ["staticsets.cloudlane.dev/static-set"]

    already = make_static_set(
        metadata={"finalizers": ["staticsets.cloudlane.dev/static-set", "other"]}
    )
    untouched = kopf.Patch()

    operator.ensure_finalizer(untouched, already)

    assert dict(untouched.metadata) == {}


def test_reject_unsupported_accepts_a_valid_object(make_resource: Any) -> None:
    """A well-formed object passes admission."""
    operator.reject_unsupported(make_resource())


def test_reject_unsupported_refuses_an_unknown_field(make_resource: Any) -> None:
    """An unknown field is refused instead of being pruned."""
    body = make_resource()
    body["spec"]["template"]["spec"]["containers"][0]["lifecycle"] = {}

    with pytest.raises(kopf.AdmissionError) as error:
        operator.reject_unsupported(body)

    assert "lifecycle" in str(error.value)


def test_reconcile_retries_when_the_api_is_unreachable(
    make_static_set: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unreachable API is retried, never interpreted as an empty desired set."""
    from static_sets.installer import ApiUnavailableError

    class BrokenCore(FakeCore):
        def list_namespaced_pod(self, namespace: str, **kwargs: Any) -> V1PodList:
            raise ApiUnavailableError("connection refused")

    class Broken(FakeCluster):
        def __init__(self) -> None:
            super().__init__()
            self.core = BrokenCore(self)

    monkeypatch.setattr(operator, "get_client", lambda: Broken())
    patch = kopf.Patch()

    with pytest.raises(kopf.TemporaryError):
        operator.reconcile(make_static_set(), patch, LOGGER)

    assert dict(patch.status) == {}
    assert dict(patch.metadata) == {}


def test_reconcile_applies_and_records_status(
    make_static_set: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A successful reconcile writes the workload and the status."""
    client = FakeCluster()
    monkeypatch.setattr(operator, "get_client", lambda: client)
    patch = kopf.Patch()

    operator.reconcile(make_static_set(spec={"replicas": 1}), patch, LOGGER)

    assert any(isinstance(item, V1Deployment) for item in client.applied)
    assert dict(patch.status)["observedGeneration"] == 3
    assert dict(patch.metadata)["finalizers"] == ["staticsets.cloudlane.dev/static-set"]


def test_finalize_releases_the_finalizer_only_when_cleanup_is_acknowledged(
    make_static_set: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pending cleanup keeps the finalizer, so the object is not released early."""
    client = FakeCluster(
        config_maps=[{"data": {"placement": "abc", "node": "node-a", "state": "Installed"}}]
    )
    monkeypatch.setattr(operator, "get_client", lambda: client)
    resource = make_static_set(
        spec={"replicas": 1},
        metadata={
            "deletionTimestamp": "2026-10-12T11:00:00Z",
            "finalizers": ["staticsets.cloudlane.dev/static-set"],
        },
    )

    with pytest.raises(kopf.TemporaryError):
        operator.finalize(resource, kopf.Patch(), LOGGER)


def test_finalize_releases_the_finalizer_when_cleanup_is_done(
    make_static_set: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An acknowledged cleanup drops the finalizer, letting the object go."""
    client = FakeCluster()
    monkeypatch.setattr(operator, "get_client", lambda: client)
    resource = make_static_set(
        spec={"replicas": 1},
        metadata={
            "deletionTimestamp": "2026-10-12T11:00:00Z",
            "finalizers": ["staticsets.cloudlane.dev/static-set", "other"],
        },
    )
    patch = kopf.Patch()

    operator.finalize(resource, patch, LOGGER)

    assert dict(patch.metadata)["finalizers"] == ["other"]


ADMISSION_PROBE = (
    "import runpy, kopf;"
    "runpy.run_path('app/static_sets_operator.py');"
    "print(len(kopf.get_default_registry()._webhooks.get_all_handlers()))"
)
"""Counts the admission handlers a fresh interpreter registers on import."""


def admission_handler_count(webhook: str | None) -> int:
    """Import the entry point in a fresh interpreter and count its admission handlers.

    Registration happens at import time, so it cannot be observed in this process:
    the module is already in ``sys.modules`` and its registry already populated.

    Args:
        webhook: The value of the operator's webhook environment variable, or
            ``None`` to leave it unset.

    Returns:
        The number of admission handlers the import registered.
    """
    environment = dict(os.environ)
    environment.pop(operator.WEBHOOK_ENV, None)
    if webhook is not None:
        environment[operator.WEBHOOK_ENV] = webhook
    environment["PYTHONPATH"] = os.pathsep.join([str(REPO_ROOT / "src"), str(REPO_ROOT / "app")])
    result = subprocess.run(
        [sys.executable, "-c", ADMISSION_PROBE],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        env=environment,
        timeout=300,
    )
    assert result.returncode == 0, result.stderr
    return int(result.stdout.strip())


def test_the_admission_handler_is_registered_only_while_admissions_are_served() -> None:
    """kopf refuses to start when admission handlers exist with nothing to serve them.

    The chart's default deployment does not serve admissions, so registering the
    handler unconditionally leaves the controller in a crash loop — which the
    end-to-end suite caught and no unit test could, because the handler function
    itself behaves correctly either way. This pins the registration, not the handler.
    """
    assert admission_handler_count(None) == 0
    assert admission_handler_count("0") == 0
    assert admission_handler_count("1") == 1
