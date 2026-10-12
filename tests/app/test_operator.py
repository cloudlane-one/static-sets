"""The controller adapter: snapshot reading, plan application and admission.

The entry point is loaded the way ``kopf run`` loads it and the way
``based-operators scaffold`` loads it, which proves in one step that the file really
is a deployable operator entry point: the framework finds exactly the StaticSet
resource it declares.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import kopf
import pytest
from based_operators.scaffold import discover_resources, load_operator

import static_sets_operator as operator

REPO_ROOT = Path(__file__).resolve().parents[2]
ENTRY_POINT = REPO_ROOT / "app" / "static_sets_operator.py"
NAMESPACE = "edge"
SET_NAME = "web"

LOGGER = logging.getLogger("static-sets-test")


class FakeJsonClient:
    """A JSON client that answers from a path-keyed table and records calls."""

    def __init__(self, answers: Mapping[str, Any] | None = None) -> None:
        """Build the client.

        Args:
            answers: Path to response mapping.
        """
        self.answers = dict(answers or {})
        self.calls: list[tuple[str, str, Any]] = []

    def json(
        self,
        method: str,
        path: str,
        *,
        body: Any = None,
        params: Mapping[str, str] | None = None,
        missing: Any = None,
    ) -> Any:
        """Answer one call, honouring the caller's ``missing`` sentinel.

        Args:
            method: The HTTP method.
            path: The API path.
            body: The request body.
            params: Query parameters.
            missing: The value to return when the path is unknown.

        Returns:
            The recorded answer.
        """
        del params
        self.calls.append((method, path, body))
        if path in self.answers:
            return self.answers[path]
        return missing

    def written(self, method: str) -> list[Any]:
        """Return the bodies written with one method.

        Args:
            method: The HTTP method to filter on.

        Returns:
            The bodies.
        """
        return [body for recorded, _, body in self.calls if recorded == method]


def pods_path() -> str:
    """Return the Pods collection path.

    Returns:
        The path.
    """
    return f"/api/v1/namespaces/{NAMESPACE}/pods"


def config_maps_path() -> str:
    """Return the ConfigMaps collection path.

    Returns:
        The path.
    """
    return f"/api/v1/namespaces/{NAMESPACE}/configmaps"


def deployment_path(name: str) -> str:
    """Return a Deployment's path.

    Args:
        name: The Deployment's name.

    Returns:
        The path.
    """
    return f"/apis/apps/v1/namespaces/{NAMESPACE}/deployments/{name}"


def test_entry_point_is_a_deployable_operator() -> None:
    """The scaffolder finds exactly the StaticSet the entry point declares."""
    module = load_operator(ENTRY_POINT)

    resources = discover_resources(module)

    assert [resource.__name__ for resource in resources] == ["StaticSet"]
    assert resources[0]._require_resource_config().crd_name == (
        "staticsets.staticsets.cloudlane.dev"
    )


def test_selector_renders_a_stable_label_selector() -> None:
    """Selectors are sorted, so a reconcile is comparable between runs."""
    assert operator.selector(b="2", a="1") == "a=1,b=2"


def test_read_snapshot_maps_every_observed_kind(make_static_set: Any) -> None:
    """Pods, nodes and placement ConfigMaps become the planner's input types."""
    identity = "abc123"
    client = FakeJsonClient(
        {
            operator.NODES_PATH: {
                "items": [
                    {
                        "metadata": {"name": "node-a", "labels": {"role": "edge"}},
                        "spec": {},
                        "status": {"conditions": [{"type": "Ready", "status": "True"}]},
                    },
                    {
                        "metadata": {"name": "node-b", "labels": {}},
                        "spec": {"unschedulable": True},
                        "status": {"conditions": [{"type": "Ready", "status": "False"}]},
                    },
                ]
            },
            pods_path(): {
                "items": [
                    {
                        "metadata": {
                            "name": "web-installer-1",
                            "uid": "u1",
                            "labels": {"app.kubernetes.io/component": "installer"},
                        },
                        "spec": {"nodeName": "node-a"},
                        "status": {"phase": "Running"},
                    },
                    {
                        "metadata": {
                            "name": f"web-{identity}",
                            "labels": {"app.kubernetes.io/component": "workload"},
                            "annotations": {"kubernetes.io/config.mirror": "hash"},
                        },
                        "spec": {"nodeName": "node-a"},
                        "status": {
                            "phase": "Running",
                            "containerStatuses": [{"ready": True, "restartCount": 2}],
                        },
                    },
                ]
            },
            config_maps_path(): {
                "items": [
                    {
                        "data": {
                            "placement": identity,
                            "node": "node-a",
                            "revision": "r-1",
                            "state": "Installed",
                            "acknowledged": "true",
                        }
                    }
                ]
            },
        }
    )

    state = operator.read_snapshot(client, make_static_set(spec={"replicas": 1}))

    assert state.synced is True
    assert [(node.name, node.ready, node.unschedulable) for node in state.nodes] == [
        ("node-a", True, False),
        ("node-b", False, True),
    ]
    assert [pod.node for pod in state.installer_pods] == ["node-a"]
    assert state.placements[0].identity == identity
    assert state.placements[0].acknowledged is True
    assert state.workloads[0].ready is True
    assert state.workloads[0].restart_count == 2


def test_read_snapshot_skips_a_mirror_pod_as_an_installer(make_static_set: Any) -> None:
    """A mirror Pod is never mistaken for a live installer."""
    client = FakeJsonClient(
        {
            pods_path(): {
                "items": [
                    {
                        "metadata": {
                            "name": "web-abc",
                            "labels": {"app.kubernetes.io/component": "installer"},
                            "annotations": {"kubernetes.io/config.mirror": "hash"},
                        },
                        "spec": {"nodeName": "node-a"},
                    }
                ]
            }
        }
    )

    state = operator.read_snapshot(client, make_static_set())

    assert state.installer_pods == ()
    assert len(state.workloads) == 1


def test_apply_plan_creates_the_installer_workload(make_static_set: Any) -> None:
    """A first reconcile POSTs the installer Deployment."""
    client = FakeJsonClient()
    resource = make_static_set(spec={"replicas": 1})

    result = operator.apply_plan(
        client,
        resource,
        operator.ObservedState(observed_at="2026-10-12T10:00:00Z"),
        NAMESPACE,
    )

    assert result.workload is not None
    posted = client.written("POST")
    assert len(posted) == 1
    assert posted[0]["kind"] == "Deployment"
    assert posted[0]["spec"]["replicas"] == 1


def test_apply_plan_replaces_an_existing_workload(make_static_set: Any) -> None:
    """An existing workload is replaced in place, carrying its resource version."""
    name = "web-installer-deployment"
    client = FakeJsonClient(
        {deployment_path(name): {"metadata": {"name": name, "resourceVersion": "42"}}}
    )
    resource = make_static_set(spec={"replicas": 1})

    operator.apply_plan(client, resource, operator.ObservedState(observed_at="x"), NAMESPACE)

    put = client.written("PUT")
    assert put[0]["metadata"]["resourceVersion"] == "42"


def test_apply_plan_publishes_a_cleanup_intent_and_a_worker(make_static_set: Any) -> None:
    """An orphaned placement gets an intent ConfigMap and a pinned worker Pod."""
    from static_sets.placement import ObservedPlacement

    client = FakeJsonClient()
    resource = make_static_set(spec={"replicas": 1})
    state = operator.ObservedState(
        nodes=(operator.NodeInfo(name="node-a", labels={}, ready=True),),
        placements=(
            ObservedPlacement(
                identity="abc123",
                node="node-a",
                revision="r-1",
                state="Installed",
                acknowledged=True,
            ),
        ),
        observed_at="2026-10-12T10:00:00Z",
    )

    operator.apply_plan(client, resource, state, NAMESPACE)

    posted = client.written("POST")
    kinds = sorted(item["kind"] for item in posted)
    assert kinds == ["ConfigMap", "Deployment", "Pod"]
    config_map = next(item for item in posted if item["kind"] == "ConfigMap")
    assert config_map["data"]["cleanup"] == "true"
    worker = next(item for item in posted if item["kind"] == "Pod")
    assert worker["spec"]["nodeName"] == "node-a"


def test_apply_plan_deletes_both_installer_kinds(make_static_set: Any) -> None:
    """Deletion clears a Deployment and a DaemonSet, so a mode switch leaves nothing."""
    client = FakeJsonClient(
        {
            f"/apis/apps/v1/namespaces/{NAMESPACE}/deployments": {
                "items": [{"metadata": {"name": "web-installer-deployment"}}]
            },
            f"/apis/apps/v1/namespaces/{NAMESPACE}/daemonsets": {
                "items": [{"metadata": {"name": "web-installer-daemonset"}}]
            },
        }
    )
    resource = make_static_set(
        spec={"replicas": 1}, metadata={"deletionTimestamp": "2026-10-12T11:00:00Z"}
    )

    operator.apply_plan(client, resource, operator.ObservedState(observed_at="x"), NAMESPACE)

    deleted = [path for method, path, _ in client.calls if method == "DELETE"]
    assert len(deleted) == 2
    assert any(path.endswith("web-installer-deployment") for path in deleted)
    assert any(path.endswith("web-installer-daemonset") for path in deleted)


def test_publish_status_writes_every_reported_field(make_static_set: Any) -> None:
    """Status is published field by field, so a merge patch never drops a key."""
    from static_sets.controller import ObservedState, plan

    resource = make_static_set(spec={"replicas": 1})
    result = plan(resource, ObservedState(observed_at="2026-10-12T10:00:00Z"), namespace=NAMESPACE)
    patch = kopf.Patch()

    operator.publish_status(patch, result)

    written = dict(patch.status)
    assert set(written) >= {
        "observedGeneration",
        "placements",
        "health",
        "cleanup",
        "conditions",
    }
    assert written["observedGeneration"] == 3
    assert isinstance(written["conditions"], list)


def test_ensure_finalizer_adds_it_once(make_static_set: Any) -> None:
    """The finalizer is added, and an object that already has it is left alone."""
    patch = kopf.Patch()

    operator.ensure_finalizer(patch, make_static_set())

    assert dict(patch.metadata)["finalizers"] == ["staticsets.cloudlane.dev/static-set"]

    already = make_static_set(
        metadata={"finalizers": ["staticsets.cloudlane.dev/static-set", "other"]}
    )
    untouched = kopf.Patch()

    operator.ensure_finalizer(untouched, already)

    assert dict(untouched.metadata) == {}


def test_reject_unsupported_accepts_a_valid_object(make_resource: Any) -> None:
    """A well-formed object passes admission."""
    operator.reject_unsupported(make_resource())


def test_reject_unsupported_refuses_an_unknown_field(make_resource: Any) -> None:
    """An unknown field is refused instead of being pruned."""
    body = make_resource()
    body["spec"]["template"]["spec"]["containers"][0]["lifecycle"] = {}

    with pytest.raises(kopf.AdmissionError) as error:
        operator.reject_unsupported(body)

    assert "lifecycle" in str(error.value)


def test_reconcile_retries_when_the_api_is_unreachable(
    make_static_set: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unreachable API is retried, never interpreted as an empty desired set."""
    from static_sets.installer import ApiUnavailableError

    class Broken(FakeJsonClient):
        def json(self, method: str, path: str, **kwargs: Any) -> Any:
            raise ApiUnavailableError("connection refused")

    monkeypatch.setattr(operator, "get_client", lambda: Broken())
    patch = kopf.Patch()

    with pytest.raises(kopf.TemporaryError):
        operator.reconcile(make_static_set(), patch, LOGGER)

    assert dict(patch.status) == {}
    assert dict(patch.metadata) == {}


def test_reconcile_applies_and_records_status(
    make_static_set: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A successful reconcile writes the workload and the status."""
    client = FakeJsonClient()
    monkeypatch.setattr(operator, "get_client", lambda: client)
    patch = kopf.Patch()

    operator.reconcile(make_static_set(spec={"replicas": 1}), patch, LOGGER)

    assert any(method == "POST" for method, _, _ in client.calls)
    assert dict(patch.status)["observedGeneration"] == 3
    assert dict(patch.metadata)["finalizers"] == ["staticsets.cloudlane.dev/static-set"]


def test_finalize_releases_the_finalizer_only_when_cleanup_is_acknowledged(
    make_static_set: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pending cleanup keeps the finalizer, so the object is not released early."""
    client = FakeJsonClient(
        {
            config_maps_path(): {
                "items": [{"data": {"placement": "abc", "node": "node-a", "state": "Installed"}}]
            }
        }
    )
    monkeypatch.setattr(operator, "get_client", lambda: client)
    resource = make_static_set(
        spec={"replicas": 1},
        metadata={
            "deletionTimestamp": "2026-10-12T11:00:00Z",
            "finalizers": ["staticsets.cloudlane.dev/static-set"],
        },
    )

    with pytest.raises(kopf.TemporaryError):
        operator.finalize(resource, kopf.Patch(), LOGGER)


def test_finalize_releases_the_finalizer_when_cleanup_is_done(
    make_static_set: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An acknowledged cleanup drops the finalizer, letting the object go."""
    client = FakeJsonClient()
    monkeypatch.setattr(operator, "get_client", lambda: client)
    resource = make_static_set(
        spec={"replicas": 1},
        metadata={
            "deletionTimestamp": "2026-10-12T11:00:00Z",
            "finalizers": ["staticsets.cloudlane.dev/static-set", "other"],
        },
    )
    patch = kopf.Patch()

    operator.finalize(resource, patch, LOGGER)

    assert dict(patch.metadata)["finalizers"] == ["other"]


ADMISSION_PROBE = (
    "import runpy, kopf;"
    "runpy.run_path('app/static_sets_operator.py');"
    "print(len(kopf.get_default_registry()._webhooks.get_all_handlers()))"
)
"""Counts the admission handlers a fresh interpreter registers on import."""


def admission_handler_count(webhook: str | None) -> int:
    """Import the entry point in a fresh interpreter and count its admission handlers.

    Registration happens at import time, so it cannot be observed in this process:
    the module is already in ``sys.modules`` and its registry already populated.

    Args:
        webhook: The value of the operator's webhook environment variable, or
            ``None`` to leave it unset.

    Returns:
        The number of admission handlers the import registered.
    """
    environment = dict(os.environ)
    environment.pop(operator.WEBHOOK_ENV, None)
    if webhook is not None:
        environment[operator.WEBHOOK_ENV] = webhook
    environment["PYTHONPATH"] = os.pathsep.join([str(REPO_ROOT / "src"), str(REPO_ROOT / "app")])
    result = subprocess.run(
        [sys.executable, "-c", ADMISSION_PROBE],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        env=environment,
        timeout=300,
    )
    assert result.returncode == 0, result.stderr
    return int(result.stdout.strip())


def test_the_admission_handler_is_registered_only_while_admissions_are_served() -> None:
    """kopf refuses to start when admission handlers exist with nothing to serve them.

    The chart's default deployment does not serve admissions, so registering the
    handler unconditionally leaves the controller in a crash loop — which the
    end-to-end suite caught and no unit test could, because the handler function
    itself behaves correctly either way. This pins the registration, not the handler.
    """
    assert admission_handler_count(None) == 0
    assert admission_handler_count("0") == 0
    assert admission_handler_count("1") == 1
