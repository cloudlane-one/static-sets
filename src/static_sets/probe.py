"""Ask the Kubernetes scheduler where a StaticSet's static Pods should go.

kubelet never schedules a static Pod: it starts whatever manifest it finds on the
node whose kubelet finds it. So placement cannot be decided by the static workload
itself, and the only scheduler in the cluster that knows about capacity, taints,
affinity and topology is the Kubernetes one.

This module builds the object that lets that scheduler decide: a **scheduling probe**.
It is an ordinary ReplicaSet or DaemonSet whose Pod template copies everything about
the workload that a scheduling decision depends on — the containers' resource
requests and limits, the host ports it publishes, whether it wants the host network —
plus the StaticSet's own ``spec.placement`` fields. What it deliberately does *not*
copy is anything that would let the Pod run:

* every container is replaced by an inert image and carries only resources and host
  ports, so nothing from the workload's template can execute;
* the Pod runs under a ``RuntimeClass`` whose handler no container runtime
  implements, so kubelet binds the Pod, fails to create its sandbox, and never
  starts a container.

That combination is what makes a probe *scheduled but never executed*: the decision
is real, the Pod is not.

The intended runtime class is not lost: it travels on the
:data:`~static_sets.naming.RUNTIME_CLASS_LABEL` label, because the probe's own
``runtimeClassName`` is spoken for.

Resource accounting is the reason the probe exists at all, so it is worth stating
what it costs. While a probe exists, the node it was scheduled on accounts the
workload's requests twice — once for the probe, once for the static Pod kubelet is
already running. The controller deletes the probe as soon as the static Pod is
installed and mirrored, so the overlap is bounded by one installation. After that the
scheduler no longer sees the workload's usage at all; that is a property of static
Pods, not of this design, and the placement guide says so.
"""

from __future__ import annotations

import os
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
)
from static_sets.naming import (
    ANNOTATION_GENERATION,
    ANNOTATION_SET,
    ANNOTATION_SET_UID,
    MANAGED_BY,
    RUNTIME_CLASS_LABEL,
    probe_labels,
    probe_workload_name,
)

__all__ = [
    "DEFAULT_PROBE_RUNTIME_CLASS",
    "NodeInfo",
    "ObservedProbePod",
    "ObservedProbeWorkload",
    "ObservedWorkload",
    "PROBE_IMAGE",
    "PROBE_RUNTIME_CLASS_ENV",
    "eligible_nodes",
    "probe_pod_template",
    "probe_runtime_class",
    "probe_workload",
]

PROBE_IMAGE = "registry.k8s.io/pause:3.10"
"""The image a probe container names.

It is never pulled: the sandbox is never created, so no container in a probe Pod
ever starts. A real, well-known reference is used anyway so that the manifest is
valid to every consumer that inspects it.
"""

PROBE_RUNTIME_CLASS_ENV = "STATIC_SETS_PROBE_RUNTIME_CLASS"
"""The environment variable the chart uses to name the placeholder runtime class."""

DEFAULT_PROBE_RUNTIME_CLASS = "staticsets-scheduling-probe"
"""The placeholder runtime class when the environment names none."""


def probe_runtime_class() -> str:
    """Return the runtime class the scheduling probes declare.

    The class is created by the chart, not by the operator: it is a cluster-scoped
    object with no per-StaticSet content, so creating it would be a privilege the
    operator does not need. Its handler is not implemented by any container runtime,
    which is exactly the point — kubelet must fail to start the Pod.

    Returns:
        The runtime class name.
    """
    return os.environ.get(PROBE_RUNTIME_CLASS_ENV) or DEFAULT_PROBE_RUNTIME_CLASS


@dataclass(frozen=True)
class NodeInfo:
    """A node the operator reports on.

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
class ObservedProbePod:
    """One Pod of a scheduling probe.

    Attributes:
        name: The Pod's name.
        node: The node the scheduler bound it to, or ``None`` while it is unbound.
        deleting: Whether the Pod carries a deletion timestamp.
    """

    name: str
    node: str | None = None
    deleting: bool = False


@dataclass(frozen=True)
class ObservedProbeWorkload:
    """A probe workload the operator observed.

    Attributes:
        kind: ``ReplicaSet`` or ``DaemonSet``.
        name: The workload's name.
        generation: The StaticSet generation encoded in its selector.
        replicas: How many Pods the workload intends to have. For a ReplicaSet that
            is its replica count; for a DaemonSet the nodes it wants a Pod on.
        status_seen: Whether the workload's controller has published a status yet.
            Without one, "no Pods" says nothing about completeness.
        pods: The Pods it currently has.
    """

    kind: Literal["ReplicaSet", "DaemonSet"]
    name: str
    generation: int
    replicas: int = 0
    status_seen: bool = False
    pods: tuple[ObservedProbePod, ...] = ()

    @property
    def scheduled(self) -> tuple[ObservedProbePod, ...]:
        """The Pods that are bound to a node and not being deleted."""
        return tuple(pod for pod in self.pods if pod.node and not pod.deleting)

    @property
    def unscheduled(self) -> int:
        """How many Pods exist but have no node yet."""
        return sum(1 for pod in self.pods if not pod.node and not pod.deleting)

    @property
    def complete(self) -> bool:
        """Whether every Pod the workload intends to create exists and is placed.

        A probe's Pods never become *ready* — they cannot start — so completeness has
        to be judged from the objects and their node assignments. A Pod that stays
        unbound is a real answer — nothing admits the workload — and not something to
        conclude a pass from.
        """
        if not self.status_seen:
            return False
        if self.replicas == 0:
            return True
        return len(self.pods) == self.replicas and self.unscheduled == 0


@dataclass(frozen=True)
class ObservedWorkload:
    """A mirror Pod kubelet created for a static Pod.

    Attributes:
        name: The mirror Pod's name, which equals the static Pod's name.
        node: The node it runs on.
        placement: The placement identity from the static Pod's annotation.
        revision: The revision from the static Pod's annotation.
        phase: The mirror Pod's phase, when known.
        ready: Whether the mirror Pod reports ``Ready``, when known.
        restart_count: The containers' aggregate restart count, when known.
    """

    name: str
    node: str
    placement: str | None = None
    revision: str | None = None
    phase: str | None = None
    ready: bool | None = None
    restart_count: int | None = None


def probe_workload(
    static_set: StaticSet,
    *,
    namespace: str,
    generation: int,
) -> tuple[Literal["ReplicaSet", "DaemonSet"], dict[str, Any]]:
    """Build the scheduling probe for one StaticSet generation.

    Args:
        static_set: The owning StaticSet.
        namespace: The namespace to create the probe in.
        generation: The StaticSet generation the probe is created for.

    Returns:
        The workload's kind and its complete manifest.
    """
    spec = static_set.spec
    name = static_set.metadata.name or ""
    labels = probe_labels(static_set.metadata.uid or "", name, generation)
    template = probe_pod_template(static_set, generation=generation)
    metadata: dict[str, Any] = {
        "name": probe_workload_name(name, generation),
        "namespace": namespace,
        "labels": {**labels, "app.kubernetes.io/managed-by": MANAGED_BY},
        "annotations": {
            ANNOTATION_SET: name,
            ANNOTATION_SET_UID: static_set.metadata.uid or "",
            ANNOTATION_GENERATION: str(generation),
        },
    }
    owner = _owner_reference(static_set)
    if owner:
        metadata["ownerReferences"] = owner
    workload_spec: dict[str, Any] = {
        "selector": {"matchLabels": labels},
        "template": template,
    }
    if spec.mode is Mode.DEPLOYMENT:
        workload_spec["replicas"] = spec.replicas
        kind: Literal["ReplicaSet", "DaemonSet"] = "ReplicaSet"
    else:
        # ``OnDelete`` so the DaemonSet controller never replaces a probe Pod on its
        # own: a probe exists for one generation and is deleted as a whole, and a
        # controller-initiated replacement would silently re-run a scheduling pass
        # the operator believes is finished.
        workload_spec["updateStrategy"] = {"type": "OnDelete"}
        kind = "DaemonSet"
    return kind, {
        "apiVersion": "apps/v1",
        "kind": kind,
        "metadata": metadata,
        "spec": workload_spec,
    }


def probe_pod_template(static_set: StaticSet, *, generation: int) -> dict[str, Any]:
    """Build the Pod template of a scheduling probe.

    Args:
        static_set: The owning StaticSet.
        generation: The StaticSet generation the probe is created for.

    Returns:
        The complete Pod template.
    """
    spec = static_set.spec
    pod_spec = spec.template.spec
    template_meta = spec.template.metadata
    name = static_set.metadata.name or ""
    labels = {
        **((template_meta.labels if template_meta else None) or {}),
        **probe_labels(static_set.metadata.uid or "", name, generation),
        "app.kubernetes.io/managed-by": MANAGED_BY,
    }
    annotations: dict[str, str] = {
        **((template_meta.annotations if template_meta else None) or {}),
        # Ours last: a template annotation must not be able to describe this object as
        # a different StaticSet's placement.
        ANNOTATION_SET: name,
        ANNOTATION_SET_UID: static_set.metadata.uid or "",
        ANNOTATION_GENERATION: str(generation),
    }
    if pod_spec.runtime_class_name:
        # The workload's own runtime class cannot be used: the probe must never
        # start. It is carried here so the placement stays self-describing.
        labels[RUNTIME_CLASS_LABEL] = pod_spec.runtime_class_name
    emitted: dict[str, Any] = {
        "runtimeClassName": probe_runtime_class(),
        "restartPolicy": "Always",
        "terminationGracePeriodSeconds": 0,
        "containers": [_probe_container(container) for container in pod_spec.containers],
    }
    if pod_spec.init_containers:
        emitted["initContainers"] = [
            _probe_container(container) for container in pod_spec.init_containers
        ]
    if pod_spec.host_network is not None:
        # The only other Pod-level field that changes a scheduling decision: two
        # host-network Pods conflict on a host port, and nothing else does.
        emitted["hostNetwork"] = pod_spec.host_network
    emitted.update(_placement_fields(spec))
    return {"metadata": {"labels": labels, "annotations": annotations}, "spec": emitted}


def _probe_container(container: Any) -> dict[str, Any]:
    """Mirror one workload container as an inert reservation.

    Args:
        container: The workload container.

    Returns:
        The probe container: the same name, the same resources and the same host
        ports, and an image that is never pulled because the Pod never starts.
    """
    emitted: dict[str, Any] = {
        "name": container.name,
        "image": PROBE_IMAGE,
        "imagePullPolicy": "IfNotPresent",
    }
    if container.resources is not None:
        emitted["resources"] = container.resources.model_dump(by_alias=True, exclude_none=True)
    ports = [
        port.model_dump(by_alias=True, exclude_none=True)
        for port in container.ports or []
        if port.host_port is not None
    ]
    if ports:
        emitted["ports"] = ports
    return emitted


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
    """Return the owner reference tying a probe workload to its StaticSet.

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
    """Return the nodes a DaemonSet-mode placement is expected on, conservatively.

    This is a *filter*, not a scheduler: it understands the node-selector and
    node-affinity subset a StaticSet can express, plus cordoning. The DaemonSet
    controller remains authoritative for where a probe Pod actually lands; this
    function only tells the operator how many placements a complete pass should
    produce, so that a missing one can be noticed and a new pass started.

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
    """Return whether a node satisfies the required node affinity.

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
