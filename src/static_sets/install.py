"""Durable local installation of a compiled static Pod.

This is the only part of the operator that writes to a host, and it is written so
that an interruption at any point leaves a consistent state:

1. the placement directory is created with restrictive permissions;
2. an ownership record is persisted *before* anything else, so that files left by
   an interrupted installation are still discoverable and attributable;
3. the revision's configuration files are written and ``fsync``-ed into an
   immutable, revision-named directory outside kubelet's watched path;
4. the manifest is written to a temporary file next to kubelet's path and then
   renamed onto the final name, so kubelet sees either the old revision or the new
   one and never a half-written file;
5. the ownership record is updated to name the active revision;
6. obsolete revisions are pruned, never the active one.

Nothing here deletes anything it does not own. Every destructive operation first
checks the ownership record, the StaticSet UID and the generation, which is what
stops a replaced installer or a late cleanup worker from destroying the workload a
successor already published.
"""

from __future__ import annotations

import json
import os
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from static_sets.errors import InstallationError, OwnershipError, PathSecurityError
from static_sets.naming import manifest_filename
from static_sets.paths import (
    assert_within,
    atomic_write,
    remove_tree_within,
)
from static_sets.revision import is_revision
from static_sets.translation import CompiledWorkload, render_manifest

__all__ = [
    "CleanupIntent",
    "CleanupOutcome",
    "InstallResult",
    "OwnershipRecord",
    "PlacementLayout",
    "claim_placement",
    "install_revision",
    "read_intent",
    "read_record",
    "remove_workload",
    "verify_owner",
    "write_intent",
]

RECORD_MODE = 0o600
"""Mode of an ownership record: readable only by the installer's own user."""

DIRECTORY_MODE = 0o700
"""Mode of the placement and revision directories."""

MANIFEST_MODE = 0o644
"""Mode of the manifest file kubelet reads."""

_ACTIVE_MANIFEST = "manifest.yaml"
"""File name of the manifest copy kept inside a revision directory."""


class OwnershipRecord(BaseModel):
    """The persistent record of who owns a placement and what is installed there.

    The record is the authority for every destructive operation. It is written
    before the first file and updated after the manifest swap, so a crash between
    the two leaves a record that says "installing", which the next run reconciles.
    """

    model_config = ConfigDict(extra="forbid")

    placement: str = Field(description="The placement identity.")
    pod_name: str = Field(description="The static Pod's name.")
    static_set: str = Field(description="The owning StaticSet's name.")
    static_set_uid: str = Field(description="The owning StaticSet's UID.")
    namespace: str = Field(description="The StaticSet's namespace.")
    node: str = Field(description="The node this placement lives on.")
    generation: int = Field(description="The generation the record was written for.")
    revision: str | None = Field(default=None, description="The active revision, if any.")
    history: list[str] = Field(
        default_factory=list,
        description=(
            "Installed revisions, newest first. Revision tokens are content hashes, so "
            "their order cannot be recovered from their names: the record is the only "
            "authority for which revision is newer."
        ),
    )
    state: Literal["Installing", "Installed", "Removed"] = Field(
        default="Installing", description="How far the local state has progressed."
    )
    owner: str = Field(description="The installer instance that last wrote this record.")
    updated_at: str = Field(description="When the record was last written, RFC 3339.")


class CleanupIntent(BaseModel):
    """An authoritative, persisted request to remove a placement's workload."""

    model_config = ConfigDict(extra="forbid")

    placement: str = Field(description="The placement identity.")
    pod_name: str = Field(description="The static Pod's name.")
    static_set: str = Field(description="The owning StaticSet's name.")
    static_set_uid: str = Field(description="The owning StaticSet's UID.")
    namespace: str = Field(description="The StaticSet's namespace.")
    node: str = Field(description="The node the workload is on.")
    generation: int = Field(description="The generation the intent was raised for.")
    reason: str = Field(description="Why the placement is being cleaned up.")
    remove_configuration: bool = Field(
        default=True, description="Whether configuration files may be deleted after acknowledgment."
    )
    requested_at: str = Field(description="When the intent was persisted, RFC 3339.")


@dataclass(frozen=True)
class PlacementLayout:
    """The fixed set of host paths one placement may touch.

    Attributes:
        bundle_dir: The persistent bundle directory.
        static_pod_path: kubelet's watched static Pod directory.
        identity: The placement identity.
        pod_name: The static Pod's name, which doubles as the manifest file stem.
    """

    bundle_dir: str
    static_pod_path: str
    identity: str
    pod_name: str

    @property
    def placement_dir(self) -> str:
        """The directory holding everything this placement persists."""
        return f"{self.bundle_dir.rstrip('/')}/placements/{self.identity}"

    @property
    def revisions_dir(self) -> str:
        """The directory holding the placement's revision directories."""
        return f"{self.placement_dir}/revisions"

    @property
    def record_path(self) -> str:
        """The ownership record's path."""
        return f"{self.placement_dir}/ownership.json"

    @property
    def intent_path(self) -> str:
        """The cleanup intent's path."""
        return f"{self.placement_dir}/cleanup-intent.json"

    @property
    def manifest_path(self) -> str:
        """The manifest's path inside kubelet's watched directory."""
        return f"{self.static_pod_path.rstrip('/')}/{manifest_filename(self.pod_name)}"

    def revision_dir(self, revision: str) -> str:
        """Return the directory that holds one revision's content.

        Args:
            revision: The revision token.

        Returns:
            The absolute revision directory.

        Raises:
            InstallationError: If the token is not a well-formed revision, which
                would otherwise let a caller inject a path.
        """
        if not is_revision(revision):
            raise InstallationError(f"{revision!r} is not a well-formed revision token")
        return f"{self.revisions_dir}/{revision}"


@dataclass(frozen=True)
class InstallResult:
    """What an installation attempt did.

    Attributes:
        revision: The revision that is now active.
        manifest_path: The manifest that kubelet reads.
        changed: Whether the manifest was rewritten.
        warnings: Disclosures the controller should report in status.
        retained: The revision directories that were kept.
    """

    revision: str
    manifest_path: str
    changed: bool
    warnings: tuple[str, ...]
    retained: tuple[str, ...]


@dataclass(frozen=True)
class CleanupOutcome:
    """What a cleanup attempt did.

    Attributes:
        state: Where the cleanup stands.
        acknowledged: Whether the local removal is confirmed complete.
        configuration_removed: Whether the revision directories were deleted.
        message: A human-readable explanation for status.
    """

    state: Literal["Acknowledged", "Blocked"]
    acknowledged: bool
    configuration_removed: bool
    message: str


def read_record(layout: PlacementLayout) -> OwnershipRecord | None:
    """Read the ownership record of a placement.

    Args:
        layout: The placement's layout.

    Returns:
        The record, or ``None`` when the placement has never been installed.

    Raises:
        InstallationError: If the record exists but cannot be parsed.
    """
    path = Path(layout.record_path)
    if not path.is_file():
        return None
    try:
        return OwnershipRecord.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise InstallationError(f"ownership record {path} is unreadable: {error}") from error


def read_intent(layout: PlacementLayout) -> CleanupIntent | None:
    """Read a placement's cleanup intent.

    Args:
        layout: The placement's layout.

    Returns:
        The intent, or ``None`` when no cleanup was requested.

    Raises:
        InstallationError: If the intent exists but cannot be parsed.
    """
    path = Path(layout.intent_path)
    if not path.is_file():
        return None
    try:
        return CleanupIntent.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise InstallationError(f"cleanup intent {path} is unreadable: {error}") from error


def write_intent(layout: PlacementLayout, intent: CleanupIntent) -> None:
    """Persist a cleanup intent durably, before anybody acts on it.

    The intent is written by whoever learned of it from the API — the installer
    that still serves the node, or a cleanup worker — so that the decision survives
    a crash between learning of it and completing it.

    Args:
        layout: The placement's layout.
        intent: The intent to persist.
    """
    payload = intent.model_dump_json(indent=2).encode("utf-8") + b"\n"
    atomic_write(Path(layout.intent_path), payload, mode=RECORD_MODE, directory_mode=DIRECTORY_MODE)


def verify_owner(layout: PlacementLayout, owner: str) -> OwnershipRecord:
    """Assert that this process is the placement's current owner.

    Call this before any destructive operation. A stale installer that was replaced
    on the same node fails this check and therefore cannot delete the successor's
    workload.

    Args:
        layout: The placement's layout.
        owner: The calling installer or cleanup worker's instance identity.

    Returns:
        The ownership record.

    Raises:
        OwnershipError: If there is no record, or the record belongs to somebody
            else.
    """
    record = read_record(layout)
    if record is None:
        raise OwnershipError(f"placement {layout.identity} has no ownership record")
    if record.owner != owner:
        raise OwnershipError(
            f"placement {layout.identity} is owned by {record.owner!r}, not by {owner!r}; "
            "refusing to modify it"
        )
    return record


def claim_placement(
    layout: PlacementLayout,
    *,
    static_set: str,
    static_set_uid: str,
    namespace: str,
    node: str,
    generation: int,
    owner: str,
    now: str,
) -> OwnershipRecord:
    """Take ownership of a placement's local state, handed over or fresh.

    Handover is generation-aware: a successor may adopt a placement recorded for an
    older or equal generation, which is what lets an installer replace one that
    disappeared on the same node. A record from a *newer* generation is authoritative
    and is never overwritten, so a resurrected stale Pod cannot reclaim a placement
    that has already moved on.

    Args:
        layout: The placement's layout.
        static_set: The owning StaticSet's name.
        static_set_uid: The owning StaticSet's UID.
        namespace: The StaticSet's namespace.
        node: The node name.
        generation: The generation the current reconcile is for.
        owner: The calling installer's instance identity.
        now: The current time, RFC 3339.

    Returns:
        The record now in force.

    Raises:
        OwnershipError: If the existing record belongs to another StaticSet or to a
            newer generation.
    """
    existing = read_record(layout)
    if existing is not None:
        if existing.static_set_uid != static_set_uid:
            raise OwnershipError(
                f"placement {layout.identity} belongs to StaticSet uid "
                f"{existing.static_set_uid}, not to {static_set_uid}"
            )
        if existing.generation > generation:
            raise OwnershipError(
                f"placement {layout.identity} was taken over at generation "
                f"{existing.generation}, which is newer than {generation}"
            )
    record = OwnershipRecord(
        placement=layout.identity,
        pod_name=layout.pod_name,
        static_set=static_set,
        static_set_uid=static_set_uid,
        namespace=namespace,
        node=node,
        generation=generation,
        revision=existing.revision if existing is not None else None,
        history=list(existing.history) if existing is not None else [],
        state="Installing",
        owner=owner,
        updated_at=now,
    )
    _write_record(layout, record)
    return record


def install_revision(
    layout: PlacementLayout,
    compiled: CompiledWorkload,
    *,
    namespace: str,
    generation: int,
    owner: str,
    now: str,
    retained_revisions: int,
    labels: dict[str, str],
    annotations: dict[str, str],
) -> InstallResult:
    """Install a compiled revision durably and make it the active manifest.

    The configuration is materialized and flushed before the manifest is published,
    so a crash can leave an unused revision directory but never a manifest whose
    mounts do not exist. An error raised anywhere before the manifest swap leaves
    the previously active revision intact.

    Args:
        layout: The placement's layout.
        compiled: The compiled workload.
        namespace: The namespace the mirror Pod should appear in.
        generation: The generation this revision was computed for.
        owner: The calling installer's instance identity.
        now: The current time, RFC 3339.
        retained_revisions: How many revision directories to keep, newest first.
        labels: Provenance labels for the static Pod.
        annotations: Provenance annotations for the static Pod.

    Returns:
        The installation result.

    Raises:
        OwnershipError: If this process is not the placement's owner.
        InstallationError: If a revision token is malformed or a path is unsafe.
        PathSecurityError: If a path fails validation.
    """
    record = verify_owner(layout, owner)
    revision = compiled.revision
    revision_dir = layout.revision_dir(revision)

    _ensure_directory(Path(layout.placement_dir))
    _ensure_directory(Path(layout.revisions_dir))
    _ensure_directory(Path(revision_dir))
    for relative, content in compiled.files.items():
        mode = compiled.modes[relative]
        _write_revision_file(revision_dir, relative, content, mode)

    manifest = render_manifest(
        compiled,
        pod_name=layout.pod_name,
        namespace=namespace,
        revision_dir=revision_dir,
        labels=labels,
        annotations=annotations,
    )
    document = json.dumps(manifest, indent=2, sort_keys=True).encode("utf-8") + b"\n"

    # The manifest is stored inside the revision directory too, so that the exact
    # content that produced the active revision survives independently of kubelet's
    # watched directory.
    atomic_write(Path(revision_dir) / _ACTIVE_MANIFEST, document, mode=MANIFEST_MODE)

    new_history = [revision, *(item for item in record.history if item != revision)]
    # The manifest is rewritten whenever the on-disk copy is not exactly what this
    # revision renders, so a manifest edited or truncated on the node is repaired on
    # the next reconcile instead of being assumed to be ours.
    changed = record.revision != revision or _read_manifest(layout) != document
    if changed:
        atomic_write(Path(layout.manifest_path), document, mode=MANIFEST_MODE)

    _write_record(
        layout,
        record.model_copy(
            update={
                "revision": revision,
                "history": new_history,
                "state": "Installed",
                "updated_at": now,
            }
        ),
    )
    retained = prune_revisions(
        layout, keep=retained_revisions, active=revision, history=new_history
    )
    return InstallResult(
        revision=revision,
        manifest_path=layout.manifest_path,
        changed=changed,
        warnings=compiled.warnings,
        retained=retained,
    )


def prune_revisions(
    layout: PlacementLayout,
    *,
    keep: int,
    active: str | None,
    history: Sequence[str] = (),
) -> tuple[str, ...]:
    """Remove old revision directories, keeping a bounded number of them.

    Revisions are content hashes, so their *names* carry no ordering. The order
    therefore comes from the ownership record's history, which the installer
    updates on every install; directories that are not in the history at all are
    leftovers from an interrupted run and are removed first.

    The active revision is never removed, and neither is any revision a pending
    cleanup still has to reason about, so the operator cannot delete the workload it
    is currently serving.

    Args:
        layout: The placement's layout.
        keep: How many revisions to keep, newest first.
        active: The active revision, which is always kept.
        history: The installed revisions, newest first.

    Returns:
        The revisions that remain.

    Raises:
        InstallationError: If the revisions directory cannot be listed.
    """
    directory = Path(layout.revisions_dir)
    if not directory.is_dir():
        return ()
    present = {
        entry.name for entry in directory.iterdir() if entry.is_dir() and is_revision(entry.name)
    }
    ordered = [revision for revision in history if revision in present]
    ordered.extend(sorted(present - set(ordered)))
    protected = {active} if active else set()
    surviving: list[str] = []
    for revision in ordered:
        if revision in protected or len(surviving) < keep:
            surviving.append(revision)
            continue
        remove_tree_within(directory, directory / revision)
    return tuple(surviving)


def remove_workload(
    layout: PlacementLayout,
    *,
    intent: CleanupIntent,
    terminated: bool,
) -> CleanupOutcome:
    """Remove a placement's static workload, idempotently.

    The operation is safe to retry after any crash: removing an already-absent
    manifest is not an error, and the intent and record are simply re-persisted.

    Configuration files are only deleted once the workload is confirmed terminated
    and the intent allows it, because deleting the files a still-running workload
    mounts would break it.

    Args:
        layout: The placement's layout.
        intent: The authoritative cleanup intent.
        terminated: Whether the workload is confirmed to have stopped.

    Returns:
        The outcome of the attempt.

    Raises:
        OwnershipError: If the intent does not match the local record, which means
            the placement has already been taken over by a newer StaticSet or
            generation.
    """
    if intent.placement != layout.identity:
        raise OwnershipError(
            f"intent for placement {intent.placement} cannot be applied to {layout.identity}"
        )
    record = read_record(layout)
    if record is None:
        return CleanupOutcome(
            state="Acknowledged",
            acknowledged=True,
            configuration_removed=False,
            message="no local record; the placement was never installed on this node",
        )
    if record.static_set_uid != intent.static_set_uid:
        raise OwnershipError(
            f"placement {layout.identity} belongs to StaticSet uid {record.static_set_uid}, "
            f"but the cleanup intent names {intent.static_set_uid}"
        )
    if record.generation > intent.generation:
        raise OwnershipError(
            f"placement {layout.identity} has moved on to generation {record.generation}; "
            f"the cleanup intent is for generation {intent.generation}"
        )

    _remove_manifest(layout)
    removed_configuration = False
    if intent.remove_configuration and terminated:
        revisions = Path(layout.revisions_dir)
        if revisions.is_dir():
            remove_tree_within(Path(layout.placement_dir), revisions)
            removed_configuration = True

    _rmdir_if_empty(Path(layout.revisions_dir))
    _write_record(
        layout,
        record.model_copy(
            update={
                "revision": None,
                "history": [],
                "state": "Removed",
                "updated_at": intent.requested_at,
            }
        ),
    )
    if removed_configuration and terminated:
        return CleanupOutcome(
            state="Acknowledged",
            acknowledged=True,
            configuration_removed=True,
            message="manifest and configuration removed",
        )
    return CleanupOutcome(
        state="Acknowledged" if terminated else "Blocked",
        acknowledged=terminated,
        configuration_removed=False,
        message=(
            "manifest removed; configuration retained until the workload's termination is confirmed"
            if not terminated
            else "manifest removed; configuration retained by policy"
        ),
    )


def _remove_manifest(layout: PlacementLayout) -> None:
    """Remove the active manifest, tolerating its absence.

    Args:
        layout: The placement's layout.

    Raises:
        PathSecurityError: If the manifest is not inside kubelet's static Pod path,
            or is a symbolic link.
    """
    manifest = Path(layout.manifest_path)
    assert_within(Path(layout.static_pod_path), manifest)
    if manifest.is_symlink():
        raise PathSecurityError(f"{manifest} is a symbolic link; refusing to remove it")
    if manifest.exists():
        manifest.unlink()
        _fsync(manifest.parent)


def _write_revision_file(revision_dir: str, relative: str, content: bytes, mode: int) -> None:
    """Write one materialized configuration file.

    Args:
        revision_dir: The absolute revision directory.
        relative: The bundle-relative path of the file.
        content: The content to write.
        mode: The file mode.

    Raises:
        PathSecurityError: If the path escapes the revision directory.
        InstallationError: If the file cannot be written.
    """
    target = Path(revision_dir) / relative
    assert_within(Path(revision_dir), target)
    target.parent.mkdir(parents=True, exist_ok=True, mode=DIRECTORY_MODE)
    atomic_write(target, content, mode=mode, directory_mode=DIRECTORY_MODE)


def _read_manifest(layout: PlacementLayout) -> bytes | None:
    """Return the manifest currently on disk, if there is one.

    Args:
        layout: The placement's layout.

    Returns:
        The file's bytes, or ``None`` when it is absent or unreadable.

    Raises:
        PathSecurityError: If the manifest is a symbolic link.
    """
    manifest = Path(layout.manifest_path)
    if manifest.is_symlink():
        raise PathSecurityError(f"{manifest} is a symbolic link; refusing to read it")
    try:
        return manifest.read_bytes()
    except OSError:
        return None


def _write_record(layout: PlacementLayout, record: OwnershipRecord) -> None:
    """Persist an ownership record durably.

    Args:
        layout: The placement's layout.
        record: The record to write.
    """
    payload = record.model_dump_json(indent=2).encode("utf-8") + b"\n"
    atomic_write(Path(layout.record_path), payload, mode=RECORD_MODE, directory_mode=DIRECTORY_MODE)


def _ensure_directory(path: Path) -> None:
    """Create a directory with restrictive permissions.

    Args:
        path: The directory to create.
    """
    path.mkdir(parents=True, exist_ok=True, mode=DIRECTORY_MODE)


def _rmdir_if_empty(path: Path) -> None:
    """Remove a directory when it is empty, ignoring absence.

    Args:
        path: The directory to remove.
    """
    try:
        path.rmdir()
    except OSError:
        return


def _fsync(path: Path) -> None:
    """Flush a directory so that an unlink survives a crash.

    Args:
        path: The directory to flush.
    """
    directory_flag = int(getattr(os, "O_DIRECTORY", 0))
    if not directory_flag:  # pragma: no cover - Windows
        return
    descriptor = os.open(path, os.O_RDONLY | directory_flag)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
