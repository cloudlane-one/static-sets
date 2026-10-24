"""The Kubernetes client, with this project's failure semantics.

Everything else comes from the official ``kubernetes`` client: transport,
authentication, serialization, and the pydantic models for the built-in
resources. The generated API groups are exposed as they are, so a call site reads
like the generated documentation and keeps the generated signatures:

>>> client.core.read_namespaced_config_map("web-conf", "edge")  # -> V1ConfigMap
>>> client.apps.list_namespaced_deployment("edge")              # -> V1DeploymentList

Only three things are added, because the operator's correctness depends on them
and they have nowhere else to live:

* **failures are classified.** An unreachable API is not an empty desired state:
  the installer keeps the last committed revision and the controller retries,
  while a refusal (403, 409, …) is a real answer to act on. The generated client
  reports the first as a status-less :class:`ApiException` in some paths and lets
  urllib3 raise in others, so the classification is done once, in the client.
* **absence can be an answer.** Optional reads use :func:`or_none`, so "not there
  yet" is a value at the few call sites that probe, not an exception everywhere.
* **the client is built from an environment mapping**, without mutating the
  process-global configuration, so tests and the entry points configure it the
  same way.

The operator's own models are untouched: ``spec.template`` is the StaticSet API,
and a static Pod's template has deliberately narrower (and stricter) semantics
than ``V1Pod``. See :mod:`static_sets.workload`.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from urllib3.exceptions import HTTPError as Urllib3Error

from kubernetes.client import (
    ApiClient,
    ApiException,
    ApiResponse,
    AppsV1Api,
    Configuration,
    CoreV1Api,
    CustomObjectsApi,
    V1ConfigMap,
    V1DaemonSet,
    V1Deployment,
    V1Pod,
)
from kubernetes.client.rest import RESTResponse
from kubernetes.config.config_exception import ConfigException
from kubernetes.config.incluster_config import InClusterConfigLoader

from static_sets.installer import ApiUnavailableError

__all__ = [
    "ApiRequestError",
    "KubernetesClient",
    "KubernetesObject",
    "or_none",
    "upsert",
]

SERVICE_ACCOUNT_DIR = "/var/run/secrets/kubernetes.io/serviceaccount"
"""Where a Pod finds its projected service-account token and cluster CA."""

DEFAULT_TIMEOUT = 15.0
"""Seconds to wait for the API server before declaring it unreachable."""

type KubernetesObject = V1ConfigMap | V1Deployment | V1DaemonSet | V1Pod
"""The built-in objects this operator creates or replaces."""


class ApiRequestError(RuntimeError):
    """The API server answered, but refused the request.

    This is distinct from the API being unreachable: a 403 or a 422 is a real
    answer, and callers act on it rather than falling back to retained state.
    """

    def __init__(self, message: str, *, status: int) -> None:
        """Build the error.

        Args:
            message: What failed and how.
            status: The HTTP status the server answered with.
        """
        super().__init__(message)
        self.status = status


def _translated(error: ApiException, action: str) -> ApiRequestError | ApiUnavailableError:
    """Classify one generated-client failure.

    Args:
        error: The failure the generated client raised.
        action: What was being attempted, for the message.

    Returns:
        The project's equivalent error.
    """
    if not error.status:
        # A status-less ApiException is a failed connection, not a refusal.
        return ApiUnavailableError(f"{action} failed: {error.reason}")
    return ApiRequestError(
        f"{action} returned {error.status}: {error.reason}",
        status=error.status,
    )


class _GuardedApiClient(ApiClient):
    """The generated client, with the project's timeout and failure semantics.

    Every generated endpoint funnels through :meth:`call_api`, so translating
    there covers all of them without wrapping a single method: the API groups
    keep their generated signatures and return types.
    """

    def __init__(self, configuration: Configuration, *, timeout: float) -> None:
        """Build the client.

        Args:
            configuration: The connection, credentials and CA to use.
            timeout: Seconds to wait before declaring the API unreachable.
        """
        super().__init__(configuration=configuration)
        self.request_timeout = timeout

    def call_api(
        self,
        method: str,
        url: str,
        header_params: Any = None,
        body: Any = None,
        post_params: Any = None,
        _request_timeout: Any = None,
    ) -> RESTResponse:
        """Perform one request, translating its failure.

        Args:
            method: The HTTP method.
            url: The absolute URL.
            header_params: The request headers.
            body: The request body.
            post_params: Form parameters.
            _request_timeout: A per-call timeout, defaulting to the client's.

        Returns:
            The raw response.

        Raises:
            ApiUnavailableError: If the API could not be reached at all.
            ApiRequestError: If the API answered and refused the request.
        """
        try:
            return super().call_api(
                method,
                url,
                header_params=header_params,
                body=body,
                post_params=post_params,
                _request_timeout=(
                    self.request_timeout if _request_timeout is None else _request_timeout
                ),
            )
        except ApiException as error:
            raise _translated(error, f"{method} {url}") from error
        except (OSError, Urllib3Error) as error:
            # The generated client lets some transport failures escape as urllib3's
            # own exception (a retry exhaustion, for instance).
            raise ApiUnavailableError(f"{method} {url} failed: {error}") from error

    def response_deserialize(
        self,
        response_data: RESTResponse,
        response_types_map: dict[str, Any] | None = None,
    ) -> ApiResponse[Any]:
        """Deserialize a response, translating a non-2xx status.

        The generated client raises a typed :class:`ApiException` subclass from here
        rather than from :meth:`call_api`, so both funnels have to be covered.

        Args:
            response_data: The raw response.
            response_types_map: The status-to-type map of the endpoint.

        Returns:
            The deserialized response.

        Raises:
            ApiUnavailableError: If the API could not be reached at all.
            ApiRequestError: If the API answered and refused the request.
        """
        try:
            return super().response_deserialize(response_data, response_types_map)
        except ApiException as error:
            raise _translated(error, "the API") from error


class KubernetesClient:
    """The generated API groups, connected and configured for this operator."""

    def __init__(
        self,
        configuration: Configuration,
        *,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        """Build a client from an official client configuration.

        Args:
            configuration: The connection, credentials and CA to use.
            timeout: Seconds to wait before declaring the API unreachable.
        """
        self.configuration = configuration
        api_client = _GuardedApiClient(configuration, timeout=timeout)
        self.core = CoreV1Api(api_client)
        self.apps = AppsV1Api(api_client)
        self.custom = CustomObjectsApi(api_client)

    @classmethod
    def in_cluster(cls, **overrides: Any) -> KubernetesClient:
        """Build a client from the Pod's service-account configuration.

        Args:
            **overrides: Values that replace the discovered ones.

        Returns:
            The client.
        """
        return cls.from_environment(os.environ, **overrides)

    @classmethod
    def from_environment(
        cls,
        environ: Mapping[str, str],
        *,
        configuration: Configuration | None = None,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> KubernetesClient:
        """Build a client from an environment mapping.

        Args:
            environ: The environment to read.
            configuration: A ready-made configuration, which replaces discovery
                entirely. Used by tests and by callers with a kubeconfig.
            timeout: Seconds to wait before declaring the API unreachable.

        Returns:
            The client.

        Raises:
            ApiUnavailableError: If the API server address cannot be determined.
        """
        if configuration is None:
            configuration = _configuration_from_environment(environ)
        return cls(configuration, timeout=timeout)


def or_none[**P, R](operation: Callable[P, R], *args: P.args, **kwargs: P.kwargs) -> R | None:
    """Call an API operation, returning ``None`` when the object does not exist.

    The generated client raises for a 404 like for any other refusal. A caller that
    is probing — an optional ConfigMap, a placement record written on a previous
    pass — treats absence as a normal answer instead.

    Args:
        operation: The generated API method to call.
        *args: Its positional arguments.
        **kwargs: Its keyword arguments.

    Returns:
        Its result, or ``None`` when the server answered 404.

    Raises:
        ApiUnavailableError: If the API could not be reached at all.
        ApiRequestError: If the API refused the request for another reason.
    """
    try:
        return operation(*args, **kwargs)
    except ApiRequestError as error:
        if error.status == 404:
            return None
        raise


def upsert(client: KubernetesClient, namespace: str, obj: KubernetesObject) -> None:
    """Create an object, or replace it in place when it already exists.

    The existing object's ``resourceVersion`` is carried onto the replacement,
    which is what makes the write an update rather than a conflict.

    Args:
        client: The client whose API groups are used.
        namespace: The object's namespace.
        obj: The desired object.

    Raises:
        ValueError: If the object has no name.
        ApiUnavailableError: If the API could not be reached.
        ApiRequestError: If the API refused the read or the write.
    """
    metadata = obj.metadata
    if metadata is None or not metadata.name:
        raise ValueError(f"a {type(obj).__name__} needs a metadata.name to be applied")
    read, create, replace = _operations(client, type(obj))
    existing = or_none(read, metadata.name, namespace)
    if existing is None:
        create(namespace, obj)
        return
    if existing.metadata is not None:
        metadata.resource_version = existing.metadata.resource_version
    replace(metadata.name, namespace, obj)


def _operations(
    client: KubernetesClient,
    kind: type[KubernetesObject],
) -> tuple[Callable[..., Any], Callable[..., Any], Callable[..., Any]]:
    """Return the read, create and replace operations for an object type.

    Args:
        client: The client whose API groups are used.
        kind: The object type.

    Returns:
        The read, create and replace callables.

    Raises:
        TypeError: If the type is not one this operator applies.
    """
    group, noun = _GROUPS.get(kind, (None, None))
    if group is None or noun is None:
        raise TypeError(f"{kind.__name__} is not an object this operator applies")
    api: Any = getattr(client, group)
    return (
        getattr(api, f"read_namespaced_{noun}"),
        getattr(api, f"create_namespaced_{noun}"),
        getattr(api, f"replace_namespaced_{noun}"),
    )


_GROUPS: dict[type[Any], tuple[str, str]] = {
    V1ConfigMap: ("core", "config_map"),
    V1Pod: ("core", "pod"),
    V1Deployment: ("apps", "deployment"),
    V1DaemonSet: ("apps", "daemon_set"),
}
"""Where each applied kind lives: its API group and its generated method stem."""


def _configuration_from_environment(environ: Mapping[str, str]) -> Configuration:
    """Build the official client's configuration from an environment.

    The service-account directory is read by the official in-cluster loader, so the
    bearer token is refreshed when the projected token rotates. A directory without a
    token or CA is not an error: a client may be built outside a cluster, and the
    first call then fails as unreachable rather than here.

    Args:
        environ: The environment to read.

    Returns:
        The configuration.

    Raises:
        ApiUnavailableError: If the API server address cannot be determined.
    """
    host = environ.get("KUBERNETES_SERVICE_HOST")
    if not host:
        raise ApiUnavailableError(
            "KUBERNETES_SERVICE_HOST is not set and no API server was configured"
        )
    port = environ.get("KUBERNETES_SERVICE_PORT", "443")
    directory = Path(environ.get("STATIC_SETS_SERVICE_ACCOUNT_DIR", SERVICE_ACCOUNT_DIR))
    configuration = Configuration()
    loader = InClusterConfigLoader(
        token_filename=str(directory / "token"),
        cert_filename=str(directory / "ca.crt"),
        environ={**environ, "KUBERNETES_SERVICE_PORT": port},
    )
    try:
        loader.load_and_set(client_configuration=configuration)
    except (ConfigException, OSError):
        # No usable token or CA: build an unauthenticated client rather than failing
        # to construct one. The first call then reports the real answer.
        configuration.host = f"https://{host}:{port}"
    return configuration
