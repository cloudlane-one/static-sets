"""The StaticSet custom resource: generation, defaults and validation."""

from __future__ import annotations

from typing import Any

import based_operators as bk
import pytest
from pydantic import ValidationError

from static_sets.errors import DefinitionError
from static_sets.models import (
    ImageProvisioning,
    InstallerSpec,
    Mode,
    NodeUnreachablePolicy,
    StaticSet,
    UpdateStrategy,
)


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
        "installer",
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
    installer = spec["properties"]["installer"]
    assert installer["properties"]["staticPodPath"]["default"] == "/etc/kubernetes/manifests"


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
    assert resource.spec.installer.static_pod_path == "/etc/kubernetes/manifests"
    assert resource.spec.installer.bundle_dir == "/var/lib/static-sets"


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


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("static_pod_path", "etc/kubernetes/manifests"),
        ("bundle_dir", "var/lib/static-sets"),
    ],
)
def test_installer_paths_must_be_absolute(field: str, value: str) -> None:
    """A relative host path is refused."""
    with pytest.raises(ValidationError) as error:
        InstallerSpec.model_validate({"image": "img", field: value})

    assert "must be an absolute path" in str(error.value)


@pytest.mark.parametrize(
    ("static_pod_path", "bundle_dir"),
    [
        ("/etc/kubernetes/manifests", "/etc/kubernetes/manifests"),
        ("/etc/kubernetes/manifests", "/etc/kubernetes/manifests/tmp"),
        ("/etc/kubernetes", "/etc/kubernetes/manifests"),
    ],
)
def test_installer_paths_must_be_disjoint(static_pod_path: str, bundle_dir: str) -> None:
    """The bundle must not live inside, or contain, kubelet's watched directory."""
    with pytest.raises(ValidationError):
        InstallerSpec.model_validate(
            {"image": "img", "staticPodPath": static_pod_path, "bundleDir": bundle_dir}
        )


def test_definition_error_is_a_library_error() -> None:
    """Definition errors are part of the library's error hierarchy."""
    from static_sets.errors import StaticSetsError

    assert issubclass(DefinitionError, StaticSetsError)
