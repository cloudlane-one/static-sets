# Installation: what ends up on a node

Everything the installer writes lives beneath two directories it was told about:
kubelet's static Pod path and the persistent bundle directory. This page documents the
layout, the ownership protocol that protects it, and the cleanup protocol that removes
it.

## Layout

```
/etc/kubernetes/manifests/            kubelet watches this
  web-e6383089b2.yaml                 the active manifest, named after the static Pod

/var/lib/static-sets/                 the bundle the installer owns
  placements/
    e6383089b2/                       one directory per placement identity
      ownership.json                  who owns it, which revision, which generation
      cleanup-intent.json             present only while a cleanup is outstanding
      revisions/
        r-8a5ca1.../                  immutable, revision-named
          manifest.yaml               the exact manifest this revision rendered
          config/<volume>/…           materialized ConfigMap files
          secrets/<volume>/…          materialized Secret files, 0400 by default
```

Two invariants hold throughout:

- **the manifest file name equals the static Pod's name.** kubelet derives a static
  Pod's name from the file it read, so keeping the two identical is the convention
  every release accepts;
- **temporary files, backups and configuration live outside kubelet's watched
  directory.** Writing a partially written file there would make kubelet try to start
  it.

## Placement identity

```
identity = sha256(resource_uid + "\0" + node)[:10]
```

The identity is derived from the owning StaticSet's UID and the **node**, never from the
installer Pod. Consequences:

- a replacement installer computes the same identity, so it adopts the same revision
  directory, the same manifest and the same record instead of orphaning them;
- several installer replicas scheduled onto one node converge on one placement instead
  of writing two conflicting manifests;
- the static Pod's name is `<StaticSet name>-<identity>`, so names and paths cannot
  collide between placements, and a placement can never collide with its own successor.

## The ownership record

`placements/<identity>/ownership.json` is the authority for every destructive
operation:

```json
{
  "placement": "e6383089b2",
  "pod_name": "web-e6383089b2",
  "static_set": "web",
  "static_set_uid": "0f9a4b6c-…",
  "namespace": "static-sets",
  "node": "worker-1",
  "generation": 3,
  "revision": "r-8a5ca1…",
  "history": ["r-8a5ca1…", "r-cf722c…"],
  "state": "Installed",
  "owner": "web-installer-6d9f…",
  "updated_at": "2026-10-12T10:00:00+00:00"
}
```

- it is written **before** anything else, so files left by an interrupted installation
  are still discoverable and attributable;
- `owner` is the installer Pod's name, the *instance* identity. A stale installer that
  comes back finds a different owner and refuses to act, which is what stops it
  deleting its successor's workload;
- `history` is the ordering of revisions. Revision tokens are content hashes, so their
  *names* carry no ordering; the record is the only authority for which revision is
  newer, and retention prunes against it;
- `generation` guards handover: a successor may adopt a placement recorded for an older
  or equal generation, but a record from a *newer* generation is authoritative and is
  never overwritten, so a resurrected older installer cannot reclaim a placement that
  has moved on.

## Installing a revision

The order is chosen so that an interruption at any point leaves something consistent:

1. the placement directory is created with mode `0700`;
2. the ownership record is persisted;
3. the revision's configuration files are written into an immutable, revision-named
   directory, each with its declared mode, and each flushed with `fsync`;
4. the rendered manifest is written into the revision directory as well, so the exact
   content that produced the revision survives independently of kubelet's directory;
5. the manifest is written to a temporary file next to kubelet's path, `fsync`-ed, and
   `rename`d onto the final name — kubelet sees either the old revision or the new one,
   never a half-written file;
6. the record is updated to name the active revision;
7. obsolete revisions are pruned against `history`, keeping
   `spec.offline.retainedRevisions` of them, and never the active one.

An error anywhere before step 5 leaves the previously active revision intact, which is
the "invalid updates must leave the active revision intact" rule.

## Path and permission safety

- every path the installer builds is validated as being inside the directory it was
  given: absolute paths, `..` segments and escaping item paths are refused;
- symbolic links are refused at every component on the way to a file it is about to
  write, and inside any tree it is about to remove;
- files are created with an explicit mode, never the process umask; the bundle is
  `0700`, records are `0600`, Secret files are `0400` by default;
- the installer only removes directories strictly inside the placement directory it
  owns. It never removes a bundle root.

## Cleanup

A finalizer is bookkeeping, not cleanup: it holds the StaticSet object while the work
is done, and it never performs it. Installer termination does not remove a static Pod
either — the workload is meant to outlive the process that installed it.

When a placement is no longer wanted — scale-down, deselection, relocation, or deletion
of the StaticSet — the controller:

1. **persists an authoritative intent** naming the exact placement, in a ConfigMap in
   the StaticSet's namespace. The controller has no host access, so the intent travels
   through the API;
2. **requests cleanup from the installer** that still serves that node, if there is
   one. It learns of the intent on its next reconcile, writes it durably to
   `cleanup-intent.json` *before* acting, and carries it out;
3. **otherwise creates a restricted cleanup worker** — a bare Pod pinned to the
   recorded node with `nodeName`, mounting only the same two directories, running the
   same command with the `cleanup` action, carrying its own finalizer;
4. **verifies ownership and generation** before touching anything: an intent whose
   StaticSet UID or generation does not match the local record is refused;
5. **removes the manifest durably** (unlink plus a directory `fsync`) and, once the
   workload's termination is confirmed and `removeConfigurationAfterAck` allows it,
   removes the revision directories;
6. **records the acknowledgment** in the ownership record and in the placement object,
   frees the worker's own finalizer, and only then does the controller release the
   StaticSet's finalizer.

The whole protocol is **idempotent**: removing an already-absent manifest is not an
error, the intent is simply re-persisted, and a placement with no local record at all
acknowledges immediately.

### When a node cannot be reached

A placement on a node whose installer is gone *and* which does not report `Ready` is
reported as `Blocked`, not chased: a cleanup worker sent there could not run either.
The intent stays pending, the message says so, and `spec.cleanup.onNodeUnreachable`
decides what happens next:

- `Retain` (default) keeps waiting and keeps reporting. The StaticSet is not released;
- `AbandonAfterGrace` lets an administrator stop waiting after
  `abandonmentGraceSeconds`. The record is then marked abandoned, the operator stops
  tracking it, and it **never claims the remote workload was removed** — a residual
  static Pod may still be started by that node's kubelet. Removing it is a manual
  operation on that node.

## Security notes

**Host access.** An installer mounts exactly two host paths, both read-write:
kubelet's static Pod directory, and the bundle. It runs as root because kubelet's
manifest directory is root-owned. It has all capabilities dropped, no privilege
escalation, a read-only root filesystem, no host networking or PID namespace, and no
other mount. Restricting further would mean either a different kubelet configuration or
accepting that installation cannot happen.

**A static Pod bypasses admission.** A manifest file is not an API object: no admission
controller, no Pod Security Admission, no policy engine sees it. That is why
`spec.policy.allowPrivileged` defaults to refusing a privileged workload, and why the
`hostPath` volume source is refused in templates: those are exactly the cases where
normal Pod admission would have been the only guard.

**Secrets at rest.** Secret material is written to the node's filesystem in plaintext,
mode `0400` under `0700` directories, and mounted read-only into the workload. It is
never written into the manifest, into status, or into logs. Anyone who can read the
bundle directory as root can read it; node encryption, and the choice of `bundleDir`
on a suitably protected filesystem, remain the operator's responsibility.

**Least privilege.** The installer's ServiceAccount may read the StaticSet and its
configuration, write its own acknowledgment objects, and release its own finalizer —
in one namespace. It is **one account, shared by every installer in that namespace**:
an account per StaticSet would mean the operator holding permission to create
ServiceAccounts, Roles and RoleBindings, which is a privilege it does not ask for. An
administrator who needs that isolation can point the chart's
`installer.serviceAccountName` at an account they manage themselves. The controller's
account is wider (it creates the installer workload, reads nodes, observes mirror
Pods) but still namespaced, except for two cluster-scoped reads: `nodes`, for placement
and reachability, and `namespaces`, which kopf itself requires because it manages one
watch per namespace. Neither account may touch the other's workloads.