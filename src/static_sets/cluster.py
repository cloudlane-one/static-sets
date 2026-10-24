"""The cluster-backed implementation of the manager's API slice.

:class:`~static_sets.manager.PlacementApi` is a protocol so that the manager's whole
behaviour can be tested without a cluster. This module is the one implementation that
talks to a real API server, shared by both of the manager's entry points — the kopf
handler that runs in the DaemonSet, and the ``static-set-manager`` command used for a
one-off pass — so the two cannot disagree about how a StaticSet, a ConfigMap or a
mirror Pod is read.
"""

from __future__ import annotations

import base64
from collections.abc import Mapping
from typing import Any, cast

from static_sets.api import (
    KubernetesClient,
    or_none,
)
from static_sets.manager import MirrorPod
from static_sets.models import StaticSet
from static_sets.placements import StaticPodPlacement

__all__ = ["ClusterApi"]


class ClusterApi:
    """The :class:`~static_sets.manager.PlacementApi` slice, against a cluster.

    The API operations themselves come from
    :class:`~static_sets.api.KubernetesClient`, which returns the official
    client's pydantic models. This adapter only turns those models into the
    byte-oriented shapes the manager's runtime works with.
    """

    def __init__(self, client: KubernetesClient) -> None:
        """Bind the API to a client.

        Args:
            client: The Kubernetes API client to use.
        """
        self.client = client

    def get_static_set(self, namespace: str, name: str) -> StaticSet | None:
        """Return the StaticSet a placement belongs to.

        Args:
            namespace: The StaticSet's namespace.
            name: The StaticSet's name.

        Returns:
            The validated StaticSet, or ``None`` when it no longer exists.

        Raises:
            ApiUnavailableError: If the API could not be reached.
            ApiRequestError: If the API refused the request for another reason.
        """
        config = StaticSet._require_resource_config()
        body = _as_body(
            or_none(
                self.client.custom.get_namespaced_custom_object,
                config.group,
                config.version,
                namespace,
                config.plural,
                name,
            )
        )
        if body is None:
            return None
        return StaticSet.model_validate(_body(config, body))

    def get_config_map(self, namespace: str, name: str) -> Mapping[str, bytes] | None:
        """Return a ConfigMap's data, decoding any binary entries.

        Args:
            namespace: The ConfigMap's namespace.
            name: The ConfigMap's name.

        Returns:
            The data, or ``None`` when the ConfigMap does not exist.

        Raises:
            ApiUnavailableError: If the API could not be reached.
        """
        record = or_none(self.client.core.read_namespaced_config_map, name, namespace)
        if record is None:
            return None
        data: dict[str, bytes] = {
            key: value.encode("utf-8") for key, value in (record.data or {}).items()
        }
        for key, value in (record.binary_data or {}).items():
            data[key] = base64.b64decode(value)
        return data

    def get_secret(self, namespace: str, name: str) -> Mapping[str, bytes] | None:
        """Return a Secret's data, decoded from its base64 representation.

        Args:
            namespace: The Secret's namespace.
            name: The Secret's name.

        Returns:
            The data, or ``None`` when the Secret does not exist.

        Raises:
            ApiUnavailableError: If the API could not be reached.
        """
        record = or_none(self.client.core.read_namespaced_secret, name, namespace)
        if record is None:
            return None
        return {key: base64.b64decode(value) for key, value in (record.data or {}).items()}

    def get_pod(self, namespace: str, name: str) -> MirrorPod | None:
        """Return the mirror Pod kubelet published for a static Pod.

        Args:
            namespace: The namespace the static Pod's manifest declares.
            name: The static Pod's name.

        Returns:
            The mirror Pod, or ``None`` when kubelet has not published one.

        Raises:
            ApiUnavailableError: If the API could not be reached.
        """
        record = or_none(self.client.core.read_namespaced_pod, name, namespace)
        if record is None:
            return None
        containers = (record.status.container_statuses if record.status else None) or []
        ready = all(container.ready for container in containers) if containers else None
        return MirrorPod(
            phase=(record.status.phase if record.status else None),
            ready=ready,
        )

    def publish_status(self, namespace: str, name: str, payload: Mapping[str, str]) -> None:
        """Write a manager's report into a placement's status subresource.

        A merge patch is used rather than a replace: the manager owns the status, but
        a replace would also mean writing back whatever it happened to read, and a
        409 from a concurrent change would lose the report for no reason.

        Args:
            namespace: The placement's namespace.
            name: The placement's name.
            payload: The status fields to merge.

        Raises:
            ApiUnavailableError: If the API could not be reached.
            ApiRequestError: If the write was refused.
        """
        config = StaticPodPlacement._require_resource_config()
        status: dict[str, Any] = {
            "revision": payload.get("revision") or None,
            "staticPodName": payload.get("staticPodName") or None,
            "phase": payload.get("phase"),
            "acknowledged": payload.get("acknowledged") == "true",
            "observedGeneration": int(payload.get("observedGeneration", "0") or 0),
            "message": payload.get("message") or None,
            "updatedAt": payload.get("updatedAt"),
            "offline": payload.get("offline") == "true",
        }
        self.client.custom.patch_namespaced_custom_object_status(
            config.group,
            config.version,
            namespace,
            config.plural,
            name,
            {"status": status},
        )

    def identity_of(self, placement: StaticPodPlacement) -> str:
        """Return the placement identity a record stands for.

        Args:
            placement: The placement object.

        Returns:
            The identity derived from its StaticSet UID and node.
        """
        from static_sets.naming import placement_identity

        return placement_identity(placement.spec.static_set_uid, placement.spec.node)

    def list_placements(self, node: str) -> list[StaticPodPlacement]:
        """Return every placement that names one node, in every namespace.

        This is what a one-off manager pass is driven from: the manager is cluster-wide
        because a StaticSet may live in any namespace, and each record names the node
        it belongs to.

        Args:
            node: The node the manager owns.

        Returns:
            The placements for that node.

        Raises:
            ApiUnavailableError: If the API could not be reached.
            ApiRequestError: If the read was refused.
        """
        config = StaticPodPlacement._require_resource_config()
        items = cast(
            "dict[str, Any]",
            self.client.custom.list_cluster_custom_object(
                config.group,
                config.version,
                config.plural,
            ),
        )
        placements: list[StaticPodPlacement] = []
        for item in items.get("items", []):
            placement = StaticPodPlacement.model_validate(
                _body(config, cast("dict[str, Any]", item))
            )
            if placement.spec.node == node:
                placements.append(placement)
        return placements

    def placement_by_name(self, namespace: str, name: str) -> StaticPodPlacement | None:
        """Read one placement record.

        Args:
            namespace: The placement's namespace.
            name: The placement's name.

        Returns:
            The placement, or ``None`` when it does not exist.

        Raises:
            ApiUnavailableError: If the API could not be reached.
        """
        config = StaticPodPlacement._require_resource_config()
        body = _as_body(
            or_none(
                self.client.custom.get_namespaced_custom_object,
                config.group,
                config.version,
                namespace,
                config.plural,
                name,
            )
        )
        if body is None:
            return None
        return StaticPodPlacement.model_validate(_body(config, body))


def _as_body(value: object) -> dict[str, Any] | None:
    """Narrow an API response to a mapping.

    Args:
        value: Whatever the generated client returned.

    Returns:
        The object as a mapping, or ``None`` when there was no object.
    """
    if value is None:
        return None
    return cast("dict[str, Any]", value)


def _body(config: Any, item: Mapping[str, Any]) -> dict[str, Any]:
    """Fill in the fields the API returns without, before validating an object.

    Args:
        config: The resource configuration.
        item: The raw custom object.

    Returns:
        The body, with ``apiVersion`` and ``kind`` set.
    """
    return {
        **item,
        "apiVersion": item.get("apiVersion") or f"{config.group}/{config.version}",
        "kind": item.get("kind") or config.kind,
    }
