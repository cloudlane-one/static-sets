"""Turn a StaticSet and the observed cluster into a reconcile plan.

The controller is split in two on purpose. Everything that decides *what should
happen* lives here, as a pure function of the StaticSet and a snapshot of the
cluster, so it can be tested exhaustively without a cluster. The thin kopf adapter
in ``app/`` only reads the snapshot and applies the plan.

Two rules shape the whole module:

* an incomplete view is never an empty view. If watch synchronization has not
  finished, or the operator could not read the placements, the plan performs **no**
  destructive action and asks to be re-queued. Only a placement that is
  authoritatively observed to have no installer is cleaned up;
* cleanup is a protocol, not a side effect. A placement that is no longer wanted
  gets a persisted intent and either a request to the installer that still serves it
  or a restricted cleanup worker pinned to the recorded node. Nothing claims the
  remote workload is gone until an acknowledgment says so.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

from static_sets.models import (
    CleanupReason,
    CleanupRecord,
    CleanupState,
    Condition,
    Mode,
    NodeUnreachablePolicy,
    PlacementPhase,
    PlacementStatus,
    StaticSet,
    StaticSetStatus,
    WorkloadHealth,
)
from static_sets.naming import (
    ANNOTATION_GENERATION,
    ANNOTATION_PLACEMENT,
    CLEANUP_FINALIZER,
    MANAGED_BY,
    PLACEMENT_LABEL,
    cleanup_worker_name,
    installer_labels,
    placement_identity,
    static_pod_name,
)
from static_sets.placement import (
    CLEANUP_COMMAND,
    BUNDLE_MOUNT,
    INSTALLER_CONTAINER,
    STATIC_POD_MOUNT,
    InstallerPod,
    NodeInfo,
    ObservedPlacement,
    eligible_nodes,
    installer_workload,
)

__all__ = [
    "CleanupAction",
    "ObservedState",
    "ObservedWorkload",
    "ReconcilePlan",
    "cleanup_worker_pod",
    "plan",
]


@dataclass(frozen=True)
class ObservedWorkload:
    """A mirror Pod kubelet created for a static Pod.

    Attributes:
        name: The mirror Pod's name, which equals the static Pod's name.
        node: The node it runs on.
        phase: The mirror Pod's phase, when known.
        ready: Whether the mirror Pod reports ``Ready``, when known.
        restart_count: The containers' aggregate restart count, when known.
    """

    name: str
    node: str
    phase: str | None = None
    ready: bool | None = None
    restart_count: int | None = None


@dataclass(frozen=True)
class ObservedState:
    """Everything the plan is computed from.

    Attributes:
        nodes: The nodes of the cluster.
        installer_pods: The installer Pods currently observed.
        placements: The placements acknowledged by installers.
        workloads: The mirror Pods kubelet created for the static workloads.
        synced: Whether watch synchronization has completed. When ``False`` the
            plan is non-destructive.
        observed_at: A timestamp used for condition transitions.
    """

    nodes: tuple[NodeInfo, ...] = ()
    installer_pods: tuple[InstallerPod, ...] = ()
    placements: tuple[ObservedPlacement, ...] = ()
    workloads: tuple[ObservedWorkload, ...] = ()
    synced: bool = True
    observed_at: str = ""


@dataclass(frozen=True)
class CleanupAction:
    """One persisted cleanup intent, with the worker that may carry it out.

    Attributes:
        record: The cleanup record to publish in status.
        node: The node the workload lives on.
        identity: The placement identity.
        pod_name: The static Pod's name.
        worker: The cleanup worker Pod manifest, when one is needed.
        payload: The intent as data, for the ConfigMap that carries it to the node.
    """

    record: CleanupRecord
    node: str
    identity: str
    pod_name: str
    worker: dict[str, Any] | None
    payload: Mapping[str, str]


@dataclass(frozen=True)
class ReconcilePlan:
    """What the adapter should do, and what it should report.

    Attributes:
        workload: The installer workload to apply, as ``(kind, manifest)``, or
            ``None`` when nothing should be created.
        delete_workload: Whether the installer workload should be deleted instead.
        cleanup: The cleanup actions to persist and act on.
        status: The status to publish.
        requeue: Whether to reconcile again shortly.
    """

    workload: tuple[Literal["Deployment", "DaemonSet"], dict[str, Any]] | None
    delete_workload: bool
    cleanup: tuple[CleanupAction, ...]
    status: StaticSetStatus
    requeue: bool


def plan(
    static_set: StaticSet,
    observed: ObservedState,
    *,
    namespace: str,
) -> ReconcilePlan:
    """Compute the reconcile plan for a StaticSet.

    Args:
        static_set: The StaticSet being reconciled.
        observed: The snapshot of the cluster.
        namespace: The namespace the StaticSet lives in.

    Returns:
        The plan to apply.
    """
    generation = static_set.metadata.generation or 0
    if not observed.synced:
        return ReconcilePlan(
            workload=None,
            delete_workload=False,
            cleanup=(),
            status=_status_without_action(
                static_set,
                observed,
                reason="Synchronizing",
                message=(
                    "the operator has not finished synchronizing its view; no action is "
                    "taken until it has"
                ),
            ),
            requeue=True,
        )
    if static_set.metadata.deletion_timestamp is not None:
        return _deletion_plan(static_set, observed, namespace=namespace)
    kind, manifest = installer_workload(static_set, namespace=namespace, generation=generation)
    live = _live_installers(observed.installer_pods)
    cleanup = _cleanup_actions(static_set, observed, live=live, namespace=namespace)
    return ReconcilePlan(
        workload=(kind, manifest),
        delete_workload=False,
        cleanup=cleanup,
        status=_running_status(static_set, observed, live=live, cleanup=cleanup),
        requeue=any(not action.record.acknowledged for action in cleanup),
    )


def _live_installers(installer_pods: tuple[InstallerPod, ...]) -> dict[str, InstallerPod]:
    """Index the installer Pods that currently serve a node.

    A Pod that is terminating no longer serves its node, so its placement becomes a
    cleanup candidate rather than a live one.

    Args:
        installer_pods: The observed installer Pods.

    Returns:
        The node name to installer Pod mapping, newest name winning.
    """
    live: dict[str, InstallerPod] = {}
    for pod in installer_pods:
        if pod.node is None or pod.deleting:
            continue
        live[pod.node] = pod
    return live


def _deletion_plan(
    static_set: StaticSet,
    observed: ObservedState,
    *,
    namespace: str,
) -> ReconcilePlan:
    """Compute the plan for a StaticSet that is being deleted.

    Deletion is the one case where every observed placement is cleaned up, because
    the user's intent is unambiguous: the object is going away and its static Pods
    should go with it. The installer workload is deleted first, but nothing is
    released until each placement acknowledges removal.

    Args:
        static_set: The StaticSet being deleted.
        observed: The snapshot of the cluster.
        namespace: The namespace the StaticSet lives in.

    Returns:
        The plan to apply.
    """
    live = _live_installers(observed.installer_pods)
    ready = _ready_nodes(observed)
    cleanup = tuple(
        _cleanup_action(
            static_set,
            placement,
            reason=CleanupReason.RESOURCE_DELETED,
            installer=live.get(placement.node),
            node_ready=placement.node in ready,
            namespace=namespace,
            observed_at=observed.observed_at,
        )
        for placement in observed.placements
    )
    acknowledged = all(action.record.acknowledged for action in cleanup)
    status = StaticSetStatus(
        observed_generation=static_set.metadata.generation or 0,
        desired_revision=None,
        placements=[
            PlacementStatus(
                identity=action.identity,
                node=action.node,
                revision=action.record.revision,
                phase=PlacementPhase.CLEANING,
                acknowledged=action.record.acknowledged,
                generation=action.record.generation,
                message=action.record.message,
            )
            for action in cleanup
        ],
        cleanup=[action.record for action in cleanup],
        conditions=[
            _condition(
                "Deleting",
                "True",
                "ResourceDeleted",
                "the StaticSet is being deleted; placements are being cleaned up",
                static_set,
                observed,
            ),
            _condition(
                "Ready",
                "False",
                "Deleting",
                "the StaticSet is being deleted",
                static_set,
                observed,
            ),
            _condition(
                "CleanedUp",
                "True" if acknowledged else "False",
                "CleanupAcknowledged" if acknowledged else "CleanupPending",
                (
                    "every placement acknowledged cleanup"
                    if acknowledged
                    else "waiting for placements to acknowledge cleanup"
                ),
                static_set,
                observed,
            ),
        ],
    )
    return ReconcilePlan(
        workload=None,
        delete_workload=True,
        cleanup=cleanup,
        status=status,
        requeue=not acknowledged,
    )


def _cleanup_actions(
    static_set: StaticSet,
    observed: ObservedState,
    *,
    live: dict[str, InstallerPod],
    namespace: str,
) -> tuple[CleanupAction, ...]:
    """Return the cleanup actions for placements no installer serves any more.

    Args:
        static_set: The StaticSet being reconciled.
        observed: The snapshot of the cluster.
        live: The installer Pods that currently serve a node.
        namespace: The namespace the StaticSet lives in.

    Returns:
        The cleanup actions, sorted by placement identity.
    """
    actions: list[CleanupAction] = []
    ready = _ready_nodes(observed)
    for placement in observed.placements:
        if placement.node in live and placement.state != "Removed":
            continue
        if placement.state == "Removed":
            continue
        actions.append(
            _cleanup_action(
                static_set,
                placement,
                reason=_cleanup_reason(static_set, observed, live=live),
                installer=live.get(placement.node),
                node_ready=placement.node in ready,
                namespace=namespace,
                observed_at=observed.observed_at,
            )
        )
    return tuple(sorted(actions, key=lambda action: action.identity))


def _ready_nodes(observed: ObservedState) -> set[str]:
    """Return the nodes that report ``Ready``.

    A node whose kubelet is not reporting Ready is not reachable for cleanup
    purposes either, so its placements are reported as pending rather than chased.

    Args:
        observed: The snapshot of the cluster.

    Returns:
        The names of the nodes that report Ready.
    """
    return {node.name for node in observed.nodes if node.ready}


def _cleanup_reason(
    static_set: StaticSet,
    observed: ObservedState,
    *,
    live: dict[str, InstallerPod],
) -> CleanupReason:
    """Decide why a placement is being cleaned up.

    Args:
        static_set: The StaticSet being reconciled.
        observed: The snapshot of the cluster.
        live: The installer Pods that currently serve a node.

    Returns:
        The cleanup reason recorded in the intent.
    """
    spec = static_set.spec
    if spec.mode is Mode.DEPLOYMENT and spec.replicas is not None and len(live) < spec.replicas:
        return CleanupReason.SCALED_DOWN
    if spec.mode is Mode.DAEMON_SET:
        eligible = set(eligible_nodes(observed.nodes, spec))
        if any(
            placement.node not in eligible
            for placement in observed.placements
            if placement.node not in live
        ):
            return CleanupReason.DESELECTED
    return CleanupReason.RELOCATED


def _cleanup_action(
    static_set: StaticSet,
    placement: ObservedPlacement,
    *,
    reason: CleanupReason,
    installer: InstallerPod | None,
    node_ready: bool,
    namespace: str,
    observed_at: str,
) -> CleanupAction:
    """Build one cleanup action, including a worker when the installer is gone.

    Args:
        static_set: The StaticSet being reconciled.
        placement: The placement being cleaned up.
        reason: Why it is being cleaned up.
        installer: The installer Pod still serving the placement, if any.
        node_ready: Whether the node holding the placement reports Ready.
        namespace: The namespace the StaticSet lives in.
        observed_at: The current time, RFC 3339, stamped onto the intent.

    Returns:
        The cleanup action.
    """
    generation = static_set.metadata.generation or 0
    pod_name = static_pod_name(static_set.metadata.name or "", placement.identity)
    if installer is not None:
        state = CleanupState.REQUESTED
    elif node_ready:
        state = CleanupState.QUEUED
    else:
        state = CleanupState.BLOCKED
    record = CleanupRecord(
        identity=placement.identity,
        node=placement.node,
        revision=placement.revision,
        generation=generation,
        reason=reason,
        state=state,
        acknowledged=placement.state == "Removed",
        message=_cleanup_message(static_set, placement, installer, node_ready),
    )
    worker = None
    if installer is None and node_ready:
        worker = cleanup_worker_pod(
            static_set,
            namespace=namespace,
            identity=placement.identity,
            node=placement.node,
            generation=generation,
            reason=reason.value,
            pod_name=pod_name,
        )
    return CleanupAction(
        record=record,
        node=placement.node,
        identity=placement.identity,
        pod_name=pod_name,
        worker=worker,
        payload={
            "cleanup": "true",
            "placement": placement.identity,
            "podName": pod_name,
            "staticSet": static_set.metadata.name or "",
            "uid": static_set.metadata.uid or "",
            "namespace": namespace,
            "node": placement.node,
            "generation": str(generation),
            "reason": reason.value,
            "removeConfiguration": (
                "true" if static_set.spec.cleanup.remove_configuration_after_ack else "false"
            ),
            "requestedAt": observed_at,
        },
    )


def _cleanup_message(
    static_set: StaticSet,
    placement: ObservedPlacement,
    installer: InstallerPod | None,
    node_ready: bool,
) -> str:
    """Explain the cleanup state for status.

    Args:
        static_set: The StaticSet being reconciled.
        placement: The placement being cleaned up.
        installer: The installer Pod still serving it, if any.
        node_ready: Whether the node holding the placement reports Ready.

    Returns:
        The explanation.
    """
    if installer is not None:
        return "cleanup requested from the installer that serves this placement"
    policy = static_set.spec.cleanup.on_node_unreachable
    if not node_ready:
        grace = static_set.spec.cleanup.abandonment_grace_seconds
        followed = (
            f", after which an administrator may abandon it following a {grace}s grace period"
            if policy is NodeUnreachablePolicy.ABANDON_AFTER_GRACE
            else ""
        )
        return (
            f"node {placement.node} is not Ready, so the static workload there cannot be "
            "reached; cleanup stays pending and a residual workload may still be running "
            f"there. The declared policy is {policy.value}" + followed
        )
    return (
        f"no installer serves node {placement.node}; a restricted cleanup worker is queued. "
        "The workload is reported as pending until the worker acknowledges removal"
    )


def _running_status(
    static_set: StaticSet,
    observed: ObservedState,
    *,
    live: dict[str, InstallerPod],
    cleanup: tuple[CleanupAction, ...],
) -> StaticSetStatus:
    """Build the status of a StaticSet that is not being deleted.

    Args:
        static_set: The StaticSet being reconciled.
        observed: The snapshot of the cluster.
        live: The installer Pods that currently serve a node.
        cleanup: The cleanup actions computed for this reconcile.

    Returns:
        The status to publish.
    """
    name = static_set.metadata.name or ""
    acknowledged = {placement.identity: placement for placement in observed.placements}
    placements: list[PlacementStatus] = []
    for node in sorted(live):
        identity = placement_identity(static_set.metadata.uid or "", node)
        placement = acknowledged.get(identity)
        if placement is None:
            placements.append(
                PlacementStatus(identity=identity, node=node, phase=PlacementPhase.PENDING)
            )
            continue
        placements.append(
            PlacementStatus(
                identity=identity,
                node=node,
                revision=placement.revision,
                phase=(
                    PlacementPhase.INSTALLED if placement.acknowledged else PlacementPhase.PENDING
                ),
                acknowledged=placement.acknowledged,
                generation=static_set.metadata.generation or 0,
                message=placement.message,
            )
        )
    installed = [item for item in placements if item.phase is PlacementPhase.INSTALLED]
    revisions = sorted({item.revision for item in installed if item.revision})
    health = _health(static_set, placements, observed)
    pending_cleanup = [action for action in cleanup if not action.record.acknowledged]
    ready = _is_ready(static_set, observed, placements=placements, installed=installed)
    status = StaticSetStatus(
        observed_generation=static_set.metadata.generation or 0,
        desired_revision=revisions[-1] if revisions else None,
        placements=placements,
        health=health,
        cleanup=[action.record for action in cleanup],
        conditions=[
            _condition(
                "Synchronized",
                "True",
                "Synchronized",
                "the operator's view of the cluster is complete",
                static_set,
                observed,
            ),
            _condition(
                "Ready",
                "True" if ready else "False",
                "AllPlacementsInstalled" if ready else "PlacementsPending",
                (
                    "every desired placement is installed and acknowledged"
                    if ready
                    else f"{len(installed)} of {_desired_count(static_set, observed)} placement(s) "
                    "acknowledged installation"
                ),
                static_set,
                observed,
            ),
            _condition(
                "CleanupPending",
                "True" if pending_cleanup else "False",
                "CleanupPending" if pending_cleanup else "CleanupComplete",
                (
                    f"{len(pending_cleanup)} placement(s) await cleanup acknowledgment"
                    if pending_cleanup
                    else "no placement awaits cleanup"
                ),
                static_set,
                observed,
            ),
        ],
    )
    if name:
        status.conditions.append(
            _condition(
                "InstallersRunning",
                "True" if live else "False",
                "InstallersLive" if live else "NoInstallers",
                f"{len(live)} installer(s) currently serve {name}",
                static_set,
                observed,
            )
        )
    if any(item.phase is PlacementPhase.FAILED for item in placements):
        status.conditions.append(
            _condition(
                "Degraded",
                "True",
                "PlacementFailed",
                "at least one placement reported a failure",
                static_set,
                observed,
            )
        )
    return status


def _health(
    static_set: StaticSet,
    placements: list[PlacementStatus],
    observed: ObservedState,
) -> list[WorkloadHealth]:
    """Match observed mirror Pods to placements.

    Health is reported separately from installed state and never feeds a decision:
    a placement can be installed and unhealthy, or installed and unobservable.

    Args:
        static_set: The StaticSet being reconciled.
        placements: The placements computed for this reconcile.
        observed: The snapshot of the cluster.

    Returns:
        The observed workload health, one entry per placement.
    """
    by_name = {workload.name: workload for workload in observed.workloads}
    health: list[WorkloadHealth] = []
    for placement in placements:
        mirror_name = static_pod_name(static_set.metadata.name or "", placement.identity)
        mirror = by_name.get(mirror_name)
        if mirror is None:
            health.append(
                WorkloadHealth(
                    identity=placement.identity,
                    node=placement.node,
                    observed=False,
                    message="no mirror Pod observed; kubelet may not have started the workload yet",
                )
            )
            continue
        health.append(
            WorkloadHealth(
                identity=placement.identity,
                node=placement.node,
                observed=True,
                phase=mirror.phase,
                ready=mirror.ready,
                restart_count=mirror.restart_count,
            )
        )
    return health


def _desired_count(static_set: StaticSet, observed: ObservedState) -> int:
    """Return how many placements the specification asks for.

    Args:
        static_set: The StaticSet being reconciled.
        observed: The snapshot of the cluster.

    Returns:
        The desired number of placements.
    """
    if static_set.spec.mode is Mode.DAEMON_SET:
        return len(eligible_nodes(observed.nodes, static_set.spec))
    return static_set.spec.replicas or 0


def _is_ready(
    static_set: StaticSet,
    observed: ObservedState,
    *,
    placements: list[PlacementStatus],
    installed: list[PlacementStatus],
) -> bool:
    """Return whether every desired placement is installed.

    Args:
        static_set: The StaticSet being reconciled.
        observed: The snapshot of the cluster.
        placements: The placements computed for this reconcile.
        installed: The installed subset of those placements.

    Returns:
        ``True`` when the desired number of placements is installed.
    """
    desired = _desired_count(static_set, observed)
    if desired == 0:
        return not placements
    return len(installed) == desired and len(placements) == desired


def _status_without_action(
    static_set: StaticSet,
    observed: ObservedState,
    *,
    reason: str,
    message: str,
) -> StaticSetStatus:
    """Build the status of a reconcile that deliberately did nothing.

    Args:
        static_set: The StaticSet being reconciled.
        observed: The snapshot of the cluster.
        reason: The condition reason.
        message: The condition message.

    Returns:
        The status to publish.
    """
    return StaticSetStatus(
        observed_generation=static_set.metadata.generation or 0,
        placements=[
            PlacementStatus(
                identity=placement.identity,
                node=placement.node,
                revision=placement.revision,
                acknowledged=placement.acknowledged,
            )
            for placement in observed.placements
        ],
        conditions=[
            _condition("Synchronized", "False", reason, message, static_set, observed),
            _condition(
                "Ready",
                "Unknown",
                reason,
                "readiness cannot be determined before synchronization completes",
                static_set,
                observed,
            ),
        ],
    )


def _condition(
    condition_type: str,
    status: Literal["True", "False", "Unknown"],
    reason: str,
    message: str,
    static_set: StaticSet,
    observed: ObservedState,
) -> Condition:
    """Build one status condition.

    Args:
        condition_type: The condition's type.
        status: The condition's status.
        reason: A short, machine-readable reason.
        message: A human-readable explanation.
        static_set: The StaticSet being reconciled.
        observed: The snapshot of the cluster.

    Returns:
        The condition.
    """
    return Condition(
        type=condition_type,
        status=status,
        reason=reason,
        message=message,
        observed_generation=static_set.metadata.generation,
        last_transition_time=observed.observed_at or None,
    )


def cleanup_worker_pod(
    static_set: StaticSet,
    *,
    namespace: str,
    identity: str,
    node: str,
    generation: int,
    reason: str,
    pod_name: str,
) -> dict[str, Any]:
    """Build the restricted cleanup worker Pod for one placement.

    A cleanup worker exists because an installer can disappear before it performed
    the cleanup it was asked to perform. The worker is deliberately narrow: it is
    pinned to one node, it mounts only the two directories an installer mounts, and
    it carries a finalizer so that the operator does not release the StaticSet's
    finalizer while the worker is still holding local state.

    Args:
        static_set: The StaticSet the placement belongs to.
        namespace: The namespace to create the worker in.
        identity: The placement identity.
        node: The node the workload lives on.
        generation: The StaticSet generation.
        reason: Why the cleanup is happening.
        pod_name: The static Pod's name.

    Returns:
        The cleanup worker Pod manifest.
    """
    installer = static_set.spec.installer
    name = cleanup_worker_name(static_set.metadata.name or "", identity)
    labels = {**installer_labels(static_set.metadata.name or ""), PLACEMENT_LABEL: identity}
    return {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {
            "name": name,
            "namespace": namespace,
            "labels": {
                **labels,
                "app.kubernetes.io/component": "cleanup",
                "app.kubernetes.io/managed-by": MANAGED_BY,
            },
            "annotations": {
                "staticsets.cloudlane.dev/cleanup-reason": reason,
                ANNOTATION_PLACEMENT: identity,
                ANNOTATION_GENERATION: str(generation),
            },
            "finalizers": [CLEANUP_FINALIZER],
            "ownerReferences": [
                {
                    "apiVersion": static_set.apiVersion,
                    "kind": static_set.kind,
                    "name": static_set.metadata.name,
                    "uid": static_set.metadata.uid,
                    "controller": False,
                    "blockOwnerDeletion": True,
                }
            ]
            if static_set.metadata.uid
            else [],
        },
        "spec": {
            "restartPolicy": "Never",
            "nodeName": node,
            "serviceAccountName": f"{static_set.metadata.name}-installer",
            "enableServiceLinks": False,
            "securityContext": {"runAsUser": 0, "seccompProfile": {"type": "RuntimeDefault"}},
            "containers": [
                {
                    "name": INSTALLER_CONTAINER,
                    "image": installer.image,
                    "imagePullPolicy": installer.image_pull_policy.value,
                    "command": list(CLEANUP_COMMAND),
                    "env": [
                        {"name": "STATIC_SETS_SET", "value": static_set.metadata.name or ""},
                        {"name": "STATIC_SETS_NAMESPACE", "value": namespace},
                        {"name": "STATIC_SETS_UID", "value": static_set.metadata.uid or ""},
                        {"name": "STATIC_SETS_GENERATION", "value": str(generation)},
                        {"name": "STATIC_SETS_NODE", "value": node},
                        {"name": "STATIC_SETS_PLACEMENT", "value": identity},
                        {"name": "STATIC_SETS_POD_NAME", "value": pod_name},
                        {"name": "STATIC_SETS_STATIC_POD_PATH", "value": installer.static_pod_path},
                        {"name": "STATIC_SETS_BUNDLE_DIR", "value": installer.bundle_dir},
                    ],
                    "resources": {
                        "requests": {"cpu": "10m", "memory": "32Mi"},
                        "limits": {"memory": "128Mi"},
                    },
                    "volumeMounts": [
                        {"name": STATIC_POD_MOUNT, "mountPath": installer.static_pod_path},
                        {"name": BUNDLE_MOUNT, "mountPath": installer.bundle_dir},
                    ],
                    "securityContext": {
                        "allowPrivilegeEscalation": False,
                        "privileged": False,
                        "readOnlyRootFilesystem": True,
                        "capabilities": {"drop": ["ALL"]},
                        "seccompProfile": {"type": "RuntimeDefault"},
                    },
                }
            ],
            "volumes": [
                {
                    "name": STATIC_POD_MOUNT,
                    "hostPath": {"path": installer.static_pod_path, "type": "Directory"},
                },
                {
                    "name": BUNDLE_MOUNT,
                    "hostPath": {"path": installer.bundle_dir, "type": "DirectoryOrCreate"},
                },
            ],
        },
    }
