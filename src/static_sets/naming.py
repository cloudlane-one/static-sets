"""Deterministic names, labels and identities for a StaticSet and its placements.

Every name the operator creates is a pure function of the StaticSet's identity,
its namespace and a node. Nothing depends on the order in which objects are
observed or on a random suffix, so two runs of the controller derive the same
names and a restart cannot produce a second, differently named copy of the same
placement.
"""

from __future__ import annotations

import hashlib
import re

__all__ = [
    "ANNOTATION_GENERATION",
    "ANNOTATION_OWNER",
    "ANNOTATION_PLACEMENT",
    "ANNOTATION_REVISION",
    "ANNOTATION_SET",
    "ANNOTATION_SET_UID",
    "CLEANUP_FINALIZER",
    "INSTALLER_FINALIZER",
    "MANAGED_BY",
    "MIRROR_ANNOTATION",
    "PLACEMENT_LABEL",
    "STATIC_SET_FINALIZER",
    "cleanup_worker_name",
    "identity_suffix",
    "installer_labels",
    "installer_name",
    "manifest_filename",
    "placement_configmap_name",
    "placement_identity",
    "static_pod_name",
]

MANAGED_BY = "static-sets"
"""Value of the ``app.kubernetes.io/managed-by`` label on every owned object."""

PLACEMENT_LABEL = "staticsets.cloudlane.dev/placement"
"""Label holding a placement identity, used to find an owned cleanup worker."""

ANNOTATION_SET = "staticsets.cloudlane.dev/static-set"
"""Annotation naming the StaticSet a static Pod was compiled from."""

ANNOTATION_SET_UID = "staticsets.cloudlane.dev/static-set-uid"
"""Annotation holding the StaticSet UID, the authoritative ownership token."""

ANNOTATION_PLACEMENT = "staticsets.cloudlane.dev/placement"
"""Annotation holding the placement identity a static Pod belongs to."""

ANNOTATION_REVISION = "staticsets.cloudlane.dev/revision"
"""Annotation holding the revision a static Pod manifest was rendered from."""

ANNOTATION_OWNER = "staticsets.cloudlane.dev/owner"
"""Annotation naming the installer instance that wrote a manifest or a record."""

ANNOTATION_GENERATION = "staticsets.cloudlane.dev/generation"
"""Annotation holding the ``metadata.generation`` the revision was computed for."""

MIRROR_ANNOTATION = "kubernetes.io/config.mirror"
"""The annotation kubelet adds to a mirror Pod. The installer never sets it."""

STATIC_SET_FINALIZER = "staticsets.cloudlane.dev/static-set"
"""Finalizer on the StaticSet: removed only after cleanup is acknowledged."""

INSTALLER_FINALIZER = "staticsets.cloudlane.dev/installer"
"""Finalizer on installer Pods: held until the workload may be handed over."""

CLEANUP_FINALIZER = "staticsets.cloudlane.dev/cleanup"
"""Finalizer on cleanup worker Pods: removed after local removal is confirmed."""

_MAX_LABEL = 63

_DNS_LABEL = re.compile(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$")


def identity_suffix(*parts: str, length: int = 10) -> str:
    """Return a short, deterministic hex digest of the given parts.

    Args:
        *parts: The identity inputs, joined with a NUL so that ``("ab", "c")`` and
            ``("a", "bc")`` cannot collide.
        length: The number of hex characters to keep.

    Returns:
        A lowercase hex digest of ``length`` characters.
    """
    digest = hashlib.sha256("\x00".join(parts).encode("utf-8")).hexdigest()
    return digest[:length]


def placement_identity(resource_uid: str, node: str) -> str:
    """Return the stable identity of the placement on one node.

    The identity is derived from the owning StaticSet's UID and the node name,
    never from the installer Pod that currently happens to serve it. That is what
    lets a replacement installer adopt the same placement, the same revision
    directories and the same manifest instead of orphaning them.

    Args:
        resource_uid: The ``metadata.uid`` of the StaticSet.
        node: The node name the placement lives on.

    Returns:
        The placement identity.
    """
    return identity_suffix("placement", resource_uid, node)


def static_pod_name(resource_name: str, identity: str) -> str:
    """Return the name of the static Pod for a placement.

    Args:
        resource_name: The StaticSet's name.
        identity: The placement identity.

    Returns:
        A cell name of the shape ``<name>-<identity>``.
    """
    return f"{resource_name}-{identity}"


def manifest_filename(pod_name: str) -> str:
    """Return the manifest file name for a static Pod.

    kubelet derives a static Pod's name from the manifest's ``metadata.name`` and
    reports the file it came from; keeping the two identical is the convention
    every kubelet release accepts.

    Args:
        pod_name: The static Pod's name.

    Returns:
        The manifest file name, including its extension.
    """
    return f"{pod_name}.yaml"


def installer_name(resource_name: str, mode: str) -> str:
    """Return the name of the installer workload.

    Args:
        resource_name: The StaticSet's name.
        mode: ``Deployment`` or ``DaemonSet``, so that switching modes cannot make
            the new workload adopt the old one's identity.

    Returns:
        The workload name.
    """
    return f"{resource_name}-installer-{mode.lower()}"


def cleanup_worker_name(resource_name: str, identity: str) -> str:
    """Return the name of the restricted cleanup worker for a placement.

    Args:
        resource_name: The StaticSet's name.
        identity: The placement identity.

    Returns:
        The cleanup worker Pod name.
    """
    return f"{resource_name}-cleanup-{identity}"


def placement_configmap_name(resource_name: str, identity: str) -> str:
    """Return the ConfigMap name an installer acknowledges a placement through.

    Args:
        resource_name: The StaticSet's name.
        identity: The placement identity.

    Returns:
        The ConfigMap name.
    """
    return f"{resource_name}-placement-{identity}"


def installer_labels(resource_name: str) -> dict[str, str]:
    """Return the labels an installer workload and its Pods carry.

    The labels are the installer selector, so they must stay within the DNS label
    length limit even for a long StaticSet name; the name is therefore truncated.

    Args:
        resource_name: The StaticSet's name.

    Returns:
        The selector labels.
    """
    return {
        "app.kubernetes.io/name": MANAGED_BY,
        "app.kubernetes.io/component": "installer",
        "staticsets.cloudlane.dev/set": _truncate_label(resource_name),
    }


def _truncate_label(value: str) -> str:
    """Return a value usable as a label, shortened if necessary.

    Args:
        value: The candidate label value.

    Returns:
        A DNS-1123 label of at most 63 characters.
    """
    if len(value) <= _MAX_LABEL and _DNS_LABEL.match(value):
        return value
    digest = identity_suffix(value, length=8)
    head = value[: _MAX_LABEL - len(digest) - 1].strip("-")
    if not _DNS_LABEL.match(head):
        head = "s"
    return f"{head}-{digest}"
