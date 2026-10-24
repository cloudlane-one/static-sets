"""The scheduling probe: what it copies, what it must never copy, and why."""

from __future__ import annotations

from typing import Any

import pytest

from static_sets.naming import (
    GENERATION_LABEL,
    PROBE_LABEL,
    RUNTIME_CLASS_LABEL,
    SET_UID_LABEL,
)
from static_sets.probe import (
    PROBE_IMAGE,
    PROBE_RUNTIME_CLASS_ENV,
    NodeInfo,
    ObservedProbePod,
    ObservedProbeWorkload,
    eligible_nodes,
    probe_workload,
)


def probe(make_static_set: Any, **overrides: Any) -> tuple[str, dict[str, Any]]:
    """Build a probe for the base StaticSet.

    Args:
        make_static_set: The fixture factory.
        **overrides: StaticSet body overrides.

    Returns:
        The kind and manifest.
    """
    return probe_workload(make_static_set(**overrides), namespace="edge", generation=3)


def test_deployment_mode_asks_for_a_replicaset_with_the_replica_count(make_static_set: Any) -> None:
    """Deployment mode is realised by a ReplicaSet, one placement per replica."""
    kind, manifest = probe(make_static_set)

    assert kind == "ReplicaSet"
    assert manifest["spec"]["replicas"] == 2
    assert manifest["metadata"]["name"] == "web-probe-3"
    assert manifest["metadata"]["namespace"] == "edge"


def test_daemonset_mode_asks_for_a_daemonset_and_never_rolls_it(make_static_set: Any) -> None:
    """A DaemonSet probe has no replica count, and its controller must not roll it."""
    kind, manifest = probe(make_static_set, spec={"mode": "DaemonSet", "replicas": None})

    assert kind == "DaemonSet"
    assert "replicas" not in manifest["spec"]
    assert manifest["spec"]["updateStrategy"] == {"type": "OnDelete"}


def test_the_probe_carries_the_workload_resources_and_nothing_runnable(
    make_static_set: Any,
) -> None:
    """The containers reserve exactly what the workload asked for, and cannot run."""
    _, manifest = probe(
        make_static_set,
        spec={
            "template": {
                "spec": {
                    "containers": [
                        {
                            "name": "app",
                            "image": "registry.example.com/web:1.27",
                            "resources": {
                                "requests": {"cpu": "250m", "memory": "512Mi"},
                                "limits": {"memory": "1Gi"},
                            },
                            "command": ["/bin/web"],
                            "env": [{"name": "MODE", "value": "prod"}],
                        }
                    ]
                }
            }
        },
    )

    container = manifest["spec"]["template"]["spec"]["containers"][0]

    assert container["name"] == "app"
    assert container["resources"]["requests"] == {"cpu": "250m", "memory": "512Mi"}
    assert container["resources"]["limits"] == {"memory": "1Gi"}
    # The image the workload asked for is not what the probe declares: nothing from
    # the template may be in a position to execute.
    assert container["image"] == PROBE_IMAGE
    assert "command" not in container
    assert "env" not in container


def test_init_containers_are_mirrored_for_their_resource_effect(make_static_set: Any) -> None:
    """An init container's requests affect scheduling, so the probe reserves them too."""
    _, manifest = probe(
        make_static_set,
        spec={
            "template": {
                "spec": {
                    "initContainers": [
                        {
                            "name": "prepare",
                            "image": "registry.example.com/prep:1",
                            "resources": {"requests": {"cpu": "2"}},
                        }
                    ]
                }
            }
        },
    )

    init = manifest["spec"]["template"]["spec"]["initContainers"][0]

    assert init["name"] == "prepare"
    assert init["resources"]["requests"] == {"cpu": "2"}
    assert init["image"] == PROBE_IMAGE


def test_host_ports_are_reserved_so_the_scheduler_avoids_conflicts(make_static_set: Any) -> None:
    """A published host port is what makes two Pods conflict, so it is copied."""
    _, manifest = probe(
        make_static_set,
        spec={
            "template": {
                "spec": {
                    "containers": [
                        {
                            "name": "app",
                            "image": "registry.example.com/web:1.27",
                            "ports": [
                                {"containerPort": 8080, "name": "http"},
                                {"containerPort": 9090, "hostPort": 9090},
                            ],
                        }
                    ]
                }
            }
        },
    )

    ports = manifest["spec"]["template"]["spec"]["containers"][0]["ports"]

    assert ports == [{"containerPort": 9090, "hostPort": 9090, "protocol": "TCP"}]


def test_placement_fields_are_copied_onto_the_probe(make_static_set: Any) -> None:
    """``spec.placement`` steers the scheduler, so the probe is what carries it."""
    _, manifest = probe(
        make_static_set,
        spec={
            "placement": {
                "nodeSelector": {"kubernetes.io/os": "linux"},
                "tolerations": [{"key": "edge", "operator": "Exists", "effect": "NoSchedule"}],
                "topologySpreadConstraints": [{"maxSkew": 1, "topologyKey": "zone"}],
            }
        },
    )

    spec = manifest["spec"]["template"]["spec"]

    assert spec["nodeSelector"] == {"kubernetes.io/os": "linux"}
    assert spec["tolerations"][0]["key"] == "edge"
    assert spec["topologySpreadConstraints"][0]["topologyKey"] == "zone"


def test_the_probe_runs_under_the_placeholder_runtime_class(
    make_static_set: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The probe asks for a class no node can start, which is what keeps it inert."""
    monkeypatch.setenv(PROBE_RUNTIME_CLASS_ENV, "custom-probe-class")

    _, manifest = probe(make_static_set)

    assert manifest["spec"]["template"]["spec"]["runtimeClassName"] == "custom-probe-class"


def test_the_intended_runtime_class_is_carried_on_a_label(make_static_set: Any) -> None:
    """The class the static Pod really uses cannot be the probe's own, so it travels."""
    _, manifest = probe(
        make_static_set,
        spec={"template": {"spec": {"runtimeClassName": "nvidia.com/gpu-runtime"}}},
    )

    template = manifest["spec"]["template"]

    assert template["metadata"]["labels"][RUNTIME_CLASS_LABEL] == "nvidia.com/gpu-runtime"
    assert template["spec"]["runtimeClassName"] != "nvidia.com/gpu-runtime"


def test_the_probe_selector_is_unique_per_generation(make_static_set: Any) -> None:
    """A new generation must never adopt the previous probe's Pods."""
    static_set = make_static_set()
    labels = static_set.metadata.labels or {}

    first = probe_workload(static_set, namespace="edge", generation=3)
    second = probe_workload(static_set, namespace="edge", generation=4)

    first_selector = first[1]["spec"]["selector"]["matchLabels"]
    second_selector = second[1]["spec"]["selector"]["matchLabels"]

    assert first_selector[GENERATION_LABEL] == "3"
    assert second_selector[GENERATION_LABEL] == "4"
    assert first_selector != second_selector
    assert first_selector[SET_UID_LABEL] == "0f9a4b6c-1111-2222-3333-444455556666"
    assert first_selector[PROBE_LABEL] == "true"
    assert labels == {}


def test_the_probe_pods_carry_the_template_labels_for_spread_constraints(
    make_static_set: Any,
) -> None:
    """A topology spread constraint that selects the workload's labels must match."""
    _, manifest = probe(make_static_set)

    assert manifest["spec"]["template"]["metadata"]["labels"]["app"] == "web"


def test_a_complete_probe_has_every_pod_scheduled() -> None:
    """Completeness is judged from the objects and their bindings, never readiness."""
    complete = ObservedProbeWorkload(
        kind="ReplicaSet",
        name="web-probe-3",
        generation=3,
        replicas=2,
        status_seen=True,
        pods=(ObservedProbePod("a", "node-a"), ObservedProbePod("b", "node-b")),
    )
    incomplete = ObservedProbeWorkload(
        kind="ReplicaSet",
        name="web-probe-3",
        generation=3,
        replicas=2,
        status_seen=True,
        pods=(ObservedProbePod("a", "node-a"), ObservedProbePod("b")),
    )
    unseen = ObservedProbeWorkload(
        kind="ReplicaSet", name="web-probe-3", generation=3, replicas=2, status_seen=False
    )

    assert complete.complete is True
    assert incomplete.complete is False
    assert incomplete.unscheduled == 1
    assert unseen.complete is False


def test_a_terminating_probe_pod_is_not_a_placement() -> None:
    """A Pod that is being deleted no longer says where anything runs."""
    probe_workload_ = ObservedProbeWorkload(
        kind="DaemonSet",
        name="web-probe-3",
        generation=3,
        replicas=1,
        status_seen=True,
        pods=(ObservedProbePod("a", "node-a", deleting=True),),
    )

    assert probe_workload_.scheduled == ()


def test_a_zero_replica_probe_is_complete() -> None:
    """Scaling to zero asks the scheduler for nothing, which is a finished pass."""
    probe_workload_ = ObservedProbeWorkload(
        kind="ReplicaSet", name="web-probe-3", generation=3, replicas=0, status_seen=True
    )

    assert probe_workload_.complete is True


def test_eligible_nodes_applies_the_node_selector(make_static_set: Any) -> None:
    """The expectation filter understands a plain node selector."""
    nodes = [
        NodeInfo("a", {"pool": "edge"}),
        NodeInfo("b", {"pool": "core"}),
        NodeInfo("c", {}),
    ]

    spec = make_static_set(spec={"placement": {"nodeSelector": {"pool": "edge"}}}).spec

    assert eligible_nodes(nodes, spec) == ["a"]


def test_eligible_nodes_compares_numeric_labels(make_static_set: Any) -> None:
    """``Gt`` and ``Lt`` compare numerically, and a non-numeric label fails closed."""
    nodes = [
        NodeInfo("a", {"cores": "4"}),
        NodeInfo("b", {"cores": "16"}),
        NodeInfo("c", {"cores": "x"}),
    ]
    spec = make_static_set(
        spec={
            "mode": "DaemonSet",
            "replicas": None,
            "placement": {
                "affinity": {
                    "nodeAffinity": {
                        "requiredDuringSchedulingIgnoredDuringExecution": {
                            "nodeSelectorTerms": [
                                {
                                    "matchExpressions": [
                                        {"key": "cores", "operator": "Gt", "values": ["8"]}
                                    ]
                                }
                            ]
                        }
                    }
                }
            },
        }
    ).spec

    assert eligible_nodes(nodes, spec) == ["b"]


def test_cordoned_nodes_need_a_toleration(make_static_set: Any) -> None:
    """A cordoned node is only expected to host a placement it tolerates."""
    nodes = [NodeInfo("a", {}, unschedulable=True)]

    assert (
        eligible_nodes(nodes, make_static_set(spec={"mode": "DaemonSet", "replicas": None}).spec)
        == []
    )

    tolerating = make_static_set(
        spec={
            "mode": "DaemonSet",
            "replicas": None,
            "placement": {
                "tolerations": [{"key": "node.kubernetes.io/unschedulable", "operator": "Exists"}]
            },
        }
    ).spec

    assert eligible_nodes(nodes, tolerating) == ["a"]
