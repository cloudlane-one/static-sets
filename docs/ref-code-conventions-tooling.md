# Coding Conventions and Tooling

- VSCode or any derived editor (supporting the VSCode extension system) should be used for code editing.
- Required VSCode extensions are recommmended in `/.vscode/extensions.json` and should be installed before starting work on the repo.
- Core code tooling is directly integrated into VSCode UI (via extensions).
  - `ruff` is used for linting.
  - `black` is used for formatting on file save.
  - `pyright` is used for type-checking (via PyLance).
  - `pytest` is used for testing.
- Additional tooling is integrated via VSCode task definitions in `/.vscode/tasks.json`.
  - They can be executed via `vtr TASK_NAME` in the terminal as well (provided by `vscode-task-runner` package).
  - Tasks are divided into different types:
    - `assert`: Succeed / fail depending on some quality condition (and print additional info to stdout).
    - `build`: Generate files for various purposes (e.g. docs, package builds, etc).
    - `print`: Only print info to stdout to be interpreted by a human or LLM.
    - `serve`: Run some kind of development server (usually with live updates).
  - See task definitions for details.
- All Python-specific config can be found in `pyproject.toml`
- Modern typehints should be used throughout the code and all functions, methods and classes should have Google-style docstrings
- `properdocs` is used for docs generation
  - config in `/docs/.config.yml`
  - `mkdosctring.python` for API docs
  - `mkdocs-typer2` renders the command reference from the Typer applications, so a
    page never hand-copies an option list
  - `properdocs` = maintained `mkdocs` successor
- Command-line entry points are [Typer](https://typer.tiangolo.com/) applications.
  The command declaration is the single source of truth for parsing, `--help` and the
  generated reference page.
- Kubernetes API access goes through the official `kubernetes` client, whose
  generated models are pydantic models. Built-in objects are read and written as
  those models, and the scheduler-placement types in `spec.placement` are the
  generated models themselves; the operator's own models describe only the
  StaticSet API, because a static Pod template is deliberately narrower and
  stricter than `V1Pod`.
- CI pipelines for both GitHub (`/.github/workflows`) and GitLab (`/.gitlab-ci.yml`) exist
  - Checks are run on every push to main or every pull request event, involving most of aforementioned tooling
  - [Semantic-release](https://python-semantic-release.readthedocs.io/en/latest/index.html) is run on every push to main, leveraging [Gitmoji](https://gitmoji.dev/) semantics to determine version numbers
