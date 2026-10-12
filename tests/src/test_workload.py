"""The supported workload subset: what compiles, and what is refused."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from static_sets.workload import PodSpec, PodTemplate, Volume, VolumeMount


def container(**overrides: Any) -> dict[str, Any]:
    """Return a minimal container body with overrides applied.

    Args:
        **overrides: Fields to merge into the container.

    Returns:
        The container body.
    """
    base: dict[str, Any] = {"name": "app", "image": "img:1"}
    base.update(overrides)
    return base


def test_minimal_template_is_accepted() -> None:
    """A template from the supported subset validates."""
    template = PodTemplate.model_validate({"spec": {"containers": [container()]}})

    assert template.spec.containers[0].image == "img:1"
    assert template.spec.restart_policy == "Always"


@pytest.mark.parametrize(
    "field",
    [
        "serviceAccountName",
        "automountServiceAccountToken",
        "imagePullSecrets",
        "schedulerName",
        "priorityClassName",
        "enableServiceLinks",
    ],
)
def test_api_dependent_fields_are_rejected(field: str) -> None:
    """Template fields that need the API server or the scheduler are refused."""
    values = {
        "serviceAccountName": "svc",
        "automountServiceAccountToken": True,
        "imagePullSecrets": [{"name": "pull"}],
        "schedulerName": "default-scheduler",
        "priorityClassName": "high",
        "enableServiceLinks": True,
    }

    with pytest.raises(ValidationError) as error:
        PodSpec.model_validate({"containers": [container()], field: values[field]})

    assert field in str(error.value)


@pytest.mark.parametrize(
    "field",
    ["nodeSelector", "affinity", "tolerations", "topologySpreadConstraints"],
)
def test_placement_fields_are_rejected_in_the_template(field: str) -> None:
    """Placement belongs to spec.placement, not to the emitted static workload."""
    values = {
        "nodeSelector": {"role": "edge"},
        "affinity": {"nodeAffinity": {}},
        "tolerations": [{"key": "x"}],
        "topologySpreadConstraints": [{"maxSkew": 1, "topologyKey": "zone"}],
    }

    with pytest.raises(ValidationError) as error:
        PodSpec.model_validate({"containers": [container()], field: values[field]})

    assert "spec.placement" in str(error.value)


@pytest.mark.parametrize(
    "source",
    ["hostPath", "persistentVolumeClaim", "projected", "downwardAPI"],
)
def test_api_dependent_volume_sources_are_rejected(source: str) -> None:
    """Volume sources that need the API server, or escape the bundle, are refused."""
    values = {
        "hostPath": {"path": "/etc"},
        "persistentVolumeClaim": {"claimName": "data"},
        "projected": {"sources": []},
        "downwardAPI": {"items": []},
    }

    with pytest.raises(ValidationError) as error:
        Volume.model_validate({"name": "v", source: values[source]})

    assert source in str(error.value)


def test_volume_without_a_supported_source_is_rejected() -> None:
    """A volume that names nothing the operator can materialize is refused."""
    with pytest.raises(ValidationError) as error:
        Volume.model_validate({"name": "v"})

    assert "sets no supported source" in str(error.value)


@pytest.mark.parametrize(
    "field",
    ["subPath", "subPathExpr", "mountPropagation", "recursiveReadOnly"],
)
def test_unsupported_mount_semantics_are_rejected(field: str) -> None:
    """Mount options that change path semantics are refused explicitly."""
    values = {
        "subPath": "a/b",
        "subPathExpr": "$(POD_NAME)",
        "mountPropagation": "Bidirectional",
        "recursiveReadOnly": "Enabled",
    }

    with pytest.raises(ValidationError) as error:
        VolumeMount.model_validate({"name": "v", "mountPath": "/m", field: values[field]})

    assert field in str(error.value)


def test_env_from_requires_exactly_one_source() -> None:
    """An envFrom entry must name one source, not zero and not two."""
    with pytest.raises(ValidationError):
        PodSpec.model_validate({"containers": [container(envFrom=[{"prefix": "P_"}])]})

    with pytest.raises(ValidationError):
        PodSpec.model_validate(
            {
                "containers": [
                    container(
                        envFrom=[
                            {
                                "configMapRef": {"name": "c"},
                                "secretRef": {"name": "s"},
                            }
                        ]
                    )
                ]
            }
        )


def test_duplicate_container_names_are_rejected() -> None:
    """Two containers may not share a name."""
    with pytest.raises(ValidationError) as error:
        PodSpec.model_validate({"containers": [container(), container()]})

    assert "unique" in str(error.value)


def test_duplicate_volume_names_are_rejected() -> None:
    """Two volumes may not share a name."""
    with pytest.raises(ValidationError):
        PodSpec.model_validate(
            {
                "containers": [container()],
                "volumes": [
                    {"name": "v", "emptyDir": {}},
                    {"name": "v", "emptyDir": {}},
                ],
            }
        )


def test_restart_policy_other_than_always_is_rejected() -> None:
    """A static Pod always restarts, so any other policy would be a lie."""
    with pytest.raises(ValidationError):
        PodSpec.model_validate({"containers": [container()], "restartPolicy": "Never"})


def test_host_ip_uses_the_kubernetes_field_name() -> None:
    """A port's host address serializes as ``hostIP``, not ``hostIp``."""
    spec = PodSpec.model_validate(
        {"containers": [container(ports=[{"containerPort": 80, "hostIP": "127.0.0.1"}])]}
    )

    dumped = spec.containers[0].ports
    assert dumped is not None
    assert dumped[0].model_dump(by_alias=True, exclude_none=True) == {
        "containerPort": 80,
        "hostIP": "127.0.0.1",
        "protocol": "TCP",
    }
