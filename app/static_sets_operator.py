"""The StaticSet operator entry point.

This is the file ``kopf run`` executes inside the controller image. It does two
things and nothing else: it reads the cluster into an
:class:`~static_sets.controller.ObservedState`, and it applies the
:class:`~static_sets.controller.ReconcilePlan` that
:func:`~static_sets.controller.plan` derives from it. Every decision lives in the
library, where it is unit-tested; this module is only the adapter that talks to
Kubernetes.

The controller never touches a host filesystem. Static Pods are written by the
*installer* Pods it creates, and a placement whose installer is gone is cleaned up
by a restricted cleanup worker pinned to the node. The controller's job is to decide
what should exist and to report what it observed.

A validating admission webhook is registered alongside the reconciler — but only when
one is actually being served. It exists because the generated CRD is a structural
schema, which *prunes* an unknown field instead of refusing it; the webhook turns that
silent drop into a rejection. The chart sets ``STATIC_SETS_WEBHOOK=1`` (and imports
``based_operators.webhook`` to configure the server) when its ``webhook.enabled`` is
on, and leaves the handler unregistered otherwise, because kopf refuses to start when
admission handlers exist with no admission server to serve them.
"""

from __future__ import annotations

import datetime
import os
from typing import Any

import kopf

import based_operators as bk  # noqa: F401 - the resource class carries the resource config

from static_sets.api import ApiUnavailableError, JsonClient, KubernetesClient
from static_sets.controller import CleanupAction, ObservedState, ObservedWorkload, plan
from static_sets.models import StaticSet
from static_sets.naming import (
    MANAGED_BY,
    MIRROR_ANNOTATION,
    PLACEMENT_LABEL,
    STATIC_SET_FINALIZER,
    placement_configmap_name,
)
from static_sets.placement import InstallerPod, NodeInfo, ObservedPlacement
from static_sets.validation import strict_problems

__all__ = [
    "JsonClient",
    "apply_plan",
    "read_snapshot",
    "reject_unsupported",
    "reconcile",
]

GROUP = "staticsets.cloudlane.dev"
VERSION = "v1"
PLURAL = "staticsets"

WEBHOOK_ENV = "STATIC_SETS_WEBHOOK"
"""The environment variable the chart uses to say whether admissions are served."""

SET_LABEL = "staticsets.cloudlane.dev/set"
"""Label tying every object the operator creates to its StaticSet."""

STATIC_SET_PATH = f"/apis/{GROUP}/{VERSION}/namespaces/{{namespace}}/{PLURAL}/{{name}}"
"""The REST path of a StaticSet."""

PODS_PATH = "/api/v1/namespaces/{namespace}/pods"
"""The REST path of the Pod collection."""

CONFIG_MAPS_PATH = "/api/v1/namespaces/{namespace}/configmaps"
"""The REST path of the ConfigMap collection."""

NODES_PATH = "/api/v1/nodes"
"""The REST path of the Node collection."""

APP_COLLECTIONS = (
    "/apis/apps/v1/namespaces/{namespace}/deployments",
    "/apis/apps/v1/namespaces/{namespace}/daemonsets",
)
"""Both installer workload collections, so a mode switch leaves nothing behind."""

INSTALLER_COMPONENT = "installer"
PLACEMENT_COMPONENT = "placement"

TIMER_INTERVAL = 120
"""Seconds between periodic re-reconciles, so pending cleanups are retried."""


def get_client() -> JsonClient:
    """Return the API client the adapter uses.

    Returns:
        An in-cluster client.
    """
    return KubernetesClient.in_cluster()


def selector(**labels: str) -> str:
    """Build a label selector string.

    Args:
        **labels: The labels to select on.

    Returns:
        The selector.
    """
    return ",".join(f"{key}={value}" for key, value in sorted(labels.items()))


def read_snapshot(client: JsonClient, resource: StaticSet) -> ObservedState:
    """Read the cluster state a reconcile plan is computed from.

    Every read below is a synchronous list or get that raises on failure, so the
    snapshot is either complete or absent. A partial snapshot is exactly what must
    never be mistaken for an empty one.

    Args:
        client: The API client.
        resource: The StaticSet being reconciled.

    Returns:
        The observed state.

    Raises:
        ApiUnavailableError: If any read failed.
    """
    namespace = resource.metadata.namespace or "default"
    name = resource.metadata.name or ""
    pods = client.json(
        "GET",
        PODS_PATH.format(namespace=namespace),
        params={"labelSelector": f"{SET_LABEL}={name}"},
        missing={},
    )
    nodes = client.json("GET", NODES_PATH, missing={})
    config_maps = client.json(
        "GET",
        CONFIG_MAPS_PATH.format(namespace=namespace),
        params={
            "labelSelector": selector(
                **{"app.kubernetes.io/component": PLACEMENT_COMPONENT, SET_LABEL: name}
            )
        },
        missing={},
    )
    items = (pods or {}).get("items", [])
    return ObservedState(
        nodes=tuple(_node(item) for item in (nodes or {}).get("items", [])),
        installer_pods=tuple(
            pod for pod in (_installer_pod(item) for item in items) if pod is not None
        ),
        placements=tuple(
            placement
            for placement in (_placement(item) for item in (config_maps or {}).get("items", []))
            if placement is not None
        ),
        workloads=tuple(
            workload for workload in (_mirror_pod(item) for item in items) if workload is not None
        ),
        synced=True,
        observed_at=_now(),
    )


def apply_plan(
    client: JsonClient,
    resource: StaticSet,
    state: ObservedState,
    namespace: str,
) -> Any:
    """Compute and apply the reconcile plan.

    Args:
        client: The API client.
        resource: The StaticSet being reconciled.
        state: The observed state.
        namespace: The namespace the StaticSet lives in.

    Returns:
        The plan that was applied, so the caller can publish its status.
    """
    result = plan(resource, state, namespace=namespace)
    if result.delete_workload:
        _delete_installers(client, namespace=namespace, name=resource.metadata.name or "")
    elif result.workload is not None:
        kind, manifest = result.workload
        path = _workload_path(kind, namespace=namespace, name=manifest["metadata"]["name"])
        _apply(client, path, manifest)
    for action in result.cleanup:
        _publish_intent(client, namespace, action)
        if action.worker is not None:
            worker_name = action.worker["metadata"]["name"]
            _apply(client, f"{PODS_PATH.format(namespace=namespace)}/{worker_name}", action.worker)
    return result


def publish_status(patch: kopf.Patch, result: Any) -> None:
    """Write a plan's status into the object's status subresource.

    Args:
        patch: The kopf patch of the object being reconciled.
        result: The plan that was applied.
    """
    status = result.status.model_dump(mode="json", by_alias=True, exclude_none=True)
    patch.status["observedGeneration"] = status.get("observedGeneration", 0)
    patch.status["placements"] = status.get("placements", [])
    patch.status["health"] = status.get("health", [])
    patch.status["cleanup"] = status.get("cleanup", [])
    patch.status["conditions"] = status.get("conditions", [])
    if "desiredRevision" in status:
        patch.status["desiredRevision"] = status["desiredRevision"]


def ensure_finalizer(patch: kopf.Patch, resource: StaticSet) -> None:
    """Make sure the StaticSet carries the operator's finalizer.

    Args:
        patch: The kopf patch of the object being reconciled.
        resource: The StaticSet being reconciled.
    """
    finalizers = list(resource.metadata.finalizers or [])
    if STATIC_SET_FINALIZER not in finalizers:
        patch.metadata["finalizers"] = [*finalizers, STATIC_SET_FINALIZER]


def reject_unsupported(body: dict[str, Any], **_: Any) -> None:
    """Refuse a StaticSet the operator could not compile faithfully.

    Args:
        body: The candidate object, as the API server received it.
        **_: Any further webhook arguments.

    Raises:
        kopf.AdmissionError: If the object has problems the CRD would have pruned.
    """
    problems = strict_problems(body)
    if problems:
        raise kopf.AdmissionError("; ".join(problems))


def _node(item: dict[str, Any]) -> NodeInfo:
    """Convert one API node into the shape the planner expects.

    Args:
        item: The raw node.

    Returns:
        The node.
    """
    metadata = item.get("metadata", {})
    conditions = item.get("status", {}).get("conditions", [])
    ready = any(
        condition.get("type") == "Ready" and condition.get("status") == "True"
        for condition in conditions
    )
    return NodeInfo(
        name=metadata.get("name", ""),
        labels=metadata.get("labels") or {},
        unschedulable=bool(item.get("spec", {}).get("unschedulable")),
        ready=ready,
    )


def _installer_pod(item: dict[str, Any]) -> InstallerPod | None:
    """Convert one API Pod into an installer Pod, when it is one.

    Args:
        item: The raw Pod.

    Returns:
        The installer Pod, or ``None`` when the Pod is not a live installer.
    """
    metadata = item.get("metadata", {})
    labels = metadata.get("labels") or {}
    if labels.get("app.kubernetes.io/component") != INSTALLER_COMPONENT:
        return None
    if MIRROR_ANNOTATION in (metadata.get("annotations") or {}):
        return None
    return InstallerPod(
        name=metadata.get("name", ""),
        uid=metadata.get("uid", ""),
        node=item.get("spec", {}).get("nodeName"),
        phase=item.get("status", {}).get("phase", "Pending"),
        deleting=metadata.get("deletionTimestamp") is not None,
    )


def _mirror_pod(item: dict[str, Any]) -> ObservedWorkload | None:
    """Convert one API Pod into observed workload health, when it is a mirror Pod.

    Args:
        item: The raw Pod.

    Returns:
        The observed workload, or ``None`` when the Pod is not a mirror Pod.
    """
    metadata = item.get("metadata", {})
    if MIRROR_ANNOTATION not in (metadata.get("annotations") or {}):
        return None
    status = item.get("status", {})
    containers = status.get("containerStatuses") or []
    ready = all(container.get("ready") for container in containers) if containers else None
    return ObservedWorkload(
        name=metadata.get("name", ""),
        node=item.get("spec", {}).get("nodeName", ""),
        phase=status.get("phase"),
        ready=ready,
        restart_count=sum(container.get("restartCount", 0) for container in containers),
    )


def _placement(item: dict[str, Any]) -> ObservedPlacement | None:
    """Convert one placement ConfigMap into an observed placement.

    Args:
        item: The raw ConfigMap.

    Returns:
        The observed placement, or ``None`` when the ConfigMap is not a placement.
    """
    data = item.get("data") or {}
    if not data.get("placement"):
        return None
    return ObservedPlacement(
        identity=data["placement"],
        node=data.get("node", ""),
        revision=data.get("revision") or None,
        state=data.get("state", "Installing"),
        acknowledged=data.get("acknowledged") == "true",
        message=data.get("message") or None,
    )


def _workload_path(kind: str, *, namespace: str, name: str) -> str:
    """Return the API path of an installer workload.

    Args:
        kind: ``Deployment`` or ``DaemonSet``.
        namespace: The namespace.
        name: The workload's name.

    Returns:
        The API path.
    """
    collection = APP_COLLECTIONS[0] if kind == "Deployment" else APP_COLLECTIONS[1]
    return f"{collection.format(namespace=namespace)}/{name}"


def _apply(client: JsonClient, path: str, manifest: dict[str, Any]) -> None:
    """Create or replace an object, leaving it in the desired shape.

    Args:
        client: The API client.
        path: The object's API path.
        manifest: The desired object.
    """
    existing = client.json("GET", path, missing={})
    if existing:
        manifest["metadata"]["resourceVersion"] = existing["metadata"]["resourceVersion"]
        client.json("PUT", path, body=manifest)
    else:
        client.json("POST", path.rsplit("/", 1)[0], body=manifest)


def _delete_installers(client: JsonClient, *, namespace: str, name: str) -> None:
    """Delete both installer workload kinds, so a mode switch leaves nothing behind.

    Args:
        client: The API client.
        namespace: The namespace the StaticSet lives in.
        name: The StaticSet's name.
    """
    query = {"labelSelector": f"{SET_LABEL}={name}"}
    for collection in APP_COLLECTIONS:
        path = collection.format(namespace=namespace)
        listing = client.json("GET", path, params=query, missing={})
        for item in (listing or {}).get("items", []):
            client.json(
                "DELETE",
                f"{path}/{item['metadata']['name']}",
                body={"propagationPolicy": "Background"},
            )


def _publish_intent(client: JsonClient, namespace: str, action: CleanupAction) -> None:
    """Publish a cleanup intent so the node can learn of it.

    The controller has no host access, so the intent travels through the API: the
    installer that still serves the node — or the cleanup worker pinned to it —
    reads it, persists it locally, and only then acts.

    Args:
        client: The API client.
        namespace: The namespace the StaticSet lives in.
        action: The cleanup action to publish.
    """
    static_set = action.payload["staticSet"]
    name = placement_configmap_name(static_set, action.identity)
    manifest = {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {
            "name": name,
            "namespace": namespace,
            "labels": {
                "app.kubernetes.io/managed-by": MANAGED_BY,
                "app.kubernetes.io/component": PLACEMENT_COMPONENT,
                SET_LABEL: static_set,
                PLACEMENT_LABEL: action.identity,
            },
        },
        "data": dict(action.payload),
    }
    _apply(client, f"{CONFIG_MAPS_PATH.format(namespace=namespace)}/{name}", manifest)


@StaticSet.on.create
@StaticSet.on.resume
@StaticSet.on.update
@StaticSet.on.timer(interval=TIMER_INTERVAL, initial_delay=30, idle=10)
def reconcile(resource: StaticSet, patch: bk.Patch, logger: Any, **_: Any) -> None:
    """Bring the installer placements of a StaticSet in line with its specification.

    Args:
        resource: The StaticSet being reconciled.
        patch: The kopf patch used to record status and the finalizer.
        logger: The kopf logger.
        **_: Any further handler arguments.

    Raises:
        kopf.TemporaryError: If the API could not be read, so the reconcile is
            retried rather than taken as an empty desired state.
    """
    namespace = resource.metadata.namespace or "default"
    client = get_client()
    try:
        state = read_snapshot(client, resource)
    except ApiUnavailableError as error:
        raise kopf.TemporaryError(f"the API is unreachable: {error}", delay=15) from error
    result = apply_plan(client, resource, state, namespace)
    logger.info(
        "reconciled %s/%s: %s placement(s), %s cleanup(s) pending",
        namespace,
        resource.metadata.name,
        len(result.status.placements),
        len(result.status.cleanup),
    )
    publish_status(patch, result)
    if resource.metadata.deletion_timestamp is None:
        ensure_finalizer(patch, resource)


@StaticSet.on.delete
def finalize(resource: StaticSet, patch: bk.Patch, logger: Any, **_: Any) -> None:
    """Release the StaticSet's finalizer once every placement is cleaned up.

    Args:
        resource: The StaticSet being deleted.
        patch: The kopf patch used to record status and the finalizer.
        logger: The kopf logger.
        **_: Any further handler arguments.

    Raises:
        kopf.TemporaryError: While cleanups are still pending, so the object is not
            released before its static Pods are acknowledged removed.
    """
    namespace = resource.metadata.namespace or "default"
    client = get_client()
    try:
        state = read_snapshot(client, resource)
    except ApiUnavailableError as error:
        raise kopf.TemporaryError(f"the API is unreachable: {error}", delay=15) from error
    result = apply_plan(client, resource, state, namespace)
    publish_status(patch, result)
    pending = [record for record in result.status.cleanup if not record.acknowledged]
    logger.info(
        "finalizing %s/%s: %s placement(s) still pending",
        namespace,
        resource.metadata.name,
        len(pending),
    )
    if pending:
        raise kopf.TemporaryError(
            f"{len(pending)} placement(s) still await cleanup acknowledgment",
            delay=30,
        )
    finalizers = list(resource.metadata.finalizers or [])
    patch.metadata["finalizers"] = [item for item in finalizers if item != STATIC_SET_FINALIZER]


def _now() -> str:
    """Return the current time as RFC 3339 UTC.

    Returns:
        The timestamp.
    """
    return datetime.datetime.now(datetime.UTC).replace(microsecond=0).isoformat()


def validate_static_set(**kwargs: Any) -> None:
    """Refuse an object the operator could not service.

    Args:
        **kwargs: The webhook arguments kopf passes, including ``body``.

    Raises:
        kopf.AdmissionError: If the object has problems.
    """
    reject_unsupported(kwargs["body"])


# The handler is registered only when admissions are actually being served. kopf
# refuses to start at all when admission handlers exist but no admission server is
# configured in ``settings.admission.server`` — and that server is configured by
# ``based_operators.webhook``, which the chart imports with ``-m`` only when its
# webhook is enabled. Registering unconditionally therefore crash-loops the controller
# in the default deployment, and the switch is the chart's own environment variable
# rather than a guess from the command line.
if os.environ.get(WEBHOOK_ENV) == "1":
    kopf.on.validate(GROUP, VERSION, PLURAL, operations=["CREATE", "UPDATE"])(validate_static_set)
