# Installation: what ends up on a node

Everything the node manager writes lives beneath two directories it was told about at
start-up: kubelet's static Pod path and the persistent bundle directory. This page
documents the layout, the two records that describe it, the ownership protocol that
protects it, and the cleanup protocol that removes it.

The manager is configured by the chart, once per node — `manager.staticPodPath` and
`manager.bundleDir`. A StaticSet does not declare either: a StaticSet cannot widen the
host access of the process that writes it.

## Layout

```
/etc/kubernetes/manifests/                  kubelet watches this
  web-e6383089b2-r-8a5ca1….yaml             one manifest per placement and revision

/var/lib/static-sets/                       the bundle the manager owns
  placements/
    e6383089b2/                             one directory per placement identity
      ownership.json                        who owns it, which revision, which generation
      cleanup-intent.json                   present only while a cleanup is outstanding
      revisions/
        r-8a5ca1…/                           immutable, revision-named
          manifest.yaml                     the exact manifest this revision rendered
          config/<volume>/…                 materialized ConfigMap files
          secrets/<volume>/…                materialized Secret files, 0400 by default
```

Two invariants hold throughout:

- **the manifest file name equals the static Pod's name.** kubelet derives a static
  Pod's name from the file it read, so keeping the two identical is the convention
  every release accepts. The name carries the revision, which is what lets a `Surge`
  rollout leave the previous revision's file in place until its successor runs;
- **temporary files, backups and configuration live outside kubelet's watched
  directory.** Writing a partially written file there would make kubelet try to start
  it.

## The placement record

`StaticPodPlacement` is a namespaced custom resource, one per node and StaticSet, named
`<StaticSet>-<identity>` in the StaticSet's namespace and owned by it.

```yaml
apiVersion: staticsets.cloudlane.dev/v1
kind: StaticPodPlacement
metadata:
  name: web-e6383089b2
  namespace: static-pods
spec:                       # the controller decides this
  staticSet: web
  staticSetUid: 0f9a4b6c-…
  node: worker-1
  generation: 3
  desired: true
status:                     # the node's manager reports this
  identity: e6383089b2
  revision: r-8a5ca1…
  staticPodName: web-e6383089b2-r-8a5ca1…
  phase: Installed
  acknowledged: true
  observedGeneration: 3
  message: installed r-8a5ca1… (updated)
```

The split matters:

- the **controller** writes the spec: which node the scheduler chose, for which
  generation, and whether the placement is still wanted. Those are the things only the
  control plane knows;
- the **node's manager** writes the status: which revision is installed, whether the
  write is durable, and why it failed. A per-node manager writing into a shared status
  would fight the controller for the same field; one object per manager is what an
  optimistic-concurrency write is designed for;
- `spec.desired: false` **is** the cleanup intent. There is no separate message: the
  record stays until the manager reports the static Pod gone, and only then does the
  controller delete it.

Placement records are also why a `StaticSet`'s status can be honest about *where* its
workloads are: the operator's view of a placement is a record the node itself filled
in, not an inference from a Pod name.

## Placement identity

```
identity = sha256(resource_uid + "\0" + node)[:10]
```

The identity is derived from the owning StaticSet's UID and the **node**, never from
the manager Pod. Consequences:

- a replacement manager computes the same identity, so it adopts the same revision
  directory, the same manifest and the same record instead of orphaning them;
- two managers can never converge on one placement with different content, because
  only one of them runs on that node;
- the static Pod's name is `<StaticSet name>-<identity>-<revision>`, so names and paths
  cannot collide between placements, and a placement can never collide with its own
  successor.

## The ownership record

`placements/<identity>/ownership.json` is the authority for every destructive
operation:

```json
{
  "placement": "e6383089b2",
  "pod_name": "web-e6383089b2-r-8a5ca1…",
  "static_set": "web",
  "static_set_uid": "0f9a4b6c-…",
  "namespace": "static-pods",
  "node": "worker-1",
  "generation": 3,
  "revision": "r-8a5ca1…",
  "retiring": "r-cf722c…",
  "history": ["r-8a5ca1…", "r-cf722c…"],
  "state": "Installed",
  "owner": "manager-worker-1-6d9f…",
  "updated_at": "2026-10-12T10:00:00+00:00"
}
```

- it is written **before** anything else, so files left by an interrupted installation
  are still discoverable and attributable;
- `owner` is the manager Pod's name, the *instance* identity. A manager that was
  replaced finds a different owner and refuses to act, which is what stops it deleting
  its successor's workload;
- `history` is the ordering of revisions. Revision tokens are content hashes, so their
  *names* carry no ordering; the record is the only authority for which revision is
  newer, and retention prunes against it;
- `retiring` names the revision whose manifest is still installed while a newer one
  proves it can run. It is set only between a `Surge` installation and the retirement
  that follows it, and it is cleared as soon as the file is removed;
- `generation` guards handover: a successor may adopt a placement recorded for an older
  or equal generation, but a record from a *newer* generation is authoritative and is
  never overwritten, so a resurrected older manager cannot reclaim a placement that has
  moved on.

## The manager keeps reconciling

The manager is a kopf application. It is *told* when a placement changes — that is what
the watch on `StaticPodPlacement` is for — and it also runs a periodic pass per
placement, because the other half of the desired state is a ConfigMap or a Secret it
does not watch. One pass installs and acknowledges; the next ones notice
configuration that changed, a revision that moved, and a cleanup that was requested
after the previous pass.

The pass never removes anything the API has not asked it to. If the API becomes
unreachable it reports that it is offline, changes nothing, and keeps the last
committed revision running until the API returns — never treating an unreachable API
as an empty desired state.

## Installing a revision

The order is chosen so that an interruption at any point leaves something consistent:

1. the placement directory is created with mode `0700`;
2. the ownership record is persisted;
3. the revision's configuration files are written into an immutable, revision-named
   directory, each with its declared mode, and each flushed with `fsync`;
4. the rendered manifest is written into the revision directory as well, so the exact
   content that produced the revision survives independently of kubelet's directory;
5. under `Recreate`, the previous revision's manifest is removed first; under `Surge`,
   the new manifest is written while the old one is still in place;
6. the new manifest is written to a temporary file next to kubelet's path, `fsync`-ed,
   and `rename`d onto its final name — kubelet sees either the old revision or the new
   one, never a half-written file;
7. the record is updated to name the active revision, and the previous one as
   `retiring` under `Surge`;
8. under `Surge`, once kubelet reports the new static Pod running, the retiring
   revision's manifest is removed and `retiring` is cleared;
9. obsolete revisions are pruned against `history`, keeping
   `spec.offline.retainedRevisions` of them, and never the active one.

An error anywhere before step 6 leaves the previously active revision intact, which is
the "invalid updates must leave the active revision intact" rule.

## Path and permission safety

- every path the manager builds is validated as being inside the directory it was
  given: absolute paths, `..` segments and escaping item paths are refused;
- symbolic links are refused at every component on the way to a file it is about to
  write, and inside any tree it is about to remove;
- files are created with an explicit mode, never the process umask; the bundle is
  `0700`, records are `0600`, Secret files are `0400` by default;
- the manager only removes directories strictly inside the placement directory it
  owns. It never removes a bundle root.

## Cleanup

A finalizer is bookkeeping, not cleanup: it holds the object while the work is done,
and it never performs it. Manager termination does not remove a static Pod either —
the workload is meant to outlive the process that installed it.

When a placement is no longer wanted — scale-down, deselection, relocation, or
deletion of the StaticSet — the controller:

1. **sets `spec.desired: false`** on the placement record, with a reason. The
   controller has no host access, so the intent travels through the API;
2. **waits for the node's manager**, which writes the intent durably to
   `cleanup-intent.json` *before* acting, removes the manifest durably (unlink plus a
   directory `fsync`), and only deletes the revision directories once the static Pod is
   confirmed stopped and `removeConfigurationAfterAck` allows it;
3. **reports the phase as `Cleaning` until then**, and only `Cleaned` once the manager
   says the Pod is gone;
4. **deletes the record and releases the StaticSet's finalizer** only after that.

There is no cleanup worker Pod any more: the manager already runs on every node, so the
node that has to do the work is the node whose manager is doing it.

The whole protocol is **idempotent**: removing an already-absent manifest is not an
error, the intent is simply re-persisted, and a placement with no local record at all
acknowledges immediately.

### When a node cannot be reached

A placement on a node that does not report `Ready` has no manager to do the work, so it
is reported as `Blocked`, not chased. The intent stays pending, the message says so,
and `spec.cleanup.onNodeUnreachable` decides what happens next:

- `Retain` (default) keeps waiting and keeps reporting. The StaticSet is not released;
- `AbandonAfterGrace` lets an administrator stop waiting after
  `abandonmentGraceSeconds`. The record is then marked abandoned, the operator stops
  tracking it, and it **never claims the remote workload was removed** — a residual
  static Pod may still be started by that node's kubelet. Removing it is a manual
  operation on that node.

## Security notes

**Host access.** The manager Pod mounts exactly two host paths, both read-write:
kubelet's static Pod directory, and the bundle. It runs as root because kubelet's
manifest directory is root-owned. It has all capabilities dropped, no privilege
escalation, a read-only root filesystem, no host networking or PID namespace, and no
other mount. Restricting further would mean either a different kubelet configuration or
accepting that installation cannot happen.

**One manager, every namespace.** The manager serves StaticSets from any namespace,
because a placement names a node rather than a namespace. It holds one ServiceAccount
for the whole cluster, and that account may read StaticSets and the configuration they
reference, read the Pods it needs to observe, and write the *status* of the placements
that name its node. It cannot create a placement, cannot move one, and cannot delete
one: where a workload goes is not its decision.

**A static Pod bypasses admission.** A manifest file is not an API object: no admission
controller, no Pod Security Admission, no policy engine sees it. That is why
`spec.policy.allowPrivileged` defaults to refusing a privileged workload, and why the
`hostPath` volume source is refused in templates: those are exactly the cases where
normal Pod admission would have been the only guard.

**The probe is inert by construction.** It names a `RuntimeClass` whose handler no
container runtime implements, and it carries none of the workload's command, arguments,
environment or volumes. Even if a node were misconfigured into starting a probe Pod, it
would run a pause image and nothing else.

**Secrets at rest.** Secret material is written to the node's filesystem in plaintext,
mode `0400` under `0700` directories, and mounted read-only into the workload. It is
never written into the manifest, into status, or into logs. Anyone who can read the
bundle directory as root can read it; node encryption, and the choice of `bundleDir`
on a suitably protected filesystem, remain the operator's responsibility.
