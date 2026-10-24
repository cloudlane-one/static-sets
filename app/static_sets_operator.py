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
from kubernetes.client import (
    V1ConfigMap,
    V1DaemonSet,
    V1DeleteOptions,
    V1Deployment,
    V1Node,
    V1ObjectMeta,
    V1Pod,
)

import based_operators as bk  # noqa: F401 - the resource class carries the resource config

from static_sets.api import ApiUnavailableError, KubernetesClient, KubernetesObject, upsert
from static_sets.controller import CleanupAction, ObservedState, ObservedWorkload, plan
from static_sets.models import StaticSet
from static_sets.naming import (
    MANAGED_BY,
    MIRROR_ANNOTATION,
    COMPONENT_LABEL,
    PLACEMENT_COMPONENT,
    PLACEMENT_LABEL,
    SET_LABEL,
    STATIC_SET_FINALIZER,
    placement_configmap_name,
)
from static_sets.placement import InstallerPod, NodeInfo, ObservedPlacement
from static_sets.validation import strict_problems

__all__ = [
    "KubernetesClient",
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


INSTALLER_COMPONENT = "installer"
"""Value of ``app.kubernetes.io/component`` on the installer workload."""

_MODEL_FOR_KIND: dict[str, type[KubernetesObject]] = {
    "ConfigMap": V1ConfigMap,
    "Deployment": V1Deployment,
    "DaemonSet": V1DaemonSet,
    "Pod": V1Pod,
}
"""The official client's model for each kind the planner can emit."""

TIMER_INTERVAL = 120
"""Seconds between periodic re-reconciles, so pending cleanups are retried."""


def get_client() -> KubernetesClient:
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


def read_snapshot(client: KubernetesClient, resource: StaticSet) -> ObservedState:
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
    pods = (
        client.core.list_namespaced_pod(namespace, label_selector=f"{SET_LABEL}={name}").items or []
    )
    nodes = client.core.list_node().items or []
    config_maps = (
        client.core.list_namespaced_config_map(
            namespace,
            label_selector=selector(**{COMPONENT_LABEL: PLACEMENT_COMPONENT, SET_LABEL: name}),
        ).items
        or []
    )
    return ObservedState(
        nodes=tuple(_node(node) for node in nodes),
        installer_pods=tuple(
            pod for pod in (_installer_pod(item) for item in pods) if pod is not None
        ),
        placements=tuple(
            placement
            for placement in (_placement(config_map) for config_map in config_maps)
            if placement is not None
        ),
        workloads=tuple(
            workload for workload in (_mirror_pod(item) for item in pods) if workload is not None
        ),
        synced=True,
        observed_at=_now(),
    )


def apply_plan(
    client: KubernetesClient,
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
        _, manifest = result.workload
        _apply(client, namespace, manifest)
    for action in result.cleanup:
        _publish_intent(client, namespace, action)
        if action.worker is not None:
            _apply(client, namespace, action.worker)
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


def _node(node: V1Node) -> NodeInfo:
    """Convert one API node into the shape the planner expects.

    Args:
        node: The node, as the official client models it.

    Returns:
        The node.
    """
    conditions = (node.status.conditions if node.status else None) or []
    ready = any(
        condition.type == "Ready" and condition.status == "True" for condition in conditions
    )
    metadata = node.metadata
    return NodeInfo(
        name=(metadata.name if metadata else None) or "",
        labels=(metadata.labels if metadata else None) or {},
        unschedulable=bool(node.spec.unschedulable) if node.spec else False,
        ready=ready,
    )


def _installer_pod(pod: V1Pod) -> InstallerPod | None:
    """Convert one API Pod into an installer Pod, when it is one.

    Args:
        pod: The Pod, as the official client models it.

    Returns:
        The installer Pod, or ``None`` when the Pod is not a live installer.
    """
    metadata = pod.metadata
    annotations = (metadata.annotations if metadata else None) or {}
    if ((metadata.labels if metadata else None) or {}).get(COMPONENT_LABEL) != INSTALLER_COMPONENT:
        return None
    if MIRROR_ANNOTATION in annotations:
        return None
    return InstallerPod(
        name=(metadata.name if metadata else None) or "",
        uid=(metadata.uid if metadata else None) or "",
        node=pod.spec.node_name if pod.spec else None,
        phase=(pod.status.phase if pod.status else None) or "Pending",
        deleting=bool(metadata.deletion_timestamp) if metadata else False,
    )


def _mirror_pod(pod: V1Pod) -> ObservedWorkload | None:
    """Convert one API Pod into observed workload health, when it is a mirror Pod.

    Args:
        pod: The Pod, as the official client models it.

    Returns:
        The observed workload, or ``None`` when the Pod is not a mirror Pod.
    """
    metadata = pod.metadata
    if MIRROR_ANNOTATION not in ((metadata.annotations if metadata else None) or {}):
        return None
    containers = (pod.status.container_statuses if pod.status else None) or []
    ready = all(container.ready for container in containers) if containers else None
    return ObservedWorkload(
        name=(metadata.name if metadata else None) or "",
        node=(pod.spec.node_name if pod.spec else None) or "",
        phase=pod.status.phase if pod.status else None,
        ready=ready,
        restart_count=sum(container.restart_count or 0 for container in containers),
    )


def _placement(config_map: V1ConfigMap) -> ObservedPlacement | None:
    """Convert one placement ConfigMap into an observed placement.

    Args:
        config_map: The ConfigMap, as the official client models it.

    Returns:
        The observed placement, or ``None`` when the ConfigMap is not a placement.
    """
    data = config_map.data or {}
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


def _apply(client: KubernetesClient, namespace: str, manifest: dict[str, Any]) -> None:
    """Create or replace an object, leaving it in the desired shape.

    The manifest is the planner's own output, which is already validated. It is
    turned into the official client's model for its kind, so that serialization is
    the client's job and a field this operator does not model is not guessed at.

    Args:
        client: The API client.
        namespace: The namespace the object belongs to.
        manifest: The desired object.
    """
    model = _MODEL_FOR_KIND[manifest["kind"]].model_validate(manifest)
    upsert(client, namespace, model)


def _delete_installers(client: KubernetesClient, *, namespace: str, name: str) -> None:
    """Delete both installer workload kinds, so a mode switch leaves nothing behind.

    Args:
        client: The API client.
        namespace: The namespace the StaticSet lives in.
        name: The StaticSet's name.
    """
    options = V1DeleteOptions(propagation_policy="Background")
    deployments = (
        client.apps.list_namespaced_deployment(
            namespace, label_selector=f"{SET_LABEL}={name}"
        ).items
        or []
    )
    for deployment in deployments:
        if deployment.metadata and deployment.metadata.name:
            client.apps.delete_namespaced_deployment(
                deployment.metadata.name, namespace, body=options
            )
    daemon_sets = (
        client.apps.list_namespaced_daemon_set(
            namespace, label_selector=f"{SET_LABEL}={name}"
        ).items
        or []
    )
    for daemon_set in daemon_sets:
        if daemon_set.metadata and daemon_set.metadata.name:
            client.apps.delete_namespaced_daemon_set(
                daemon_set.metadata.name, namespace, body=options
            )


def _publish_intent(client: KubernetesClient, namespace: str, action: CleanupAction) -> None:
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
    upsert(
        client,
        namespace,
        V1ConfigMap(
            api_version="v1",
            kind="ConfigMap",
            metadata=V1ObjectMeta(
                name=name,
                namespace=namespace,
                labels={
                    "app.kubernetes.io/managed-by": MANAGED_BY,
                    COMPONENT_LABEL: PLACEMENT_COMPONENT,
                    SET_LABEL: static_set,
                    PLACEMENT_LABEL: action.identity,
                },
            ),
            data=dict(action.payload),
        ),
    )


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
