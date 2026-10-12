"""Smoke-test the image built from ``deploy/Dockerfile``.

The build is slow, so the suite is skipped unless ``STATIC_SETS_IMAGE_SMOKE=1`` is
set — as the CI image-smoke job does — and a Docker daemon is reachable.

What it proves is what only a real image can: that the project installs, that both
entry points are present and runnable inside the container, and that the console
script the chart's installer Pods invoke is on ``PATH``.
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
    """The wheel ships the library and the installer package."""
    result = _inside(
        image,
        "python",
        "-c",
        "import static_sets, static_sets_installer; print(static_sets.__name__)",
    )

    assert result.stdout.strip() == "static_sets"


def test_the_console_script_the_chart_uses_is_on_path(image: str) -> None:
    """Installer Pods run ``static-set-installer``, so it has to exist in the image."""
    result = _inside(image, "static-set-installer", "--help")

    assert result.returncode == 0
    assert "reconcile" in result.stdout


def test_the_controller_is_installed(image: str) -> None:
    """kopf is present, which is what the image's command runs."""
    result = _inside(image, "kopf", "--version")

    assert result.returncode == 0


def test_the_entry_point_and_the_installer_module_both_run(image: str) -> None:
    """Both halves of the operator start inside the image."""
    controller = _inside(
        image, "python", "-c", "import runpy; runpy.run_path('app/static_sets_operator.py')"
    )
    installer = _inside(image, "static-set-installer", "reconcile", check=False)

    assert controller.returncode == 0, controller.stderr
    # No cluster and no environment: the installer must fail cleanly, not hang or
    # traceback into an unknown state.
    assert installer.returncode == 2, installer.stdout + installer.stderr
    assert "invalid-environment" in installer.stdout


def test_the_crd_is_rendered_from_the_installed_library(image: str) -> None:
    """The shipped models render the CRD the cluster will be given."""
    result = _inside(
        image,
        "python",
        "-c",
        "from based_operators import crd_manifest; from static_sets.models import StaticSet;"
        "print(crd_manifest(StaticSet)['metadata']['name'])",
    )

    assert result.stdout.strip() == "staticsets.staticsets.cloudlane.dev"


# The committed CRD is compared against the models the installed library renders by
# tests/app/test_manifests.py, which runs against the same wheel in the project's
# virtual environment. The image deliberately does not carry the chart, so that check
# stays where the chart is.
