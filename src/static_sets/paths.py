"""Path validation and durable file primitives for the local installer.

Everything the installer writes lives beneath two directories it was told about:
kubelet's static Pod path and the persistent bundle directory. This module is the
single place that decides whether a path is allowed, so that traversal, absolute
mount paths and symlinked directories are refused consistently rather than being
re-checked (and forgotten) at each call site.
"""

from __future__ import annotations

import os
from pathlib import Path, PurePosixPath
from typing import Literal

from static_sets.errors import PathSecurityError

__all__ = [
    "assert_owned_by_current_user",
    "assert_within",
    "atomic_write",
    "is_within",
    "join_within",
    "normalize_absolute",
    "nsync_rename",
    "remove_tree_within",
    "validate_relative",
]

_WRITE_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_TRUNC

# ``O_NOFOLLOW`` and ``O_DIRECTORY`` do not exist on Windows, and a static type
# checker on Windows cannot know them at all. They are read dynamically and
# default to 0, which is the documented "feature unavailable" behaviour.
_NO_FOLLOW: int = int(getattr(os, "O_NOFOLLOW", 0))
_DIRECTORY: int = int(getattr(os, "O_DIRECTORY", 0))
_GET_EUID = getattr(os, "geteuid", None)

_WRITE_FLAGS |= _NO_FOLLOW


def normalize_absolute(path: str) -> str:
    """Return a lexically normalized absolute POSIX path.

    ``..`` segments are resolved and duplicate separators collapsed, so that a
    comparison of two normalized paths is a real containment test rather than a
    string-prefix guess. The function does not touch the filesystem.

    Args:
        path: An absolute POSIX path.

    Returns:
        The normalized path, still absolute.

    Raises:
        PathSecurityError: If the path is not absolute or normalizes to a
            traversal above the root.
    """
    pure = PurePosixPath(path)
    if not pure.is_absolute():
        raise PathSecurityError(f"{path!r} is not an absolute path")
    parts: list[str] = []
    for part in pure.parts[1:]:
        if part == "..":
            if not parts:
                raise PathSecurityError(f"{path!r} escapes the filesystem root")
            parts.pop()
        elif part not in ("", "."):
            parts.append(part)
    return "/" + "/".join(parts)


def is_within(child: str, parent: str) -> bool:
    """Return whether one normalized absolute path lies inside another.

    This is a *lexical* containment test for paths that come from Kubernetes, such
    as a declared ``staticPodPath`` or a volume's mount path. It never touches the
    filesystem; use :func:`assert_within` before mutating a real path.

    Args:
        child: The candidate path.
        parent: The containing directory.

    Returns:
        ``True`` when ``child`` equals ``parent`` or is a descendant of it.
    """
    child_normalized = normalize_absolute(child)
    parent_normalized = normalize_absolute(parent)
    return child_normalized == parent_normalized or child_normalized.startswith(
        f"{parent_normalized}/"
    )


def assert_within(root: Path, target: Path, *, allow_equal: bool = False) -> None:
    """Assert that a real filesystem path lies inside a root directory.

    Unlike :func:`is_within`, this works on the paths the host actually has, so it
    behaves the same on a Linux node and on a developer's machine.

    Args:
        root: The directory the path must stay inside.
        target: The path being checked.
        allow_equal: Whether ``target`` may be ``root`` itself.

    Raises:
        PathSecurityError: If the path is outside, or is the root when that is not
            allowed.
    """
    if target == root:
        if allow_equal:
            return
        raise PathSecurityError(f"{target} is the root itself; refusing to treat it as contained")
    if not target.is_relative_to(root):
        raise PathSecurityError(f"{target} is not inside {root}")


def validate_relative(path: str) -> str:
    """Validate a bundle-relative path and return it normalized.

    Args:
        path: A path relative to the placement's revision directory.

    Returns:
        The normalized relative path.

    Raises:
        PathSecurityError: If the path is absolute, empty after normalization or
            contains a ``..`` segment.
    """
    pure = PurePosixPath(path)
    if pure.is_absolute():
        raise PathSecurityError(f"{path!r} must be relative to the revision directory")
    parts = [part for part in pure.parts if part not in ("", ".")]
    if not parts or any(part == ".." for part in parts):
        raise PathSecurityError(f"{path!r} is not a safe relative path")
    return "/".join(parts)


def join_within(base: str, *parts: str) -> str:
    """Join bundle-relative parts to a base directory, refusing traversal.

    Args:
        base: The absolute directory the result must stay inside.
        *parts: Relative path components.

    Returns:
        The absolute, validated path.

    Raises:
        PathSecurityError: If the result would escape ``base``.
    """
    relative = validate_relative("/".join(parts)) if parts else "."
    candidate = base if relative == "." else f"{normalize_absolute(base)}/{relative}"
    if not is_within(candidate, base):
        raise PathSecurityError(f"{candidate!r} escapes {base!r}")
    return normalize_absolute(candidate)


def assert_no_symlink(path: Path) -> None:
    """Refuse to operate on a path whose components include a symbolic link.

    The installer only creates regular directories and files, so a symlink anywhere
    on the way to a file it is about to write — the file itself included — means
    somebody else arranged for it to write somewhere unexpected, or is pointing at a
    file the installer does not own. Both are refusals: the protocol only touches its
    own files.

    Args:
        path: The path to inspect.

    Raises:
        PathSecurityError: If any component of ``path`` is a symbolic link.
    """
    for candidate in [*path.parents][::-1] + [path]:
        if candidate.is_symlink():
            raise PathSecurityError(f"{candidate} is a symbolic link; refusing to write")


def atomic_write(
    path: Path,
    data: bytes,
    *,
    mode: int = 0o600,
    directory_mode: int = 0o700,
) -> None:
    """Write a file durably, replacing any previous content atomically.

    The bytes are written to a temporary file in the same directory, flushed and
    ``fsync``-ed, and then renamed onto the target. A crash therefore leaves either
    the old file or the new one, never a mixture, which is what makes a manifest
    swap safe to interrupt.

    Args:
        path: The absolute file path to write.
        data: The content to write.
        mode: The file mode to apply to the temporary file before the rename.
        directory_mode: The mode to apply to a directory this call creates.

    Raises:
        PathSecurityError: If a component of ``path`` is a symbolic link.
    """
    path.parent.mkdir(parents=True, exist_ok=True, mode=directory_mode)
    assert_no_symlink(path)
    temporary = path.with_name(f".{path.name}.tmp")
    descriptor = os.open(temporary, _WRITE_FLAGS, mode)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    os.chmod(temporary, mode)
    nsync_rename(temporary, path)
    _fsync_directory(path.parent)


def nsync_rename(source: Path, target: Path) -> None:
    """Rename ``source`` onto ``target``.

    Args:
        source: The temporary file.
        target: The final path.
    """
    os.replace(source, target)


def _fsync_directory(directory: Path) -> None:
    """Flush a directory entry so that a rename survives a crash.

    Args:
        directory: The directory to flush. On platforms without ``O_DIRECTORY``
            the flush is skipped, because the rename itself is still atomic.
    """
    if not _DIRECTORY:  # pragma: no cover - Windows
        return
    descriptor = os.open(directory, os.O_RDONLY | _DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def remove_tree_within(root: Path, target: Path, *, keep: Literal["none"] = "none") -> None:
    """Remove a directory tree, but only beneath a known root.

    Args:
        root: The absolute directory the target must stay inside.
        target: The directory to remove.
        keep: Reserved; the parameter documents that no child is exempted.

    Raises:
        PathSecurityError: If ``target`` is not strictly inside ``root``, or is a
            symbolic link.
    """
    del keep
    resolved_root = Path(root)
    resolved_target = Path(target)
    if resolved_target == resolved_root:
        raise PathSecurityError(f"{target} is the root itself; refusing to remove it")
    if resolved_target.is_symlink():
        raise PathSecurityError(f"{target} is a symbolic link; refusing to remove it")
    assert_within(resolved_root, resolved_target)
    if resolved_target.is_dir():
        for child in sorted(resolved_target.rglob("*"), reverse=True):
            if child.is_symlink():
                raise PathSecurityError(f"{child} is a symbolic link; refusing to remove it")
        _rmtree(resolved_target)
    else:
        resolved_target.unlink(missing_ok=True)


def _rmtree(path: Path) -> None:
    """Remove a directory tree without following symbolic links.

    Args:
        path: The directory to remove, already validated as symlink-free.
    """
    import shutil

    shutil.rmtree(path)


def assert_owned_by_current_user(path: Path) -> None:
    """Refuse to act on a file that belongs to somebody else.

    A shared bundle directory is the only writable host path the installer has, so
    a file owned by another user is either a mistake or an attack.

    Args:
        path: The file or directory to check.

    Raises:
        PathSecurityError: If the path is owned by another user.
    """
    if _GET_EUID is None:  # pragma: no cover - Windows
        return
    if not path.exists():
        return
    owner = path.stat().st_uid
    if owner != _GET_EUID():
        raise PathSecurityError(f"{path} is owned by uid {owner}, not by this process")
