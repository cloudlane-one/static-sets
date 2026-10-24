"""The controller adapter: reading a snapshot and applying a plan."""

from __future__ import annotations

from typing import Any

import kopf
import pytest
from kubernetes.client import (
    V1DaemonSet,
    V1DaemonSetSpec,
    V1DaemonSetStatus,
    V1LabelSelector,
    V1ObjectMeta,
    V1OwnerReference,
    V1Pod,
    V1PodSpec,
    V1PodTemplateSpec,
    V1PodStatus,
    V1ReplicaSet,
    V1ReplicaSetSpec,
    V1ReplicaSetStatus,
)

import static_sets_operator as operator
from static_sets.api import ApiRequestError, KubernetesClient
from static_sets.controller import ObservedState
from static_sets.naming import (
    COMPONENT_LABEL,
    GENERATION_LABEL,
    MANAGED_BY,
    MIRROR_ANNOTATION,
    PLACEMENT_LABEL,
    PROBE_COMPONENT,
    SET_LABEL,
    SET_UID_LABEL,
    STATIC_SET_FINALIZER,
    placement_identity,
)

UID = "0f9a4b6c-1111-2222-3333-444455556666"


class FakeCore:
    """The ``core`` API group, over in-memory lists."""

    def __init__(self, cluster: FakeCluster) -> None:
        """Bind the group to its cluster.

        Args:
            cluster: The owning fake.
        """
        self.cluster = cluster

    def list_namespaced_pod(self, namespace: str, label_selector: str = "") -> Any:
        """Return the Pods matching a selector."""
        return _Listing(
            [pod for pod in self.cluster.pods if _matches(pod.metadata, label_selector)]
        )

    def list_node(self) -> Any:
        """Return the nodes."""
        return _Listing(self.cluster.nodes)

    def read_namespaced_pod(self, name: str, namespace: str) -> Any:
        """Return one Pod, or fail as the client does."""
        for pod in self.cluster.pods:
            if pod.metadata.name == name:
                return pod
        raise ApiRequestError("not found", status=404)


class FakeApps:
    """The ``apps`` API group, over in-memory lists."""

    def __init__(self, cluster: FakeCluster) -> None:
        """Bind the group to its cluster.

        Args:
            cluster: The owning fake.
        """
        self.cluster = cluster

    def list_namespaced_replica_set(self, namespace: str, label_selector: str = "") -> Any:
        """Return the ReplicaSets matching a selector."""
        return _Listing(
            [item for item in self.cluster.replica_sets if _matches(item.metadata, label_selector)]
        )

    def list_namespaced_daemon_set(self, namespace: str, label_selector: str = "") -> Any:
        """Return the DaemonSets matching a selector."""
        return _Listing(
            [item for item in self.cluster.daemon_sets if _matches(item.metadata, label_selector)]
        )

    def read_namespaced_replica_set(self, name: str, namespace: str) -> Any:
        """Return one ReplicaSet, or fail as the client does."""
        for item in self.cluster.replica_sets:
            if item.metadata.name == name:
                return item
        raise ApiRequestError("not found", status=404)

    def create_namespaced_replica_set(self, namespace: str, body: Any) -> Any:
        """Create a ReplicaSet."""
        self.cluster.replica_sets.append(body)
        self.cluster.created.append(("ReplicaSet", body.metadata.name))
        return body

    def replace_namespaced_replica_set(self, name: str, namespace: str, body: Any) -> Any:
        """Replace a ReplicaSet."""
        self.cluster.replica_sets = [
            item for item in self.cluster.replica_sets if item.metadata.name != name
        ]
        self.cluster.replica_sets.append(body)
        return body

    def delete_namespaced_replica_set(self, name: str, namespace: str, body: Any = None) -> Any:
        """Delete a ReplicaSet, or fail with 404 when it is already gone."""
        for item in self.cluster.replica_sets:
            if item.metadata.name == name:
                self.cluster.replica_sets.remove(item)
                self.cluster.deleted.append(("ReplicaSet", name))
                return None
        raise ApiRequestError("not found", status=404)

    def delete_namespaced_daemon_set(self, name: str, namespace: str, body: Any = None) -> Any:
        """Delete a DaemonSet, or fail with 404 when it is already gone."""
        for item in self.cluster.daemon_sets:
            if item.metadata.name == name:
                self.cluster.daemon_sets.remove(item)
                self.cluster.deleted.append(("DaemonSet", name))
                return None
        raise ApiRequestError("not found", status=404)


class FakeCustom:
    """The custom-objects API group, over in-memory placements."""

    def __init__(self, cluster: FakeCluster) -> None:
        """Bind the group to its cluster.

        Args:
            cluster: The owning fake.
        """
        self.cluster = cluster

    def list_namespaced_custom_object(
        self,
        group: str,
        version: str,
        namespace: str,
        plural: str,
        label_selector: str = "",
    ) -> Any:
        """Return the placement bodies matching a selector."""
        return {
            "items": [
                body
                for body in self.cluster.placements
                if _matches(_metadata(body), label_selector)
            ]
        }

    def get_namespaced_custom_object(
        self, group: str, version: str, namespace: str, plural: str, name: str
    ) -> Any:
        """Return one placement body, or fail with 404."""
        for body in self.cluster.placements:
            if _metadata(body).name == name:
                return body
        raise ApiRequestError("not found", status=404)

    def create_namespaced_custom_object(
        self, group: str, version: str, namespace: str, plural: str, body: Any
    ) -> Any:
        """Create a placement."""
        self.cluster.placements.append(body)
        self.cluster.created.append(("StaticPodPlacement", body["metadata"]["name"]))
        return body

    def patch_namespaced_custom_object(
        self, group: str, version: str, namespace: str, plural: str, name: str, body: Any
    ) -> Any:
        """Merge-patch a placement's spec."""
        for index, existing in enumerate(self.cluster.placements):
            if _metadata(existing).name == name:
                patched = {**existing, **body}
                patched["metadata"] = {
                    **existing.get("metadata", {}),
                    **body.get("metadata", {}),
                }
                self.cluster.placements[index] = patched
                self.cluster.patched.append(body)
                return patched
        raise ApiRequestError("not found", status=404)

    def delete_namespaced_custom_object(
        self, group: str, version: str, namespace: str, plural: str, name: str
    ) -> Any:
        """Delete a placement, or fail with 404."""
        for body in self.cluster.placements:
            if _metadata(body).name == name:
                self.cluster.placements.remove(body)
                self.cluster.deleted.append(("StaticPodPlacement", name))
                return None
        raise ApiRequestError("not found", status=404)


class FakeCluster:
    """A :class:`KubernetesClient`-shaped fake with the clusters' objects."""

    def __init__(self) -> None:
        """Build an empty cluster."""
        self.nodes: list[Any] = []
        self.pods: list[Any] = []
        self.replica_sets: list[Any] = []
        self.daemon_sets: list[Any] = []
        self.placements: list[dict[str, Any]] = []
        self.created: list[tuple[str, str]] = []
        self.deleted: list[tuple[str, str]] = []
        self.patched: list[dict[str, Any]] = []
        self.offline = False
        self.core = FakeCore(self)
        self.apps = FakeApps(self)
        self.custom = FakeCustom(self)


class _Listing:
    """The ``.items`` shape the generated client returns."""

    def __init__(self, items: list[Any]) -> None:
        """Wrap a list of objects.

        Args:
            items: The objects.
        """
        self.items = items


def _metadata(body: Any) -> Any:
    """Return an object's metadata, whether it is a model or a mapping.

    Args:
        body: The object.

    Returns:
        The metadata.
    """
    if isinstance(body, dict):
        data = body.get("metadata", {})
        return V1ObjectMeta(
            name=data.get("name"),
            namespace=data.get("namespace"),
            labels=data.get("labels") or {},
            annotations=data.get("annotations") or {},
        )
    return body.metadata


def _matches(metadata: Any, label_selector: str) -> bool:
    """Return whether an object's labels satisfy a selector.

    Args:
        metadata: The object's metadata.
        label_selector: The selector, as ``k=v,k2=v2``.

    Returns:
        ``True`` when every equality holds.
    """
    if not label_selector:
        return True
    labels = (metadata.labels if metadata else None) or {}
    for requirement in label_selector.split(","):
        key, _, value = requirement.partition("=")
        if labels.get(key) != value:
            return False
    return True


def client(cluster: FakeCluster) -> KubernetesClient:
    """Return a client whose groups are the fake's.

    Args:
        cluster: The fake cluster.

    Returns:
        A client-shaped object.
    """
    return cluster  # type: ignore[return-value]


def node(name: str, *, ready: bool = True) -> Any:
    """Build an API node.

    Args:
        name: The node's name.
        ready: Whether it reports Ready.

    Returns:
        The node.
    """
    from kubernetes.client import V1Node, V1NodeCondition, V1NodeStatus

    return V1Node(
        metadata=V1ObjectMeta(name=name, labels={"kubernetes.io/os": "linux"}),
        status=V1NodeStatus(
            conditions=[V1NodeCondition(type="Ready", status="True" if ready else "False")]
        ),
    )


def pod(
    name: str,
    *,
    node_name: str | None = None,
    labels: dict[str, str] | None = None,
    annotations: dict[str, str] | None = None,
    owner: str | None = None,
) -> V1Pod:
    """Build an API Pod.

    Args:
        name: The Pod's name.
        node_name: The node it is bound to.
        labels: Its labels.
        annotations: Its annotations.
        owner: The name of the workload that owns it.

    Returns:
        The Pod.
    """
    return V1Pod(
        metadata=V1ObjectMeta(
            name=name,
            namespace="edge",
            labels=labels or {},
            annotations=annotations or {},
            owner_references=(
                [
                    V1OwnerReference(
                        api_version="apps/v1",
                        kind="ReplicaSet",
                        name=owner,
                        uid="u",
                        controller=True,
                    )
                ]
                if owner
                else None
            ),
        ),
        spec=V1PodSpec(containers=[], node_name=node_name),
        status=V1PodStatus(phase="Pending"),
    )


def probe_labels(generation: int) -> dict[str, str]:
    """Return the labels of a probe's Pods.

    Args:
        generation: The StaticSet generation.

    Returns:
        The labels.
    """
    return {
        COMPONENT_LABEL: PROBE_COMPONENT,
        SET_LABEL: "web",
        SET_UID_LABEL: UID,
        GENERATION_LABEL: str(generation),
    }


def replica_set(
    name: str, *, generation: int, replicas: int, observed: bool = True
) -> V1ReplicaSet:
    """Build an API ReplicaSet.

    Args:
        name: Its name.
        generation: The StaticSet generation in its labels.
        replicas: Its replica count.
        observed: Whether it carries a status.

    Returns:
        The ReplicaSet.
    """
    return V1ReplicaSet(
        metadata=V1ObjectMeta(name=name, namespace="edge", labels=probe_labels(generation)),
        spec=V1ReplicaSetSpec(replicas=replicas, selector=V1LabelSelector(match_labels={})),
        status=V1ReplicaSetStatus(replicas=replicas) if observed else None,
    )


def daemon_set(name: str, *, generation: int, desired: int) -> V1DaemonSet:
    """Build an API DaemonSet with a status.

    Args:
        name: Its name.
        generation: The StaticSet generation in its labels.
        desired: How many nodes its controller wants a Pod on.

    Returns:
        The DaemonSet.
    """
    return V1DaemonSet(
        metadata=V1ObjectMeta(name=name, namespace="edge", labels=probe_labels(generation)),
        spec=V1DaemonSetSpec(
            selector=V1LabelSelector(match_labels={}),
            template=V1PodTemplateSpec(spec=V1PodSpec(containers=[])),
        ),
        status=V1DaemonSetStatus(
            desired_number_scheduled=desired,
            current_number_scheduled=desired,
            updated_number_scheduled=desired,
            number_ready=0,
            number_available=0,
            number_misscheduled=0,
        ),
    )


def placement_body(*, node: str, desired: bool = True, **status: Any) -> dict[str, Any]:
    """Build a placement custom object, as the API returns it.

    Args:
        node: The node it names.
        desired: Whether it is wanted.
        **status: Fields of the status to publish.

    Returns:
        The body.
    """
    return {
        "apiVersion": "staticsets.cloudlane.dev/v1",
        "kind": "StaticPodPlacement",
        "metadata": {
            "name": f"web-{node}",
            "namespace": "edge",
            "labels": {SET_UID_LABEL: UID, SET_LABEL: "web"},
        },
        "spec": {
            "staticSet": "web",
            "staticSetUid": UID,
            "node": node,
            "generation": 3,
            "desired": desired,
            **({"reason": "ScaledDown"} if not desired else {}),
        },
        "status": status,
    }


def test_entry_point_is_a_deployable_operator() -> None:
    """The module registers a reconcile handler and names the CRD it serves."""
    assert operator.GROUP == "staticsets.cloudlane.dev"
    assert operator.PLURAL == "staticsets"
    assert callable(operator.reconcile)
    assert callable(operator.finalize)


def test_selector_renders_a_stable_label_selector() -> None:
    """A selector is sorted, so two calls render the same string."""
    assert operator.selector(b="2", a="1") == "a=1,b=2"


def test_read_snapshot_maps_every_observed_kind(make_static_set: Any) -> None:
    """One read produces the whole state the planner needs."""
    cluster = FakeCluster()
    cluster.nodes = [node("a"), node("b", ready=False)]
    cluster.replica_sets = [replica_set("web-probe-3", generation=3, replicas=2)]
    cluster.pods = [
        pod("probe-a", node_name="a", labels=probe_labels(3), owner="web-probe-3"),
        pod("probe-b", labels=probe_labels(3), owner="web-probe-3"),
        pod(
            "web-abc-r-1",
            node_name="a",
            labels={MANAGED_BY: MANAGED_BY, SET_UID_LABEL: UID},
            annotations={
                MIRROR_ANNOTATION: "abc",
                PLACEMENT_LABEL: placement_identity(UID, "a"),
            },
        ),
    ]
    cluster.placements = [placement_body(node="a", revision="r-1", phase="Installed")]

    state = operator.read_snapshot(client(cluster), make_static_set())

    assert [item.name for item in state.nodes] == ["a", "b"]
    assert state.nodes[1].ready is False
    assert len(state.probes) == 1
    assert state.probes[0].kind == "ReplicaSet"
    assert state.probes[0].replicas == 2
    assert state.probes[0].generation == 3
    assert len(state.probes[0].pods) == 2
    assert state.probes[0].scheduled[0].node == "a"
    assert state.probes[0].unscheduled == 1
    assert [item.node for item in state.placements] == ["a"]
    assert state.placements[0].identity == state.workloads[0].placement
    assert state.workloads[0].node == "a"
    assert state.synced is True


def test_read_snapshot_reports_a_daemonset_probe_as_such(make_static_set: Any) -> None:
    """DaemonSet mode's probe is read with the node count its controller intends."""
    cluster = FakeCluster()
    cluster.daemon_sets = [daemon_set("web-probe-3", generation=3, desired=3)]

    state = operator.read_snapshot(client(cluster), make_static_set())

    assert state.probes[0].kind == "DaemonSet"
    assert state.probes[0].replicas == 3


def test_apply_plan_creates_the_probe_and_the_placements(make_static_set: Any) -> None:
    """A plan's probe workload and placement records are written through the client."""
    cluster = FakeCluster()
    cluster.nodes = [node("a"), node("b")]
    cluster.replica_sets = [replica_set("web-probe-3", generation=3, replicas=2)]
    cluster.pods = [
        pod("probe-a", node_name="a", labels=probe_labels(3), owner="web-probe-3"),
        pod("probe-b", node_name="b", labels=probe_labels(3), owner="web-probe-3"),
    ]
    resource = make_static_set()
    state = operator.read_snapshot(client(cluster), resource)

    operator.apply_plan(client(cluster), resource, state, "edge")

    created = [name for kind, name in cluster.created if kind == "StaticPodPlacement"]
    assert len(created) == 2
    assert len(cluster.placements) == 2
    nodes = sorted(body["spec"]["node"] for body in cluster.placements)
    assert nodes == ["a", "b"]


def test_apply_plan_replaces_an_existing_probe(make_static_set: Any) -> None:
    """Applying a probe that already exists is a replacement, not a second object."""
    cluster = FakeCluster()
    resource = make_static_set()
    state = ObservedState(nodes=(), synced=True)

    operator.apply_plan(client(cluster), resource, state, "edge")

    assert [kind for kind, _ in cluster.created] == ["ReplicaSet"]
    assert cluster.replica_sets[0].metadata.name == "web-probe-3"

    operator.apply_plan(client(cluster), resource, state, "edge")

    assert len(cluster.replica_sets) == 1


def test_apply_plan_deletes_a_stale_probe(make_static_set: Any) -> None:
    """The probe of a superseded generation does not survive its replacement."""
    cluster = FakeCluster()
    cluster.daemon_sets = [daemon_set("web-probe-2", generation=2, desired=0)]
    resource = make_static_set()
    state = operator.read_snapshot(client(cluster), resource)

    operator.apply_plan(client(cluster), resource, state, "edge")

    assert ("DaemonSet", "web-probe-2") in cluster.deleted


def test_apply_plan_deletes_a_cleaned_placement(make_static_set: Any) -> None:
    """A removal the node acknowledged closes its record."""
    cluster = FakeCluster()
    cluster.replica_sets = [replica_set("web-probe-3", generation=3, replicas=1)]
    cluster.pods = [
        pod("probe-a", node_name="a", labels=probe_labels(3), owner="web-probe-3"),
    ]
    cluster.placements = [
        placement_body(node="b", desired=False, phase="Cleaned"),
    ]
    resource = make_static_set()
    state = operator.read_snapshot(client(cluster), resource)

    operator.apply_plan(client(cluster), resource, state, "edge")

    assert ("StaticPodPlacement", "web-b") in cluster.deleted


def test_publish_status_writes_every_reported_field() -> None:
    """The status subresource carries the placement table and the phase markers."""

    class Patch:
        """A kopf-shaped patch."""

        def __init__(self) -> None:
            """Build an empty patch."""
            self.status: dict[str, Any] = {}
            self.metadata: dict[str, Any] = {}

    from static_sets.models import PlacementStatus, StaticSetStatus

    class Result:
        """A plan-shaped result."""

        status = StaticSetStatus(
            observed_generation=3,
            placed_generation=3,
            desired_revision="r-1",
            placements=[PlacementStatus(identity="abc", node="a", revision="r-1")],
        )

    patch = Patch()
    operator.publish_status(patch, Result())  # type: ignore[arg-type]

    assert patch.status["observedGeneration"] == 3
    assert patch.status["placedGeneration"] == 3
    assert patch.status["desiredRevision"] == "r-1"
    assert patch.status["placements"][0]["node"] == "a"


def test_ensure_finalizer_adds_it_once(make_static_set: Any) -> None:
    """The finalizer is added once and left alone after that."""

    class Patch:
        """A kopf-shaped patch."""

        def __init__(self) -> None:
            """Build an empty patch."""
            self.metadata: dict[str, Any] = {}

    missing = make_static_set()
    first = Patch()
    operator.ensure_finalizer(first, missing)  # type: ignore[arg-type]
    assert first.metadata["finalizers"] == [STATIC_SET_FINALIZER]

    present = make_static_set(metadata={"finalizers": [STATIC_SET_FINALIZER]})
    second = Patch()
    operator.ensure_finalizer(second, present)  # type: ignore[arg-type]
    assert second.metadata == {}


def test_reject_unsupported_refuses_an_unknown_field(make_resource: Any) -> None:
    """The admission check turns a pruned field into a rejection."""
    body = make_resource()
    body["spec"]["template"]["spec"]["serviceAccountName"] = "default"

    with pytest.raises(kopf.AdmissionError) as error:
        operator.reject_unsupported(body)

    assert "serviceAccountName" in str(error.value)


def test_read_snapshot_raises_when_the_api_is_unreachable(
    make_static_set: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed read must not look like an empty cluster."""
    from static_sets.errors import ApiUnavailableError

    class Offline(FakeCluster):
        """A cluster whose reads fail."""

        def __init__(self) -> None:
            super().__init__()

            def fail(*_: Any, **__: Any) -> Any:
                raise ApiUnavailableError("connection refused")

            self.core.list_namespaced_pod = fail

    with pytest.raises(ApiUnavailableError):
        operator.read_snapshot(client(Offline()), make_static_set())


def test_reconcile_retries_when_the_api_is_unreachable(
    make_static_set: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A reconcile that cannot read retries instead of deleting anything."""
    from static_sets.errors import ApiUnavailableError

    monkeypatch.setattr(
        operator,
        "read_snapshot",
        lambda *args, **kwargs: (_ for _ in ()).throw(ApiUnavailableError("offline")),
    )
    monkeypatch.setattr(operator, "get_client", FakeCluster)

    class Patch:
        """A kopf-shaped patch."""

        def __init__(self) -> None:
            """Build an empty patch."""
            self.status: dict[str, Any] = {}
            self.metadata: dict[str, Any] = {}

    with pytest.raises(kopf.TemporaryError):
        operator.reconcile(make_static_set(), Patch(), _logger())  # type: ignore[arg-type]


def test_finalize_waits_while_a_placement_is_not_cleaned(
    make_static_set: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The StaticSet is not released before every node acknowledged its removal."""
    cluster = FakeCluster()
    cluster.placements = [placement_body(node="a", desired=False, phase="Cleaning")]
    monkeypatch.setattr(operator, "get_client", lambda: client(cluster))
    resource = make_static_set(metadata={"deletionTimestamp": "2026-10-24T10:00:00+00:00"})

    class Patch:
        """A kopf-shaped patch."""

        def __init__(self) -> None:
            """Build an empty patch."""
            self.status: dict[str, Any] = {}
            self.metadata: dict[str, Any] = {}

    with pytest.raises(kopf.TemporaryError):
        operator.finalize(resource, Patch(), _logger())  # type: ignore[arg-type]


def _logger() -> Any:
    """Return a logger that swallows everything.

    Returns:
        A stub logger.
    """

    class Logger:
        """A no-op logger."""

        def info(self, *args: Any, **kwargs: Any) -> None:
            """Ignore an informational message."""

        def warning(self, *args: Any, **kwargs: Any) -> None:
            """Ignore a warning."""

        def error(self, *args: Any, **kwargs: Any) -> None:
            """Ignore an error."""

    return Logger()
