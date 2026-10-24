"""The StaticSet controller entry point.

This is the file ``kopf run`` executes inside the controller image, and it is
registered cluster-wide: it watches ``StaticSet`` objects in every namespace. It
does two things and nothing else: it reads the cluster into an
:class:`~static_sets.controller.ObservedState`, and it applies the
:class:`~static_sets.controller.ReconcilePlan` that
:func:`~static_sets.controller.plan` derives from it. Every decision lives in the
library, where it is unit-tested; this module is only the adapter that talks to
Kubernetes.

The controller never touches a host filesystem. Placement is decided by the
Kubernetes scheduler through a probe workload; the manifests are written by the
``static-pod-manager`` DaemonSet, one Pod per node. The controller's job is to decide
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
from typing import Any, cast

import kopf
from kubernetes.client import (
    V1DaemonSet,
    V1DeleteOptions,
    V1Pod,
    V1ReplicaSet,
)

import based_operators as bk  # the resource class carries the resource config

from static_sets.api import (
    ApiRequestError,
    ApiUnavailableError,
    KubernetesClient,
    KubernetesObject,
    apply_placement,
    delete_placement,
    upsert,
)
from static_sets.controller import (
    ObservedPlacement,
    ObservedState,
    PlacementChange,
    ProbeRef,
    plan,
)
from static_sets.models import PlacementPhase, StaticSet
from static_sets.naming import (
    COMPONENT_LABEL,
    GENERATION_LABEL,
    MIRROR_ANNOTATION,
    PLACEMENT_LABEL,
    PROBE_COMPONENT,
    SET_UID_LABEL,
    STATIC_SET_FINALIZER,
    placement_labels,
)
from static_sets.placements import PlacementSpec, PlacementState, StaticPodPlacement
from static_sets.probe import NodeInfo, ObservedProbePod, ObservedProbeWorkload, ObservedWorkload
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

_MODEL_FOR_KIND: dict[str, type[KubernetesObject]] = {
    "DaemonSet": V1DaemonSet,
    "ReplicaSet": V1ReplicaSet,
    "Pod": V1Pod,
}
"""The official client's model for each kind the planner can emit."""

TIMER_INTERVAL = 120
"""Seconds between periodic re-reconciles, so a missing placement is noticed."""


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
    uid = resource.metadata.uid or ""
    owned = selector(**{SET_UID_LABEL: uid})
    pods = client.core.list_namespaced_pod(namespace, label_selector=owned).items or []
    nodes = client.core.list_node().items or []
    return ObservedState(
        nodes=tuple(_node(node) for node in nodes),
        probes=_probes(client, namespace, owned=owned, pods=pods),
        placements=_placements(client, namespace, owned=owned),
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
    for reference in result.delete_probes:
        _delete_probe(client, namespace, reference)
    if result.probe is not None:
        _, manifest = result.probe
        _apply(client, namespace, manifest)
    for change in result.placements:
        _apply_placement(client, resource, namespace, change)
    return result


def publish_status(patch: kopf.Patch, result: Any) -> None:
    """Write a plan's status into the object's status subresource.

    Args:
        patch: The kopf patch of the object being reconciled.
        result: The plan that was applied.
    """
    status = result.status.model_dump(mode="json", by_alias=True, exclude_none=True)
    patch.status["observedGeneration"] = status.get("observedGeneration", 0)
    patch.status["placedGeneration"] = status.get("placedGeneration", 0)
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


def _node(node: Any) -> NodeInfo:
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


def _probes(
    client: KubernetesClient,
    namespace: str,
    *,
    owned: str,
    pods: list[Any],
) -> tuple[ObservedProbeWorkload, ...]:
    """Read the scheduling probes of one StaticSet.

    Args:
        client: The API client.
        namespace: The namespace the StaticSet lives in.
        owned: The selector matching everything the StaticSet owns.
        pods: The Pods already listed for that selector.

    Returns:
        The observed probe workloads, with their Pods attached.
    """
    label = selector(**{COMPONENT_LABEL: PROBE_COMPONENT})
    by_owner: dict[str, list[ObservedProbePod]] = {}
    for pod in pods:
        metadata = pod.metadata
        if ((metadata.labels if metadata else None) or {}).get(COMPONENT_LABEL) != PROBE_COMPONENT:
            continue
        owner = _owner_name(pod)
        if owner is None:
            continue
        by_owner.setdefault(owner, []).append(
            ObservedProbePod(
                name=(metadata.name if metadata else None) or "",
                node=pod.spec.node_name if pod.spec else None,
                deleting=bool(metadata.deletion_timestamp) if metadata else False,
            )
        )
    probes: list[ObservedProbeWorkload] = []
    for kind, operation in (
        ("ReplicaSet", client.apps.list_namespaced_replica_set),
        ("DaemonSet", client.apps.list_namespaced_daemon_set),
    ):
        items = operation(namespace, label_selector=f"{owned},{label}").items or []
        for item in items:
            name = (item.metadata.name if item.metadata else None) or ""
            probes.append(_probe(kind, item, by_owner.get(name, [])))
    return tuple(probes)


def _owner_name(pod: V1Pod) -> str | None:
    """Return the name of the controller that owns a Pod.

    Args:
        pod: The Pod.

    Returns:
        The owning workload's name, or ``None`` when no owner is recorded yet.
    """
    metadata = pod.metadata
    if metadata is None or metadata.name is None:
        return None
    for reference in metadata.owner_references or []:
        if reference.controller:
            return reference.name
    return None


def _probe(
    kind: str,
    item: Any,
    pods: list[ObservedProbePod],
) -> ObservedProbeWorkload:
    """Convert one API workload into an observed probe.

    Args:
        kind: ``ReplicaSet`` or ``DaemonSet``.
        item: The workload, as the official client models it.
        pods: The Pods it owns.

    Returns:
        The observed probe.
    """
    metadata = item.metadata
    labels = (metadata.labels if metadata else None) or {}
    status = item.status
    if kind == "DaemonSet":
        replicas = int((status.desired_number_scheduled if status else 0) or 0)
    else:
        replicas = int((item.spec.replicas if item.spec else 0) or 0)
    return ObservedProbeWorkload(
        kind="DaemonSet" if kind == "DaemonSet" else "ReplicaSet",
        name=(metadata.name if metadata else None) or "",
        generation=int(labels.get(GENERATION_LABEL, "0") or 0),
        replicas=replicas,
        status_seen=status is not None,
        pods=tuple(pods),
    )


def _mirror_pod(pod: V1Pod) -> ObservedWorkload | None:
    """Convert one API Pod into observed workload health, when it is a mirror Pod.

    Args:
        pod: The Pod, as the official client models it.

    Returns:
        The observed workload, or ``None`` when the Pod is not a mirror Pod.
    """
    metadata = pod.metadata
    annotations = (metadata.annotations if metadata else None) or {}
    if MIRROR_ANNOTATION not in annotations:
        return None
    containers = (pod.status.container_statuses if pod.status else None) or []
    ready = all(container.ready for container in containers) if containers else None
    return ObservedWorkload(
        name=(metadata.name if metadata else None) or "",
        node=(pod.spec.node_name if pod.spec else None) or "",
        placement=annotations.get(PLACEMENT_LABEL),
        revision=annotations.get("staticsets.cloudlane.dev/revision"),
        phase=pod.status.phase if pod.status else None,
        ready=ready,
        restart_count=sum(container.restart_count or 0 for container in containers),
    )


def _placements(
    client: KubernetesClient,
    namespace: str,
    *,
    owned: str,
) -> tuple[ObservedPlacement, ...]:
    """Read the placement records of one StaticSet.

    Args:
        client: The API client.
        namespace: The namespace the StaticSet lives in.
        owned: The selector matching everything the StaticSet owns.

    Returns:
        The observed placements.
    """
    config = StaticPodPlacement._require_resource_config()
    items = cast(
        "dict[str, Any]",
        client.custom.list_namespaced_custom_object(
            config.group,
            config.version,
            namespace,
            config.plural,
            label_selector=owned,
        ),
    )
    placements: list[ObservedPlacement] = []
    for item in items.get("items", []):
        placement = StaticPodPlacement.model_validate(_custom_body(config, item))
        status = placement.status or PlacementState()
        placements.append(
            ObservedPlacement(
                name=placement.metadata.name or "",
                identity=_identity_of(placement.spec),
                node=placement.spec.node,
                desired=placement.spec.desired,
                reason=placement.spec.reason,
                generation=placement.spec.generation,
                revision=status.revision,
                phase=status.phase,
                acknowledged=status.acknowledged,
                static_pod_name=status.static_pod_name,
                message=status.message,
            )
        )
    return tuple(placements)


def _custom_body(config: Any, item: dict[str, Any]) -> dict[str, Any]:
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


def _identity_of(spec: PlacementSpec) -> str:
    """Return the placement identity implied by a spec.

    Args:
        spec: The placement's spec.

    Returns:
        The identity.
    """
    from static_sets.naming import placement_identity

    return placement_identity(spec.static_set_uid, spec.node)


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


def _delete_probe(client: KubernetesClient, namespace: str, reference: ProbeRef) -> None:
    """Delete one scheduling probe, tolerating its absence.

    Args:
        client: The API client.
        namespace: The namespace the probe lives in.
        reference: The probe to delete.
    """
    options = V1DeleteOptions(propagation_policy="Background")
    operation = (
        client.apps.delete_namespaced_daemon_set
        if reference.kind == "DaemonSet"
        else client.apps.delete_namespaced_replica_set
    )
    try:
        operation(reference.name, namespace, body=options)
    except ApiRequestError as error:
        if error.status != 404:
            raise


def _apply_placement(
    client: KubernetesClient,
    resource: StaticSet,
    namespace: str,
    change: PlacementChange,
) -> None:
    """Create, update or delete one placement record.

    Args:
        client: The API client.
        resource: The owning StaticSet.
        namespace: The namespace the placement lives in.
        change: The write to perform.
    """
    if change.kind == "delete":
        delete_placement(client, namespace, change.name)
        return
    if change.spec is None:  # pragma: no cover - the planner never emits this
        raise ValueError(f"an apply of {change.name} carries no spec")
    apply_placement(
        client,
        StaticPodPlacement(
            metadata=bk.ObjectMeta.model_validate(
                {
                    "name": change.name,
                    "namespace": namespace,
                    "labels": placement_labels(
                        resource.metadata.name or "", resource.metadata.uid or ""
                    ),
                    "ownerReferences": _owner(resource),
                }
            ),
            spec=change.spec,
        ),
    )


def _owner(resource: StaticSet) -> list[dict[str, Any]]:
    """Return the owner reference tying a placement to its StaticSet.

    Args:
        resource: The owning StaticSet.

    Returns:
        A single-entry list, or an empty list when the UID is unknown.
    """
    if not resource.metadata.uid:
        return []
    return [
        {
            "apiVersion": resource.apiVersion,
            "kind": resource.kind,
            "name": resource.metadata.name or "",
            "uid": resource.metadata.uid,
            "controller": True,
            "blockOwnerDeletion": True,
        }
    ]


@StaticSet.on.create
@StaticSet.on.resume
@StaticSet.on.update
@StaticSet.on.timer(interval=TIMER_INTERVAL, initial_delay=30, idle=10)
def reconcile(resource: StaticSet, patch: bk.Patch, logger: Any, **_: Any) -> None:
    """Bring the placements of a StaticSet in line with its specification.

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
    leftovers = [
        placement for placement in state.placements if placement.phase is not PlacementPhase.CLEANED
    ]
    logger.info(
        "finalizing %s/%s: %s placement(s) still pending",
        namespace,
        resource.metadata.name,
        len(pending),
    )
    if pending or leftovers:
        raise kopf.TemporaryError(
            f"{max(len(pending), len(leftovers))} placement(s) still await cleanup acknowledgment",
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
