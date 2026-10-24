# Command-line interface

The `static-set-manager` command runs the node manager without kopf. The DaemonSet
runs `kopf run app/static_pod_manager.py`, which is watch-driven; this command does the
same work in one pass (or on an interval), which is what an operator wants when
debugging a single host: it needs a kubeconfig, not a running controller.

It reads the node and the two host paths from its environment, reads every
`StaticPodPlacement` that names that node, and compiles, installs, retires or removes
the static Pods it finds — then reports the outcome in each record's status. Both entry
points call the same library functions, so a manual pass cannot behave differently from
the DaemonSet.

How that fits into a deployment is described in
[Installation](./ref-installation.md). This page is the command reference itself,
generated from the Typer application's own declaration, so it always agrees with
what the command actually accepts:

::: mkdocs-typer2
    :module: static_sets_manager
    :name: static-set-manager
    :pretty: true
    :engine: native

`reconcile` repeats its pass every `--interval` seconds by default, so a configuration
change, a moved revision or a cleanup request published just after the previous pass is
noticed without waiting for anything; `--interval 0` runs one pass and exits.

## The CRD renderer

The renderer is a Typer application as well. It is not part of the deployed image
and is run from a checkout:

```shell
uv run app/render_crds.py
```

::: mkdocs-typer2
    :module: render_crds
    :name: render-crds
    :pretty: true
    :engine: native