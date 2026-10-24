# Placement and capacity

A static Pod is never scheduled. kubelet starts the file it finds, on the node whose
kubelet finds it — so "where should this run" is a question the cluster's scheduler
never gets asked on the workload's behalf. This page describes how the operator asks
it anyway, and what that costs.

## The scheduling probe

For every StaticSet, per generation, the controller creates a **scheduling probe**: an
ordinary ReplicaSet (in `Deployment` mode) or DaemonSet (in `DaemonSet` mode) whose Pod
template is the workload's *scheduling-relevant* shape and nothing else:

- every container is replaced by an inert image, and carries only the workload's
  `resources` and its published `hostPort`s;
- init containers are mirrored the same way, because their requests affect scheduling;
- `hostNetwork` is copied, because two host-network Pods conflict on a port;
- `spec.placement` — node selector, node affinity, tolerations, topology spread
  constraints — is copied onto the probe's Pods;
- the template's labels are copied, so a spread constraint that selects them matches.

What the probe does **not** carry is anything that could execute: no command, no
arguments, no environment, no volumes, no probes. And it runs under a `RuntimeClass`
whose handler no container runtime implements, so kubelet binds the Pod, asks the CRI
for that handler, fails, and never starts a container.

The result is a real scheduling decision attached to a Pod that never runs:

```
spec.placement + workload resources  ->  probe Pods  ->  scheduler picks nodes
                                                       ->  each node's manager writes
                                                           the static Pod there
```

The controller deletes the probe as soon as every chosen node has installed its static
Pod *and* kubelet has published the mirror Pod for it. The reservation is then
released, and the decision lives on in the placement records.

## Why not let the manager pick nodes

Because "the first node that happens to run a manager" is not a placement policy.
Concentrating placement in the Kubernetes scheduler is the whole point: node
selectors, affinity, taints, spread constraints, preemption and capacity accounting are
implemented there, by people who have seen more of them than an operator will.

The manager's job is deliberately dull: it is told which node hosts which revision, and
it writes, updates or removes that manifest. It never decides.

## Resource accounting is a deliberate trade-off

The probe declares the **workload's** requests, not a token reservation. That is what
makes the scheduler's answer meaningful: a node is chosen because it can actually fit
what will run there.

It does mean the workload is counted **twice while the probe exists** — once by the
probe, once by the static Pod kubelet is already running. The overlap is bounded by
one installation: the controller deletes the probe as soon as the static Pod is
installed and mirrored. After that, the scheduler does not see the workload's usage at
all.

That second half is not a bug in the probe; it is the nature of a static Pod. Nothing
outside kubelet accounts for it. The operator's position:

- capacity is checked **at placement time**, with the real requests, which is the
  moment a wrong answer is still recoverable;
- a static Pod that grows past its declared requests is invisible to the scheduler,
  exactly like a `systemd` unit;
- if placement must respect capacity *continuously*, express it as policy — pin the
  StaticSet to a node class with `spec.placement.nodeSelector` or node affinity.

## Placement identity is per node

A placement is identified by the owning StaticSet's UID and the node name — never by
the manager Pod that happens to serve it:

- when a manager is replaced, its successor computes the *same* identity, adopts the
  same placement directory, record and revisions, and re-acknowledges them. Nothing is
  orphaned and nothing is duplicated;
- the static Pod's name carries the revision: `<set>-<identity>-<revision>`, so two
  revisions of one placement are two different Pods, which is what makes an overlap
  expressible at all;
- a stale manager that comes back to life fails its ownership check and cannot delete
  its successor's workload.

## Update strategies

`spec.update.strategy` decides how a new revision replaces the one a placement carries.

| Strategy | Behaviour | Use when |
| --- | --- | --- |
| `Recreate` (default) | The old static Pod is removed before the new one is written. | Always, unless you have a specific reason not to. At most one revision exists on a node at any moment, and the workload is down for the length of one installation. |
| `Surge` | The new static Pod is written first and the old one is removed once the new one is observed running. | You would rather have two revisions briefly than a gap. `spec.update.maxSurge` bounds how many placements may overlap. |

`Recreate` combined with `maxSurge` is rejected, because the two contradict each other.

A rollout also re-asks the scheduler: a new generation means a new probe workload, so a
placement may move to a different node — that is a *relocation*, and the operator
records it as such. Under `Recreate` the old probe is deleted before the new one is
created, because it holds the capacity the new pass needs; under `Surge` both exist
until the new placement is installed.

If the new revision never comes up under `Surge`, the previous one is **not** retired:
the placement reports that it is still installed, and the old static Pod keeps running.
Two revisions on one node is the price of no gap, and it is reported rather than hidden.
A third revision arriving while that is still true retires the oldest, because three
revisions on one node is worse than the gap.

## Availability versus fencing

Fencing — guaranteeing that a node cannot keep running a workload you have retired —
takes time, because it depends on that node doing something (removing a file). Any
system that claims instant fencing is either lying or has the API server still
attached, which is exactly what a static Pod is for.

The operator's choice is to fence conservatively and report honestly:

- nothing is removed until the node's own manager acknowledges it;
- a node that cannot be reached keeps its pending cleanup and is reported as such;
- the only way to stop waiting is an explicit administrative policy
  (`spec.cleanup.onNodeUnreachable: AbandonAfterGrace`), which still never claims the
  remote workload was removed — only that the operator stopped tracking it.
