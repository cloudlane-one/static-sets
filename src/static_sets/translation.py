"""Compile a supported ``PodTemplateSpec`` into a self-contained static Pod.

The compiler never guesses. Every ConfigMap and Secret the template references is
resolved *now*, its data is materialized into revision-specific files, and the
emitted Pod mounts those files read-only. What cannot be resolved without the API
server is rejected with a message that names the field and the supported
alternative, because a static Pod that silently lost a field would start, look
healthy, and behave differently from the template it was compiled from.

What the emitted Pod does **not** carry is as important as what it does:

* no scheduler placement (``nodeSelector``, ``affinity``, ``tolerations``,
  ``topologySpreadConstraints``) — kubelet never schedules a static Pod, so those
  fields would be inert; they belong to ``spec.placement``;
* no ``serviceAccountName``, ``imagePullSecrets`` or projected service-account
  token — all of them need the API server, which is precisely what a reboot must
  not need;
* no ``hostPath`` volume taken from the template — the only ``hostPath`` mounts in
  the manifest are the revision directories this compiler materialized, which keeps
  the workload inside the bundle the installer owns.

Secret material is handled deliberately. A ConfigMap value is not confidential, so
it is inlined into the environment where the template asked for it. A Secret value
is written to a mode-restricted file inside the revision directory, and a Secret
asked for as a *literal environment value* is refused unless the StaticSet opts in
(``spec.policy.allowInlineSecretEnv``), because the only offline ways to satisfy
that request both disclose the value: either it is written into the manifest — and
therefore into the mirror Pod and into every copy of the manifest — or the variable
is simply gone after the reboot.
"""

from __future__ import annotations

import copy
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

from static_sets.errors import (
    SecretDisclosureError,
    TranslationError,
    UnsupportedFieldError,
)
from static_sets.models import ImageProvisioning, StaticSetSpec
from static_sets.paths import validate_relative
from static_sets.revision import compute_revision
from static_sets.workload import (
    Container,
    EnvFromSource,
    EnvVarSource,
    PodSpec,
    PullPolicy,
    Volume,
)

__all__ = [
    "CompiledWorkload",
    "ConfigReference",
    "ResolvedConfig",
    "check_policies",
    "compile_workload",
    "config_references",
    "referenced_config",
    "render_manifest",
]

_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

_COMPILED_SEPARATELY = ("env", "envFrom", "volumeMounts")
"""Container fields the compiler replaces rather than copies."""

_POD_SPEC_SCALARS = (
    ("termination_grace_period_seconds", "terminationGracePeriodSeconds"),
    ("dns_policy", "dnsPolicy"),
    ("host_network", "hostNetwork"),
    ("host_pid", "hostPID"),
    ("host_ipc", "hostIPC"),
    ("hostname", "hostname"),
    ("share_process_namespace", "shareProcessNamespace"),
)

_POD_SPEC_MODELS = (
    ("dns_config", "dnsConfig"),
    ("host_aliases", "hostAliases"),
    ("security_context", "securityContext"),
)


@dataclass(frozen=True)
class ResolvedConfig:
    """The configuration a template referenced, already read from the cluster.

    Attributes:
        config_maps: ConfigMap data, keyed by ConfigMap name then key. Binary
            entries are decoded from their base64 representation.
        secrets: Secret data, keyed by Secret name then key.
    """

    config_maps: Mapping[str, Mapping[str, bytes]] = field(default_factory=dict)
    secrets: Mapping[str, Mapping[str, bytes]] = field(default_factory=dict)


@dataclass(frozen=True, order=True)
class ConfigReference:
    """One ConfigMap or Secret a template depends on.

    The operator watches exactly these objects, so the list is also the answer to
    "which changes produce a new revision".
    """

    kind: Literal["ConfigMap", "Secret"]
    name: str


@dataclass(frozen=True)
class CompiledWorkload:
    """A compiled, node-independent static Pod.

    Attributes:
        pod: The Pod manifest with *relative* configuration mount paths and without
            a name, a namespace or a revision annotation.
        files: The configuration files to materialize, by bundle-relative path.
        modes: The file mode of every path in :attr:`files`.
        references: The ConfigMaps and Secrets the template depends on.
        revision: The deterministic revision of this content.
        warnings: Non-fatal disclosures the caller should surface in status.
    """

    pod: dict[str, Any]
    files: dict[str, bytes]
    modes: dict[str, int]
    references: tuple[ConfigReference, ...]
    revision: str
    warnings: tuple[str, ...] = ()


class _OptionalMissingError(Exception):
    """Raised internally when an optional reference resolved to nothing."""


def referenced_config(resolved: ResolvedConfig) -> tuple[ConfigReference, ...]:
    """Return the references implied by a resolved configuration set.

    Args:
        resolved: The configuration read from the cluster.

    Returns:
        The ConfigMap and Secret references, sorted and deduplicated.
    """
    references = {ConfigReference("ConfigMap", name) for name in resolved.config_maps}
    references |= {ConfigReference("Secret", name) for name in resolved.secrets}
    return tuple(sorted(references))


def config_references(spec: StaticSetSpec) -> tuple[ConfigReference, ...]:
    """Return every ConfigMap and Secret a template names, before resolving them.

    The installer needs the names *before* it can read the objects, and the
    controller needs them to decide which changes require a new revision.

    Args:
        spec: The validated specification.

    Returns:
        The references, sorted and deduplicated.
    """
    pod_spec = spec.template.spec
    references: set[ConfigReference] = set()
    for volume in pod_spec.volumes or []:
        if volume.config_map is not None:
            references.add(ConfigReference("ConfigMap", volume.config_map.name))
        if volume.secret is not None:
            references.add(ConfigReference("Secret", volume.secret.secret_name))
    for container in [*pod_spec.containers, *(pod_spec.init_containers or [])]:
        for env_var in container.env or []:
            source = env_var.value_from
            if source is None:
                continue
            if source.config_map_key_ref is not None:
                references.add(ConfigReference("ConfigMap", source.config_map_key_ref.name))
            if source.secret_key_ref is not None:
                references.add(ConfigReference("Secret", source.secret_key_ref.name))
        for env_from in container.env_from or []:
            if env_from.config_map_ref is not None:
                references.add(ConfigReference("ConfigMap", env_from.config_map_ref.name))
            if env_from.secret_ref is not None:
                references.add(ConfigReference("Secret", env_from.secret_ref.name))
    return tuple(sorted(references))


def compile_workload(spec: StaticSetSpec, resolved: ResolvedConfig) -> CompiledWorkload:
    """Compile a StaticSet specification into a self-contained static Pod.

    Args:
        spec: The validated specification.
        resolved: The configuration the template's references were resolved to.

    Returns:
        The compiled workload, including its deterministic revision.

    Raises:
        UnsupportedFieldError: If the template uses a field or a policy
            combination this operator does not support.
        SecretDisclosureError: If Secret material would have to be disclosed.
        TranslationError: If a referenced ConfigMap or Secret is missing and not
            optional.
    """
    pod_spec = spec.template.spec
    check_policies(spec)

    materializer = _Materializer(resolved)
    volumes = _build_volumes(pod_spec, materializer)

    containers = [
        _compile_container(item, pod_spec, spec, materializer) for item in pod_spec.containers
    ]
    init_containers = [
        _compile_container(item, pod_spec, spec, materializer)
        for item in pod_spec.init_containers or []
    ]

    pod = _assemble_pod(spec, containers, init_containers, volumes)
    revision = compute_revision(pod, materializer.files, materializer.modes)
    return CompiledWorkload(
        pod=pod,
        files=materializer.files,
        modes=materializer.modes,
        references=tuple(sorted(materializer.references)),
        revision=revision,
        warnings=tuple(materializer.warnings),
    )


def render_manifest(
    compiled: CompiledWorkload,
    *,
    pod_name: str,
    namespace: str,
    revision_dir: str,
    labels: Mapping[str, str],
    annotations: Mapping[str, str],
) -> dict[str, Any]:
    """Render the final static Pod manifest for one placement.

    The only per-placement inputs are the Pod's name, its namespace, the directory
    the materialized files live in and the provenance labels and annotations.
    Nothing here can change the revision, so the same compiled content renders the
    same revision on every node.

    Args:
        compiled: The compiled workload.
        pod_name: The static Pod's name, which kubelet also needs as file name.
        namespace: The namespace the mirror Pod should appear in.
        revision_dir: The absolute directory holding this revision's files.
        labels: Provenance labels to merge onto the static Pod.
        annotations: Provenance annotations to merge onto the static Pod.

    Returns:
        The complete static Pod manifest.
    """
    pod = copy.deepcopy(compiled.pod)
    metadata = pod.setdefault("metadata", {})
    metadata["name"] = pod_name
    metadata["namespace"] = namespace
    metadata["labels"] = {**metadata.get("labels", {}), **labels}
    metadata["annotations"] = {**metadata.get("annotations", {}), **annotations}
    for volume in pod.get("spec", {}).get("volumes", []):
        host_path = volume.get("hostPath")
        if host_path is not None:
            host_path["path"] = f"{revision_dir.rstrip('/')}/{host_path['path']}"
    return pod


class _Materializer:
    """Collects the files and references a compilation produces.

    Attributes:
        files: Bundle-relative path to content.
        modes: Bundle-relative path to file mode.
        references: The configuration objects the template depended on.
        warnings: Disclosures the caller should report.
    """

    def __init__(self, resolved: ResolvedConfig) -> None:
        """Bind the materializer to a resolved configuration.

        Args:
            resolved: The configuration read from the cluster.
        """
        self.resolved = resolved
        self.files: dict[str, bytes] = {}
        self.modes: dict[str, int] = {}
        self.references: set[ConfigReference] = set()
        self.warnings: list[str] = []

    def add(self, path: str, content: bytes, mode: int) -> None:
        """Register a file to write.

        Args:
            path: The bundle-relative path.
            content: The file content.
            mode: The file mode.

        Raises:
            TranslationError: If two sources disagree about the same path.
        """
        if path in self.files and self.files[path] != content:
            raise TranslationError(f"two volume sources disagree about {path!r}")
        self.files[path] = content
        self.modes[path] = mode

    def read(self, kind: Literal["ConfigMap", "Secret"], name: str) -> Mapping[str, bytes] | None:
        """Return the data of a referenced object.

        Args:
            kind: ``ConfigMap`` or ``Secret``.
            name: The object's name.

        Returns:
            The object's data, or ``None`` when it was not read.
        """
        self.references.add(ConfigReference(kind, name))
        source = self.resolved.config_maps if kind == "ConfigMap" else self.resolved.secrets
        return source.get(name)


def check_policies(spec: StaticSetSpec) -> None:
    """Reject a specification the workload policy does not permit.

    This is the same check the compiler runs, exposed so that admission can refuse
    such an object instead of accepting it and failing at install time.

    Args:
        spec: The specification to check.

    Raises:
        UnsupportedFieldError: If a privileged container or an always-pull image
            conflicts with the declared policy.
        SecretDisclosureError: If a Secret would be exposed without opting in.
    """
    _check_workload_policy(spec)
    _check_secret_env_policy(spec)


def _check_workload_policy(spec: StaticSetSpec) -> None:
    """Reject a workload the StaticSet's policy does not permit.

    Args:
        spec: The specification to check.

    Raises:
        UnsupportedFieldError: If a privileged container or an always-pull image
            conflicts with the declared policy.
    """
    containers = [*spec.template.spec.containers, *(spec.template.spec.init_containers or [])]
    if not spec.policy.allow_privileged:
        for container in containers:
            context = container.security_context
            if context is not None and context.privileged:
                raise UnsupportedFieldError(
                    f"container {container.name!r} requests securityContext.privileged; set "
                    "spec.policy.allowPrivileged to true to accept the risk of a static Pod "
                    "that no admission controller reviews"
                )
    always = sorted(
        container.name
        for container in containers
        if container.image_pull_policy is PullPolicy.ALWAYS
    )
    if always and (
        not spec.offline.allow_disconnected_start
        or spec.offline.image_provisioning is ImageProvisioning.VERIFIED_BEFORE_INSTALL
    ):
        raise UnsupportedFieldError(
            f"container(s) {', '.join(always)} use imagePullPolicy Always, which cannot be "
            "honoured offline; set spec.offline.allowDisconnectedStart to true to accept it "
            "anyway, or use IfNotPresent or Never"
        )


def _check_secret_env_policy(spec: StaticSetSpec) -> None:
    """Reject Secret environment references unless disclosure is accepted.

    The check is separate from resolution so that the rejection can name the exact
    container, variable and Secret.

    Args:
        spec: The specification to check.

    Raises:
        SecretDisclosureError: If a Secret would be exposed as an environment
            value and ``spec.policy.allowInlineSecretEnv`` is false.
    """
    if spec.policy.allow_inline_secret_env:
        return
    containers = [*spec.template.spec.containers, *(spec.template.spec.init_containers or [])]
    for container in containers:
        for env_var in container.env or []:
            source = env_var.value_from
            if source is not None and source.secret_key_ref is not None:
                raise SecretDisclosureError(
                    f"container {container.name!r} reads Secret "
                    f"{source.secret_key_ref.name!r} into environment variable "
                    f"{env_var.name!r}; a static Pod cannot resolve secretKeyRef offline. "
                    "Mount the Secret as a volume instead, or set "
                    "spec.policy.allowInlineSecretEnv to true to accept that the value is "
                    "written into the manifest and exposed through the mirror Pod"
                )
        for env_from in container.env_from or []:
            if env_from.secret_ref is not None:
                raise SecretDisclosureError(
                    f"container {container.name!r} imports Secret "
                    f"{env_from.secret_ref.name!r} into its environment; a static Pod cannot "
                    "resolve secretRef offline. Mount the Secret as a volume instead, or set "
                    "spec.policy.allowInlineSecretEnv to true"
                )


def _build_volumes(
    pod_spec: PodSpec,
    materializer: _Materializer,
) -> list[dict[str, Any]]:
    """Materialize every volume and emit the static Pod's volume list.

    Args:
        pod_spec: The template's Pod specification.
        materializer: The collector for materialized files.

    Returns:
        The emitted volumes.

    Raises:
        TranslationError: If a referenced ConfigMap or Secret is missing and not
            optional.
    """
    emitted: list[dict[str, Any]] = []
    for volume in pod_spec.volumes or []:
        if volume.empty_dir is not None:
            emitted.append({"name": volume.name, "emptyDir": _dump(volume.empty_dir)})
            continue
        if volume.config_map is not None:
            directory = f"config/{volume.name}"
            _materialize(
                materializer,
                "ConfigMap",
                volume,
                volume.config_map.name,
                directory,
                default_mode=0o644,
            )
        else:
            secret = volume.secret
            assert secret is not None  # the model requires exactly one supported source
            directory = f"secrets/{volume.name}"
            _materialize(
                materializer,
                "Secret",
                volume,
                secret.secret_name,
                directory,
                default_mode=0o400 if secret.default_mode is None else secret.default_mode,
            )
        emitted.append({"name": volume.name, "hostPath": {"path": directory, "type": "Directory"}})
    return emitted


def _materialize(
    materializer: _Materializer,
    kind: Literal["ConfigMap", "Secret"],
    volume: Volume,
    name: str,
    directory: str,
    *,
    default_mode: int,
) -> None:
    """Write one volume's files into the materializer.

    Args:
        materializer: The collector for materialized files.
        kind: The referenced object's kind.
        volume: The volume being materialized.
        name: The referenced object's name.
        directory: The bundle-relative directory the files belong in.
        default_mode: The mode for files whose item does not set one.

    Raises:
        TranslationError: If the object was not resolved and the volume is not
            optional, or if a selected key is absent.
        UnsupportedFieldError: If an item maps to an unusable path.
    """
    source = volume.config_map if kind == "ConfigMap" else volume.secret
    assert source is not None
    data = materializer.read(kind, name)
    if data is None:
        if source.optional:
            return
        raise TranslationError(
            f"{kind} {name!r} referenced by volume {volume.name!r} was not found; mark the "
            "volume optional if it may legitimately be absent"
        )

    items = source.items
    if items:
        for item in items:
            if item.key not in data:
                if source.optional:
                    continue
                raise TranslationError(
                    f"{kind} {name!r} has no key {item.key!r} for volume {volume.name!r}"
                )
            path = _item_path(directory, item.path, volume.name)
            materializer.add(path, data[item.key], _mode(item.mode, default_mode, path))
        return

    for key in sorted(data):
        path = _item_path(directory, key, volume.name)
        materializer.add(path, data[key], _mode(None, default_mode, path))


def _item_path(directory: str, relative: str, volume_name: str) -> str:
    """Return the bundle-relative path for one volume item.

    Args:
        directory: The volume's bundle-relative directory.
        relative: The item's relative path within the volume.
        volume_name: The volume's name, used in the error message.

    Returns:
        The validated bundle-relative path.

    Raises:
        UnsupportedFieldError: If the item path is absolute, empty or traversing.
    """
    try:
        validated = validate_relative(relative)
    except Exception as error:  # noqa: BLE001 - re-raised with volume context
        raise UnsupportedFieldError(
            f"volume {volume_name!r} maps a key to path {relative!r}, which is not a safe "
            "relative path inside the volume"
        ) from error
    return f"{directory}/{validated}"


def _mode(explicit: int | None, default: int, path: str) -> int:
    """Return the mode to use for a materialized file.

    Args:
        explicit: The item's mode, if it set one.
        default: The volume's default mode.
        path: The file's path, used in the error message.

    Returns:
        The validated mode.

    Raises:
        UnsupportedFieldError: If the mode is out of range.
    """
    mode = default if explicit is None else explicit
    if not 0 <= mode <= 0o777:
        raise UnsupportedFieldError(f"mode {mode!r} for {path!r} is out of range")
    return mode


def _compile_container(
    container: Container,
    pod_spec: PodSpec,
    spec: StaticSetSpec,
    materializer: _Materializer,
) -> dict[str, Any]:
    """Compile one container, resolving its environment and rewriting its mounts.

    Args:
        container: The template container.
        pod_spec: The template's Pod specification, needed for the pod-level
            security context.
        spec: The whole specification, needed for the workload policy.
        materializer: The collector for materialized files.

    Returns:
        The emitted container.

    Raises:
        TranslationError: If an environment reference cannot be resolved.
        SecretDisclosureError: If a Secret would be disclosed.
        UnsupportedFieldError: If a probe declares no action, or a Secret volume is
            mounted without a usable mode.
    """
    emitted = _dump_except(container, _COMPILED_SEPARATELY)
    environment = _resolve_env(container, spec, materializer)
    if environment:
        emitted["env"] = environment
    mounts = _resolve_mounts(container, pod_spec)
    if mounts:
        emitted["volumeMounts"] = mounts
    for wire_name in ("livenessProbe", "readinessProbe", "startupProbe"):
        probe = emitted.get(wire_name)
        if probe is not None:
            _check_probe(probe, container.name, wire_name)
    return emitted


def _resolve_env(
    container: Container,
    spec: StaticSetSpec,
    materializer: _Materializer,
) -> list[dict[str, str]]:
    """Resolve a container's environment into literal name/value pairs.

    ``envFrom`` sources are expanded first and in order, so that a later source wins
    over an earlier one; explicit ``env`` entries are applied last and win over
    everything, matching Kubernetes' own precedence.

    Args:
        container: The template container.
        spec: The whole specification, for the Secret disclosure policy.
        materializer: The collector, holding the resolved configuration.

    Returns:
        The resolved environment, sorted by variable name.

    Raises:
        TranslationError: If a reference cannot be resolved and is not optional.
        SecretDisclosureError: If a Secret value would be inlined.
    """
    environment: dict[str, str] = {}
    for source in container.env_from or []:
        environment.update(_expand_env_from(source, container.name, materializer))
    for env_var in container.env or []:
        if env_var.value is not None:
            environment[env_var.name] = env_var.value
            continue
        source = env_var.value_from
        if source is None:
            raise TranslationError(
                f"container {container.name!r} variable {env_var.name!r} sets neither "
                "'value' nor 'valueFrom'"
            )
        try:
            environment[env_var.name] = _resolve_env_source(
                source, env_var.name, container.name, spec, materializer
            )
        except _OptionalMissingError:
            environment.pop(env_var.name, None)
    return [{"name": name, "value": environment[name]} for name in sorted(environment)]


def _expand_env_from(
    source: EnvFromSource,
    container_name: str,
    materializer: _Materializer,
) -> dict[str, str]:
    """Expand one ``envFrom`` source into environment entries.

    Args:
        source: The ``envFrom`` entry.
        container_name: The container's name, used in error messages.
        materializer: The collector, holding the resolved configuration.

    Returns:
        The expanded environment entries.

    Raises:
        TranslationError: If the referenced object is missing and not optional.
        SecretDisclosureError: If a Secret would be inlined without opting in.
    """
    prefix = source.prefix or ""
    if source.config_map_ref is not None:
        reference = source.config_map_ref
        data = materializer.read("ConfigMap", reference.name)
        if data is None:
            if reference.optional:
                return {}
            raise TranslationError(
                f"container {container_name!r} imports ConfigMap {reference.name!r}, which was "
                "not found; mark the reference optional if it may legitimately be absent"
            )
        return _keyed(data, prefix, container_name, reference.name)
    reference = source.secret_ref
    assert reference is not None
    raise SecretDisclosureError(
        f"container {container_name!r} imports Secret {reference.name!r} through envFrom; a "
        "static Pod cannot resolve secretRef offline. Mount the Secret as a volume instead, "
        "or set spec.policy.allowInlineSecretEnv to true"
    )


def _keyed(
    data: Mapping[str, bytes], prefix: str, container_name: str, name: str
) -> dict[str, str]:
    """Turn object data into environment entries, dropping unusable keys.

    Kubernetes drops keys that are not valid environment variable names, so the
    compiler does the same rather than producing a Pod the API server would reject.

    Args:
        data: The object's data.
        prefix: The optional variable-name prefix.
        container_name: The container's name, used in error messages.
        name: The object's name, used in error messages.

    Returns:
        The environment entries.

    Raises:
        TranslationError: If a value is not valid UTF-8 text.
    """
    entries: dict[str, str] = {}
    for key in sorted(data):
        variable = f"{prefix}{key}"
        if not _ENV_NAME.match(variable):
            continue
        entries[variable] = _decode(data, key, "ConfigMap", name, variable, container_name)
    return entries


def _decode(
    data: Mapping[str, bytes],
    key: str,
    kind: str,
    name: str,
    variable: str,
    container_name: str,
) -> str:
    """Decode one object key into an environment value.

    Args:
        data: The object's data.
        key: The key to decode.
        kind: The object's kind, used in the error message.
        name: The object's name, used in the error message.
        variable: The environment variable, used in the error message.
        container_name: The container's name, used in the error message.

    Returns:
        The decoded value.

    Raises:
        TranslationError: If the value is not valid UTF-8 text.
    """
    try:
        return data[key].decode("utf-8")
    except UnicodeDecodeError as error:
        raise TranslationError(
            f"{kind} {name!r} key {key!r} read into {variable!r} of container "
            f"{container_name!r} is not UTF-8 text and cannot become an environment "
            "variable; mount it as a file"
        ) from error


def _resolve_env_source(
    source: EnvVarSource,
    variable: str,
    container_name: str,
    spec: StaticSetSpec,
    materializer: _Materializer,
) -> str:
    """Resolve one ``valueFrom`` reference to a literal value.

    Args:
        source: The ``valueFrom`` source.
        variable: The variable being resolved, used in error messages.
        container_name: The container's name, used in error messages.
        spec: The whole specification, for the Secret disclosure policy.
        materializer: The collector, holding the resolved configuration.

    Returns:
        The literal value.

    Raises:
        TranslationError: If the reference cannot be resolved and is not optional.
        SecretDisclosureError: If a Secret would be inlined without opting in.
        _OptionalMissingError: If an optional reference resolved to nothing.
    """
    if source.config_map_key_ref is not None:
        reference = source.config_map_key_ref
        data = materializer.read("ConfigMap", reference.name)
        if data is None or reference.key not in data:
            if reference.optional:
                raise _OptionalMissingError
            raise TranslationError(
                f"container {container_name!r} variable {variable!r} reads "
                f"{reference.name!r}/{reference.key!r}, which was not found"
            )
        return _decode(data, reference.key, "ConfigMap", reference.name, variable, container_name)

    reference = source.secret_key_ref
    assert reference is not None
    if not spec.policy.allow_inline_secret_env:
        raise SecretDisclosureError(
            f"container {container_name!r} variable {variable!r} reads Secret "
            f"{reference.name!r}; set spec.policy.allowInlineSecretEnv to true to write the "
            "value into the manifest and expose it through the mirror Pod"
        )
    data = materializer.read("Secret", reference.name)
    if data is None or reference.key not in data:
        if reference.optional:
            raise _OptionalMissingError
        raise TranslationError(
            f"container {container_name!r} variable {variable!r} reads Secret "
            f"{reference.name!r}/{reference.key!r}, which was not found"
        )
    value = _decode(data, reference.key, "Secret", reference.name, variable, container_name)
    materializer.warnings.append(
        f"Secret {reference.name!r} key {reference.key!r} was written into the static Pod "
        f"manifest as environment variable {variable!r} of container {container_name!r}; it is "
        "readable through the mirror Pod"
    )
    return value


def _resolve_mounts(container: Container, pod_spec: PodSpec) -> list[dict[str, Any]]:
    """Rewrite a container's volume mounts for the static Pod.

    Materialized ConfigMap and Secret volumes become read-only; ``emptyDir`` mounts
    keep the read-only flag the template asked for.

    Args:
        container: The template container.
        pod_spec: The template's Pod specification.

    Returns:
        The emitted mounts.

    Raises:
        UnsupportedFieldError: If a materialized Secret volume has no usable mode.
        TranslationError: If a mount names a volume the Pod does not define.
    """
    defined = {volume.name: volume for volume in pod_spec.volumes or []}
    emitted: list[dict[str, Any]] = []
    for mount in container.volume_mounts or []:
        volume = defined.get(mount.name)
        if volume is None:
            raise TranslationError(
                f"container {container.name!r} mounts volume {mount.name!r}, which the template "
                "does not define"
            )
        if volume.secret is not None and volume.secret.default_mode is None:
            _require_explicit_secret_mode(container, pod_spec, mount.name)
        entry = _dump(mount)
        if volume.config_map is not None or volume.secret is not None:
            entry["readOnly"] = True
        emitted.append(entry)
    return emitted


def _require_explicit_secret_mode(container: Container, pod_spec: PodSpec, volume: str) -> None:
    """Refuse a Secret volume mount whose readability cannot be guaranteed.

    The installer writes Secret files as ``0400`` by default, because that is the
    most restrictive mode that still works when the container runs as root. When the
    container is not *provably* root — because the template declares a non-root user,
    or declares nothing at all — the file would simply be unreadable, so the template
    has to say which mode it wants.

    Args:
        container: The container mounting the Secret.
        pod_spec: The template's Pod specification.
        volume: The volume's name, used in the error message.

    Raises:
        UnsupportedFieldError: If the container is not provably root.
    """
    if _provably_root(container, pod_spec):
        return
    raise UnsupportedFieldError(
        f"container {container.name!r} mounts Secret volume {volume!r} without a "
        "defaultMode; the installer writes Secret files as 0400, which a non-root container "
        "cannot read. Set spec.template.spec.volumes[].secret.defaultMode explicitly, or "
        "declare the container as root with securityContext.runAsUser: 0"
    )


def _provably_root(container: Container, pod_spec: PodSpec) -> bool:
    """Return whether the container is known to run as root.

    Args:
        container: The container being checked.
        pod_spec: The template's Pod specification.

    Returns:
        ``True`` only when the template states, at container or Pod level, that the
        process runs as uid 0.
    """
    context = container.security_context
    pod_context = pod_spec.security_context
    if context is not None and context.run_as_non_root:
        return False
    if pod_context is not None and pod_context.run_as_non_root:
        return False
    user = None
    if context is not None and context.run_as_user is not None:
        user = context.run_as_user
    elif pod_context is not None and pod_context.run_as_user is not None:
        user = pod_context.run_as_user
    return user == 0


def _check_probe(probe: Mapping[str, Any], container: str, probe_name: str) -> None:
    """Refuse a probe that declares no handler.

    Args:
        probe: The emitted probe.
        container: The container's name, used in the error message.
        probe_name: The probe's field name, used in the error message.

    Raises:
        UnsupportedFieldError: If no handler is set.
    """
    handlers = {"exec", "httpGet", "tcpSocket", "grpc"}
    if not handlers.intersection(probe):
        raise UnsupportedFieldError(
            f"container {container!r} {probe_name} declares no exec, httpGet, tcpSocket or "
            "grpc handler"
        )


def _assemble_pod(
    spec: StaticSetSpec,
    containers: list[dict[str, Any]],
    init_containers: list[dict[str, Any]],
    volumes: list[dict[str, Any]],
) -> dict[str, Any]:
    """Assemble the static Pod from its compiled parts.

    Args:
        spec: The specification, for the Pod-level settings and template metadata.
        containers: The compiled containers.
        init_containers: The compiled init containers.
        volumes: The emitted volumes.

    Returns:
        The Pod manifest, without a name, a namespace or a revision annotation.
    """
    pod_spec = spec.template.spec
    emitted: dict[str, Any] = {"containers": containers, "restartPolicy": "Always"}
    if init_containers:
        emitted["initContainers"] = init_containers
    if volumes:
        emitted["volumes"] = volumes
    for attribute, wire_name in _POD_SPEC_SCALARS:
        value = getattr(pod_spec, attribute)
        if value is not None:
            emitted[wire_name] = value
    for attribute, wire_name in _POD_SPEC_MODELS:
        value = getattr(pod_spec, attribute)
        if value is not None:
            emitted[wire_name] = _dump(value)
    metadata = spec.template.metadata
    return {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {
            "labels": dict(metadata.labels or {}) if metadata else {},
            "annotations": dict(metadata.annotations or {}) if metadata else {},
        },
        "spec": emitted,
    }


def _dump(model: Any) -> Any:
    """Serialize a wire model to its Kubernetes form.

    Args:
        model: A :class:`~static_sets.base.WireModel` instance or a plain value.

    Returns:
        A dict with camelCase keys and unset fields removed, or the value itself.
    """
    if isinstance(model, list):
        return [_dump(item) for item in model]
    if hasattr(model, "model_dump"):
        return model.model_dump(by_alias=True, exclude_none=True)
    return model


def _dump_except(model: Any, excluded: Iterable[str]) -> dict[str, Any]:
    """Serialize a wire model, leaving out fields that are compiled separately.

    Args:
        model: The model to serialize.
        excluded: The wire field names to leave out.

    Returns:
        The serialized mapping.
    """
    values: dict[str, Any] = model.model_dump(by_alias=True, exclude_none=True)
    for name in excluded:
        values.pop(name, None)
    return values
