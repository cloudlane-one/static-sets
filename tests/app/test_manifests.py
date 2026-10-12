"""The shipped manifests: CRD drift, samples, chart rendering and the Dockerfile.

The CRD test is the important one: the document in the chart is generated from the
Pydantic models, so re-rendering it here proves the committed file still describes
the type the operator actually reconciles.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml
from based_operators import crd_yaml

from static_sets.models import StaticSet

REPO_ROOT = Path(__file__).resolve().parents[2]
CHART = REPO_ROOT / "deploy" / "helm" / "static-sets"
SAMPLES = REPO_ROOT / "deploy" / "samples"
DOCKERFILE = REPO_ROOT / "deploy" / "Dockerfile"

HELM = shutil.which("helm")
"""The ``helm`` executable, or ``None`` when it is not installed."""

requires_helm = pytest.mark.skipif(
    HELM is None and not os.environ.get("CI"),
    reason="helm is not installed; install it in CI so this check cannot be skipped vacuously",
)


def documents(path: Path) -> list[dict[str, Any]]:
    """Parse a multi-document YAML file.

    Args:
        path: The file to read.

    Returns:
        The parsed documents.
    """
    return [item for item in yaml.safe_load_all(path.read_text(encoding="utf-8")) if item]


def test_the_committed_crd_matches_the_models() -> None:
    """The CRD in the chart is exactly what the models render."""
    committed = documents(CHART / "crds" / "staticsets.staticsets.cloudlane.dev.yaml")

    assert len(committed) == 1
    assert yaml.safe_dump(committed[0], sort_keys=True) == yaml.safe_dump(
        yaml.safe_load(crd_yaml(StaticSet)), sort_keys=True
    )


@pytest.mark.parametrize("sample", sorted(SAMPLES.glob("*.yaml")), ids=lambda path: path.name)
def test_samples_are_valid(sample: Path) -> None:
    """Every sample parses, and each StaticSet in it is a valid object."""
    for document in documents(sample):
        if document.get("kind") == "StaticSet":
            StaticSet.model_validate(document)


def test_the_samples_cover_both_modes_and_both_configuration_kinds() -> None:
    """The sample set is what makes the supported shapes executable knowledge."""
    resources = [
        document for sample in sorted(SAMPLES.glob("*.yaml")) for document in documents(sample)
    ]
    static_sets = [item for item in resources if item.get("kind") == "StaticSet"]
    modes = {item["spec"]["mode"] for item in static_sets}

    assert modes == {"Deployment", "DaemonSet"}
    assert any(item.get("kind") == "ConfigMap" for item in resources)
    assert any(item.get("kind") == "Secret" for item in resources)


def test_samples_use_the_namespace_the_chart_watches() -> None:
    """A sample that lands outside the watched namespace would never reconcile."""
    for sample in sorted(SAMPLES.glob("*.yaml")):
        for document in documents(sample):
            namespace = (document.get("metadata") or {}).get("namespace")
            assert namespace == "static-sets", f"{sample.name} targets {namespace}"


@requires_helm
def test_chart_lints() -> None:
    """The chart passes ``helm lint``."""
    result = subprocess.run(
        [str(HELM), "lint", str(CHART)],
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr


@requires_helm
def test_chart_renders_the_controller_and_its_namespaced_permissions() -> None:
    """The default render contains the controller, both identities and its Roles."""
    templates = render_chart()

    kinds = [document["kind"] for document in templates]
    assert kinds.count("Deployment") == 1
    assert kinds.count("ServiceAccount") == 2
    assert "Role" in kinds
    assert "RoleBinding" in kinds
    assert "ClusterRole" in kinds
    assert "Service" not in kinds
    assert "Certificate" not in kinds


@requires_helm
def test_chart_controller_needs_no_privilege() -> None:
    """The controller mounts nothing from the host and runs unprivileged."""
    deployment = _document(render_chart(), "Deployment")
    pod = deployment["spec"]["template"]["spec"]

    assert "volumes" not in pod
    assert pod["containers"][0]["securityContext"]["runAsNonRoot"] is True
    assert pod["containers"][0]["securityContext"]["capabilities"]["drop"] == ["ALL"]
    assert pod["containers"][0]["command"][0] == "kopf"


@requires_helm
def test_chart_enables_the_webhook_when_asked() -> None:
    """Turning the webhook on adds the service, the certificate and the module."""
    templates = render_chart("webhook.enabled=true")

    kinds = [document["kind"] for document in templates]
    assert "Service" in kinds
    assert "Certificate" in kinds
    assert "Issuer" in kinds

    deployment = _document(templates, "Deployment")
    container = deployment["spec"]["template"]["spec"]["containers"][0]
    assert "based_operators.webhook" in container["command"]
    environment = {item["name"]: item["value"] for item in container["env"]}
    assert environment["STATIC_SETS_WEBHOOK"] == "1"
    assert environment["WEBHOOK_CERT_DIR"] == "/etc/webhook/certs"


@requires_helm
def test_chart_disables_the_webhook_by_default() -> None:
    """Without cert-manager the operator still starts, without the webhook module."""
    deployment = _document(render_chart(), "Deployment")
    container = deployment["spec"]["template"]["spec"]["containers"][0]

    assert "based_operators.webhook" not in container["command"]
    environment = {item["name"]: item["value"] for item in container["env"]}
    assert environment["STATIC_SETS_WEBHOOK"] == "0"


@requires_helm
def test_chart_watches_the_release_namespace_by_default() -> None:
    """The controller is scoped to one namespace, which is what makes Roles enough."""
    deployment = _document(render_chart(), "Deployment")
    container = deployment["spec"]["template"]["spec"]["containers"][0]

    assert "--namespace=static-sets" in container["command"]


@requires_helm
def test_chart_watchers_can_be_pointed_elsewhere() -> None:
    """The watched namespace is a value, not a hard-coded default."""
    deployment = _document(render_chart("watchNamespace=edge"), "Deployment")
    container = deployment["spec"]["template"]["spec"]["containers"][0]

    assert "--namespace=edge" in container["command"]


def render_chart(*settings: str) -> list[dict[str, Any]]:
    """Render the chart with ``helm template``.

    Args:
        *settings: ``--set`` values.

    Returns:
        The rendered documents.
    """
    command = [
        str(HELM),
        "template",
        "static-sets",
        str(CHART),
        "--namespace",
        "static-sets",
    ]
    for setting in settings:
        command += ["--set", setting]
    result = subprocess.run(command, capture_output=True, text=True, timeout=300, check=True)
    return [item for item in yaml.safe_load_all(result.stdout) if item]


def _document(templates: list[dict[str, Any]], kind: str) -> dict[str, Any]:
    """Return the single rendered document of one kind.

    Args:
        templates: The rendered documents.
        kind: The kind to find.

    Returns:
        The document.
    """
    matches = [document for document in templates if document["kind"] == kind]
    assert len(matches) == 1, f"expected exactly one {kind}, found {len(matches)}"
    return matches[0]


def test_the_image_builds_the_project_and_runs_the_entry_point() -> None:
    """The Dockerfile installs the project and its command names the real file."""
    content = DOCKERFILE.read_text(encoding="utf-8")

    assert "pip install --no-cache-dir ." in content
    assert "--liveness=http://0.0.0.0:8080/healthz" in content
    assert "USER 65532" in content
    assert "--privileged" not in content
    assert (REPO_ROOT / "app" / "static_sets_operator.py").is_file()


def test_every_dockerfile_references_an_existing_entry_point() -> None:
    """A COPY path or a command that names a missing file would break the build."""
    for line in DOCKERFILE.read_text(encoding="utf-8").splitlines():
        if line.startswith("COPY "):
            assert line.split()[1] == "."
        if line.startswith("ENTRYPOINT") or line.startswith("CMD"):
            for token in line.split():
                if token.startswith("app/"):
                    assert (REPO_ROOT / token.strip('",')).is_file()


def test_dockerignore_excludes_build_output() -> None:
    """The image context would otherwise carry the virtualenv and prior builds."""
    ignored = (REPO_ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()

    assert ".venv/" in ignored
    assert "build/" in ignored
    assert "dist/" in ignored
