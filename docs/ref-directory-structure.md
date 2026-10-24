# Directory Structure

Names of directories and config files in this repository are based on common
conventions and requirements of specific development tools, with a few custom ideas
on top. Where a directory holds project-specific content, it is described below.

- `.venv`: Python virtual environment (*not checked out in Git*)
- `app`: Executable scripts built on the library in `src`
  - `static_sets_operator.py`: the controller entry point. This is the file
    `kopf run` executes inside the operator image, and the file the
    `based-operators` scaffolder loads to derive the CRD and the chart.
  - `static_pod_manager.py`: the node manager entry point. `kopf run` executes it
    inside the manager DaemonSet, one Pod per node; it watches placement records and
    writes the static Pods on the node it runs on.
  - `static_sets_manager/`: the `static-set-manager` command, which runs the same
    library once (or repeatedly) for one node without kopf — what an operator reaches
    for when debugging a single host. Also ships as a console script.
  - `render_crds.py`: regenerates both CRDs in the chart from the Pydantic models, so
    the committed schemas cannot drift from the types the operator reconciles.
- `deploy`: What a cluster needs to run the operator
  - `Dockerfile`: the operator image, carrying both the controller and the manager.
  - `helm/static-sets/`: the chart, including `crds/` (the generated CRD), the RBAC
    for both identities, and the optional admission webhook.
  - `samples/`: StaticSets that the test suite validates, so they always describe
    objects the operator accepts.
- `docs`: Source files (mostly Markdown) for project documentation, named after the
  [Diátaxis](https://diataxis.fr/) mode they are written in: `how-to-*.md` for tasks,
  `ref-*.md` for reference, `about-*.md` for explanation.
- `src`: Source code for the core library modules
  - `static_sets/models.py`: the `StaticSet` resource — spec, status and policies.
  - `static_sets/workload.py`: the supported `PodTemplateSpec` subset.
  - `static_sets/translation.py`: the compiler, and what it refuses.
  - `static_sets/revision.py`: deterministic revision identity.
  - `static_sets/install.py`: durable installation, ownership and cleanup on a node.
  - `static_sets/placements.py`: the `StaticPodPlacement` record — one node's static
    Pod for one StaticSet.
  - `static_sets/probe.py`: the scheduling probe, which is how the Kubernetes
    scheduler is asked for a placement.
  - `static_sets/controller.py`: the pure reconcile planner and status builder.
  - `static_sets/manager.py`: the on-node runtime, including the rollout order.
  - `static_sets/cluster.py`: the cluster-backed implementation of the manager's API.
  - `static_sets/paths.py`: path validation and durable write primitives.
  - `static_sets/api.py`: the official Kubernetes client, configured for the
    operator, with failures classified as "refused" or "unreachable" and absence
    handled at the call sites that probe.
  - `static_sets/validation.py`: strict admission validation.
  - `static_sets/naming.py`: deterministic names, labels and identities.
- `tests`: Code for automated testing of library modules and applications (via
  [pytest](https://pypi.org/project/pytest/))
  - `tests/src/`: unit tests for the library, one module per unit of behaviour.
  - `tests/app/`: tests for the entry points, the manager command and the shipped
    manifests (including chart rendering and CRD drift).
  - `tests/test_e2e.py`: the end-to-end suite against a real cluster, opt-in.
- `.dockerignore`: Files excluded from the image build context
- `.gitignore`: Files to ignore when committing into the Git repository
- `AGENTS.md`: Short map for AI agents, pointing into `docs/`
- `Brewfile`: System dependencies for development on MacOS or Linux
- `configuration.winget`: System dependencies for development on Windows
- `pyproject.toml`: Central Python project config (dependencies, linting, test config)
- `README.md`: Main description of the repository
- `uv.lock`: Exact dependency versions determined by UV (generated file)