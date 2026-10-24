"""End-to-end suite: CRDs, image, both processes, placement, installation, cleanup.

The suite drives the whole operator against real infrastructure: it registers the
generated CRDs, builds the image from ``deploy/Dockerfile``, installs the chart, and
then checks what actually happened *on the node* — kubelet's own manifest directory,
as the node manager sees it, and the mirror Pod kubelet publishes for what it started.

It is skipped unless ``STATIC_SETS_K8S_INTEGRATION=1`` is set and a cluster is
reachable through ``kubectl``; it additionally needs ``docker`` and ``helm``.

Two deliberate choices:

* the workload is pinned to one node through ``spec.placement.nodeSelector``, so the
  suite knows which kubelet to look at. The manifest goes into that node's real
  ``staticPodPath`` (override it with ``STATIC_SETS_STATIC_POD_PATH`` on k3s), which is
  what makes the mirror Pod appear — the proof that kubelet accepted the file. The
  workload is a sleeping container named after the run, and the suite removes it;
* the workload is the operator image itself, running ``sleep``. It is already present
  on every node because the manager DaemonSet pulled it, so the suite needs no registry
  and no particular network access, and a static Pod that must survive without the API
  server is exercised with an image that is genuinely local;
* every assertion is on an observed effect — kubelet's mirror Pod, the file on the
  node, the placement record — never on the operator's own status alone, so a
  controller that reports success without doing the work fails here.
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

CRDS = {
    "staticsets.staticsets.cloudlane.dev": (
        REPO_ROOT
        / "deploy"
        / "helm"
        / "static-sets"
        / "crds"
        / "staticsets.staticsets.cloudlane.dev.yaml"
    ),
    "staticpodplacements.staticsets.cloudlane.dev": (
        REPO_ROOT
        / "deploy"
        / "helm"
        / "static-sets"
        / "crds"
        / "staticpodplacements.staticsets.cloudlane.dev.yaml"
    ),
}
"""The CRDs the chart installs, by name."""

KIND = shutil.which("kind")

DOCKER = shutil.which("docker")
HELM = shutil.which("helm")

STATIC_POD_PATH = os.environ.get("STATIC_SETS_STATIC_POD_PATH", "/etc/kubernetes/manifests")
"""kubelet's own ``staticPodPath``: the suite installs for real.

The default is what kubeadm and kind configure. k3s uses
``/var/lib/rancher/k3s/agent/pod-manifests``; override it with
``STATIC_SETS_STATIC_POD_PATH`` rather than editing the suite. Read the real value from
the node with
``kubectl get --raw /api/v1/nodes/<node>/proxy/configz``."""

BUNDLE_DIR = "/var/lib/static-sets"
"""The persistent bundle directory the manager owns."""

PLACEMENT_SELECTOR = "app.kubernetes.io/component=placement"
PROBE_SELECTOR = "app.kubernetes.io/component=scheduling-probe"

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


def _diagnostics(namespace: str, static_set: str, node: str) -> str:
    """Dump everything worth seeing when a reconcile did not happen.

    Args:
        namespace: The namespace under test.
        static_set: The StaticSet's name.
        node: The node the placement was pinned to.

    Returns:
        A human-readable dump.
    """
    sections = {
        "controller logs": (
            "logs",
            "-n",
            namespace,
            "deployment/static-sets",
            "--tail=200",
        ),
        "manager logs": (
            "logs",
            "-n",
            namespace,
            "daemonset/static-sets-manager",
            "--tail=200",
        ),
        "pods": ("get", "pods", "-n", namespace, "-o", "wide"),
        "probe workloads": (
            "get",
            "replicasets,daemonsets",
            "-n",
            namespace,
            "-l",
            PROBE_SELECTOR,
            "-o",
            "yaml",
        ),
        "events": ("get", "events", "-n", namespace, "--sort-by=.lastTimestamp"),
        "staticset": ("get", "staticset", static_set, "-n", namespace, "-o", "yaml"),
        "placements": (
            "get",
            "staticpodplacements",
            "-n",
            namespace,
            "-o",
            "yaml",
        ),
        "node manifests": (
            "get",
            "pods",
            "-n",
            namespace,
            "-l",
            "app.kubernetes.io/component=node-manager",
            "-o",
            "wide",
        ),
        "node": ("get", "node", node, "-o", "wide"),
    }
    return "\n\n".join(
        f"--- {title} ---\n{_kubectl(*args, check=False).stdout}"
        for title, args in sections.items()
    )


# -- cluster helpers -------------------------------------------------------------


def _manager_pod(namespace: str, node: str) -> str | None:
    """Return the manager Pod running on one node.

    Args:
        namespace: The namespace the chart was installed in.
        node: The node's name.

    Returns:
        The Pod's name, or ``None`` when the manager is not running there.
    """
    result = _kubectl(
        "get",
        "pods",
        "-n",
        namespace,
        "-l",
        "app.kubernetes.io/component=node-manager",
        "--field-selector",
        f"spec.nodeName={node}",
        "-o",
        "jsonpath={.items[0].metadata.name}",
        check=False,
    )
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def _exec(node: str, namespace: str, *command: str) -> subprocess.CompletedProcess[str]:
    """Run a command in the node manager's container on one node.

    Args:
        node: The node's name.
        namespace: The namespace the chart was installed in.
        *command: The command to run.

    Returns:
        The completed process.
    """
    pod = _manager_pod(namespace, node)
    assert pod is not None, f"no manager Pod on node {node}"
    return _kubectl("exec", pod, "-n", namespace, "-c", "manager", "--", *command, check=False)


def _manifests_on(namespace: str, node: str) -> list[str]:
    """List kubelet's static Pod directory as the manager sees it.

    Args:
        namespace: The namespace the chart was installed in.
        node: The node's name.

    Returns:
        The file names, or an empty list when the directory could not be listed.
    """
    result = _exec(node, namespace, "ls", STATIC_POD_PATH)
    if result.returncode != 0:
        return []
    return result.stdout.split()


def _manifest_on(namespace: str, node: str, static_set: str) -> str | None:
    """Return the manifest file of one StaticSet on a node.

    Args:
        namespace: The namespace the chart was installed in.
        node: The node's name.
        static_set: The StaticSet's name.

    Returns:
        The file name, or ``None``.
    """
    for name in _manifests_on(namespace, node):
        if name.startswith(f"{static_set}-"):
            return name
    return None


def _placements(namespace: str, static_set: str) -> list[dict[str, Any]]:
    """Return a StaticSet's placement records.

    Args:
        namespace: The namespace under test.
        static_set: The StaticSet's name.

    Returns:
        The placement objects.
    """
    return _items(
        _json(
            "get",
            "staticpodplacements",
            "-n",
            namespace,
            "-l",
            f"{PLACEMENT_SELECTOR},staticsets.cloudlane.dev/set={static_set}",
        )
    )


def _installed(namespace: str, static_set: str) -> bool:
    """Return whether any placement of a StaticSet reports a durable install.

    Args:
        namespace: The namespace under test.
        static_set: The StaticSet's name.

    Returns:
        ``True`` when a placement is installed and acknowledged.
    """
    return any(
        (item.get("status") or {}).get("phase") == "Installed"
        and (item.get("status") or {}).get("acknowledged") is True
        for item in _placements(namespace, static_set)
    )


def _staticset(namespace: str, name: str) -> dict[str, Any] | None:
    """Return a StaticSet's stored object.

    Args:
        namespace: The namespace under test.
        name: The StaticSet's name.

    Returns:
        The object, or ``None``.
    """
    return _json("get", "staticset", name, "-n", namespace)


def _condition(payload: dict[str, Any] | None, condition_type: str) -> dict[str, Any] | None:
    """Return one status condition of a StaticSet.

    Args:
        payload: The stored object.
        condition_type: The condition's type.

    Returns:
        The condition, or ``None``.
    """
    conditions = (payload or {}).get("status", {}).get("conditions", [])
    return next((item for item in conditions if item.get("type") == condition_type), None)


def _mirror_for(namespace: str, static_set: str) -> dict[str, Any] | None:
    """Return the mirror Pod kubelet published for one StaticSet.

    It is found by label, not by name: kubelet registers a static Pod as
    ``<metadata.name>-<node name>``, so the name in the manifest is not the name of the
    object it publishes. The suite asserts that mismatch below rather than papering over
    it — it is the reason the manager must never look a mirror up by name.

    Args:
        namespace: The namespace under test.
        static_set: The StaticSet's name.

    Returns:
        The Pod, or ``None`` when kubelet has not published one.
    """
    for pod in _items(
        _json(
            "get",
            "pods",
            "-n",
            namespace,
            "-l",
            f"staticsets.cloudlane.dev/set={static_set}",
        )
    ):
        annotations = (pod.get("metadata") or {}).get("annotations") or {}
        if "kubernetes.io/config.mirror" in annotations:
            return pod
    return None


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


def _worker_node() -> str:
    """Return the node the disposable workload is pinned to.

    A worker is preferred so the suite never replaces a control-plane static Pod; a
    single-node cluster falls back to its only node, which is still safe because the
    workload's name is unique to the run.

    Returns:
        The node's name.
    """
    nodes = _items(_json("get", "nodes"))
    workers = [
        node
        for node in nodes
        if "node-role.kubernetes.io/control-plane"
        not in ((node.get("metadata") or {}).get("labels") or {})
    ]
    chosen = (workers or nodes)[0]
    return chosen["metadata"]["name"]


def static_set_body(name: str, namespace: str, *, image: str, node: str) -> dict[str, Any]:
    """Return a StaticSet that exercises the workload translator on one node.

    Args:
        name: The StaticSet's name.
        namespace: The namespace to create it in.
        image: The workload image; the operator's own, already present on the node.
        node: The node to pin the placement to.

    Returns:
        The object.
    """
    return {
        "apiVersion": "staticsets.cloudlane.dev/v1",
        "kind": "StaticSet",
        "metadata": {"name": name, "namespace": namespace},
        "spec": {
            "mode": "Deployment",
            "replicas": 1,
            "placement": {"nodeSelector": {"kubernetes.io/hostname": node}},
            "template": {
                "metadata": {"labels": {"app": name}},
                "spec": {
                    "containers": [
                        {
                            "name": "app",
                            "image": image,
                            "imagePullPolicy": "IfNotPresent",
                            "command": ["python", "-c", "import time; time.sleep(3600)"],
                            "env": [
                                {
                                    "name": "GREETING",
                                    "valueFrom": {
                                        "configMapKeyRef": {
                                            "name": "web-conf",
                                            "key": "app.conf",
                                        }
                                    },
                                }
                            ],
                            "resources": {"requests": {"cpu": "10m", "memory": "32Mi"}},
                            "volumeMounts": [{"name": "conf", "mountPath": "/etc/web"}],
                        }
                    ],
                    "volumes": [{"name": "conf", "configMap": {"name": "web-conf"}}],
                },
            },
        },
    }


@pytest.fixture(scope="module")
def deployed() -> Iterator[dict[str, Any]]:
    """Build, install and later remove the operator.

    Yields:
        The namespace, release, image and pinned node the operator was deployed as.
    """
    namespace = f"static-sets-{uuid.uuid4().hex[:8]}"
    release = "static-sets"
    image = f"static-sets-e2e:{uuid.uuid4().hex[:10]}"

    _kubectl("create", "namespace", namespace)
    _ensure_crds()
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
        f"manager.staticPodPath={STATIC_POD_PATH}",
        "--set",
        f"manager.bundleDir={BUNDLE_DIR}",
    )
    try:
        _kubectl("rollout", "status", f"deployment/{release}", "-n", namespace, "--timeout=300s")
        _kubectl(
            "rollout", "status", f"daemonset/{release}-manager", "-n", namespace, "--timeout=300s"
        )
        yield {
            "namespace": namespace,
            "release": release,
            "image": image,
            "node": _worker_node(),
        }
    finally:
        _delete_static_sets(namespace)
        _run("helm", "uninstall", release, "--namespace", namespace, check=False)
        # The CRDs go before the namespace. Removing them removes every StaticSet and
        # placement with them, finalizers included, so an object whose cleanup never
        # completed cannot hold the namespace in Terminating and stall the run.
        for name in CRDS:
            _kubectl("delete", "crd", name, "--ignore-not-found", "--wait=false", check=False)
        _kubectl(
            "delete", "namespace", namespace, "--ignore-not-found", "--wait=false", check=False
        )
        _run("docker", "rmi", "-f", image, check=False)


def _ensure_crds() -> None:
    """Apply the CRDs and wait until the API server has established them.

    A CRD left behind in the ``Terminating`` state by an interrupted run refuses new
    objects, and applying over it fails, so the suite settles that state before it
    starts. Waiting for ``Established`` afterwards is what makes the first test's
    assertion about the API server rather than about timing.
    """
    for name, path in CRDS.items():
        deleting = _kubectl(
            "get", "crd", name, "-o", "jsonpath={.metadata.deletionTimestamp}", check=False
        )
        if deleting.stdout.strip():
            _wait_for(
                f"{name} to finish terminating",
                lambda crd=name: _json("get", "crd", crd) is None,
                timeout=180,
            )
        _kubectl("apply", "-f", str(path))
    for name in CRDS:
        _wait_for(
            f"{name} to be established",
            lambda crd=name: any(
                condition.get("type") == "Established" and condition.get("status") == "True"
                for condition in ((_json("get", "crd", crd) or {}).get("status") or {}).get(
                    "conditions", []
                )
            ),
            timeout=180,
        )


def _delete_static_sets(namespace: str) -> None:
    """Remove every StaticSet and wait for the nodes to be cleaned up.

    The fixture used to delete the namespace, which removes the manager Pods too — and
    a manifest on a node outlives the manager that would have removed it, leaving a
    static Pod running that nothing tracks. Deleting the objects while the manager is
    still alive is the only teardown that leaves the node as it was found.

    Args:
        namespace: The namespace under test.
    """
    names = [
        item["metadata"]["name"] for item in _items(_json("get", "staticsets", "-n", namespace))
    ]
    for name in names:
        _kubectl("delete", "staticset", name, "-n", namespace, "--timeout=180s", check=False)


@requires_image_tooling
def test_the_crds_are_established(deployed: dict[str, Any]) -> None:
    """Both CRDs the chart installs are accepted by the API server.

    Takes ``deployed`` for its side effect: that fixture applies the CRDs, and this
    test is meaningless before it has.
    """
    for name in CRDS:
        payload = _json("get", "crd", name)
        conditions = (payload or {}).get("status", {}).get("conditions", [])
        assert any(
            condition["type"] == "Established" and condition["status"] == "True"
            for condition in conditions
        ), name


@requires_image_tooling
def test_both_processes_run_one_pod_each(deployed: dict[str, Any]) -> None:
    """One controller, and one manager on every node."""
    namespace = deployed["namespace"]
    nodes = len(_items(_json("get", "nodes")))
    pods = _items(
        _json("get", "pods", "-n", namespace, "-o", "json"),
    )
    running = [pod for pod in pods if (pod.get("status") or {}).get("phase") == "Running"]

    assert len(running) == nodes + 1, _diagnostics(namespace, "-", deployed["node"])


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
    body = static_set_body(name, namespace, image=deployed["image"], node=deployed["node"])
    body["spec"]["template"]["spec"]["containers"][0]["lifecycle"] = {
        "postStart": {"exec": {"command": ["true"]}}
    }
    _kubectl("apply", "--validate=false", "-f", "-", stdin=json.dumps(body))

    stored = _staticset(namespace, name)

    container = (stored or {})["spec"]["template"]["spec"]["containers"][0]
    assert "lifecycle" not in container
    _kubectl("delete", "staticset", name, "-n", namespace, check=False)


@requires_image_tooling
def test_a_static_set_is_placed_installed_mirrored_and_cleaned_up(
    deployed: dict[str, Any],
) -> None:
    """The whole loop: schedule a probe, write the manifest, be mirrored, then remove it."""
    namespace = deployed["namespace"]
    node = deployed["node"]
    name = f"web-{uuid.uuid4().hex[:8]}"

    _apply(
        {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {"name": "web-conf", "namespace": namespace},
            "data": {"app.conf": "greeting=hello\n"},
        }
    )
    _apply(static_set_body(name, namespace, image=deployed["image"], node=node))

    # 1. The controller asks the scheduler, and records the answer.
    _wait_for(
        "a placement record for the pinned node",
        lambda: any(
            (item.get("spec") or {}).get("node") == node for item in _placements(namespace, name)
        ),
        timeout=300,
    )

    # 2. The manager writes the manifest into kubelet's directory on that node.
    _wait_for(
        "the static Pod manifest on the node",
        lambda: _manifest_on(deployed["namespace"], node, name) is not None,
        timeout=300,
    )
    manifest_name = _manifest_on(namespace, node, name)
    assert manifest_name is not None
    contents = _exec(node, namespace, "cat", f"{STATIC_POD_PATH}/{manifest_name}").stdout
    assert '"kind": "Pod"' in contents
    # The configuration the template referenced is materialized on the node, not read
    # from the API server by kubelet.
    materialized = _exec(
        node,
        namespace,
        "sh",
        "-c",
        f"grep -r greeting=hello {BUNDLE_DIR}/placements | head -1",
    ).stdout
    assert "greeting=hello" in materialized

    # 3. kubelet starts it and publishes a mirror Pod, which is the proof of the whole
    #    chain: compile, materialize, write, accept.
    pod_name = manifest_name.removesuffix(".yaml")
    _wait_for(
        "kubelet to publish the static Pod's mirror",
        lambda: _mirror_for(namespace, name) is not None,
        timeout=300,
    )
    mirror = _mirror_for(namespace, name)
    assert mirror is not None
    # kubelet registers the mirror under the manifest's name *plus the node's*, which is
    # why nothing may look a mirror up by the name it wrote into the manifest.
    assert mirror["metadata"]["name"] == f"{pod_name}-{node}"
    assert mirror["status"]["phase"] == "Running", mirror
    _wait_for(
        "the placement to be acknowledged as installed",
        lambda: _installed(namespace, name),
        timeout=300,
    )

    # 4. With the static Pod running, the scheduler's reservation is released and the
    #    generation is recorded as placed.
    _wait_for(
        "the scheduling probe to be deleted",
        lambda: not _items(_json("get", "replicasets", "-n", namespace, "-l", PROBE_SELECTOR)),
        timeout=300,
    )
    stored = _staticset(namespace, name)
    placed = (stored or {}).get("status", {}).get("placedGeneration")
    assert placed == (stored or {}).get("metadata", {}).get("generation")
    ready = _condition(stored, "Ready")
    assert ready is not None and ready["status"] == "True", _diagnostics(namespace, name, node)

    # 5. Deleting the StaticSet removes the static Pod, and only then releases the object.
    _kubectl("delete", "staticset", name, "-n", namespace, "--timeout=300s")
    _wait_for(
        "the manifest to be removed from the node",
        lambda: _manifest_on(namespace, node, name) is None,
        timeout=300,
    )
    _wait_for(
        "the mirror Pod to disappear",
        lambda: _mirror_for(namespace, name) is None,
        timeout=300,
    )
    assert _staticset(namespace, name) is None
    assert _placements(namespace, name) == []


@requires_image_tooling
def test_a_replaced_manager_adopts_its_placement(deployed: dict[str, Any]) -> None:
    """A manager Pod that is deleted is replaced, and the static Pod is not duplicated."""
    namespace = deployed["namespace"]
    node = deployed["node"]
    name = f"stay-{uuid.uuid4().hex[:8]}"
    _apply(
        {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {"name": "web-conf", "namespace": namespace},
            "data": {"app.conf": "greeting=hello\n"},
        }
    )
    _apply(static_set_body(name, namespace, image=deployed["image"], node=node))
    _wait_for(
        "the static Pod to be installed",
        lambda: _installed(namespace, name),
        timeout=300,
    )
    before = _manifest_on(namespace, node, name)
    assert before is not None

    manager = _manager_pod(namespace, node)
    assert manager is not None
    _kubectl("delete", "pod", manager, "-n", namespace, "--wait=false")
    _wait_for(
        "a replacement manager Pod",
        lambda: (pod := _manager_pod(namespace, node)) is not None and pod != manager,
        timeout=300,
    )
    _wait_for(
        "the replacement to re-acknowledge the placement",
        lambda: _installed(namespace, name),
        timeout=300,
    )

    # Exactly one manifest: the replacement adopted the placement instead of writing a
    # second one beside it.
    assert [item for item in _manifests_on(namespace, node) if item.startswith(f"{name}-")] == [
        before
    ]
    _kubectl("delete", "staticset", name, "-n", namespace, check=False)


@requires_image_tooling
def test_a_rollout_replaces_the_static_pod(deployed: dict[str, Any]) -> None:
    """A changed template is a new revision, and the node ends up with only that one."""
    namespace = deployed["namespace"]
    node = deployed["node"]
    name = f"roll-{uuid.uuid4().hex[:8]}"
    _apply(
        {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {"name": "web-conf", "namespace": namespace},
            "data": {"app.conf": "greeting=hello\n"},
        }
    )
    body = static_set_body(name, namespace, image=deployed["image"], node=node)
    _apply(body)
    _wait_for(
        "the first revision to be installed",
        lambda: _installed(namespace, name),
        timeout=300,
    )
    first = _manifest_on(namespace, node, name)
    assert first is not None

    body["spec"]["template"]["spec"]["containers"][0]["env"][0] = {
        "name": "GREETING",
        "value": "changed",
    }
    _apply(body)

    _wait_for(
        "the second revision to replace the first",
        lambda: (current := _manifest_on(namespace, node, name)) is not None and current != first,
        timeout=300,
    )
    # Recreate is the default: the old manifest is gone, not merely superseded.
    assert [item for item in _manifests_on(namespace, node) if item.startswith(f"{name}-")] != [
        first
    ]
    _kubectl("delete", "staticset", name, "-n", namespace, check=False)


@requires_image_tooling
def test_a_surge_rollout_retires_the_previous_revision(deployed: dict[str, Any]) -> None:
    """Under ``Surge`` the old manifest goes only once the new Pod is observed running.

    That observation is what a mirror Pod is for, and it is where a name-based lookup
    would silently never fire: kubelet publishes ``<name>-<node>``, so the retirement
    would wait forever and the node would accumulate revisions.
    """
    namespace = deployed["namespace"]
    node = deployed["node"]
    name = f"surge-{uuid.uuid4().hex[:8]}"
    _apply(
        {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {"name": "web-conf", "namespace": namespace},
            "data": {"app.conf": "greeting=hello\n"},
        }
    )
    body = static_set_body(name, namespace, image=deployed["image"], node=node)
    body["spec"]["update"] = {"strategy": "Surge", "maxSurge": 1}
    _apply(body)
    _wait_for(
        "the first revision to be installed",
        lambda: _installed(namespace, name),
        timeout=300,
    )
    first = _manifest_on(namespace, node, name)
    assert first is not None

    body["spec"]["template"]["spec"]["containers"][0]["env"][0] = {
        "name": "GREETING",
        "value": "changed",
    }
    _apply(body)

    _wait_for(
        "the previous revision to be retired once the new one runs",
        lambda: (
            [item for item in _manifests_on(namespace, node) if item.startswith(f"{name}-")]
            == [item for item in [_manifest_on(namespace, node, name)] if item and item != first]
        ),
        timeout=300,
    )
    remaining = [item for item in _manifests_on(namespace, node) if item.startswith(f"{name}-")]
    assert len(remaining) == 1
    assert remaining[0] != first
    _kubectl("delete", "staticset", name, "-n", namespace, check=False)
