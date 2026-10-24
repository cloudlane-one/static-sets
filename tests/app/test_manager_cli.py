"""The ``static-set-manager`` command: one pass over a node's placements."""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast

import pytest

import static_sets_manager as manager_cli
from static_sets.api import KubernetesClient
from static_sets.errors import ApiUnavailableError
from static_sets.manager import MirrorPod
from static_sets.models import StaticSet
from static_sets.naming import placement_identity
from static_sets.placements import StaticPodPlacement

UID = "0f9a4b6c-1111-2222-3333-444455556666"

CLIENT = cast("KubernetesClient", object())
"""A client the fake replaces: the command never touches it, the API does."""
NODE = "worker-1"


class FakeApi:
    """A manager API plus the two methods the command itself needs."""

    def __init__(self, static_set: StaticSet, placements: list[StaticPodPlacement]) -> None:
        """Bind the fake to a StaticSet and the placements it can serve.

        Args:
            static_set: The StaticSet the placements belong to.
            placements: The placement records the cluster holds.
        """
        self.static_set = static_set
        self.placements = placements
        self.published: list[tuple[str, dict[str, str]]] = []
        self.offline = False

    def list_placements(self, node: str) -> list[StaticPodPlacement]:
        """Return the placements that name one node."""
        self._check()
        return [item for item in self.placements if item.spec.node == node]

    def identity_of(self, placement: StaticPodPlacement) -> str:
        """Return the placement's identity."""
        return placement_identity(placement.spec.static_set_uid, placement.spec.node)

    def publish_status(self, namespace: str, name: str, payload: Mapping[str, str]) -> None:
        """Record a published status."""
        self._check()
        self.published.append((name, dict(payload)))

    def get_static_set(self, namespace: str, name: str) -> StaticSet | None:
        """Return the StaticSet."""
        self._check()
        return self.static_set

    def get_config_map(self, namespace: str, name: str) -> Mapping[str, bytes] | None:
        """Return a ConfigMap."""
        self._check()
        return {"app.conf": b"greeting=hello\n"}

    def get_secret(self, namespace: str, name: str) -> Mapping[str, bytes] | None:
        """Return a Secret, when one exists."""
        self._check()
        return None

    def get_pod(self, namespace: str, name: str) -> MirrorPod | None:
        """Return no mirror Pod: kubelet has not published one in this fake."""
        self._check()
        return None

    def _check(self) -> None:
        """Fail the way the client fails when the API is unreachable."""
        if self.offline:
            raise ApiUnavailableError("connection refused")


@pytest.fixture()
def environment(temp_roots: tuple[Path, Path]) -> dict[str, str]:
    """Return a manager environment pointing at disposable host paths.

    Args:
        temp_roots: The disposable (manifest, bundle) pair.

    Returns:
        The environment.
    """
    manifests, bundle = temp_roots
    return {
        "STATIC_SETS_NODE": NODE,
        "STATIC_SETS_MANAGER_POD": "manager-1",
        "STATIC_SETS_STATIC_POD_PATH": str(manifests).replace("\\", "/"),
        "STATIC_SETS_BUNDLE_DIR": str(bundle).replace("\\", "/"),
    }


def install_fake(monkeypatch: pytest.MonkeyPatch, api: FakeApi) -> None:
    """Make the command use a fake cluster API.

    Args:
        monkeypatch: Pytest's monkeypatch fixture.
        api: The fake API.
    """
    monkeypatch.setattr(manager_cli, "ClusterApi", lambda _client: api)


def test_one_pass_reconciles_the_placements_that_name_the_node(
    make_static_set: Any,
    make_placement: Any,
    environment: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A pass installs the node's placements and reports them as installed."""
    api = FakeApi(make_static_set(), [make_placement()])
    install_fake(monkeypatch, api)

    status = manager_cli.run(
        environ=environment, client=CLIENT, interval=0, now="2026-10-24T10:00:00+00:00"
    )

    assert status == 0
    assert [name for name, _ in api.published] == ["web-e6383089b2"]
    payload = api.published[0][1]
    assert payload["phase"] == "Installed"
    assert payload["acknowledged"] == "true"
    assert payload["observedGeneration"] == "3"
    assert payload["identity"] == placement_identity(UID, NODE)
    events = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert events[0]["event"] == "reconciled"


def test_placements_of_other_nodes_are_left_alone(
    make_static_set: Any,
    make_placement: Any,
    environment: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Another node's placement is another manager's business."""
    api = FakeApi(
        make_static_set(),
        [make_placement(), make_placement(spec={"node": "worker-2"})],
    )
    install_fake(monkeypatch, api)

    manager_cli.run(environ=environment, client=CLIENT, interval=0)

    assert len(api.published) == 1


def test_a_cleanup_request_is_carried_out(
    make_static_set: Any,
    make_placement: Any,
    environment: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A placement that says ``desired: false`` is removed, not installed."""
    placement = make_placement(
        spec={"desired": False, "reason": "ScaledDown"}, status={"revision": "r-1"}
    )
    api = FakeApi(make_static_set(), [placement])
    install_fake(monkeypatch, api)

    manager_cli.run(environ=environment, client=CLIENT, interval=0)

    assert api.published[0][1]["phase"] == "Cleaned"


def test_an_unreachable_api_is_not_a_failure(
    make_static_set: Any,
    make_placement: Any,
    environment: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Being offline is a state to work through, not a verdict about the set."""
    api = FakeApi(make_static_set(), [make_placement()])
    install_fake(monkeypatch, api)
    api.offline = True

    status = manager_cli.run(environ=environment, client=CLIENT, interval=0)

    assert status == 0
    assert api.published == []
    assert json.loads(capsys.readouterr().out.splitlines()[0])["event"] == "offline"


def test_a_refusal_is_reported_on_the_placement(
    make_static_set: Any,
    make_placement: Any,
    environment: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A compilation refusal is published instead of being swallowed."""
    api = FakeApi(make_static_set(), [make_placement()])
    install_fake(monkeypatch, api)

    def refuse(*_: Any, **__: Any) -> Any:
        from static_sets.errors import TranslationError

        raise TranslationError("volumes[].hostPath is not supported")

    monkeypatch.setattr(manager_cli, "reconcile", refuse)

    status = manager_cli.run(environ=environment, client=CLIENT, interval=0)

    assert status == 1
    assert api.published[0][1]["phase"] == "Failed"
    assert "hostPath" in (api.published[0][1]["message"] or "")


def test_a_missing_node_configuration_stops_the_command(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Without a node the manager would not know which host it serves."""
    status = manager_cli.run(environ={}, client=CLIENT, interval=0)

    assert status == 2
    assert json.loads(capsys.readouterr().out.splitlines()[0])["event"] == "invalid-environment"


def test_the_node_may_be_given_on_the_command_line(
    make_static_set: Any,
    make_placement: Any,
    environment: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--node`` overrides the downward API, which is what a manual pass needs."""
    api = FakeApi(make_static_set(), [make_placement()])
    install_fake(monkeypatch, api)

    manager_cli.run(
        environ={**environment, "STATIC_SETS_NODE": "elsewhere"},
        client=CLIENT,
        interval=0,
        node=NODE,
    )

    assert len(api.published) == 1
