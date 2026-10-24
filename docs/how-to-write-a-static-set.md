# How to write a StaticSet

A StaticSet is a `PodTemplateSpec` plus the placement, update and offline policies a
static Pod cannot inherit from anywhere. This guide writes one from scratch; the
[reference](./ref-staticset.md) lists every field.

## 1. Write the configuration it needs

The operator resolves ConfigMaps and Secrets itself, so they have to exist in the
same namespace as the StaticSet before it is installed:

```yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: web-conf
  namespace: static-sets
data:
  app.conf: |
    greeting=hello
```

```shell
kubectl apply -f deploy/samples/configmap.yaml
```

## 2. Write the StaticSet

```yaml
apiVersion: staticsets.cloudlane.dev/v1
kind: StaticSet
metadata:
  name: web
  namespace: static-sets
spec:
  mode: Deployment
  replicas: 2

  placement:
    nodeSelector:
      kubernetes.io/os: linux

  template:
    metadata:
      labels:
        app: web
    spec:
      containers:
        - name: web
          image: registry.example.com/web:1.27
          imagePullPolicy: IfNotPresent
          env:
            - name: GREETING
              valueFrom:
                configMapKeyRef:
                  name: web-conf
                  key: app.conf
          volumeMounts:
            - name: conf
              mountPath: /etc/web
          readinessProbe:
            httpGet:
              path: /healthz
              port: 8080
      volumes:
        - name: conf
          configMap:
            name: web-conf
```

```shell
kubectl apply -f web-staticset.yaml
```

Worth noting about each part:

- **`mode`** and **`replicas`** are a pair. `Deployment` mode requires `replicas`:
  it is how many placements the operator asks the scheduler for, and therefore how
  many nodes will host the workload. `DaemonSet` mode must *not* set `replicas`,
  because the DaemonSet controller decides how many placements a pass produces.
- **`placement`** is what the scheduler uses to place the *scheduling probe*, and
  therefore the workload. It is not copied onto the workload itself; see
  [placement and capacity](./about-placement-and-capacity.md).
- **`template`** is an ordinary `PodTemplateSpec`, restricted to the subset the
  operator can compile, and it must not carry its own placement fields. Its containers'
  `resources` are what the scheduler sees when it decides where the workload goes.
- **Where the manifest is written** is not part of a StaticSet: the node manager is
  configured once per node by the chart (`manager.staticPodPath`, `manager.bundleDir`),
  so a StaticSet cannot widen the host access of the process that writes it.

## 3. Watch it install

Installation is asynchronous, and happens in visible steps: the controller creates a
scheduling probe, the scheduler places it, and the manager on each chosen node compiles
the workload and writes the manifest before anything is installed.

```shell
kubectl -n static-sets get staticset web -o yaml
kubectl -n static-sets get staticpodplacements -l staticsets.cloudlane.dev/set=web -o wide
kubectl -n static-sets get replicasets -l app.kubernetes.io/component=scheduling-probe
```

The probe workload is transient: it exists only while the scheduler's answer is being
installed, and its Pods never start. The placement records are the durable part.

The status separates what was **installed** from what was **observed**:

```yaml
status:
  observedGeneration: 1
  desiredRevision: r-8a5c...
  placements:
    - identity: e6383089b2
      node: worker-1
      revision: r-8a5c...
      phase: Installed
      acknowledged: true
  health:
    - identity: e6383089b2
      node: worker-1
      observed: true
      phase: Running
      ready: true
  conditions:
    - type: Ready
      status: "True"
      reason: AllPlacementsInstalled
```

`placements` is what the node's manager *did*; `health` is what kubelet was *seen* to
run. A placement can be `Installed` and unhealthy, or `Installed` and unobservable
because its node is gone, and the two are never conflated.

## 4. Change it

Editing the configuration a template references produces a **new revision**, because
the revision is a hash of the compiled Pod and every file it needs:

```shell
kubectl -n static-sets patch configmap web-conf \
  --type merge -p '{"data":{"app.conf":"greeting=goodbye\n"}}'
```

Watch `desiredRevision` change and the placement re-acknowledge. Editing
`spec.template` does the same. Nothing is removed until the new revision is in place,
and the previous revision's files stay for `spec.offline.retainedRevisions`
generations so a rollback has something to roll back to.

## 5. Delete it

```shell
kubectl -n static-sets delete staticset web
```

The object stays until every placement has acknowledged that its manifest is gone.
If a node is unreachable it waits, and the reason appears in the object's status. See
[the installation reference](./ref-installation.md) for the protocol and for the
administrative escape hatch.

## Common refusals

The operator refuses rather than silently dropping. The ones you are most likely to
meet, and what to do instead:

| Refusal | Use instead |
| --- | --- |
| `spec.template.spec.nodeSelector` / `affinity` / `tolerations` | `spec.placement` |
| `serviceAccountName`, `automountServiceAccountToken` | Nothing: a static Pod cannot hold an API token offline. |
| `imagePullSecrets` | Pre-pull the image on the node and use `imagePullPolicy: IfNotPresent`. |
| `persistentVolumeClaim`, any other API-backed volume | An `emptyDir`, or a ConfigMap or Secret volume. |
| `hostPath` volume in the template | Nothing: the only `hostPath` mounts are the revision directories the operator materializes. |
| `volumeMounts[].subPath` | Separate volumes, or an `items` mapping that puts the file where you want it. |
| `securityContext.privileged` | `spec.policy.allowPrivileged: true`, if you accept that nothing reviews the manifest. |
| `env[].valueFrom.secretKeyRef` | A Secret volume, or `spec.policy.allowInlineSecretEnv: true` if you accept the disclosure. |

Every refusal names the field and, where one exists, the alternative. The
[translation reference](./ref-translation.md) has the complete list.