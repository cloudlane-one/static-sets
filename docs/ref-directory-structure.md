# Directory Structure

Names of directories and config files in this repository are mostly based on common conventions and requirements by specific development tools, with few custom ideas added on top:

- `.venv`: Python virtual environment (*not checked out in Git*)
- `app`: Executable scripts for GUIs, CLIs and APIs (based on library code in `src`)
- `docs`: Source files (mostly Markdown) for project documentation (how-tos, API-reference, explanations, etc.)
- `src`: Source code for the core library modules
- `test`: Code for automated testing of library modules (via [pytest](https://pypi.org/project/pytest/))
- `.gitignore`: Files to ignore when commiting into the Git repository
- `.releaserc.yml`: Configuration for auto-generating releases based on commits (via [semantic-release](https://github.com/semantic-release/semantic-release))
- `AGENTS.md`: Common, repo-specific system prompt for all AI agents.
- `Brewfile`: System-dependencies for development on MacOS or Linux (installable via [Homebrew](https://github.com/Homebrew/brew))
- `configuration.winget`: System-dependencies for development on Windows (installable via [WinGet](https://learn.microsoft.com/en-us/windows/package-manager/winget/))
- `pyproject.toml`: Central Python project config ([PyPI](https://pypi.org/) dependencies, name, authors, linting config, etc.)
- `README.md`: Main description of the repository
- `uv.lock`: Exact dependency versions determined by UV (generated file)
