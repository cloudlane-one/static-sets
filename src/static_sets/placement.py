"""Compute installer placements and build the installer workload.

A StaticSet is realised by *installer* Pods, never by the workload itself. In
``Deployment`` mode the operator creates one Deployment with the requested number
of replicas and lets the scheduler choose the nodes; in ``DaemonSet`` mode it
creates one DaemonSet and the DaemonSet controller puts exactly one installer on
every eligible node. Either way the placement identity is derived from the *node*
the installer landed on, so a replacement installer adopts the placement its
predecessor served instead of orphaning it.

The installer's host access is deliberately two mounts and no more: kubelet's
static Pod path, and the persistent bundle directory. Everything else — the
installer image, its resource reservation, its security context — is fixed here so
that a StaticSet cannot widen it.

Resource accounting is an explicit trade-off (see
:func:`installer_container_resources`): the installer reserves a small fixed amount
and does **not** copy the target workload's requests, because the static Pod's
requests are invisible to the scheduler and copying them onto the installer would
either double-count capacity or make the installer unschedulable on a node that can
comfortably host the workload itself.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

from static_sets.models import (
    Mode,
    NodeSelector,
    NodeSelectorTerm,
    StaticSet,
    StaticSetSpec,
    Toleration,
    TopologySpreadConstraint,
    UpdateStrategy,
)
from static_sets.naming import (
    ANNOTATION_GENERATION,
    ANNOTATION_SET,
    ANNOTATION_SET_UID,
    MANAGED_BY,
    installer_labels,
    installer_name,
)

__all__ = [
    "InstallerPod",
    "NodeInfo",
    "ObservedPlacement",
    "eligible_nodes",
    "installer_container_resources",
    "installer_workload",
    "selector_for",
]

INSTALLER_CONTAINER = "installer"
"""Name of the installer container, so RBAC and probes can refer to it."""

INSTALLER_COMMAND = ("static-set-installer", "reconcile")
"""Default command of the installer container."""

CLEANUP_COMMAND = ("static-set-installer", "cleanup")
"""Command of a restricted cleanup worker."""

STATIC_POD_MOUNT = "static-pod-path"
"""Volume name of kubelet's static Pod directory mount."""

BUNDLE_MOUNT = "bundle"
"""Volume name of the persistent bundle directory mount."""


@dataclass(frozen=True)
class NodeInfo:
    """A node the operator may place an installer on.

    Attributes:
        name: The node's name.
        labels: The node's labels.
        unschedulable: Whether the node is cordoned.
        ready: Whether the node reports ``Ready``.
    """

    name: str
    labels: Mapping[str, str] = field(default_factory=dict)
    unschedulable: bool = False
    ready: bool = True


@dataclass(frozen=True)
class InstallerPod:
    """An observed installer Pod.

    Attributes:
        name: The Pod's name, which is the installer instance identity.
        uid: The Pod's UID.
        node: The node the Pod is bound to, if it is bound yet.
        phase: The Pod's phase.
        deleting: Whether the Pod carries a deletion timestamp.
    """

    name: str
    uid: str
    node: str | None = None
    phase: str = "Pending"
    deleting: bool = False


@dataclass(frozen=True)
class ObservedPlacement:
    """A placement as acknowledged by the installer that serves it.

    Attributes:
        identity: The placement identity.
        node: The node the placement is on.
        revision: The revision the installer reports as installed.
        state: ``Installing``, ``Installed`` or ``Removed``.
        acknowledged: Whether the installer confirmed durable local installation.
        message: An explanation for the operator's status.
    """

    identity: str
    node: str
    revision: str | None = None
    state: str = "Installing"
    acknowledged: bool = False
    message: str | None = None


def installer_container_resources(spec: StaticSetSpec) -> dict[str, dict[str, str]]:
    """Return the resource reservation the installer container declares.

    The installer only writes files, so it asks for a small, fixed reservation
    rather than the target workload's requests. The consequence is documented rather
    than hidden: because the static Pod's own requests never reach the scheduler,
    request-based scheduling cannot see the workload a placement will eventually
    host. Operators that need that guarantee size their nodes or pin StaticSets to
    node classes through ``spec.placement`` instead. Setting
    ``spec.installer.resources`` overrides the default when a deliberate reservation
    is wanted.

    Args:
        spec: The StaticSet specification.

    Returns:
        A Kubernetes ``resources`` mapping.
    """
    if spec.installer.resources is not None:
        return spec.installer.resources.model_dump(by_alias=True, exclude_none=True)
    return {"requests": {"cpu": "10m", "memory": "64Mi"}, "limits": {"memory": "256Mi"}}


def selector_for(static_set: StaticSet) -> dict[str, str]:
    """Return the installer selector labels used by the workload's Pod template.

    Args:
        static_set: The owning StaticSet.

    Returns:
        The selector labels.
    """
    return installer_labels(static_set.metadata.name or "")


def installer_workload(
    static_set: StaticSet,
    *,
    namespace: str,
    generation: int,
) -> tuple[Literal["Deployment", "DaemonSet"], dict[str, Any]]:
    """Build the installer workload manifest for a StaticSet.

    Args:
        static_set: The owning StaticSet.
        namespace: The namespace to create the workload in.
        generation: The StaticSet generation the manifest is built for.

    Returns:
        The workload's kind and its complete manifest.
    """
    spec = static_set.spec
    name = installer_name(static_set.metadata.name or "", spec.mode.value)
    labels = selector_for(static_set)
    pod_template = _installer_pod_template(static_set, namespace=namespace, generation=generation)
    metadata = {
        "name": name,
        "namespace": namespace,
        "labels": {**labels, "app.kubernetes.io/managed-by": MANAGED_BY},
        "ownerReferences": _owner_reference(static_set),
    }
    workload_spec: dict[str, Any] = {
        "selector": {"matchLabels": labels},
        "template": pod_template,
    }
    if spec.mode is Mode.DEPLOYMENT:
        workload_spec["replicas"] = spec.replicas
        # The declared update strategy decides how an installer is replaced. Under
        # ``Recreate`` the old installer is retired before the new one starts, so a
        # node never has two installers at once. Under ``Surge`` the new one starts
        # first; that is safe because a placement is identified by its *node*, so two
        # installers on one node converge on the same placement and the same revision
        # rather than racing to write different manifests.
        if spec.update.strategy is UpdateStrategy.SURGE:
            workload_spec["strategy"] = {
                "type": "RollingUpdate",
                "rollingUpdate": {
                    "maxSurge": spec.update.max_surge,
                    "maxUnavailable": spec.update.max_unavailable,
                },
            }
        else:
            workload_spec["strategy"] = {"type": "Recreate"}
        kind: Literal["Deployment", "DaemonSet"] = "Deployment"
    else:
        workload_spec["updateStrategy"] = {"type": "OnDelete"}
        kind = "DaemonSet"
    return kind, {
        "apiVersion": "apps/v1",
        "kind": kind,
        "metadata": metadata,
        "spec": workload_spec,
    }


def _installer_pod_template(
    static_set: StaticSet,
    *,
    namespace: str,
    generation: int,
) -> dict[str, Any]:
    """Build the Pod template of the installer workload.

    Args:
        static_set: The owning StaticSet.
        namespace: The namespace the installer runs in.
        generation: The StaticSet generation, published to the installer.

    Returns:
        The complete Pod template.
    """
    spec = static_set.spec
    installer = spec.installer
    labels = selector_for(static_set)
    annotations = {
        ANNOTATION_SET: static_set.metadata.name or "",
        ANNOTATION_SET_UID: static_set.metadata.uid or "",
        ANNOTATION_GENERATION: str(generation),
    }
    pod_spec: dict[str, Any] = {
        "serviceAccountName": f"{static_set.metadata.name}-installer",
        "enableServiceLinks": False,
        "automountServiceAccountToken": True,
        "securityContext": {
            "runAsUser": 0,
            "runAsGroup": 0,
            "seccompProfile": {"type": "RuntimeDefault"},
        },
        "containers": [
            _installer_container(static_set, namespace=namespace, generation=generation)
        ],
        "volumes": [
            {
                "name": STATIC_POD_MOUNT,
                "hostPath": {"path": installer.static_pod_path, "type": "Directory"},
            },
            {
                "name": BUNDLE_MOUNT,
                "hostPath": {"path": installer.bundle_dir, "type": "DirectoryOrCreate"},
            },
        ],
    }
    pod_spec.update(_placement_fields(spec))
    return {
        "metadata": {
            "labels": {**labels, "app.kubernetes.io/managed-by": MANAGED_BY},
            "annotations": annotations,
        },
        "spec": pod_spec,
    }


def _installer_container(
    static_set: StaticSet,
    *,
    namespace: str,
    generation: int,
) -> dict[str, Any]:
    """Build the installer container.

    The container runs as root because kubelet's manifest directory is root-owned,
    but it is otherwise as small as it can be: all capabilities dropped, no
    privilege escalation, a read-only root filesystem and two host mounts.

    Args:
        static_set: The owning StaticSet.
        namespace: The namespace the installer runs in.
        generation: The StaticSet generation, published to the installer.

    Returns:
        The container specification.
    """
    installer = static_set.spec.installer
    environment = [
        {"name": "STATIC_SETS_SET", "value": static_set.metadata.name or ""},
        {"name": "STATIC_SETS_NAMESPACE", "value": namespace},
        {"name": "STATIC_SETS_UID", "value": static_set.metadata.uid or ""},
        {"name": "STATIC_SETS_GENERATION", "value": str(generation)},
        {"name": "STATIC_SETS_STATIC_POD_PATH", "value": installer.static_pod_path},
        {"name": "STATIC_SETS_BUNDLE_DIR", "value": installer.bundle_dir},
        _field_env("STATIC_SETS_NODE", "spec.nodeName"),
        _field_env("STATIC_SETS_POD_NAME", "metadata.name"),
        _field_env("STATIC_SETS_POD_UID", "metadata.uid"),
    ]
    return {
        "name": INSTALLER_CONTAINER,
        "image": installer.image,
        "imagePullPolicy": installer.image_pull_policy.value,
        "command": list(INSTALLER_COMMAND),
        "env": environment,
        "resources": installer_container_resources(static_set.spec),
        "volumeMounts": [
            {"name": STATIC_POD_MOUNT, "mountPath": installer.static_pod_path},
            {"name": BUNDLE_MOUNT, "mountPath": installer.bundle_dir},
        ],
        "securityContext": {
            "allowPrivilegeEscalation": False,
            "privileged": False,
            "readOnlyRootFilesystem": True,
            "capabilities": {"drop": ["ALL"]},
            "seccompProfile": {"type": "RuntimeDefault"},
        },
    }


def _field_env(variable: str, path: str) -> dict[str, Any]:
    """Return an environment entry sourced from the downward API.

    Args:
        variable: The variable name.
        path: The field path, e.g. ``spec.nodeName``.

    Returns:
        The environment entry.
    """
    return {"name": variable, "valueFrom": {"fieldRef": {"fieldPath": path}}}


def _placement_fields(spec: StaticSetSpec) -> dict[str, Any]:
    """Translate ``spec.placement`` into Pod-scheduler fields.

    Args:
        spec: The StaticSet specification.

    Returns:
        The placement fields, omitting anything the user did not set.
    """
    placement = spec.placement
    fields: dict[str, Any] = {}
    if placement.node_selector:
        fields["nodeSelector"] = dict(placement.node_selector)
    if placement.affinity is not None and placement.affinity.node_affinity is not None:
        fields["affinity"] = {
            "nodeAffinity": placement.affinity.node_affinity.model_dump(
                by_alias=True, exclude_none=True
            )
        }
    if placement.tolerations:
        fields["tolerations"] = [
            toleration.model_dump(by_alias=True, exclude_none=True)
            for toleration in placement.tolerations
        ]
    if placement.topology_spread_constraints:
        fields["topologySpreadConstraints"] = [
            _constraint(constraint) for constraint in placement.topology_spread_constraints
        ]
    return fields


def _constraint(constraint: TopologySpreadConstraint) -> dict[str, Any]:
    """Render a topology spread constraint.

    Args:
        constraint: The constraint to render.

    Returns:
        The rendered constraint, with the label selector nested under ``labelSelector``.
    """
    rendered = constraint.model_dump(by_alias=True, exclude_none=True)
    if constraint.label_selector is not None:
        rendered["labelSelector"] = constraint.label_selector.model_dump(
            by_alias=True, exclude_none=True
        )
    return rendered


def _owner_reference(static_set: StaticSet) -> list[dict[str, Any]]:
    """Return the owner reference tying an installer workload to its StaticSet.

    Args:
        static_set: The owning StaticSet.

    Returns:
        A single-entry owner reference list, or an empty list when the UID is not
        known yet.
    """
    if not static_set.metadata.uid:
        return []
    return [
        {
            "apiVersion": static_set.apiVersion,
            "kind": static_set.kind,
            "name": static_set.metadata.name,
            "uid": static_set.metadata.uid,
            "controller": True,
            "blockOwnerDeletion": True,
        }
    ]


def eligible_nodes(nodes: Iterable[NodeInfo], spec: StaticSetSpec) -> list[str]:
    """Return the nodes an installer may be placed on, conservatively.

    This is a *filter*, not a scheduler: it understands the node-selector and
    node-affinity subset the StaticSet can express, plus cordoning and the ``Ready``
    condition. The API server's scheduler remains authoritative for where an
    installer actually lands; this function only tells the operator which nodes it
    should expect a DaemonSet-mode installer on, so that status and cleanup can be
    computed without guessing.

    Args:
        nodes: The observed nodes.
        spec: The StaticSet specification.

    Returns:
        The eligible node names, sorted.
    """
    eligible: list[str] = []
    for node in nodes:
        if node.unschedulable and not _tolerates_unschedulable(spec.placement.tolerations):
            continue
        if not _matches_selector(node, spec.placement.node_selector):
            continue
        if not _matches_node_affinity(node, spec):
            continue
        eligible.append(node.name)
    return sorted(eligible)


def _tolerates_unschedulable(tolerations: Iterable[Toleration] | None) -> bool:
    """Return whether the tolerations admit a cordoned node.

    Args:
        tolerations: The declared tolerations.

    Returns:
        ``True`` when a toleration exists for ``node.kubernetes.io/unschedulable``.
    """
    for toleration in tolerations or []:
        if toleration.key == "node.kubernetes.io/unschedulable" and toleration.effect in (
            "NoSchedule",
            "PreferNoSchedule",
            None,
        ):
            return True
    return False


def _matches_selector(node: NodeInfo, selector: Mapping[str, str] | None) -> bool:
    """Return whether a node satisfies a plain node selector.

    Args:
        node: The node to test.
        selector: The required label values.

    Returns:
        ``True`` when every required label matches.
    """
    for key, value in (selector or {}).items():
        if node.labels.get(key) != value:
            return False
    return True


def _matches_node_affinity(node: NodeInfo, spec: StaticSetSpec) -> bool:
    """Return whether a node satisfies the required node affinity and any selector terms.

    An unset affinity matches everything, which mirrors the scheduler's behaviour.

    Args:
        node: The node to test.
        spec: The StaticSet specification.

    Returns:
        ``True`` when at least one required term matches, or no requirement exists.
    """
    affinity = spec.placement.affinity
    if affinity is None or affinity.node_affinity is None:
        return True
    required: NodeSelector | None = (
        affinity.node_affinity.required_during_scheduling_ignored_during_execution
    )
    if required is None:
        return True
    return any(_matches_term(node, term) for term in required.node_selector_terms)


def _matches_term(node: NodeInfo, term: NodeSelectorTerm) -> bool:
    """Return whether a node satisfies every predicate of one selector term.

    Args:
        node: The node to test.
        term: The selector term.

    Returns:
        ``True`` when all match expressions and match fields hold.
    """
    expressions = [*(term.match_expressions or []), *(term.match_fields or [])]
    return all(_matches_expression(node, expression) for expression in expressions)


def _matches_expression(node: NodeInfo, expression: Any) -> bool:
    """Return whether one node-selector predicate holds.

    Args:
        node: The node to test.
        expression: The predicate.

    Returns:
        ``True`` when the predicate holds for the node.

    Raises:
        NotImplementedError: If the operator is one this function does not know,
            which cannot happen for a validated specification.
    """
    values = expression.values or []
    present = expression.key in node.labels
    actual = node.labels.get(expression.key, "")
    match expression.operator:
        case "In":
            return present and actual in values
        case "NotIn":
            return not present or actual not in values
        case "Exists":
            return present
        case "DoesNotExist":
            return not present
        case "Gt":
            return bool(values) and _numeric(actual) > _numeric(values[0])
        case "Lt":
            return bool(values) and _numeric(actual) < _numeric(values[0])
        case _:  # pragma: no cover - the model restricts the operator set
            raise NotImplementedError(f"unknown node selector operator {expression.operator!r}")


def _numeric(value: str) -> int:
    """Parse a node label as an integer comparison value.

    Args:
        value: The label value.

    Returns:
        The parsed integer, or 0 when the value is not numeric, which makes the
        comparison fail closed.
    """
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0
