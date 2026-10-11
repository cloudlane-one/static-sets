# How to contribute

## Types of Contributions

The nature of a contribution roughly aligns with the folders/files, in which it happens, and can be divided into 5 types:

1. ***Refactoring*** contributions may add/modify any files in the repository, but are **not** allowed to change the *public interface* of modules in `/src` or applications in `/app`, nor make content changes to anything in `/docs`. This may be ensured by not changing any code in `/tests` during refactoring, though this is not a requirement.
2. ***Documentation*** contributions **only** add/modify files in `/docs`.
3. ***Library*** contributions add/modify *modules* in `/src` and may include ***Documentation*** contributions related to the changes in `/src`. Additionally, they must add cases in `/tests/src` to cover at least **90 %** of the contributed code.
4. ***Application*** contributions add/modify *notebook scripts* (in the form of [Jupytext](https://jupytext.org/)) in `/app`. These may be rather abstract interfaces for general use of library code, or manifest very specific use cases including elaborate explanations within the notebooks. They may include ***Library*** contributions required by respective applications. Additionally, they must add cases in `/tests/app` to cover at least **90 %** of the contributed code.
5. ***Major Upgrade*** contributions include larger, breaking changes to the repository, and as such are not restricted in what they add/modify. However, these contributions will trigger a major version change and thus need to be coordinated carefully with the maintainers.

## Pull Requests

Any contribution should be made via forking the repo / branching off `main` and later merging all changes back using a *Pull Request (PR)* (or *Merge Request (MR)* on GitLab). Every PR should have a single contribution type (see above), which has to be mentioned at the beginning of the PR title like so:

```txt
<Contribution type>: <Rest of your title>
```

> If multiple types apply to your contribution, pick the one with the highest number in the list above.

For the rest of the PR description, please use the [PR template](./pull_request_template.md).

## Developing locally

Please refer to the [local setup guide](./how-to-setup-locally.md) to get started with working on your local copy of the repo.

## Commits and Issues

Larger contributions / PRs should be split into multiple commits with their own types according to the [Gitmoji Spec](https://gitmoji.dev/). If a commit addresses an existing *Issue* on GitHub / GitLab / etc., please reference that issue via `#<ISSUE_ID>` somewhere in the commit message. If it resolves the issue (meaning it should be closed), add a footer line to the commit message with `Closes:`, `Fixes:`, or `Resolves:` followed by the issue reference. Make sure you plan ahead how to split up your contribution into commits and in which order.

## Writing Good Code

Please adhere to coding conventions and use tooling as described in [this reference](./ref-code-conventions-tooling.md). In case you plan on adding any new external (pip) dependencies, please refer to [this guide](./how-to-add-dependencies.md) in doing so. Finally, once you are ready to push your code and make a pull request, please make sure you check its quality locally first according to [this guide](./how-to-check-quality.md).

## The Complete Workflow

The entire workflow for making a contribution goes roughly as follows:

1. Create a fork or banch to work in.
2. Clone your fork / branch and open it in VSCode.
3. Install all recommended extensions.
4. Install/update dependencies via the VSCode task `update-dependencies`.
5. Make one or more commits adhering to [Gitmoji Spec](https://gitmoji.dev/).
6. Run local checks and make sure all pass.
7. Push your commits and make a PR with a title conforming to above spec and description according to the [PR template](./pull_request_template.md).
8. Have a kind interaction with the maintainers in the PR comments, get approval, and merge 😊.
