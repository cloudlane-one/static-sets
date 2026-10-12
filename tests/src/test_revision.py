"""Deterministic revision identity."""

from __future__ import annotations

import pytest

from static_sets.revision import REVISION_PREFIX, compute_revision, is_revision

POD = {"apiVersion": "v1", "kind": "Pod", "spec": {"containers": [{"name": "a"}]}}


def test_same_content_gives_the_same_revision() -> None:
    """The revision is a pure function of the content."""
    files = {"config/x": b"one", "secrets/y": b"two"}
    modes = {"config/x": 0o644, "secrets/y": 0o400}

    assert compute_revision(POD, files, modes) == compute_revision(POD, files, modes)


def test_key_order_does_not_change_the_revision() -> None:
    """Mapping order is irrelevant, so a re-ordered reconcile is not a rollout."""
    first = compute_revision(
        {"b": 1, "a": {"d": 4, "c": 3}}, {"z": b"1", "a": b"2"}, {"z": 0o400, "a": 0o400}
    )
    second = compute_revision(
        {"a": {"c": 3, "d": 4}, "b": 1}, {"a": b"2", "z": b"1"}, {"a": 0o400, "z": 0o400}
    )

    assert first == second


def test_content_change_changes_the_revision() -> None:
    """A changed ConfigMap value produces a new revision."""
    modes = {"config/x": 0o644}

    assert compute_revision(POD, {"config/x": b"one"}, modes) != compute_revision(
        POD, {"config/x": b"two"}, modes
    )


def test_added_file_changes_the_revision() -> None:
    """A new key in a watched ConfigMap produces a new revision."""
    modes = {"config/x": 0o644, "config/y": 0o644}

    assert compute_revision(POD, {"config/x": b"one"}, {"config/x": 0o644}) != compute_revision(
        POD, {"config/x": b"one", "config/y": b"two"}, modes
    )


def test_mode_change_changes_the_revision() -> None:
    """A changed file mode is a different revision, because it changes the Pod."""
    files = {"secrets/y": b"two"}

    assert compute_revision(POD, files, {"secrets/y": 0o400}) != compute_revision(
        POD, files, {"secrets/y": 0o444}
    )


def test_pod_change_changes_the_revision() -> None:
    """A changed Pod field produces a new revision."""
    modes = {"config/x": 0o644}
    other = {"apiVersion": "v1", "kind": "Pod", "spec": {"containers": [{"name": "b"}]}}

    assert compute_revision(POD, {"config/x": b"one"}, modes) != compute_revision(
        other, {"config/x": b"one"}, modes
    )


def test_missing_mode_is_an_error() -> None:
    """Every file must declare its mode, so the revision cannot omit it."""
    with pytest.raises(KeyError):
        compute_revision(POD, {"config/x": b"one"}, {})


def test_revision_token_shape() -> None:
    """A revision is prefixed and 128 bits long."""
    revision = compute_revision(POD, {}, {})

    assert revision.startswith(REVISION_PREFIX)
    assert len(revision) == len(REVISION_PREFIX) + 32
    assert is_revision(revision)


@pytest.mark.parametrize("value", ["", "r-", "r-zz", "deadbeef", "r-" + "0" * 31])
def test_malformed_revisions_are_rejected(value: str) -> None:
    """Anything that is not a well-formed token is refused."""
    assert not is_revision(value)
