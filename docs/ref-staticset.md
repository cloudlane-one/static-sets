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
| `mode` | `Deployment` \| `DaemonSet` | — | How many placements the scheduler is asked for, and by what kind of probe. |
| `replicas` | integer 0–64 | — | Desired placements. Required for `Deployment`, invalid for `DaemonSet`. |
| `template` | `PodTemplateSpec` | — | The workload. Restricted to the [supported subset](./ref-translation.md). |
| `placement` | `Placement` | `{}` | Scheduler placement for the *scheduling probe*, and therefore for the workload. |
| `update` | `UpdatePolicy` | see below | How a new revision replaces an old placement. |
| `offline` | `OfflinePolicy` | see below | What must hold before a revision may be published. |
| `cleanup` | `CleanupPolicy` | see below | How unwanted placements are removed. |
| `policy` | `WorkloadPolicy` | see below | Guards on the static workload itself. |

There is no `installer` field: where a static Pod is written is a property of the node,
not of a StaticSet. The chart configures the manager once per node
(`manager.staticPodPath`, `manager.bundleDir`), which is what stops a StaticSet from
widening the host access of the process that writes it.

### `spec.placement`

Steers the Kubernetes scheduler, which places the scheduling probe's Pods. Copied onto
the probe, never onto the emitted static Pod; see
[placement and capacity](./about-placement-and-capacity.md).

| Field | Meaning |
| --- | --- |
| `nodeSelector` | Label equality requirements. |
| `affinity.nodeAffinity.requiredDuringSchedulingIgnoredDuringExecution` | Required node selector terms (`matchExpressions`, `matchFields`). |
| `affinity.nodeAffinity.preferredDuringSchedulingIgnoredDuringExecution` | Weighted preferred terms. |
| `tolerations` | Tolerations the workload's placement requires. |
| `topologySpreadConstraints` | The upstream `V1TopologySpreadConstraint`: `maxSkew`, `topologyKey`, `whenUnsatisfiable` (default `DoNotSchedule`), `labelSelector`, `minDomains`, and the pass-through `matchLabelKeys`/`nodeAffinityPolicy`/`nodeTaintsPolicy`. |

The field types are the generated Kubernetes client models, so the CRD documents the
scheduler API as Kubernetes defines it. The operator still refuses what it cannot act
on, with the field named: pod affinity and anti-affinity, node-selector and
label-selector operators outside the set it evaluates, toleration operators/effects
outside the API server's set, and a preferred-term weight outside 1–100.

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

### `spec.template.spec.runtimeClassName`

Optional, and passed through to the static Pod's manifest. It is the one
API-adjacent Pod field a static Pod can keep, because kubelet resolves it through its
own cache rather than through a scheduler. Note the cost: a `RuntimeClass` is an API
object, so a workload that declares one is not guaranteed to start on a reboot that
happens while the API server is unreachable. Leave it unset for a workload whose point
is to come back disconnected. The value must fit a label value (≤63 characters), because
it is also carried on the scheduling probe's
`staticsets.cloudlane.dev/runtime-class` label — the probe itself must run under a
placeholder class that no node can start.

## `status`

| Field | Meaning |
| --- | --- |
| `observedGeneration` | The `metadata.generation` this status describes. |
| `placedGeneration` | The generation a complete scheduling pass has been carried out for. A pass is only started when this lags behind, when a placement is missing, or when a new eligible node appears. |
| `desiredRevision` | The revision every installed placement agrees on, or unset while a rollout has two revisions in flight — revisions are content hashes, so there is no honest "newest" to report. |
| `placements[]` | What each node's manager **did**: `identity`, `node`, `revision`, `phase` (`Pending`, `Installing`, `Installed`, `Retiring`, `Cleaning`, `Cleaned`, `Failed`), `acknowledged`, `generation`, `message`. |
| `health[]` | What was **observed**: `identity`, `node`, `observed`, `phase`, `ready`, `restartCount`, `message`, derived from the mirror Pods. |
| `cleanup[]` | Each cleanup intent: `identity`, `node`, `revision`, `generation`, `reason`, `state` (`Requested`, `Acknowledged`, `Blocked`, `Abandoned`), `acknowledged`, `message`. |
| `conditions[]` | Standard Kubernetes conditions, listed below. |

Installed state is deliberately separate from observed health. A placement can be
`Installed` and unhealthy, or `Installed` and unobservable because its node is gone;
neither is a reason to reinstall.

### Conditions

| Type | Status | Reason | Meaning |
| --- | --- | --- | --- |
| `Synchronized` | `True` when the operator's view of the cluster is complete; `False` with reason `Synchronizing` before that. | `Synchronized`, `Synchronizing` | Until this is `True` the operator performs no destructive action. |
| `Ready` | `True` when every desired placement is installed, acknowledged and mirrored. | `AllPlacementsInstalled`, `PlacementsPending`, `Deleting` | |
| `CleanupPending` | `True` while any cleanup intent is unacknowledged. | `CleanupPending`, `CleanupComplete` | |
| `Scheduling` | `True` while a scheduling pass is in progress. | `ProbeCreated`, `ProbePending`, `Unschedulable`, `Installing`, `RetiringPreviousPlacement`, `Placed` | |
| `Degraded` | `True` when a placement reported a failure. | `PlacementFailed` | |
| `Deleting` | `True` while a deleted StaticSet is cleaning up. | `ResourceDeleted` | |
| `CleanedUp` | `True` once every placement acknowledged removal. The finalizer is released at that point. | `CleanupAcknowledged`, `CleanupPending` | |

## Example

See [`deploy/samples/`](https://github.com/cloudlane-one/static-sets/tree/main/deploy/samples):
`deployment-mode.yaml`, `daemonset-mode.yaml`, `secret-volume.yaml` and the
`configmap.yaml` they reference. The sample set is validated by the test suite, so it
always describes objects the operator accepts.