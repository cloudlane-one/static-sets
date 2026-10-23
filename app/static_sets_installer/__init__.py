"""The installer front end that runs inside an installer Pod.

The Pod is given everything it needs through its environment: which StaticSet it
serves, which node it is on, which paths it may write, and which placement it owns.
Nothing is discovered by guessing, so a replaced installer adopts the placement its
predecessor left rather than creating a second, conflicting one.

The command has two actions. ``reconcile`` compiles the StaticSet and installs the
resulting revision on this node. ``cleanup`` carries out a pending cleanup intent
for a placement whose installer is gone; it is what a restricted cleanup worker
runs, and it releases its own finalizer once removal is acknowledged.

The command itself is a Typer application, so its options and its generated
reference page come from one declaration. :func:`run` is the same behaviour
without the command line, which is what the tests drive.
"""

from __future__ import annotations

import base64
import json
import os
import sys
import time
from collections.abc import Mapping, Sequence
from enum import StrEnum
from typing import Annotated

import typer
from kubernetes.client import V1ConfigMap, V1ObjectMeta, V1Pod

from static_sets.api import ApiRequestError, KubernetesClient, or_none, upsert
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
    "Action",
    "ClusterApi",
    "app",
    "assignment_from_environment",
    "main",
    "run",
]


DEFAULT_INTERVAL = 30.0
"""Seconds between reconcile passes when ``--interval`` is not given."""


class Action(StrEnum):
    """What the installer command should do."""

    RECONCILE = "reconcile"
    CLEANUP = "cleanup"


class ClusterApi:
    """The :class:`~static_sets.installer.PlacementApi` slice, against a cluster.

    The API operations themselves come from
    :class:`~static_sets.api.KubernetesClient`, which returns the official
    client's pydantic models. This adapter only turns those models into the
    byte-oriented shape the installer's runtime works with, and merges the labels
    an acknowledgment has to keep.
    """

    def __init__(self, client: KubernetesClient) -> None:
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
        config = StaticSet._require_resource_config()
        body = self.client.custom.get_namespaced_custom_object(
            config.group,
            config.version,
            namespace,
            config.plural,
            name,
        )
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
        record = or_none(self.client.core.read_namespaced_config_map, name, namespace)
        if record is None:
            return None
        data: dict[str, bytes] = {
            key: value.encode("utf-8") for key, value in (record.data or {}).items()
        }
        for key, value in (record.binary_data or {}).items():
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
        record = or_none(self.client.core.read_namespaced_secret, name, namespace)
        if record is None:
            return None
        return {key: base64.b64decode(value) for key, value in (record.data or {}).items()}

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
        record = or_none(self.client.core.read_namespaced_config_map, name, namespace)
        if record is None:
            return None
        return dict(record.data or {})

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
        existing = or_none(self.client.core.read_namespaced_config_map, name, namespace)
        # The object the operator published carries labels it selects this record by,
        # so they are merged rather than replaced: an acknowledgment written with a
        # label set of its own is one the controller never sees.
        existing_metadata = existing.metadata if existing else None
        published_labels = dict((existing_metadata.labels if existing_metadata else None) or {})
        labels = {
            **published_labels,
            "app.kubernetes.io/managed-by": MANAGED_BY,
            COMPONENT_LABEL: PLACEMENT_COMPONENT,
            SET_LABEL: static_set,
            PLACEMENT_LABEL: payload.get("placement", ""),
        }
        annotations = dict((existing_metadata.annotations if existing_metadata else None) or {})
        metadata = V1ObjectMeta(
            name=name,
            namespace=namespace,
            labels=labels,
            annotations=annotations or None,
        )
        upsert(
            self.client,
            namespace,
            V1ConfigMap(
                api_version="v1",
                kind="ConfigMap",
                metadata=metadata,
                data=dict(payload),
            ),
        )

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
        try:
            self.client.core.patch_namespaced_pod(
                pod_name,
                namespace,
                V1Pod(metadata=V1ObjectMeta(finalizers=[])),
                field_manager=MANAGED_BY,
            )
            self.client.core.delete_namespaced_pod(
                pod_name,
                namespace,
                grace_period_seconds=0,
            )
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


app = typer.Typer(
    name="static-set-installer",
    help="Install a StaticSet's static Pod on the node this Pod runs on.",
    add_completion=False,
    context_settings={"help_option_names": ["-h", "--help"]},
)
"""The Typer application behind the ``static-set-installer`` command."""


@app.command()
def install(
    action: Annotated[
        Action,
        typer.Argument(
            help="Reconcile the placement, or carry out a pending cleanup intent.",
            show_default=True,
        ),
    ] = Action.RECONCILE,
    timeout: Annotated[
        float,
        typer.Option(
            "--timeout",
            help="Seconds to wait for the API server before treating it as unreachable.",
        ),
    ] = 15.0,
    interval: Annotated[
        float,
        typer.Option(
            "--interval",
            help="Seconds between reconcile passes; 0 runs one pass and exits.",
        ),
    ] = DEFAULT_INTERVAL,
    workload_running: Annotated[
        bool,
        typer.Option(
            "--workload-running",
            help="Treat the static workload as still running, so its configuration is kept.",
        ),
    ] = False,
) -> None:
    """Place the compiled static Pod on this node, or clean it up."""
    raise typer.Exit(
        code=run(
            action,
            timeout=timeout,
            interval=interval,
            workload_running=workload_running,
        )
    )


def run(
    action: Action,
    *,
    environ: Mapping[str, str] | None = None,
    client: KubernetesClient | None = None,
    now: str | None = None,
    interval: float | None = None,
    timeout: float = 15.0,
    workload_running: bool = False,
) -> int:
    """Run the installer's action.

    A ``reconcile`` keeps reconciling: it repeats its pass every ``interval`` seconds
    until it is stopped. A placement has to notice configuration that changed, a
    revision that moved, and above all a cleanup intent that the operator published
    *after* the pass that finished a moment ago. Running once and exiting would leave
    that discovery to kubelet's container restart backoff, which grows to five minutes
    — long enough for a pending cleanup to look like a stuck one. A ``cleanup`` runs
    its pass and stops, because a worker exists for exactly one placement's removal.

    Args:
        action: Whether to reconcile the placement or carry out a cleanup.
        environ: The environment, defaulting to :data:`os.environ`.
        client: An API client, used by tests instead of discovering a cluster.
        now: The current time, RFC 3339, defaulting to the real clock. A loop reuses
            it for every pass so that a test driving several passes sees one clock.
        interval: Seconds between passes, defaulting to ``30``. ``0`` runs a single
            pass, which is what the tests drive.
        timeout: Seconds to wait for the API server before declaring it unreachable.
        workload_running: Whether the static workload is still running, which decides
            whether a cleanup may remove its configuration.

    Returns:
        The process exit status. Being offline is not a failure: the installer is a
        reconciler, and crash-looping would not bring the API back.
    """
    environment = os.environ if environ is None else environ
    timestamp = now or _now()
    try:
        assignment = assignment_from_environment(environment)
        cluster = client or KubernetesClient.from_environment(environment, timeout=timeout)
    except ApiUnavailableError as error:
        print(json.dumps({"event": "offline", "error": str(error)}))
        return 0
    except StaticSetsError as error:
        print(json.dumps({"event": "invalid-environment", "error": str(error)}))
        return 2

    api = ClusterApi(cluster)
    if action is Action.CLEANUP:
        return _single_pass(
            api,
            assignment,
            timestamp=timestamp,
            cleanup=True,
            workload_running=workload_running,
        )

    period = DEFAULT_INTERVAL if interval is None else interval
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


def main(argv: Sequence[str] | None = None) -> None:
    """Run the command line application.

    Args:
        argv: The arguments, defaulting to ``sys.argv[1:]``.
    """
    app(args=list(argv) if argv is not None else None, prog_name="static-set-installer")


if __name__ == "__main__":  # pragma: no cover - exercised through __main__
    main()
    sys.exit(0)
