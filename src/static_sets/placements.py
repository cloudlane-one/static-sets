"""The per-placement record: ``StaticPodPlacement``.

A placement is the durable fact "node *N* hosts this StaticSet's static Pod, at
revision *R*". It has to outlive the scheduler's decision and every process, because
a static Pod lives on a host and only a host can remove it.

The record is therefore its own object rather than a field of the StaticSet's
status:

* the **central controller** owns its spec — which node was chosen, for which
  generation, and whether the placement is still wanted;
* the **node's manager** owns its status — which revision is installed, whether the
  write is acknowledged, and why it failed. A per-node manager writing into a shared
  StaticSet status would fight the controller for the same field, and an object owned
  by one manager is exactly what an optimistic-concurrency write is designed for;
* it holds a finalizer, so no deletion — its own, or garbage collection after the
  StaticSet is gone — can orphan a manifest that kubelet would keep starting.

A placement object is not the *scheduling* mechanism. Where a placement goes is
decided by a probe workload and the real scheduler; the record is what that decision
is written down in, once it has been made.
"""

from __future__ import annotations

from pydantic import Field, model_validator

import based_operators as bk

from static_sets.base import WireModel
from static_sets.models import CleanupReason, PlacementPhase

__all__ = [
    "PlacementSpec",
    "PlacementState",
    "StaticPodPlacement",
]


class PlacementSpec(WireModel):
    """What the controller has decided about one placement."""

    static_set: str = Field(min_length=1)
    """The owning StaticSet's name."""

    static_set_uid: str = Field(min_length=1)
    """The owning StaticSet's UID, the authoritative ownership token."""

    node: str = Field(min_length=1)
    """The node the scheduler chose for this placement."""

    generation: int = Field(default=0, ge=0)
    """The StaticSet generation this placement was placed for.

    It is the *ordering* marker. Revisions are content hashes, so their names carry
    no order; the generation is what tells a manager which of two revisions on one
    node is the newer one.
    """

    desired: bool = True
    """Whether the placement is still wanted.

    ``False`` is a cleanup intent: the manager removes the static Pod and no longer
    installs anything for this placement. The object stays until the manager
    acknowledges the removal, because the operator never claims a host was cleaned up
    before the host says so.
    """

    reason: CleanupReason | None = None
    """Why the placement is no longer wanted. Only set when :attr:`desired` is ``False``."""

    remove_configuration: bool = True
    """Whether the materialized configuration may be deleted once the workload stops.

    Copied from ``spec.cleanup`` when the placement is created, because a cleanup may
    have to happen after the StaticSet itself is gone and its policy is no longer
    readable.
    """

    @model_validator(mode="after")
    def _check_reason(self) -> PlacementSpec:
        """Keep ``reason`` consistent with ``desired``.

        Returns:
            The validated specification.

        Raises:
            ValueError: If a cleanup intent carries no reason, or a desired placement
                carries one.
        """
        if not self.desired and self.reason is None:
            raise ValueError(
                "spec.reason is required when spec.desired is false: a removal has to say "
                "why the placement is no longer wanted"
            )
        if self.desired and self.reason is not None:
            raise ValueError("spec.reason is only valid when spec.desired is false")
        return self


class PlacementState(WireModel):
    """What the node's manager reports about one placement."""

    identity: str | None = None
    """The placement identity, echoed back for anybody reading the object alone."""

    revision: str | None = None
    """The revision durably installed on the node, if any."""

    static_pod_name: str | None = None
    """The name of the manifest backing the placement, which carries the revision."""

    phase: PlacementPhase = PlacementPhase.PENDING
    """Where the placement stands on its node."""

    acknowledged: bool = False
    """Whether the manager confirmed the reported state is durable."""

    observed_generation: int = 0
    """The StaticSet generation the reported state was produced for."""

    message: str | None = None
    """An explanation for the operator's status, including any refusal."""

    warnings: list[str] = Field(default_factory=list)
    """Non-fatal disclosures the compiler made, surfaced rather than swallowed."""

    updated_at: str | None = None
    """When the manager last wrote this status, RFC 3339."""


class StaticPodPlacement(bk.CustomResource[PlacementSpec, PlacementState]):
    """A namespaced record of one node's static Pod for one StaticSet."""

    resource_config = bk.ResourceConfig(
        group="staticsets.cloudlane.dev",
        version="v1",
        kind="StaticPodPlacement",
        plural="staticpodplacements",
        scope=bk.Scope.NAMESPACED,
        singular="staticpodplacement",
        short_names=("spp",),
    )
