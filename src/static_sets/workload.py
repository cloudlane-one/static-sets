"""The supported subset of a workload ``PodTemplateSpec``.

Only the fields this operator can compile into a *self-contained* static Pod are
modelled. A field that is not modelled is not silently ignored: because every
model forbids unknown fields, it is rejected while the object is validated, and
the operator refuses to install a workload it would otherwise quietly degrade.

Two groups of fields are deliberately excluded from the template and modelled
only so that they can be rejected with an actionable message:

**Scheduler placement versus the emitted static workload.**
Fields such as ``nodeSelector``, ``affinity``, ``tolerations`` and
``topologySpreadConstraints`` decide *which node* the scheduling probe is scheduled onto.
kubelet never schedules a static Pod: it starts whatever file it finds, on the
node whose kubelet finds it. Placement therefore belongs to
``spec.placement`` of the StaticSet, and the operator strips those fields from the
emitted static Pod. A template that sets them is rejected rather than accepted and
ignored.

**API-dependent references.** ``serviceAccountName``, ``automountServiceAccountToken``,
``imagePullSecrets``, ``schedulerName``, ``priorityClassName`` and
``enableServiceLinks`` all require a live API server or a scheduler, so they
cannot survive an offline host reboot and are rejected.

The models here are therefore *not* the official client's ``V1Pod`` family, even
though those are pydantic models too. They are the StaticSet API: they define the
CRD, they forbid unknown fields, and they name the alternative when a field cannot
be honoured. Using ``V1Pod`` would admit every field Kubernetes has, including the
ones this operator must refuse, and would turn a rejection into a silent drop. The
official models are used for the scheduler-placement API and where objects cross the
API boundary — see :mod:`static_sets.models` and :mod:`static_sets.api`.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal

from kubernetes import client as k8s
from pydantic import Field, model_validator

from static_sets.base import WireModel

__all__ = [
    "Capabilities",
    "ConfigMapEnvSource",
    "ConfigMapKeySelector",
    "ConfigMapVolumeSource",
    "Container",
    "ContainerPort",
    "ContainerSecurityContext",
    "DnsConfig",
    "EmptyDirVolumeSource",
    "EnvFromSource",
    "EnvVar",
    "EnvVarSource",
    "ExecAction",
    "GRPCAction",
    "HTTPGetAction",
    "HostAlias",
    "KeyToPath",
    "PodSecurityContext",
    "PodSpec",
    "PodTemplate",
    "Probe",
    "PullPolicy",
    "ResourceRequirements",
    "SecretEnvSource",
    "SecretKeySelector",
    "SecretVolumeSource",
    "SeccompProfile",
    "TCPSocketAction",
    "TemplateMeta",
    "Volume",
    "VolumeMount",
]


class PullPolicy(StrEnum):
    """Supported ``imagePullPolicy`` values.

    ``Never`` is the only policy that is safe on a disconnected host, because it
    never contacts a registry. ``IfNotPresent`` is the default and is safe as long
    as the image is already present.
    """

    ALWAYS = "Always"
    IF_NOT_PRESENT = "IfNotPresent"
    NEVER = "Never"


class SeccompProfile(WireModel):
    """A ``seccompProfile`` hint, passed through unchanged."""

    type: Literal["RuntimeDefault", "Unconfined", "Localhost"]
    localhost_profile: str | None = None
    """Path of the profile when ``type`` is ``Localhost``."""


class Capabilities(WireModel):
    """Linux capabilities to add or drop."""

    add: list[str] | None = None
    drop: list[str] | None = None


class HostAlias(WireModel):
    """An entry appended to the container's ``/etc/hosts``."""

    ip: str
    hostnames: list[str] = Field(min_length=1)


class DnsConfig(WireModel):
    """Pod DNS settings that kubelet can apply without the API server."""

    nameservers: list[str] | None = None
    searches: list[str] | None = None
    options: list[dict[str, Any]] | None = None


class EnvVarSource(WireModel):
    """Sources a single environment variable.

    Only ``configMapKeyRef`` and ``secretKeyRef`` are supported; ``fieldRef`` and
    ``resourceFieldRef`` are rejected by the unknown-field rule.
    """

    config_map_key_ref: ConfigMapKeySelector | None = None
    secret_key_ref: SecretKeySelector | None = None


class EnvVar(WireModel):
    """A container environment variable.

    The name is constrained to the C identifier form Kubernetes requires, so an
    unusable name is refused here rather than producing a manifest kubelet rejects.
    """

    name: str = Field(min_length=1, pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")
    value: str | None = None
    value_from: EnvVarSource | None = None


class ConfigMapKeySelector(WireModel):
    """A reference to one key of a ConfigMap, used to fill an environment variable."""

    name: str = Field(min_length=1)
    key: str = Field(min_length=1)
    optional: bool = False


class SecretKeySelector(WireModel):
    """A reference to one key of a Secret, used to fill an environment variable."""

    name: str = Field(min_length=1)
    key: str = Field(min_length=1)
    optional: bool = False


class ConfigMapEnvSource(WireModel):
    """A whole-ConfigMap environment source, as ``envFrom`` uses it."""

    name: str = Field(min_length=1)
    optional: bool = False


class SecretEnvSource(WireModel):
    """A whole-Secret environment source, as ``envFrom`` uses it."""

    name: str = Field(min_length=1)
    optional: bool = False


class EnvFromSource(WireModel):
    """A bulk environment source, with an optional prefix."""

    prefix: str | None = None
    config_map_ref: ConfigMapEnvSource | None = None
    secret_ref: SecretEnvSource | None = None

    @model_validator(mode="after")
    def _exactly_one_source(self) -> EnvFromSource:
        """Reject an ``envFrom`` entry that names zero or several sources.

        Returns:
            The validated source.

        Raises:
            UnsupportedFieldError: If neither or both of ``configMapRef`` and
                ``secretRef`` are set.
        """
        sources = [self.config_map_ref, self.secret_ref]
        if sum(source is not None for source in sources) != 1:
            raise ValueError(
                "envFrom entries must set exactly one of 'configMapRef' or 'secretRef'"
            )
        return self


class KeyToPath(WireModel):
    """Maps one key of a ConfigMap or Secret to a file name."""

    key: str = Field(min_length=1)
    path: str = Field(min_length=1)
    mode: int | None = None


class ConfigMapVolumeSource(WireModel):
    """A ConfigMap mounted as files, materialized into the static bundle."""

    name: str = Field(min_length=1)
    items: list[KeyToPath] | None = None
    default_mode: int | None = None
    optional: bool = False


class SecretVolumeSource(WireModel):
    """A Secret mounted as files, materialized with restrictive permissions."""

    secret_name: str = Field(min_length=1)
    items: list[KeyToPath] | None = None
    default_mode: int | None = None
    optional: bool = False


class EmptyDirVolumeSource(WireModel):
    """An ``emptyDir`` volume, which kubelet provides without the API server."""

    medium: Literal["", "Memory"] = ""
    size_limit: str | None = None


class Volume(WireModel):
    """A Pod volume from the supported subset."""

    name: str = Field(min_length=1)
    config_map: ConfigMapVolumeSource | None = None
    secret: SecretVolumeSource | None = None
    empty_dir: EmptyDirVolumeSource | None = None

    # Modelled purely so that they can be rejected with a clear reason. ``hostPath``
    # would let a template reach outside its own bundle; ``persistentVolumeClaim``
    # needs a live API server (and a bound PV) to attach; ``projected`` composes
    # service-account tokens and other API references.
    host_path: dict[str, Any] | None = None
    persistent_volume_claim: dict[str, Any] | None = None
    projected: dict[str, Any] | None = None
    downward_api: dict[str, Any] | None = None

    @model_validator(mode="after")
    def _reject_api_dependent_sources(self) -> Volume:
        """Reject volume sources that cannot survive an offline reboot.

        Returns:
            The validated volume.

        Raises:
            UnsupportedFieldError: If an unsupported source is set, or if the
                volume names no supported source at all.
        """
        unsupported = {
            "hostPath": self.host_path,
            "persistentVolumeClaim": self.persistent_volume_claim,
            "projected": self.projected,
            "downwardAPI": self.downward_api,
        }
        for field, value in unsupported.items():
            if value is not None:
                raise ValueError(
                    f"volumes[].{field} is not supported: a static Pod may only use "
                    "'configMap', 'secret' and 'emptyDir' volumes"
                )
        if self.config_map is None and self.secret is None and self.empty_dir is None:
            raise ValueError(
                f"volume {self.name!r} sets no supported source; use one of "
                "'configMap', 'secret' or 'emptyDir'"
            )
        return self


class VolumeMount(WireModel):
    """A container's mount of one of the Pod's volumes."""

    name: str = Field(min_length=1)
    mount_path: str = Field(min_length=1)
    read_only: bool = False

    # Present only to be rejected explicitly; see the module docstring.
    sub_path: str | None = None
    sub_path_expr: str | None = None
    mount_propagation: str | None = None
    recursive_read_only: str | None = None

    @model_validator(mode="after")
    def _reject_unsupported_mount_semantics(self) -> VolumeMount:
        """Reject mount options that change path semantics.

        ``subPath`` and ``subPathExpr`` are resolved against a volume that kubelet
        has to prepare before the container starts; on a static Pod the volume is a
        directory the installer owns, so a sub-path would escape the revision
        directory it was validated against.

        Returns:
            The validated mount.

        Raises:
            UnsupportedFieldError: If any unsupported mount option is set.
        """
        unsupported = {
            "subPath": self.sub_path,
            "subPathExpr": self.sub_path_expr,
            "mountPropagation": self.mount_propagation,
            "recursiveReadOnly": self.recursive_read_only,
        }
        for field, value in unsupported.items():
            if value is not None:
                raise ValueError(f"volumeMounts[].{field} is not supported for a static Pod")
        return self


class ContainerPort(WireModel):
    """A port a container listens on."""

    container_port: int = Field(ge=1, le=65535)
    host_port: int | None = None
    host_ip: str | None = Field(default=None, alias="hostIP")
    """Bound to an explicit ``hostIP`` alias because ``to_camel`` would emit
    ``hostIp``, which is not the field name Kubernetes uses."""
    name: str | None = None
    protocol: Literal["TCP", "UDP", "SCTP"] = "TCP"


class ExecAction(WireModel):
    """A command executed inside the container."""

    command: list[str] = Field(min_length=1)


class HTTPGetAction(WireModel):
    """An HTTP GET performed against the container."""

    path: str | None = None
    port: int | str
    host: str | None = None
    scheme: Literal["HTTP", "HTTPS"] = "HTTP"
    http_headers: list[dict[str, str]] | None = None


class TCPSocketAction(WireModel):
    """A TCP connection attempt against the container."""

    port: int | str
    host: str | None = None


class GRPCAction(WireModel):
    """A gRPC health probe."""

    port: int = Field(ge=1, le=65535)
    service: str | None = None


class Probe(WireModel):
    """A liveness, readiness or startup probe."""

    exec: ExecAction | None = None
    http_get: HTTPGetAction | None = None
    tcp_socket: TCPSocketAction | None = None
    grpc: GRPCAction | None = None
    initial_delay_seconds: int | None = None
    timeout_seconds: int | None = None
    period_seconds: int | None = None
    success_threshold: int | None = None
    failure_threshold: int | None = None
    termination_grace_period_seconds: int | None = None


class ResourceRequirements(k8s.V1ResourceRequirements):
    """Container resource requests and limits, from the official Kubernetes type.

    ``claims`` belongs to the scheduler, which never sees a static Pod: kubelet
    starts it from a file and cannot resolve a claim. It is refused rather than
    written into a manifest that would silently never take effect.
    """

    @model_validator(mode="after")
    def _reject_claims(self) -> ResourceRequirements:
        """Refuse resource claims.

        Returns:
            The validated requirements.

        Raises:
            ValueError: If a claim is declared.
        """
        if self.claims:
            raise ValueError(
                "resources.claims is not supported: a static Pod is not scheduled, so "
                "a resource claim could never be resolved"
            )
        return self


class ContainerSecurityContext(WireModel):
    """Container-level security settings, preserved for the static workload."""

    privileged: bool | None = None
    allow_privilege_escalation: bool | None = None
    read_only_root_filesystem: bool | None = None
    run_as_user: int | None = None
    run_as_group: int | None = None
    run_as_non_root: bool | None = None
    capabilities: Capabilities | None = None
    seccomp_profile: SeccompProfile | None = None


class PodSecurityContext(WireModel):
    """Pod-level security settings, preserved for the static workload."""

    run_as_user: int | None = None
    run_as_group: int | None = None
    run_as_non_root: bool | None = None
    fs_group: int | None = None
    supplemental_groups: list[int] | None = None
    seccomp_profile: SeccompProfile | None = None
    sysctls: list[dict[str, str]] | None = None


class Container(WireModel):
    """A container from the supported subset of the workload API."""

    name: str = Field(min_length=1)
    image: str = Field(min_length=1)
    image_pull_policy: PullPolicy | None = None
    command: list[str] | None = None
    args: list[str] | None = None
    working_dir: str | None = None
    env: list[EnvVar] | None = None
    env_from: list[EnvFromSource] | None = None
    ports: list[ContainerPort] | None = None
    resources: ResourceRequirements | None = None
    volume_mounts: list[VolumeMount] | None = None
    liveness_probe: Probe | None = None
    readiness_probe: Probe | None = None
    startup_probe: Probe | None = None
    security_context: ContainerSecurityContext | None = None
    stdin: bool | None = None
    stdin_once: bool | None = None
    tty: bool | None = None


class PodSpec(WireModel):
    """The workload Pod specification this operator can compile."""

    containers: list[Container] = Field(min_length=1)
    init_containers: list[Container] | None = None
    volumes: list[Volume] | None = None
    restart_policy: Literal["Always"] = "Always"
    runtime_class_name: str | None = Field(default=None, min_length=1, max_length=63)
    """The ``RuntimeClass`` the static Pod is started with.

    Passed through to the manifest because a workload may legitimately need a
    non-default runtime (a sandboxed or a GPU runtime). Note what it costs: kubelet
    resolves a ``RuntimeClass`` through its own cache of the API object, so a
    workload that declares one is *not* guaranteed to start on a host reboot that
    happens while the API server is unreachable. Leave it unset for a workload whose
    whole point is to come back disconnected.

    The scheduling probe cannot use it — the probe must never start — so it is
    carried on the probe's ``staticsets.cloudlane.dev/runtime-class`` label and
    applied when the manifest is rendered.

    The limit is 63 characters because the value doubles as a label value; a
    ``RuntimeClass`` name is a DNS subdomain, which longer names would also be.
    """

    termination_grace_period_seconds: int | None = None
    dns_policy: Literal["ClusterFirstWithHostNet", "ClusterFirst", "Default", "None"] | None = None
    dns_config: DnsConfig | None = None
    host_network: bool | None = None
    host_pid: bool | None = None
    host_ipc: bool | None = None
    hostname: str | None = None
    host_aliases: list[HostAlias] | None = None
    share_process_namespace: bool | None = None
    security_context: PodSecurityContext | None = None

    # Scheduler placement: modelled so that it can be rejected in favour of
    # ``spec.placement``. See the module docstring.
    node_selector: dict[str, str] | None = None
    affinity: dict[str, Any] | None = None
    tolerations: list[dict[str, Any]] | None = None
    topology_spread_constraints: list[dict[str, Any]] | None = None

    # API-dependent references, rejected for the reasons in the module docstring.
    service_account_name: str | None = None
    automount_service_account_token: bool | None = None
    image_pull_secrets: list[dict[str, Any]] | None = None
    scheduler_name: str | None = None
    priority_class_name: str | None = None
    enable_service_links: bool | None = None

    @model_validator(mode="after")
    def _reject_api_dependent_fields(self) -> PodSpec:
        """Reject template fields that need the API server or a scheduler.

        Returns:
            The validated Pod specification.

        Raises:
            UnsupportedFieldError: If any unsupported field is set.
        """
        unsupported = {
            "serviceAccountName": self.service_account_name,
            "automountServiceAccountToken": self.automount_service_account_token,
            "imagePullSecrets": self.image_pull_secrets,
            "schedulerName": self.scheduler_name,
            "priorityClassName": self.priority_class_name,
            "enableServiceLinks": self.enable_service_links,
        }
        for field, value in unsupported.items():
            if value is not None:
                raise ValueError(
                    f"spec.template.spec.{field} is not supported: a static Pod cannot "
                    "depend on the API server or on the scheduler after a host reboot"
                )
        return self

    @model_validator(mode="after")
    def _reject_placement_fields(self) -> PodSpec:
        """Reject placement fields, which belong to ``spec.placement``.

        Returns:
            The validated Pod specification.

        Raises:
            UnsupportedFieldError: If any placement field is set in the template.
        """
        placement = {
            "nodeSelector": self.node_selector,
            "affinity": self.affinity,
            "tolerations": self.tolerations,
            "topologySpreadConstraints": self.topology_spread_constraints,
        }
        for field, value in placement.items():
            if value is not None:
                raise ValueError(
                    f"spec.template.spec.{field} controls where the scheduling probe runs and "
                    "belongs under spec.placement; the emitted static workload carries no "
                    "scheduler placement fields"
                )
        return self

    @model_validator(mode="after")
    def _reject_duplicate_names(self) -> PodSpec:
        """Reject containers, init containers and volumes that share a name.

        Returns:
            The validated Pod specification.

        Raises:
            UnsupportedFieldError: If two entries share a name.
        """
        containers = [*self.containers, *(self.init_containers or [])]
        names = [container.name for container in containers]
        if len(set(names)) != len(names):
            raise ValueError("container names must be unique within the template")
        volumes = [volume.name for volume in self.volumes or []]
        if len(set(volumes)) != len(volumes):
            raise ValueError("volume names must be unique within the template")
        return self


class TemplateMeta(WireModel):
    """The subset of Pod metadata the operator carries onto the static Pod."""

    labels: dict[str, str] | None = None
    annotations: dict[str, str] | None = None


class PodTemplate(WireModel):
    """A ``PodTemplateSpec`` restricted to the supported subset."""

    metadata: TemplateMeta | None = None
    spec: PodSpec
