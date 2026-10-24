# Command-line interface

The `static-set-installer` command is what an installer Pod runs. It reads its
assignment from the Pod's environment, compiles the StaticSet, writes the static
Pod on its own node and acknowledges the result through the placement record.

How that fits into a deployment is described in
[Installation](./ref-installation.md). This page is the command reference itself,
generated from the Typer application's own declaration, so it always agrees with
what the command actually accepts:

::: mkdocs-typer2
    :module: static_sets_installer
    :name: static-set-installer
    :pretty: true
    :engine: native

`reconcile` keeps running: it repeats its pass every `--interval` seconds so that a
configuration change, a moved revision or a cleanup intent published just after the
previous pass is noticed without waiting for kubelet's restart backoff. `cleanup`
runs a single pass, because a cleanup worker exists for exactly one placement's
removal.

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