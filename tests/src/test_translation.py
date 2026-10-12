"""Compiling a template into a self-contained static Pod."""

from __future__ import annotations

import json
from typing import Any

import pytest

from static_sets.errors import (
    SecretDisclosureError,
    TranslationError,
    UnsupportedFieldError,
)
from static_sets.translation import (
    ResolvedConfig,
    compile_workload,
    config_references,
    render_manifest,
)

CONFIGMAP = {"web-conf": {"app.conf": b"greeting=hello\n", "flags.conf": b"flag=1\n"}}
ENV_CONFIGMAP = {"env-conf": {"GREETING": b"hello", "FLAG": b"1"}}
SECRET = {"web-secret": {"token": b"s3cr3t-value"}}


def compiled(make_static_set: Any, resolved: ResolvedConfig | None = None, **overrides: Any) -> Any:
    """Compile the base StaticSet, with overrides, against the given configuration.

    Args:
        make_static_set: The fixture factory for validated StaticSets.
        resolved: The configuration set to compile against.
        **overrides: Fields to merge into the resource body.

    Returns:
        The compiled workload.
    """
    resource = make_static_set(**overrides)
    return compile_workload(resource.spec, resolved or ResolvedConfig(config_maps=CONFIGMAP))


def test_configmap_env_reference_is_resolved(make_static_set: Any, make_resource: Any) -> None:
    """A configMapKeyRef becomes a literal value, which is what offline needs."""
    body = make_resource()
    body["spec"]["template"]["spec"]["containers"][0]["env"] = [
        {
            "name": "GREETING",
            "valueFrom": {"configMapKeyRef": {"name": "web-conf", "key": "app.conf"}},
        }
    ]
    resource = make_static_set(**body)

    workload = compile_workload(resource.spec, ResolvedConfig(config_maps=CONFIGMAP))
    environment = workload.pod["spec"]["containers"][0]["env"]

    assert environment == [{"name": "GREETING", "value": "greeting=hello\n"}]


def test_env_from_expands_keys_with_prefix_and_precedence(
    make_static_set: Any, make_resource: Any
) -> None:
    """envFrom expands every key, applies the prefix, and loses to an explicit env."""
    body = make_resource()
    container = body["spec"]["template"]["spec"]["containers"][0]
    container["envFrom"] = [
        {"configMapRef": {"name": "env-conf"}},
        {"prefix": "P_", "configMapRef": {"name": "env-conf"}},
    ]
    container["env"] = [{"name": "GREETING", "value": "explicit-wins"}]
    resource = make_static_set(**body)

    workload = compile_workload(
        resource.spec, ResolvedConfig(config_maps={**CONFIGMAP, **ENV_CONFIGMAP})
    )
    environment = {
        item["name"]: item["value"] for item in workload.pod["spec"]["containers"][0]["env"]
    }

    assert environment["FLAG"] == "1"
    assert environment["P_GREETING"] == "hello"
    assert environment["GREETING"] == "explicit-wins"


def test_env_from_drops_unusable_keys(make_static_set: Any, make_resource: Any) -> None:
    """Keys that are not valid variable names are dropped, as Kubernetes does."""
    body = make_resource()
    body["spec"]["template"]["spec"]["containers"][0]["envFrom"] = [
        {"configMapRef": {"name": "web-conf"}}
    ]
    resource = make_static_set(**body)

    workload = compile_workload(
        resource.spec, ResolvedConfig(config_maps={"web-conf": {"not-a-name": b"v", "OK": b"v"}})
    )

    assert workload.pod["spec"]["containers"][0]["env"] == [{"name": "OK", "value": "v"}]


def test_optional_configmap_reference_is_omitted(make_static_set: Any, make_resource: Any) -> None:
    """An optional reference that resolved to nothing simply produces no variable."""
    body = make_resource()
    body["spec"]["template"]["spec"]["containers"][0]["env"] = [
        {
            "name": "MAYBE",
            "valueFrom": {
                "configMapKeyRef": {"name": "absent", "key": "k", "optional": True},
            },
        }
    ]
    resource = make_static_set(**body)

    workload = compile_workload(resource.spec, ResolvedConfig(config_maps=CONFIGMAP))

    assert "env" not in workload.pod["spec"]["containers"][0]


def test_missing_mandatory_configmap_is_an_error(make_static_set: Any, make_resource: Any) -> None:
    """A mandatory reference that does not exist is refused, not silently dropped."""
    body = make_resource()
    body["spec"]["template"]["spec"]["containers"][0]["env"] = [
        {"name": "MISSING", "valueFrom": {"configMapKeyRef": {"name": "absent", "key": "k"}}}
    ]
    resource = make_static_set(**body)

    with pytest.raises(TranslationError) as error:
        compile_workload(resource.spec, ResolvedConfig(config_maps=CONFIGMAP))

    assert "absent" in str(error.value)


def test_missing_configmap_volume_is_an_error(make_static_set: Any, make_resource: Any) -> None:
    """A volume that names an absent ConfigMap is refused unless it is optional."""
    resource = make_static_set()
    with pytest.raises(TranslationError):
        compile_workload(resource.spec, ResolvedConfig())

    optional = make_resource()
    optional["spec"]["template"]["spec"]["volumes"][0]["configMap"]["optional"] = True
    workload = compile_workload(make_static_set(**optional).spec, ResolvedConfig())

    assert workload.files == {}


def test_secret_environment_reference_is_refused_by_default(
    make_static_set: Any, make_resource: Any
) -> None:
    """A Secret must not reach the environment without an explicit opt-in."""
    body = make_resource()
    body["spec"]["template"]["spec"]["containers"][0]["env"] = [
        {"name": "TOKEN", "valueFrom": {"secretKeyRef": {"name": "web-secret", "key": "token"}}}
    ]
    resource = make_static_set(**body)

    with pytest.raises(SecretDisclosureError) as error:
        compile_workload(resource.spec, ResolvedConfig(config_maps=CONFIGMAP, secrets=SECRET))

    assert "allowInlineSecretEnv" in str(error.value)


def test_secret_env_from_is_refused_by_default(make_static_set: Any, make_resource: Any) -> None:
    """A Secret envFrom import is refused as well."""
    body = make_resource()
    body["spec"]["template"]["spec"]["containers"][0]["envFrom"] = [
        {"secretRef": {"name": "web-secret"}}
    ]
    resource = make_static_set(**body)

    with pytest.raises(SecretDisclosureError):
        compile_workload(resource.spec, ResolvedConfig(secrets=SECRET))


def test_opted_in_secret_env_discloses_and_warns(make_static_set: Any, make_resource: Any) -> None:
    """With the opt-in the value is inlined, and the disclosure is reported."""
    body = make_resource()
    body["spec"]["policy"] = {"allowInlineSecretEnv": True}
    body["spec"]["template"]["spec"]["containers"][0]["env"] = [
        {"name": "TOKEN", "valueFrom": {"secretKeyRef": {"name": "web-secret", "key": "token"}}}
    ]
    resource = make_static_set(**body)

    workload = compile_workload(
        resource.spec, ResolvedConfig(config_maps=CONFIGMAP, secrets=SECRET)
    )

    assert workload.pod["spec"]["containers"][0]["env"] == [
        {"name": "TOKEN", "value": "s3cr3t-value"}
    ]
    assert len(workload.warnings) == 1
    assert "readable through the mirror Pod" in workload.warnings[0]


def test_secret_volume_is_materialized_with_restrictive_mode(
    make_static_set: Any, make_resource: Any
) -> None:
    """A Secret volume becomes a read-only hostPath over a 0400 file."""
    body = make_resource()
    body["spec"]["template"]["spec"]["volumes"] = [
        {"name": "conf", "configMap": {"name": "web-conf"}},
        {"name": "creds", "secret": {"secretName": "web-secret", "defaultMode": 256}},
    ]
    body["spec"]["template"]["spec"]["containers"][0]["volumeMounts"] = [
        {"name": "creds", "mountPath": "/etc/creds"},
        {"name": "conf", "mountPath": "/etc/web"},
    ]
    resource = make_static_set(**body)

    workload = compile_workload(
        resource.spec, ResolvedConfig(config_maps=CONFIGMAP, secrets=SECRET)
    )

    assert workload.files["secrets/creds/token"] == b"s3cr3t-value"
    assert workload.modes["secrets/creds/token"] == 0o400
    volumes = {volume["name"]: volume for volume in workload.pod["spec"]["volumes"]}
    assert volumes["creds"]["hostPath"]["path"] == "secrets/creds"
    assert volumes["creds"]["hostPath"]["type"] == "Directory"
    mounts = {
        mount["name"]: mount for mount in workload.pod["spec"]["containers"][0]["volumeMounts"]
    }
    assert mounts["creds"]["readOnly"] is True


def test_secret_volume_without_mode_needs_a_provably_root_container(
    make_static_set: Any, make_resource: Any
) -> None:
    """Without a declared mode, a container that is not provably root is refused."""
    body = make_resource()
    body["spec"]["template"]["spec"]["volumes"] = [
        {"name": "conf", "configMap": {"name": "web-conf"}},
        {"name": "creds", "secret": {"secretName": "web-secret"}},
    ]
    body["spec"]["template"]["spec"]["containers"][0]["volumeMounts"] = [
        {"name": "creds", "mountPath": "/etc/creds"},
        {"name": "conf", "mountPath": "/etc/web"},
    ]
    resource = make_static_set(**body)

    with pytest.raises(UnsupportedFieldError) as error:
        compile_workload(resource.spec, ResolvedConfig(config_maps=CONFIGMAP, secrets=SECRET))

    assert "defaultMode" in str(error.value)


def test_secret_volume_without_mode_is_accepted_for_root(
    make_static_set: Any, make_resource: Any
) -> None:
    """A container declared as uid 0 may rely on the 0400 default."""
    body = make_resource()
    body["spec"]["template"]["spec"]["volumes"] = [
        {"name": "conf", "configMap": {"name": "web-conf"}},
        {"name": "creds", "secret": {"secretName": "web-secret"}},
    ]
    container = body["spec"]["template"]["spec"]["containers"][0]
    container["securityContext"] = {"runAsUser": 0}
    container["volumeMounts"] = [
        {"name": "creds", "mountPath": "/etc/creds"},
        {"name": "conf", "mountPath": "/etc/web"},
    ]
    resource = make_static_set(**body)

    workload = compile_workload(
        resource.spec, ResolvedConfig(config_maps=CONFIGMAP, secrets=SECRET)
    )

    assert workload.modes["secrets/creds/token"] == 0o400


def test_secret_material_never_reaches_the_manifest(
    make_static_set: Any, make_resource: Any
) -> None:
    """The compiled Pod never contains a Secret value, only a mount path."""
    body = make_resource()
    body["spec"]["template"]["spec"]["volumes"] = [
        {"name": "conf", "configMap": {"name": "web-conf"}},
        {"name": "creds", "secret": {"secretName": "web-secret", "defaultMode": 256}},
    ]
    body["spec"]["template"]["spec"]["containers"][0]["volumeMounts"] = [
        {"name": "creds", "mountPath": "/etc/creds"},
        {"name": "conf", "mountPath": "/etc/web"},
    ]
    resource = make_static_set(**body)

    workload = compile_workload(
        resource.spec, ResolvedConfig(config_maps=CONFIGMAP, secrets=SECRET)
    )
    manifest = render_manifest(
        workload,
        pod_name="web-abc",
        namespace="edge",
        revision_dir="/var/lib/static-sets/placements/abc/revisions/" + workload.revision,
        labels={},
        annotations={},
    )

    assert "s3cr3t-value" not in json.dumps(manifest)


def test_configmap_volume_items_and_modes(make_static_set: Any, make_resource: Any) -> None:
    """Items select keys, rename them, and set per-file modes."""
    body = make_resource()
    body["spec"]["template"]["spec"]["volumes"] = [
        {
            "name": "conf",
            "configMap": {
                "name": "web-conf",
                "defaultMode": 420,
                "items": [{"key": "app.conf", "path": "nested/app.conf", "mode": 384}],
            },
        }
    ]
    resource = make_static_set(**body)

    workload = compile_workload(resource.spec, ResolvedConfig(config_maps=CONFIGMAP))

    assert workload.files == {"config/conf/nested/app.conf": b"greeting=hello\n"}
    assert workload.modes["config/conf/nested/app.conf"] == 0o600


def test_missing_item_key_is_an_error(make_static_set: Any, make_resource: Any) -> None:
    """An item naming a key the object lacks is refused."""
    body = make_resource()
    body["spec"]["template"]["spec"]["volumes"] = [
        {"name": "conf", "configMap": {"name": "web-conf", "items": [{"key": "nope", "path": "x"}]}}
    ]
    resource = make_static_set(**body)

    with pytest.raises(TranslationError) as error:
        compile_workload(resource.spec, ResolvedConfig(config_maps=CONFIGMAP))

    assert "nope" in str(error.value)


def test_item_path_traversal_is_refused(make_static_set: Any, make_resource: Any) -> None:
    """An item may not map a key outside its own volume directory."""
    body = make_resource()
    body["spec"]["template"]["spec"]["volumes"] = [
        {
            "name": "conf",
            "configMap": {
                "name": "web-conf",
                "items": [{"key": "app.conf", "path": "../../escape"}],
            },
        }
    ]
    resource = make_static_set(**body)

    with pytest.raises(UnsupportedFieldError):
        compile_workload(resource.spec, ResolvedConfig(config_maps=CONFIGMAP))


def test_undefined_volume_mount_is_an_error(make_static_set: Any, make_resource: Any) -> None:
    """A container may not mount a volume the Pod does not define."""
    body = make_resource()
    body["spec"]["template"]["spec"]["containers"][0]["volumeMounts"] = [
        {"name": "ghost", "mountPath": "/etc/ghost"}
    ]
    resource = make_static_set(**body)

    with pytest.raises(TranslationError) as error:
        compile_workload(resource.spec, ResolvedConfig(config_maps=CONFIGMAP))

    assert "ghost" in str(error.value)


def test_empty_dir_volume_is_kept_as_is(make_static_set: Any, make_resource: Any) -> None:
    """An emptyDir needs nothing materialized and stays an emptyDir."""
    body = make_resource()
    body["spec"]["template"]["spec"]["volumes"] = [
        {"name": "conf", "configMap": {"name": "web-conf"}},
        {"name": "tmp", "emptyDir": {"medium": "Memory", "sizeLimit": "16Mi"}},
    ]
    body["spec"]["template"]["spec"]["containers"][0]["volumeMounts"] = [
        {"name": "tmp", "mountPath": "/tmp"},
        {"name": "conf", "mountPath": "/etc/web"},
    ]
    resource = make_static_set(**body)

    workload = compile_workload(resource.spec, ResolvedConfig(config_maps=CONFIGMAP))

    volumes = {volume["name"]: volume for volume in workload.pod["spec"]["volumes"]}
    assert volumes["tmp"]["emptyDir"] == {"medium": "Memory", "sizeLimit": "16Mi"}
    assert workload.files == {
        "config/conf/app.conf": b"greeting=hello\n",
        "config/conf/flags.conf": b"flag=1\n",
    }


def test_privileged_container_needs_an_explicit_policy(
    make_static_set: Any, make_resource: Any
) -> None:
    """A privileged workload is refused until the policy accepts the risk."""
    body = make_resource()
    body["spec"]["template"]["spec"]["containers"][0]["securityContext"] = {"privileged": True}
    resource = make_static_set(**body)

    with pytest.raises(UnsupportedFieldError) as error:
        compile_workload(resource.spec, ResolvedConfig(config_maps=CONFIGMAP))

    assert "allowPrivileged" in str(error.value)

    allowed = make_resource()
    allowed["spec"]["policy"] = {"allowPrivileged": True}
    allowed["spec"]["template"]["spec"]["containers"][0]["securityContext"] = {"privileged": True}
    workload = compile_workload(
        make_static_set(**allowed).spec, ResolvedConfig(config_maps=CONFIGMAP)
    )

    assert workload.pod["spec"]["containers"][0]["securityContext"]["privileged"] is True


def test_always_pull_is_refused_when_disconnected_start_is_disallowed(
    make_static_set: Any, make_resource: Any
) -> None:
    """``Always`` cannot be honoured offline, so the policy can forbid it."""
    body = make_resource()
    body["spec"]["offline"] = {"allowDisconnectedStart": False}
    body["spec"]["template"]["spec"]["containers"][0]["imagePullPolicy"] = "Always"
    resource = make_static_set(**body)

    with pytest.raises(UnsupportedFieldError) as error:
        compile_workload(resource.spec, ResolvedConfig(config_maps=CONFIGMAP))

    assert "Always" in str(error.value)


def test_verified_image_provisioning_forbids_always_pull(
    make_static_set: Any, make_resource: Any
) -> None:
    """Verified image preparation and ``Always`` are contradictory."""
    body = make_resource()
    body["spec"]["offline"] = {"imageProvisioning": "VerifiedBeforeInstall"}
    body["spec"]["template"]["spec"]["containers"][0]["imagePullPolicy"] = "Always"
    resource = make_static_set(**body)

    with pytest.raises(UnsupportedFieldError):
        compile_workload(resource.spec, ResolvedConfig(config_maps=CONFIGMAP))


def test_probe_without_a_handler_is_refused(make_static_set: Any, make_resource: Any) -> None:
    """A probe that declares no action would be rejected by kubelet anyway."""
    body = make_resource()
    body["spec"]["template"]["spec"]["containers"][0]["livenessProbe"] = {"periodSeconds": 5}
    resource = make_static_set(**body)

    with pytest.raises(UnsupportedFieldError) as error:
        compile_workload(resource.spec, ResolvedConfig(config_maps=CONFIGMAP))

    assert "livenessProbe" in str(error.value)


def test_supported_probe_and_resources_are_preserved(
    make_static_set: Any, make_resource: Any
) -> None:
    """Probes, resources and security contexts survive compilation unchanged."""
    body = make_resource()
    container = body["spec"]["template"]["spec"]["containers"][0]
    container["livenessProbe"] = {"httpGet": {"path": "/healthz", "port": 8080}}
    container["readinessProbe"] = {"exec": {"command": ["true"]}}
    container["resources"] = {"requests": {"cpu": "100m"}, "limits": {"memory": "128Mi"}}
    container["securityContext"] = {"readOnlyRootFilesystem": True, "runAsUser": 0}
    resource = make_static_set(**body)

    workload = compile_workload(resource.spec, ResolvedConfig(config_maps=CONFIGMAP))
    emitted = workload.pod["spec"]["containers"][0]

    assert emitted["livenessProbe"] == {
        "httpGet": {"path": "/healthz", "port": 8080, "scheme": "HTTP"}
    }
    assert emitted["readinessProbe"] == {"exec": {"command": ["true"]}}
    assert emitted["resources"] == {"requests": {"cpu": "100m"}, "limits": {"memory": "128Mi"}}
    assert emitted["securityContext"]["readOnlyRootFilesystem"] is True


def test_emitted_pod_carries_no_placement_or_identity_fields(
    make_static_set: Any, make_resource: Any
) -> None:
    """The static workload has no scheduler fields and no service account."""
    body = make_resource()
    body["spec"]["placement"] = {"nodeSelector": {"role": "edge"}}
    resource = make_static_set(**body)

    workload = compile_workload(resource.spec, ResolvedConfig(config_maps=CONFIGMAP))

    assert "nodeSelector" not in workload.pod["spec"]
    assert "serviceAccountName" not in workload.pod["spec"]
    assert "nodeName" not in workload.pod["spec"]
    assert workload.pod["spec"]["restartPolicy"] == "Always"


def test_revision_is_deterministic_and_config_sensitive(make_static_set: Any) -> None:
    """The same inputs give the same revision; changed configuration does not."""
    first = compiled(make_static_set)
    second = compiled(make_static_set)
    changed = compiled(
        make_static_set, ResolvedConfig(config_maps={"web-conf": {"app.conf": b"changed\n"}})
    )

    assert first.revision == second.revision
    assert changed.revision != first.revision


def test_revision_survives_a_bundle_prefix(make_static_set: Any) -> None:
    """The revision does not depend on where the bundle happens to be mounted."""
    workload = compiled(make_static_set)
    rendered = render_manifest(
        workload,
        pod_name="web-abc",
        namespace="edge",
        revision_dir="/var/lib/static-sets/placements/abc/revisions/" + workload.revision,
        labels={},
        annotations={},
    )

    assert rendered["metadata"]["name"] == "web-abc"
    volumes = {volume["name"]: volume for volume in rendered["spec"]["volumes"]}
    assert volumes["conf"]["hostPath"]["path"].startswith("/var/lib/static-sets/placements/abc")


def test_render_manifest_merges_template_and_provenance_metadata(
    make_static_set: Any, make_resource: Any
) -> None:
    """Template labels survive alongside the provenance the installer adds."""
    body = make_resource()
    body["spec"]["template"]["metadata"] = {
        "labels": {"app": "web"},
        "annotations": {"owner": "team"},
    }
    resource = make_static_set(**body)
    workload = compile_workload(resource.spec, ResolvedConfig(config_maps=CONFIGMAP))

    rendered = render_manifest(
        workload,
        pod_name="web-abc",
        namespace="edge",
        revision_dir="/b/" + workload.revision,
        labels={"staticsets.cloudlane.dev/set": "web"},
        annotations={"staticsets.cloudlane.dev/revision": workload.revision},
    )

    assert rendered["metadata"]["labels"] == {
        "app": "web",
        "staticsets.cloudlane.dev/set": "web",
    }
    assert rendered["metadata"]["annotations"] == {
        "owner": "team",
        "staticsets.cloudlane.dev/revision": workload.revision,
    }
    assert rendered["metadata"]["namespace"] == "edge"


def test_config_references_names_every_referenced_object(
    make_static_set: Any, make_resource: Any
) -> None:
    """The watch set is every ConfigMap and Secret the template names."""
    body = make_resource()
    container = body["spec"]["template"]["spec"]["containers"][0]
    container["env"] = [
        {"name": "A", "valueFrom": {"configMapKeyRef": {"name": "cf-a", "key": "k"}}},
        {"name": "B", "valueFrom": {"secretKeyRef": {"name": "sec-b", "key": "k"}}},
    ]
    container["envFrom"] = [{"configMapRef": {"name": "cf-c"}}]
    body["spec"]["template"]["spec"]["volumes"] = [
        {"name": "conf", "configMap": {"name": "web-conf"}},
        {"name": "creds", "secret": {"secretName": "web-secret"}},
    ]
    resource = make_static_set(**body)

    references = config_references(resource.spec)

    assert [(item.kind, item.name) for item in references] == [
        ("ConfigMap", "cf-a"),
        ("ConfigMap", "cf-c"),
        ("ConfigMap", "web-conf"),
        ("Secret", "sec-b"),
        ("Secret", "web-secret"),
    ]
