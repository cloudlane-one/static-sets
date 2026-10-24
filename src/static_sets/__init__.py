"""Manage sets of static Pods from Kubernetes.

A :class:`~static_sets.models.StaticSet` describes a workload that must become a
*static Pod*: a Pod definition persisted on a host and started by that host's
kubelet, so that it returns after a reboot even when the Kubernetes API is
unreachable.

Two processes realise it:

* the **central controller** (``app/static_sets_operator.py``) watches StaticSets in
  every namespace. To learn where a workload should run it creates a *scheduling
  probe* — a ReplicaSet or a DaemonSet whose Pods the real scheduler places and
  whose runtime class no node can start — and records the scheduler's answer as one
  :class:`~static_sets.placements.StaticPodPlacement` per node;
* the **node manager** (``app/static_pod_manager.py``) runs once per node as a
  DaemonSet, watches those placement records, and writes, updates and removes the
  static Pod manifests on the node it owns.

The modules map onto the pieces of that job:

:mod:`static_sets.models`
    The ``StaticSet`` custom resource and its policies and status.
:mod:`static_sets.placements`
    The ``StaticPodPlacement`` record: the durable fact "this node hosts this
    revision of this StaticSet".
:mod:`static_sets.workload`
    The supported subset of ``PodTemplateSpec``: what can be compiled, and what is
    rejected rather than silently dropped.
:mod:`static_sets.translation`
    The compiler, including its Secret-handling rules.
:mod:`static_sets.revision`
    Deterministic revision identity.
:mod:`static_sets.probe`
    The scheduling probe, which is how the Kubernetes scheduler is asked for a
    placement.
:mod:`static_sets.controller`
    The pure reconcile planner and status builder.
:mod:`static_sets.manager`
    The on-node runtime, including the offline behaviour and the rollout order.
:mod:`static_sets.install`
    Durable, ownership-checked installation of a revision on a node.
:mod:`static_sets.paths`
    Path validation and durable write primitives.
"""

from static_sets.controller import (
    ObservedPlacement,
    ObservedState,
    PlacementChange,
    ProbeRef,
    ReconcilePlan,
    plan,
)
from static_sets.errors import (
    ApiUnavailableError,
    CleanupError,
    DefinitionError,
    InstallationError,
    NotOfflineReadyError,
    OwnershipError,
    PathSecurityError,
    SecretDisclosureError,
    StaticSetsError,
    TranslationError,
    UnsupportedFieldError,
)
from static_sets.install import (
    CleanupIntent,
    CleanupOutcome,
    InstallResult,
    OwnershipRecord,
    PlacementLayout,
    claim_placement,
    install_revision,
    read_intent,
    read_record,
    remove_workload,
    retire_revision,
    verify_owner,
)
from static_sets.manager import (
    Assignment,
    ImageChecker,
    ManagerApi,
    MirrorPod,
    PlacementApi,
    PlacementReport,
    cleanup,
    reconcile,
)
from static_sets.models import (
    CleanupPolicy,
    CleanupReason,
    CleanupRecord,
    CleanupState,
    Condition,
    ImageProvisioning,
    Mode,
    NodeUnreachablePolicy,
    OfflinePolicy,
    Placement,
    PlacementPhase,
    PlacementStatus,
    StaticSet,
    StaticSetSpec,
    StaticSetStatus,
    UpdatePolicy,
    UpdateStrategy,
    WorkloadHealth,
    WorkloadPolicy,
)
from static_sets.placements import (
    PlacementSpec,
    PlacementState,
    StaticPodPlacement,
)
from static_sets.probe import (
    NodeInfo,
    ObservedProbePod,
    ObservedProbeWorkload,
    ObservedWorkload,
    eligible_nodes,
    probe_workload,
)
from static_sets.revision import compute_revision, is_revision
from static_sets.translation import (
    CompiledWorkload,
    ConfigReference,
    ResolvedConfig,
    compile_workload,
    config_references,
    render_manifest,
)

__all__ = [
    "ApiUnavailableError",
    "Assignment",
    "CleanupError",
    "CleanupIntent",
    "CleanupOutcome",
    "CleanupPolicy",
    "CleanupReason",
    "CleanupRecord",
    "CleanupState",
    "CompiledWorkload",
    "Condition",
    "ConfigReference",
    "DefinitionError",
    "ImageChecker",
    "ImageProvisioning",
    "InstallResult",
    "InstallationError",
    "ManagerApi",
    "MirrorPod",
    "Mode",
    "NodeInfo",
    "NodeUnreachablePolicy",
    "NotOfflineReadyError",
    "ObservedPlacement",
    "ObservedProbePod",
    "ObservedProbeWorkload",
    "ObservedState",
    "ObservedWorkload",
    "OfflinePolicy",
    "OwnershipError",
    "OwnershipRecord",
    "PathSecurityError",
    "Placement",
    "PlacementApi",
    "PlacementChange",
    "PlacementLayout",
    "PlacementPhase",
    "PlacementReport",
    "PlacementSpec",
    "PlacementState",
    "PlacementStatus",
    "ProbeRef",
    "ReconcilePlan",
    "ResolvedConfig",
    "SecretDisclosureError",
    "StaticPodPlacement",
    "StaticSet",
    "StaticSetSpec",
    "StaticSetStatus",
    "StaticSetsError",
    "TranslationError",
    "UnsupportedFieldError",
    "UpdatePolicy",
    "UpdateStrategy",
    "WorkloadHealth",
    "WorkloadPolicy",
    "claim_placement",
    "cleanup",
    "compile_workload",
    "compute_revision",
    "config_references",
    "eligible_nodes",
    "install_revision",
    "is_revision",
    "plan",
    "probe_workload",
    "read_intent",
    "read_record",
    "reconcile",
    "remove_workload",
    "render_manifest",
    "retire_revision",
    "verify_owner",
]
