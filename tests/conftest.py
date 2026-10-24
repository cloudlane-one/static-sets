"""Shared fixtures for the StaticSet test suite.

The builders here return plain dictionaries rather than model instances, so that
each test starts from the same wire-shaped input the API server would see. Tests
differ from the base only in the one field they are about, which keeps a failure
attributable to that field.
"""

from __future__ import annotations

import copy
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from static_sets.models import StaticSet
from static_sets.placements import StaticPodPlacement

BUNDLE = "/var/lib/static-sets"
MANIFESTS = "/etc/kubernetes/manifests"

BASE_RESOURCE: dict[str, Any] = {
    "apiVersion": "staticsets.cloudlane.dev/v1",
    "kind": "StaticSet",
    "metadata": {
        "name": "web",
        "namespace": "edge",
        "uid": "0f9a4b6c-1111-2222-3333-444455556666",
        "generation": 3,
    },
    "spec": {
        "mode": "Deployment",
        "replicas": 2,
        "template": {
            "metadata": {"labels": {"app": "web"}},
            "spec": {
                "containers": [
                    {
                        "name": "app",
                        "image": "registry.example.com/web:1.27",
                        "imagePullPolicy": "IfNotPresent",
                        "ports": [{"containerPort": 8080, "name": "http"}],
                        "volumeMounts": [{"name": "conf", "mountPath": "/etc/web"}],
                    }
                ],
                "volumes": [{"name": "conf", "configMap": {"name": "web-conf"}}],
            },
        },
    },
}


def deep_merge(base: dict[str, Any], overrides: dict[str, Any]) -> dict[str, Any]:
    """Merge overrides into a copy of a nested mapping.

    Args:
        base: The base mapping.
        overrides: The values to merge in, recursively for nested mappings.

    Returns:
        A new mapping.
    """
    result = copy.deepcopy(base)
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def resource_dict(**overrides: Any) -> dict[str, Any]:
    """Return a StaticSet body with the given overrides applied.

    Args:
        **overrides: Fields to merge into the base body.

    Returns:
        The wire-shaped body.
    """
    return deep_merge(BASE_RESOURCE, overrides)


def build_static_set(**overrides: Any) -> StaticSet:
    """Return a validated StaticSet with the given overrides applied.

    Args:
        **overrides: Fields to merge into the base body.

    Returns:
        The validated resource.
    """
    return StaticSet.model_validate(resource_dict(**overrides))


PLACEMENT_BASE: dict[str, Any] = {
    "apiVersion": "staticsets.cloudlane.dev/v1",
    "kind": "StaticPodPlacement",
    "metadata": {"name": "web-e6383089b2", "namespace": "edge", "generation": 1},
    "spec": {
        "staticSet": "web",
        "staticSetUid": "0f9a4b6c-1111-2222-3333-444455556666",
        "node": "worker-1",
        "generation": 3,
    },
}


def build_placement(**overrides: Any) -> StaticPodPlacement:
    """Return a validated ``StaticPodPlacement`` with the given overrides applied.

    Args:
        **overrides: Fields to merge into the base body.

    Returns:
        The validated resource.
    """
    return StaticPodPlacement.model_validate(deep_merge(PLACEMENT_BASE, overrides))


@pytest.fixture()
def make_placement() -> Callable[..., StaticPodPlacement]:
    """Return a factory for validated placement records.

    Returns:
        The factory.
    """
    return build_placement


@pytest.fixture()
def make_resource() -> Callable[..., dict[str, Any]]:
    """Return a factory for wire-shaped StaticSet bodies.

    Returns:
        The factory.
    """
    return resource_dict


@pytest.fixture()
def make_static_set() -> Callable[..., StaticSet]:
    """Return a factory for validated StaticSets.

    Returns:
        The factory.
    """
    return build_static_set


@pytest.fixture()
def temp_roots(tmp_path: Path) -> tuple[Path, Path]:
    """Return a disposable (static Pod path, bundle directory) pair.

    Args:
        tmp_path: Pytest's per-test temporary directory.

    Returns:
        The two host paths an installer would be given.
    """
    manifests = tmp_path / "manifests"
    bundle = tmp_path / "bundle"
    manifests.mkdir()
    bundle.mkdir()
    return manifests, bundle
