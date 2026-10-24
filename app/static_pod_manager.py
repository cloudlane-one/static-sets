"""The ``static-pod-manager`` entry point.

This is the file ``kopf run`` executes inside the manager DaemonSet, one Pod per
node. It is registered cluster-wide, because a StaticSet may live in any namespace
and a placement record names the node that has to serve it — not the namespace it
happens to sit in.

The process owns exactly one thing: the static Pods on the node it runs on. It does
not decide *where* anything goes; that is the controller's job, through the
scheduler. Here it only:

* watches ``StaticPodPlacement`` objects and reconciles the ones that name its node;
* compiles the StaticSet each of them points at, materializes the configuration, and
  writes or retires the manifest under kubelet's static Pod directory;
* reports what it did in the placement's status, including a refusal, so the
  controller can surface it instead of guessing;
* removes the static Pod when the placement says ``desired: false``, or when the
  object itself is deleted — and only then releases the finalizer that keeps the
  object around.

Two handlers are registered: one for creation, updates, resumes and a periodic timer
(the timer is what notices configuration drift, because a ConfigMap changing does not
change any object this process watches), and one for deletion.
"""

from __future__ import annotations

import datetime
import os
from typing import Any

import kopf

import based_operators as bk

from static_sets.api import KubernetesClient
from static_sets.cluster import ClusterApi
from static_sets.errors import ApiUnavailableError
from static_sets.manager import (
    ManagerConfig,
    PlacementReport,
    cleanup,
    reconcile,
)
from static_sets.models import CleanupReason
from static_sets.naming import PLACEMENT_FINALIZER
from static_sets.placements import StaticPodPlacement

__all__ = [
    "CONFIG",
    "finalize_placement",
    "reconcile_placement",
]

TIMER_INTERVAL = 30
"""Seconds between periodic passes for one placement.

Watches catch a change to the placement itself; only a timer catches the other half
of the desired state, which is a ConfigMap or a Secret the workload references.
"""

CONFIG = ManagerConfig.from_environment(os.environ)
"""The node this manager owns, and the paths it may write."""


def get_client() -> KubernetesClient:
    """Return the API client the manager uses.

    Returns:
        An in-cluster client.
    """
    return KubernetesClient.in_cluster()


def _report(
    report: PlacementReport,
    patch: kopf.Patch,
    *,
    identity: str,
    generation: int,
) -> None:
    """Write a report into the placement's status.

    Args:
        report: What the manager accomplished, or why it refused.
        patch: The kopf patch of the placement.
        identity: The placement identity the report is about.
        generation: The StaticSet generation the report is about.
    """
    patch.status["identity"] = identity
    patch.status["revision"] = report.revision
    patch.status["staticPodName"] = report.static_pod_name
    patch.status["phase"] = report.phase.value
    patch.status["acknowledged"] = report.acknowledged
    patch.status["observedGeneration"] = generation
    patch.status["offline"] = report.offline
    patch.status["message"] = report.message
    patch.status["warnings"] = list(report.warnings)
    patch.status["updatedAt"] = _now()


@StaticPodPlacement.on.create
@StaticPodPlacement.on.resume
@StaticPodPlacement.on.update
@StaticPodPlacement.on.timer(interval=TIMER_INTERVAL, initial_delay=10, idle=10)
def reconcile_placement(
    resource: StaticPodPlacement,
    patch: bk.Patch,
    logger: Any,
    **_: Any,
) -> None:
    """Bring one placement's static Pod in line with its StaticSet.

    Args:
        resource: The placement being reconciled.
        patch: The kopf patch used to record status and the finalizer.
        logger: The kopf logger.
        **_: Any further handler arguments.

    Raises:
        kopf.TemporaryError: If the API is unreachable, so the pass is retried instead
            of being reported as a failure of the StaticSet.
    """
    if resource.spec.node != CONFIG.node:
        # Every node's manager sees every placement. Only the one whose node is named
        # reports anything, so a placement is never written by several managers.
        return
    if resource.metadata.deletion_timestamp is not None:
        # Deletion is handled by the delete handler; touching status here would race
        # with the object going away.
        return
    assignment = CONFIG.assignment(resource)
    api = ClusterApi(get_client())
    finalizers = list(resource.metadata.finalizers or [])
    if PLACEMENT_FINALIZER not in finalizers:
        patch.metadata["finalizers"] = [*finalizers, PLACEMENT_FINALIZER]
    try:
        if resource.spec.desired:
            report = reconcile(assignment, api=api, now=_now())
        else:
            report = cleanup(
                assignment,
                api=api,
                now=_now(),
                reason=(resource.spec.reason or CleanupReason.RELOCATED).value,
                remove_configuration=resource.spec.remove_configuration,
            )
    except ApiUnavailableError as error:
        raise kopf.TemporaryError(f"the API is unreachable: {error}", delay=15) from error
    logger.info(
        "%s %s/%s on %s: %s",
        "reconciled" if resource.spec.desired else "cleaned",
        resource.metadata.namespace,
        resource.metadata.name,
        CONFIG.node,
        report.message,
    )
    _report(report, patch, identity=assignment.identity, generation=resource.spec.generation)


@StaticPodPlacement.on.delete
def finalize_placement(
    resource: StaticPodPlacement,
    patch: bk.Patch,
    logger: Any,
    **_: Any,
) -> None:
    """Remove a placement's static Pod before the object is released.

    A placement can be deleted while it is still desired: by an administrator, or by
    garbage collection once its StaticSet is gone. Neither may orphan a manifest that
    kubelet would keep starting, so the static Pod is removed first and the finalizer
    is released only after that.

    Args:
        resource: The placement being deleted.
        patch: The kopf patch used to release the finalizer.
        logger: The kopf logger.
        **_: Any further handler arguments.

    Raises:
        kopf.TemporaryError: While the workload cannot be confirmed stopped, or the
            API is unreachable.
    """
    if resource.spec.node != CONFIG.node:
        return
    assignment = CONFIG.assignment(resource)
    api = ClusterApi(get_client())
    try:
        report = cleanup(
            assignment,
            api=api,
            now=_now(),
            reason=CleanupReason.RESOURCE_DELETED.value,
            remove_configuration=resource.spec.remove_configuration,
        )
    except ApiUnavailableError as error:
        raise kopf.TemporaryError(f"the API is unreachable: {error}", delay=15) from error
    logger.info(
        "finalized %s/%s: %s", resource.metadata.namespace, resource.metadata.name, report.message
    )
    if not report.acknowledged:
        raise kopf.TemporaryError(
            f"placement {resource.metadata.name} is not cleaned up yet: {report.message}",
            delay=10,
        )
    finalizers = list(resource.metadata.finalizers or [])
    patch.metadata["finalizers"] = [item for item in finalizers if item != PLACEMENT_FINALIZER]


def _now() -> str:
    """Return the current time as RFC 3339 UTC.

    Returns:
        The timestamp.
    """
    return datetime.datetime.now(datetime.UTC).replace(microsecond=0).isoformat()
