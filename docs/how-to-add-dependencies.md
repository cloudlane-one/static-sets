# How to add dependencies

## Choosing third-party deps

There are many aspects to consider when choosing which third-party packages to rely on. Bad things can happen, if choices are not made wisely, as perfectly illustrated by this XKCD comic:

![xkcd dependency stack meme](img/xkcd-deps.png)

Arguably the most important aspect is whether packages *currently* have *known* vulnerabilities. Accordingly, a whole fleet of tools and platforms exists for scanning your deps to that end, some freely usable. For looking up singular packages online, any one of these is good for use:

- [LFX-Insights](https://insights.linuxfoundation.org/): Curated info on popular code projects
- [deps.dev](https://deps.dev/): Exhaustive info on Python and other packages
- [Snyk Advisor](https://security.snyk.io/): Exhaustive info on Python and other packages

> *vulnerability* = bug or backdoor in the source code, which can be used by an attacker to disturb the software, get unallowed access or steal data

For Python packages specifically, an open-source tool exists for scanning all your dependencies for vulnerabilities at once: [`pip-audit`](https://github.com/pypa/pip-audit), which is in fact used within this repo.

Beyond that, package health in terms of maintenance and code quality is another important signal, that ideally serves to prevent vulnerabilities alltogether.

## Installing dependencies

All dependency installation should be done using `uv`. See the [official docs](https://docs.astral.sh/uv/concepts/projects/dependencies/#adding-dependencies) for details. Tl;dr: `uv add PACKAGE_NAME` to add it to the core library dependencies, `uv add --group GROUP_NAME PACKAGE_NAME` to add it to a specific group.

> See `/pyproject.toml` for a list of all dependency groups with descriptions in the comments.
