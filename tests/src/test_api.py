"""The Kubernetes client wrapper and strict admission validation.

The wrapper adds three things to the official client — failure classification, a
timeout, and absence-as-``None`` — so those are what is tested. Transport,
authentication, serialization and the API groups themselves are the official
client's job; a throwaway HTTP server is enough to prove the wiring reaches it.
"""

from __future__ import annotations

import http.server
import json
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast

import pytest
from kubernetes.client import Configuration, V1ConfigMap, V1Node, V1ObjectMeta

from static_sets.api import ApiRequestError, KubernetesClient, or_none, upsert
from static_sets.errors import ApiUnavailableError
from static_sets.validation import strict_problems


class _Answers:
    """The canned responses a test server returns, keyed by path."""

    def __init__(self) -> None:
        self.answers: dict[str, tuple[int, object]] = {}
        self.requests: list[tuple[str, str, dict[str, str]]] = []
        self.url = ""


def _handler(answers: _Answers) -> type[http.server.BaseHTTPRequestHandler]:
    """Return a handler class that answers from a shared table.

    Args:
        answers: The answers and the request log to use.

    Returns:
        The handler class.
    """

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            headers = {key.lower(): value for key, value in self.headers.items()}
            answers.requests.append(("GET", self.path, headers))
            status, body = answers.answers.get(self.path, (404, {"reason": "NotFound"}))
            payload = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - inherited signature
            del format, args

    return Handler


@pytest.fixture()
def answers() -> Iterator[_Answers]:
    """Serve a table of canned answers on a loopback port.

    Yields:
        The answers object, so a test can fill in responses and inspect requests.
    """
    canned = _Answers()
    server = http.server.HTTPServer(("127.0.0.1", 0), _handler(canned))
    canned.url = f"http://127.0.0.1:{server.server_port}"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield canned
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.fixture()
def client(answers: _Answers) -> KubernetesClient:
    """Return a client pointed at the test server.

    Args:
        answers: The fixture that owns the server URL.

    Returns:
        The client.
    """
    return KubernetesClient(Configuration(host=answers.url), timeout=5)


def test_a_list_returns_the_clients_typed_models(
    answers: _Answers, client: KubernetesClient
) -> None:
    """A listing is a list of the official models, not of dictionaries."""
    answers.answers["/api/v1/nodes"] = (
        200,
        {
            "apiVersion": "v1",
            "kind": "NodeList",
            "metadata": {},
            "items": [{"metadata": {"name": "node-a"}}],
        },
    )

    nodes = client.core.list_node().items or []

    assert len(nodes) == 1
    assert isinstance(nodes[0], V1Node)
    assert nodes[0].metadata is not None
    assert nodes[0].metadata.name == "node-a"


def test_an_absent_config_map_is_none(answers: _Answers, client: KubernetesClient) -> None:
    """A 404 on a read that expects absence is an answer, not a failure."""
    assert or_none(client.core.read_namespaced_config_map, "absent", "edge") is None


def test_or_none_still_raises_a_refusal(answers: _Answers, client: KubernetesClient) -> None:
    """Only absence is turned into a value; any other refusal is still an error."""
    answers.answers["/api/v1/nodes"] = (403, {"reason": "Forbidden"})

    with pytest.raises(ApiRequestError):
        or_none(client.core.list_node)


def test_a_refusal_is_reported_with_its_status(answers: _Answers, client: KubernetesClient) -> None:
    """A 403 is surfaced with enough detail to diagnose it."""
    answers.answers["/api/v1/nodes"] = (403, {"reason": "Forbidden"})

    with pytest.raises(ApiRequestError) as error:
        client.core.list_node()

    assert error.value.status == 403
    assert "403" in str(error.value)
    assert "Forbidden" in str(error.value)


def test_an_unreachable_server_is_reported_as_unavailable() -> None:
    """A refused connection means the API is unreachable, not that it refused."""
    closed = Configuration(host="http://127.0.0.1:1")

    with pytest.raises(ApiUnavailableError):
        KubernetesClient(closed, timeout=1).core.list_node()


def test_the_bearer_token_is_sent(answers: _Answers) -> None:
    """The client authenticates with the projected service-account token."""
    answers.answers["/api/v1/nodes"] = (
        200,
        {"apiVersion": "v1", "kind": "NodeList", "metadata": {}, "items": []},
    )
    configuration = Configuration(host=answers.url)
    configuration.api_key["BearerToken"] = "bearer t0ken"

    KubernetesClient(configuration, timeout=5).core.list_node()

    assert answers.requests[0][2]["authorization"] == "bearer t0ken"


def test_from_environment_discovers_the_in_cluster_endpoint() -> None:
    """The service host and port produce the API server URL."""
    built = KubernetesClient.from_environment(
        {"KUBERNETES_SERVICE_HOST": "10.0.0.1", "KUBERNETES_SERVICE_PORT": "6443"}
    )

    assert built.configuration.host == "https://10.0.0.1:6443"
    assert built.configuration.api_key.get("BearerToken") is None


def test_from_environment_refuses_without_a_server() -> None:
    """Without a service host there is nothing to talk to."""
    with pytest.raises(ApiUnavailableError):
        KubernetesClient.from_environment({})


def test_from_environment_reads_the_token_and_ca_files(tmp_path: Path) -> None:
    """The projected service-account token and CA are picked up from disk."""
    directory = tmp_path / "sa"
    directory.mkdir()
    # A projected token file carries the token and nothing else, not even a newline.
    (directory / "token").write_text("secret-token", encoding="utf-8")
    (directory / "ca.crt").write_text("ca", encoding="utf-8")

    built = KubernetesClient.from_environment(
        {
            "KUBERNETES_SERVICE_HOST": "10.0.0.1",
            "STATIC_SETS_SERVICE_ACCOUNT_DIR": str(directory),
        }
    )

    assert built.configuration.api_key["BearerToken"] == "bearer secret-token"
    assert built.configuration.ssl_ca_cert == str(directory / "ca.crt")


class FakeCore:
    """A ConfigMap API group that records writes and can pretend to be empty."""

    def __init__(self, existing: V1ConfigMap | None = None) -> None:
        """Build the fake.

        Args:
            existing: The object reads return, or ``None`` to answer 404.
        """
        self.existing = existing
        self.created: list[V1ConfigMap] = []
        self.replaced: list[V1ConfigMap] = []

    def read_namespaced_config_map(self, name: str, namespace: str, **_: Any) -> V1ConfigMap:
        """Return the configured object, or answer 404.

        Args:
            name: The ConfigMap's name.
            namespace: The ConfigMap's namespace.
            **_: Further client arguments.

        Returns:
            The existing object.

        Raises:
            ApiRequestError: If the fake was built empty.
        """
        del name, namespace
        if self.existing is None:
            raise ApiRequestError("ConfigMap does not exist", status=404)
        return self.existing

    def create_namespaced_config_map(
        self, namespace: str, body: V1ConfigMap, **_: Any
    ) -> V1ConfigMap:
        """Record a creation.

        Args:
            namespace: The namespace.
            body: The object to create.
            **_: Further client arguments.

        Returns:
            The created object.
        """
        del namespace
        self.created.append(body)
        return body

    def replace_namespaced_config_map(
        self, name: str, namespace: str, body: V1ConfigMap, **_: Any
    ) -> V1ConfigMap:
        """Record a replacement.

        Args:
            name: The ConfigMap's name, which the generated signature puts first.
            namespace: The namespace.
            body: The object to replace with.
            **_: Further client arguments.

        Returns:
            The replaced object.
        """
        del namespace
        assert body.metadata is not None and body.metadata.name == name
        self.replaced.append(body)
        return body


def fake_core_client(core: FakeCore) -> KubernetesClient:
    """Return a client whose ConfigMap API group is a fake.

    Args:
        core: The fake to install.

    Returns:
        The client.
    """
    client = KubernetesClient(Configuration(host="https://api.test"))
    client.core = cast(Any, core)
    return client


def test_upsert_creates_an_object_that_is_absent() -> None:
    """A first upsert is a creation."""
    core = FakeCore()
    client = fake_core_client(core)

    upsert(
        client,
        "edge",
        V1ConfigMap(metadata=V1ObjectMeta(name="web-placement-id1", namespace="edge")),
    )

    assert [(item.metadata.name if item.metadata else None) for item in core.created] == [
        "web-placement-id1"
    ]
    assert core.replaced == []


def test_upsert_replaces_an_existing_object_with_its_resource_version() -> None:
    """A later upsert is a replacement that carries the version it read."""
    existing = V1ConfigMap(
        metadata=V1ObjectMeta(name="web-placement-id1", namespace="edge", resource_version="42")
    )
    core = FakeCore(existing)
    client = fake_core_client(core)

    upsert(
        client,
        "edge",
        V1ConfigMap(metadata=V1ObjectMeta(name="web-placement-id1", namespace="edge")),
    )

    assert core.created == []
    assert core.replaced[0].metadata is not None
    assert core.replaced[0].metadata.resource_version == "42"


def test_strict_problems_accepts_a_valid_body(make_resource: Any) -> None:
    """A well-formed StaticSet has no problems."""
    assert strict_problems(make_resource()) == []


def test_strict_problems_names_the_unknown_field(make_resource: Any) -> None:
    """An unknown field is reported with its path, so the message is actionable."""
    body = make_resource()
    body["spec"]["template"]["spec"]["containers"][0]["lifecycle"] = {"postStart": {}}

    problems = strict_problems(body)

    assert len(problems) == 1
    assert "spec.template.spec.containers.0.lifecycle" in problems[0]
    assert "Extra inputs are not permitted" in problems[0]


def test_strict_problems_reports_cross_field_violations(make_resource: Any) -> None:
    """A mode/replicas mismatch is refused before the object is stored."""
    body = make_resource(spec={"replicas": None})

    problems = strict_problems(body)

    assert any("replicas is required" in problem for problem in problems)


def test_strict_problems_enforces_the_workload_policy(make_resource: Any) -> None:
    """A privileged workload is refused unless the policy accepts the risk."""
    body = make_resource()
    body["spec"]["template"]["spec"]["containers"][0]["securityContext"] = {"privileged": True}

    problems = strict_problems(body)

    assert len(problems) == 1
    assert problems[0].startswith("spec: ")
    assert "allowPrivileged" in problems[0]


def test_strict_problems_enforces_secret_non_disclosure(make_resource: Any) -> None:
    """A Secret read into the environment is refused at admission."""
    body = make_resource()
    body["spec"]["template"]["spec"]["containers"][0]["env"] = [
        {"name": "TOKEN", "valueFrom": {"secretKeyRef": {"name": "s", "key": "k"}}}
    ]

    problems = strict_problems(body)

    assert len(problems) == 1
    assert "allowInlineSecretEnv" in problems[0]


def test_strict_problems_truncates_a_flood(make_resource: Any) -> None:
    """A body with many problems is summarized rather than dumped in full."""
    body = make_resource()
    body["spec"]["template"]["spec"]["containers"][0].update({f"unknown{i}": i for i in range(30)})

    problems = strict_problems(body)

    assert len(problems) == 21
    assert problems[-1].startswith("... and ")
