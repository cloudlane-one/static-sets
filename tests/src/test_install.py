"""Durable installation, ownership handover and cleanup."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from static_sets.errors import InstallationError, OwnershipError, PathSecurityError
from static_sets.install import (
    CleanupIntent,
    PlacementLayout,
    claim_placement,
    install_revision,
    read_intent,
    read_record,
    remove_workload,
    verify_owner,
    write_intent,
)
from static_sets.translation import ResolvedConfig, compile_workload

CONFIGMAP = {"web-conf": {"app.conf": b"greeting=hello\n"}}
NOW = "2026-10-12T10:00:00Z"
UID = "0f9a4b6c-1111-2222-3333-444455556666"
IDENTITY = "abcdef0123"


def layout(temp_roots: tuple[Path, Path], *, identity: str = IDENTITY) -> PlacementLayout:
    """Return a placement layout rooted in the test's temporary directories.

    Args:
        temp_roots: The (manifests, bundle) pair from the fixture.
        identity: The placement identity.

    Returns:
        The layout.
    """
    manifests, bundle = temp_roots
    return PlacementLayout(
        bundle_dir=str(bundle),
        static_pod_path=str(manifests),
        identity=identity,
        pod_name=f"web-{identity}",
    )


def claim(target: PlacementLayout, *, owner: str = "installer-1", generation: int = 3) -> None:
    """Claim a placement for an installer instance.

    Args:
        target: The placement layout.
        owner: The installer instance identity.
        generation: The generation to claim at.
    """
    claim_placement(
        target,
        static_set="web",
        static_set_uid=UID,
        namespace="edge",
        node="node-a",
        generation=generation,
        owner=owner,
        now=NOW,
    )


def install(
    target: PlacementLayout,
    make_static_set: Any,
    *,
    owner: str = "installer-1",
    resolved: ResolvedConfig | None = None,
    retained: int = 2,
    **overrides: Any,
) -> Any:
    """Compile and install the base StaticSet into a layout.

    Args:
        target: The placement layout.
        make_static_set: The fixture factory for validated StaticSets.
        owner: The installer instance identity.
        resolved: The configuration to compile against.
        retained: How many revisions to retain.
        **overrides: Fields to merge into the resource body.

    Returns:
        The installation result.
    """
    spec = make_static_set(**overrides).spec
    compiled = compile_workload(spec, resolved or ResolvedConfig(config_maps=CONFIGMAP))
    return install_revision(
        target,
        compiled,
        namespace="edge",
        generation=3,
        owner=owner,
        now=NOW,
        retained_revisions=retained,
        labels={"staticsets.cloudlane.dev/set": "web"},
        annotations={"staticsets.cloudlane.dev/revision": compiled.revision},
    )


def test_revision_dir_rejects_a_malformed_token(temp_roots: tuple[Path, Path]) -> None:
    """A revision token that is not well formed cannot become a path."""
    with pytest.raises(InstallationError):
        layout(temp_roots).revision_dir("../../etc")


def test_claim_records_the_owner_before_anything_is_written(
    temp_roots: tuple[Path, Path],
) -> None:
    """The ownership record exists before installation, so files stay attributable."""
    target = layout(temp_roots)

    claim(target)

    record = read_record(target)
    assert record is not None
    assert record.owner == "installer-1"
    assert record.state == "Installing"
    assert record.revision is None
    assert record.static_set_uid == UID
    assert read_intent(target) is None


def test_claim_refuses_a_foreign_static_set(temp_roots: tuple[Path, Path]) -> None:
    """A placement belongs to exactly one StaticSet UID."""
    target = layout(temp_roots)
    claim(target)

    with pytest.raises(OwnershipError):
        claim_placement(
            target,
            static_set="web",
            static_set_uid="someone-else",
            namespace="edge",
            node="node-a",
            generation=4,
            owner="installer-2",
            now=NOW,
        )


def test_claim_refuses_to_go_backwards_in_generations(temp_roots: tuple[Path, Path]) -> None:
    """A resurrected older installer cannot reclaim a placement that moved on."""
    target = layout(temp_roots)
    claim(target, generation=5)

    with pytest.raises(OwnershipError) as error:
        claim(target, owner="installer-2", generation=4)

    assert "newer" in str(error.value)


def test_claim_hands_the_placement_over_to_a_successor(temp_roots: tuple[Path, Path]) -> None:
    """A same-generation successor adopts the placement instead of orphaning it."""
    target = layout(temp_roots)
    claim(target)

    claim(target, owner="installer-2")

    record = read_record(target)
    assert record is not None
    assert record.owner == "installer-2"


def test_verify_owner_rejects_a_stale_installer(temp_roots: tuple[Path, Path]) -> None:
    """A replaced installer cannot act on the placement any more."""
    target = layout(temp_roots)
    claim(target)
    claim(target, owner="installer-2")

    with pytest.raises(OwnershipError):
        verify_owner(target, "installer-1")


def test_install_requires_an_ownership_record(
    temp_roots: tuple[Path, Path], make_static_set: Any
) -> None:
    """Installing without claiming first is refused."""
    with pytest.raises(OwnershipError):
        install(layout(temp_roots), make_static_set)


def test_install_writes_a_durable_manifest_under_kubelets_path(
    temp_roots: tuple[Path, Path], make_static_set: Any
) -> None:
    """The manifest lands where kubelet watches, named after the static Pod."""
    target = layout(temp_roots)
    claim(target)

    result = install(target, make_static_set)

    manifest = Path(result.manifest_path)
    assert manifest.name == f"{target.pod_name}.yaml"
    assert manifest.parent == Path(target.static_pod_path)
    document = json.loads(manifest.read_text(encoding="utf-8"))
    assert document["metadata"]["name"] == target.pod_name
    assert document["metadata"]["namespace"] == "edge"
    assert (
        document["metadata"]["annotations"]["staticsets.cloudlane.dev/revision"] == result.revision
    )


def test_install_materializes_configuration_before_publishing(
    temp_roots: tuple[Path, Path], make_static_set: Any
) -> None:
    """The configuration file exists and the manifest points at its revision directory."""
    target = layout(temp_roots)
    claim(target)

    result = install(target, make_static_set)

    revision_dir = Path(target.revision_dir(result.revision))
    assert (revision_dir / "config" / "conf" / "app.conf").read_bytes() == b"greeting=hello\n"
    assert (revision_dir / "manifest.yaml").is_file()
    document = json.loads(Path(result.manifest_path).read_text(encoding="utf-8"))
    host_path = document["spec"]["volumes"][0]["hostPath"]["path"]
    assert Path(host_path) == revision_dir / "config" / "conf"


def test_reinstalling_the_same_revision_reports_no_change(
    temp_roots: tuple[Path, Path], make_static_set: Any
) -> None:
    """A reconcile that changes nothing does not rewrite the manifest."""
    target = layout(temp_roots)
    claim(target)
    first = install(target, make_static_set)
    stamp = Path(first.manifest_path).stat().st_mtime_ns

    second = install(target, make_static_set)

    assert second.changed is False
    assert second.revision == first.revision
    assert Path(second.manifest_path).stat().st_mtime_ns == stamp


def test_a_new_revision_replaces_the_manifest_and_prunes_old_ones(
    temp_roots: tuple[Path, Path], make_static_set: Any
) -> None:
    """Retention keeps the newest revisions and never removes the active one."""
    target = layout(temp_roots)
    claim(target)
    revisions = []
    for content in (b"one", b"two", b"three"):
        revisions.append(
            install(
                target,
                make_static_set,
                resolved=ResolvedConfig(config_maps={"web-conf": {"app.conf": content}}),
                retained=2,
            ).revision
        )

    remaining = sorted(entry.name for entry in Path(target.revisions_dir).iterdir())
    assert revisions[-1] in remaining
    assert len(remaining) == 2
    assert revisions[0] not in remaining


def test_install_refuses_a_stale_owner(temp_roots: tuple[Path, Path], make_static_set: Any) -> None:
    """A replaced installer can neither install nor delete a successor's workload."""
    target = layout(temp_roots)
    claim(target)
    install(target, make_static_set)
    claim(target, owner="installer-2")
    install(target, make_static_set, owner="installer-2")
    manifest = Path(target.manifest_path)
    assert manifest.is_file()

    with pytest.raises(OwnershipError):
        install(target, make_static_set, owner="installer-1")

    assert manifest.is_file()


def test_cleanup_removes_the_manifest_and_acknowledges(
    temp_roots: tuple[Path, Path], make_static_set: Any
) -> None:
    """A cleanup intent removes the manifest, the record and the configuration."""
    target = layout(temp_roots)
    claim(target)
    install(target, make_static_set)
    intent = CleanupIntent(
        placement=IDENTITY,
        pod_name=target.pod_name,
        static_set="web",
        static_set_uid=UID,
        namespace="edge",
        node="node-a",
        generation=3,
        reason="ScaledDown",
        requested_at=NOW,
    )

    outcome = remove_workload(target, intent=intent, terminated=True)

    assert outcome.state == "Acknowledged"
    assert outcome.acknowledged is True
    assert outcome.configuration_removed is True
    assert not Path(target.manifest_path).exists()
    assert not Path(target.revisions_dir).exists()
    record = read_record(target)
    assert record is not None
    assert record.state == "Removed"
    assert record.revision is None


def test_cleanup_keeps_configuration_until_termination_is_confirmed(
    temp_roots: tuple[Path, Path], make_static_set: Any
) -> None:
    """The files a still-running workload mounts are not deleted early."""
    target = layout(temp_roots)
    claim(target)
    result = install(target, make_static_set)
    intent = CleanupIntent(
        placement=IDENTITY,
        pod_name=target.pod_name,
        static_set="web",
        static_set_uid=UID,
        namespace="edge",
        node="node-a",
        generation=3,
        reason="ScaledDown",
        requested_at=NOW,
    )

    outcome = remove_workload(target, intent=intent, terminated=False)

    assert outcome.acknowledged is False
    assert outcome.configuration_removed is False
    assert not Path(target.manifest_path).exists()
    assert Path(target.revision_dir(result.revision)).is_dir()


def test_cleanup_is_idempotent_across_retries(
    temp_roots: tuple[Path, Path], make_static_set: Any
) -> None:
    """Repeating a cleanup after a crash is safe and still acknowledges."""
    target = layout(temp_roots)
    claim(target)
    install(target, make_static_set)
    intent = CleanupIntent(
        placement=IDENTITY,
        pod_name=target.pod_name,
        static_set="web",
        static_set_uid=UID,
        namespace="edge",
        node="node-a",
        generation=3,
        reason="ScaledDown",
        requested_at=NOW,
    )

    first = remove_workload(target, intent=intent, terminated=True)
    second = remove_workload(target, intent=intent, terminated=True)

    assert first.acknowledged and second.acknowledged
    assert not Path(target.manifest_path).exists()


def test_cleanup_on_an_uninstalled_placement_acknowledges_immediately(
    temp_roots: tuple[Path, Path],
) -> None:
    """Nothing to remove is a successful cleanup."""
    target = layout(temp_roots)
    intent = CleanupIntent(
        placement=IDENTITY,
        pod_name=target.pod_name,
        static_set="web",
        static_set_uid=UID,
        namespace="edge",
        node="node-a",
        generation=3,
        reason="ScaledDown",
        requested_at=NOW,
    )

    outcome = remove_workload(target, intent=intent, terminated=True)

    assert outcome.acknowledged is True
    assert "never installed" in outcome.message


def test_cleanup_refuses_a_foreign_static_set(
    temp_roots: tuple[Path, Path], make_static_set: Any
) -> None:
    """An intent for a different StaticSet UID cannot remove this workload."""
    target = layout(temp_roots)
    claim(target)
    install(target, make_static_set)
    intent = CleanupIntent(
        placement=IDENTITY,
        pod_name=target.pod_name,
        static_set="web",
        static_set_uid="other-uid",
        namespace="edge",
        node="node-a",
        generation=3,
        reason="ScaledDown",
        requested_at=NOW,
    )

    with pytest.raises(OwnershipError):
        remove_workload(target, intent=intent, terminated=True)

    assert Path(target.manifest_path).is_file()


def test_cleanup_refuses_an_intent_for_an_older_generation(
    temp_roots: tuple[Path, Path], make_static_set: Any
) -> None:
    """A late intent cannot remove a workload that has moved on."""
    target = layout(temp_roots)
    claim(target, generation=7)
    install(target, make_static_set)
    intent = CleanupIntent(
        placement=IDENTITY,
        pod_name=target.pod_name,
        static_set="web",
        static_set_uid=UID,
        namespace="edge",
        node="node-a",
        generation=3,
        reason="ScaledDown",
        requested_at=NOW,
    )

    with pytest.raises(OwnershipError):
        remove_workload(target, intent=intent, terminated=True)


def test_cleanup_refuses_an_intent_for_another_placement(
    temp_roots: tuple[Path, Path],
) -> None:
    """An intent names the exact placement it belongs to."""
    target = layout(temp_roots)
    intent = CleanupIntent(
        placement="another-identity",
        pod_name=target.pod_name,
        static_set="web",
        static_set_uid=UID,
        namespace="edge",
        node="node-a",
        generation=3,
        reason="ScaledDown",
        requested_at=NOW,
    )

    with pytest.raises(OwnershipError):
        remove_workload(target, intent=intent, terminated=True)


def test_intent_round_trips_through_the_filesystem(temp_roots: tuple[Path, Path]) -> None:
    """A persisted intent is discoverable again after a crash."""
    target = layout(temp_roots)
    intent = CleanupIntent(
        placement=IDENTITY,
        pod_name=target.pod_name,
        static_set="web",
        static_set_uid=UID,
        namespace="edge",
        node="node-a",
        generation=3,
        reason="Relocated",
        remove_configuration=False,
        requested_at=NOW,
    )

    write_intent(target, intent)

    assert read_intent(target) == intent


def test_manifest_removal_refuses_a_symlink(temp_roots: tuple[Path, Path]) -> None:
    """A symlinked manifest is refused rather than followed."""
    target = layout(temp_roots)
    outside = Path(temp_roots[1]) / "outside.yaml"
    outside.write_text("no", encoding="utf-8")
    manifest = Path(target.manifest_path)
    try:
        manifest.symlink_to(outside)
    except (OSError, NotImplementedError):  # pragma: no cover - Windows without privileges
        pytest.skip("symlinks are not available in this environment")

    with pytest.raises(PathSecurityError):
        claim(target)
        remove_workload(
            target,
            intent=CleanupIntent(
                placement=IDENTITY,
                pod_name=target.pod_name,
                static_set="web",
                static_set_uid=UID,
                namespace="edge",
                node="node-a",
                generation=3,
                reason="ScaledDown",
                requested_at=NOW,
            ),
            terminated=True,
        )


def test_existing_record_survives_a_reload(temp_roots: tuple[Path, Path]) -> None:
    """The record is stored as JSON that validates back into the model."""
    target = layout(temp_roots)
    claim(target)

    raw = json.loads(Path(target.record_path).read_text(encoding="utf-8"))

    assert raw["placement"] == IDENTITY
    assert raw["pod_name"] == target.pod_name


def test_a_tampered_manifest_is_repaired(
    temp_roots: tuple[Path, Path], make_static_set: Any
) -> None:
    """A manifest edited on the node is rewritten on the next reconcile."""
    target = layout(temp_roots)
    claim(target)
    install(target, make_static_set)
    manifest = Path(target.manifest_path)
    manifest.write_text("{}", encoding="utf-8")

    result = install(target, make_static_set)

    assert result.changed is True
    document = json.loads(manifest.read_text(encoding="utf-8"))
    assert document["metadata"]["name"] == target.pod_name


def test_an_unreadable_manifest_leaves_an_error_only_for_its_owner(
    temp_roots: tuple[Path, Path], make_static_set: Any
) -> None:
    """A missing manifest is simply rewritten, not treated as a failure."""
    target = layout(temp_roots)
    claim(target)
    install(target, make_static_set)
    Path(target.manifest_path).unlink()

    result = install(target, make_static_set)

    assert result.changed is True
    assert Path(target.manifest_path).is_file()
