"""Test the `based_pyproject_demo` notebook."""

import subprocess


def test_notebook_runs_without_error():
    """Make sure the `based_pyproject_demo` notebook runs without error."""
    result = subprocess.run(
        ["uv", "run", "app/hello_world.py"],
        capture_output=True,
        text=True,
        input="Tester\n",
        timeout=30,
    )
    assert result.returncode == 0, f"Notebook failed with error: {result.stderr}"
