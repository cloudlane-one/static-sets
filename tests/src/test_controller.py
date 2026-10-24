"""The reconcile plan: when a probe is started, concluded and retired."""

from __future__ import annotations

from typing import Any

from static_sets.controller import (
    ObservedPlacement,
    ObservedState,
    PlacementChange,
    ReconcilePlan,
    plan,
)
from static_sets.models import CleanupReason, PlacementPhase
from static_sets.naming import placement_identity
from static_sets.probe import NodeInfo, ObservedProbePod, ObservedProbeWorkload, ObservedWorkload

UID = "0f9a4b6c-1111-2222-3333-444455556666"
GENERATION = 3


def identity(node: str) -> str:
    """Return the placement identity of a node, as the operator derives it.

    Args:
        node: The node name.

    Returns:
        The identity.
    """
    return placement_identity(UID, node)


def placement(
    node: str,
    *,
    desired: bool = True,
    phase: PlacementPhase = PlacementPhase.INSTALLED,
    acknowledged: bool = True,
    generation: int = GENERATION,
    revision: str | None = "r-11111111111111111111111111111111",
    reason: CleanupReason | None = None,
) -> ObservedPlacement:
    """Build one observed placement record.

    Args:
        node: The node it is on.
        desired: Whether it is still wanted.
        phase: Where it stands.
        acknowledged: Whether the manager confirmed it.
        generation: The generation it was placed for.
        revision: The revision the manager reports.
        reason: Why it is being removed, when it is not wanted.

    Returns:
        The observed placement.
    """
    return ObservedPlacement(
        name=f"web-{identity(node)}",
        identity=identity(node),
        node=node,
        desired=desired,
        reason=reason,
        generation=generation,
        revision=revision,
        phase=phase,
        acknowledged=acknowledged,
        static_pod_name=f"web-{identity(node)}-{revision}",
    )


def probe(
    *,
    generation: int = GENERATION,
    kind: str = "ReplicaSet",
    nodes: tuple[str, ...] = ("a", "b"),
    replicas: int | None = None,
    unscheduled: int = 0,
    status_seen: bool = True,
) -> ObservedProbeWorkload:
    """Build one observed probe workload with a Pod per node.

    Args:
        generation: The generation it was created for.
        kind: ``ReplicaSet`` or ``DaemonSet``.
        nodes: The nodes its Pods are bound to.
        replicas: The Pod count it intends; defaults to the number of nodes.
        unscheduled: How many Pods are still unbound.
        status_seen: Whether its status has been observed.

    Returns:
        The observed probe.
    """
    pods = [ObservedProbePod(f"web-probe-{generation}-{node}", node) for node in nodes]
    pods.extend(
        ObservedProbePod(f"web-probe-{generation}-pending-{index}") for index in range(unscheduled)
    )
    return ObservedProbeWorkload(
        kind="DaemonSet" if kind == "DaemonSet" else "ReplicaSet",
        name=f"web-probe-{generation}",
        generation=generation,
        replicas=len(nodes) + unscheduled if replicas is None else replicas,
        status_seen=status_seen,
        pods=tuple(pods),
    )


def mirror(node: str, *, ready: bool = True) -> ObservedWorkload:
    """Build the mirror Pod kubelet publishes for a placement's static Pod.

    Args:
        node: The node it runs on.
        ready: Whether it reports Ready.

    Returns:
        The observed workload.
    """
    return ObservedWorkload(
        name=f"web-{identity(node)}-r-1",
        node=node,
        placement=identity(node),
        revision="r-11111111111111111111111111111111",
        phase="Running",
        ready=ready,
    )


def nodes(*additions: NodeInfo) -> tuple[NodeInfo, ...]:
    """Build the cluster's nodes, ready unless a caller says otherwise.

    Args:
        *additions: Nodes to add; ``a`` and ``b`` are always present.

    Returns:
        The nodes.
    """
    base = [NodeInfo("a", {}, ready=True), NodeInfo("b", {}, ready=True)]
    return (*base, *additions)


def run(
    make_static_set: Any,
    *,
    state: ObservedState | None = None,
    **overrides: Any,
) -> ReconcilePlan:
    """Compute a plan for the base StaticSet.

    Args:
        make_static_set: The fixture factory.
        state: The observed state, defaulting to an empty, synchronized one.
        **overrides: StaticSet body overrides.

    Returns:
        The plan.
    """
    resource = make_static_set(**overrides)
    return plan(resource, state or ObservedState(nodes=nodes()), namespace="edge")


def applied(plan_result: ReconcilePlan) -> list[str]:
    """Return the nodes a plan writes a placement for.

    Args:
        plan_result: The plan.

    Returns:
        The nodes of the applied placements.
    """
    return sorted(
        change.spec.node
        for change in plan_result.placements
        if change.kind == "apply" and change.spec is not None and change.spec.desired
    )


def test_an_unsynchronized_view_is_never_read_as_an_empty_one(make_static_set: Any) -> None:
    """Nothing is created or deleted before the operator has a complete view."""
    result = run(make_static_set, state=ObservedState(synced=False))

    assert result.probe is None
    assert result.delete_probes == ()
    assert result.placements == ()
    assert result.requeue is True
    assert result.status.conditions[0].type == "Synchronized"
    assert result.status.conditions[0].status == "False"


def test_a_new_generation_starts_a_scheduling_pass(make_static_set: Any) -> None:
    """The first reconcile asks the scheduler, and creates no placement yet."""
    result = run(make_static_set)

    assert result.probe is not None
    assert result.probe[0] == "ReplicaSet"
    assert result.probe[1]["spec"]["replicas"] == 2
    assert result.placements == ()
    assert result.requeue is True
    assert result.status.placed_generation == 0
    assert any(item.type == "Scheduling" for item in result.status.conditions)


def test_an_incomplete_probe_concludes_nothing(make_static_set: Any) -> None:
    """A pass whose Pods do not all exist yet must not be read as a decision."""
    result = run(
        make_static_set,
        state=ObservedState(
            nodes=nodes(),
            probes=(probe(nodes=("a",), replicas=2),),
        ),
    )

    assert result.placements == ()
    assert result.delete_probes == ()
    assert result.requeue is True


def test_an_unschedulable_probe_reports_and_changes_nothing(make_static_set: Any) -> None:
    """A Pod nothing admits is an answer about the cluster, not a reason to remove."""
    state = ObservedState(
        nodes=nodes(),
        probes=(probe(nodes=("a",), unscheduled=1),),
        placements=(placement("a"),),
        workloads=(mirror("a"),),
    )

    result = run(make_static_set, state=state)

    assert result.placements == ()
    assert result.delete_probes == ()
    reason = next(item.reason for item in result.status.conditions if item.type == "Scheduling")
    assert reason == "Unschedulable"


def test_a_complete_probe_writes_one_placement_per_chosen_node(make_static_set: Any) -> None:
    """The scheduler's answer becomes a placement record for each node it chose."""
    result = run(
        make_static_set,
        state=ObservedState(nodes=nodes(), probes=(probe(),)),
    )

    assert applied(result) == ["a", "b"]
    names = {change.name for change in result.placements}
    assert names == {f"web-{identity('a')}", f"web-{identity('b')}"}
    # Nothing has been installed yet, so the pass is still running.
    assert result.delete_probes == ()
    assert result.requeue is True
    assert result.status.placed_generation == 0


def test_a_placement_that_cannot_be_read_back_is_written_again(make_static_set: Any) -> None:
    """A record left over from an older generation is updated, not duplicated."""
    old = placement("a", generation=GENERATION - 1)

    result = run(
        make_static_set,
        state=ObservedState(nodes=nodes(), probes=(probe(),), placements=(old,)),
    )

    assert applied(result) == ["a", "b"]
    change = next(change for change in result.placements if change.name == old.name)
    assert change.spec is not None
    assert change.spec.generation == GENERATION


def test_a_concluded_pass_deletes_the_probe_and_records_it(make_static_set: Any) -> None:
    """The reservation is released once every static Pod is installed and mirrored."""
    state = ObservedState(
        nodes=nodes(),
        probes=(probe(),),
        placements=(placement("a"), placement("b")),
        workloads=(mirror("a"), mirror("b")),
    )

    result = run(make_static_set, state=state)

    assert result.delete_probes[0].name == "web-probe-3"
    assert result.placements == ()
    assert result.status.placed_generation == GENERATION
    assert result.requeue is False
    scheduling = next(item for item in result.status.conditions if item.type == "Scheduling")
    assert scheduling.reason == "Placed"


def test_a_pass_waits_for_the_mirror_pod_before_it_is_concluded(make_static_set: Any) -> None:
    """Installed but not yet mirrored is not proof that kubelet accepted the manifest."""
    state = ObservedState(
        nodes=nodes(),
        probes=(probe(),),
        placements=(placement("a"), placement("b")),
        workloads=(mirror("a"),),
    )

    result = run(make_static_set, state=state)

    assert result.delete_probes == ()
    assert result.status.placed_generation == 0
    assert result.requeue is True


def test_a_converged_staticset_does_nothing(make_static_set: Any) -> None:
    """Once a generation is placed and covered, the timer must not start another pass."""
    state = ObservedState(
        nodes=nodes(),
        placements=(placement("a"), placement("b")),
        workloads=(mirror("a"), mirror("b")),
    )

    result = run(make_static_set, state=state, status={"placedGeneration": GENERATION})

    assert result.probe is None
    assert result.delete_probes == ()
    assert result.placements == ()
    assert result.requeue is False


def test_a_shortfall_starts_a_new_pass(make_static_set: Any) -> None:
    """A placement that disappeared is replaced, as the replica count says."""
    state = ObservedState(
        nodes=nodes(),
        placements=(placement("a"),),
        workloads=(mirror("a"),),
    )

    result = run(make_static_set, state=state, status={"placedGeneration": GENERATION})

    assert result.probe is not None
    assert result.requeue is True


def test_a_new_node_starts_a_pass_in_daemonset_mode(make_static_set: Any) -> None:
    """In DaemonSet mode the expected node set is what detects a node that joined."""
    state = ObservedState(
        nodes=nodes(NodeInfo("c")),
        placements=(placement("a"), placement("b")),
        workloads=(mirror("a"), mirror("b")),
    )

    result = run(
        make_static_set,
        state=state,
        spec={"mode": "DaemonSet", "replicas": None},
        status={"placedGeneration": GENERATION},
    )

    assert result.probe is not None
    assert result.probe[0] == "DaemonSet"


def test_recreate_retires_the_old_probe_before_asking_again(make_static_set: Any) -> None:
    """Under Recreate the old reservation is released before the new pass is started."""
    state = ObservedState(
        nodes=nodes(),
        probes=(probe(generation=GENERATION - 1),),
        placements=(placement("a", generation=GENERATION - 1),),
        workloads=(mirror("a"),),
    )

    result = run(make_static_set, state=state)

    assert result.probe is None
    assert [reference.name for reference in result.delete_probes] == ["web-probe-2"]
    assert result.requeue is True
    reason = next(item.reason for item in result.status.conditions if item.type == "Scheduling")
    assert reason == "RetiringPreviousPlacement"


def test_surge_does_not_wait_for_the_old_probe_to_go(make_static_set: Any) -> None:
    """Under Surge the new pass starts in the same reconcile as the old probe's exit.

    Recreate defers the new probe until the old one's Pods are gone, because both
    would otherwise compete for the same capacity. Surge accepts that overlap, so the
    only difference the strategy makes at this step is whether the new probe is
    created now or on the next pass.
    """
    state = ObservedState(
        nodes=nodes(),
        probes=(probe(generation=GENERATION - 1),),
        placements=(placement("a", generation=GENERATION - 1),),
        workloads=(mirror("a"),),
    )

    result = run(
        make_static_set, state=state, spec={"update": {"strategy": "Surge", "maxSurge": 1}}
    )

    assert result.probe is not None
    assert [reference.name for reference in result.delete_probes] == ["web-probe-2"]
    assert result.requeue is True


def test_a_mode_change_replaces_the_probe_kind(make_static_set: Any) -> None:
    """A StaticSet switching to DaemonSet must not leave a ReplicaSet behind."""
    state = ObservedState(
        nodes=nodes(),
        probes=(probe(kind="ReplicaSet", generation=GENERATION),),
    )

    result = run(
        make_static_set,
        state=state,
        spec={
            "mode": "DaemonSet",
            "replicas": None,
            "update": {"strategy": "Surge", "maxSurge": 1},
        },
        status={"placedGeneration": 0},
    )

    assert result.probe is not None
    assert result.probe[0] == "DaemonSet"


def test_a_scaled_down_placement_says_so(make_static_set: Any) -> None:
    """A placement the new pass did not choose is removed, with the reason recorded."""
    state = ObservedState(
        nodes=nodes(),
        probes=(probe(nodes=("a",)),),
        placements=(placement("a"), placement("b")),
        workloads=(mirror("a"), mirror("b")),
    )

    result = run(make_static_set, state=state)

    removal = next(change for change in result.placements if change.name == f"web-{identity('b')}")
    assert removal.spec is not None
    assert removal.spec.desired is False
    assert removal.spec.reason is CleanupReason.SCALED_DOWN


def test_a_deselected_daemonset_placement_says_so(make_static_set: Any) -> None:
    """In DaemonSet mode a node the pass no longer chose was deselected."""
    state = ObservedState(
        nodes=nodes(),
        probes=(probe(kind="DaemonSet", nodes=("a",)),),
        placements=(placement("a"), placement("b")),
        workloads=(mirror("a"), mirror("b")),
    )

    result = run(
        make_static_set,
        state=state,
        spec={
            "mode": "DaemonSet",
            "replicas": None,
            "update": {"strategy": "Surge", "maxSurge": 1},
        },
    )

    removal = next(change for change in result.placements if change.name == f"web-{identity('b')}")
    assert removal.spec is not None
    assert removal.spec.reason is CleanupReason.DESELECTED


def test_a_removal_that_finished_deletes_its_placement(make_static_set: Any) -> None:
    """A placement object is removed only after the node acknowledged the removal."""
    cleaned = placement(
        "b", desired=False, phase=PlacementPhase.CLEANED, reason=CleanupReason.SCALED_DOWN
    )
    state = ObservedState(
        nodes=nodes(),
        probes=(probe(nodes=("a",)),),
        placements=(placement("a"), cleaned),
        workloads=(mirror("a"),),
    )

    result = run(make_static_set, state=state)

    deletes = [change.name for change in result.placements if change.kind == "delete"]
    assert deletes == [cleaned.name]


def test_deletion_removes_every_placement_before_releasing(make_static_set: Any) -> None:
    """The object keeps its finalizer until each node's manager says the Pod is gone."""
    resource = make_static_set(
        metadata={"deletionTimestamp": "2026-10-24T10:00:00+00:00"},
    )
    state = ObservedState(
        nodes=nodes(),
        probes=(probe(),),
        placements=(placement("a"), placement("b")),
        workloads=(mirror("a"), mirror("b")),
    )

    result = plan(resource, state, namespace="edge")

    assert result.probe is None
    assert [reference.name for reference in result.delete_probes] == ["web-probe-3"]
    assert {change.spec.reason for change in result.placements if change.spec} == {
        CleanupReason.RESOURCE_DELETED
    }
    assert result.requeue is True
    cleaned = next(item for item in result.status.conditions if item.type == "CleanedUp")
    assert cleaned.status == "False"


def test_deletion_releases_once_every_placement_is_cleaned(make_static_set: Any) -> None:
    """Nothing is left to wait for once every record is cleaned and deleted."""
    resource = make_static_set(metadata={"deletionTimestamp": "2026-10-24T10:00:00+00:00"})
    state = ObservedState(
        nodes=nodes(),
        placements=(
            placement("a", desired=False, phase=PlacementPhase.CLEANED),
            placement("b", desired=False, phase=PlacementPhase.CLEANED),
        ),
    )

    result = plan(resource, state, namespace="edge")

    assert result.requeue is False
    assert {change.kind for change in result.placements} == {"delete"}
    cleaned = next(item for item in result.status.conditions if item.type == "CleanedUp")
    assert cleaned.status == "True"


def test_an_unreachable_node_blocks_a_cleanup_and_says_so(make_static_set: Any) -> None:
    """The operator never claims a host was cleaned up that it cannot reach."""
    state = ObservedState(
        nodes=(NodeInfo("a", ready=True), NodeInfo("b", ready=False)),
        probes=(probe(nodes=("a",)),),
        placements=(placement("a"), placement("b", desired=False, phase=PlacementPhase.INSTALLED)),
        workloads=(mirror("a"),),
    )

    result = run(make_static_set, state=state)

    record = next(record for record in result.status.cleanup if record.node == "b")
    assert record.state.value == "Blocked"
    assert "not Ready" in (record.message or "")


def test_the_abandonment_policy_is_reported_but_never_claimed_as_done(
    make_static_set: Any,
) -> None:
    """A force policy changes what is said, never what is known."""
    state = ObservedState(
        nodes=(NodeInfo("a"), NodeInfo("b", ready=False)),
        placements=(placement("a"), placement("b", desired=False, phase=PlacementPhase.INSTALLED)),
        workloads=(mirror("a"),),
    )

    result = run(
        make_static_set,
        state=state,
        spec={"cleanup": {"onNodeUnreachable": "AbandonAfterGrace", "abandonmentGraceSeconds": 60}},
    )

    record = next(record for record in result.status.cleanup if record.node == "b")
    assert record.acknowledged is False
    assert "AbandonAfterGrace" in (record.message or "")
    assert "60s" in (record.message or "")


def test_health_is_reported_separately_from_installed_state(make_static_set: Any) -> None:
    """A placement can be installed and unhealthy, or installed and unobservable."""
    state = ObservedState(
        nodes=nodes(),
        placements=(placement("a"), placement("b")),
        workloads=(mirror("a", ready=False),),
    )

    result = run(make_static_set, state=state, status={"placedGeneration": GENERATION})

    health = {item.node: item for item in result.status.health}
    assert health["a"].observed is True
    assert health["a"].ready is False
    assert health["b"].observed is False
    assert {row.phase for row in result.status.placements} == {PlacementPhase.INSTALLED}


def test_readiness_needs_every_placement_mirrored_and_acknowledged(make_static_set: Any) -> None:
    """Readiness is the state of the whole set, not of one placement."""
    ready_state = ObservedState(
        nodes=nodes(),
        placements=(placement("a"), placement("b")),
        workloads=(mirror("a"), mirror("b")),
    )
    unmirrored = ObservedState(
        nodes=nodes(),
        placements=(placement("a"), placement("b")),
        workloads=(mirror("a"),),
    )

    ready = run(make_static_set, state=ready_state, status={"placedGeneration": GENERATION})
    not_ready = run(make_static_set, state=unmirrored, status={"placedGeneration": GENERATION})

    assert next(item for item in ready.status.conditions if item.type == "Ready").status == "True"
    assert (
        next(item for item in not_ready.status.conditions if item.type == "Ready").status == "False"
    )


def test_a_rollout_reports_no_single_desired_revision(make_static_set: Any) -> None:
    """Two revisions are two content hashes: there is no honest "newest" to report."""
    state = ObservedState(
        nodes=nodes(),
        placements=(
            placement("a", revision="r-11111111111111111111111111111111"),
            placement("b", revision="r-22222222222222222222222222222222"),
        ),
        workloads=(mirror("a"), mirror("b")),
    )

    result = run(make_static_set, state=state, status={"placedGeneration": GENERATION})

    assert result.status.desired_revision is None


def test_a_failed_placement_degrades_the_set(make_static_set: Any) -> None:
    """A refusal is surfaced instead of being hidden behind "not ready yet"."""
    state = ObservedState(
        nodes=nodes(),
        placements=(placement("a", phase=PlacementPhase.FAILED, acknowledged=False),),
    )

    result = run(make_static_set, state=state)

    degraded = [item for item in result.status.conditions if item.type == "Degraded"]
    assert degraded and degraded[0].status == "True"


def test_a_placement_write_carries_the_cleanup_policy(make_static_set: Any) -> None:
    """``removeConfigurationAfterAck`` is copied, because cleanup may outlive the set."""
    result = run(
        make_static_set,
        state=ObservedState(nodes=nodes(), probes=(probe(),)),
        spec={"cleanup": {"removeConfigurationAfterAck": False}},
    )

    change: PlacementChange = result.placements[0]
    assert change.spec is not None
    assert change.spec.remove_configuration is False
