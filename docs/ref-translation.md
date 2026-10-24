# Translation: the supported workload subset

A static Pod is started by kubelet directly from a file, so none of the machinery that
normally resolves a Pod's references runs for it: there is no admission controller, no
scheduler, and — after the reboot this exists for — no API server. The operator
therefore compiles the template into a **self-contained** Pod: every reference is
resolved at install time, every piece of configuration is materialized on the node,
and anything it cannot resolve that way is **rejected**.

Rejection is the point. A template field that was silently dropped would produce a Pod
that starts, looks healthy, and behaves differently from what was asked for.

## What the emitted Pod contains

| Kind | Fields |
| --- | --- |
| Pod | `containers`, `initContainers`, `volumes`, `restartPolicy` (always `Always`), `terminationGracePeriodSeconds`, `dnsPolicy`, `dnsConfig`, `hostNetwork`, `hostPID`, `hostIPC`, `hostname`, `hostAliases`, `shareProcessNamespace`, `securityContext` |
| Container | `name`, `image`, `imagePullPolicy`, `command`, `args`, `workingDir`, `env`, `envFrom`, `ports`, `resources`, `volumeMounts`, `livenessProbe`, `readinessProbe`, `startupProbe`, `securityContext`, `stdin`, `stdinOnce`, `tty` |
| Probe | `exec`, `httpGet`, `tcpSocket`, `grpc`, and the timing and threshold fields |
| Volume | `configMap`, `secret`, `emptyDir` |
| Volume mount | `name`, `mountPath`, `readOnly` |
| Template metadata | `labels`, `annotations` |

Everything else is refused. A field the model does not know is rejected by validation;
a field the model knows but the compiler cannot honour is rejected with a message that
names it and, where one exists, the alternative.

## Environment

Environment values are resolved to **literals** at install time, because a static Pod
cannot resolve a `configMapKeyRef` or a `secretKeyRef` offline: kubelet would have to
ask the API server.

- `env[].value` is kept as-is.
- `env[].valueFrom.configMapKeyRef` is resolved from the ConfigMap now. A missing key
  fails unless `optional: true`, in which case the variable is omitted.
- `envFrom[].configMapRef` expands every key, applying `prefix`. Sources are applied in
  order, so a later source wins over an earlier one; explicit `env` entries are applied
  last and win over everything, matching Kubernetes' own precedence.
- Keys that are not valid environment variable names are dropped, as Kubernetes does.
- A value that is not valid UTF-8 text cannot become an environment variable; it is
  refused with a message that suggests mounting it as a file instead.

### Secrets

A ConfigMap value is not confidential, so inlining it is safe. A Secret value is not,
and a static Pod has no offline way to resolve one:

- **as a volume** — supported, and the recommended shape. The data is materialized
  as a file inside the revision directory with a restrictive mode, and the emitted Pod
  mounts that directory read-only. The value never appears in the manifest, in the
  mirror Pod, or in status.
- **as an environment value** — refused unless `spec.policy.allowInlineSecretEnv` is
  `true`. The only offline ways to satisfy the request both disclose the value: either
  it is written into the manifest, where the mirror Pod and every copy of the file
  expose it, or the variable is simply gone after the reboot. With the opt-in the value
  is inlined and the disclosure is recorded in the placement's warnings so it is visible
  in status.

### Secret volume modes

Secret files are written with mode `0400` by default — the most restrictive mode that
still works when the container runs as root. When the container is not *provably* root
(the template declares a non-root user, or declares nothing at all), that file would be
unreadable, so the template must state `volumes[].secret.defaultMode` explicitly. This
is refused rather than guessed at:

```
container 'web' mounts Secret volume 'tls' without a defaultMode; the manager writes
Secret files as 0400, which a non-root container cannot read. Set
spec.template.spec.volumes[].secret.defaultMode explicitly, or declare the container as
root with securityContext.runAsUser: 0
```

## Volumes

| Source | Handling |
| --- | --- |
| `configMap` | Materialized as files under the revision directory (`0644` by default), mounted read-only as a `hostPath`. `items` selects keys, renames them and sets per-file modes; `optional` makes an absent ConfigMap or key acceptable. |
| `secret` | As above, under a `secrets/` directory, with `0400` as the default mode. |
| `emptyDir` | Passed through unchanged: kubelet provides it without the API server. |
| `hostPath` | **Refused.** The only `hostPath` mounts in the manifest are the revision directories the operator materialized, which keeps the workload inside the bundle the manager owns. |
| `persistentVolumeClaim` | **Refused.** Attaching a volume needs the API server and a bound PV. |
| `projected`, `downwardAPI`, `nfs`, … | **Refused.** Each needs a source that will not exist offline. |

`volumeMounts[].subPath`, `subPathExpr`, `mountPropagation` and `recursiveReadOnly`
are refused: they change path semantics against a volume the manager owns, and a
sub-path would escape the directory that was validated.

Item paths are validated, so an item cannot map a key outside its own volume
directory.

## Probes, resources and security contexts

Probes, `resources`, container and Pod `securityContext` and `ports` are preserved
unchanged. A probe that declares no handler is refused, because kubelet would reject
the Pod anyway and the error would be harder to read.

`securityContext.privileged: true` is refused unless `spec.policy.allowPrivileged` is
set: kubelet starts a static Pod straight from a file, so no admission controller gets
a chance to review it.

## What the emitted Pod deliberately does not carry

**Scheduler placement.** `nodeSelector`, `affinity`, `tolerations` and
`topologySpreadConstraints` in the template are rejected with a message pointing at
`spec.placement`. kubelet never schedules a static Pod, so those fields would be
inert, and silently deleting them would look like they had been honoured.

**API-dependent references.** These are refused because a static Pod must survive a
reboot with no API server:

| Field | Why |
| --- | --- |
| `serviceAccountName`, `automountServiceAccountToken` | A projected service-account token is fetched from the API server; without one, a client that needs credentials cannot start. |
| `imagePullSecrets` | Pull credentials come from a Secret through the API server. Pre-pull the image and use `IfNotPresent`. |
| `schedulerName` | There is no scheduler. |
| `priorityClassName` | Admission resolves it; kubelet cannot preempt for a static Pod. |
| `enableServiceLinks` | Service environment variables are discovered from the API server. |

## Revisions

The revision is a hash of the compiled Pod (with *relative* configuration paths, so it
does not depend on where the bundle is mounted) and of every materialized file and its
mode. Two consequences matter:

- it is **deterministic**: the same template and the same resolved configuration always
  produce the same revision, so a reconcile that changes nothing is not a rollout;
- it is **content-sensitive**: editing a referenced ConfigMap or Secret produces a new
  revision, because the file content is part of the hash.

Nothing node-specific enters the hash, so all placements of a StaticSet agree on what
`desiredRevision` means.

## The admission caveat

The generated CRD is a structural schema, so the API server *prunes* a field it does
not know instead of refusing it. Without the validating webhook
(`webhook.enabled=true` in the chart, which needs cert-manager) an unknown template
field is dropped by the API server before the operator ever sees it — the one case
where "never silently dropped" degrades to "dropped by somebody else". Deploy with the
webhook enabled if that matters to you; see
[How to install](./how-to-install.md#3-decide-about-the-admission-webhook).