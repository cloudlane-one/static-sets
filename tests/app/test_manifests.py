"""The shipped manifests: CRD drift, samples, chart rendering and the Dockerfile.

The CRD test is the important one: the documents in the chart are generated from the
Pydantic models, so re-rendering them here proves the committed files still describe
the types the operator actually reconciles.
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
from static_sets.placements import StaticPodPlacement

REPO_ROOT = Path(__file__).resolve().parents[2]
CHART = REPO_ROOT / "deploy" / "helm" / "static-sets"
CRDS = CHART / "crds"
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


@pytest.mark.parametrize(
    ("resource", "name"),
    [
        (StaticSet, "staticsets.staticsets.cloudlane.dev.yaml"),
        (StaticPodPlacement, "staticpodplacements.staticsets.cloudlane.dev.yaml"),
    ],
    ids=lambda value: getattr(value, "__name__", str(value)),
)
def test_every_committed_crd_matches_its_model(resource: Any, name: str) -> None:
    """Each CRD in the chart is exactly what its model renders."""
    committed = documents(CRDS / name)

    assert len(committed) == 1
    assert yaml.safe_dump(committed[0], sort_keys=True) == yaml.safe_dump(
        yaml.safe_load(crd_yaml(resource)), sort_keys=True
    )


def test_the_chart_ships_a_crd_for_every_resource_the_operator_serves() -> None:
    """A missing CRD is an operator that cannot cache what it watches."""
    names = {path.name for path in CRDS.glob("*.yaml")}

    assert names == {
        "staticsets.staticsets.cloudlane.dev.yaml",
        "staticpodplacements.staticsets.cloudlane.dev.yaml",
    }


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


def test_samples_do_not_carry_installer_settings() -> None:
    """Where the static Pod is written is the node manager's configuration now."""
    for sample in sorted(SAMPLES.glob("*.yaml")):
        for document in documents(sample):
            if document.get("kind") == "StaticSet":
                assert "installer" not in document["spec"], sample.name


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
def test_chart_renders_the_controller_the_manager_and_their_permissions() -> None:
    """The default render contains both processes, both identities and two cluster roles."""
    templates = render_chart()

    kinds = [document["kind"] for document in templates]
    assert kinds.count("Deployment") == 1
    assert kinds.count("DaemonSet") == 1
    assert kinds.count("RuntimeClass") == 1
    assert kinds.count("ServiceAccount") == 2
    assert "ClusterRole" in kinds
    assert "ClusterRoleBinding" in kinds
    # Nothing is namespaced any more: a StaticSet may live in any namespace, and the
    # manager serves placements chosen by node, not by namespace.
    assert "Role" not in kinds
    assert "RoleBinding" not in kinds
    assert "Service" not in kinds
    assert "Certificate" not in kinds


@requires_helm
def test_the_controller_is_cluster_wide() -> None:
    """A namespaced watch would leave every StaticSet outside it unreconciled."""
    deployment = _document(render_chart(), "Deployment")
    container = deployment["spec"]["template"]["spec"]["containers"][0]

    assert not any(str(argument).startswith("--namespace") for argument in container["command"])


@requires_helm
def test_chart_controller_needs_no_privilege() -> None:
    """The controller mounts nothing from the host and runs unprivileged."""
    deployment = _document(render_chart(), "Deployment")
    pod = deployment["spec"]["template"]["spec"]

    assert "volumes" not in pod
    assert pod["containers"][0]["securityContext"]["runAsNonRoot"] is True
    assert pod["containers"][0]["securityContext"]["capabilities"]["drop"] == ["ALL"]
    assert pod["containers"][0]["command"][0] == "kopf"
    assert "app/static_sets_operator.py" in pod["containers"][0]["command"]


@requires_helm
def test_the_manager_runs_once_per_node_with_exactly_two_host_mounts() -> None:
    """One Pod per node, kubelet's directory and the bundle, and nothing else."""
    daemon_set = _document(render_chart(), "DaemonSet")
    pod = daemon_set["spec"]["template"]["spec"]
    container = pod["containers"][0]

    assert "app/static_pod_manager.py" in container["command"]
    assert [volume["name"] for volume in pod["volumes"]] == ["static-pod-path", "bundle"]
    assert {volume["hostPath"]["type"] for volume in pod["volumes"]} == {
        "Directory",
        "DirectoryOrCreate",
    }
    assert [mount["mountPath"] for mount in container["volumeMounts"]] == [
        "/etc/kubernetes/manifests",
        "/var/lib/static-sets",
    ]
    assert container["securityContext"]["readOnlyRootFilesystem"] is True
    assert container["securityContext"]["allowPrivilegeEscalation"] is False
    assert container["securityContext"]["capabilities"]["drop"] == ["ALL"]


@requires_helm
def test_the_manager_learns_its_node_from_the_downward_api() -> None:
    """The manager is only correct if it knows which host it is responsible for."""
    daemon_set = _document(render_chart(), "DaemonSet")
    container = daemon_set["spec"]["template"]["spec"]["containers"][0]
    environment = {item["name"]: item for item in container["env"]}

    assert environment["STATIC_SETS_NODE"]["valueFrom"]["fieldRef"]["fieldPath"] == "spec.nodeName"
    assert (
        environment["STATIC_SETS_MANAGER_POD"]["valueFrom"]["fieldRef"]["fieldPath"]
        == "metadata.name"
    )
    assert environment["STATIC_SETS_STATIC_POD_PATH"]["value"] == "/etc/kubernetes/manifests"


@requires_helm
def test_the_manager_tolerates_every_taint_by_default() -> None:
    """A placement can be asked for on any node, so the manager must be able to run there."""
    daemon_set = _document(render_chart(), "DaemonSet")

    assert daemon_set["spec"]["template"]["spec"]["tolerations"] == [{"operator": "Exists"}]


@requires_helm
def test_the_chart_installs_the_placeholder_runtime_class() -> None:
    """The probe's runtime class must exist and must have a handler no CRI implements."""
    rendered = render_chart()
    runtime_class = _document(rendered, "RuntimeClass")
    deployment = _document(rendered, "Deployment")
    container = deployment["spec"]["template"]["spec"]["containers"][0]
    environment = {item["name"]: item.get("value") for item in container["env"]}

    assert runtime_class["handler"] == runtime_class["metadata"]["name"]
    assert environment["STATIC_SETS_PROBE_RUNTIME_CLASS"] == runtime_class["metadata"]["name"]


@requires_helm
def test_the_runtime_class_can_be_left_to_an_administrator() -> None:
    """A cluster that manages its runtime classes itself renders no RuntimeClass."""
    rendered = render_chart("probe.createRuntimeClass=false")

    assert "RuntimeClass" not in [document["kind"] for document in rendered]


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
def test_the_manager_account_is_one_the_chart_creates() -> None:
    """A Pod naming a ServiceAccount that does not exist is refused at admission."""
    rendered = render_chart()
    accounts = {
        document["metadata"]["name"]
        for document in rendered
        if document["kind"] == "ServiceAccount"
    }
    daemon_set = _document(rendered, "DaemonSet")
    pod = daemon_set["spec"]["template"]["spec"]

    assert pod["serviceAccountName"] in accounts
    assert (
        pod["serviceAccountName"]
        != _document(rendered, "Deployment")["spec"]["template"]["spec"]["serviceAccountName"]
    )


@requires_helm
def test_both_processes_may_read_the_placement_records() -> None:
    """The controller writes them and the manager reports on them."""
    rendered = render_chart()
    roles = {
        document["metadata"]["name"]: document
        for document in rendered
        if document["kind"] == "ClusterRole"
    }
    controller = next(role for name, role in roles.items() if name.endswith("-controller"))
    manager = next(role for name, role in roles.items() if name.endswith("-manager"))

    def resources(role: dict[str, Any]) -> set[str]:
        return {item for rule in role["rules"] for item in rule.get("resources", [])}

    assert "staticpodplacements" in resources(controller)
    assert "staticpodplacements/status" in resources(manager)
    assert "staticsets" in resources(manager)
    # The manager never writes the one thing that decides where a placement goes.
    for rule in manager["rules"]:
        if "staticpodplacements" in rule.get("resources", []):
            assert "update" in rule["verbs"] or "patch" in rule["verbs"]
            assert "create" not in rule["verbs"]
            assert "delete" not in rule["verbs"]


@requires_helm
def test_chart_grants_the_namespace_read_kopf_needs() -> None:
    """kopf manages a watch per namespace, so it reads them."""
    rendered = render_chart()
    roles = [
        document
        for document in rendered
        if document["kind"] == "ClusterRole"
        and any("namespaces" in rule.get("resources", []) for rule in document.get("rules", []))
    ]

    assert len(roles) == 1
    rule = next(rule for rule in roles[0]["rules"] if "namespaces" in rule.get("resources", []))
    assert set(rule["verbs"]) >= {"get", "list", "watch"}


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
    assert (REPO_ROOT / "app" / "static_pod_manager.py").is_file()


def test_every_dockerfile_references_an_existing_entry_point() -> None:
    """Every source an image build copies exists in the repository.

    The builder stage installs the whole context into ``/app``, so a later stage that
    copies ``/app/<path>`` out of it is copying ``<path>`` out of the repository.
    """
    for line in DOCKERFILE.read_text(encoding="utf-8").splitlines():
        if line.startswith("COPY "):
            tokens = [token for token in line.split()[1:] if not token.startswith("--")]
            source = tokens[0]
            if source == ".":
                assert (REPO_ROOT / "pyproject.toml").is_file()
            elif source.startswith("/app/"):
                assert (REPO_ROOT / source.removeprefix("/app/")).exists(), source
            elif not source.startswith("/"):
                assert (REPO_ROOT / source).exists(), source
        if line.startswith(("CMD", "ENTRYPOINT")):
            for token in line.split():
                if token.startswith("app/"):
                    assert (REPO_ROOT / token.strip('",')).is_file()


def test_the_shipped_image_carries_no_build_tooling() -> None:
    """git and a compiler stay in the builder stage, out of the node-facing image.

    The manager runs as root with kubelet's manifest directory mounted, so tooling that
    is only needed to fetch a Git-referenced dependency must not survive into it.
    """
    content = DOCKERFILE.read_text(encoding="utf-8")
    final_stage = content.rsplit("\nFROM ", 1)[1]

    assert "apt-get" not in final_stage
    assert all(
        "--from=builder" in line for line in final_stage.splitlines() if line.startswith("COPY ")
    )
    assert ".git/" in (REPO_ROOT / ".dockerignore").read_text(encoding="utf-8")


def test_dockerignore_excludes_build_output() -> None:
    """The image context would otherwise carry the virtualenv and prior builds."""
    ignored = (REPO_ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()

    assert ".venv/" in ignored
    assert "build/" in ignored
    assert "dist/" in ignored
