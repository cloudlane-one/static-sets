"""The reconcile planner: desired state, cleanup intents and status."""

from __future__ import annotations

from typing import Any

import pytest

from static_sets.controller import (
    ObservedState,
    ObservedWorkload,
    cleanup_worker_pod,
    plan,
)
from static_sets.models import CleanupState, PlacementPhase
from static_sets.naming import placement_identity
from static_sets.placement import (
    INSTALLER_SERVICE_ACCOUNT_ENV,
    InstallerPod,
    NodeInfo,
    ObservedPlacement,
)

NOW = "2026-10-12T10:00:00Z"
UID = "0f9a4b6c-1111-2222-3333-444455556666"


def node(name: str, *, ready: bool = True) -> NodeInfo:
    """Return a node.

    Args:
        name: The node name.
        ready: Whether it reports Ready.

    Returns:
        The node.
    """
    return NodeInfo(name=name, labels={"role": "edge"}, ready=ready)


def installer(node_name: str, *, name: str = "pod", deleting: bool = False) -> InstallerPod:
    """Return an installer Pod bound to a node.

    Args:
        node_name: The node it runs on.
        name: Its name.
        deleting: Whether it carries a deletion timestamp.

    Returns:
        The installer Pod.
    """
    return InstallerPod(name=name, uid=f"uid-{name}", node=node_name, deleting=deleting)


def ack(
    node_name: str, *, state: str = "Installed", revision: str = "r-" + "a" * 32
) -> ObservedPlacement:
    """Return a placement acknowledgment for a node.

    Args:
        node_name: The node the placement is on.
        state: The state the installer reported.
        revision: The revision it reported.

    Returns:
        The observed placement.
    """
    return ObservedPlacement(
        identity=placement_identity(UID, node_name),
        node=node_name,
        revision=revision,
        state=state,
        acknowledged=state == "Installed",
    )


def conditions(status: Any) -> dict[str, str]:
    """Index a status' conditions by type.

    Args:
        status: The status.

    Returns:
        Condition type to status mapping.
    """
    return {condition.type: condition.status for condition in status.conditions}


def test_an_unsynchronized_view_is_never_read_as_an_empty_one(make_static_set: Any) -> None:
    """Before synchronization the plan does nothing and asks to be re-queued."""
    resource = make_static_set()

    result = plan(
        resource,
        ObservedState(placements=(ack("node-a"),), synced=False, observed_at=NOW),
        namespace="edge",
    )

    assert result.workload is None
    assert result.delete_workload is False
    assert result.cleanup == ()
    assert result.requeue is True
    assert conditions(result.status)["Synchronized"] == "False"
    assert conditions(result.status)["Ready"] == "Unknown"
    assert len(result.status.placements) == 1


def test_a_running_staticset_applies_the_installer_workload(make_static_set: Any) -> None:
    """A healthy reconcile keeps the installer workload in place."""
    resource = make_static_set(spec={"replicas": 2})
    observed = ObservedState(
        nodes=(node("node-a"), node("node-b")),
        installer_pods=(installer("node-a"), installer("node-b", name="pod-b")),
        observed_at=NOW,
    )

    result = plan(resource, observed, namespace="edge")

    assert result.workload is not None
    kind, manifest = result.workload
    assert kind == "Deployment"
    assert manifest["spec"]["replicas"] == 2
    assert result.delete_workload is False
    assert result.cleanup == ()
    assert [item.node for item in result.status.placements] == ["node-a", "node-b"]
    assert all(item.phase is PlacementPhase.PENDING for item in result.status.placements)
    assert conditions(result.status)["Ready"] == "False"


def test_installations_are_acknowledged_per_placement(make_static_set: Any) -> None:
    """An acknowledged placement is installed and contributes the desired revision."""
    resource = make_static_set(spec={"replicas": 1})
    observed = ObservedState(
        nodes=(node("node-a"),),
        installer_pods=(installer("node-a"),),
        placements=(ack("node-a"),),
        observed_at=NOW,
    )

    result = plan(resource, observed, namespace="edge")

    placement = result.status.placements[0]
    assert placement.phase is PlacementPhase.INSTALLED
    assert placement.acknowledged is True
    assert result.status.desired_revision == "r-" + "a" * 32
    assert conditions(result.status)["Ready"] == "True"


def test_health_is_reported_separately_from_installed_state(make_static_set: Any) -> None:
    """Observed health never feeds the readiness decision."""
    resource = make_static_set(spec={"replicas": 1})
    identity = placement_identity(UID, "node-a")
    observed = ObservedState(
        nodes=(node("node-a"),),
        installer_pods=(installer("node-a"),),
        placements=(ack("node-a"),),
        workloads=(
            ObservedWorkload(
                name=f"web-{identity}", node="node-a", phase="Running", ready=False, restart_count=4
            ),
        ),
        observed_at=NOW,
    )

    result = plan(resource, observed, namespace="edge")

    health = result.status.health[0]
    assert health.observed is True
    assert health.ready is False
    assert health.restart_count == 4
    assert result.status.placements[0].phase is PlacementPhase.INSTALLED
    assert conditions(result.status)["Ready"] == "True"


def test_an_unobserved_workload_is_reported_as_such(make_static_set: Any) -> None:
    """A placement with no mirror Pod yet is not a failed placement."""
    resource = make_static_set(spec={"replicas": 1})
    observed = ObservedState(
        nodes=(node("node-a"),),
        installer_pods=(installer("node-a"),),
        placements=(ack("node-a"),),
        observed_at=NOW,
    )

    result = plan(resource, observed, namespace="edge")

    assert result.status.health[0].observed is False
    assert "mirror Pod" in (result.status.health[0].message or "")


def test_an_orphaned_placement_gets_an_intent_and_a_cleanup_worker(make_static_set: Any) -> None:
    """A placement no installer serves is cleaned up by a pinned worker."""
    resource = make_static_set(spec={"replicas": 1})
    observed = ObservedState(
        nodes=(node("node-a"),),
        placements=(ack("node-a"),),
        observed_at=NOW,
    )

    result = plan(resource, observed, namespace="edge")

    assert len(result.cleanup) == 1
    action = result.cleanup[0]
    assert action.record.state is CleanupState.QUEUED
    assert action.record.acknowledged is False
    assert action.worker is not None
    assert action.payload["cleanup"] == "true"
    assert action.payload["generation"] == "3"
    assert result.requeue is True
    assert conditions(result.status)["CleanupPending"] == "True"


def test_an_unreachable_node_retains_cleanup_and_says_so(make_static_set: Any) -> None:
    """A not-Ready node is not chased with a worker that could not run there."""
    resource = make_static_set(spec={"replicas": 1})
    observed = ObservedState(
        nodes=(node("node-a", ready=False),),
        placements=(ack("node-a"),),
        observed_at=NOW,
    )

    result = plan(resource, observed, namespace="edge")

    action = result.cleanup[0]
    assert action.record.state is CleanupState.BLOCKED
    assert action.worker is None
    assert "not Ready" in (action.record.message or "")
    assert "Retain" in (action.record.message or "")


def test_the_abandonment_policy_is_reported_but_never_claimed_as_done(make_static_set: Any) -> None:
    """The force policy is explained in status without claiming remote success."""
    resource = make_static_set(
        spec={
            "replicas": 1,
            "cleanup": {"onNodeUnreachable": "AbandonAfterGrace", "abandonmentGraceSeconds": 60},
        }
    )
    observed = ObservedState(
        nodes=(node("node-a", ready=False),),
        placements=(ack("node-a"),),
        observed_at=NOW,
    )

    result = plan(resource, observed, namespace="edge")

    action = result.cleanup[0]
    assert action.record.acknowledged is False
    assert action.record.state is CleanupState.BLOCKED
    assert "AbandonAfterGrace" in (action.record.message or "")
    assert "60s" in (action.record.message or "")


def test_a_scaled_down_placement_is_attributed_to_the_scale_down(make_static_set: Any) -> None:
    """Scaling in explains why a placement is being removed."""
    resource = make_static_set(spec={"replicas": 2})
    observed = ObservedState(
        nodes=(node("node-a"), node("node-b")),
        installer_pods=(installer("node-a"),),
        placements=(ack("node-b"),),
        observed_at=NOW,
    )

    result = plan(resource, observed, namespace="edge")

    assert result.cleanup[0].record.reason.value == "ScaledDown"


def test_a_deselected_node_is_attributed_to_deselection(make_static_set: Any) -> None:
    """A DaemonSet whose node selector changed attributes removal to deselection."""
    resource = make_static_set(
        spec={
            "mode": "DaemonSet",
            "replicas": None,
            "placement": {"nodeSelector": {"role": "core"}},
        }
    )
    observed = ObservedState(
        nodes=(NodeInfo(name="node-a", labels={"role": "edge"}),),
        placements=(ack("node-a"),),
        observed_at=NOW,
    )

    result = plan(resource, observed, namespace="edge")

    assert result.cleanup[0].record.reason.value == "Deselected"


def test_deletion_cleans_up_every_placement_before_releasing(make_static_set: Any) -> None:
    """A deleted StaticSet removes its workload and waits for acknowledgments."""
    resource = make_static_set(
        spec={"replicas": 1}, metadata={"deletionTimestamp": "2026-10-12T11:00:00Z"}
    )
    observed = ObservedState(
        nodes=(node("node-a"),),
        installer_pods=(installer("node-a"),),
        placements=(ack("node-a"),),
        observed_at=NOW,
    )

    result = plan(resource, observed, namespace="edge")

    assert result.delete_workload is True
    assert result.workload is None
    assert len(result.cleanup) == 1
    assert result.cleanup[0].record.reason.value == "ResourceDeleted"
    assert result.cleanup[0].worker is None
    assert result.requeue is True
    assert conditions(result.status)["Deleting"] == "True"
    assert conditions(result.status)["CleanedUp"] == "False"


def test_deletion_acknowledged_releases_the_finalizer(make_static_set: Any) -> None:
    """Once every placement acknowledges, the plan stops asking to be re-queued."""
    resource = make_static_set(
        spec={"replicas": 1}, metadata={"deletionTimestamp": "2026-10-12T11:00:00Z"}
    )
    observed = ObservedState(
        nodes=(node("node-a"),),
        placements=(ack("node-a", state="Removed", revision=""),),
        observed_at=NOW,
    )

    result = plan(resource, observed, namespace="edge")

    assert result.requeue is False
    assert conditions(result.status)["CleanedUp"] == "True"
    assert result.status.cleanup[0].acknowledged is True


def test_a_terminating_installer_makes_its_placement_a_cleanup_candidate(
    make_static_set: Any,
) -> None:
    """An installer that is going away no longer serves its node."""
    resource = make_static_set(spec={"replicas": 1})
    observed = ObservedState(
        nodes=(node("node-a"),),
        installer_pods=(installer("node-a", deleting=True),),
        placements=(ack("node-a"),),
        observed_at=NOW,
    )

    result = plan(resource, observed, namespace="edge")

    assert len(result.cleanup) == 1
    assert result.status.placements == []


def test_a_removed_placement_is_not_cleaned_up_twice(make_static_set: Any) -> None:
    """An already-removed placement is left alone."""
    resource = make_static_set(spec={"replicas": 1})
    observed = ObservedState(
        nodes=(node("node-a"),),
        placements=(ack("node-a", state="Removed", revision=""),),
        observed_at=NOW,
    )

    result = plan(resource, observed, namespace="edge")

    assert result.cleanup == ()
    assert result.requeue is False


def test_cleanup_worker_is_pinned_and_narrow(make_static_set: Any) -> None:
    """The cleanup worker mounts the two directories and nothing else."""
    resource = make_static_set()

    worker = cleanup_worker_pod(
        resource,
        namespace="edge",
        identity="id1",
        node="node-a",
        generation=3,
        reason="ScaledDown",
        pod_name="web-id1",
    )

    assert worker["spec"]["nodeName"] == "node-a"
    assert worker["spec"]["restartPolicy"] == "Never"
    assert worker["metadata"]["finalizers"] == ["staticsets.cloudlane.dev/cleanup"]
    assert worker["metadata"]["annotations"]["staticsets.cloudlane.dev/cleanup-reason"] == (
        "ScaledDown"
    )
    assert worker["spec"]["volumes"][1]["hostPath"]["path"] == "/var/lib/static-sets"
    container = worker["spec"]["containers"][0]
    assert container["command"] == ["static-set-installer", "cleanup"]
    assert container["securityContext"]["privileged"] is False
    environment = {item["name"]: item["value"] for item in container["env"]}
    assert environment["STATIC_SETS_PLACEMENT"] == "id1"
    assert environment["STATIC_SETS_POD_NAME"] == "web-id1"


def test_installers_running_condition_reflects_the_live_count(make_static_set: Any) -> None:
    """The installer count is visible without reading the Deployment."""
    resource = make_static_set(spec={"replicas": 2})
    observed = ObservedState(
        nodes=(node("node-a"), node("node-b")),
        installer_pods=(installer("node-a"),),
        observed_at=NOW,
    )

    result = plan(resource, observed, namespace="edge")

    condition = next(item for item in result.status.conditions if item.type == "InstallersRunning")
    assert condition.status == "True"
    assert "1 installer" in condition.message


def test_daemonset_readiness_counts_eligible_nodes(make_static_set: Any) -> None:
    """DaemonSet readiness is measured against the nodes the selector admits."""
    resource = make_static_set(
        spec={
            "mode": "DaemonSet",
            "replicas": None,
            "placement": {"nodeSelector": {"role": "edge"}},
        }
    )
    observed = ObservedState(
        nodes=(node("node-a"), node("node-b"), NodeInfo(name="node-c", labels={"role": "core"})),
        installer_pods=(installer("node-a"),),
        placements=(ack("node-a"),),
        observed_at=NOW,
    )

    result = plan(resource, observed, namespace="edge")

    assert conditions(result.status)["Ready"] == "False"
    ready_condition = next(item for item in result.status.conditions if item.type == "Ready")
    assert "1 of 2" in ready_condition.message


@pytest.mark.parametrize("mode", ["Deployment", "DaemonSet"])
def test_both_modes_apply_a_workload(make_static_set: Any, mode: str) -> None:
    """Both supported modes produce an installer workload."""
    resource = make_static_set(spec={"mode": mode, "replicas": 1 if mode == "Deployment" else None})

    result = plan(resource, ObservedState(observed_at=NOW), namespace="edge")

    assert result.workload is not None
    assert result.workload[0] == mode


def test_a_cleanup_worker_runs_as_the_same_account_as_the_installer(
    make_static_set: Any, monkeypatch: Any
) -> None:
    """The worker writes the same directories, so it needs the same permissions.

    Both names come from the environment the chart sets, which is what keeps them
    from drifting apart — a worker naming an account that does not exist would never
    start, and the placement would never be cleaned up.
    """
    monkeypatch.setenv(INSTALLER_SERVICE_ACCOUNT_ENV, "custom-installer")
    resource = make_static_set(spec={"replicas": 1})

    worker = cleanup_worker_pod(
        resource,
        namespace="edge",
        identity="abcdef0123",
        node="worker-1",
        generation=3,
        reason="ScaleDown",
        pod_name="web-abcdef0123",
    )

    assert worker["spec"]["serviceAccountName"] == "custom-installer"
