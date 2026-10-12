"""Manage sets of static Pods from Kubernetes.

A :class:`~static_sets.models.StaticSet` describes a workload that must become a
*static Pod*: a Pod definition persisted on a host and started by that host's
kubelet, so that it returns after a reboot even when the Kubernetes API is
unreachable. The operator realises the request by scheduling installer Pods — a
Deployment or a DaemonSet — whose containers compile the workload, materialize the
configuration it references, and write the manifest durably to their own node.

The modules map onto the pieces of that job:

:mod:`static_sets.models`
    The ``StaticSet`` custom resource, including its placement, update, offline
    and cleanup policies and its status.
:mod:`static_sets.workload`
    The supported subset of ``PodTemplateSpec``: what can be compiled, and what is
    rejected rather than silently dropped.
:mod:`static_sets.translation`
    The compiler, including its Secret-handling rules.
:mod:`static_sets.revision`
    Deterministic revision identity.
:mod:`static_sets.install`
    Durable, ownership-checked installation of a revision on a node.
:mod:`static_sets.installer`
    The on-node runtime, including the offline behaviour.
:mod:`static_sets.placement`
    Installer placement and the installer workload itself.
:mod:`static_sets.controller`
    The pure reconcile planner and status builder.
:mod:`static_sets.paths`
    Path validation and durable write primitives.
"""

from static_sets.controller import (
    CleanupAction,
    ObservedState,
    ObservedWorkload,
    ReconcilePlan,
    cleanup_worker_pod,
    plan,
)
from static_sets.errors import (
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
    verify_owner,
)
from static_sets.installer import (
    ApiUnavailableError,
    Assignment,
    ImageChecker,
    PlacementApi,
    PlacementReport,
    cleanup_placement,
    reconcile,
)
from static_sets.models import (
    CleanupPolicy,
    CleanupReason,
    CleanupRecord,
    CleanupState,
    Condition,
    ImageProvisioning,
    InstallerSpec,
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
from static_sets.placement import (
    InstallerPod,
    NodeInfo,
    ObservedPlacement,
    eligible_nodes,
    installer_workload,
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
    "CleanupAction",
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
    "InstallerPod",
    "InstallerSpec",
    "Mode",
    "NodeInfo",
    "NodeUnreachablePolicy",
    "NotOfflineReadyError",
    "ObservedPlacement",
    "ObservedState",
    "ObservedWorkload",
    "OfflinePolicy",
    "OwnershipError",
    "OwnershipRecord",
    "PathSecurityError",
    "Placement",
    "PlacementApi",
    "PlacementLayout",
    "PlacementPhase",
    "PlacementReport",
    "PlacementStatus",
    "ReconcilePlan",
    "ResolvedConfig",
    "SecretDisclosureError",
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
    "cleanup_placement",
    "cleanup_worker_pod",
    "compile_workload",
    "compute_revision",
    "config_references",
    "eligible_nodes",
    "install_revision",
    "installer_workload",
    "is_revision",
    "plan",
    "read_intent",
    "read_record",
    "reconcile",
    "remove_workload",
    "render_manifest",
    "verify_owner",
]
