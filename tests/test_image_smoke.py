"""Smoke-test the image built from ``deploy/Dockerfile``.

The build is slow, so the suite is skipped unless ``STATIC_SETS_IMAGE_SMOKE=1`` is
set — as the CI image-smoke job does — and a Docker daemon is reachable.

What it proves is what only a real image can: that the project installs, that both
entry points are present and runnable inside the container, and that the console
script an operator reaches for on a node is on ``PATH``.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
"""The repository root; the build context the Dockerfile expects."""

DOCKER = shutil.which("docker")
"""The ``docker`` executable, or ``None`` when it is not installed."""

pytestmark = pytest.mark.skipif(
    os.environ.get("STATIC_SETS_IMAGE_SMOKE") != "1" or DOCKER is None,
    reason="set STATIC_SETS_IMAGE_SMOKE=1 with a working Docker daemon",
)


def _run(*args: str, check: bool = True, timeout: int = 1800) -> subprocess.CompletedProcess[str]:
    """Run a command from the repository root.

    Args:
        *args: The command and its arguments.
        check: Whether a non-zero exit should raise.
        timeout: Seconds to wait.

    Returns:
        The completed process.
    """
    result = subprocess.run(args, capture_output=True, text=True, timeout=timeout, cwd=REPO_ROOT)
    if check and result.returncode != 0:
        raise AssertionError(f"{' '.join(args)} failed:\n{result.stdout}\n{result.stderr}")
    return result


@pytest.fixture(scope="module")
def image() -> Iterator[str]:
    """Build the operator image once for the module.

    Yields:
        The image reference that was built.
    """
    reference = f"static-sets-smoke:{uuid.uuid4().hex[:10]}"
    _run("docker", "build", "-f", "deploy/Dockerfile", "-t", reference, ".")
    try:
        yield reference
    finally:
        _run("docker", "rmi", "-f", reference, check=False)


def _inside(image: str, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    """Run a command inside the image.

    Args:
        image: The image reference.
        *args: The command to run.
        check: Whether a non-zero exit should raise.

    Returns:
        The completed process.
    """
    return _run("docker", "run", "--rm", image, *args, check=check)


def test_the_library_is_installed(image: str) -> None:
    """The wheel ships the library and the manager package."""
    result = _inside(
        image,
        "python",
        "-c",
        "import static_sets, static_sets_manager; print(static_sets.__name__)",
    )

    assert result.stdout.strip() == "static_sets"


def test_the_console_script_an_operator_uses_is_on_path(image: str) -> None:
    """``static-set-manager`` is what a manual pass on a node runs, so it must exist."""
    result = _inside(image, "static-set-manager", "--help")

    assert result.returncode == 0
    assert "manage" in result.stdout


def test_kopf_is_installed(image: str) -> None:
    """Both entry points are kopf applications, and kopf is what the image runs."""
    result = _inside(image, "kopf", "--version")

    assert result.returncode == 0


def test_both_entry_points_start_inside_the_image(image: str) -> None:
    """Both halves of the operator load their handlers without a cluster."""
    controller = _inside(
        image, "python", "-c", "import runpy; runpy.run_path('app/static_sets_operator.py')"
    )
    # The manager reads its node at import time: without one it must refuse loudly
    # rather than start and guess which host it owns.
    manager = _inside(
        image,
        "python",
        "-c",
        "import runpy; runpy.run_path('app/static_pod_manager.py')",
        check=False,
    )

    assert controller.returncode == 0, controller.stderr
    assert manager.returncode != 0
    assert "names no node" in manager.stderr


def test_the_manager_command_fails_cleanly_without_an_environment(image: str) -> None:
    """No node and no cluster: report it and exit, do not hang or traceback."""
    result = _inside(image, "static-set-manager", "manage", check=False)

    assert result.returncode == 2, result.stdout + result.stderr
    assert "invalid-environment" in result.stdout


def test_the_crds_are_rendered_from_the_installed_library(image: str) -> None:
    """The shipped models render both CRDs the cluster will be given."""
    result = _inside(
        image,
        "python",
        "-c",
        "from based_operators import crd_manifest;"
        "from static_sets.models import StaticSet;"
        "from static_sets.placements import StaticPodPlacement;"
        "print(crd_manifest(StaticSet)['metadata']['name']);"
        "print(crd_manifest(StaticPodPlacement)['metadata']['name'])",
    )

    assert result.stdout.split() == [
        "staticsets.staticsets.cloudlane.dev",
        "staticpodplacements.staticsets.cloudlane.dev",
    ]


# The committed CRDs are compared against the models the installed library renders by
# tests/app/test_manifests.py, which runs against the same wheel in the project's
# virtual environment. The image deliberately does not carry the chart, so that check
# stays where the chart is.
