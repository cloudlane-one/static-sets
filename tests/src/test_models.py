"""The StaticSet custom resource: generation, defaults and validation."""

from __future__ import annotations

from typing import Any

import based_operators as bk
import pytest
from pydantic import ValidationError

from static_sets.errors import DefinitionError
from static_sets.models import (
    CleanupReason,
    ImageProvisioning,
    Mode,
    NodeUnreachablePolicy,
    PlacementPhase,
    StaticSet,
    UpdateStrategy,
)
from static_sets.placements import PlacementSpec, StaticPodPlacement


def test_crd_is_structurally_valid_and_namespaced() -> None:
    """The generated CRD has the identity, scope and status subresource we expect."""
    manifest = bk.crd_manifest(StaticSet)
    spec = manifest["spec"]

    assert manifest["apiVersion"] == "apiextensions.k8s.io/v1"
    assert spec["group"] == "staticsets.cloudlane.dev"
    assert spec["scope"] == "Namespaced"
    assert spec["names"]["plural"] == "staticsets"
    assert spec["names"]["kind"] == "StaticSet"
    assert spec["names"]["shortNames"] == ["sset"]
    assert spec["versions"][0]["name"] == "v1"
    assert spec["versions"][0]["subresources"] == {"status": {}}


def test_crd_schema_uses_wire_names_and_constraints() -> None:
    """The schema carries camelCase names and the constraints the API server enforces."""
    schema = bk.crd_manifest(StaticSet)["spec"]["versions"][0]["schema"]["openAPIV3Schema"]
    spec = schema["properties"]["spec"]

    assert sorted(spec["properties"]) == [
        "cleanup",
        "mode",
        "offline",
        "placement",
        "policy",
        "replicas",
        "template",
        "update",
    ]
    assert spec["properties"]["mode"]["enum"] == ["Deployment", "DaemonSet"]
    assert spec["properties"]["replicas"]["maximum"] == 64
    runtime_class = spec["properties"]["template"]["properties"]["spec"]["properties"][
        "runtimeClassName"
    ]
    assert runtime_class["maxLength"] == 63


def test_status_schema_separates_installed_state_from_health() -> None:
    """Installed placements and observed health are distinct status fields."""
    schema = bk.crd_manifest(StaticSet)["spec"]["versions"][0]["schema"]["openAPIV3Schema"]
    status = schema["properties"]["status"]["properties"]

    assert set(status) >= {"observedGeneration", "desiredRevision", "placements", "health"}
    assert "cleanup" in status


def test_minimal_spec_applies_documented_defaults(make_static_set: Any) -> None:
    """Unset policy fields take their documented defaults."""
    resource = make_static_set(spec={"replicas": 1})

    assert resource.spec.update.strategy is UpdateStrategy.RECREATE
    assert resource.spec.update.max_surge == 0
    assert resource.spec.offline.image_provisioning is ImageProvisioning.EXTERNALLY_PROVISIONED
    assert resource.spec.offline.retained_revisions == 2
    assert resource.spec.cleanup.on_node_unreachable is NodeUnreachablePolicy.RETAIN
    assert resource.spec.policy.allow_privileged is False
    assert resource.spec.policy.allow_inline_secret_env is False


def test_replicas_required_for_deployment_mode(make_resource: Any) -> None:
    """Deployment mode without replicas is rejected."""
    body = make_resource(spec={"replicas": None})

    with pytest.raises(ValidationError) as error:
        StaticSet.model_validate(body)

    assert "spec.replicas is required when mode is Deployment" in str(error.value)


def test_replicas_invalid_for_daemonset_mode(make_resource: Any) -> None:
    """DaemonSet mode with replicas is rejected."""
    body = make_resource(spec={"mode": "DaemonSet", "replicas": 2})

    with pytest.raises(ValidationError) as error:
        StaticSet.model_validate(body)

    assert "spec.replicas is invalid when mode is DaemonSet" in str(error.value)


def test_daemonset_mode_without_replicas_is_accepted(make_static_set: Any) -> None:
    """DaemonSet mode needs no replica count."""
    resource = make_static_set(spec={"mode": "DaemonSet", "replicas": None})

    assert resource.spec.mode is Mode.DAEMON_SET
    assert resource.spec.replicas is None


def test_recreate_strategy_rejects_surge(make_resource: Any) -> None:
    """Recreate cannot be combined with a surge, because it never overlaps."""
    body = make_resource(spec={"update": {"strategy": "Recreate", "maxSurge": 2}})

    with pytest.raises(ValidationError) as error:
        StaticSet.model_validate(body)

    assert "maxSurge" in str(error.value)


def test_unknown_spec_field_is_rejected(make_resource: Any) -> None:
    """An unknown field is refused rather than pruned silently by the operator."""
    body = make_resource(spec={"replicas": 1})
    body["spec"]["somethingElse"] = True

    with pytest.raises(ValidationError):
        StaticSet.model_validate(body)


def test_the_placement_record_is_its_own_namespaced_resource() -> None:
    """The placement record has the identity, scope and status subresource it needs."""
    manifest = bk.crd_manifest(StaticPodPlacement)
    spec = manifest["spec"]

    assert spec["group"] == "staticsets.cloudlane.dev"
    assert spec["scope"] == "Namespaced"
    assert spec["names"]["kind"] == "StaticPodPlacement"
    assert spec["names"]["plural"] == "staticpodplacements"
    assert spec["names"]["shortNames"] == ["spp"]
    assert spec["versions"][0]["subresources"] == {"status": {}}


def test_a_removal_must_say_why() -> None:
    """An undesired placement without a reason is refused."""
    with pytest.raises(ValidationError) as error:
        PlacementSpec(
            static_set="web",
            static_set_uid="uid",
            node="worker-1",
            generation=1,
            desired=False,
        )

    assert "spec.reason is required" in str(error.value)


def test_a_desired_placement_carries_no_removal_reason() -> None:
    """A wanted placement cannot carry a cleanup reason, which would be a contradiction."""
    with pytest.raises(ValidationError) as error:
        PlacementSpec(
            static_set="web",
            static_set_uid="uid",
            node="worker-1",
            generation=1,
            desired=True,
            reason=CleanupReason.SCALED_DOWN,
        )

    assert "only valid when spec.desired is false" in str(error.value)


def test_a_placement_record_defaults_to_a_pending_state() -> None:
    """A record the manager has not reported on yet reads as pending, not as installed."""
    spec = PlacementSpec(static_set="web", static_set_uid="uid", node="worker-1", generation=2)

    assert spec.desired is True
    assert spec.remove_configuration is True
    assert PlacementPhase.PENDING.value == "Pending"


def test_definition_error_is_a_library_error() -> None:
    """Definition errors are part of the library's error hierarchy."""
    from static_sets.errors import StaticSetsError

    assert issubclass(DefinitionError, StaticSetsError)


def _placement_error(body: dict[str, Any]) -> str:
    """Validate a resource and return the placement error it raised.

    Args:
        body: The wire-shaped resource.

    Returns:
        The validation message.
    """
    with pytest.raises(ValidationError) as error:
        StaticSet.model_validate(body)
    return str(error.value)


def test_placement_refuses_pod_affinity(make_resource: Any) -> None:
    """Pod affinity is refused instead of accepted and then ignored by the planner."""
    body = make_resource(
        spec={
            "placement": {
                "affinity": {
                    "podAffinity": {
                        "requiredDuringSchedulingIgnoredDuringExecution": [
                            {"topologyKey": "zone", "labelSelector": {"matchLabels": {"a": "b"}}}
                        ]
                    }
                }
            }
        }
    )

    message = _placement_error(body)

    assert "podAffinity" in message
    assert "not supported" in message


def test_placement_refuses_an_unknown_node_selector_operator(make_resource: Any) -> None:
    """The planner can only evaluate the documented node-selector operators."""
    body = make_resource(
        spec={
            "placement": {
                "affinity": {
                    "nodeAffinity": {
                        "requiredDuringSchedulingIgnoredDuringExecution": {
                            "nodeSelectorTerms": [
                                {"matchExpressions": [{"key": "zone", "operator": "Near"}]}
                            ]
                        }
                    }
                }
            }
        }
    )

    message = _placement_error(body)

    assert "'Near'" in message
    assert "not supported" in message


def test_placement_refuses_an_unknown_toleration_effect(make_resource: Any) -> None:
    """A toleration effect is checked against the API server's set."""
    body = make_resource(spec={"placement": {"tolerations": [{"key": "k", "effect": "Maybe"}]}})

    message = _placement_error(body)

    assert "'Maybe'" in message
    assert "NoSchedule" in message


def test_placement_refuses_an_out_of_range_weight(make_resource: Any) -> None:
    """A preferred term's weight must be 1-100, which the official type does not check."""
    body = make_resource(
        spec={
            "placement": {
                "affinity": {
                    "nodeAffinity": {
                        "preferredDuringSchedulingIgnoredDuringExecution": [
                            {"weight": 0, "preference": {"matchExpressions": []}}
                        ]
                    }
                }
            }
        }
    )

    assert "1-100" in _placement_error(body)


def test_placement_constrains_topology_spread_fields(make_resource: Any) -> None:
    """The skew and key are constrained, and the spread policy defaults as before."""
    invalid = make_resource(
        spec={
            "placement": {
                "topologySpreadConstraints": [
                    {"maxSkew": 0, "topologyKey": "zone", "whenUnsatisfiable": "DoNotSchedule"}
                ]
            }
        }
    )

    assert "greater than or equal to 1" in _placement_error(invalid)

    valid = StaticSet.model_validate(
        make_resource(
            spec={
                "placement": {"topologySpreadConstraints": [{"maxSkew": 1, "topologyKey": "zone"}]}
            }
        )
    )

    constraints = valid.spec.placement.topology_spread_constraints or []
    assert constraints[0].when_unsatisfiable == "DoNotSchedule"


def test_placement_refuses_an_unknown_label_selector_operator(make_resource: Any) -> None:
    """A topology spread selector's operator is checked against the API server's set."""
    body = make_resource(
        spec={
            "placement": {
                "topologySpreadConstraints": [
                    {
                        "maxSkew": 1,
                        "topologyKey": "zone",
                        "whenUnsatisfiable": "DoNotSchedule",
                        "labelSelector": {"matchExpressions": [{"key": "app", "operator": "Like"}]},
                    }
                ]
            }
        }
    )

    assert "'Like'" in _placement_error(body)
