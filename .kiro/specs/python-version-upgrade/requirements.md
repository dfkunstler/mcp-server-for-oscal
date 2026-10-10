# Requirements Document

## Introduction

Raise the supported Python floor of `mcp-server-for-oscal` from 3.11 to 3.13, make 3.14 the default development interpreter, and test on 3.13 and 3.14. Every place that declares a Python version (package metadata, hatch environments, ruff, the dependency lock command, CI workflows, the AgentCore container, the MCPB bundle, and user/contributor/agent docs) moves together so no site advertises or tests a version the project no longer supports.

CI expands to a test matrix on Linux, macOS, and Windows, adds a stdio smoke test that exercises the real server process at the current stateless MCP protocol revision (`server/discover`) and at the newest `initialize`-handshake revision, with both versions read from the installed MCP SDK. The MCP SDK fixes the protocol era on the first request of a connection, and a stdio process is one connection, so the smoke test launches the server once per era with the same launch command. CI also adds a separate MCPB bundle job on macOS and Windows that packs the bundle, installs it from its unpacked contents, and runs the same smoke test against it.

Out of scope: Python 3.15, 3.15t (free-threaded), and 3.16-dev. These are a later change.

### Verified version declaration sites

| Site | Current | Target |
|---|---|---|
| `pyproject.toml` `[project] requires-python` | `>=3.11` | `>=3.13` |
| `pyproject.toml` classifiers | 3.11, 3.12 | 3.13, 3.14 |
| `pyproject.toml` `[tool.hatch.envs.default] python` | `3.12` | `3.14` |
| `pyproject.toml` `[tool.ruff] target-version` | `py311` | `py313` |
| `pyproject.toml` `update` script `uv pip compile --python-version` | `3.11` | `3.13` |
| `pyproject.toml` `[[tool.hatch.envs.hatch-test.matrix]] python` | `["3.11", "3.12"]` | `["3.13", "3.14"]` |
| `requirements.txt` | locked for 3.11 | re-locked via `hatch run update` |
| `conf/mcpb/pyproject.toml` `requires-python` | `>=3.11` | `>=3.13` |
| `conf/mcpb/manifest.json` `compatibility.runtimes.python` | `>=3.11` | `>=3.13` |
| `conf/agentcore/Dockerfile` base image | `python:3.12-slim` | `python:3.14-slim` |
| `.github/workflows/build.yml` `setup-python` | `3.12` | `3.14` (plus matrix) |
| `.github/workflows/update-awesome-oscal.yml` `setup-python` | `3.12` | `3.14` |
| `mise.toml` `[tools] python` | `3.12` | `3.14` |
| `README.md` (~403) | 3.11+, `uv install python 3.12`, "only test 3.11 & 3.12" | 3.13+, 3.14, "test 3.13 & 3.14" |
| `CONTRIBUTING.md` (~62) | "pytest on Python 3.11 and 3.12" | 3.13 and 3.14 |
| `conf/powers/oscal/POWER.md` (~202, ~355) | 3.11 or higher | 3.13 or higher |
| `.kiro/steering/tech.md` (5, 70) | 3.11+/3.12, ruff target 3.11 | 3.13+/3.14, ruff target 3.13 |
| `.kiro/steering/hatch.md` (~20, ~33-35) | `-py 3.12`, matrix 3.11/3.12, default 3.12 | `-py 3.14`, matrix 3.13/3.14, default 3.14 |
| `AGENTS.md` (~85, ~112) | `hatch run tests` "(3.11 + 3.12)", `build.yml` "on Python 3.12" | 3.13 + 3.14, Python 3.14 (plus matrix) |
| `.agents/summary/codebase_info.md` (~12) | `>=3.11`, CI matrix 3.11/3.12, default dev 3.12, mise pins 3.12 | `>=3.13`, 3.13/3.14, 3.14, mise pins 3.14 |
| `.agents/summary/dependencies.md` (~38, ~48) | mise pins python 3.12, `--python-version 3.11` | 3.14, `--python-version 3.13` |
| `.agents/summary/index.md` (~14) | "Python 3.11+" | "Python 3.13+" |
| `.agents/summary/workflows.md` (~48, ~54) | pytest matrix 3.11/3.12, lock "universal, py3.11" | 3.13/3.14, py3.13 |

Checked and found no version declaration: `[tool.mypy]` (no section, no `python_version`), `DEVELOPING.md`, `bin/build_mcpb.py`, `conf/mcpb/src/server.py`, `server.json`, `.github/workflows/release.yml`. `release.yml` publishes artifacts built by `build.yml` and has no `setup-python` step.

## Glossary

- **Package**: the `mcp-server-for-oscal` Python distribution defined by the root `pyproject.toml`.
- **Supported_Versions**: the set of CPython minor versions the Package supports and tests: 3.13 and 3.14.
- **Minimum_Version**: CPython 3.13, the lowest member of Supported_Versions.
- **Default_Dev_Version**: CPython 3.14, the interpreter for the hatch `default` environment.
- **Hatch_Default_Env**: the `[tool.hatch.envs.default]` environment used by `hatch run`.
- **Hatch_Test_Matrix**: the `[[tool.hatch.envs.hatch-test.matrix]]` Python list used by `hatch test --all`.
- **Lock_File**: `requirements.txt`, the universal lock produced by the `hatch run update` script and applied to every hatch environment through `UV_CONSTRAINT`.
- **Version_Site**: any file location listed in the "Verified version declaration sites" table.
- **Server**: the MCP server process started by the `mcp-server-for-oscal` console script (stdio transport by default).
- **Smoke_Test**: a pytest test that starts the Server as a subprocess over stdio once per protocol era, using the same launch command, and exercises the Modern_Exchange at Latest_Modern_Version and the Handshake_Exchange at Latest_Handshake_Version, each on its own subprocess, checking each response.
- **MCP_SDK**: the installed `mcp` Python SDK (2.1.1 at the time of writing), whose `mcp_types.version` module defines the protocol version constants.
- **Latest_Modern_Version**: the value of `mcp_types.version.LATEST_MODERN_VERSION` in the installed MCP_SDK (`2026-07-28` in mcp 2.1.1), the newest stateless MCP protocol revision.
- **Latest_Handshake_Version**: the value of `mcp_types.version.LATEST_HANDSHAKE_VERSION` in the installed MCP_SDK (`2025-11-25` in mcp 2.1.1), the newest protocol revision negotiated through `initialize`.
- **Modern_Envelope**: the per-request `params._meta` object required by modern protocol revisions, containing the protocol version under `PROTOCOL_VERSION_META_KEY` (`io.modelcontextprotocol/protocolVersion`), client info under `CLIENT_INFO_META_KEY`, and client capabilities under `CLIENT_CAPABILITIES_META_KEY`, with the key names taken from `mcp_types`.
- **Modern_Exchange**: a `server/discover` request followed by a `tools/list` request, each carrying a Modern_Envelope at Latest_Modern_Version, with no `initialize` request.
- **Handshake_Exchange**: an `initialize` request at Latest_Handshake_Version, the `notifications/initialized` notification, and a `tools/list` request.
- **Expected_Tool_Set**: the tool names returned by `get_tool_list()` in `mcp_server_for_oscal.tools`, with the `about` tool and conditionally registered tools handled as `main.py` registers them.
- **CI_Workflow**: `.github/workflows/build.yml`.
- **Test_Matrix_Job**: the CI_Workflow job that runs the test suite on each combination of runner OS and Supported_Versions.
- **Runner_OS_Set**: `ubuntu-latest`, `macos-latest`, `windows-latest`.
- **Build_Job**: the existing CI_Workflow job that runs `hatch run release` and uploads the wheel, sdist, and `.mcpb` artifacts.
- **MCPB_Job**: a new CI_Workflow job that verifies the MCP Bundle on `macos-latest` and `windows-latest`.
- **MCPB_Bundle**: the `.mcpb` file produced by `hatch run build-mcpb` (via `bin/build_mcpb.py`), using MCPB `manifest_version` 0.4 and server type `uv`.
- **MCPB_CLI**: the `@anthropic-ai/mcpb` command-line tool invoked through `npx`.
- **Unpacked_Bundle_Dir**: a temporary directory holding the extracted contents of an MCPB_Bundle.
- **AgentCore_Image**: the container built from `conf/agentcore/Dockerfile`.
- **Docs_Set**: `README.md`, `CONTRIBUTING.md`, `conf/powers/oscal/POWER.md`, `.kiro/steering/tech.md`, `.kiro/steering/hatch.md`.
- **Agent_Docs_Set**: `AGENTS.md`, `.agents/summary/codebase_info.md`, `.agents/summary/dependencies.md`, `.agents/summary/index.md`, `.agents/summary/workflows.md`, and any other file under `.agents/summary/` that states a supported, tested, default, or lock-target Python version.

## Requirements

### Requirement 1: Package metadata

**User Story:** As a user installing from PyPI, I want the Package to declare 3.13 as its floor, so that installers reject interpreters the project does not test.

#### Acceptance Criteria

1. THE Package SHALL declare `requires-python = ">=3.13"` in the root `pyproject.toml`.
2. THE Package SHALL list the trove classifiers `Programming Language :: Python :: 3.13` and `Programming Language :: Python :: 3.14`.
3. THE Package SHALL list no `Programming Language :: Python :: 3.<minor>` classifier for a minor version outside Supported_Versions.
4. WHEN the Package is built with `hatch build`, THE Package wheel metadata SHALL contain `Requires-Python: >=3.13`.

### Requirement 2: Hatch environments

**User Story:** As a developer, I want hatch to develop on 3.14 and test on 3.13 and 3.14, so that local runs match the supported range.

#### Acceptance Criteria

1. THE Hatch_Default_Env SHALL set `python = "3.14"`.
2. THE Hatch_Test_Matrix SHALL set `python = ["3.13", "3.14"]`.
3. WHEN a developer runs `hatch run tests`, THE Package test pipeline SHALL run mypy, pytest on each Hatch_Test_Matrix version, and bandit, and exit with status 0.
4. THE `hatch run typing` script SHALL exit with status 0 under the Default_Dev_Version.

### Requirement 3: Lint target

**User Story:** As a developer, I want ruff to target the Minimum_Version, so that lint and format rules reflect the syntax the Package can rely on.

#### Acceptance Criteria

1. THE root `pyproject.toml` SHALL set `[tool.ruff] target-version = "py313"`.
2. WHEN a developer runs `hatch check fmt` and `hatch check code`, THE ruff checks SHALL exit with status 0.
3. IF the new target version enables lint findings in existing code, THEN THE change SHALL resolve each finding by a code fix or by a targeted `# noqa: RULE - reason` comment.

### Requirement 4: Dependency lock

**User Story:** As a maintainer, I want the Lock_File resolved for the Minimum_Version, so that every Supported_Version installs the same pinned dependencies.

#### Acceptance Criteria

1. THE `update` script in the root `pyproject.toml` SHALL pass `--python-version 3.13` to `uv pip compile`.
2. WHEN the `update` script runs, THE Lock_File SHALL be regenerated as a universal lock for `>=3.13`.
3. THE Lock_File SHALL resolve to packages that install on CPython 3.13 and 3.14 on Linux, macOS, and Windows.
4. IF the regenerated Lock_File changes a pinned version of a direct dependency, THEN THE change summary SHALL list each such version change.

### Requirement 5: Runtime and packaging sites

**User Story:** As an operator deploying the server, I want container and bundle declarations to match the supported range, so that deployed runtimes satisfy `requires-python`.

#### Acceptance Criteria

1. THE AgentCore_Image base image SHALL be `public.ecr.aws/docker/library/python:3.14-slim`.
2. THE `conf/mcpb/pyproject.toml` SHALL declare `requires-python = ">=3.13"`.
3. THE `conf/mcpb/manifest.json` SHALL declare `compatibility.runtimes.python` as `">=3.13"`.
4. THE `conf/mcpb/manifest.json` SHALL keep `manifest_version` `0.4` and server type `uv`.
5. WHEN `hatch run build-mcpb` runs, THE MCPB_CLI `validate` step SHALL exit with status 0.

### Requirement 6: Documentation and steering

**User Story:** As a reader of the docs, I want every stated Python version to match Supported_Versions, so that setup instructions work.

#### Acceptance Criteria

1. THE Docs_Set SHALL state Python 3.13 or higher as the minimum version.
2. THE `README.md` prerequisites SHALL state that the project tests Python 3.13 and 3.14 and SHALL reference installing Python 3.14 with uv.
3. THE `CONTRIBUTING.md` SHALL state that `hatch run tests` runs pytest on Python 3.13 and 3.14.
4. THE `.kiro/steering/tech.md` SHALL state Python 3.13+, tested on 3.13 and 3.14, default dev environment 3.14, and ruff target Python 3.13.
5. THE `.kiro/steering/hatch.md` SHALL use `3.14` in the single-version `hatch test -py` example and SHALL state matrix 3.13, 3.14 and default environment Python 3.14.
6. THE Agent_Docs_Set SHALL state Python 3.13 or higher as the minimum version wherever a minimum version is stated.
7. THE Agent_Docs_Set SHALL state the test matrix as 3.13 and 3.14, the default dev environment and `mise.toml` pin as 3.14, the CI build interpreter as 3.14, and the `hatch run update` lock target as 3.13, wherever each of those facts is stated.
8. WHEN a search for `3.11` and `3.12` as Python versions runs across Version_Sites and the Agent_Docs_Set, THE search SHALL return no match that describes a supported, tested, default, or lock-target Python version.

### Requirement 7: CI interpreter versions

**User Story:** As a maintainer, I want CI jobs to use a Supported_Version, so that CI never builds on an interpreter the Package rejects.

#### Acceptance Criteria

1. THE Build_Job SHALL install Python 3.14 with `actions/setup-python`.
2. THE `.github/workflows/update-awesome-oscal.yml` workflow SHALL install Python 3.14 with `actions/setup-python`.
3. THE Build_Job SHALL keep its existing outputs: `server.json` validation, `hatch run release`, and the coverage, python-packages, and mcpb artifact uploads.
4. THE `draft-release` job SHALL keep its dependency on the Build_Job.

### Requirement 8: CI cross-platform test matrix

**User Story:** As a maintainer, I want tests on Linux, macOS, and Windows for each Supported_Version, so that platform-specific breakage is caught before release.

#### Acceptance Criteria

1. THE CI_Workflow SHALL define a Test_Matrix_Job whose matrix is the cross product of Runner_OS_Set and Supported_Versions (6 combinations).
2. WHEN the Test_Matrix_Job runs for one combination, THE Test_Matrix_Job SHALL install that Python version and run the pytest suite through `hatch test` for that version only.
3. THE Test_Matrix_Job SHALL set `fail-fast: false` so one failing combination does not cancel the other combinations.
4. IF any Test_Matrix_Job combination fails, THEN THE CI_Workflow run SHALL report failure.
5. WHERE a test depends on a POSIX-only facility, THE test SHALL be skipped on Windows with a skip reason that names the facility.

### Requirement 9: stdio smoke test

**User Story:** As a maintainer, I want a test that talks to the real server process over stdio at the current MCP protocol revision and at the newest handshake revision, so that startup, transport, protocol negotiation, and tool registration are verified end to end for both modern and legacy clients.

#### Acceptance Criteria

1. THE Smoke_Test SHALL start the Server as a subprocess using the stdio transport once per protocol era, with the same launch command, and SHALL run the Modern_Exchange and the Handshake_Exchange each on its own subprocess launch.
2. THE Smoke_Test SHALL read Latest_Modern_Version, Latest_Handshake_Version, and the Modern_Envelope meta key names from the `mcp_types` constants of the installed MCP_SDK, so that an MCP_SDK upgrade moves the Smoke_Test to the newest protocol revisions without a test edit.
3. WHEN the Server subprocess starts, THE Smoke_Test SHALL send a `server/discover` request carrying a Modern_Envelope at Latest_Modern_Version, before any `initialize` request.
4. WHEN the `server/discover` response is received, THE Smoke_Test SHALL assert that the response is a JSON-RPC result whose `supportedVersions` contains Latest_Modern_Version and whose `capabilities` contains `tools`.
5. WHEN the `server/discover` assertions pass, THE Smoke_Test SHALL send a `tools/list` request carrying a Modern_Envelope at Latest_Modern_Version.
6. WHEN a modern `tools/list` response is received, THE Smoke_Test SHALL assert that the response is a JSON-RPC result containing `resultType`.
7. WHILE a `tools/list` response in either exchange contains `nextCursor`, THE Smoke_Test SHALL request the next page with that cursor and the same protocol era, and SHALL collect tool names across all pages.
8. WHEN the Modern_Exchange tool names are collected, THE Smoke_Test SHALL assert that the collected tool names equal the Expected_Tool_Set for the subprocess configuration.
9. WHEN a fresh Server subprocess starts for the Handshake_Exchange, THE Smoke_Test SHALL send an `initialize` request with `protocolVersion` set to Latest_Handshake_Version and SHALL assert that the response is a JSON-RPC result containing `serverInfo`, `capabilities.tools`, and `protocolVersion` equal to Latest_Handshake_Version.
10. WHEN the `initialize` response is received, THE Smoke_Test SHALL send `notifications/initialized` followed by a `tools/list` request.
11. WHEN the Handshake_Exchange tool names are collected, THE Smoke_Test SHALL assert that the collected tool names equal the Expected_Tool_Set for the subprocess configuration.
12. IF the Server does not return a response within 60 seconds of a request, THEN THE Smoke_Test SHALL fail and report the Server stderr output.
13. IF a Server response in either exchange is a JSON-RPC error, THEN THE Smoke_Test SHALL fail and report the protocol era, the request method, the error, and the Server stderr output.
14. WHEN the Smoke_Test finishes, THE Smoke_Test SHALL terminate the Server subprocess and release its pipes, on pass and on failure.
15. THE Smoke_Test SHALL run through `hatch test` and SHALL carry the `integration` pytest marker.
16. THE Smoke_Test SHALL run on Linux, macOS, and Windows.
17. THE Smoke_Test SHALL accept the Server launch command as a parameter, so that the MCPB_Job can run the same test against the Unpacked_Bundle_Dir.
18. THE Smoke_Test SHALL run both the Modern_Exchange and the Handshake_Exchange in the Test_Matrix_Job (source mode) and in the MCPB_Job (bundle mode).
19. THE Smoke_Test SHALL run without network access and without AWS credentials.

### Requirement 10: MCPB bundle CI job

**User Story:** As a maintainer, I want CI to install and run the MCP Bundle the way a desktop host does, so that a broken bundle is caught before release.

#### Acceptance Criteria

1. THE CI_Workflow SHALL define an MCPB_Job with a matrix over `macos-latest` and `windows-latest`.
2. THE MCPB_Job SHALL produce or obtain the MCPB_Bundle for the commit under test.
3. WHEN the MCPB_Job has the MCPB_Bundle, THE MCPB_Job SHALL run MCPB_CLI `validate` on the bundle manifest and SHALL fail on a nonzero exit status.
4. WHEN validation passes, THE MCPB_Job SHALL run MCPB_CLI `pack` and SHALL fail on a nonzero exit status.
5. WHEN packing completes, THE MCPB_Job SHALL unpack the MCPB_Bundle into an Unpacked_Bundle_Dir.
6. WHEN the MCPB_Bundle is unpacked, THE MCPB_Job SHALL run `uv sync` in the Unpacked_Bundle_Dir with a Supported_Version and SHALL fail on a nonzero exit status.
7. WHEN `uv sync` completes, THE MCPB_Job SHALL run the Smoke_Test against the Server launched from the Unpacked_Bundle_Dir.
8. IF any MCPB_Job step fails, THEN THE CI_Workflow run SHALL report failure.
9. THE MCPB_Job SHALL set `fail-fast: false` so a failure on one OS does not cancel the other OS.

### Requirement 11: Out of scope

**User Story:** As a maintainer, I want the scope bounded, so that pre-release interpreters land in a separate change.

#### Acceptance Criteria

1. THE Supported_Versions SHALL contain only CPython 3.13 and 3.14 for this change.
2. THE Hatch_Test_Matrix, Test_Matrix_Job, and MCPB_Job SHALL include no 3.15, 3.15t, or 3.16-dev interpreter.
