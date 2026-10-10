[issues-url]: https://github.com/dfkunstler/mcp-server-for-oscal/issues/
[new-issue-url]: https://github.com/dfkunstler/mcp-server-for-oscal/issues/new

# Contributing Guidelines

<!-- tags: contributing, process, community -->

Thank you for your interest in contributing to our project. Whether it's a bug report, new feature, correction, or additional
documentation, we greatly value feedback and contributions from our community.

Please read through this document before submitting any issues or pull requests to ensure we have all the necessary
information to effectively respond to your bug report or contribution.

## Contents

- [Reporting Bugs/Feature Requests](#reporting-bugsfeature-requests): check existing issues first, include reproduction details
- [Contributing via Pull Requests](#contributing-via-pull-requests): fork, focused change, passing tests, CI
- [Development standards](#development-standards): setup, required checks, code style, and what to update with each change
- [Finding contributions to work on](#finding-contributions-to-work-on): issue labels to start from
- [Code of Conduct](#code-of-conduct), [Security issue notifications](#security-issue-notifications), [Licensing](#licensing)


## Reporting Bugs/Feature Requests

We welcome you to use the GitHub issue tracker to report bugs or suggest features.

When filing an issue, [please check existing open or recently closed issues][issues-url] to make sure somebody else hasn't already
reported the issue. Please try to include as much information as you can. Details like these are incredibly useful:

* A reproducible test case or series of steps
* The version of our code being used
* Any modifications you've made relevant to the bug
* Anything unusual about your environment or deployment


## Contributing via Pull Requests
Contributions via pull requests are much appreciated. Before sending us a pull request, please ensure that:

1. You are working against the latest source on the *main* branch.
2. You check existing open, and recently merged, pull requests to make sure someone else hasn't addressed the problem already.
3. You [open an issue][new-issue-url] to discuss any significant work - we would hate for your time to be wasted.

To send us a pull request, please:

1. Fork the repository.
2. Modify the source; please focus on the specific change you are contributing. If you also reformat all the code, it will be hard for us to focus on your change.
3. Ensure local tests pass.
4. Commit to your fork using clear commit messages.
5. Send us a pull request, answering any default questions in the pull request interface.
6. Pay attention to any automated CI failures reported in the pull request, and stay involved in the conversation.

GitHub provides additional document on [forking a repository](https://help.github.com/articles/fork-a-repo/) and
[creating a pull request](https://help.github.com/articles/creating-a-pull-request/).


## Development standards

<!-- tags: development, setup, testing, style -->

Environment setup, configuration, and the full list of hatch scripts are in [DEVELOPING.md](DEVELOPING.md). A map of the code and its conventions, written for AI coding assistants but useful to people too, is in [AGENTS.md](AGENTS.md).

- **Use hatch for everything.** Run tests, type checks, lint, and scripts through `hatch` so they use the locked environment (`requirements.txt`). `hatch run tests` (mypy, pytest on Python 3.13 and 3.14 with coverage, and bandit) must pass before you open a PR. To pass flags to pytest, put them after `--`, for example `hatch test -- -k schema`.
- **Format and lint** with `hatch check fmt --fix` and `hatch check code --fix`. Ruff settings and the reasons for each ignored rule are in `pyproject.toml`. Prefer a targeted `# noqa: RULE - reason` to adding a new global ignore.
- **Use oscal-bindings** (`oscal_bindings.models`) to parse, validate, and serialize OSCAL. Don't hand-roll OSCAL parsing or validation.
- **Reference an issue.** Work on a branch for a single issue and include `#<issue>` in your commit messages.
- **Keep related files in sync:**
  - New or changed MCP tool: update `get_tool_list()` in `src/mcp_server_for_oscal/tools/__init__.py`, the tool's docstring (which clients see as the tool description), and `src/mcp_server_for_oscal/tools/README.md`.
  - New environment variable: update `config.py`, `dotenv.example`, and the table in DEVELOPING.md. If users should be able to set it, also update `server.json` and `conf/mcpb/`.
  - Changed bundled schemas or content: run `hatch run rehash`, and `hatch run build-db` for anything under `data/`.
- **Tests:** use pytest, with hypothesis for property tests. Put tool tests under `tests/tools/` and reuse the fixtures in `tests/conftest.py`.


## Finding contributions to work on
Looking at the [existing issues][issues-url] is a great way to find something to contribute on. As our projects, by default, use the default GitHub issue labels (enhancement/bug/duplicate/help wanted/invalid/question/wontfix), looking at any 'help wanted' issues is a great place to start.


## Code of Conduct
This project has adopted the [Amazon Open Source Code of Conduct](https://aws.github.io/code-of-conduct).
For more information see the [Code of Conduct FAQ](https://aws.github.io/code-of-conduct-faq) or contact
opensource-codeofconduct@amazon.com with any additional questions or comments.


## Security issue notifications
If you discover a potential security issue in this project we ask that you notify AWS/Amazon Security via our [vulnerability reporting page](http://aws.amazon.com/security/vulnerability-reporting/). Please do **not** create a public github issue.


## Licensing

See the [LICENSE](LICENSE) file for our project's licensing. We will ask you to confirm the licensing of your contribution.
