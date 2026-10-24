# Deferred features and known limitations

This is a working prototype, not a finished operator. This page lists what is **not**
implemented, so that nothing has to be discovered by reading the source.

## Not verified by automation

| Item | Status |
| --- | --- |
| **Reboot recovery** | Not verified in CI. The end-to-end suite proves the operator compiles the workload, materializes its configuration, writes the manifest to the configured directory, acknowledges it, and removes it again on cleanup, against a real cluster. Whether a rebooted kubelet then *starts* that manifest is verified by the manual procedure in [How to verify the disconnected reboot guarantee](./how-to-verify-disconnected-reboot.md). No automated test in this repository has observed a host reboot. |
| **Air-gapped image preparation** | `spec.offline.imageProvisioning: VerifiedBeforeInstall` refuses to publish a revision until a checker confirms the images are present, but no checker is shipped: `reconcile(..., image_checker=...)` is a seam, and the manager supplies none. The default policy (`ExternallyProvisioned`) reports that image availability is somebody else's responsibility. |
| **Real registry / disconnected registry behaviour** | Not exercised. Pull policies are validated (`Always` is refused unless the policy accepts it) but no test pulls from a registry or fails to. |

## Accepted but not acted on

| Field | Status |
| --- | --- |
| `spec.update.gracePeriodSeconds` | Validated, carried, and **not used to bound retirement**. A placement is retired when its node's manager removes the manifest, not when a timer expires. |

## Deliberately not supported

These are design decisions with reasons, not gaps; they are listed here so the
trade-off is visible rather than surprising. Each is also in the
[translation reference](./ref-translation.md).

- **Secret to environment, offline.** Refused unless `spec.policy.allowInlineSecretEnv`
  is set. The only offline ways to satisfy the request both disclose the value — write
  it into the manifest, where the mirror Pod exposes it, or lose it after the reboot —
  so the operator refuses and offers the Secret-as-a-volume shape instead.
- **Pod affinity and anti-affinity.** They describe co-location with other Pods, which
  a static Pod cannot honour, because kubelet does not run the scheduler for it.
- **`hostPath` volumes from the template.** The only `hostPath` mounts in an emitted
  manifest are the revision directories the operator materialized.
- **Any API-backed volume.** PVCs, projected volumes, downward API, NFS: all need a
  source that will not exist offline.
- **Service accounts and image pull secrets.** Both are fetched from the API server.
- **A wide slice of the Pod API** that has no representation here: `lifecycle`,
  `terminationMessagePath`/`Policy`, `hostAliases` sub-cases, `topologySpreadConstraints`
  in the workload, `priority`, `overhead`, `readinessGates`, `resourceClaims`, and
  anything else the models do not name. They are refused, not pruned — unless the
  admission webhook is disabled, in which case the API server prunes them first; see
  [the translation reference](./ref-translation.md#the-admission-caveat).

## Known sharp edges

| Item | Consequence |
| --- | --- |
| **The manager runs as root.** kubelet's manifest directory is root-owned, so writing there needs it. Pod Security Admission in a `restricted` namespace will refuse the manager Pod; install the chart into a namespace with a policy that permits it, or use a node-local kubelet configuration that lets a different user write the path. |
| **Static Pods bypass admission entirely.** A manifest file is not an API object. `spec.policy.allowPrivileged` and the webhook are the only guards this operator offers; nothing else in the cluster will see the workload. |
| **Capacity is checked once, at placement time.** The scheduling probe declares the workload's requests, so the node the scheduler picks can fit it — but once the probe is deleted, the running static Pod is invisible to the scheduler. A workload that outgrows its declared requests is neither rescheduled nor evicted. See [capacity](./about-placement-and-capacity.md#resource-accounting-is-a-deliberate-trade-off). |
| **Fencing is eventually consistent.** A node keeps running its workload until its own manager removes the manifest. An unreachable node keeps it indefinitely unless `AbandonAfterGrace` is set, and even then the operator only stops tracking it. |
| **One manager per node, one account for the cluster.** The manager reads every StaticSet and every ConfigMap and Secret those StaticSets reference, in every namespace. Narrowing that would mean one DaemonSet per namespace, each mounting the same host paths but seeing only its own namespace's placements. |
| **Placement is one pass per generation.** A new node does not receive a DaemonSet-mode placement until the operator notices it is eligible, and a placement is not moved because a *better* node appeared: the scheduler is asked again on the next generation, not continuously. A probe pass holds a real reservation for the workload, so running it constantly would cost more than it bought. |
| **Drift is repaired on an interval, not immediately.** The manager rewrites a manifest that no longer matches the revision it rendered, and it runs a periodic pass per placement (30 seconds by default), so a manifest edited on the node is corrected on the next pass rather than the instant it changes. There is no watch on ConfigMaps or Secrets, and no alert: the repair is visible only as the file changing back. |
| **The probe's runtime class is a cluster prerequisite.** The probe needs a `RuntimeClass` whose handler no CRI implements; the chart installs one, and a cluster that manages runtime classes itself must set `probe.createRuntimeClass=false` and provide it. |
| **A placement on a lost node outlives it.** Its record is reported as blocked, not deleted: the operator stops tracking it only under `AbandonAfterGrace`. Deleting the record by hand is safe — its finalizer belongs to a manager that is not there — but the residual manifest on that host becomes untracked. |

## Suggested next steps

In rough order of value:

1. **A dedicated acknowledgment resource** (or a lease) instead of a ConfigMap, so a
   placement's state has a schema and a lifecycle of its own.
2. **A real image checker** for `VerifiedBeforeInstall`, driven by the container
   runtime's own inventory.
3. **Honour `gracePeriodSeconds`** so a retired placement has a bounded window to
   confirm termination before it is reported as blocked.
4. **Automated reboot verification**, most plausibly a small VM-based job that boots a
   node image with the manifest already on it and asserts that the containers came up
   with no API server listening.
5. **Cleanup of orphaned placement records** once a StaticSet's UID is known to be
   gone.