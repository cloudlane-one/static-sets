# Placement and capacity

Two different schedulers are involved in a StaticSet, and confusing them is the
easiest way to end up with a workload on the wrong node or a node that cannot fit it.

## The installer is scheduled; the workload is not

`spec.placement` — node selector, node affinity, tolerations, topology spread
constraints — steers the **Kubernetes scheduler**, which places *installer* Pods.

The emitted static Pod carries none of those fields. kubelet does not run the
scheduler for a static Pod: it starts the file it finds, on the node whose kubelet
finds it. Placement fields in the workload would be inert, so they are rejected in
`spec.template.spec` and only accepted under `spec.placement`.

The practical consequence: **you choose nodes by choosing where installers land.**
In `Deployment` mode the scheduler decides, subject to `spec.placement`. In
`DaemonSet` mode the DaemonSet controller puts one installer on every node the
selector admits.

## Placement identity is per node

A placement is identified by the owning StaticSet's UID and the node name — never by
the installer Pod that happens to serve it. That is what makes replacement safe:

- when an installer is replaced, its successor computes the *same* placement
  identity, adopts the same revision directory and the same manifest, and re-acknowledges
  it. Nothing is orphaned and nothing is duplicated;
- two installer replicas that the scheduler happens to place on one node converge on
  one placement instead of writing two conflicting manifests under two names;
- a stale installer that comes back to life fails its ownership check and cannot
  delete its successor's workload.

## Resource accounting is a deliberate trade-off

The installer container reserves a **small fixed amount** (10m CPU, 64Mi memory by
default), not the target workload's requests:

- copying the workload's full requests onto the installer would **double-count**
  capacity: the node would see a reservation for a Pod that is not the workload, while
  the static Pod's own requests are separately accounted by kubelet;
- omitting them entirely does mean the **scheduler cannot see the workload**: a node
  it places an installer on may be unable to actually run the workload the installer
  will go on to install.

Neither option is free, and pretending the scheduler is making a decision it cannot
make would be worse than saying so. The operator's position:

- the installer declares what it really needs, which is the truth;
- `spec.installer.resources` lets you set a deliberate reservation where you want one;
- if you need placement to respect capacity, express it as **placement policy** —
  a node selector or node affinity that pins the StaticSet to a node class you have
  sized for it — rather than relying on request-based scheduling.

## Update strategies

`spec.update.strategy` decides how a new revision replaces the placement that carries
the old one.

| Strategy | Behaviour | Use when |
| --- | --- | --- |
| `Recreate` (default) | The old placement is retired before the new one is installed. | Always, unless you have a specific reason not to. At most one revision of a StaticSet exists on a node at any moment. |
| `Surge` | The new placement is installed before the old one is retired. | You would rather have two revisions briefly than a gap. `spec.update.maxSurge` is the number of placements that may overlap. |

`Recreate` combined with `maxSurge` is rejected, because the two contradict each
other.

Note what "retired" means with the availability-versus-fencing trade-off in play: a
placement whose installer is gone still holds a static Pod until cleanup completes,
so during a rollout a node can briefly host both the old workload and, after the new
installer lands, the new one. Duplicate retention is bounded by the fact that
placements are cleaned up by the protocol in the
[installation reference](./ref-installation.md), and it is reported in status rather
than hidden.

## Availability versus fencing

Fencing — guaranteeing that a node cannot keep running a workload you have retired —
takes time, because it depends on that node doing something (removing a file). Any
system that claims instant fencing is either lying or has the API server still
attached, which is exactly what a static Pod is for.

The operator's choice is to fence conservatively and report honestly:

- nothing is removed until the node's own installer (or a cleanup worker pinned to
  that node) acknowledges it;
- a node that cannot be reached keeps its pending cleanup and is reported as such;
- the only way to stop waiting is an explicit administrative policy
  (`spec.cleanup.onNodeUnreachable: AbandonAfterGrace`), which still never claims the
  remote workload was removed — only that the operator stopped tracking it.