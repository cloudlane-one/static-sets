# How to install the operator

## Prerequisites

- A Kubernetes cluster, 1.27 or newer.
- `kubectl` and `helm`.
- A container image built from this repository (see below).
- `cert-manager` only if you want strict admission validation; the default install
  does not need it.

## 1. Build and push the image

```shell
docker build -f deploy/Dockerfile -t ghcr.io/EXAMPLE/static-sets:0.1.0 .
docker push ghcr.io/EXAMPLE/static-sets:0.1.0
```

One image carries both processes: the controller that decides what should exist and
where, and the node manager that writes the static Pods on each host. Both are derived
from the same library, so they cannot disagree about the revision hashing or the
ownership protocol.

## 2. Install the chart

```shell
helm install static-sets ./deploy/helm/static-sets \
  --namespace static-sets --create-namespace \
  --set image.repository=ghcr.io/EXAMPLE/static-sets \
  --set image.tag=0.1.0
```

The controller is **cluster-wide**: a `StaticSet` may live in any namespace, and the
node that serves a placement is named by the placement rather than by its namespace.
The chart therefore installs a `Deployment`, a `DaemonSet` (one manager Pod per node),
a cluster-scoped `RuntimeClass` the scheduling probes ask for, and two `ClusterRole`s —
one for each process, neither of which can do the other's job.

The manager mounts two host paths on every node: kubelet's static Pod directory
(`manager.staticPodPath`, default `/etc/kubernetes/manifests`) and its bundle
(`manager.bundleDir`, default `/var/lib/static-sets`). Both are values, and a node
whose kubelet is configured differently needs them set accordingly:

```shell
helm install static-sets ./deploy/helm/static-sets   --namespace static-sets --create-namespace   --set manager.staticPodPath=/var/lib/kubelet/manifests
```

If your cluster manages `RuntimeClass` objects centrally, set
`probe.createRuntimeClass=false` and create
`staticsets-scheduling-probe` yourself — it must have a handler no container runtime
implements, because a probe Pod that could start would run next to the workload.

## 3. Decide about the admission webhook

The generated CustomResourceDefinition is a *structural* schema, so the API server
**prunes** a field it does not know rather than refusing it. With the webhook off
(the default), a template field the operator cannot compile is dropped silently
before the operator ever sees it.

Turn it on to have such an object rejected instead:

```shell
helm upgrade static-sets ./deploy/helm/static-sets --namespace static-sets \
  --set image.repository=ghcr.io/EXAMPLE/static-sets \
  --set image.tag=0.1.0 \
  --set webhook.enabled=true
```

This needs cert-manager (the chart creates a self-signed `Issuer` and a
`Certificate`, or point `webhook.certManager.issuerRef` at your own). The webhook
also refuses the policy violations the compiler would otherwise only catch at install
time: a privileged workload, a Secret read into the environment.

## 4. Verify

```shell
kubectl -n static-sets rollout status deployment/static-sets
kubectl -n static-sets rollout status daemonset/static-sets-manager
kubectl get crd staticsets.staticsets.cloudlane.dev
kubectl get crd staticpodplacements.staticsets.cloudlane.dev
kubectl get runtimeclass staticsets-scheduling-probe
```

Then follow [How to write a StaticSet](./how-to-write-a-static-set.md).

## Upgrades

The operator is a Deployment with a single replica and `Recreate` strategy: two
controllers reconciling the same objects collide, so the new one replaces the old one
rather than rolling over it. Upgrading the image is therefore:

```shell
docker build -f deploy/Dockerfile -t ghcr.io/EXAMPLE/static-sets:0.2.0 .
docker push ghcr.io/EXAMPLE/static-sets:0.2.0
helm upgrade static-sets ./deploy/helm/static-sets --namespace static-sets \
  --set image.repository=ghcr.io/EXAMPLE/static-sets --set image.tag=0.2.0
```

Manager Pods are replaced by the DaemonSet, and each replacement adopts the placements
its predecessor left, so an upgrade does not reinstall the workloads. Only the
controller is `Recreate`: two controllers reconciling the same objects collide.

**Schema changes need the CRD applied by hand.** Helm installs CRDs from `crds/` and
never upgrades them:

```shell
kubectl apply -f deploy/helm/static-sets/crds/
```

The CRD in the chart is generated from the models (`uv run app/render_crds.py`), and
the test suite fails if the committed file has drifted from them.

## Rollback

Roll the operator image back with `helm rollback`, or pin the previous tag. Your
workloads are unaffected: they are static Pods on nodes, not objects the controller
owns. The older controller version will still understand a CRD that has only gained
optional fields, which is the reason to keep schema changes additive.

## Uninstall

```shell
helm uninstall static-sets --namespace static-sets
kubectl delete crd staticsets.staticsets.cloudlane.dev
kubectl delete crd staticpodplacements.staticsets.cloudlane.dev
```

Deleting the CRD deletes every StaticSet, and each one's finalizer keeps it alive
until its placements have acknowledged cleanup. If a node is unreachable the
finalizer waits, and `kubectl get staticset -o yaml` says why. Nothing is force-removed
without `spec.cleanup.onNodeUnreachable: AbandonAfterGrace`; see the
[installation reference](./ref-installation.md).

Deleting the chart's namespace does **not** remove the static Pods: they live on
nodes. Delete the StaticSets first, or accept that the manifests stay behind and are
not tracked by anything.

## Next steps

- [How to write a StaticSet](./how-to-write-a-static-set.md)
- [The `StaticSet` reference](./ref-staticset.md)
- [How to verify the disconnected reboot guarantee](./how-to-verify-disconnected-reboot.md)