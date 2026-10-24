# static-sets

Deploys the StaticSet operator: a cluster-wide controller that asks the Kubernetes
scheduler where a StaticSet's workload should run, and a node manager whose Pods write
the static Pod manifests on the nodes the scheduler chose.

## Prerequisites

- Kubernetes 1.27 or newer.
- `cert-manager`, **only** when `webhook.enabled` is true (it issues the admission
  webhook's serving certificate).

## Install

```shell
helm install static-sets ./deploy/helm/static-sets \
  --namespace static-sets --create-namespace \
  --set image.repository=ghcr.io/cloudlane-one/static-sets \
  --set image.tag=0.1.0
```

The chart installs both CustomResourceDefinitions from `crds/`, which Helm never
upgrades. Re-apply them after a schema change:

```shell
kubectl apply -f deploy/helm/static-sets/crds/
```

## What it creates

| Object | Purpose |
| --- | --- |
| `Deployment` | The controller. One replica, `Recreate`, no host mounts. |
| `DaemonSet` `…-manager` | The node manager: one Pod per node, mounting kubelet's static Pod directory and the bundle. |
| `RuntimeClass` `staticsets-scheduling-probe` | The placeholder class the scheduling probes ask for. No container runtime implements its handler, so probe Pods are scheduled and never start. |
| `ServiceAccount` `…-controller` | The controller's identity. |
| `ServiceAccount` `…-manager` | The identity the manager Pods run as. |
| `ClusterRole` `…-controller` | StaticSets, placement records, the probe workloads, and read access to Pods. |
| `ClusterRole` `…-manager` | Read StaticSets and their configuration, write placement *status*, hold the placement finalizer. It cannot create, move or delete a placement. |
| `ClusterRole` `…-node-reader` | Reading Nodes and Namespaces, which are cluster-scoped. Shared by both identities. |
| `ClusterRole` `…-webhook` | Only with `webhook.enabled`: kopf keeps the webhook configurations in sync. |
| `Service`, `Issuer`, `Certificate` | Only with `webhook.enabled`. |

The controller is cluster-wide on purpose: a StaticSet may live in any namespace, and
the node that serves a placement is named by the placement rather than by its
namespace.

## Strict admission validation

The generated CRD is a structural schema, so the API server **prunes** a field it
does not know rather than refusing it. With `webhook.enabled=false` (the default) a
template field the operator cannot compile is therefore dropped silently by the API
server before the operator ever sees it.

Setting `webhook.enabled=true` deploys a validating admission webhook that turns
that silent drop into a rejection, and also refuses the policy violations the
compiler would catch at install time (a privileged workload, a Secret read into the
environment). It requires cert-manager for the serving certificate.

## Values

Run `helm show values ./deploy/helm/static-sets` for the annotated defaults. The
ones worth knowing:

| Value | Default | Meaning |
| --- | --- | --- |
| `image.repository` / `image.tag` | `ghcr.io/cloudlane-one/static-sets:latest` | The operator image; it carries both processes. |
| `manager.image.*` | the operator image | Override only if the manager must differ. |
| `manager.staticPodPath` | `/etc/kubernetes/manifests` | kubelet's `staticPodPath` on the node. Must match the node's kubelet configuration, and the directory must already exist. |
| `manager.bundleDir` | `/var/lib/static-sets` | The persistent directory the manager owns. |
| `manager.tolerations` | tolerate everything | A placement can be asked for on any node, so the manager has to run there. |
| `probe.runtimeClassName` | `staticsets-scheduling-probe` | The placeholder class. `probe.createRuntimeClass=false` leaves it to you. |
| `webhook.enabled` | `false` | Strict admission validation (needs cert-manager). |
| `manager.serviceAccountName` | `…-manager` | The identity the manager Pods run as. |
| `replicaCount` | `1` | Must stay 1: two controllers reconciling one object collide. |
| `securityContext` | non-root, all capabilities dropped | The controller needs no privilege. |

## Uninstall

```shell
helm uninstall static-sets --namespace static-sets
kubectl delete crd staticsets.staticsets.cloudlane.dev
kubectl delete crd staticpodplacements.staticsets.cloudlane.dev
```

Deleting the CRDs deletes every StaticSet, and each StaticSet's finalizer keeps it
alive until every placement has acknowledged its cleanup. If a node is unreachable,
the finalizer waits and the reason is reported in the object's status; see the
operator documentation for the abandonment policy.