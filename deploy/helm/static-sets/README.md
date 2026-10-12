# static-sets

Deploys the StaticSet operator: a controller that decides which nodes should host a
StaticSet's workload, and the installer identity whose Pods write the static Pod
manifests on those nodes.

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

The chart installs the CustomResourceDefinition from `crds/`, which Helm never
upgrades. Re-apply it after a schema change:

```shell
kubectl apply -f deploy/helm/static-sets/crds/
```

## What it creates

| Object | Purpose |
| --- | --- |
| `Deployment` | The controller. One replica, `Recreate`, no host mounts. |
| `ServiceAccount` `…-controller` | The controller's identity. |
| `ServiceAccount` `…-installer` | The identity installer Pods run as. |
| `Role`/`RoleBinding` `…-controller` | Namespaced access to StaticSets, the installer workload, Pods, ConfigMaps, Secrets and events. |
| `Role`/`RoleBinding` `…-installer` | Narrower: read the StaticSet and its configuration, write acknowledgments, release its own finalizer. |
| `ClusterRole` `…-node-reader` | Reading Nodes, which is cluster-scoped. The only cluster-wide access the controller has. |
| `ClusterRole` `…-webhook` | Only with `webhook.enabled`: kopf keeps the webhook configurations in sync. |
| `Service`, `Issuer`, `Certificate` | Only with `webhook.enabled`. |

The operator runs in one namespace and manages StaticSets in that namespace, which
is what makes the namespaced Roles sufficient. Point `watchNamespace` elsewhere only
if you also widen the RBAC.

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
| `image.repository` / `image.tag` | `ghcr.io/cloudlane-one/static-sets:latest` | The operator image. |
| `watchNamespace` | the release namespace | The namespace StaticSets are managed in. |
| `webhook.enabled` | `false` | Strict admission validation (needs cert-manager). |
| `installer.serviceAccountName` | `…-installer` | The identity installer Pods run as. |
| `replicaCount` | `1` | Must stay 1: two controllers reconciling one object collide. |
| `securityContext` | non-root, all capabilities dropped | The controller needs no privilege. |

## Uninstall

```shell
helm uninstall static-sets --namespace static-sets
kubectl delete crd staticsets.staticsets.cloudlane.dev
```

Deleting the CRD deletes every StaticSet, and each StaticSet's finalizer keeps it
alive until every placement has acknowledged its cleanup. If a node is unreachable,
the finalizer waits and the reason is reported in the object's status; see the
operator documentation for the abandonment policy.