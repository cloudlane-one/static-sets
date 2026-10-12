"""The typed ``StaticSet`` custom resource.

A StaticSet asks for a set of *static Pods*: Pod definitions that are persisted on
a host and started by that host's kubelet, so that they come back after a reboot
with no Kubernetes API access. The operator realises the request by scheduling
*installer* Pods (a Deployment or a DaemonSet) whose containers place the compiled
static workload on their own node.

The specification is deliberately explicit about three things a static Pod cannot
inherit from the API server: placement, update policy and offline-retention
policy. Nothing is defaulted silently where a wrong default would be unsafe.
"""

from __future__ import annotations

import os
from enum import StrEnum
from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator

import based_operators as bk

from static_sets.base import WireModel
from static_sets.workload import PodTemplate, PullPolicy, ResourceRequirements

__all__ = [
    "Affinity",
    "CleanupPolicy",
    "CleanupReason",
    "CleanupRecord",
    "CleanupState",
    "Condition",
    "ImageProvisioning",
    "InstallerSpec",
    "LabelSelector",
    "Mode",
    "NodeAffinity",
    "NodeSelector",
    "NodeSelectorRequirement",
    "NodeSelectorTerm",
    "NodeUnreachablePolicy",
    "OfflinePolicy",
    "Placement",
    "PlacementPhase",
    "PlacementStatus",
    "PreferredSchedulingTerm",
    "StaticSet",
    "StaticSetSpec",
    "StaticSetStatus",
    "Toleration",
    "TopologySpreadConstraint",
    "UpdatePolicy",
    "UpdateStrategy",
    "WorkloadHealth",
    "WorkloadPolicy",
]


class Mode(StrEnum):
    """How many installers the operator creates, and where they land."""

    DEPLOYMENT = "Deployment"
    DAEMON_SET = "DaemonSet"


class UpdateStrategy(StrEnum):
    """How a new revision replaces the placements that carry the old one."""

    RECREATE = "Recreate"
    """Retire the old placement before installing the new one.

    The conservative default: at most one placement per node at any moment, so a
    node never holds two revisions of the same StaticSet at once.
    """

    SURGE = "Surge"
    """Install the new placement before retiring the old one.

    Trades temporary double-counting of capacity (and two revisions coexisting on
    a node) for availability during a rollout.
    """


class ImageProvisioning(StrEnum):
    """Who guarantees that an image is present on the node before it is needed."""

    EXTERNALLY_PROVISIONED = "ExternallyProvisioned"
    """Image availability is someone else's problem; reported as such in status."""

    VERIFIED_BEFORE_INSTALL = "VerifiedBeforeInstall"
    """The installer must confirm the image is locally present before publishing.

    A revision that cannot be confirmed is refused, so an unreachable registry
    cannot silently produce a revision that fails after a reboot.
    """


class NodeUnreachablePolicy(StrEnum):
    """What to do when a node holding a workload cannot be reached to clean up."""

    RETAIN = "Retain"
    """Keep the cleanup intent pending and keep reporting it. The default."""

    ABANDON_AFTER_GRACE = "AbandonAfterGrace"
    """After the grace period, stop waiting and record the workload as abandoned.

    An explicit administrator policy: it never claims the remote workload was
    removed, only that the operator stopped tracking it, and it warns about the
    residual static Pod that may still be started by that node's kubelet.
    """


class NodeSelectorRequirement(WireModel):
    """One ``matchExpressions``/``matchFields`` predicate of a node selector term."""

    key: str = Field(min_length=1)
    operator: Literal["In", "NotIn", "Exists", "DoesNotExist", "Gt", "Lt"]
    values: list[str] | None = None


class NodeSelectorTerm(WireModel):
    """A node selector term: the predicates a node must satisfy."""

    match_expressions: list[NodeSelectorRequirement] | None = None
    match_fields: list[NodeSelectorRequirement] | None = None


class NodeSelector(WireModel):
    """A required node-affinity selector."""

    node_selector_terms: list[NodeSelectorTerm] = Field(min_length=1)


class PreferredSchedulingTerm(WireModel):
    """A weighted preferred node-affinity term."""

    weight: int = Field(ge=1, le=100)
    preference: NodeSelectorTerm


class NodeAffinity(WireModel):
    """The node-affinity half of scheduler placement."""

    required_during_scheduling_ignored_during_execution: NodeSelector | None = None
    preferred_during_scheduling_ignored_during_execution: list[PreferredSchedulingTerm] | None = (
        None
    )


class Affinity(WireModel):
    """Scheduler affinity for the *installer* Pods.

    Only node affinity is supported. Pod affinity and anti-affinity describe
    co-location with other Pods, which a static Pod cannot honour because kubelet
    does not run the scheduler for it.
    """

    node_affinity: NodeAffinity | None = None


class Toleration(WireModel):
    """A toleration an installer Pod must carry to be scheduled on a tainted node."""

    key: str | None = None
    operator: Literal["Exists", "Equal"] | None = None
    value: str | None = None
    effect: Literal["NoSchedule", "PreferNoSchedule", "NoExecute"] | None = None
    toleration_seconds: int | None = None


class LabelSelectorRequirement(WireModel):
    """One predicate of a label selector."""

    key: str = Field(min_length=1)
    operator: Literal["In", "NotIn", "Exists", "DoesNotExist"]
    values: list[str] | None = None


class LabelSelector(WireModel):
    """A label selector used by a topology spread constraint."""

    match_labels: dict[str, str] | None = None
    match_expressions: list[LabelSelectorRequirement] | None = None


class TopologySpreadConstraint(WireModel):
    """A supported topology spread constraint for installer placement."""

    max_skew: int = Field(ge=1)
    topology_key: str = Field(min_length=1)
    when_unsatisfiable: Literal["DoNotSchedule", "ScheduleAnyway"] = "DoNotSchedule"
    label_selector: LabelSelector | None = None
    min_domains: int | None = None


class Placement(WireModel):
    """Where the operator may schedule installers.

    These settings steer the *scheduler*, which places installer Pods. They are
    not copied onto the emitted static workload, because kubelet never schedules a
    static Pod: it starts the file it finds on the node whose kubelet finds it.
    """

    node_selector: dict[str, str] | None = None
    affinity: Affinity | None = None
    tolerations: list[Toleration] | None = None
    topology_spread_constraints: list[TopologySpreadConstraint] | None = None


class UpdatePolicy(WireModel):
    """How a new revision is rolled out across installer placements."""

    strategy: UpdateStrategy = UpdateStrategy.RECREATE
    max_surge: int = Field(default=0, ge=0, le=10)
    max_unavailable: int = Field(default=0, ge=0, le=10)
    grace_period_seconds: int = Field(default=300, ge=0)

    @model_validator(mode="after")
    def _check_surge(self) -> UpdatePolicy:
        """Keep ``maxSurge`` consistent with the chosen strategy.

        Returns:
            The validated policy.

        Raises:
            DefinitionError: If ``Recreate`` is combined with a non-zero surge.
        """
        if self.strategy is UpdateStrategy.RECREATE and self.max_surge:
            raise ValueError(
                "update.strategy Recreate never overlaps placements, so update.maxSurge "
                "must be 0; use strategy Surge to allow overlap"
            )
        return self


class OfflinePolicy(WireModel):
    """What must hold before a revision may be declared offline-ready."""

    retained_revisions: int = Field(default=2, ge=1, le=10)
    """How many revision directories to keep per placement, newest first."""

    image_provisioning: ImageProvisioning = ImageProvisioning.EXTERNALLY_PROVISIONED
    allow_disconnected_start: bool = True
    """Whether a container with ``imagePullPolicy: Always`` may be installed.

    Set to ``False`` to require ``IfNotPresent`` or ``Never`` everywhere, so that
    a disconnected reboot can never wait on a registry.
    """


class CleanupPolicy(WireModel):
    """How the operator cleans up placements that are no longer wanted."""

    on_node_unreachable: NodeUnreachablePolicy = NodeUnreachablePolicy.RETAIN
    abandonment_grace_seconds: int = Field(default=3600, ge=0)
    remove_configuration_after_ack: bool = True
    """Delete the materialized configuration files after cleanup is acknowledged."""


class WorkloadPolicy(WireModel):
    """Guards the static workload, whose manifest bypasses normal Pod admission."""

    allow_privileged: bool = False
    """Permit ``securityContext.privileged`` in the workload.

    Off by default: a privileged static Pod is started by kubelet directly from a
    file, so nothing else in the cluster gets a chance to review it.
    """

    allow_inline_secret_env: bool = False
    """Permit Secret material to be written into the static Pod manifest as env.

    A static Pod has no offline way to resolve ``secretKeyRef``: either the value
    is written into the manifest (where the mirror Pod exposes it) or the variable
    is absent after a reboot. Off by default; see the translation guide.
    """


class InstallerSpec(WireModel):
    """The installer image and the exact host paths it is allowed to touch."""

    image: str = Field(min_length=1)
    image_pull_policy: PullPolicy = PullPolicy.IF_NOT_PRESENT
    static_pod_path: str = Field(default="/etc/kubernetes/manifests", min_length=1)
    """kubelet's ``staticPodPath``: where it watches for Pod manifests.

    The operator only reads this value to validate and to place files; it never
    writes kubelet configuration.
    """

    bundle_dir: str = Field(default="/var/lib/static-sets", min_length=1)
    """The persistent bundle directory the installer owns.

    Holds persisted placement records, revision directories and temporary files.
    Must live outside :attr:`static_pod_path`, because kubelet watches that
    directory and would try to start a half-written temporary file.
    """

    resources: ResourceRequirements | None = None
    """The installer container's resource reservation.

    Unset means a small fixed reservation (see the installer placement guide): the
    installer only writes files, and the static workload's own requests are not
    visible to the scheduler, so copying them onto the installer would double-count
    capacity without making placement decisions any more accurate.
    """

    @model_validator(mode="after")
    def _check_paths(self) -> InstallerSpec:
        """Validate the two host paths.

        Returns:
            The validated installer specification.

        Raises:
            DefinitionError: If either path is relative, or the two overlap.
        """
        for field in ("static_pod_path", "bundle_dir"):
            value: str = getattr(self, field)
            if not _is_absolute_path(value):
                raise ValueError(f"installer.{field} must be an absolute path, got {value!r}")
        static = Path(self.static_pod_path)
        bundle = Path(self.bundle_dir)
        if _is_within(static, bundle) or _is_within(bundle, static):
            raise ValueError(
                f"installer.static_pod_path ({self.static_pod_path!r}) and "
                f"installer.bundle_dir ({self.bundle_dir!r}) must be disjoint: kubelet "
                "watches the static Pod path, so temporary and backup files must not "
                "live beneath it"
            )
        return self


class StaticSetSpec(WireModel):
    """What a user asks for."""

    mode: Mode
    replicas: int | None = Field(default=None, ge=0, le=64)
    """Desired installer placements. Required for ``Deployment``, invalid for
    ``DaemonSet``."""

    template: PodTemplate
    """The workload template the operator compiles into a static Pod."""

    placement: Placement = Field(default_factory=Placement)
    update: UpdatePolicy = Field(default_factory=UpdatePolicy)
    offline: OfflinePolicy = Field(default_factory=OfflinePolicy)
    cleanup: CleanupPolicy = Field(default_factory=CleanupPolicy)
    policy: WorkloadPolicy = Field(default_factory=WorkloadPolicy)
    installer: InstallerSpec

    @model_validator(mode="after")
    def _check_replicas(self) -> StaticSetSpec:
        """Enforce the ``mode``/``replicas`` relationship.

        Returns:
            The validated specification.

        Raises:
            DefinitionError: If replicas are missing for a Deployment or present
                for a DaemonSet.
        """
        if self.mode is Mode.DEPLOYMENT and self.replicas is None:
            raise ValueError("spec.replicas is required when mode is Deployment")
        if self.mode is Mode.DAEMON_SET and self.replicas is not None:
            raise ValueError(
                "spec.replicas is invalid when mode is DaemonSet: the DaemonSet "
                "controller decides how many installers exist"
            )
        return self


class PlacementPhase(StrEnum):
    """The lifecycle of one installer placement."""

    PENDING = "Pending"
    INSTALLED = "Installed"
    CLEANING = "Cleaning"
    CLEANED = "Cleaned"
    FAILED = "Failed"


class PlacementStatus(WireModel):
    """The installed state of one placement, as acknowledged by its installer."""

    identity: str
    """The placement identity: stable across installer restarts and revisions."""

    node: str
    revision: str | None = None
    phase: PlacementPhase = PlacementPhase.PENDING
    acknowledged: bool = False
    """Whether the installer confirmed the revision is durably installed locally."""

    generation: int = 0
    message: str | None = None


class WorkloadHealth(WireModel):
    """Observed health of a workload, kept separate from installed state.

    A placement can be ``Installed`` and simultaneously unhealthy, or
    ``Installed`` and unobservable because its node is unreachable. Health is
    never used to decide whether installation succeeded.
    """

    identity: str
    node: str
    observed: bool = False
    phase: str | None = None
    ready: bool | None = None
    restart_count: int | None = None
    message: str | None = None


class CleanupReason(StrEnum):
    """Why a placement is being cleaned up."""

    SCALED_DOWN = "ScaledDown"
    DESELECTED = "Deselected"
    RELOCATED = "Relocated"
    RESOURCE_DELETED = "ResourceDeleted"


class CleanupState(StrEnum):
    """Where a cleanup intent is in the protocol of section 7."""

    REQUESTED = "Requested"
    """Persisted, and the installer has been asked to perform it."""

    QUEUED = "Queued"
    """The installer is gone; a cleanup worker pinned to the node is needed."""

    ACKNOWLEDGED = "Acknowledged"
    """The installer or worker confirmed the manifest is gone."""

    BLOCKED = "Blocked"
    """The node is unreachable, so cleanup cannot be confirmed."""

    ABANDONED = "Abandoned"
    """The administrator's force policy gave up; a residual workload may remain."""


class CleanupRecord(WireModel):
    """A persisted, authoritative cleanup intent for one placement."""

    identity: str
    node: str
    revision: str | None = None
    generation: int = 0
    reason: CleanupReason
    state: CleanupState = CleanupState.REQUESTED
    acknowledged: bool = False
    message: str | None = None


class Condition(WireModel):
    """A standard Kubernetes-style condition."""

    type: str
    status: Literal["True", "False", "Unknown"]
    reason: str
    message: str = ""
    observed_generation: int | None = None
    last_transition_time: str | None = None


class StaticSetStatus(WireModel):
    """What the operator reports about a StaticSet."""

    observed_generation: int = 0
    desired_revision: str | None = None
    placements: list[PlacementStatus] = Field(default_factory=list)
    health: list[WorkloadHealth] = Field(default_factory=list)
    cleanup: list[CleanupRecord] = Field(default_factory=list)
    conditions: list[Condition] = Field(default_factory=list)


def _is_absolute_path(value: str) -> bool:
    """Return whether a declared host path is absolute.

    A Kubernetes-declared path is a POSIX path, so a leading slash is the test that
    matters; a Windows drive path is also accepted so that the same code can be
    exercised on a developer's machine.

    Args:
        value: The declared path.

    Returns:
        ``True`` when the path is absolute.
    """
    return value.startswith("/") or Path(value).is_absolute()


def _is_within(inner: Path, outer: Path) -> bool:
    """Return whether one path is the other or lies inside it.

    Uses the platform's own path semantics, so it validates a Linux host layout and
    a developer's working copy with the same code.

    Args:
        inner: The candidate child path.
        outer: The candidate parent path.

    Returns:
        ``True`` when ``inner`` is ``outer`` or a descendant of it.
    """
    if os.path.normcase(str(inner)) == os.path.normcase(str(outer)):
        return True
    return Path(os.path.normcase(str(inner))).is_relative_to(Path(os.path.normcase(str(outer))))


class StaticSet(bk.CustomResource[StaticSetSpec, StaticSetStatus]):
    """A namespaced set of static Pods, managed through installer Pods."""

    resource_config = bk.ResourceConfig(
        group="staticsets.cloudlane.dev",
        version="v1",
        kind="StaticSet",
        plural="staticsets",
        scope=bk.Scope.NAMESPACED,
        singular="staticset",
        short_names=("sset",),
    )
