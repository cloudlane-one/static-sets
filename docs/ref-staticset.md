# The `StaticSet` reference

```yaml
apiVersion: staticsets.cloudlane.dev/v1
kind: StaticSet
metadata:
  name: web
  namespace: static-sets
spec: {…}
status: {…}
```

Namespaced, plural `staticsets`, singular `staticset`, short name `sset`, served and
stored as `v1`. The CRD is generated from the Pydantic models
(`uv run app/render_crds.py`) and installed by the chart from `crds/`.

## `spec`

| Field | Type | Default | Meaning |
| --- | --- | --- | --- |
| `mode` | `Deployment` \| `DaemonSet` | — | How installers are created and placed. |
| `replicas` | integer 0–64 | — | Desired installer placements. Required for `Deployment`, invalid for `DaemonSet`. |
| `template` | `PodTemplateSpec` | — | The workload. Restricted to the [supported subset](./ref-translation.md). |
| `placement` | `Placement` | `{}` | Scheduler placement for the *installers*. |
| `update` | `UpdatePolicy` | see below | How a new revision replaces an old placement. |
| `offline` | `OfflinePolicy` | see below | What must hold before a revision may be published. |
| `cleanup` | `CleanupPolicy` | see below | How unwanted placements are removed. |
| `policy` | `WorkloadPolicy` | see below | Guards on the static workload itself. |
| `installer` | `InstallerSpec` | — required | The installer image and the host paths it may touch. |

### `spec.placement`

Steers the Kubernetes scheduler, which places installer Pods. **Not** copied onto the
emitted static Pod; see [placement and capacity](./about-placement-and-capacity.md).

| Field | Meaning |
| --- | --- |
| `nodeSelector` | Label equality requirements. |
| `affinity.nodeAffinity.requiredDuringSchedulingIgnoredDuringExecution` | Required node selector terms (`matchExpressions`, `matchFields`). |
| `affinity.nodeAffinity.preferredDuringSchedulingIgnoredDuringExecution` | Weighted preferred terms. |
| `tolerations` | Tolerations installers carry. |
| `topologySpreadConstraints` | `maxSkew`, `topologyKey`, `whenUnsatisfiable`, `labelSelector`, `minDomains`. |

Pod affinity and anti-affinity are not supported: they describe co-location with other
Pods, which a static Pod cannot honour because kubelet does not run the scheduler for
it.

### `spec.update`

| Field | Default | Meaning |
| --- | --- | --- |
| `strategy` | `Recreate` | `Recreate` retires the old placement before installing the new one; `Surge` installs the new one first. |
| `maxSurge` | `0` | Placements that may overlap under `Surge`. Must be `0` under `Recreate`. |
| `maxUnavailable` | `0` | Installer replicas that may be unavailable during a `Surge` rollout. Ignored under `Recreate`. |
| `gracePeriodSeconds` | `300` | **Reserved.** Accepted and validated, but not yet used to bound retirement. |

### `spec.offline`

| Field | Default | Meaning |
| --- | --- | --- |
| `retainedRevisions` | `2` | Revision directories kept per placement, newest first. The active revision is always kept. |
| `imageProvisioning` | `ExternallyProvisioned` | `ExternallyProvisioned` means image availability is someone else's problem, and status says so. `VerifiedBeforeInstall` refuses to publish a revision until a checker confirms the images are present. |
| `allowDisconnectedStart` | `true` | Whether `imagePullPolicy: Always` is accepted. Set to `false` to require `IfNotPresent` or `Never` everywhere. |

### `spec.cleanup`

| Field | Default | Meaning |
| --- | --- | --- |
| `onNodeUnreachable` | `Retain` | `Retain` keeps a cleanup intent pending and keeps reporting it. `AbandonAfterGrace` lets an administrator stop waiting after `abandonmentGraceSeconds`, without ever claiming the remote workload was removed. |
| `abandonmentGraceSeconds` | `3600` | The grace period `AbandonAfterGrace` refers to. |
| `removeConfigurationAfterAck` | `true` | Whether the materialized configuration is deleted once cleanup is acknowledged. |

### `spec.policy`

| Field | Default | Meaning |
| --- | --- | --- |
| `allowPrivileged` | `false` | Permit `securityContext.privileged` in the workload. A static Pod is started by kubelet straight from a file, so nothing else reviews it. |
| `allowInlineSecretEnv` | `false` | Permit Secret material to be written into the static Pod manifest as an environment value. See [Secret handling](./ref-translation.md#secrets). |

### `spec.installer`

| Field | Default | Meaning |
| --- | --- | --- |
| `image` | — required | The installer image. Not the target application. |
| `imagePullPolicy` | `IfNotPresent` | Pull policy for the installer container. |
| `staticPodPath` | `/etc/kubernetes/manifests` | kubelet's `staticPodPath`. Must match the node's kubelet configuration; the operator never edits kubelet configuration. |
| `bundleDir` | `/var/lib/static-sets` | The persistent directory the installer owns. Must be disjoint from `staticPodPath`, because kubelet watches that directory and would try to start a temporary file. |
| `resources` | unset | Overrides the installer's small fixed reservation. See [capacity](./about-placement-and-capacity.md#resource-accounting-is-a-deliberate-trade-off). |

## `status`

| Field | Meaning |
| --- | --- |
| `observedGeneration` | The `metadata.generation` this status describes. |
| `desiredRevision` | The newest revision any placement reports as installed. |
| `placements[]` | What each installer **did**: `identity`, `node`, `revision`, `phase` (`Pending`, `Installed`, `Cleaning`, `Cleaned`, `Failed`), `acknowledged`, `generation`, `message`. |
| `health[]` | What was **observed**: `identity`, `node`, `observed`, `phase`, `ready`, `restartCount`, `message`, derived from the mirror Pods. |
| `cleanup[]` | Each cleanup intent: `identity`, `node`, `revision`, `generation`, `reason`, `state` (`Requested`, `Queued`, `Acknowledged`, `Blocked`, `Abandoned`), `acknowledged`, `message`. |
| `conditions[]` | Standard Kubernetes conditions, listed below. |

Installed state is deliberately separate from observed health. A placement can be
`Installed` and unhealthy, or `Installed` and unobservable because its node is gone;
neither is a reason to reinstall.

### Conditions

| Type | Status | Reason | Meaning |
| --- | --- | --- | --- |
| `Synchronized` | `True` when the operator's view of the cluster is complete; `False` with reason `Synchronizing` before that. | `Synchronized`, `Synchronizing` | Until this is `True` the operator performs no destructive action. |
| `Ready` | `True` when every desired placement is installed and acknowledged. | `AllPlacementsInstalled`, `PlacementsPending`, `Deleting` | |
| `CleanupPending` | `True` while any cleanup intent is unacknowledged. | `CleanupPending`, `CleanupComplete` | |
| `InstallersRunning` | `True` while at least one installer serves a placement. | `InstallersLive`, `NoInstallers` | |
| `Degraded` | `True` when a placement reported a failure. | `PlacementFailed` | |
| `Deleting` | `True` while a deleted StaticSet is cleaning up. | `ResourceDeleted` | |
| `CleanedUp` | `True` once every placement acknowledged removal. The finalizer is released at that point. | `CleanupAcknowledged`, `CleanupPending` | |

## Example

See [`deploy/samples/`](https://github.com/cloudlane-one/static-sets/tree/main/deploy/samples):
`deployment-mode.yaml`, `daemonset-mode.yaml`, `secret-volume.yaml` and the
`configmap.yaml` they reference. The sample set is validated by the test suite, so it
always describes objects the operator accepts.