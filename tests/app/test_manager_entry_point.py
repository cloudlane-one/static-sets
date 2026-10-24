"""The manager's kopf entry point: which placement it serves, and what it reports.

The handler is the only place that decides whether a placement is this node's
business, and it is what turns a report into status. Both are worth testing without a
cluster, because getting either wrong is silent: a manager that reports on another
node's placement races the node that owns it, and one that forgets a field leaves the
controller waiting forever.
"""

from __future__ import annotations

from typing import Any, cast

import kopf
import pytest

import static_pod_manager as entry
from static_sets.errors import ApiUnavailableError
from static_sets.manager import PlacementReport
from static_sets.models import PlacementPhase
from static_sets.naming import PLACEMENT_FINALIZER
from static_sets.placements import StaticPodPlacement

NODE = "worker-1"
UID = "0f9a4b6c-1111-2222-3333-444455556666"


class _Patch:
    """A kopf-shaped patch that records what the handler wrote."""

    def __init__(self) -> None:
        """Build an empty patch."""
        self.status: dict[str, Any] = {}
        self.metadata: dict[str, Any] = {}


def make_patch() -> kopf.Patch:
    """Return a patch the handlers can write to.

    Returns:
        The patch, typed as the one kopf passes, which is what the handler annotates.
    """
    return cast(kopf.Patch, _Patch())


def placement(node: str = NODE, *, desired: bool = True) -> StaticPodPlacement:
    """Build a placement record the entry point can be handed.

    Args:
        node: The node it names.
        desired: Whether it is wanted.
    """
    spec: dict[str, Any] = {
        "staticSet": "web",
        "staticSetUid": UID,
        "node": node,
        "generation": 3,
    }
    if not desired:
        spec |= {"desired": False, "reason": "ScaledDown"}
    return StaticPodPlacement.model_validate(
        {
            "apiVersion": "staticsets.cloudlane.dev/v1",
            "kind": "StaticPodPlacement",
            "metadata": {"name": "web-e6383089b2", "namespace": "edge", "finalizers": []},
            "spec": spec,
        }
    )


def logger() -> Any:
    """Return a logger that swallows everything.

    Returns:
        A stub logger.
    """

    class Logger:
        """A no-op logger."""

        def info(self, *args: Any, **kwargs: Any) -> None:
            """Ignore an informational message."""

    return Logger()


@pytest.fixture()
def config(monkeypatch: pytest.MonkeyPatch) -> entry.ManagerConfig:
    """Point the entry point at a node.

    Args:
        monkeypatch: Pytest's monkeypatch fixture.

    Returns:
        The configuration the entry point will use.
    """
    config = entry.ManagerConfig(node=NODE, instance="manager-abc")
    monkeypatch.setattr(entry, "CONFIG", config)
    monkeypatch.setattr(entry, "get_client", lambda: object())
    monkeypatch.setattr(entry, "ClusterApi", lambda _client: object())
    return config


def never(*args: Any, **kwargs: Any) -> PlacementReport:
    """Fail if a pass reaches the workload.

    Args:
        *args: Ignored.
        **kwargs: Ignored.

    Returns:
        Nothing; it always raises.
    """
    raise AssertionError("the pass must not reach the workload")


def test_a_placement_of_another_node_is_left_to_its_own_manager(
    config: entry.ManagerConfig,
) -> None:
    """Every node's manager sees every placement; only one of them may report on it."""
    patch = make_patch()

    entry.reconcile_placement(placement("worker-2"), patch, logger())

    assert patch.status == {}
    assert patch.metadata == {}


def test_a_desired_placement_is_reconciled_and_reported(
    config: entry.ManagerConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The report becomes status, and the finalizer is held while the record lives."""
    monkeypatch.setattr(
        entry,
        "reconcile",
        lambda *args, **kwargs: PlacementReport(
            revision="r-1",
            static_pod_name="web-abc-r-1",
            phase=PlacementPhase.INSTALLED,
            acknowledged=True,
            message="installed r-1",
            warnings=("a warning",),
        ),
    )
    patch = make_patch()

    entry.reconcile_placement(placement(), patch, logger())

    assert patch.status["phase"] == "Installed"
    assert patch.status["revision"] == "r-1"
    assert patch.status["staticPodName"] == "web-abc-r-1"
    assert patch.status["acknowledged"] is True
    assert patch.status["warnings"] == ["a warning"]
    assert patch.status["message"] == "installed r-1"
    assert patch.status["observedGeneration"] == 3
    assert patch.metadata["finalizers"] == [PLACEMENT_FINALIZER]


def test_an_existing_finalizer_is_not_added_twice(
    config: entry.ManagerConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pass over a record this manager already holds must not churn metadata."""
    monkeypatch.setattr(
        entry,
        "reconcile",
        lambda *args, **kwargs: PlacementReport(phase=PlacementPhase.INSTALLED, acknowledged=True),
    )
    resource = placement()
    resource.metadata.finalizers = [PLACEMENT_FINALIZER]
    patch = make_patch()

    entry.reconcile_placement(resource, patch, logger())

    assert "finalizers" not in patch.metadata


def test_an_undesired_placement_is_cleaned_up(
    config: entry.ManagerConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``desired: false`` is the cleanup intent, and its reason is passed through."""
    seen: dict[str, Any] = {}

    def fake_cleanup(*args: Any, **kwargs: Any) -> PlacementReport:
        seen.update(kwargs)
        return PlacementReport(phase=PlacementPhase.CLEANED, acknowledged=True)

    monkeypatch.setattr(entry, "cleanup", fake_cleanup)
    patch = make_patch()

    entry.reconcile_placement(placement(desired=False), patch, logger())

    assert seen["reason"] == "ScaledDown"
    assert seen["remove_configuration"] is True
    assert patch.status["phase"] == "Cleaned"


def test_a_terminating_placement_is_not_reported_on(
    config: entry.ManagerConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The delete handler owns that transition; writing status would race it."""
    monkeypatch.setattr(entry, "reconcile", never)
    resource = placement()
    resource.metadata.deletion_timestamp = "2026-10-24T10:00:00+00:00"
    patch = make_patch()

    entry.reconcile_placement(resource, patch, logger())

    assert patch.status == {}


def test_an_unreachable_api_makes_the_pass_retry(
    config: entry.ManagerConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed read is not a verdict about the workload."""

    def offline(*args: Any, **kwargs: Any) -> PlacementReport:
        raise ApiUnavailableError("connection refused")

    monkeypatch.setattr(entry, "reconcile", offline)

    with pytest.raises(kopf.TemporaryError) as error:
        entry.reconcile_placement(placement(), make_patch(), logger())

    assert "unreachable" in str(error.value)


def test_deleting_a_placement_removes_the_static_pod_then_releases_the_finalizer(
    config: entry.ManagerConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The object may not vanish before its manifest does."""
    seen: dict[str, Any] = {}

    def fake_cleanup(*args: Any, **kwargs: Any) -> PlacementReport:
        seen.update(kwargs)
        return PlacementReport(phase=PlacementPhase.CLEANED, acknowledged=True)

    monkeypatch.setattr(entry, "cleanup", fake_cleanup)
    resource = placement()
    resource.metadata.finalizers = [PLACEMENT_FINALIZER]
    patch = make_patch()

    entry.finalize_placement(resource, patch, logger())

    assert seen["reason"] == "ResourceDeleted"
    assert patch.metadata["finalizers"] == []


def test_deleting_a_placement_waits_while_the_workload_still_runs(
    config: entry.ManagerConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A removal that is not confirmed keeps the finalizer, so the object stays."""
    monkeypatch.setattr(
        entry,
        "cleanup",
        lambda *args, **kwargs: PlacementReport(
            phase=PlacementPhase.CLEANING, acknowledged=False, message="still running"
        ),
    )
    resource = placement()
    resource.metadata.finalizers = [PLACEMENT_FINALIZER]
    patch = make_patch()

    with pytest.raises(kopf.TemporaryError):
        entry.finalize_placement(resource, patch, logger())

    assert "finalizers" not in patch.metadata


def test_deleting_a_placement_of_another_node_does_nothing(
    config: entry.ManagerConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Deleting somebody else's record is their manager's job, not this one's."""
    monkeypatch.setattr(entry, "cleanup", never)
    patch = make_patch()

    entry.finalize_placement(placement("worker-2"), patch, logger())

    assert patch.metadata == {}


def test_deleting_a_placement_retries_when_the_api_is_unreachable(
    config: entry.ManagerConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without the API the manager cannot know that the workload is gone."""

    def offline(*args: Any, **kwargs: Any) -> PlacementReport:
        raise ApiUnavailableError("connection refused")

    monkeypatch.setattr(entry, "cleanup", offline)

    with pytest.raises(kopf.TemporaryError):
        entry.finalize_placement(placement(), make_patch(), logger())


def test_the_entry_point_reports_the_placement_identity(
    config: entry.ManagerConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Status echoes the identity, so the object can be read on its own."""
    monkeypatch.setattr(
        entry,
        "reconcile",
        lambda *args, **kwargs: PlacementReport(phase=PlacementPhase.INSTALLED, acknowledged=True),
    )
    patch = make_patch()

    entry.reconcile_placement(placement(), patch, logger())

    assert patch.status["identity"] == config.assignment(placement()).identity
    assert isinstance(patch.status["updatedAt"], str)
