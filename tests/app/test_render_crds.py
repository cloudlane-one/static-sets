"""The CRD renderer: the chart's committed files must be what the models describe.

``tests/app/test_manifests.py`` compares the committed files with ``crd_yaml``; this file
covers the script that writes them, because a renderer that dropped a resource would
leave the second comparison untested.
"""

from __future__ import annotations

import pathlib

import pytest
from based_operators import crd_yaml
from typer.testing import CliRunner

import render_crds
from static_sets.models import StaticSet
from static_sets.placements import StaticPodPlacement


def expected_names() -> set[str]:
    """Return the file names the chart has to carry.

    Returns:
        One name per rendered resource.
    """
    return {
        f"{resource._require_resource_config().crd_name}.yaml"
        for resource in (StaticSet, StaticPodPlacement)
    }


@pytest.fixture()
def chart(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> pathlib.Path:
    """Point the renderer at a throwaway chart directory.

    Args:
        tmp_path: Pytest's temporary directory.
        monkeypatch: Pytest's monkeypatch fixture.

    Returns:
        The directory the renderer will write into.
    """
    monkeypatch.setattr(render_crds, "CRDS_DIR", tmp_path)
    monkeypatch.setattr(render_crds, "REPO_ROOT", tmp_path.parent)
    return tmp_path


def test_both_resources_are_rendered(chart: pathlib.Path) -> None:
    """Every resource in the chart gets a file, named after its CRD."""
    written = render_crds.render()

    assert {path.name for path in written} == expected_names()
    assert {path.name for path in chart.iterdir()} == expected_names()


def test_a_rendered_file_is_the_models_schema(chart: pathlib.Path) -> None:
    """A rendered file is the published schema of its model, not a summary of it."""
    render_crds.render()
    static_set = chart / "staticsets.staticsets.cloudlane.dev.yaml"

    assert static_set.read_text(encoding="utf-8") == crd_yaml(StaticSet)


def test_rendering_after_a_model_change_replaces_the_stale_file(chart: pathlib.Path) -> None:
    """Regenerating must overwrite, or a forgotten edit would survive."""
    render_crds.render()
    stale = chart / "staticsets.staticsets.cloudlane.dev.yaml"
    stale.write_text("stale: true\n", encoding="utf-8")

    render_crds.render()

    assert "stale: true" not in stale.read_text(encoding="utf-8")


def test_the_command_writes_the_files_and_says_where(chart: pathlib.Path) -> None:
    """The Typer command is what a contributor runs, so it must report its output."""
    result = CliRunner().invoke(render_crds.app, [])

    assert result.exit_code == 0, result.output
    assert {path.name for path in chart.iterdir()} == expected_names()
    assert "wrote" in result.output
