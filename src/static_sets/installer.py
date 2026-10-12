"""The installer that runs on a node and writes the static workload there.

An installer is a short-lived, single-purpose process. It learns which StaticSet
it serves and which node it is on from its own Pod (the operator injects both), so
it never has to guess, and it derives the placement identity from the StaticSet UID
and the node name, which is what lets it adopt the placement a replaced installer
left behind instead of creating a second, conflicting one.

The runtime is written as a pure function over an injectable API and an injectable
image check, so its behaviour — including what it does when the API is unreachable —
is exercised by unit tests rather than only by a live cluster. Two rules matter:

* **an unreachable API is not an empty desired state.** If the StaticSet cannot be
  read, the installer changes nothing locally and reports that it is offline. The
  last committed revision stays active, which is exactly what has to keep working
  after a reboot;
* **nothing is published before its dependencies exist.** Configuration is
  materialized and flushed, and images are confirmed when policy demands it, before
  the manifest is swapped in.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

from static_sets.errors import DefinitionError, NotOfflineReadyError
from static_sets.install import (
    CleanupIntent,
    PlacementLayout,
    claim_placement,
    install_revision,
    read_intent,
    read_record,
    remove_workload,
    write_intent,
)
from static_sets.models import ImageProvisioning, StaticSet
from static_sets.naming import (
    ANNOTATION_GENERATION,
    ANNOTATION_OWNER,
    ANNOTATION_PLACEMENT,
    ANNOTATION_REVISION,
    ANNOTATION_SET,
    ANNOTATION_SET_UID,
    MANAGED_BY,
    placement_configmap_name,
    placement_identity,
    static_pod_name,
)
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
    "PlacementApi",
    "PlacementReport",
    "cleanup_placement",
    "reconcile",
]


class ApiUnavailableError(RuntimeError):
    """Raised by a :class:`PlacementApi` implementation when the API is unreachable.

    The installer catches this and treats it as "retain everything", never as "the
    desired state is empty".
    """


class PlacementApi(Protocol):
    """The slice of the Kubernetes API an installer needs.

    Implemented against a real cluster in the command-line front end and against an
    in-memory fake in tests.
    """

    def get_static_set(self, namespace: str, name: str) -> StaticSet:
        """Return the StaticSet the installer serves.

        Args:
            namespace: The StaticSet's namespace.
            name: The StaticSet's name.

        Returns:
            The validated StaticSet.

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

    def publish_placement(
        self,
        namespace: str,
        name: str,
        payload: Mapping[str, str],
        *,
        static_set: str,
    ) -> None:
        """Create or update the ConfigMap an installer acknowledges through.

        Args:
            namespace: The ConfigMap's namespace.
            name: The ConfigMap's name.
            payload: The acknowledgment payload.
            static_set: The owning StaticSet's name, which the record is labelled
                with so the operator can select it.

        Raises:
            ApiUnavailable: If the API cannot be reached.
        """
        ...

    def get_placement(self, namespace: str, name: str) -> Mapping[str, str] | None:
        """Return the payload the operator published for a placement.

        The operator carries a cleanup intent through this same object, because the
        controller has no host access and the installer is the only process that
        can act on the node's filesystem.

        Args:
            namespace: The ConfigMap's namespace.
            name: The ConfigMap's name.

        Returns:
            The payload, or ``None`` when the operator published nothing.

        Raises:
            ApiUnavailable: If the API cannot be reached.
        """
        ...


ImageChecker = Callable[[list[str]], bool]
"""Confirms that a set of image references is present on the node.

Returns ``True`` only when every image is locally available. The default checker
cannot prove anything, so ``VerifiedBeforeInstall`` fails closed until one is
supplied — see the installation guide for what a real checker has to do.
"""


@dataclass(frozen=True)
class Assignment:
    """Everything the installer learns from its own Pod.

    Attributes:
        static_set: The StaticSet's name.
        namespace: The StaticSet's namespace.
        uid: The StaticSet's UID, the authoritative ownership token.
        generation: The generation the operator last published.
        node: The node the installer is running on.
        instance: The installer Pod's name, the instance identity.
        static_pod_path: kubelet's static Pod path.
        bundle_dir: The persistent bundle directory.
        placement: The placement identity, when the operator supplied one.
    """

    static_set: str
    namespace: str
    uid: str
    generation: int
    node: str
    instance: str
    static_pod_path: str
    bundle_dir: str
    placement: str | None = None

    @property
    def identity(self) -> str:
        """The placement identity, derived when the operator did not supply one."""
        return self.placement or placement_identity(self.uid, self.node)

    def layout(self, pod_name: str) -> PlacementLayout:
        """Return the layout this assignment is allowed to touch.

        Args:
            pod_name: The static Pod's name for this placement.

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
    """What an installer accomplished, for the operator's status.

    Attributes:
        placement: The placement identity.
        node: The node name.
        revision: The revision now installed.
        state: ``Installing``, ``Installed`` or ``Removed``.
        acknowledged: Whether local installation is durably complete.
        offline: Whether the API was unreachable, in which case nothing changed.
        warnings: Disclosures the operator should surface.
        message: A human-readable explanation.
    """

    placement: str
    node: str
    revision: str | None = None
    state: str = "Installing"
    acknowledged: bool = False
    offline: bool = False
    warnings: tuple[str, ...] = ()
    message: str | None = None

    def payload(self) -> dict[str, str]:
        """Return the acknowledgment payload an installer publishes.

        Returns:
            The payload, with every value a string because the transport is a
            ConfigMap.
        """
        return {
            "placement": self.placement,
            "node": self.node,
            "revision": self.revision or "",
            "state": self.state,
            "acknowledged": "true" if self.acknowledged else "false",
            "offline": "true" if self.offline else "false",
            "warnings": " | ".join(self.warnings),
            "message": self.message or "",
        }


@dataclass(frozen=True)
class _Target:
    """The resolved inputs of one reconcile, split out to keep the entry point small."""

    static_set: StaticSet
    pod_name: str
    layout: PlacementLayout
    resolved: ResolvedConfig
    references: tuple[ConfigReference, ...] = field(default_factory=tuple)
    intent: CleanupIntent | None = None


def reconcile(
    assignment: Assignment,
    *,
    api: PlacementApi,
    now: str,
    pod_name: str | None = None,
    image_checker: ImageChecker | None = None,
) -> PlacementReport:
    """Reconcile the installer's placement against its StaticSet.

    Args:
        assignment: What the installer learned from its Pod.
        api: The Kubernetes API slice it may use.
        now: The current time, RFC 3339, used for record timestamps.
        pod_name: The static Pod's name; derived from the assignment when unset.
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
    resolved_name = pod_name or _pod_name(assignment)
    target, unavailable = _resolve(assignment, api=api, pod_name=resolved_name)
    if unavailable is not None:
        return unavailable
    assert target is not None
    if target.intent is not None:
        return _carry_out_cleanup(target, assignment)
    return _apply(target, assignment, now=now, image_checker=image_checker)


def cleanup_placement(
    assignment: Assignment,
    *,
    api: PlacementApi,
    now: str,
    pod_name: str | None = None,
    terminated: bool = True,
) -> PlacementReport:
    """Carry out a pending cleanup intent for the installer's own placement.

    Args:
        assignment: What the installer learned from its Pod.
        api: The Kubernetes API slice it may use.
        now: The current time, RFC 3339.
        pod_name: The static Pod's name; derived from the assignment when unset.
        terminated: Whether the static workload is confirmed to have stopped.

    Returns:
        The report to publish.

    Raises:
        ApiUnavailableError: If the API cannot be reached, in which case nothing is
            removed.
        StaticSetsError: If the intent and the local record disagree about
            ownership, which means the placement has been taken over.
    """
    del now
    resolved_name = pod_name or _pod_name(assignment)
    layout = assignment.layout(resolved_name)
    try:
        published = api.get_placement(
            assignment.namespace,
            placement_configmap_name(assignment.static_set, assignment.identity),
        )
    except ApiUnavailableError as error:
        return PlacementReport(
            placement=assignment.identity,
            node=assignment.node,
            offline=True,
            message=(
                f"the API is unreachable ({error}); no cleanup is performed without an "
                "authoritative intent"
            ),
        )
    intent = intent_from_payload(published) or read_intent(layout)
    if intent is None:
        record = read_record(layout)
        return PlacementReport(
            placement=assignment.identity,
            node=assignment.node,
            revision=record.revision if record is not None else None,
            state=record.state if record is not None else "Installing",
            acknowledged=True,
            message="no cleanup was requested for this placement",
        )
    write_intent(layout, intent)
    outcome = remove_workload(layout, intent=intent, terminated=terminated)
    return PlacementReport(
        placement=assignment.identity,
        node=assignment.node,
        revision=None,
        state="Removed",
        acknowledged=outcome.acknowledged,
        message=outcome.message,
    )


def _pod_name(assignment: Assignment) -> str:
    """Derive the static Pod's name from the assignment.

    Args:
        assignment: The installer's assignment.

    Returns:
        The static Pod's name.
    """
    return static_pod_name(assignment.static_set, assignment.identity)


def _resolve(
    assignment: Assignment,
    *,
    api: PlacementApi,
    pod_name: str,
) -> tuple[_Target | None, PlacementReport | None]:
    """Read the StaticSet and its configuration, or report the API as unreachable.

    Args:
        assignment: The installer's assignment.
        api: The Kubernetes API slice it may use.
        pod_name: The static Pod's name.

    Returns:
        The resolved target, or the report to publish when the API is unreachable.
    """
    layout = assignment.layout(pod_name)
    try:
        static_set = api.get_static_set(assignment.namespace, assignment.static_set)
        _check_declared_paths(static_set, assignment)
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
        published = api.get_placement(
            assignment.namespace,
            placement_configmap_name(assignment.static_set, assignment.identity),
        )
    except ApiUnavailableError as error:
        record = read_record(layout)
        return None, PlacementReport(
            placement=assignment.identity,
            node=assignment.node,
            revision=record.revision if record is not None else None,
            state=record.state if record is not None else "Installing",
            acknowledged=record is not None and record.revision is not None,
            offline=True,
            message=(
                f"the API is unreachable ({error}); the last committed local revision is "
                "retained and nothing is changed"
            ),
        )
    return (
        _Target(
            static_set=static_set,
            pod_name=pod_name,
            layout=layout,
            resolved=resolved,
            intent=intent_from_payload(published),
        ),
        None,
    )


def _check_declared_paths(static_set: StaticSet, assignment: Assignment) -> None:
    """Refuse to install at paths the StaticSet did not declare.

    The installer learns its mount points from its own Pod, and the operator builds
    that Pod from the StaticSet. If the two disagree — because the Pod template was
    edited, or because a different StaticSet's installer is running — writing would
    land somewhere nobody declared, so the installer refuses instead.

    Args:
        static_set: The StaticSet being reconciled.
        assignment: The installer's assignment.

    Raises:
        DefinitionError: If the declared and mounted paths differ.
    """
    installer = static_set.spec.installer
    if installer.static_pod_path != assignment.static_pod_path:
        raise DefinitionError(
            f"installer is mounted at {assignment.static_pod_path!r} but StaticSet "
            f"{assignment.static_set!r} declares "
            f"spec.installer.staticPodPath={installer.static_pod_path!r}"
        )
    if installer.bundle_dir != assignment.bundle_dir:
        raise DefinitionError(
            f"installer is mounted at {assignment.bundle_dir!r} but StaticSet "
            f"{assignment.static_set!r} declares spec.installer.bundleDir="
            f"{installer.bundle_dir!r}"
        )


def intent_from_payload(payload: Mapping[str, str] | None) -> CleanupIntent | None:
    """Rebuild a cleanup intent from the payload the operator published.

    Args:
        payload: The placement payload, or ``None``.

    Returns:
        The intent, or ``None`` when no cleanup was requested.
    """
    if not payload or payload.get("cleanup") != "true":
        return None
    return CleanupIntent(
        placement=payload["placement"],
        pod_name=payload["podName"],
        static_set=payload["staticSet"],
        static_set_uid=payload["uid"],
        namespace=payload["namespace"],
        node=payload["node"],
        generation=int(payload.get("generation", "0")),
        reason=payload.get("reason", "Relocated"),
        remove_configuration=payload.get("removeConfiguration", "true") == "true",
        requested_at=payload.get("requestedAt", ""),
    )


def _carry_out_cleanup(target: _Target, assignment: Assignment) -> PlacementReport:
    """Persist a cleanup intent learned from the API and act on it.

    The intent is written durably *before* anything is removed, so a crash between
    learning of the request and finishing it leaves the request discoverable.

    Args:
        target: The resolved target, including the intent.
        assignment: The installer's assignment.

    Returns:
        The report to publish.
    """
    assert target.intent is not None
    write_intent(target.layout, target.intent)
    outcome = remove_workload(target.layout, intent=target.intent, terminated=True)
    return PlacementReport(
        placement=assignment.identity,
        node=assignment.node,
        revision=None,
        state="Removed",
        acknowledged=outcome.acknowledged,
        message=outcome.message,
    )


def _apply(
    target: _Target,
    assignment: Assignment,
    *,
    now: str,
    image_checker: ImageChecker | None,
) -> PlacementReport:
    """Compile and install the target revision.

    Args:
        target: The resolved target.
        assignment: The installer's assignment.
        now: The current time, RFC 3339.
        image_checker: Confirms local image availability when policy requires it.

    Returns:
        The report to publish.

    Raises:
        NotOfflineReadyError: If image preparation is required and unconfirmed.
        StaticSetsError: If compilation or installation fails.
    """
    spec = target.static_set.spec
    compiled = compile_workload(spec, target.resolved)
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
    )
    return PlacementReport(
        placement=assignment.identity,
        node=assignment.node,
        revision=result.revision,
        state="Installed",
        acknowledged=True,
        warnings=result.warnings,
        message=(
            f"installed {result.revision} ({'updated' if result.changed else 'already current'})"
        ),
    )


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
        assignment: The installer's assignment, used in the error message.

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
        assignment: The installer's assignment.

    Returns:
        The labels.
    """
    return {
        "app.kubernetes.io/managed-by": MANAGED_BY,
        "staticsets.cloudlane.dev/set": assignment.static_set,
        ANNOTATION_PLACEMENT: assignment.identity,
    }


def _annotations(assignment: Assignment, revision: str) -> dict[str, str]:
    """Return the provenance annotations of a static Pod.

    Args:
        assignment: The installer's assignment.
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


def placement_payload_name(assignment: Assignment) -> str:
    """Return the ConfigMap name an installer acknowledges its placement through.

    Args:
        assignment: The installer's assignment.

    Returns:
        The ConfigMap name.
    """
    return placement_configmap_name(assignment.static_set, assignment.identity)


def intent_for(assignment: Assignment) -> CleanupIntent | None:
    """Return the pending cleanup intent of the installer's own placement.

    Args:
        assignment: The installer's assignment.

    Returns:
        The intent, or ``None``.
    """
    return read_intent(assignment.layout(_pod_name(assignment)))
