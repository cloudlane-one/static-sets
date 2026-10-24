"""Turn a StaticSet and the observed cluster into a reconcile plan.

The controller is split in two on purpose. Everything that decides *what should
happen* lives here, as a pure function of the StaticSet and a snapshot of the
cluster, so it can be tested exhaustively without a cluster. The thin kopf adapter in
``app/`` only reads the snapshot and applies the plan.

Three rules shape the whole module:

* **an incomplete view is never an empty view.** If watch synchronization has not
  finished, the probe has not been fully created, or the operator could not read the
  placements, the plan performs **no** destructive action and asks to be re-queued.
  Placement is only *concluded* from a probe whose Pods all exist;
* **placement is asked for, not assumed.** Where a static Pod goes is decided by the
  Kubernetes scheduler, once per generation, through a probe workload. The plan never
  guesses a node from a selector: it reports what the scheduler chose;
* **cleanup is a protocol, not a side effect.** A placement that is no longer wanted
  is marked ``desired: false`` and left in place until the node's manager reports that
  the static Pod is gone. Nothing claims a host was cleaned up before the host says
  so.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
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
    UpdateStrategy,
    WorkloadHealth,
)
from static_sets.naming import placement_identity, placement_name
from static_sets.placements import PlacementSpec
from static_sets.probe import (
    NodeInfo,
    ObservedProbeWorkload,
    ObservedWorkload,
    eligible_nodes,
    probe_workload,
)

__all__ = [
    "ObservedPlacement",
    "ObservedState",
    "PlacementChange",
    "ProbeRef",
    "ReconcilePlan",
    "plan",
]


@dataclass(frozen=True)
class ObservedPlacement:
    """One placement record, as the node's manager last reported it.

    Attributes:
        name: The placement object's name.
        identity: The placement identity.
        node: The node the scheduler chose.
        desired: Whether the placement is still wanted.
        reason: Why it is no longer wanted, when it is not.
        generation: The generation the controller last placed it for.
        revision: The revision the manager reports as installed.
        phase: Where the placement stands.
        acknowledged: Whether the manager confirmed its last report is durable.
        static_pod_name: The static Pod backing the placement, as reported.
        message: The manager's explanation, if any.
    """

    name: str
    identity: str
    node: str
    desired: bool = True
    reason: CleanupReason | None = None
    generation: int = 0
    revision: str | None = None
    phase: PlacementPhase = PlacementPhase.PENDING
    acknowledged: bool = False
    static_pod_name: str | None = None
    message: str | None = None


@dataclass(frozen=True)
class ObservedState:
    """Everything the plan is computed from.

    Attributes:
        nodes: The nodes of the cluster.
        probes: The scheduling probes currently observed for this StaticSet.
        placements: The placement records currently observed.
        workloads: The mirror Pods kubelet created for the static workloads.
        synced: Whether watch synchronization has completed. When ``False`` the
            plan is non-destructive.
        observed_at: A timestamp used for condition transitions.
    """

    nodes: tuple[NodeInfo, ...] = ()
    probes: tuple[ObservedProbeWorkload, ...] = ()
    placements: tuple[ObservedPlacement, ...] = ()
    workloads: tuple[ObservedWorkload, ...] = ()
    synced: bool = True
    observed_at: str = ""


@dataclass(frozen=True)
class ProbeRef:
    """A scheduling probe to delete.

    Attributes:
        kind: ``ReplicaSet`` or ``DaemonSet``.
        name: The workload's name.
    """

    kind: Literal["ReplicaSet", "DaemonSet"]
    name: str


@dataclass(frozen=True)
class PlacementChange:
    """One write the adapter has to perform on a placement record.

    Attributes:
        kind: ``apply`` to create or update the spec, ``delete`` to remove the object.
        name: The placement object's name.
        namespace: The namespace it lives in.
        spec: The desired spec, for ``apply``.
    """

    kind: Literal["apply", "delete"]
    name: str
    namespace: str
    spec: PlacementSpec | None = None


@dataclass(frozen=True)
class ReconcilePlan:
    """What the adapter should do, and what it should report.

    Attributes:
        probe: The scheduling probe to apply, as ``(kind, manifest)``, or ``None``.
        delete_probes: The probe workloads to delete.
        placements: The placement records to write.
        status: The status to publish.
        requeue: Whether to reconcile again shortly.
    """

    probe: tuple[Literal["ReplicaSet", "DaemonSet"], dict[str, Any]] | None = None
    delete_probes: tuple[ProbeRef, ...] = ()
    placements: tuple[PlacementChange, ...] = ()
    status: StaticSetStatus = field(default_factory=StaticSetStatus)
    requeue: bool = False


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
    probe = probe_workload(static_set, namespace=namespace, generation=generation)
    target, stale = _split_probes(observed.probes, kind=probe[0], generation=generation)
    if target is not None:
        return _pass_plan(
            static_set,
            observed,
            target=target,
            stale=stale,
            namespace=namespace,
        )
    return _start_pass_plan(static_set, observed, stale=stale, manifest=probe, namespace=namespace)


def _split_probes(
    probes: Iterable[ObservedProbeWorkload],
    *,
    kind: Literal["ReplicaSet", "DaemonSet"],
    generation: int,
) -> tuple[ObservedProbeWorkload | None, tuple[ObservedProbeWorkload, ...]]:
    """Split the observed probes into the current one and the stale ones.

    Args:
        probes: The probes observed.
        kind: The kind the current mode calls for.
        generation: The current generation.

    Returns:
        The probe for the current kind and generation (newest name winning), and the
        probes that belong to another generation or another kind.
    """
    current = [probe for probe in probes if probe.kind == kind and probe.generation == generation]
    stale = tuple(
        probe for probe in probes if not (probe.kind == kind and probe.generation == generation)
    )
    newest = sorted(current, key=lambda probe: probe.name)[-1] if current else None
    return newest, stale


def _start_pass_plan(
    static_set: StaticSet,
    observed: ObservedState,
    *,
    stale: tuple[ObservedProbeWorkload, ...],
    manifest: tuple[Literal["ReplicaSet", "DaemonSet"], dict[str, Any]],
    namespace: str,
) -> ReconcilePlan:
    """Plan the beginning of a scheduling pass, or nothing at all.

    A pass is started when the current generation has not been placed yet, when the
    placements that came out of the last pass no longer cover what the specification
    asks for, or when a probe from an older generation is still around.

    Args:
        static_set: The StaticSet being reconciled.
        observed: The snapshot of the cluster.
        stale: The probes that belong to an older generation or another mode.
        manifest: The probe workload to create when a pass is needed.
        namespace: The namespace the StaticSet lives in.

    Returns:
        The plan to apply.
    """
    del namespace
    generation = static_set.metadata.generation or 0
    placed = static_set.status.placed_generation if static_set.status else 0
    if placed >= generation and not _shortfall(static_set, observed) and not stale:
        return ReconcilePlan(status=_running_status(static_set, observed))
    if stale and static_set.spec.update.strategy is UpdateStrategy.RECREATE:
        # The old probe holds the reservation the new placement needs. Deleting it is
        # the only way the scheduler can see that capacity again, and its Pods take a
        # moment to disappear, so the new probe is created on the next reconcile.
        return ReconcilePlan(
            delete_probes=_probe_refs(stale),
            status=_status_with_condition(
                static_set,
                observed,
                "Scheduling",
                "True",
                "RetiringPreviousPlacement",
                "the previous scheduling probe is being deleted before a new pass starts",
            ),
            requeue=True,
        )
    return ReconcilePlan(
        probe=manifest,
        delete_probes=_probe_refs(stale),
        status=_status_with_condition(
            static_set,
            observed,
            "Scheduling",
            "True",
            "ProbeCreated",
            (
                "the scheduler is being asked where the workload should run; its Pods are "
                "bound but never started"
            ),
        ),
        requeue=True,
    )


def _pass_plan(
    static_set: StaticSet,
    observed: ObservedState,
    *,
    target: ObservedProbeWorkload,
    stale: tuple[ObservedProbeWorkload, ...],
    namespace: str,
) -> ReconcilePlan:
    """Plan a scheduling pass that is in flight or complete.

    Args:
        static_set: The StaticSet being reconciled.
        observed: The snapshot of the cluster.
        target: The probe for the current generation.
        stale: The probes that belong to an older generation or another mode.
        namespace: The namespace the StaticSet lives in.

    Returns:
        The plan to apply.
    """
    if not target.complete:
        return ReconcilePlan(
            delete_probes=_probe_refs(stale),
            status=_status_with_condition(
                static_set,
                observed,
                "Scheduling",
                "True",
                "Unschedulable" if target.unscheduled else "ProbePending",
                (
                    f"the scheduling probe has {len(target.pods)} of {target.replicas} Pod(s); "
                    "placement is concluded only once every Pod exists"
                ),
            ),
            requeue=True,
        )
    desired_nodes = {pod.node for pod in target.scheduled if pod.node}
    if not desired_nodes and target.replicas:
        # Every probe Pod is unbound: the workload does not fit anywhere, which is a
        # real answer about the cluster rather than a reason to remove anything.
        return ReconcilePlan(
            delete_probes=_probe_refs(stale),
            status=_status_with_condition(
                static_set,
                observed,
                "Scheduling",
                "True",
                "Pending",
                "no node admitted the scheduling probe; nothing is installed yet",
            ),
            requeue=True,
        )
    changes = _placement_changes(static_set, observed, desired_nodes, namespace=namespace)
    if not _placements_complete(observed, desired_nodes):
        return ReconcilePlan(
            delete_probes=_probe_refs(stale),
            placements=changes,
            status=_status_with_condition(
                static_set,
                observed,
                "Scheduling",
                "True",
                "Installing",
                "the scheduler has placed the workload; its nodes are installing it",
            ),
            requeue=True,
        )
    # The scheduler's job is done: every static Pod is installed and mirrored, so the
    # reservation the probe holds is released and the decision lives on in the
    # placement records.
    status = _running_status(static_set, observed)
    status.placed_generation = static_set.metadata.generation or 0
    status.conditions.append(
        _condition(
            "Scheduling",
            "False",
            "Placed",
            f"the scheduler placed {len(desired_nodes)} placement(s) for this generation",
            static_set,
            observed,
        )
    )
    return ReconcilePlan(
        delete_probes=(ProbeRef(target.kind, target.name), *_probe_refs(stale)),
        placements=changes,
        status=status,
    )


def _probe_refs(probes: Iterable[ObservedProbeWorkload]) -> tuple[ProbeRef, ...]:
    """Return the references that delete a set of probes.

    Args:
        probes: The probes to delete.

    Returns:
        The references.
    """
    return tuple(ProbeRef(probe.kind, probe.name) for probe in probes)


def _shortfall(static_set: StaticSet, observed: ObservedState) -> bool:
    """Return whether the placements no longer cover the specification.

    Args:
        static_set: The StaticSet being reconciled.
        observed: The snapshot of the cluster.

    Returns:
        ``True`` when fewer placements are installed than the specification asks for.
    """
    installed = [
        placement
        for placement in observed.placements
        if placement.desired and placement.phase is PlacementPhase.INSTALLED
    ]
    return len(installed) < _desired_count(static_set, observed)


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


def _placement_changes(
    static_set: StaticSet,
    observed: ObservedState,
    desired_nodes: set[str],
    *,
    namespace: str,
) -> tuple[PlacementChange, ...]:
    """Return the placement writes a completed pass calls for.

    Args:
        static_set: The StaticSet being reconciled.
        observed: The snapshot of the cluster.
        desired_nodes: The nodes the scheduler chose.
        namespace: The namespace the StaticSet lives in.

    Returns:
        The writes, sorted by node.
    """
    generation = static_set.metadata.generation or 0
    uid = static_set.metadata.uid or ""
    name = static_set.metadata.name or ""
    remove_configuration = static_set.spec.cleanup.remove_configuration_after_ack
    existing = {placement.node: placement for placement in observed.placements if placement.desired}
    changes: list[PlacementChange] = []
    for node in sorted(desired_nodes):
        current = existing.get(node)
        if current is not None and current.generation == generation:
            continue
        changes.append(
            PlacementChange(
                kind="apply",
                name=placement_name(name, placement_identity(uid, node)),
                namespace=namespace,
                spec=PlacementSpec(
                    static_set=name,
                    static_set_uid=uid,
                    node=node,
                    generation=generation,
                    desired=True,
                    remove_configuration=remove_configuration,
                ),
            )
        )
    reason = _cleanup_reason(static_set, observed, desired_nodes)
    for placement in observed.placements:
        if placement.node in desired_nodes:
            continue
        if not placement.desired:
            if placement.phase is PlacementPhase.CLEANED:
                # The removal finished: the record has served its purpose and is closed.
                changes.append(
                    PlacementChange(kind="delete", name=placement.name, namespace=namespace)
                )
            continue
        changes.append(
            PlacementChange(
                kind="apply",
                name=placement.name,
                namespace=namespace,
                spec=PlacementSpec(
                    static_set=name,
                    static_set_uid=uid,
                    node=placement.node,
                    generation=generation,
                    desired=False,
                    reason=reason,
                    remove_configuration=remove_configuration,
                ),
            )
        )
    return tuple(changes)


def _cleanup_reason(
    static_set: StaticSet,
    observed: ObservedState,
    desired_nodes: set[str],
) -> CleanupReason:
    """Decide why a placement is being removed.

    Args:
        static_set: The StaticSet being reconciled.
        observed: The snapshot of the cluster.
        desired_nodes: The nodes the scheduler chose in this pass.

    Returns:
        The cleanup reason recorded in the placement.
    """
    if static_set.spec.mode is Mode.DEPLOYMENT:
        previous = sum(1 for placement in observed.placements if placement.desired)
        if len(desired_nodes) < previous:
            return CleanupReason.SCALED_DOWN
        return CleanupReason.RELOCATED
    return CleanupReason.DESELECTED


def _placements_complete(observed: ObservedState, desired_nodes: set[str]) -> bool:
    """Return whether every chosen node has installed and mirrored its static Pod.

    A probe reservation is only released once kubelet has published a mirror Pod for
    the workload on that node: that is the observable proof that the manifest exists
    and was accepted, which is what the reservation was standing in for.

    Args:
        observed: The snapshot of the cluster.
        desired_nodes: The nodes the scheduler chose.

    Returns:
        ``True`` when every placement for those nodes is installed and mirrored.
    """
    by_node = {placement.node: placement for placement in observed.placements}
    mirrored = {workload.placement for workload in observed.workloads if workload.placement}
    for node in desired_nodes:
        placement = by_node.get(node)
        if placement is None or not placement.acknowledged:
            return False
        if placement.phase is not PlacementPhase.INSTALLED:
            return False
        if placement.identity not in mirrored:
            return False
    return True


def _deletion_plan(
    static_set: StaticSet,
    observed: ObservedState,
    *,
    namespace: str,
) -> ReconcilePlan:
    """Compute the plan for a StaticSet that is being deleted.

    Deletion is the one case where every observed placement is removed, because the
    user's intent is unambiguous. Nothing is released until each placement's manager
    acknowledges that its static Pod is gone; the record is deleted only after that.

    Args:
        static_set: The StaticSet being deleted.
        observed: The snapshot of the cluster.
        namespace: The namespace the StaticSet lives in.

    Returns:
        The plan to apply.
    """
    generation = static_set.metadata.generation or 0
    uid = static_set.metadata.uid or ""
    name = static_set.metadata.name or ""
    changes: list[PlacementChange] = []
    for placement in observed.placements:
        if placement.phase is PlacementPhase.CLEANED:
            changes.append(PlacementChange(kind="delete", name=placement.name, namespace=namespace))
            continue
        changes.append(
            PlacementChange(
                kind="apply",
                name=placement.name,
                namespace=namespace,
                spec=PlacementSpec(
                    static_set=name,
                    static_set_uid=uid,
                    node=placement.node,
                    generation=generation,
                    desired=False,
                    reason=CleanupReason.RESOURCE_DELETED,
                    remove_configuration=static_set.spec.cleanup.remove_configuration_after_ack,
                ),
            )
        )
    acknowledged = all(
        placement.phase is PlacementPhase.CLEANED for placement in observed.placements
    )
    status = _running_status(static_set, observed)
    status.desired_revision = None
    status.conditions = [
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
    ]
    return ReconcilePlan(
        delete_probes=_probe_refs(observed.probes),
        placements=tuple(changes),
        status=status,
        requeue=not acknowledged,
    )


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
        placed_generation=static_set.status.placed_generation if static_set.status else 0,
        placements=_placement_rows(observed),
        health=_health(observed),
        cleanup=_cleanup_records(static_set, observed),
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


def _status_with_condition(
    static_set: StaticSet,
    observed: ObservedState,
    condition_type: str,
    status: Literal["True", "False", "Unknown"],
    reason: str,
    message: str,
) -> StaticSetStatus:
    """Build the status of a pass that has not concluded yet.

    Args:
        static_set: The StaticSet being reconciled.
        observed: The snapshot of the cluster.
        condition_type: The condition to set.
        status: Its status.
        reason: Its reason.
        message: Its message.

    Returns:
        The status to publish.
    """
    result = _running_status(static_set, observed)
    result.conditions.append(
        _condition(condition_type, status, reason, message, static_set, observed)
    )
    return result


def _running_status(static_set: StaticSet, observed: ObservedState) -> StaticSetStatus:
    """Build the status of a StaticSet that is not being deleted.

    Args:
        static_set: The StaticSet being reconciled.
        observed: The snapshot of the cluster.

    Returns:
        The status to publish.
    """
    rows = _placement_rows(observed)
    installed = [row for row in rows if row.phase is PlacementPhase.INSTALLED]
    revisions = {row.revision for row in installed if row.revision}
    cleanup = _cleanup_records(static_set, observed)
    pending_cleanup = [record for record in cleanup if not record.acknowledged]
    ready = _is_ready(static_set, observed, rows=rows, installed=installed)
    status = StaticSetStatus(
        observed_generation=static_set.metadata.generation or 0,
        placed_generation=static_set.status.placed_generation if static_set.status else 0,
        # Revisions are content hashes with no ordering, so a single "desired" revision
        # is only reportable while every placement agrees on it. During a rollout there
        # is no honest single answer, and reporting the newest-looking one would be a
        # guess.
        desired_revision=next(iter(revisions)) if len(revisions) == 1 else None,
        placements=rows,
        health=_health(observed),
        cleanup=cleanup,
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
                    "every desired placement is installed, acknowledged and mirrored"
                    if ready
                    else f"{len(installed)} of {_desired_count(static_set, observed)} "
                    "placement(s) are installed"
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
    if any(row.phase is PlacementPhase.FAILED for row in rows):
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


def _placement_rows(observed: ObservedState) -> list[PlacementStatus]:
    """Build the status rows for the observed placements.

    Args:
        observed: The snapshot of the cluster.

    Returns:
        One row per placement, sorted by node.
    """
    return [
        PlacementStatus(
            identity=placement.identity,
            node=placement.node,
            revision=placement.revision,
            phase=placement.phase,
            acknowledged=placement.acknowledged,
            generation=placement.generation,
            message=placement.message,
        )
        for placement in sorted(observed.placements, key=lambda item: item.node)
    ]


def _cleanup_records(
    static_set: StaticSet,
    observed: ObservedState,
) -> list[CleanupRecord]:
    """Build the cleanup records for placements that are no longer wanted.

    Args:
        static_set: The StaticSet being reconciled.
        observed: The snapshot of the cluster.

    Returns:
        One record per removal, sorted by node.
    """
    ready = {node.name for node in observed.nodes if node.ready}
    records: list[CleanupRecord] = []
    for placement in sorted(observed.placements, key=lambda item: item.node):
        if placement.desired:
            continue
        acknowledged = placement.phase is PlacementPhase.CLEANED
        if acknowledged:
            state = CleanupState.ACKNOWLEDGED
        elif placement.node not in ready:
            state = CleanupState.BLOCKED
        else:
            state = CleanupState.REQUESTED
        records.append(
            CleanupRecord(
                identity=placement.identity,
                node=placement.node,
                revision=placement.revision,
                generation=placement.generation,
                reason=placement.reason or CleanupReason.RELOCATED,
                state=state,
                acknowledged=acknowledged,
                message=_cleanup_message(static_set, placement, node_ready=placement.node in ready),
            )
        )
    return records


def _cleanup_message(
    static_set: StaticSet,
    placement: ObservedPlacement,
    *,
    node_ready: bool,
) -> str:
    """Explain the cleanup state for status.

    Args:
        static_set: The StaticSet being reconciled.
        placement: The placement being removed.
        node_ready: Whether the node holding the placement reports Ready.

    Returns:
        The explanation.
    """
    if placement.phase is PlacementPhase.CLEANED:
        return "the node's manager confirmed the static Pod is gone"
    if not node_ready:
        policy = static_set.spec.cleanup.on_node_unreachable
        grace = static_set.spec.cleanup.abandonment_grace_seconds
        followed = (
            f", after which an administrator may abandon it following a {grace}s grace period"
            if policy is NodeUnreachablePolicy.ABANDON_AFTER_GRACE
            else ""
        )
        return (
            f"node {placement.node} is not Ready, so its manager cannot remove the static Pod; "
            "cleanup stays pending and the workload may still be running there. "
            f"The declared policy is {policy.value}" + followed
        )
    return (
        f"the node's manager on {placement.node} has been asked to remove the static Pod; "
        "the placement stays until it acknowledges removal"
    )


def _health(observed: ObservedState) -> list[WorkloadHealth]:
    """Match observed mirror Pods to placements.

    Health is reported separately from installed state and never feeds a decision:
    a placement can be installed and unhealthy, or installed and unobservable.

    Args:
        observed: The snapshot of the cluster.

    Returns:
        The observed workload health, one entry per known placement.
    """
    by_placement: dict[str, ObservedWorkload] = {}
    for workload in observed.workloads:
        if not workload.placement:
            continue
        current = by_placement.get(workload.placement)
        if current is None or (workload.ready and not current.ready):
            by_placement[workload.placement] = workload
    health: list[WorkloadHealth] = []
    for placement in sorted(observed.placements, key=lambda item: item.node):
        mirror = by_placement.get(placement.identity)
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


def _is_ready(
    static_set: StaticSet,
    observed: ObservedState,
    *,
    rows: list[PlacementStatus],
    installed: list[PlacementStatus],
) -> bool:
    """Return whether every desired placement is installed.

    Args:
        static_set: The StaticSet being reconciled.
        observed: The snapshot of the cluster.
        rows: Every observed placement.
        installed: The installed subset.

    Returns:
        ``True`` when the desired number of placements is installed, acknowledged and
        mirrored, and no placement is being removed.
    """
    desired = _desired_count(static_set, observed)
    removing = [row for row in rows if row.phase is PlacementPhase.CLEANING]
    if desired == 0:
        return not rows
    if removing or len(rows) != desired or len(installed) != desired:
        return False
    mirrored = {workload.placement for workload in observed.workloads if workload.placement}
    return all(row.identity in mirrored and row.acknowledged for row in installed)


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
