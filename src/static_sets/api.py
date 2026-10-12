"""A small Kubernetes API client for the installer and the controller adapter.

Only the handful of calls this operator makes are modelled, and the transport is
injectable, so the request shaping is testable without a cluster. Connection
failures are surfaced as :class:`~static_sets.installer.ApiUnavailableError`,
which every caller treats as "retain everything": an unreachable API is never a
reason to change local state.
"""

from __future__ import annotations

import json
import os
import ssl
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, Protocol

from static_sets.installer import ApiUnavailableError

__all__ = [
    "ApiRequestError",
    "JsonClient",
    "KubernetesClient",
    "Transport",
]

SERVICE_ACCOUNT_DIR = "/var/run/secrets/kubernetes.io/serviceaccount"
"""Where a Pod finds its projected service-account token and cluster CA."""

DEFAULT_TIMEOUT = 15.0
"""Seconds to wait for the API server before declaring it unreachable."""

Transport = Callable[[str, str, "bytes | None", Mapping[str, str]], tuple[int, bytes]]
"""Performs one HTTP call and returns ``(status, body)``.

A transport is given the method, the absolute URL, an optional JSON body and the
request headers. Raising an :class:`OSError` from a transport is reported as the
API being unreachable.
"""


class ApiRequestError(RuntimeError):
    """The API server answered, but refused the request.

    This is distinct from the API being unreachable: a 404 or a 403 is a real
    answer, and callers act on it rather than falling back to retained state.
    """


class JsonClient(Protocol):
    """The JSON call surface the operator adapter and the installer rely on."""

    def json(
        self,
        method: str,
        path: str,
        *,
        body: Any = None,
        params: Mapping[str, str] | None = None,
        missing: Any = None,
    ) -> Any:
        """Perform one API call and decode the response.

        Args:
            method: The HTTP method.
            path: The API path.
            body: A JSON-serializable request body.
            params: Query parameters.
            missing: The value to return for a 404 instead of raising.

        Returns:
            The decoded response, or ``missing``.
        """
        ...


class KubernetesClient:
    """A minimal, synchronous client for the Kubernetes API."""

    def __init__(
        self,
        *,
        server: str,
        token: str | None = None,
        ca_path: str | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        transport: Transport | None = None,
    ) -> None:
        """Build a client.

        Args:
            server: The API server's base URL, e.g. ``https://10.0.0.1:443``.
            token: A bearer token, when the client authenticates.
            ca_path: Path of the cluster CA bundle. Certificate verification is
                always on: the API server is the one component whose identity must
                not be in doubt.
            timeout: Seconds to wait before declaring the API unreachable.
            transport: An alternative transport, used by tests.
        """
        self.server = server.rstrip("/")
        self.token = token
        self.ca_path = ca_path
        self.timeout = timeout
        self._transport = transport

    @classmethod
    def in_cluster(cls, **overrides: Any) -> KubernetesClient:
        """Build a client from the Pod's service-account configuration.

        Args:
            **overrides: Values that replace the discovered ones, e.g. a transport.

        Returns:
            The client.
        """
        return cls.from_environment(os.environ, **overrides)

    @classmethod
    def from_environment(
        cls,
        environ: Mapping[str, str],
        **overrides: Any,
    ) -> KubernetesClient:
        """Build a client from an environment mapping.

        Args:
            environ: The environment to read.
            **overrides: Values that replace the discovered ones.

        Returns:
            The client.

        Raises:
            ApiUnavailableError: If the API server address cannot be determined.
        """
        host = environ.get("KUBERNETES_SERVICE_HOST")
        port = environ.get("KUBERNETES_SERVICE_PORT", "443")
        server = overrides.pop("server", None)
        if server is None:
            if not host:
                raise ApiUnavailableError(
                    "KUBERNETES_SERVICE_HOST is not set and no API server was configured"
                )
            server = f"https://{host}:{port}"
        directory = Path(environ.get("STATIC_SETS_SERVICE_ACCOUNT_DIR", SERVICE_ACCOUNT_DIR))
        token = overrides.pop("token", None)
        if token is None:
            token = _read_optional(directory / "token")
        ca_path = overrides.pop("ca_path", None)
        if ca_path is None and (directory / "ca.crt").is_file():
            ca_path = str(directory / "ca.crt")
        return cls(server=server, token=token, ca_path=ca_path, **overrides)

    def call(
        self,
        method: str,
        path: str,
        *,
        body: Any = None,
        params: Mapping[str, str] | None = None,
    ) -> tuple[int, bytes]:
        """Perform one API call.

        Args:
            method: The HTTP method.
            path: The API path, starting with a slash.
            body: A JSON-serializable request body.
            params: Query parameters.

        Returns:
            The status code and the response body.

        Raises:
            ApiUnavailableError: If the API could not be reached at all.
        """
        url = f"{self.server}{path}"
        if params:
            url = f"{url}?{urllib.parse.urlencode(params)}"
        headers = {"Accept": "application/json"}
        payload: bytes | None = None
        if body is not None:
            payload = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        transport = self._transport or self._urllib_transport
        try:
            return transport(method, url, payload, headers)
        except OSError as error:
            raise ApiUnavailableError(f"{method} {path} failed: {error}") from error

    def json(
        self,
        method: str,
        path: str,
        *,
        body: Any = None,
        params: Mapping[str, str] | None = None,
        missing: Any = None,
    ) -> Any:
        """Perform one API call and decode the JSON response.

        Args:
            method: The HTTP method.
            path: The API path.
            body: A JSON-serializable request body.
            params: Query parameters.
            missing: The value to return for a 404 instead of raising.

        Returns:
            The decoded response, or ``missing`` for a 404.

        Raises:
            ApiRequestError: If the API refused the request.
            ApiUnavailableError: If the API could not be reached.
        """
        status, raw = self.call(method, path, body=body, params=params)
        if status == 404 and missing is not None:
            return missing
        if not 200 <= status < 300:
            raise ApiRequestError(
                f"{method} {path} returned {status}: {raw.decode('utf-8', 'replace')}"
            )
        if not raw:
            return None
        return json.loads(raw)

    def _urllib_transport(
        self,
        method: str,
        url: str,
        body: bytes | None,
        headers: Mapping[str, str],
    ) -> tuple[int, bytes]:
        """Perform an HTTP call with the standard library.

        Args:
            method: The HTTP method.
            url: The absolute URL.
            body: The request body.
            headers: The request headers.

        Returns:
            The status code and the response body.

        Raises:
            OSError: If the connection failed.
        """
        request = urllib.request.Request(url, data=body, headers=dict(headers), method=method)
        context = None
        if url.startswith("https"):
            context = ssl.create_default_context(cafile=self.ca_path)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout, context=context) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as error:
            return error.code, error.read()


def _read_optional(path: Path) -> str | None:
    """Read a file, returning ``None`` when it does not exist.

    Args:
        path: The file to read.

    Returns:
        The stripped content, or ``None``.
    """
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
