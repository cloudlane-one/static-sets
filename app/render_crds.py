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
# The CRD is generated from the Pydantic models, so it can never drift from the
# type the operator actually reconciles. This script writes the rendered document
# into the Helm chart's `crds/` directory, which is where Helm installs CRDs from:
#
# ```shell
# uv run app/render_crds.py
# ```
#
# The test suite re-renders the same document and compares it with the committed
# file, so a model change that was not re-rendered fails the build instead of
# silently disagreeing with the chart.

# %%
from __future__ import annotations

import sys
from pathlib import Path

from based_operators import crd_yaml

from static_sets.models import StaticSet

REPO_ROOT = Path(__file__).resolve().parents[1]
"""The repository root, so the script works from any working directory."""

CRDS_DIR = REPO_ROOT / "deploy" / "helm" / "static-sets" / "crds"
"""The chart directory Helm installs CustomResourceDefinitions from."""


def render() -> Path:
    """Write the StaticSet CRD into the chart.

    Returns:
        The path that was written.
    """
    config = StaticSet._require_resource_config()
    CRDS_DIR.mkdir(parents=True, exist_ok=True)
    target = CRDS_DIR / f"{config.crd_name}.yaml"
    target.write_text(crd_yaml(StaticSet), encoding="utf-8", newline="\n")
    return target


def main() -> int:
    """Render the CRD and report what was written.

    Returns:
        The process exit status.
    """
    target = render()
    print(f"wrote {target.relative_to(REPO_ROOT).as_posix()}")
    return 0


# %%
if __name__ == "__main__":
    sys.exit(main())
