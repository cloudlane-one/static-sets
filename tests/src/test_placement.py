"""Installer placement and the installer workload manifest."""

from __future__ import annotations

from typing import Any

import pytest

from static_sets.naming import installer_labels
from static_sets.placement import (
    InstallerPod,
    NodeInfo,
    eligible_nodes,
    installer_container_resources,
    installer_workload,
    selector_for,
)


def nodes(*specs: tuple[str, dict[str, str]]) -> list[NodeInfo]:
    """Build a list of ready, schedulable nodes.

    Args:
        *specs: ``(name, labels)`` pairs.

    Returns:
        The nodes.
    """
    return [NodeInfo(name=name, labels=labels) for name, labels in specs]


def test_deployment_workload_carries_replicas_and_recreates(make_static_set: Any) -> None:
    """Deployment mode creates a Deployment with the requested replica count."""
    resource = make_static_set(spec={"replicas": 4})

    kind, manifest = installer_workload(resource, namespace="edge", generation=3)

    assert kind == "Deployment"
    assert manifest["apiVersion"] == "apps/v1"
    assert manifest["spec"]["replicas"] == 4
    assert manifest["spec"]["strategy"] == {"type": "Recreate"}
    assert manifest["metadata"]["name"] == "web-installer-deployment"
    assert manifest["metadata"]["ownerReferences"][0]["uid"] == resource.metadata.uid


def test_daemonset_workload_has_no_replica_count(make_static_set: Any) -> None:
    """DaemonSet mode lets the DaemonSet controller decide the count."""
    resource = make_static_set(spec={"mode": "DaemonSet", "replicas": None})

    kind, manifest = installer_workload(resource, namespace="edge", generation=1)

    assert kind == "DaemonSet"
    assert "replicas" not in manifest["spec"]
    assert manifest["spec"]["updateStrategy"] == {"type": "OnDelete"}
    assert manifest["metadata"]["name"] == "web-installer-daemonset"


def test_workload_declares_only_the_two_host_mounts(make_static_set: Any) -> None:
    """The installer's host access is exactly kubelet's path and the bundle."""
    resource = make_static_set()

    _, manifest = installer_workload(resource, namespace="edge", generation=1)
    pod_spec = manifest["spec"]["template"]["spec"]

    assert pod_spec["volumes"] == [
        {
            "name": "static-pod-path",
            "hostPath": {"path": "/etc/kubernetes/manifests", "type": "Directory"},
        },
        {
            "name": "bundle",
            "hostPath": {"path": "/var/lib/static-sets", "type": "DirectoryOrCreate"},
        },
    ]
    container = pod_spec["containers"][0]
    assert container["securityContext"]["privileged"] is False
    assert container["securityContext"]["readOnlyRootFilesystem"] is True
    assert container["securityContext"]["capabilities"] == {"drop": ["ALL"]}


def test_workload_carries_placement_and_provenance(make_static_set: Any) -> None:
    """Scheduler placement and the ownership tokens reach the Pod template."""
    resource = make_static_set(
        spec={
            "placement": {
                "nodeSelector": {"role": "edge"},
                "tolerations": [{"key": "dedicated", "operator": "Exists"}],
            }
        }
    )

    _, manifest = installer_workload(resource, namespace="edge", generation=7)
    template = manifest["spec"]["template"]
    pod_spec = template["spec"]

    assert pod_spec["nodeSelector"] == {"role": "edge"}
    assert pod_spec["tolerations"] == [{"key": "dedicated", "operator": "Exists"}]
    assert template["metadata"]["annotations"]["staticsets.cloudlane.dev/generation"] == "7"
    assert template["metadata"]["annotations"]["staticsets.cloudlane.dev/static-set-uid"] == (
        resource.metadata.uid
    )
    assert manifest["spec"]["selector"]["matchLabels"] == selector_for(resource)


def test_installer_learns_its_node_from_the_downward_api(make_static_set: Any) -> None:
    """The installer never guesses which node it is on."""
    resource = make_static_set()

    _, manifest = installer_workload(resource, namespace="edge", generation=1)
    environment = {
        item["name"]: item for item in manifest["spec"]["template"]["spec"]["containers"][0]["env"]
    }

    assert environment["STATIC_SETS_NODE"]["valueFrom"]["fieldRef"]["fieldPath"] == "spec.nodeName"
    assert environment["STATIC_SETS_POD_NAME"]["valueFrom"]["fieldRef"]["fieldPath"] == (
        "metadata.name"
    )
    assert environment["STATIC_SETS_UID"]["value"] == resource.metadata.uid
    assert environment["STATIC_SETS_NAMESPACE"]["value"] == "edge"


def test_installer_reservation_is_small_and_overridable(make_static_set: Any) -> None:
    """The installer reserves a little, deliberately, and can be told otherwise."""
    default = make_static_set()

    assert installer_container_resources(default.spec) == {
        "requests": {"cpu": "10m", "memory": "64Mi"},
        "limits": {"memory": "256Mi"},
    }

    explicit = make_static_set(
        spec={"installer": {"resources": {"requests": {"cpu": "1", "memory": "1Gi"}}}}
    )

    assert installer_container_resources(explicit.spec) == {
        "requests": {"cpu": "1", "memory": "1Gi"}
    }


def test_selector_labels_stay_within_the_label_limit(make_static_set: Any) -> None:
    """A very long StaticSet name still yields a valid selector."""
    long_name = "a" + "b" * 80

    labels = installer_labels(long_name)

    value = labels["staticsets.cloudlane.dev/set"]
    assert len(value) <= 63
    assert value.startswith("a")


def test_eligible_nodes_applies_the_node_selector(make_static_set: Any) -> None:
    """Only nodes carrying the selector's labels are eligible."""
    resource = make_static_set(spec={"placement": {"nodeSelector": {"role": "edge"}}})
    cluster = nodes(("a", {"role": "edge"}), ("b", {"role": "core"}), ("c", {}))

    assert eligible_nodes(cluster, resource.spec) == ["a"]


def test_eligible_nodes_applies_required_node_affinity(make_static_set: Any) -> None:
    """Required node affinity is evaluated for the supported operators."""
    resource = make_static_set(
        spec={
            "placement": {
                "affinity": {
                    "nodeAffinity": {
                        "requiredDuringSchedulingIgnoredDuringExecution": {
                            "nodeSelectorTerms": [
                                {
                                    "matchExpressions": [
                                        {"key": "zone", "operator": "In", "values": ["a", "b"]},
                                        {"key": "gpu", "operator": "Exists"},
                                    ]
                                }
                            ]
                        }
                    }
                }
            }
        }
    )
    cluster = nodes(
        ("keep", {"zone": "a", "gpu": "true"}),
        ("wrong-zone", {"zone": "c", "gpu": "true"}),
        ("no-gpu", {"zone": "b"}),
    )

    assert eligible_nodes(cluster, resource.spec) == ["keep"]


def test_eligible_nodes_honours_not_in_and_does_not_exist(make_static_set: Any) -> None:
    """``NotIn`` and ``DoesNotExist`` match in the way the scheduler expects."""
    resource = make_static_set(
        spec={
            "placement": {
                "affinity": {
                    "nodeAffinity": {
                        "requiredDuringSchedulingIgnoredDuringExecution": {
                            "nodeSelectorTerms": [
                                {
                                    "matchExpressions": [
                                        {"key": "zone", "operator": "NotIn", "values": ["c"]},
                                        {"key": "gpu", "operator": "DoesNotExist"},
                                    ]
                                }
                            ]
                        }
                    }
                }
            }
        }
    )
    cluster = nodes(
        ("ok", {"zone": "a"}),
        ("excluded", {"zone": "c"}),
        ("has-gpu", {"zone": "a", "gpu": "true"}),
    )

    assert eligible_nodes(cluster, resource.spec) == ["ok"]


@pytest.mark.parametrize(
    ("operator", "value", "expected"),
    [("Gt", "4", True), ("Gt", "8", False), ("Lt", "8", True), ("Lt", "4", False)],
)
def test_eligible_nodes_compares_numeric_labels(
    make_static_set: Any, operator: str, value: str, expected: bool
) -> None:
    """``Gt`` and ``Lt`` compare integer label values."""
    resource = make_static_set(
        spec={
            "placement": {
                "affinity": {
                    "nodeAffinity": {
                        "requiredDuringSchedulingIgnoredDuringExecution": {
                            "nodeSelectorTerms": [
                                {
                                    "matchExpressions": [
                                        {"key": "cores", "operator": operator, "values": [value]}
                                    ]
                                }
                            ]
                        }
                    }
                }
            }
        }
    )
    cluster = nodes(("node", {"cores": "6"}))

    assert (eligible_nodes(cluster, resource.spec) == ["node"]) is expected


def test_cordoned_nodes_need_a_toleration(make_static_set: Any) -> None:
    """A cordoned node is skipped unless the StaticSet tolerates it."""
    plain = make_static_set()
    tolerating = make_static_set(
        spec={
            "placement": {
                "tolerations": [{"key": "node.kubernetes.io/unschedulable", "operator": "Exists"}]
            }
        }
    )
    cordoned = [NodeInfo(name="a", unschedulable=True)]

    assert eligible_nodes(cordoned, plain.spec) == []
    assert eligible_nodes(cordoned, tolerating.spec) == ["a"]


def test_missing_values_never_match_a_comparison(make_static_set: Any) -> None:
    """A comparison against an absent label fails closed."""
    resource = make_static_set(
        spec={
            "placement": {
                "affinity": {
                    "nodeAffinity": {
                        "requiredDuringSchedulingIgnoredDuringExecution": {
                            "nodeSelectorTerms": [
                                {
                                    "matchExpressions": [
                                        {"key": "cores", "operator": "Gt", "values": []}
                                    ]
                                }
                            ]
                        }
                    }
                }
            }
        }
    )

    assert eligible_nodes(nodes(("a", {})), resource.spec) == []


def test_installer_pod_records_its_deletion_state() -> None:
    """A terminating installer no longer serves its node."""
    pod = InstallerPod(name="p", uid="u", node="node-a", phase="Running", deleting=True)

    assert pod.deleting is True
    assert pod.node == "node-a"


def test_surge_strategy_is_honoured_by_the_installer_workload(make_static_set: Any) -> None:
    """A declared surge becomes a rolling update on the installer Deployment."""
    resource = make_static_set(
        spec={"replicas": 2, "update": {"strategy": "Surge", "maxSurge": 1, "maxUnavailable": 0}}
    )

    kind, manifest = installer_workload(resource, namespace="edge", generation=1)

    assert kind == "Deployment"
    assert manifest["spec"]["strategy"] == {
        "type": "RollingUpdate",
        "rollingUpdate": {"maxSurge": 1, "maxUnavailable": 0},
    }


def test_recreate_strategy_stays_recreate(make_static_set: Any) -> None:
    """The conservative default replaces the installer rather than rolling it."""
    resource = make_static_set(spec={"replicas": 1})

    _, manifest = installer_workload(resource, namespace="edge", generation=1)

    assert manifest["spec"]["strategy"] == {"type": "Recreate"}
