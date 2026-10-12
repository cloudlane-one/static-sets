"""End-to-end suite: CRD, image, operator deployment, reconciliation, cleanup.

The suite drives the whole operator against real infrastructure: it registers the
generated CRD, builds the image from ``deploy/Dockerfile``, installs the chart,
creates a StaticSet, and then checks what actually happened *on the node* — the
installer Pod's own view of the manifest directory.

It is skipped unless ``STATIC_SETS_K8S_INTEGRATION=1`` is set and a cluster is
reachable through ``kubectl``; it additionally needs ``docker`` and ``helm``.

Two deliberate choices:

* the StaticSet points ``staticPodPath`` at a directory kubelet does **not** watch.
  That is what makes the suite safe on a kind cluster whose control plane is itself a
  set of static Pods, and it still proves everything the operator is responsible for:
  compiling the workload, materializing its configuration, writing the manifest
  durably, acknowledging it, and removing it again on cleanup. Whether kubelet then
  starts that manifest is covered by the manual reboot procedure in the documentation,
  because it cannot be verified by a pod exec;
* every assertion is on an observed effect, never on the operator's own status alone,
  so a controller that reports success without doing the work fails here.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
"""The repository root; the build context of the operator image."""

CRD_PATH = (
    REPO_ROOT
    / "deploy"
    / "helm"
    / "static-sets"
    / "crds"
    / "staticsets.staticsets.cloudlane.dev.yaml"
)
"""The CRD the chart installs."""

CRD_NAME = "staticsets.staticsets.cloudlane.dev"
"""The CRD's name."""

KIND = shutil.which("kind")

DOCKER = shutil.which("docker")
HELM = shutil.which("helm")

STATIC_POD_PATH = "/var/lib/static-sets-kubelet"
"""A directory kubelet does not watch, so the suite cannot disturb the node."""

BUNDLE_DIR = "/var/lib/static-sets"
"""The persistent bundle directory the installer owns."""

INSTALLER_SELECTOR = "app.kubernetes.io/component=installer"
PLACEMENT_SELECTOR = "app.kubernetes.io/component=placement"
SET_SELECTOR = "staticsets.cloudlane.dev/set"

pytestmark = pytest.mark.skipif(
    os.environ.get("STATIC_SETS_K8S_INTEGRATION") != "1",
    reason="set STATIC_SETS_K8S_INTEGRATION=1 with a reachable cluster",
)

requires_image_tooling = pytest.mark.skipif(
    DOCKER is None or HELM is None,
    reason="the end-to-end tests need docker and helm",
)


def _run(*args: str, check: bool = True, timeout: int = 600) -> subprocess.CompletedProcess[str]:
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


def _kubectl(
    *args: str, check: bool = True, stdin: str | None = None
) -> subprocess.CompletedProcess[str]:
    """Run ``kubectl``.

    Args:
        *args: Arguments passed to kubectl.
        check: Whether a non-zero exit should raise.
        stdin: Optional standard input.

    Returns:
        The completed process.
    """
    result = subprocess.run(
        ["kubectl", *args], capture_output=True, text=True, input=stdin, timeout=120
    )
    if check and result.returncode != 0:
        raise AssertionError(f"kubectl {' '.join(args)} failed: {result.stderr}")
    return result


def _apply(body: dict[str, Any]) -> None:
    """Apply a manifest from a Python dictionary.

    Args:
        body: The object to apply.
    """
    _kubectl("apply", "-f", "-", stdin=json.dumps(body))


def _json(*args: str) -> dict[str, Any] | None:
    """Run ``kubectl ... -o json`` and decode it, or return ``None`` on failure.

    Args:
        *args: Arguments passed to kubectl.

    Returns:
        The decoded object, or ``None`` when the command failed.
    """
    result = _kubectl(*args, "-o", "json", check=False)
    if result.returncode != 0:
        return None
    return json.loads(result.stdout)


def _items(payload: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Return the items of a list payload.

    Args:
        payload: The decoded list, or ``None``.

    Returns:
        The items.
    """
    return list((payload or {}).get("items", []))


def _wait_for(description: str, probe: Callable[[], bool], timeout: float = 420.0) -> None:
    """Poll until a condition holds.

    Args:
        description: What is being awaited.
        probe: The condition.
        timeout: Seconds to wait.

    Raises:
        AssertionError: If the condition never holds.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if probe():
            return
        time.sleep(5)
    raise AssertionError(f"timed out after {timeout:.0f}s waiting for {description}")


def _diagnostics(namespace: str, static_set: str) -> str:
    """Dump everything worth seeing when a reconcile did not happen.

    Args:
        namespace: The namespace under test.
        static_set: The StaticSet's name.

    Returns:
        A human-readable dump.
    """
    sections = {
        "controller logs": ("logs", "-n", namespace, "deployment/static-sets", "--tail=200"),
        "pods": ("get", "pods", "-n", namespace, "-o", "wide"),
        "events": ("get", "events", "-n", namespace, "--sort-by=.lastTimestamp"),
        "staticset": ("get", "staticset", static_set, "-n", namespace, "-o", "yaml"),
        "placements": (
            "get",
            "configmaps",
            "-n",
            namespace,
            "-l",
            PLACEMENT_SELECTOR,
            "-o",
            "yaml",
        ),
    }
    return "\n\n".join(
        f"--- {title} ---\n{_kubectl(*args, check=False).stdout}"
        for title, args in sections.items()
    )


# -- cluster helpers -------------------------------------------------------------


def installer_pods(namespace: str, static_set: str) -> list[dict[str, Any]]:
    """Return the installer Pods of a StaticSet.

    Args:
        namespace: The namespace under test.
        static_set: The StaticSet's name.

    Returns:
        The Pods, or an empty list when the query failed.
    """
    return _items(
        _json(
            "get",
            "pods",
            "-n",
            namespace,
            "-l",
            f"{INSTALLER_SELECTOR},{SET_SELECTOR}={static_set}",
        )
    )


def running_installer(namespace: str, static_set: str) -> dict[str, Any] | None:
    """Return the running installer Pod of a StaticSet, if there is one.

    Args:
        namespace: The namespace under test.
        static_set: The StaticSet's name.

    Returns:
        The Pod, or ``None``.
    """
    for pod in installer_pods(namespace, static_set):
        if pod.get("status", {}).get("phase") == "Running":
            return pod
    return None


def list_manifests(namespace: str, pod: str) -> list[str] | None:
    """List the manifest directory as the installer container sees it.

    Args:
        namespace: The namespace under test.
        pod: The installer Pod's name.

    Returns:
        The file names, or ``None`` when the directory could not be listed.
    """
    result = _kubectl("exec", pod, "-n", namespace, "--", "ls", STATIC_POD_PATH, check=False)
    if result.returncode != 0:
        return None
    return result.stdout.split()


def manifest_of(namespace: str, pod: str, static_set: str) -> str | None:
    """Return the manifest file that belongs to a StaticSet.

    Args:
        namespace: The namespace under test.
        pod: The installer Pod's name.
        static_set: The StaticSet's name.

    Returns:
        The file name, or ``None`` when no such manifest is present.
    """
    for name in list_manifests(namespace, pod) or []:
        if name.startswith(f"{static_set}-"):
            return name
    return None


def placement_payloads(namespace: str, static_set: str) -> list[dict[str, str]]:
    """Return the payloads of a StaticSet's placement objects.

    Args:
        namespace: The namespace under test.
        static_set: The StaticSet's name.

    Returns:
        The payloads.
    """
    return [
        item.get("data") or {}
        for item in _items(
            _json(
                "get",
                "configmaps",
                "-n",
                namespace,
                "-l",
                f"{PLACEMENT_SELECTOR},{SET_SELECTOR}={static_set}",
            )
        )
    ]


def placement_acknowledged(namespace: str, static_set: str) -> bool:
    """Return whether a placement of a StaticSet is acknowledged.

    Args:
        namespace: The namespace under test.
        static_set: The StaticSet's name.

    Returns:
        ``True`` when any placement reports an acknowledged install.
    """
    return any(
        payload.get("acknowledged") == "true" and payload.get("state") == "Installed"
        for payload in placement_payloads(namespace, static_set)
    )


def staticset_ready(namespace: str, name: str) -> bool:
    """Return whether a StaticSet reports ``Ready=True``.

    Args:
        namespace: The namespace under test.
        name: The StaticSet's name.

    Returns:
        ``True`` when the condition is present and true.
    """
    payload = _json("get", "staticset", name, "-n", namespace)
    conditions = (payload or {}).get("status", {}).get("conditions", [])
    return any(
        condition["type"] == "Ready" and condition["status"] == "True" for condition in conditions
    )


def _current_context() -> str:
    """Return the current kubectl context name.

    Returns:
        The context.
    """
    return _kubectl("config", "current-context").stdout.strip()


def _make_image_visible(image: str) -> None:
    """Load a locally built image into the cluster when it needs it.

    Args:
        image: The image reference.
    """
    context = _current_context()
    if context.startswith("kind-") and KIND is not None:
        _run("kind", "load", "docker-image", image, "--name", context[len("kind-") :])
    elif "minikube" in context and shutil.which("minikube") is not None:
        _run("minikube", "image", "load", image)


def prepare_static_pod_directory(namespace: str) -> None:
    """Create the static Pod directory on the node that will host an installer.

    The installer mounts the configured path as a ``Directory``, not a
    ``DirectoryOrCreate``: a path kubelet cannot be watching should fail loudly at
    mount time rather than be invented. Creating it is therefore the administrator's
    job, and the suite does what an administrator would — on the single node a kind
    cluster has.

    Args:
        namespace: The namespace to run the preparation Pod in.

    Raises:
        AssertionError: If the directory could not be created.
    """
    node = _kubectl("get", "nodes", "-o", "jsonpath={.items[0].metadata.name}").stdout.strip()
    name = "prepare-static-pod-path"
    _apply(
        {
            "apiVersion": "v1",
            "kind": "Pod",
            "metadata": {"name": name, "namespace": namespace},
            "spec": {
                "nodeName": node,
                "restartPolicy": "Never",
                "containers": [
                    {
                        "name": "prepare",
                        "image": "busybox:1.36",
                        "command": [
                            "sh",
                            "-c",
                            f"mkdir -p /host{STATIC_POD_PATH} && chmod 0700 /host{STATIC_POD_PATH}",
                        ],
                        "volumeMounts": [{"name": "host-root", "mountPath": "/host"}],
                    }
                ],
                "volumes": [{"name": "host-root", "hostPath": {"path": "/"}}],
            },
        }
    )

    def finished() -> bool:
        payload = _json("get", "pod", name, "-n", namespace)
        return (payload or {}).get("status", {}).get("phase") in {"Succeeded", "Failed"}

    try:
        _wait_for("the node preparation Pod to finish", finished, timeout=240)
        payload = _json("get", "pod", name, "-n", namespace) or {}
        assert payload.get("status", {}).get("phase") == "Succeeded", payload
    finally:
        _kubectl("delete", "pod", name, "-n", namespace, check=False)


def static_set_body(image: str, name: str, namespace: str, **overrides: Any) -> dict[str, Any]:
    """Return a StaticSet that exercises the workload translator.

    Args:
        image: The installer image to use.
        name: The StaticSet's name.
        namespace: The namespace to create it in.
        **overrides: Spec fields to merge in.

    Returns:
        The object.
    """
    spec: dict[str, Any] = {
        "mode": "Deployment",
        "replicas": 1,
        "template": {
            "spec": {
                "containers": [
                    {
                        "name": "app",
                        "image": "registry.example.com/app:1",
                        "imagePullPolicy": "IfNotPresent",
                        "env": [
                            {
                                "name": "GREETING",
                                "valueFrom": {
                                    "configMapKeyRef": {"name": "web-conf", "key": "app.conf"}
                                },
                            }
                        ],
                        "volumeMounts": [{"name": "conf", "mountPath": "/etc/web"}],
                    }
                ],
                "volumes": [{"name": "conf", "configMap": {"name": "web-conf"}}],
            }
        },
        "installer": {
            "image": image,
            "staticPodPath": STATIC_POD_PATH,
            "bundleDir": BUNDLE_DIR,
        },
    }
    for key, value in overrides.items():
        spec[key] = value
    return {
        "apiVersion": "staticsets.cloudlane.dev/v1",
        "kind": "StaticSet",
        "metadata": {"name": name, "namespace": namespace},
        "spec": spec,
    }


@pytest.fixture(scope="module")
def deployed() -> Iterator[dict[str, Any]]:
    """Build, install and later remove the operator.

    Yields:
        The namespace, release and image the operator was deployed as.
    """
    namespace = f"static-sets-{uuid.uuid4().hex[:8]}"
    release = "static-sets"
    image = f"static-sets-e2e:{uuid.uuid4().hex[:10]}"

    _kubectl("create", "namespace", namespace)
    _kubectl("apply", "-f", str(CRD_PATH))
    _run("docker", "build", "-f", "deploy/Dockerfile", "-t", image, ".", timeout=1800)
    _make_image_visible(image)
    _run(
        "helm",
        "install",
        release,
        "deploy/helm/static-sets",
        "--namespace",
        namespace,
        "--set",
        f"image.repository={image.split(':')[0]}",
        "--set",
        f"image.tag={image.split(':')[1]}",
        "--set",
        f"watchNamespace={namespace}",
    )
    try:
        _kubectl("rollout", "status", f"deployment/{release}", "-n", namespace, "--timeout=300s")
        # The installer mounts the static Pod path as a Directory, so the directory
        # kubelet would be watching has to exist first — this is the administrator's
        # step, performed here the way an administrator would.
        prepare_static_pod_directory(namespace)
        yield {"namespace": namespace, "release": release, "image": image}
    finally:
        _run("helm", "uninstall", release, "--namespace", namespace, check=False)
        # The CRD goes before the namespace. Removing it removes every StaticSet with
        # it, finalizers included, so a StaticSet whose cleanup never completed cannot
        # hold the namespace in Terminating and stall the rest of the run.
        _kubectl("delete", "crd", CRD_NAME, "--ignore-not-found", "--wait=false", check=False)
        _kubectl(
            "delete", "namespace", namespace, "--ignore-not-found", "--wait=false", check=False
        )
        _run("docker", "rmi", "-f", image, check=False)


@requires_image_tooling
def test_the_crd_is_established(deployed: dict[str, Any]) -> None:
    """The CRD the chart installs is accepted by the API server.

    Takes ``deployed`` for its side effect: that fixture applies the CRD, and this
    test is meaningless before it has.
    """
    payload = _json("get", "crd", CRD_NAME)

    conditions = (payload or {}).get("status", {}).get("conditions", [])
    assert any(
        condition["type"] == "Established" and condition["status"] == "True"
        for condition in conditions
    )


@requires_image_tooling
def test_an_unsupported_template_field_is_pruned_without_the_webhook(
    deployed: dict[str, Any],
) -> None:
    """Without the webhook the API server prunes; what must never happen is installing it.

    The chart's default has no webhook, so an object naming a field the operator
    cannot compile is accepted and the field dropped. The assertion is on the stored
    object, which is the evidence for the admission caveat the documentation states.
    """
    namespace = deployed["namespace"]
    name = f"pruned-{uuid.uuid4().hex[:8]}"
    body = static_set_body(deployed["image"], name, namespace)
    body["spec"]["template"]["spec"]["containers"][0]["lifecycle"] = {
        "postStart": {"exec": {"command": ["true"]}}
    }
    _kubectl("apply", "--validate=false", "-f", "-", stdin=json.dumps(body))

    stored = _json("get", "staticset", name, "-n", namespace)

    container = (stored or {})["spec"]["template"]["spec"]["containers"][0]
    assert "lifecycle" not in container
    _kubectl("delete", "staticset", name, "-n", namespace, check=False)


@requires_image_tooling
def test_a_static_set_is_installed_and_cleaned_up_on_a_real_node(
    deployed: dict[str, Any],
) -> None:
    """The whole loop: compile, materialize, write, acknowledge, and remove again."""
    namespace = deployed["namespace"]
    name = f"web-{uuid.uuid4().hex[:8]}"
    _apply(
        {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {"name": "web-conf", "namespace": namespace},
            "data": {"app.conf": "greeting=hello\n"},
        }
    )
    _apply(static_set_body(deployed["image"], name, namespace))

    try:
        _wait_for(
            "an installer Pod to run",
            lambda: running_installer(namespace, name) is not None,
        )
        pod = running_installer(namespace, name)
        assert pod is not None
        pod_name = pod["metadata"]["name"]

        _wait_for(
            "the installer to write the static Pod manifest",
            lambda: manifest_of(namespace, pod_name, name) is not None,
        )
        manifest_file = manifest_of(namespace, pod_name, name)
        assert manifest_file is not None
        manifest = json.loads(
            _kubectl(
                "exec",
                pod_name,
                "-n",
                namespace,
                "--",
                "cat",
                f"{STATIC_POD_PATH}/{manifest_file}",
            ).stdout
        )

        # The emitted workload is self-contained: the ConfigMap became an environment
        # value and a materialized file, and the mount points at the revision directory.
        assert manifest["metadata"]["name"] == manifest_file.removesuffix(".yaml")
        assert manifest["spec"]["containers"][0]["env"] == [
            {"name": "GREETING", "value": "greeting=hello\n"}
        ]
        assert manifest["spec"]["volumes"][0]["hostPath"]["path"].startswith(BUNDLE_DIR)
        assert "nodeSelector" not in manifest["spec"]
        assert "serviceAccountName" not in manifest["spec"]

        _wait_for(
            "the installer to acknowledge the placement",
            lambda: placement_acknowledged(namespace, name),
        )
        _wait_for(
            "the StaticSet to report Ready",
            lambda: staticset_ready(namespace, name),
        )
    except AssertionError as error:
        raise AssertionError(f"{error}\n\n{_diagnostics(namespace, name)}") from error

    # Deletion is the other half of the protocol: the intent is published, the
    # installer removes the manifest, acknowledges, and only then is the object
    # allowed to disappear.
    _kubectl("delete", "staticset", name, "-n", namespace)

    _wait_for(
        "the static Pod manifest to be removed",
        lambda: manifest_of(namespace, pod_name, name) is None,
    )
    _wait_for(
        "the StaticSet to be released by its finalizer",
        lambda: _json("get", "staticset", name, "-n", namespace) is None,
    )


@requires_image_tooling
def test_replacing_the_installer_keeps_the_placement(deployed: dict[str, Any]) -> None:
    """A replaced installer adopts its predecessor's placement instead of orphaning it."""
    namespace = deployed["namespace"]
    name = f"restart-{uuid.uuid4().hex[:8]}"
    body = static_set_body(deployed["image"], name, namespace)
    body["spec"]["template"]["spec"]["containers"][0].pop("env")
    body["spec"]["template"]["spec"]["containers"][0].pop("volumeMounts")
    body["spec"]["template"]["spec"]["volumes"] = []
    _apply(body)

    _wait_for(
        "the first placement to be acknowledged",
        lambda: placement_acknowledged(namespace, name),
    )
    first = running_installer(namespace, name)
    assert first is not None
    first_name = first["metadata"]["name"]

    _kubectl("delete", "pod", first_name, "-n", namespace)

    def replaced_and_acknowledged() -> bool:
        replacement = running_installer(namespace, name)
        return (
            replacement is not None
            and replacement["metadata"]["name"] != first_name
            and placement_acknowledged(namespace, name)
        )

    _wait_for(
        "the replacement installer to re-acknowledge the placement", replaced_and_acknowledged
    )

    # The placement identity is derived from the node, so there is still exactly one
    # placement object, not one per installer Pod.
    assert len(placement_payloads(namespace, name)) == 1
    _kubectl("delete", "staticset", name, "-n", namespace)
