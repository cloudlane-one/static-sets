"""The ``static-set-manager`` command: one pass per node, without kopf.

The manager normally runs as a kopf application inside the DaemonSet, which is what
gives it watch-based updates. This command runs the same library once (or repeatedly)
against the placements that name one node, which is what an operator reaches for when
debugging a single host: it needs no watch, no leader election and no running
controller, only a kubeconfig that may write placement statuses.

Both entry points call :func:`static_sets.manager.reconcile` and
:func:`static_sets.manager.cleanup`, so a manual pass cannot behave differently from
the DaemonSet.
"""

from __future__ import annotations

import datetime
import json
import os
import sys
import time
from collections.abc import Mapping, Sequence
from typing import Annotated

import typer

from static_sets.api import ApiRequestError, ApiUnavailableError, KubernetesClient
from static_sets.cluster import ClusterApi
from static_sets.errors import StaticSetsError
from static_sets.manager import ManagerConfig, PlacementReport, cleanup, reconcile
from static_sets.models import CleanupReason, PlacementPhase
from static_sets.placements import StaticPodPlacement

__all__ = ["app", "main", "run"]

DEFAULT_INTERVAL = 30.0
"""Seconds between passes when ``--interval`` is not given."""


app = typer.Typer(
    name="static-set-manager",
    help="Write this node's static Pods from the placement records that name it.",
    add_completion=False,
    context_settings={"help_option_names": ["-h", "--help"]},
)
"""The Typer application behind the ``static-set-manager`` command."""


@app.command()
def manage(
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
            help="Seconds between passes; 0 runs one pass and exits.",
        ),
    ] = DEFAULT_INTERVAL,
    node: Annotated[
        str | None,
        typer.Option(
            "--node",
            help="Node to manage; defaults to the node name from the downward API.",
        ),
    ] = None,
) -> None:
    """Reconcile every placement that names this node."""
    raise typer.Exit(code=run(interval=interval, timeout=timeout, node=node))


def run(
    *,
    environ: Mapping[str, str] | None = None,
    client: KubernetesClient | None = None,
    now: str | None = None,
    interval: float | None = None,
    timeout: float = 15.0,
    node: str | None = None,
) -> int:
    """Run the manager's pass, once or repeatedly.

    Args:
        environ: The environment, defaulting to :data:`os.environ`.
        client: An API client, used by tests instead of discovering a cluster.
        now: The current time, RFC 3339, defaulting to the real clock. A loop reuses
            it for every pass so that a test driving several passes sees one clock.
        interval: Seconds between passes, defaulting to ``30``. ``0`` runs a single
            pass, which is what the tests drive.
        timeout: Seconds to wait for the API server before declaring it unreachable.
        node: The node to manage, overriding the environment.

    Returns:
        The process exit status. Being offline is not a failure: the manager is a
        reconciler, and crash-looping would not bring the API back.
    """
    environment = os.environ if environ is None else environ
    if node is not None:
        environment = {**environment, "STATIC_SETS_NODE": node}
    timestamp = now or _now()
    try:
        config = ManagerConfig.from_environment(environment)
    except StaticSetsError as error:
        print(json.dumps({"event": "invalid-environment", "error": str(error)}))
        return 2
    try:
        cluster = client or KubernetesClient.from_environment(environment, timeout=timeout)
    except ApiUnavailableError as error:
        print(json.dumps({"event": "offline", "error": str(error)}))
        return 0
    api = ClusterApi(cluster)
    period = DEFAULT_INTERVAL if interval is None else interval
    while True:
        status = _pass(api, config, timestamp=timestamp)
        if status == 2 or period <= 0:
            return status
        try:
            time.sleep(period)
        except KeyboardInterrupt:  # pragma: no cover - depends on a signal
            return 0


def _pass(api: ClusterApi, config: ManagerConfig, *, timestamp: str) -> int:
    """Run one pass over every placement that names this node.

    Args:
        api: The cluster API.
        config: The manager's node and paths.
        timestamp: The time to record on every report.

    Returns:
        The process exit status for this pass.
    """
    try:
        placements = api.list_placements(config.node)
    except ApiUnavailableError as error:
        # An unreachable API is a state to keep working through, not a verdict: no
        # local file is touched and the last committed revision keeps running.
        print(json.dumps({"event": "offline", "error": str(error)}))
        return 0
    status = 0
    for placement in placements:
        status = max(status, _reconcile_placement(api, config, placement, timestamp=timestamp))
    return status


def _reconcile_placement(
    api: ClusterApi,
    config: ManagerConfig,
    placement: StaticPodPlacement,
    *,
    timestamp: str,
) -> int:
    """Reconcile or clean up one placement, and publish the outcome.

    Args:
        api: The cluster API.
        config: The manager's node and paths.
        placement: The placement to serve.
        timestamp: The time to record on the report.

    Returns:
        The process exit status for this placement.
    """
    namespace = placement.metadata.namespace or "default"
    name = placement.metadata.name or ""
    try:
        assignment = config.assignment(placement)
        if placement.spec.desired:
            report = reconcile(assignment, api=api, now=timestamp)
        else:
            report = cleanup(
                assignment,
                api=api,
                now=timestamp,
                reason=(placement.spec.reason or CleanupReason.RELOCATED).value,
                remove_configuration=placement.spec.remove_configuration,
            )
    except ApiUnavailableError as error:
        print(json.dumps({"event": "offline", "error": str(error)}))
        return 0
    except StaticSetsError as error:
        report = PlacementReport(
            phase=PlacementPhase.FAILED,
            message=str(error),
        )
        _publish(api, namespace, name, placement, report, timestamp=timestamp)
        print(json.dumps({"event": "failed", "placement": name, "error": str(error)}))
        return 1
    _publish(api, namespace, name, placement, report, timestamp=timestamp)
    print(
        json.dumps(
            {
                "event": "reconciled" if placement.spec.desired else "cleaned",
                "placement": name,
                **report.payload(),
            }
        )
    )
    return 0


def _publish(
    api: ClusterApi,
    namespace: str,
    name: str,
    placement: StaticPodPlacement,
    report: PlacementReport,
    *,
    timestamp: str,
) -> None:
    """Publish a report, tolerating an unreachable API.

    Args:
        api: The cluster API.
        namespace: The placement's namespace.
        name: The placement's name.
        placement: The placement the report is about.
        report: The report to publish.
        timestamp: The time the report was produced.
    """
    payload = {
        **report.payload(),
        "identity": api.identity_of(placement),
        "observedGeneration": str(placement.spec.generation),
        "updatedAt": timestamp,
    }
    try:
        api.publish_status(namespace, name, payload)
    except (ApiUnavailableError, ApiRequestError) as error:
        print(json.dumps({"event": "status-failed", "error": str(error)}))


def _now() -> str:
    """Return the current time as RFC 3339 UTC.

    Returns:
        The timestamp.
    """
    return datetime.datetime.now(datetime.UTC).replace(microsecond=0).isoformat()


def main(argv: Sequence[str] | None = None) -> None:
    """Run the command line application.

    Args:
        argv: The arguments, defaulting to ``sys.argv[1:]``.
    """
    app(args=list(argv) if argv is not None else None, prog_name="static-set-manager")


if __name__ == "__main__":  # pragma: no cover - exercised through __main__
    main()
    sys.exit(0)
