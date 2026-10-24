# How to check quality

Certain checks are run by VSCode automatically as you edit code, while others have to be
run manually using the VSCode UI, tasks or commands in the terminal.

## Automatic checks

- Linting via `ruff`
- Type-checking via `pyright`

> Discovered issues by these tools will show up as yellow or red squiggly lines underlining your code.

## Manual checks

Pytest test cases can be run via the VSCode tests UI (beaker symbol in left sidebar) or
`uv run pytest tests` in the terminal.

VSCode tasks can be run via the UI by clicking `ctrl` + `shift` + `P` to open the command
palette and then typing `Run task`, or in the terminal via `vtr TASK_NAME`. Please run all
tasks with the prefixes `assert-`, `build-` and `info-`. Make sure none of them fail and
that their printed information does not hint at any major issues.

## What each gate covers

| Command | Checks |
| --- | --- |
| `uv run ruff check .` | Style, imports, naming, docstrings (Google convention), complexity. `src/` requires full docstrings; other trees do not. |
| `uv run ruff format --check .` | Formatting, including Python fences inside `docs/*.md`. Run `uv run ruff format .` before it, or a file you never edited will fail. |
| `uv run pyright .` | Types across `src`, `app` and `tests`. The operator entry point is a top-level module kopf loads by path, so `[tool.pyright] extraPaths` tells Pyright where to find it. |
| `uv run pytest tests --cov=src --cov=app --cov-fail-under=90` | The fast suite plus the coverage bar the [contribution guide](./how-to-contribute.md) sets. |
| `uv run vtr build-docs` | The documentation builds. |
| `uv run vtr assert-license-compliance` | Every dependency's licence is acceptable. |
| `uv run vtr assert-no-vulnerabilities` | Known vulnerabilities in the dependency tree. `pip-audit` reports `based-operators` as unauditable because it is installed from Git rather than from PyPI. |
| `uv run vtr assert-no-deptry-issues` | No unused or undeclared dependencies, and no direct import of a transitive one. |
| `uv run app/render_crds.py` | Regenerates the CRD in the chart from the models. The same rendering is asserted by `tests/app/test_manifests.py`, so a forgotten regeneration fails the suite instead of shipping. |

Helm is needed by `tests/app/test_manifests.py`, which renders the chart. That test
**fails rather than skips** when `helm` is missing while `CI` is set, so a CI image that
loses Helm cannot pass vacuously.

### Run the suite on Linux before trusting it

Several tests create symbolic links, which Windows refuses without Developer Mode, so
they **skip** on a Windows workstation and run on the Linux CI runner. A change that
only breaks on the LINUX branch therefore looks green locally. Reproduce the CI run in
a container before pushing:

```shell
docker run --rm -v "$PWD:/src" -w /src python:3.13-slim sh -c '
  apt-get update -qq && apt-get install -y -qq git
  pip install -q pytest pytest-cov pyyaml
  UV_DYNAMIC_VERSIONING_BYPASS=0.0.0 pip install -q .
  python -m pytest tests -q'
```

## Suites that need real infrastructure

They skip by default and are opt-in, because they build images or drive clusters:

| Command | Needs |
| --- | --- |
| `STATIC_SETS_IMAGE_SMOKE=1 uv run pytest tests/test_image_smoke.py -v` | A Docker daemon. Builds `deploy/Dockerfile` and runs both entry points inside the image. |
| `STATIC_SETS_K8S_INTEGRATION=1 uv run pytest tests/test_e2e.py -v` | A reachable cluster, plus `docker`, `helm` and (for kind) `kind`. Installs the chart, creates a StaticSet, waits for the manifest the node manager wrote and for kubelet's mirror Pod, and deletes the StaticSet to exercise the cleanup protocol. |

The end-to-end suite points `staticPodPath` at a directory kubelet does not watch, so it
is safe on a kind cluster whose control plane is itself static Pods. What it therefore
does **not** prove is that a rebooted kubelet starts the manifest; that is the manual
procedure in [How to verify the disconnected reboot guarantee](./how-to-verify-disconnected-reboot.md).