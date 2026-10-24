"""The cluster-backed manager API: what each read and write actually does."""

from __future__ import annotations

import base64
from typing import Any

from kubernetes.client import (
    V1ConfigMap,
    V1ContainerState,
    V1ContainerStateRunning,
    V1ContainerStatus,
    V1ObjectMeta,
    V1Pod,
    V1PodSpec,
    V1PodStatus,
    V1Secret,
)

from static_sets.api import ApiRequestError
from static_sets.cluster import ClusterApi
from static_sets.naming import ANNOTATION_REVISION, MIRROR_ANNOTATION, PLACEMENT_LABEL
from static_sets.placements import StaticPodPlacement

UID = "0f9a4b6c-1111-2222-3333-444455556666"
PLACEMENT = "e6383089b2"


class FakeCore:
    """The ``core`` group over in-memory objects."""

    def __init__(self, cluster: FakeCluster) -> None:
        """Bind the group to its cluster.

        Args:
            cluster: The owning fake.
        """
        self.cluster = cluster

    def read_namespaced_config_map(self, name: str, namespace: str) -> Any:
        """Return a ConfigMap, or fail with 404."""
        for item in self.cluster.config_maps:
            if item.metadata.name == name:
                return item
        raise ApiRequestError("not found", status=404)

    def read_namespaced_secret(self, name: str, namespace: str) -> Any:
        """Return a Secret, or fail with 404."""
        for item in self.cluster.secrets:
            if item.metadata.name == name:
                return item
        raise ApiRequestError("not found", status=404)

    def list_namespaced_pod(self, namespace: str, label_selector: str = "") -> Any:
        """Return the Pods matching a selector."""
        key, _, value = label_selector.partition("=")
        matched = [
            pod
            for pod in self.cluster.pods
            if ((pod.metadata.labels or {}) if pod.metadata else {}).get(key) == value
        ]
        return _Listing(matched)


class FakeCustom:
    """The custom-objects group over in-memory objects."""

    def __init__(self, cluster: FakeCluster) -> None:
        """Bind the group to its cluster.

        Args:
            cluster: The owning fake.
        """
        self.cluster = cluster

    def get_namespaced_custom_object(
        self, group: str, version: str, namespace: str, plural: str, name: str
    ) -> Any:
        """Return an object body, or fail with 404."""
        for body in self.cluster.objects:
            if body["metadata"]["name"] == name:
                return body
        raise ApiRequestError("not found", status=404)

    def list_cluster_custom_object(self, group: str, version: str, plural: str) -> Any:
        """Return every object body, across namespaces."""
        return {"items": list(self.cluster.objects)}

    def patch_namespaced_custom_object_status(
        self, group: str, version: str, namespace: str, plural: str, name: str, body: Any
    ) -> Any:
        """Record a status patch."""
        self.cluster.patched.append((name, body))
        return body


class FakeCluster:
    """A :class:`KubernetesClient`-shaped fake."""

    def __init__(self) -> None:
        """Build an empty cluster."""
        self.config_maps: list[Any] = []
        self.secrets: list[Any] = []
        self.pods: list[Any] = []
        self.objects: list[dict[str, Any]] = []
        self.patched: list[tuple[str, dict[str, Any]]] = []
        self.core = FakeCore(self)
        self.custom = FakeCustom(self)


class _Listing:
    """The ``.items`` shape the generated client returns."""

    def __init__(self, items: list[Any]) -> None:
        """Wrap a list of objects.

        Args:
            items: The objects.
        """
        self.items = items


class _Api(ClusterApi):
    """A :class:`ClusterApi` whose client is the fake."""

    def __init__(self, cluster: FakeCluster) -> None:
        """Bind the API to the fake cluster.

        Args:
            cluster: The fake cluster.
        """
        self.client = cluster  # type: ignore[assignment]


def pod(
    name: str,
    *,
    placement: str | None = PLACEMENT,
    mirror: bool = True,
    revision: str | None = "r-1",
    phase: str | None = "Running",
    ready: bool | None = True,
) -> V1Pod:
    """Build an API Pod.

    Args:
        name: The Pod's name.
        placement: The placement identity label, or ``None`` for no label.
        mirror: Whether to carry kubelet's mirror annotation.
        revision: The revision annotation.
        phase: The phase.
        ready: Whether the containers report ready, or ``None`` for no statuses.

    Returns:
        The Pod.
    """
    annotations = {MIRROR_ANNOTATION: "hash"} if mirror else {}
    if revision:
        annotations[ANNOTATION_REVISION] = revision
    containers = (
        []
        if ready is None
        else [
            V1ContainerStatus(
                name="app",
                image="registry.example.com/web:1",
                image_id="",
                ready=ready,
                restart_count=0,
                state=V1ContainerState(running=V1ContainerStateRunning()),
            )
        ]
    )
    return V1Pod(
        metadata=V1ObjectMeta(
            name=name,
            namespace="edge",
            labels={PLACEMENT_LABEL: placement} if placement else {},
            annotations=annotations,
        ),
        spec=V1PodSpec(containers=[], node_name="worker-1"),
        status=V1PodStatus(phase=phase or "Pending", container_statuses=containers),
    )


def placement_body(name: str, *, node: str) -> dict[str, Any]:
    """Build a placement custom object.

    Args:
        name: Its name.
        node: The node it names.

    Returns:
        The body.
    """
    return {
        "apiVersion": "staticsets.cloudlane.dev/v1",
        "kind": "StaticPodPlacement",
        "metadata": {"name": name, "namespace": "edge"},
        "spec": {
            "staticSet": "web",
            "staticSetUid": UID,
            "node": node,
            "generation": 3,
        },
    }


def test_a_staticset_is_read_and_validated() -> None:
    """The body the API returns is turned into the model the manager uses."""
    cluster = FakeCluster()
    cluster.objects = [
        {
            "apiVersion": "staticsets.cloudlane.dev/v1",
            "kind": "StaticSet",
            "metadata": {"name": "web", "namespace": "edge", "uid": UID, "generation": 3},
            "spec": {
                "mode": "Deployment",
                "replicas": 1,
                "template": {
                    "spec": {"containers": [{"name": "app", "image": "registry.example.com/web:1"}]}
                },
            },
        }
    ]

    resource = _Api(cluster).get_static_set("edge", "web")

    assert resource is not None
    assert resource.spec.replicas == 1


def test_a_deleted_staticset_is_absence_rather_than_failure() -> None:
    """A placement whose set is gone is a normal answer, not an API failure."""
    assert _Api(FakeCluster()).get_static_set("edge", "web") is None


def test_a_config_map_is_decoded_including_binary_entries() -> None:
    """ConfigMap data and binaryData both reach the installer as bytes."""
    cluster = FakeCluster()
    cluster.config_maps = [
        V1ConfigMap(
            metadata=V1ObjectMeta(name="web-conf", namespace="edge"),
            data={"app.conf": "greeting=hello"},
            binary_data={"blob": base64.b64encode(b"\x00\x01").decode()},
        )
    ]

    data = _Api(cluster).get_config_map("edge", "web-conf")

    assert data is not None
    assert data["app.conf"] == b"greeting=hello"
    assert data["blob"] == b"\x00\x01"


def test_a_missing_config_map_is_absence() -> None:
    """An optional reference that does not exist is not an error."""
    assert _Api(FakeCluster()).get_config_map("edge", "missing") is None


def test_a_secret_is_decoded_from_base64() -> None:
    """Secret data crosses the API encoded and is decoded once here."""
    cluster = FakeCluster()
    cluster.secrets = [
        V1Secret(
            metadata=V1ObjectMeta(name="web-tls", namespace="edge"),
            data={"tls.key": base64.b64encode(b"private").decode()},
        )
    ]

    data = _Api(cluster).get_secret("edge", "web-tls")

    assert data == {"tls.key": b"private"}


def test_a_missing_secret_is_absence() -> None:
    """A Secret that does not exist is not an error."""
    assert _Api(FakeCluster()).get_secret("edge", "missing") is None


def test_mirror_pods_are_found_by_placement_label() -> None:
    """kubelet names a mirror after the node too, so only the label is dependable."""
    cluster = FakeCluster()
    cluster.pods = [
        pod("web-e6383089b2-r-1-worker-1"),
        pod("web-other-r-1-worker-1", placement="aaaaaaaaaa"),
        pod("not-a-mirror", mirror=False),
    ]

    mirrors = _Api(cluster).get_mirror_pods("edge", PLACEMENT)

    assert [item.name for item in mirrors] == ["web-e6383089b2-r-1-worker-1"]
    assert mirrors[0].revision == "r-1"
    assert mirrors[0].phase == "Running"
    assert mirrors[0].ready is True


def test_a_mirror_without_container_status_is_still_reported() -> None:
    """Before kubelet starts anything the Pod is published with no statuses."""
    cluster = FakeCluster()
    cluster.pods = [pod("mirror", ready=None, phase="Pending")]

    mirrors = _Api(cluster).get_mirror_pods("edge", PLACEMENT)

    assert mirrors[0].ready is None
    assert mirrors[0].phase == "Pending"


def test_a_partly_ready_mirror_is_not_ready() -> None:
    """Every container has to be ready before the workload counts as running."""
    cluster = FakeCluster()
    cluster.pods = [pod("mirror", ready=False)]

    mirrors = _Api(cluster).get_mirror_pods("edge", PLACEMENT)

    assert mirrors[0].ready is False


def test_a_status_report_is_a_merge_patch_over_the_status_subresource() -> None:
    """The manager writes status fields only, never the spec the controller owns."""
    cluster = FakeCluster()
    api = _Api(cluster)

    api.publish_status(
        "edge",
        "web-e6383089b2",
        {
            "revision": "r-1",
            "staticPodName": "web-e6383089b2-r-1",
            "phase": "Installed",
            "acknowledged": "true",
            "observedGeneration": "3",
            "message": "",
            "updatedAt": "2026-10-24T10:00:00+00:00",
            "offline": "false",
        },
    )

    name, body = cluster.patched[0]
    assert name == "web-e6383089b2"
    assert body["status"]["phase"] == "Installed"
    assert body["status"]["acknowledged"] is True
    assert body["status"]["observedGeneration"] == 3
    assert body["status"]["message"] is None
    assert "spec" not in body


def test_the_placement_identity_is_derived_from_the_record() -> None:
    """The identity is a function of the UID and the node, never of the object name."""
    record = StaticPodPlacement.model_validate(placement_body("web-e6383089b2", node="worker-1"))

    identity = _Api(FakeCluster()).identity_of(record)

    from static_sets.naming import placement_identity

    assert identity == placement_identity(record.spec.static_set_uid, record.spec.node)
    assert len(identity) == 10


def test_every_placement_of_one_node_is_listed_and_no_other() -> None:
    """A one-off pass is driven from the records that name this node."""
    cluster = FakeCluster()
    cluster.objects = [
        placement_body("web-a", node="worker-1"),
        placement_body("web-b", node="worker-2"),
    ]

    placements = _Api(cluster).list_placements("worker-1")

    assert [item.metadata.name for item in placements] == ["web-a"]
    assert placements[0].spec.node == "worker-1"


def test_a_placement_is_readable_by_name() -> None:
    """The command's report needs the object it is about."""
    cluster = FakeCluster()
    cluster.objects = [placement_body("web-a", node="worker-1")]

    found = _Api(cluster).placement_by_name("edge", "web-a")
    missing = _Api(cluster).placement_by_name("edge", "web-z")

    assert found is not None
    assert found.spec.node == "worker-1"
    assert missing is None
