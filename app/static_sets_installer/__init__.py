"""The installer front end that runs inside an installer Pod.

The Pod is given everything it needs through its environment: which StaticSet it
serves, which node it is on, which paths it may write, and which placement it owns.
Nothing is discovered by guessing, so a replaced installer adopts the placement its
predecessor left rather than creating a second, conflicting one.

The command has two actions. ``reconcile`` compiles the StaticSet and installs the
resulting revision on this node. ``cleanup`` carries out a pending cleanup intent
for a placement whose installer is gone; it is what a restricted cleanup worker
runs, and it releases its own finalizer once removal is acknowledged.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import time
from collections.abc import Mapping, Sequence
from typing import Any

from static_sets.api import ApiRequestError, JsonClient, KubernetesClient, Transport
from static_sets.errors import NotOfflineReadyError, StaticSetsError
from static_sets.installer import (
    ApiUnavailableError,
    Assignment,
    PlacementReport,
    cleanup_placement,
    reconcile,
)
from static_sets.models import StaticSet
from static_sets.naming import (
    MANAGED_BY,
    COMPONENT_LABEL,
    PLACEMENT_COMPONENT,
    PLACEMENT_LABEL,
    SET_LABEL,
)

__all__ = [
    "ClusterApi",
    "assignment_from_environment",
    "main",
]

STATIC_SET_PATH = "/apis/staticsets.cloudlane.dev/v1/namespaces/{namespace}/staticsets/{name}"
"""The REST path of a StaticSet."""

CONFIG_MAP_PATH = "/api/v1/namespaces/{namespace}/configmaps/{name}"
"""The REST path of a ConfigMap."""

SECRET_PATH = "/api/v1/namespaces/{namespace}/secrets/{name}"
"""The REST path of a Secret."""

POD_PATH = "/api/v1/namespaces/{namespace}/pods/{name}"
"""The REST path of a Pod."""


class ClusterApi:
    """The :class:`~static_sets.installer.PlacementApi` slice, against a cluster."""

    def __init__(self, client: JsonClient) -> None:
        """Bind the API to a client.

        Args:
            client: The Kubernetes API client to use.
        """
        self.client = client

    def get_static_set(self, namespace: str, name: str) -> StaticSet:
        """Return the StaticSet the installer serves.

        Args:
            namespace: The StaticSet's namespace.
            name: The StaticSet's name.

        Returns:
            The validated StaticSet.

        Raises:
            ApiUnavailableError: If the API could not be reached.
            ApiRequestError: If the object does not exist.
        """
        body = self.client.json("GET", STATIC_SET_PATH.format(namespace=namespace, name=name))
        return StaticSet.model_validate(body)

    def get_config_map(self, namespace: str, name: str) -> Mapping[str, bytes] | None:
        """Return a ConfigMap's data, decoding any binary entries.

        Args:
            namespace: The ConfigMap's namespace.
            name: The ConfigMap's name.

        Returns:
            The data, or ``None`` when the ConfigMap does not exist.

        Raises:
            ApiUnavailableError: If the API could not be reached.
        """
        body = self.client.json(
            "GET", CONFIG_MAP_PATH.format(namespace=namespace, name=name), missing={}
        )
        if not body:
            return None
        data: dict[str, bytes] = {
            key: value.encode("utf-8") for key, value in (body.get("data") or {}).items()
        }
        for key, value in (body.get("binaryData") or {}).items():
            data[key] = base64.b64decode(value)
        return data

    def get_secret(self, namespace: str, name: str) -> Mapping[str, bytes] | None:
        """Return a Secret's data, decoded from its base64 representation.

        Args:
            namespace: The Secret's namespace.
            name: The Secret's name.

        Returns:
            The data, or ``None`` when the Secret does not exist.

        Raises:
            ApiUnavailableError: If the API could not be reached.
        """
        body = self.client.json(
            "GET", SECRET_PATH.format(namespace=namespace, name=name), missing={}
        )
        if not body:
            return None
        return {key: base64.b64decode(value) for key, value in (body.get("data") or {}).items()}

    def get_placement(self, namespace: str, name: str) -> Mapping[str, str] | None:
        """Return the payload the operator published for a placement.

        Args:
            namespace: The ConfigMap's namespace.
            name: The ConfigMap's name.

        Returns:
            The payload, or ``None`` when nothing was published.

        Raises:
            ApiUnavailableError: If the API could not be reached.
        """
        body = self.client.json(
            "GET", CONFIG_MAP_PATH.format(namespace=namespace, name=name), missing={}
        )
        if not body:
            return None
        return dict(body.get("data") or {})

    def publish_placement(
        self,
        namespace: str,
        name: str,
        payload: Mapping[str, str],
        *,
        static_set: str,
    ) -> None:
        """Create or update the ConfigMap a placement is acknowledged through.

        Args:
            namespace: The ConfigMap's namespace.
            name: The ConfigMap's name.
            payload: The acknowledgment payload.
            static_set: The owning StaticSet's name, which is one of the labels the
                operator selects placement records by.

        Raises:
            ApiUnavailableError: If the API could not be reached.
            ApiRequestError: If the write was refused.
        """
        path = CONFIG_MAP_PATH.format(namespace=namespace, name=name)
        existing = self.client.json("GET", path, missing={})
        # The object the operator published carries labels it selects this record by,
        # so they are merged rather than replaced: an acknowledgment written with a
        # label set of its own is one the controller never sees.
        published = dict((existing.get("metadata") or {}).get("labels") or {})
        labels = {
            **published,
            "app.kubernetes.io/managed-by": MANAGED_BY,
            COMPONENT_LABEL: PLACEMENT_COMPONENT,
            SET_LABEL: static_set,
            PLACEMENT_LABEL: payload.get("placement", ""),
        }
        metadata: dict[str, Any] = {"name": name, "namespace": namespace, "labels": labels}
        annotations = (existing.get("metadata") or {}).get("annotations")
        if annotations:
            metadata["annotations"] = dict(annotations)
        manifest: dict[str, Any] = {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": metadata,
            "data": dict(payload),
        }
        if existing:
            self.client.json("PUT", path, body=manifest)
        else:
            collection = CONFIG_MAP_PATH.format(namespace=namespace, name="").rstrip("/")
            self.client.json("POST", collection, body=manifest)

    def release_self(self, namespace: str, pod_name: str) -> None:
        """Remove this worker's finalizer and delete its own Pod.

        A cleanup worker holds a finalizer so that the operator cannot release the
        StaticSet's finalizer while local state is still being removed. Once removal
        is acknowledged the worker clears it and removes itself, which keeps the
        release idempotent even if the controller is not watching.

        Args:
            namespace: The Pod's namespace.
            pod_name: The Pod's name.
        """
        path = POD_PATH.format(namespace=namespace, name=pod_name)
        try:
            self.client.json(
                "PATCH",
                path,
                body={"metadata": {"finalizers": []}},
                params={"fieldManager": MANAGED_BY},
            )
            self.client.json("DELETE", path, body={"gracePeriodSeconds": 0})
        except (ApiUnavailableError, ApiRequestError) as error:
            print(json.dumps({"event": "release-self-failed", "error": str(error)}))


def assignment_from_environment(environ: Mapping[str, str]) -> Assignment:
    """Build an installer assignment from the Pod's environment.

    Args:
        environ: The environment to read.

    Returns:
        The assignment.

    Raises:
        StaticSetsError: If a required variable is missing.
    """
    required = (
        "STATIC_SETS_SET",
        "STATIC_SETS_NAMESPACE",
        "STATIC_SETS_UID",
        "STATIC_SETS_NODE",
        "STATIC_SETS_POD_NAME",
        "STATIC_SETS_STATIC_POD_PATH",
        "STATIC_SETS_BUNDLE_DIR",
    )
    missing = [name for name in required if not environ.get(name)]
    if missing:
        raise StaticSetsError(f"the installer environment is missing: {', '.join(missing)}")
    return Assignment(
        static_set=environ["STATIC_SETS_SET"],
        namespace=environ["STATIC_SETS_NAMESPACE"],
        uid=environ["STATIC_SETS_UID"],
        generation=int(environ.get("STATIC_SETS_GENERATION", "0")),
        node=environ["STATIC_SETS_NODE"],
        instance=environ["STATIC_SETS_POD_NAME"],
        static_pod_path=environ["STATIC_SETS_STATIC_POD_PATH"],
        bundle_dir=environ["STATIC_SETS_BUNDLE_DIR"],
        placement=environ.get("STATIC_SETS_PLACEMENT"),
    )


def main(
    argv: Sequence[str] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    transport: Transport | None = None,
    now: str | None = None,
    interval: float | None = None,
) -> int:
    """Run the installer command.

    A ``reconcile`` keeps reconciling: it repeats its pass every ``interval`` seconds
    until it is stopped. A placement has to notice configuration that changed, a
    revision that moved, and above all a cleanup intent that the operator published
    *after* the pass that finished a moment ago. Running once and exiting would leave
    that discovery to kubelet's container restart backoff, which grows to five minutes
    — long enough for a pending cleanup to look like a stuck one. A ``cleanup`` runs
    its pass and stops, because a worker exists for exactly one placement's removal.

    Args:
        argv: The command-line arguments, defaulting to ``sys.argv[1:]``.
        environ: The environment, defaulting to :data:`os.environ`.
        transport: An alternative HTTP transport, used by tests.
        now: The current time, RFC 3339, defaulting to the real clock. A loop reuses
            it for every pass so that a test driving several passes sees one clock.
        interval: Seconds between passes, defaulting to the parsed ``--interval``.
            ``0`` runs a single pass, which is what the tests drive.

    Returns:
        The process exit status. Being offline is not a failure: the installer is a
        reconciler, and crash-looping would not bring the API back.
    """
    environment = os.environ if environ is None else environ
    arguments = _parse(argv)
    timestamp = now or _now()
    try:
        assignment = assignment_from_environment(environment)
        client = KubernetesClient.from_environment(
            environment, transport=transport, timeout=arguments.timeout
        )
    except ApiUnavailableError as error:
        print(json.dumps({"event": "offline", "error": str(error)}))
        return 0
    except StaticSetsError as error:
        print(json.dumps({"event": "invalid-environment", "error": str(error)}))
        return 2

    api = ClusterApi(client)
    if arguments.action == "cleanup":
        return _single_pass(
            api,
            assignment,
            timestamp=timestamp,
            cleanup=True,
            workload_running=arguments.workload_running,
        )

    period = arguments.interval if interval is None else interval
    while True:
        status = _single_pass(api, assignment, timestamp=timestamp, cleanup=False)
        if status == 2 or period <= 0:
            return status
        try:
            time.sleep(period)
        except KeyboardInterrupt:  # pragma: no cover - depends on a signal
            return 0


def _single_pass(
    api: ClusterApi,
    assignment: Assignment,
    *,
    timestamp: str,
    cleanup: bool,
    workload_running: bool = False,
) -> int:
    """Run one reconcile or cleanup pass and publish its result.

    Args:
        api: The cluster API.
        assignment: The installer's assignment.
        timestamp: The time to record on the report.
        cleanup: Whether this is a cleanup pass rather than a reconcile.
        workload_running: Whether the static workload is still running, which decides
            whether its configuration may be removed.

    Returns:
        The process exit status for this pass.
    """
    try:
        if cleanup:
            report = cleanup_placement(
                assignment,
                api=api,
                now=timestamp,
                terminated=not workload_running,
            )
        else:
            report = reconcile(assignment, api=api, now=timestamp)
    except NotOfflineReadyError as error:
        _publish(api, assignment, _failure(assignment, str(error)))
        print(json.dumps({"event": "not-offline-ready", "error": str(error)}))
        return 1
    except ApiUnavailableError as error:
        # The API is unreachable, which is a state to keep working through and not a
        # verdict about the StaticSet: nothing local is touched and the last committed
        # revision keeps running.
        print(json.dumps({"event": "offline", "error": str(error)}))
        return 0
    except StaticSetsError as error:
        _publish(api, assignment, _failure(assignment, str(error)))
        print(json.dumps({"event": "failed", "error": str(error)}))
        return 1

    _publish(api, assignment, report)
    print(json.dumps({"event": "reconciled", **report.payload()}))
    if cleanup and report.acknowledged and assignment.placement:
        api.release_self(assignment.namespace, assignment.instance)
    return 0


def _parse(argv: Sequence[str] | None) -> argparse.Namespace:
    """Parse the installer's arguments.

    Args:
        argv: The arguments, or ``None`` for ``sys.argv[1:]``.

    Returns:
        The parsed arguments.
    """
    parser = argparse.ArgumentParser(
        prog="static-set-installer",
        description="Install a StaticSet's static Pod on the node this Pod runs on.",
    )
    parser.add_argument(
        "action",
        nargs="?",
        default="reconcile",
        choices=["reconcile", "cleanup"],
        help="reconcile the placement, or carry out a pending cleanup intent",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=15.0,
        help="seconds to wait for the API server before treating it as unreachable",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=30.0,
        help="seconds between reconcile passes; 0 runs one pass and exits",
    )
    parser.add_argument(
        "--workload-running",
        action="store_true",
        help="treat the static workload as still running, so its configuration is kept",
    )
    return parser.parse_args(argv)


def _publish(api: ClusterApi, assignment: Assignment, report: PlacementReport) -> None:
    """Publish an acknowledgment, tolerating an unreachable API.

    Args:
        api: The cluster API.
        assignment: The installer's assignment.
        report: The report to publish.
    """
    try:
        api.publish_placement(
            assignment.namespace,
            _placement_name(assignment),
            report.payload(),
            static_set=assignment.static_set,
        )
    except (ApiUnavailableError, ApiRequestError) as error:
        print(json.dumps({"event": "acknowledgment-failed", "error": str(error)}))


def _placement_name(assignment: Assignment) -> str:
    """Return the ConfigMap name a placement is acknowledged through.

    Args:
        assignment: The installer's assignment.

    Returns:
        The ConfigMap name.
    """
    from static_sets.naming import placement_configmap_name

    return placement_configmap_name(assignment.static_set, assignment.identity)


def _failure(assignment: Assignment, message: str) -> PlacementReport:
    """Build the report published when reconciliation fails.

    Args:
        assignment: The installer's assignment.
        message: The failure to report.

    Returns:
        The report.
    """
    return PlacementReport(
        placement=assignment.identity,
        node=assignment.node,
        state="Installing",
        acknowledged=False,
        message=message,
    )


def _now() -> str:
    """Return the current time as RFC 3339 UTC.

    Returns:
        The timestamp.
    """
    import datetime

    return datetime.datetime.now(datetime.UTC).replace(microsecond=0).isoformat()


if __name__ == "__main__":  # pragma: no cover - exercised through __main__
    sys.exit(main())
