"""The Kubernetes API client and strict admission validation."""

from __future__ import annotations

import json
from typing import Any

import pytest

from static_sets.api import ApiRequestError, KubernetesClient
from static_sets.installer import ApiUnavailableError
from static_sets.validation import strict_problems


class Recorder:
    """A transport that records calls and replays canned answers."""

    def __init__(self, answers: dict[str, tuple[int, bytes]] | None = None) -> None:
        """Build a recorder.

        Args:
            answers: Method-and-path keys to ``(status, body)`` answers.
        """
        self.answers = answers or {}
        self.calls: list[tuple[str, str, bytes | None, dict[str, str]]] = []

    def __call__(
        self,
        method: str,
        url: str,
        body: bytes | None,
        headers: Any,
    ) -> tuple[int, bytes]:
        """Record one call and answer it.

        Args:
            method: The HTTP method.
            url: The absolute URL.
            body: The request body.
            headers: The request headers.

        Returns:
            The canned answer, or an empty success.
        """
        self.calls.append((method, url, body, dict(headers)))
        key = f"{method} {url.split('?')[0].replace('https://api.test', '')}"
        return self.answers.get(key, (200, b"{}"))


def client(recorder: Any, **overrides: Any) -> KubernetesClient:
    """Return a client wired to a recorder.

    Args:
        recorder: The transport.
        **overrides: Client overrides.

    Returns:
        The client.
    """
    return KubernetesClient(
        server="https://api.test", token="t0ken", transport=recorder, **overrides
    )


def test_calls_carry_the_bearer_token_and_accept_header() -> None:
    """Every request is authenticated and asks for JSON."""
    recorder = Recorder()

    client(recorder).json("GET", "/api/v1/nodes")

    method, url, body, headers = recorder.calls[0]
    assert (method, url) == ("GET", "https://api.test/api/v1/nodes")
    assert body is None
    assert headers["Authorization"] == "Bearer t0ken"
    assert headers["Accept"] == "application/json"


def test_query_parameters_are_encoded() -> None:
    """Query parameters reach the server, encoded."""
    recorder = Recorder()

    client(recorder).json("GET", "/api/v1/pods", params={"labelSelector": "a=b,c=d"})

    assert recorder.calls[0][1] == "https://api.test/api/v1/pods?labelSelector=a%3Db%2Cc%3Dd"


def test_bodies_are_json_encoded_with_a_content_type() -> None:
    """A request body is JSON, with the matching content type."""
    recorder = Recorder()

    client(recorder).json("PUT", "/api/v1/x", body={"a": 1})

    method, _, body, headers = recorder.calls[0]
    assert method == "PUT"
    assert json.loads(body or b"") == {"a": 1}
    assert headers["Content-Type"] == "application/json"


def test_a_404_is_returned_as_the_missing_sentinel() -> None:
    """A 404 is an answer, not a failure, when the caller expects absence."""
    recorder = Recorder({"GET /api/v1/configmaps": (404, b'{"reason":"NotFound"}')})

    result = client(recorder).json("GET", "/api/v1/configmaps", missing={})

    assert result == {}


def test_a_404_without_a_sentinel_is_an_error() -> None:
    """Without a sentinel a 404 is reported as a refusal."""
    recorder = Recorder({"GET /api/v1/configmaps": (404, b'{"reason":"NotFound"}')})

    with pytest.raises(ApiRequestError):
        client(recorder).json("GET", "/api/v1/configmaps")


def test_a_refusal_reports_the_status_and_body() -> None:
    """A 403 is surfaced with enough detail to diagnose it."""
    recorder = Recorder({"POST /api/v1/x": (403, b'{"reason":"Forbidden"}')})

    with pytest.raises(ApiRequestError) as error:
        client(recorder).json("POST", "/api/v1/x", body={})

    assert "403" in str(error.value)
    assert "Forbidden" in str(error.value)


def test_a_connection_failure_is_reported_as_unreachable() -> None:
    """An OS-level failure means the API is unreachable, not that it refused."""

    def broken(*_: Any) -> tuple[int, bytes]:
        raise OSError("connection refused")

    with pytest.raises(ApiUnavailableError) as error:
        client(broken).json("GET", "/api/v1/nodes")

    assert "connection refused" in str(error.value)


def test_an_empty_body_decodes_to_none() -> None:
    """An empty 200 body is not a JSON error."""
    recorder = Recorder({"DELETE /api/v1/x": (200, b"")})

    assert client(recorder).json("DELETE", "/api/v1/x") is None


def test_from_environment_discovers_the_in_cluster_endpoint() -> None:
    """The service host and port produce the API server URL."""
    recorder = Recorder()

    built = KubernetesClient.from_environment(
        {"KUBERNETES_SERVICE_HOST": "10.0.0.1", "KUBERNETES_SERVICE_PORT": "6443"},
        transport=recorder,
    )

    assert built.server == "https://10.0.0.1:6443"
    assert built.token is None


def test_from_environment_refuses_without_a_server() -> None:
    """Without a service host there is nothing to talk to."""
    with pytest.raises(ApiUnavailableError):
        KubernetesClient.from_environment({})


def test_from_environment_reads_the_token_file(tmp_path: Any) -> None:
    """The projected service-account token is picked up from disk."""
    directory = tmp_path / "sa"
    directory.mkdir()
    (directory / "token").write_text("secret-token\n", encoding="utf-8")
    (directory / "ca.crt").write_text("ca", encoding="utf-8")

    built = KubernetesClient.from_environment(
        {
            "KUBERNETES_SERVICE_HOST": "10.0.0.1",
            "STATIC_SETS_SERVICE_ACCOUNT_DIR": str(directory),
        }
    )

    assert built.token == "secret-token"
    assert built.ca_path == str(directory / "ca.crt")


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
