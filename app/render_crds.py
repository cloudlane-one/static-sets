# ---
# jupyter:
#   jupytext:
#     formats: ipynb,py:percent
#     text_representation:
#       extension: .py
#       format_name: percent
#       format_version: '1.3'
#       jupytext_version: 1.19.5
#   kernelspec:
#     display_name: static-sets (3.13.5)
#     language: python
#     name: python3
# ---

# %% [markdown]
# # Render the CustomResourceDefinition
#
# The CRDs are generated from the Pydantic models, so they can never drift from the
# types the operator actually reconciles. There are two: the `StaticSet` a user
# writes, and the `StaticPodPlacement` the controller writes and each node's manager
# fills in. The command writes both rendered documents into the Helm chart's `crds/`
# directory, which is where Helm installs CRDs from:
#
# ```shell
# uv run app/render_crds.py
# ```
#
# The test suite re-renders the same document and compares it with the committed
# file, so a model change that was not re-rendered fails the build instead of
# silently disagreeing with the chart.
#
# The command is a Typer application like the installer, so its reference page is
# generated from the declaration below rather than written by hand.

# %%
from __future__ import annotations

from pathlib import Path

import typer
from based_operators import crd_yaml

from static_sets.models import StaticSet
from static_sets.placements import StaticPodPlacement

REPO_ROOT = Path(__file__).resolve().parents[1]
"""The repository root, so the script works from any working directory."""

CRDS_DIR = REPO_ROOT / "deploy" / "helm" / "static-sets" / "crds"
"""The chart directory Helm installs CustomResourceDefinitions from."""


RESOURCES = (StaticSet, StaticPodPlacement)
"""The custom resources the chart has to install."""


def render() -> list[Path]:
    """Write every resource's CRD into the chart.

    Returns:
        The paths that were written.
    """
    CRDS_DIR.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for resource in RESOURCES:
        config = resource._require_resource_config()
        target = CRDS_DIR / f"{config.crd_name}.yaml"
        target.write_text(crd_yaml(resource), encoding="utf-8", newline="\n")
        written.append(target)
    return written


app = typer.Typer(
    name="render-crds",
    help=(
        "Render the StaticSet and StaticPodPlacement CustomResourceDefinitions into the Helm chart."
    ),
    add_completion=False,
    context_settings={"help_option_names": ["-h", "--help"]},
)
"""The Typer application behind the CRD renderer."""


@app.command()
def render_crd() -> None:
    """Write the CRDs generated from the models into the chart's crds/ directory."""
    for target in render():
        typer.echo(f"wrote {target.relative_to(REPO_ROOT).as_posix()}")


# %%
if __name__ == "__main__":
    app(prog_name="render-crds")
