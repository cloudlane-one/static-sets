"""The manager that runs on a node and writes the static workloads there.

There is exactly one manager per node, run by a DaemonSet, and it serves *every*
StaticSet in the cluster whose placement names its node — whatever namespace that
StaticSet lives in. A placement record is what tells it so; the central controller
creates one when the scheduler has chosen a node, and the manager is the only
process that ever touches that node's filesystem.

The runtime is written as a pure function over an injectable API and an injectable
image check, so its behaviour — including what it does when the API is unreachable —
is exercised by unit tests rather than only by a live cluster. Three rules matter:

* **an unreachable API is not an empty desired state.** If the placement's StaticSet
  cannot be read, the manager changes nothing locally and reports that it is offline.
  The last committed revision stays active, which is exactly what has to keep working
  after a reboot;
* **nothing is published before its dependencies exist.** Configuration is
  materialized and flushed, and images are confirmed when policy demands it, before
  the manifest is swapped in;
* **the record decides.** Only a placement whose spec says ``desired: false`` is
  removed. A missing placement is not a removal: the operator deletes the object
  only after the node has acknowledged that the static Pod is gone.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from static_sets.errors import (
    ApiUnavailableError,
    DefinitionError,
    NotOfflineReadyError,
    StaticSetsError,
)
from static_sets.install import (
    CleanupIntent,
    OwnershipRecord,
    PlacementLayout,
    acknowledge,
    claim_placement,
    install_revision,
    read_record,
    remove_workload,
    retire_revision,
    write_intent,
)
from static_sets.models import (
    ImageProvisioning,
    PlacementPhase,
    StaticSet,
    UpdateStrategy,
)
from static_sets.naming import (
    ANNOTATION_GENERATION,
    ANNOTATION_OWNER,
    ANNOTATION_PLACEMENT,
    ANNOTATION_REVISION,
    ANNOTATION_SET,
    ANNOTATION_SET_UID,
    MANAGED_BY,
    PLACEMENT_LABEL,
    SET_LABEL,
    SET_UID_LABEL,
    placement_identity,
    static_pod_name,
)
from static_sets.placements import StaticPodPlacement
from static_sets.translation import (
    CompiledWorkload,
    ConfigReference,
    ResolvedConfig,
    compile_workload,
    config_references,
)

__all__ = [
    "Assignment",
    "ImageChecker",
    "ManagerApi",
    "ManagerConfig",
    "MirrorPod",
    "PlacementApi",
    "PlacementReport",
    "cleanup",
    "reconcile",
]

NODE_ENV = "STATIC_SETS_NODE"
"""The environment variable carrying the node's name, from the downward API."""

HOSTNAME_ENV = "NODE_NAME"
"""The conventional name of the same variable, as the downward API usually spells it."""

MANAGER_POD_ENV = "STATIC_SETS_MANAGER_POD"
"""The environment variable carrying the manager Pod's own name."""

STATIC_POD_PATH_ENV = "STATIC_SETS_STATIC_POD_PATH"
"""The environment variable carrying kubelet's static Pod directory."""

BUNDLE_DIR_ENV = "STATIC_SETS_BUNDLE_DIR"
"""The environment variable carrying the manager's persistent bundle directory."""

DEFAULT_STATIC_POD_PATH = "/etc/kubernetes/manifests"
"""kubelet's default ``staticPodPath``."""

DEFAULT_BUNDLE_DIR = "/var/lib/static-sets"
"""Where the manager keeps ownership records and revision directories by default."""


class PlacementApi(Protocol):
    """The slice of the Kubernetes API a manager needs.

    Implemented against a real cluster in :class:`static_sets.cluster.ClusterApi` and
    against an in-memory fake in tests.
    """

    def get_static_set(self, namespace: str, name: str) -> StaticSet | None:
        """Return the StaticSet a placement belongs to.

        Args:
            namespace: The StaticSet's namespace.
            name: The StaticSet's name.

        Returns:
            The validated StaticSet, or ``None`` when it no longer exists.

        Raises:
            ApiUnavailableError: If the API cannot be reached.
        """
        ...

    def get_config_map(self, namespace: str, name: str) -> Mapping[str, bytes] | None:
        """Return a ConfigMap's data, or ``None`` when it does not exist.

        Args:
            namespace: The ConfigMap's namespace.
            name: The ConfigMap's name.

        Returns:
            The data, or ``None``.

        Raises:
            ApiUnavailableError: If the API cannot be reached.
        """
        ...

    def get_secret(self, namespace: str, name: str) -> Mapping[str, bytes] | None:
        """Return a Secret's data, or ``None`` when it does not exist.

        Args:
            namespace: The Secret's namespace.
            name: The Secret's name.

        Returns:
            The data, or ``None``.

        Raises:
            ApiUnavailableError: If the API cannot be reached.
        """
        ...

    def get_pod(self, namespace: str, name: str) -> MirrorPod | None:
        """Return the mirror Pod kubelet published for a static Pod.

        The mirror Pod is the only proof that kubelet accepted a manifest, so it is
        what a ``Surge`` rollout waits for and what a cleanup waits to disappear.

        Args:
            namespace: The namespace the static Pod's manifest declares.
            name: The static Pod's name.

        Returns:
            The mirror Pod, or ``None`` when kubelet has not published one.

        Raises:
            ApiUnavailableError: If the API cannot be reached.
        """
        ...


ManagerApi = PlacementApi
"""Alias kept for readability at call sites that talk about the manager."""


ImageChecker = Callable[[list[str]], bool]
"""Confirms that a set of image references is present on the node.

Returns ``True`` only when every image is locally available. The default checker
cannot prove anything, so ``VerifiedBeforeInstall`` fails closed until one is
supplied — see the installation guide for what a real checker has to do.
"""


@dataclass(frozen=True)
class ManagerConfig:
    """How one node's manager is configured.

    Unlike the StaticSet's ``spec``, none of this is per-StaticSet: the node has one
    kubelet static Pod directory and one bundle, and every StaticSet whose placement
    lands there is written through them. A StaticSet therefore cannot widen the host
    access of the process that writes it.

    Attributes:
        node: The node this manager owns, from the downward API.
        instance: The manager Pod's name, the instance identity in local records.
        static_pod_path: kubelet's static Pod path on this node.
        bundle_dir: The persistent bundle directory this manager owns.
    """

    node: str
    instance: str
    static_pod_path: str = DEFAULT_STATIC_POD_PATH
    bundle_dir: str = DEFAULT_BUNDLE_DIR

    @classmethod
    def from_environment(cls, environ: Mapping[str, str]) -> ManagerConfig:
        """Build the configuration from the manager Pod's environment.

        Args:
            environ: The environment to read.

        Returns:
            The configuration.

        Raises:
            StaticSetsError: If the node or the Pod's name is missing, in which case
                the process cannot know which host it is responsible for.
        """
        node = environ.get(NODE_ENV) or environ.get(HOSTNAME_ENV)
        if not node:
            raise StaticSetsError(
                f"the manager environment names no node: set {NODE_ENV} from the downward "
                "API's spec.nodeName"
            )
        return cls(
            node=node,
            instance=environ.get(MANAGER_POD_ENV) or f"manager-{node}",
            static_pod_path=environ.get(STATIC_POD_PATH_ENV) or DEFAULT_STATIC_POD_PATH,
            bundle_dir=environ.get(BUNDLE_DIR_ENV) or DEFAULT_BUNDLE_DIR,
        )

    def assignment(self, placement: StaticPodPlacement) -> Assignment:
        """Build the assignment for one placement record.

        Args:
            placement: The placement this manager was asked to serve.

        Returns:
            The assignment.

        Raises:
            DefinitionError: If the placement belongs to another node, or carries no
                namespace.
        """
        spec = placement.spec
        metadata = placement.metadata
        if spec.node != self.node:
            raise DefinitionError(
                f"placement {metadata.name!r} belongs to node {spec.node!r}, not to {self.node!r}"
            )
        if not metadata.namespace:
            raise DefinitionError(f"placement {metadata.name!r} has no namespace")
        return Assignment.for_placement(
            namespace=metadata.namespace,
            static_set=spec.static_set,
            uid=spec.static_set_uid,
            node=spec.node,
            generation=spec.generation,
            instance=self.instance,
            static_pod_path=self.static_pod_path,
            bundle_dir=self.bundle_dir,
        )


@dataclass(frozen=True)
class MirrorPod:
    """What a manager observes about a static Pod's mirror.

    Attributes:
        phase: The mirror Pod's phase.
        ready: Whether every container reports ``Ready``. ``None`` when the Pod has
            no container statuses yet.
    """

    phase: str | None = None
    ready: bool | None = None


@dataclass(frozen=True)
class Assignment:
    """Everything one placement tells the manager, plus its own node configuration.

    Attributes:
        namespace: The StaticSet's namespace.
        static_set: The StaticSet's name.
        uid: The StaticSet's UID, the authoritative ownership token.
        node: The node this manager owns.
        identity: The placement identity, derived from the UID and the node.
        generation: The generation the placement was created for.
        instance: The manager Pod's name, the instance identity in the local record.
        static_pod_path: kubelet's static Pod path on this node.
        bundle_dir: The persistent bundle directory this manager owns.
    """

    namespace: str
    static_set: str
    uid: str
    node: str
    identity: str
    generation: int
    instance: str
    static_pod_path: str
    bundle_dir: str

    def __post_init__(self) -> None:
        """Refuse a node configuration that would write into kubelet's path.

        Raises:
            DefinitionError: If either path is not absolute, or they overlap.
        """
        for field_name in ("static_pod_path", "bundle_dir"):
            value: str = getattr(self, field_name)
            if not _is_absolute(value):
                raise DefinitionError(
                    f"the manager's {field_name} must be an absolute path, got {value!r}"
                )
        if _overlaps(self.static_pod_path, self.bundle_dir):
            raise DefinitionError(
                f"the manager's staticPodPath ({self.static_pod_path!r}) and bundleDir "
                f"({self.bundle_dir!r}) must be disjoint: kubelet watches the static Pod "
                "path, so temporary and revision files must not live beneath it"
            )

    @classmethod
    def for_placement(
        cls,
        *,
        namespace: str,
        static_set: str,
        uid: str,
        node: str,
        generation: int,
        instance: str,
        static_pod_path: str,
        bundle_dir: str,
    ) -> Assignment:
        """Build an assignment, deriving the placement identity.

        Args:
            namespace: The StaticSet's namespace.
            static_set: The StaticSet's name.
            uid: The StaticSet's UID.
            node: The node this manager owns.
            generation: The placement's generation.
            instance: The manager Pod's name.
            static_pod_path: kubelet's static Pod path.
            bundle_dir: The persistent bundle directory.

        Returns:
            The assignment.
        """
        return cls(
            namespace=namespace,
            static_set=static_set,
            uid=uid,
            node=node,
            identity=placement_identity(uid, node),
            generation=generation,
            instance=instance,
            static_pod_path=static_pod_path,
            bundle_dir=bundle_dir,
        )

    def layout(self, pod_name: str) -> PlacementLayout:
        """Return the layout this assignment is allowed to touch.

        Args:
            pod_name: The static Pod's name for this placement and revision.

        Returns:
            The placement layout.
        """
        return PlacementLayout(
            bundle_dir=self.bundle_dir,
            static_pod_path=self.static_pod_path,
            identity=self.identity,
            pod_name=pod_name,
        )


@dataclass(frozen=True)
class PlacementReport:
    """What a manager accomplished, for the placement's status.

    Attributes:
        revision: The revision now installed, or ``None`` after a removal.
        static_pod_name: The static Pod backing the placement, or ``None``.
        phase: The placement's phase on this node.
        acknowledged: Whether the reported state is durably complete.
        offline: Whether the API was unreachable, in which case nothing changed.
        warnings: Disclosures the operator should surface.
        message: A human-readable explanation.
    """

    revision: str | None = None
    static_pod_name: str | None = None
    phase: PlacementPhase = PlacementPhase.PENDING
    acknowledged: bool = False
    offline: bool = False
    warnings: tuple[str, ...] = ()
    message: str | None = None

    def payload(self) -> dict[str, str]:
        """Return the status payload a manager publishes.

        Returns:
            The payload, with every value a string because the transport may be a
            merge patch over a CRD status.
        """
        return {
            "revision": self.revision or "",
            "staticPodName": self.static_pod_name or "",
            "phase": self.phase.value,
            "acknowledged": "true" if self.acknowledged else "false",
            "offline": "true" if self.offline else "false",
            "warnings": " | ".join(self.warnings),
            "message": self.message or "",
        }


@dataclass(frozen=True)
class _Target:
    """The resolved inputs of one reconcile, split out to keep the entry point small."""

    static_set: StaticSet
    compiled: CompiledWorkload
    layout: PlacementLayout
    references: tuple[ConfigReference, ...] = field(default_factory=tuple)


def _is_absolute(value: str) -> bool:
    """Return whether a host path is absolute on either platform.

    A path declared by Kubernetes is POSIX, so a leading slash is the test that
    matters; a Windows drive path is accepted as well so that the same configuration
    can be exercised on a developer's machine.

    Args:
        value: The path to test.

    Returns:
        ``True`` when the path is absolute.
    """
    return value.startswith("/") or Path(value).is_absolute()


def _overlaps(first: str, second: str) -> bool:
    """Return whether two host paths are equal or one contains the other.

    The host's own path semantics are used rather than a POSIX-only test, so the same
    validation runs on a Linux node and on a developer's working copy.

    Args:
        first: One absolute path.
        second: The other absolute path.

    Returns:
        ``True`` when either path is the other or contains it.
    """
    left = os.path.normcase(os.path.abspath(first))
    right = os.path.normcase(os.path.abspath(second))
    return (
        left == right or left.startswith(f"{right}{os.sep}") or right.startswith(f"{left}{os.sep}")
    )


def reconcile(
    assignment: Assignment,
    *,
    api: PlacementApi,
    now: str,
    image_checker: ImageChecker | None = None,
) -> PlacementReport:
    """Reconcile one placement against its StaticSet.

    Args:
        assignment: The placement and the manager's node configuration.
        api: The Kubernetes API slice it may use.
        now: The current time, RFC 3339, used for record timestamps.
        image_checker: Confirms local image availability when policy requires it.

    Returns:
        The report to publish.

    Raises:
        NotOfflineReadyError: If image preparation is required by policy and the
            checker cannot confirm it. The caller reports this rather than
            publishing a revision that would not survive a reboot.
        ApiUnavailableError: If the API cannot be reached at all, in which case the
            caller must leave the local state untouched.
    """
    target, unavailable = _resolve(assignment, api=api)
    if unavailable is not None:
        return unavailable
    assert target is not None
    return _apply(target, assignment, now=now, api=api, image_checker=image_checker)


def cleanup(
    assignment: Assignment,
    *,
    api: PlacementApi,
    now: str,
    reason: str,
    remove_configuration: bool,
) -> PlacementReport:
    """Remove the static Pod of a placement that is no longer wanted.

    Args:
        assignment: The placement and the manager's node configuration.
        api: The Kubernetes API slice it may use.
        now: The current time, RFC 3339.
        reason: Why the placement is being removed, as the controller recorded it.
        remove_configuration: Whether materialized configuration may be deleted once
            the workload is confirmed stopped.

    Returns:
        The report to publish.

    Raises:
        ApiUnavailableError: If the API cannot be reached, in which case nothing is
            removed.
        StaticSetsError: If the local record disagrees with the placement about
            ownership, which means the placement has been taken over.
    """
    record = read_record(assignment.layout(_cleanup_pod_name(assignment)))
    layout = assignment.layout(
        record.pod_name if record is not None else _cleanup_pod_name(assignment)
    )
    intent = CleanupIntent(
        placement=assignment.identity,
        pod_name=record.pod_name if record is not None else _cleanup_pod_name(assignment),
        static_set=assignment.static_set,
        static_set_uid=assignment.uid,
        namespace=assignment.namespace,
        node=assignment.node,
        generation=assignment.generation,
        reason=reason,
        remove_configuration=remove_configuration,
        requested_at=now,
    )
    # Persisted before anything is touched, so a crash between learning of the
    # removal and finishing it leaves the intent discoverable.
    write_intent(layout, intent)
    terminated = _terminated(api, assignment, _installed_revisions(record))
    outcome = remove_workload(layout, intent=intent, terminated=terminated)
    return PlacementReport(
        revision=None,
        static_pod_name=None,
        phase=PlacementPhase.CLEANED if outcome.acknowledged else PlacementPhase.CLEANING,
        acknowledged=outcome.acknowledged,
        message=outcome.message,
    )


def _cleanup_pod_name(assignment: Assignment) -> str:
    """Return a placeholder static Pod name, used when no record exists.

    Args:
        assignment: The placement's assignment.

    Returns:
        A name that cannot collide with a real revision's manifest.
    """
    return f"{assignment.static_set}-{assignment.identity}-removed"


def _installed_revisions(record: OwnershipRecord | None) -> tuple[str, ...]:
    """Return the revisions whose manifests a local record says are installed.

    Args:
        record: The placement's ownership record, if any.

    Returns:
        The active and retiring revisions that are still expected on disk.
    """
    if record is None:
        return ()
    return tuple(
        revision for revision in (record.revision, record.retiring) if revision is not None
    )


def _terminated(api: PlacementApi, assignment: Assignment, revisions: tuple[str, ...]) -> bool:
    """Return whether the placement's static workloads are confirmed stopped.

    A mirror Pod that is still published by kubelet means the workload may still be
    running, and deleting the files it mounts would break it. When no revision was
    ever installed there is nothing to wait for.

    Args:
        api: The Kubernetes API slice.
        assignment: The placement's assignment.
        revisions: The revisions whose mirror Pods to look for.

    Returns:
        ``True`` when every revision's workload may be treated as gone.

    Raises:
        ApiUnavailableError: If the API cannot be reached.
    """
    for revision in revisions:
        name = static_pod_name(assignment.static_set, assignment.identity, revision)
        if api.get_pod(assignment.namespace, name) is not None:
            return False
    return True


def _resolve(
    assignment: Assignment,
    *,
    api: PlacementApi,
) -> tuple[_Target | None, PlacementReport | None]:
    """Read the StaticSet, resolve its configuration and compile it.

    Args:
        assignment: The placement's assignment.
        api: The Kubernetes API slice it may use.

    Returns:
        The resolved target, or the report to publish when the API is unreachable.

    Raises:
        ApiUnavailableError: If the API is unreachable and no local record exists.
    """
    try:
        static_set = api.get_static_set(assignment.namespace, assignment.static_set)
        if static_set is None:
            return None, PlacementReport(
                phase=PlacementPhase.CLEANING,
                message=(
                    "the StaticSet no longer exists; the placement is awaiting the "
                    "controller's removal request"
                ),
            )
        references = config_references(static_set.spec)
        resolved = ResolvedConfig(
            config_maps={
                reference.name: data
                for reference in references
                if reference.kind == "ConfigMap"
                and (data := api.get_config_map(assignment.namespace, reference.name)) is not None
            },
            secrets={
                reference.name: data
                for reference in references
                if reference.kind == "Secret"
                and (data := api.get_secret(assignment.namespace, reference.name)) is not None
            },
        )
    except ApiUnavailableError as error:
        record = read_record(assignment.layout(_cleanup_pod_name(assignment)))
        return None, PlacementReport(
            revision=record.revision if record is not None else None,
            static_pod_name=record.pod_name if record is not None else None,
            phase=(
                PlacementPhase.INSTALLED
                if record is not None and record.revision
                else PlacementPhase.PENDING
            ),
            acknowledged=record is not None and record.revision is not None,
            offline=True,
            message=(
                f"the API is unreachable ({error}); the last committed local revision is "
                "retained and nothing is changed"
            ),
        )
    compiled = compile_workload(static_set.spec, resolved)
    pod_name = static_pod_name(assignment.static_set, assignment.identity, compiled.revision)
    return (
        _Target(
            static_set=static_set,
            compiled=compiled,
            layout=assignment.layout(pod_name),
            references=references,
        ),
        None,
    )


def _apply(
    target: _Target,
    assignment: Assignment,
    *,
    now: str,
    api: PlacementApi,
    image_checker: ImageChecker | None,
) -> PlacementReport:
    """Compile and install the target revision, retiring what it replaces.

    Args:
        target: The resolved target.
        assignment: The placement's assignment.
        now: The current time, RFC 3339.
        api: The Kubernetes API slice, used to observe the new mirror Pod.
        image_checker: Confirms local image availability when policy requires it.

    Returns:
        The report to publish.

    Raises:
        NotOfflineReadyError: If image preparation is required and unconfirmed.
        StaticSetsError: If compilation or installation fails.
    """
    spec = target.static_set.spec
    compiled = target.compiled
    _confirm_images(spec.offline.image_provisioning, compiled, image_checker, assignment)

    claim_placement(
        target.layout,
        static_set=assignment.static_set,
        static_set_uid=assignment.uid,
        namespace=assignment.namespace,
        node=assignment.node,
        generation=assignment.generation,
        owner=assignment.instance,
        now=now,
    )
    record = read_record(target.layout)
    active = record.revision if record is not None else None
    retiring = record.retiring if record is not None else None
    surge = spec.update.strategy is UpdateStrategy.SURGE

    if active == compiled.revision:
        # Nothing to install. The only work left may be a Surge that is still waiting
        # for the revision already running to be observed up, so that the one it
        # replaced can finally be removed.
        message = f"already current at {compiled.revision}"
        stale = retiring if retiring and retiring != active else None
        if stale and (not surge or _mirror_ready(api, assignment, compiled.revision)):
            retire_revision(
                target.layout,
                stale,
                static_set=assignment.static_set,
                owner=assignment.instance,
                now=now,
            )
            message = f"{message}; retired {stale}"
        elif stale:
            message = f"{message}; {stale} is still installed until it is retired"
        acknowledge(target.layout, owner=assignment.instance, now=now)
        return PlacementReport(
            revision=compiled.revision,
            static_pod_name=target.layout.pod_name,
            phase=PlacementPhase.INSTALLED,
            acknowledged=True,
            warnings=compiled.warnings,
            message=message,
        )

    if not surge:
        # Recreate means exactly one revision of a node at any moment: the old
        # manifest is gone before the new one is written.
        for stale in sorted({revision for revision in (active, retiring) if revision}):
            retire_revision(
                target.layout,
                stale,
                static_set=assignment.static_set,
                owner=assignment.instance,
                now=now,
            )
        retiring = None
    else:
        # Surge: the previous revision stays installed until the new one is observed
        # running, which is what pays for two copies for a moment. If a retirement is
        # still pending when a *third* revision arrives, it is completed first: a node
        # holding three revisions is worse than the gap, and the pending one has
        # already been superseded twice.
        pending = retiring if retiring and retiring != active else None
        retiring = active
        if pending:
            retire_revision(
                target.layout,
                pending,
                static_set=assignment.static_set,
                owner=assignment.instance,
                now=now,
            )

    result = install_revision(
        target.layout,
        compiled,
        namespace=assignment.namespace,
        generation=assignment.generation,
        owner=assignment.instance,
        now=now,
        retained_revisions=spec.offline.retained_revisions,
        labels=_labels(assignment),
        annotations=_annotations(assignment, compiled.revision),
        retiring=retiring,
    )
    message = f"installed {result.revision} ({'updated' if result.changed else 'already current'})"
    if retiring:
        if _mirror_ready(api, assignment, result.revision):
            retire_revision(
                target.layout,
                retiring,
                static_set=assignment.static_set,
                owner=assignment.instance,
                now=now,
            )
            message = f"{message}; retired {retiring} once the new Pod was observed running"
        else:
            message = (
                f"{message}; {retiring} is still installed until "
                f"{result.revision} is observed running"
            )
    return PlacementReport(
        revision=result.revision,
        static_pod_name=target.layout.pod_name,
        phase=PlacementPhase.INSTALLED,
        acknowledged=True,
        warnings=result.warnings,
        message=message,
    )


def _mirror_ready(api: PlacementApi, assignment: Assignment, revision: str) -> bool:
    """Return whether kubelet reports the revision's static Pod as running.

    Args:
        api: The Kubernetes API slice.
        assignment: The placement's assignment.
        revision: The revision whose mirror Pod to look for.

    Returns:
        ``True`` when the mirror Pod exists and all of its containers are ready.
    """
    name = static_pod_name(assignment.static_set, assignment.identity, revision)
    try:
        pod = api.get_pod(assignment.namespace, name)
    except ApiUnavailableError:
        return False
    if pod is None:
        return False
    return pod.ready is True or (pod.ready is None and pod.phase == "Running")


def _confirm_images(
    provisioning: ImageProvisioning,
    compiled: CompiledWorkload,
    checker: ImageChecker | None,
    assignment: Assignment,
) -> None:
    """Refuse to publish a revision whose images are not confirmed locally present.

    Args:
        provisioning: The declared image-provisioning policy.
        compiled: The compiled workload, which names the images.
        checker: The checker supplied by the caller, if any.
        assignment: The placement's assignment, used in the error message.

    Raises:
        NotOfflineReadyError: If the policy requires verification and the checker
            is absent or reports a missing image.
    """
    if provisioning is not ImageProvisioning.VERIFIED_BEFORE_INSTALL:
        return
    images = _images(compiled.pod)
    if checker is None:
        raise NotOfflineReadyError(
            "spec.offline.imageProvisioning is VerifiedBeforeInstall but no image checker was "
            "supplied; call reconcile(..., image_checker=...) with a checker that can prove "
            "local availability, or switch the policy to ExternallyProvisioned"
        )
    if not checker(images):
        raise NotOfflineReadyError(
            f"not all images for placement {assignment.identity} are present on "
            f"{assignment.node}: {', '.join(images)}"
        )


def _images(pod: Mapping[str, Any]) -> list[str]:
    """Return every image a compiled Pod references.

    Args:
        pod: The compiled Pod.

    Returns:
        The image references, sorted and deduplicated.
    """
    containers = [
        *pod.get("spec", {}).get("containers", []),
        *pod.get("spec", {}).get("initContainers", []),
    ]
    return sorted({str(container["image"]) for container in containers})


def _labels(assignment: Assignment) -> dict[str, str]:
    """Return the provenance labels of a static Pod.

    Args:
        assignment: The placement's assignment.

    Returns:
        The labels.
    """
    return {
        "app.kubernetes.io/managed-by": MANAGED_BY,
        SET_LABEL: assignment.static_set,
        SET_UID_LABEL: assignment.uid,
        PLACEMENT_LABEL: assignment.identity,
    }


def _annotations(assignment: Assignment, revision: str) -> dict[str, str]:
    """Return the provenance annotations of a static Pod.

    Args:
        assignment: The placement's assignment.
        revision: The revision being installed.

    Returns:
        The annotations.
    """
    return {
        ANNOTATION_SET: assignment.static_set,
        ANNOTATION_SET_UID: assignment.uid,
        ANNOTATION_PLACEMENT: assignment.identity,
        ANNOTATION_REVISION: revision,
        ANNOTATION_GENERATION: str(assignment.generation),
        ANNOTATION_OWNER: assignment.instance,
    }
