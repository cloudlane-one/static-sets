# AGENTS.md

## Project type

A Kubernetes operator for sets of static Pods, built on the
[based-operators](https://github.com/cloudlane-one/based-operators) framework. Pure
library code lives in `/src`, the entry points (controller and per-node manager) in `/app`,
cluster artifacts in `/deploy`, and documentation in `/docs`.

Managed by `uv` with the main config in `/pyproject.toml`. Everything relevant to
contributing is in the [Contribution Guide](./docs/how-to-contribute.md) and the pages
it links to.

## Commands

```shell
uv sync --group ci              # install everything, including the CI tooling
uv run ruff check .             # lint
uv run ruff format .            # format (run this before `ruff format --check .`)
uv run pyright .                # type-check
uv run pytest tests             # fast suite; the e2e and image suites skip
uv run vtr build-docs           # build the docs (needs SITE_URL, see below)
uv run app/render_crds.py       # regenerate the CRD in the chart
uv run vtr assert-license-compliance
uv run vtr assert-no-vulnerabilities
uv run vtr assert-no-deptry-issues
```

`SITE_URL=https://example.com/ uv run properdocs build -f ./docs/.config.yml` is the
same build without the task runner; the config reads `site_url: !ENV SITE_URL`, so the
build fails without it.

The opt-in suites need real infrastructure:

```shell
STATIC_SETS_IMAGE_SMOKE=1     uv run pytest tests/test_image_smoke.py -v   # docker
STATIC_SETS_K8S_INTEGRATION=1 uv run pytest tests/test_e2e.py -v          # kind/docker/helm/kubectl
```

## Where to read before changing something

| Changing | Read first |
| --- | --- |
| The resource's fields or policies | [The `StaticSet` reference](./docs/ref-staticset.md) |
| What a template may contain, or a refusal | [Translation](./docs/ref-translation.md), then `src/static_sets/translation.py` |
| Anything the node manager writes on a node | [Installation](./docs/ref-installation.md) |
| Placement, rollouts, resource accounting | [Placement and capacity](./docs/about-placement-and-capacity.md) |
| The promise to users | [What a StaticSet guarantees](./docs/about-static-pods.md) |
| What is deliberately missing | [Deferred features](./docs/ref-deferred-features.md) |
| Layout of the repo | [Directory structure](./docs/ref-directory-structure.md) |

## Rules that are easy to get wrong

- **Both CRDs in the chart are generated.** Change a model, then run
  `uv run app/render_crds.py`. `tests/app/test_manifests.py` fails if a committed file
  has drifted, so a forgotten regeneration is caught rather than shipped.
- **The kopf entry points are loaded by path, not imported as packages.**
  `app/static_sets_operator.py` and `app/static_pod_manager.py` are executed directly by
  `kopf run`; keep them self-contained and keep `StaticSet` / `StaticPodPlacement` in
  their module namespaces. `pyright` finds them through `extraPaths` in
  `[tool.pyright]`.
- **The controller never writes a host, and the manager never decides a placement.**
  Placement belongs to the Kubernetes scheduler, asked through a probe workload; the
  manager acts only on placement records that name its node. A change that moves either
  responsibility across that line is a design change, not a refactor.
- **Rejections are explicit.** When the compiler cannot honour a field, it raises
  `UnsupportedFieldError` naming the field and the alternative. Do not soften one into
  a silent drop: the whole design rests on that.
- **Revision hashes must be deterministic.** Nothing node-specific, manager-specific
  or time-dependent may enter `compute_revision`. A perpetual rollout is the failure
  mode.
- **Revisions are content hashes, so their names carry no ordering.** Retention
  orders by the ownership record's `history`, never by sorting revision names.
- **Every pydantic validator raises `ValueError`.** Pydantic only turns `ValueError`
  (and `AssertionError`) into a `ValidationError`; a custom exception escapes
  validation and surfaces as a crash instead of a rejected object.
- **The symlink tests skip on Windows and run on Linux CI.** A change that only breaks
  on the Linux branch looks green on a Windows workstation. Run the suite in a Linux
  container before pushing if you touched `paths.py` or anything the manager writes —
  the recipe is in [How to check quality](./docs/how-to-check-quality.md).
- **Commit with a Gitmoji shortcode in text form at the start of the subject**
  (`:sparkles: …`, `:bug: …`, `:memo: …`). A rendered emoji glyph never matches the
  `emoji` commit parser, so the release is silently skipped.
- **`docs/` pages carry a Diátaxis prefix** — `how-to-*`, `ref-*`, `about-*` — and every
  new page needs a `nav:` entry in `docs/.config.yml`.