# What a StaticSet guarantees, and what it does not

A static Pod is a Pod that a *node* owns rather than the cluster: kubelet reads a
manifest from a directory on its own filesystem and starts it. Kubernetes finds out
about it afterwards, as a *mirror* Pod it cannot edit and must not schedule. That is
the whole point — the workload keeps running, and comes back after a reboot, without
the control plane being reachable.

## The guarantee

After an installation has been acknowledged, kubelet restarts the last installed
revision following a host reboot, without Kubernetes API access.

"Restarts" here means kubelet starts the containers again from the manifest it finds.
Whether they come *up* depends on the same things it always does, which is why the
next section exists.

## What that depends on

The guarantee is about the manifest, not about the application. It holds only while
these are true on the node:

- **Images are present locally.** A container whose image is missing will not start
  without a registry. `spec.offline.imageProvisioning` decides whether the operator
  simply reports that images are externally provisioned, or refuses to publish a
  revision until they are confirmed present.
- **The container runtime and networking are up.** A static Pod uses the node's
  runtime and CNI like any other Pod; kubelet restoring a manifest does not restore
  a broken runtime.
- **Storage is available.** Anything the workload mounts from the node must still be
  there. Kubernetes-backed storage (a `persistentVolumeClaim`) is rejected outright,
  because attaching a volume needs the API server.
- **The application's own dependencies are reachable.** Data stores, peers, secrets
  it fetches at runtime: none of that is the operator's promise.

## What it is not

- **Not arbitrary Pod compatibility.** Only a conservative, validated subset of the
  Pod API is compiled; see [the translation reference](./ref-translation.md) for the
  exact list. A field the operator cannot represent faithfully is *rejected*, never
  dropped, because a static Pod that silently lost a field would start, look healthy,
  and behave differently from the template it came from.
- **Not a replica count.** `spec.replicas` counts *installer placements*, not running
  copies of the workload. A placement that is replaced on one node and relocated to
  another can briefly leave two installers' worth of offline workloads in existence;
  see [placement and capacity](./about-placement-and-capacity.md).
- **Not an exact replica guarantee during a partition.** A partitioned node keeps
  serving whatever it last installed. Nothing the operator can do from the control
  plane changes that, and pretending otherwise would be the worst outcome.
- **Not a configuration manager.** The operator copies configuration *content* onto
  the node at install time. A ConfigMap that changes in the cluster becomes a new
  revision; a ConfigMap that changes and is then never reconciled does not.

## Why installer Pods

The alternative — writing manifests from the controller — would require the
controller to have a filesystem relationship with every node, which is both a large
privilege and a large failure domain. Instead:

- the **controller** decides what should exist and reports what it observes. It
  mounts nothing from the host and runs unprivileged;
- an **installer** Pod is scheduled by Kubernetes, lands on one node, learns that it
  is there, and writes only that node's workload. It is replaced by the ordinary
  Deployment or DaemonSet machinery, and a replacement adopts the placement its
  predecessor left behind.

The cost is that installation is asynchronous: a StaticSet is not installed until its
installer says so, which is why the status distinguishes *installed* state from
*observed* health instead of collapsing both into "ready".

## Where to go next

- [Placement and capacity](./about-placement-and-capacity.md) — what the scheduler
  does and does not see.
- [The `StaticSet` reference](./ref-staticset.md) — every field.
- [The installation reference](./ref-installation.md) — what ends up on the node, and
  the ownership and cleanup protocols that protect it.
- [Deferred features](./ref-deferred-features.md) — what this prototype does not do.
