"""Deterministic revision identity for a compiled static workload.

A revision names a *content*: the compiled static Pod plus every configuration
file it needs. Two compilations of the same inputs must produce the same revision,
and any change to a ConfigMap, a Secret, a field of the template or a policy that
affects the emitted manifest must produce a different one. Nothing that varies per
node, per installer or per wall-clock time may enter the hash, or a rollout would
never converge.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from typing import Any

__all__ = [
    "REVISION_PREFIX",
    "compute_revision",
    "is_revision",
]

REVISION_PREFIX = "r-"
"""Prefix distinguishing a revision token from any other identifier."""

_REVISION_PATTERN = re.compile(r"^r-[0-9a-f]{32}$")
"""A revision is 128 bits of SHA-256, prefixed to make it self-describing."""


def compute_revision(
    pod: Mapping[str, Any],
    files: Mapping[str, bytes],
    modes: Mapping[str, int],
) -> str:
    """Return the deterministic revision of a compiled workload.

    Args:
        pod: The compiled static Pod with *relative* configuration mount paths, so
            that the hash cannot depend on where the bundle happens to be mounted.
        files: Configuration files by bundle-relative path.
        modes: The file mode of each path in ``files``.

    Returns:
        The revision token.

    Raises:
        KeyError: If ``modes`` names a path that is not in ``files``.
    """
    digest = hashlib.sha256()
    digest.update(_canonical(pod))
    for path in sorted(files):
        content = files[path]
        mode = modes[path]
        digest.update(b"\x00")
        digest.update(path.encode("utf-8"))
        digest.update(b"\x00")
        digest.update(f"{mode:o}".encode("ascii"))
        digest.update(b"\x00")
        digest.update(hashlib.sha256(content).digest())
    return f"{REVISION_PREFIX}{digest.hexdigest()[:32]}"


def _canonical(pod: Mapping[str, Any]) -> bytes:
    """Serialize a mapping so that equal content always yields equal bytes.

    Args:
        pod: The compiled static Pod.

    Returns:
        Canonical JSON, with sorted keys and no insignificant whitespace.
    """
    return json.dumps(pod, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")


def is_revision(value: str) -> bool:
    """Return whether a string is a well-formed revision token.

    Args:
        value: The candidate token.

    Returns:
        ``True`` when the token has the revision prefix and digest shape.
    """
    return bool(_REVISION_PATTERN.match(value))
