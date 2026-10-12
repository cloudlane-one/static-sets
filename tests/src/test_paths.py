"""Path validation and durable write primitives."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from static_sets.errors import PathSecurityError
from static_sets.paths import (
    atomic_write,
    is_within,
    join_within,
    normalize_absolute,
    remove_tree_within,
    validate_relative,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("/a/b", "/a/b"),
        ("/a//b/", "/a/b"),
        ("/a/./b", "/a/b"),
        ("/a/b/../c", "/a/c"),
        ("//a", "/a"),
    ],
)
def test_normalize_absolute_collapses_redundant_segments(raw: str, expected: str) -> None:
    """Normalization removes duplicate separators and resolves ``..`` lexically."""
    assert normalize_absolute(raw) == expected


@pytest.mark.parametrize("raw", ["relative/path", "", "a/b"])
def test_normalize_absolute_rejects_relative_paths(raw: str) -> None:
    """A relative path cannot be normalized into an absolute one."""
    with pytest.raises(PathSecurityError):
        normalize_absolute(raw)


def test_normalize_absolute_rejects_escaping_root() -> None:
    """Climbing above the root is refused rather than clamped."""
    with pytest.raises(PathSecurityError):
        normalize_absolute("/../etc")


def test_is_within_is_a_real_containment_test() -> None:
    """A sibling directory with a shared prefix is not considered contained."""
    assert is_within("/var/lib/static-sets/x", "/var/lib/static-sets")
    assert is_within("/var/lib/static-sets", "/var/lib/static-sets")
    assert not is_within("/var/lib/static-sets-other", "/var/lib/static-sets")
    assert not is_within("/var/lib", "/var/lib/static-sets")


@pytest.mark.parametrize(
    "relative",
    ["/absolute", "..", "a/../../b", "", "./.."],
)
def test_validate_relative_rejects_unsafe_paths(relative: str) -> None:
    """Absolute, empty and traversing relative paths are refused."""
    with pytest.raises(PathSecurityError):
        validate_relative(relative)


def test_join_within_stays_inside_the_base() -> None:
    """Joining safe components yields a path under the base."""
    assert join_within("/base", "a", "b") == "/base/a/b"
    assert join_within("/base") == "/base"


def test_join_within_refuses_escape() -> None:
    """A traversing component is refused."""
    with pytest.raises(PathSecurityError):
        join_within("/base", "../outside")


def test_atomic_write_replaces_content_and_sets_mode(tmp_path: Path) -> None:
    """A write replaces the file and applies the requested mode."""
    target = tmp_path / "dir" / "file.txt"

    atomic_write(target, b"one", mode=0o640)
    atomic_write(target, b"two", mode=0o640)

    assert target.read_bytes() == b"two"
    if os.name == "posix":
        assert target.stat().st_mode & 0o777 == 0o640
    assert not list(tmp_path.glob("dir/.*.tmp"))


def test_atomic_write_refuses_a_symlinked_target(tmp_path: Path) -> None:
    """A symlink where a file is expected is refused, not followed."""
    outside = tmp_path / "outside.txt"
    outside.write_text("safe", encoding="utf-8")
    link = tmp_path / "link.txt"
    try:
        link.symlink_to(outside)
    except (OSError, NotImplementedError):  # pragma: no cover - Windows without privileges
        pytest.skip("symlinks are not available in this environment")

    with pytest.raises(PathSecurityError):
        atomic_write(link, b"clobbered", mode=0o600)

    assert outside.read_text(encoding="utf-8") == "safe"


def test_remove_tree_within_deletes_only_inside_the_root(tmp_path: Path) -> None:
    """A tree inside the root is removed."""
    root = tmp_path / "root"
    target = root / "a" / "b"
    target.mkdir(parents=True)
    (target / "file").write_text("x", encoding="utf-8")

    remove_tree_within(root, root / "a")

    assert not (root / "a").exists()
    assert root.exists()


def test_remove_tree_within_refuses_the_root_itself(tmp_path: Path) -> None:
    """The root is never removed by a call that targets it."""
    root = tmp_path / "root"
    root.mkdir()

    with pytest.raises(PathSecurityError):
        remove_tree_within(root, root)


def test_remove_tree_within_refuses_an_outside_target(tmp_path: Path) -> None:
    """A target outside the root is refused."""
    root = tmp_path / "root"
    other = tmp_path / "other"
    root.mkdir()
    other.mkdir()

    with pytest.raises(PathSecurityError):
        remove_tree_within(root, other)
