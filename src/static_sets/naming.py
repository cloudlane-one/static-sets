"""Deterministic names, labels and identities for a StaticSet and its placements.

Every name the operator creates is a pure function of the StaticSet's identity, its
namespace, a node and a revision. Nothing depends on the order in which objects are
observed or on a random suffix, so two runs of the controller derive the same names
and a restart cannot produce a second, differently named copy of the same placement.

Two labels do the routing, and they are the only labels anything selects on:

``staticsets.cloudlane.dev/probe``
    ``"true"`` on the Pods of a scheduling probe. The node's manager never looks at
    them; the central controller does, to learn which nodes the scheduler chose.
``staticsets.cloudlane.dev/set``
    The owning StaticSet's name, truncated to a label value, on every object either
    side creates. Everything else about ownership is an annotation or a field,
    because a label value cannot hold an arbitrary identifier.
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
    "COMPONENT_LABEL",
    "GENERATION_LABEL",
    "MANAGED_BY",
    "MIRROR_ANNOTATION",
    "PLACEMENT_COMPONENT",
    "PLACEMENT_FINALIZER",
    "PLACEMENT_LABEL",
    "PROBE_COMPONENT",
    "PROBE_LABEL",
    "RUNTIME_CLASS_LABEL",
    "SET_LABEL",
    "SET_UID_LABEL",
    "STATIC_SET_FINALIZER",
    "identity_suffix",
    "manifest_filename",
    "placement_identity",
    "placement_labels",
    "placement_name",
    "probe_label_selector",
    "probe_labels",
    "probe_workload_name",
    "static_pod_name",
]

MANAGED_BY = "static-sets"
"""Value of the ``app.kubernetes.io/managed-by`` label on every owned object."""

SET_LABEL = "staticsets.cloudlane.dev/set"
"""Label naming the StaticSet that owns an object either side created.

Both sides write objects that are read back by selector, so the label lives here.
When each side spelled it out for itself they disagreed: the operator selected on a
label the manager never wrote, and no acknowledgment was ever observed.
"""

SET_UID_LABEL = "staticsets.cloudlane.dev/set-uid"
"""Label holding the StaticSet UID, the authoritative ownership token.

A UID is a UUID, which is a valid label value, so ownership can be *selected* on
rather than only read. A StaticSet recreated under the same name has a different
UID, and nothing it owns may be adopted by the new object.
"""

GENERATION_LABEL = "staticsets.cloudlane.dev/generation"
"""Label holding the StaticSet generation a scheduling probe was created for.

It is part of the probe's selector, so a probe for a new generation can never adopt
the Pods of the previous one: the old probe keeps its own Pods until it is deleted,
which is what makes a ``Surge`` overlap expressible.
"""

RUNTIME_CLASS_LABEL = "staticsets.cloudlane.dev/runtime-class"
"""Label holding the ``RuntimeClass`` the workload asked for.

The probe Pods themselves run under the placeholder runtime class (they must be
scheduled and must never execute), so the class the static Pod will really use is
carried here instead. It is a label rather than an annotation because it describes
what the probe stands for, and because the intended class is one of the few facts
about a pending placement that is worth selecting on.
"""

PROBE_LABEL = "staticsets.cloudlane.dev/probe"
"""Label marking a scheduling-probe Pod. Its value is always ``"true"``."""

PROBE_VALUE = "true"
"""The only value :data:`PROBE_LABEL` carries."""

COMPONENT_LABEL = "app.kubernetes.io/component"
"""Label key naming what an object is: a scheduling probe, a placement record."""

PROBE_COMPONENT = "scheduling-probe"
"""Value of ``app.kubernetes.io/component`` on a probe workload and its Pods."""

PLACEMENT_COMPONENT = "placement"
"""Value of ``app.kubernetes.io/component`` on a placement record."""

PLACEMENT_LABEL = "staticsets.cloudlane.dev/placement"
"""Label holding a placement identity."""

ANNOTATION_SET = "staticsets.cloudlane.dev/static-set"
"""Annotation naming the StaticSet a static Pod was compiled from."""

ANNOTATION_SET_UID = "staticsets.cloudlane.dev/static-set-uid"
"""Annotation holding the StaticSet UID, the authoritative ownership token."""

ANNOTATION_PLACEMENT = "staticsets.cloudlane.dev/placement"
"""Annotation holding the placement identity a static Pod belongs to."""

ANNOTATION_REVISION = "staticsets.cloudlane.dev/revision"
"""Annotation holding the revision a static Pod manifest was rendered from."""

ANNOTATION_OWNER = "staticsets.cloudlane.dev/owner"
"""Annotation naming the manager instance that wrote a manifest or a record."""

ANNOTATION_GENERATION = "staticsets.cloudlane.dev/generation"
"""Annotation holding the ``metadata.generation`` the revision was computed for."""

MIRROR_ANNOTATION = "kubernetes.io/config.mirror"
"""The annotation kubelet adds to a mirror Pod. The manager never sets it."""

STATIC_SET_FINALIZER = "staticsets.cloudlane.dev/static-set"
"""Finalizer on the StaticSet: removed only after every placement is cleaned up."""

PLACEMENT_FINALIZER = "staticsets.cloudlane.dev/placement"
"""Finalizer on a placement record: removed only after its static Pod is removed.

The node's manager holds it, so deleting a placement object — directly, or through
garbage collection after its StaticSet is gone — cannot orphan a static Pod that
kubelet would otherwise keep starting forever.
"""

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
    never from the manager Pod that serves it. That is what lets a restarted
    manager adopt the same placement, the same revision directories and the same
    manifest instead of orphaning them.

    Args:
        resource_uid: The ``metadata.uid`` of the StaticSet.
        node: The node name the placement lives on.

    Returns:
        The placement identity.
    """
    return identity_suffix("placement", resource_uid, node)


def static_pod_name(resource_name: str, identity: str, revision: str) -> str:
    """Return the name of the static Pod for one placement and revision.

    The revision is part of the name, which is what lets two revisions of one
    StaticSet coexist on a node during a ``Surge`` rollout: kubelet keys a static
    Pod by the name in the manifest it reads, so a stable name could not express
    "the old one is still running".

    Args:
        resource_name: The StaticSet's name.
        identity: The placement identity.
        revision: The revision token the manifest was rendered from.

    Returns:
        A Pod name of the shape ``<name>-<identity>-<revision>``.
    """
    return f"{resource_name}-{identity}-{revision}"


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


def probe_workload_name(resource_name: str, generation: int) -> str:
    """Return the name of the scheduling probe for one generation.

    Args:
        resource_name: The StaticSet's name.
        generation: The StaticSet generation the probe is created for.

    Returns:
        The probe workload name.
    """
    return f"{resource_name}-probe-{generation}"


def placement_name(resource_name: str, identity: str) -> str:
    """Return the name of the placement record for one placement.

    Args:
        resource_name: The StaticSet's name.
        identity: The placement identity.

    Returns:
        The placement object's name.
    """
    return f"{resource_name}-{identity}"


def placement_labels(resource_name: str, static_set_uid: str) -> dict[str, str]:
    """Return the labels a placement record carries.

    The UID is what callers *select* on: a name can be longer than a label value and
    is then truncated to a digest, while a UID always fits.

    Args:
        resource_name: The StaticSet's name.
        static_set_uid: The owning StaticSet's UID.

    Returns:
        The labels.
    """
    return {
        "app.kubernetes.io/name": MANAGED_BY,
        COMPONENT_LABEL: PLACEMENT_COMPONENT,
        SET_LABEL: _truncate_label(resource_name),
        SET_UID_LABEL: _truncate_label(static_set_uid),
    }


def probe_labels(static_set_uid: str, resource_name: str, generation: int) -> dict[str, str]:
    """Return the selector labels of a scheduling probe and its Pods.

    Args:
        static_set_uid: The owning StaticSet's UID.
        resource_name: The StaticSet's name.
        generation: The StaticSet generation.

    Returns:
        The selector labels, all of which fit a label value.
    """
    return {
        "app.kubernetes.io/name": MANAGED_BY,
        COMPONENT_LABEL: PROBE_COMPONENT,
        SET_LABEL: _truncate_label(resource_name),
        SET_UID_LABEL: _truncate_label(static_set_uid),
        GENERATION_LABEL: str(generation),
        PROBE_LABEL: PROBE_VALUE,
    }


def probe_label_selector(resource_name: str, generation: int) -> str:
    """Return the label selector that finds one generation's probe Pods.

    Args:
        resource_name: The StaticSet's name.
        generation: The StaticSet generation.

    Returns:
        A selector string, as the Kubernetes list operations accept.
    """
    labels = {
        COMPONENT_LABEL: PROBE_COMPONENT,
        SET_LABEL: _truncate_label(resource_name),
        GENERATION_LABEL: str(generation),
    }
    return ",".join(f"{key}={value}" for key, value in sorted(labels.items()))


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
