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

The workload itself is never scheduled by Kubernetes, but its *placement* is: the
controller creates a temporary *scheduling probe* that the cluster's scheduler places
and that no node can start, and the node each probe Pod landed on then gets the real
static Pod from that node's manager. Two long-lived processes do the work — one
cluster-wide controller and one manager per node — and neither of them decides anything
the other could get wrong.

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
