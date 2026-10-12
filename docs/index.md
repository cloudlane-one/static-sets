---
hide:
- navigation
---
# StaticSets

Manage sets of [static Pods](https://kubernetes.io/docs/tasks/configure-pod-container/static-pod/)
from Kubernetes.

A **StaticSet** describes a workload that must become a static Pod: a Pod definition
persisted on a host and started by that host's kubelet, so that it returns after a
reboot even when the Kubernetes API is unreachable. You write a `PodTemplateSpec`
you already know; the operator compiles it into a self-contained Pod, materializes
the configuration it references on the node, writes it durably where kubelet looks,
and keeps it in step as the template and its configuration change.

The workload itself is never scheduled by Kubernetes. Instead the operator creates
*installer* Pods — a Deployment or a DaemonSet — and each installer writes the
workload onto its own node. Everything the installer needs is explicit: its node,
its placement, the paths it may touch, and the revision it owns.

## Start here

- [What a StaticSet guarantees, and what it does not](./about-static-pods.md)
- [How to install the operator](./how-to-install.md)
- [How to write a StaticSet](./how-to-write-a-static-set.md)
- [The `StaticSet` reference](./ref-staticset.md)

## Development

This project follows the conventions of the repository template it was bootstrapped
from. See the [contribution guide](./how-to-contribute.md) and the
[local setup guide](./how-to-setup-locally.md).

```shell
uv sync                    # install dependencies
uv run pytest tests        # run the test suite
uv run ruff check .        # lint
uv run pyright .           # type-check
uv run vtr build-docs      # build this documentation
```

The end-to-end suite drives a real cluster and is skipped unless
`STATIC_SETS_K8S_INTEGRATION=1` is set with `kubectl`, `docker` and `helm` available;
the image smoke test needs `STATIC_SETS_IMAGE_SMOKE=1` and a Docker daemon.
[How to check quality](./how-to-check-quality.md) lists every gate and what it covers.
