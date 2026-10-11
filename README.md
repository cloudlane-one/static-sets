# Based PyProject

This repository is intended as a template for quickly setting up modern Python projects including dependency management, linting, docs generation, continuous integration and AI tooling.

> Please replace this with your own documentation.

## How to fork this repo as a template

> On GitHub, you can use this repo as a template for creating a new repository instead of forking via the default mechanism.

This repo is intended to get you up and running with a new Python project ASAP, so there are only a few places requiring manual changes after you created your fork:

- `/docs`
  - `.config.yml`: Change `site_name`, `repo_url` and `repo_name`
  - `index.md`: Change title and contents
  - `ref-python-api.md`: Change title to your *library name*
- `/src`
  - `based_pyproject`: rename folder to your *library name* (should be normalized version of your package name)
- `/pyproject.toml`
  - Change `project.name` to your *package name*
  - Adapt `project.description`
  - Change `tool.deptry.known_first_party` to contain only your *library name*
  - *Optionally:* Uncomment `[tool.semantic_release.remote]` section if using ForgeJo / Gitea as primary host
  - *Optionally:* [Import](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-rulesets/managing-rulesets-for-a-repository#importing-a-ruleset) the main branch protection ruleset `/.github/rulesets/main.json` to your new repo if using GitHub
- `/README.md` (this file): Change title and contents
