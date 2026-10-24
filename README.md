# StaticSets

**Manage sets of static Pods from Kubernetes.**

A static Pod is a Pod a *node* owns rather than the cluster: kubelet reads a manifest
from its own filesystem and starts it, so it keeps running — and comes back after a
reboot — with the Kubernetes API unreachable. A `StaticSet` describes such a workload
as an ordinary `PodTemplateSpec`, and this operator compiles it into a self-contained
Pod, materializes the configuration it references on the node, writes it durably where
kubelet looks, and keeps it in step as the template and its configuration change.

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
    spec:
      containers:
        - name: web
          image: registry.example.com/web:1.27
          imagePullPolicy: IfNotPresent
          resources:
            requests:
              cpu: 250m
              memory: 256Mi
```

The workload itself is never scheduled by Kubernetes, but its *placement* is. The
controller creates a temporary **scheduling probe** — a ReplicaSet or a DaemonSet whose
Pods carry the workload's resources and the StaticSet's placement rules, and which run
under a `RuntimeClass` no node can start — so the cluster's own scheduler decides where
the workload goes. Every node a probe Pod landed on then gets the real static Pod from
that node's **manager**, which runs once per node as a DaemonSet and serves every
StaticSet placed on it, in every namespace.

## What it does

- **Compiles, never guesses.** Only a conservative subset of the Pod API is accepted.
  A field that cannot be represented faithfully is *rejected*, never silently dropped,
  because a static Pod that lost a field would start, look healthy, and behave
  differently from the template it came from.
- **Resolves configuration at install time.** ConfigMaps and Secrets are materialized
  as revision-specific files on the node and mounted read-only, because a static Pod
  cannot resolve a reference offline.
- **Keeps Secret material out of the manifest.** Secret-to-file is supported;
  Secret-to-environment is refused unless you explicitly opt into the disclosure.
- **Installs durably.** Immutable revision directories, explicit file modes, `fsync`,
  and atomic manifest replacement, so an interruption leaves either the old revision or
  the new one and never a half-written file. The manifest's name carries the revision,
  so a `Surge` rollout can keep the previous one running until its successor is up.
- **Fences with an ownership protocol.** Placement identity is derived from the node,
  so a replacement manager adopts its predecessor's placement; a stale manager cannot
  delete a successor's workload.
- **Separates installed state from observed health.** A placement can be installed and
  unhealthy, or installed and unobservable, and status says which.

## What it is not

The guarantee is about the *manifest*, not the application. It depends on locally
available images, a working runtime, networking, storage and the application's own
dependencies, and it is not an exact replica guarantee during a partition. The
[explanation page](https://cloudlane-one.github.io/static-sets/about-static-pods.html)
is specific about where the promise ends, and the
[deferred-features page](https://cloudlane-one.github.io/static-sets/ref-deferred-features.html)
lists what this prototype does not implement yet.

## Install

```shell
docker build -f deploy/Dockerfile -t ghcr.io/EXAMPLE/static-sets:0.1.0 .
docker push ghcr.io/EXAMPLE/static-sets:0.1.0
helm install static-sets ./deploy/helm/static-sets \
  --namespace static-sets --create-namespace \
  --set image.repository=ghcr.io/EXAMPLE/static-sets --set image.tag=0.1.0
```

Then follow [How to write a StaticSet](https://cloudlane-one.github.io/static-sets/how-to-write-a-static-set.html).
Samples live in [`deploy/samples/`](./deploy/samples), and the test suite validates them.

## Documentation

Published at [cloudlane-one.github.io/static-sets](https://cloudlane-one.github.io/static-sets/).

| Page | Why |
| --- | --- |
| [What a StaticSet guarantees](https://cloudlane-one.github.io/static-sets/about-static-pods.html) | The promise, its dependencies, and its limits. |
| [Placement and capacity](https://cloudlane-one.github.io/static-sets/about-placement-and-capacity.html) | Which scheduler sees what, and the resource-accounting trade-off. |
| [How to install](https://cloudlane-one.github.io/static-sets/how-to-install.html) | Prerequisites, chart, upgrades, rollback, uninstall. |
| [`StaticSet` reference](https://cloudlane-one.github.io/static-sets/ref-staticset.html) | Every spec and status field. |
| [Translation](https://cloudlane-one.github.io/static-sets/ref-translation.html) | The supported workload subset and every refusal. |
| [Installation](https://cloudlane-one.github.io/static-sets/ref-installation.html) | The on-node layout, the ownership protocol and cleanup. |
| [Disconnected reboot procedure](https://cloudlane-one.github.io/static-sets/how-to-verify-disconnected-reboot.html) | How to verify the reboot guarantee yourself. |
| [Deferred features](https://cloudlane-one.github.io/static-sets/ref-deferred-features.html) | What is not implemented, and the known sharp edges. |

## Development

Built on the [based-operators](https://github.com/cloudlane-one/based-operators)
framework: the custom resource is a Pydantic model, the CRD is generated from it, and
the reconciliation logic is ordinary Python, with kopf as the runtime.

```shell
uv sync                    # install dependencies
uv run pytest tests        # run the test suite
uv run ruff check .        # lint
uv run pyright .           # type-check
uv run vtr build-docs      # build the documentation
uv run app/render_crds.py  # regenerate the CRD in the chart
```

Two suites are opt-in because they need real infrastructure:

```shell
BASED_OPERATORS_IMAGE_SMOKE=1  uv run pytest tests/test_image_smoke.py -v   # needs docker
STATIC_SETS_K8S_INTEGRATION=1  uv run pytest tests/test_e2e.py -v          # needs kind/docker/helm/kubectl
```

This project follows the conventions of the repository template it was bootstrapped
from. See the [contribution guide](./docs/how-to-contribute.md) and the
[local setup guide](./docs/how-to-setup-locally.md) to get started.