# Implementation Plan: python-version-upgrade

## Overview

Move every Python version site to the 3.13 floor and 3.14 default, fix the things that would break on Windows, and add a stdio smoke test plus a version-site guard test. The smoke test covers two MCP protocol eras, with one server launch per era. After that, extend `build.yml` with the 6-cell test matrix and the MCPB job, then update the docs, steering, and agent docs. Work that can be checked on this macOS machine comes first. Windows and Linux behavior is only checked once a branch is pushed, and pushing needs explicit user approval.

Language: Python (hatch-managed). Run all Python through hatch (`hatch test`, `hatch run ...`, with pytest flags after `--`). Never call `python`, `pytest`, `mypy`, or `ruff` directly, and never prefix commands with `cd`; use the tool's working-directory parameter instead. `hatch run python -c '…'` treats `{…}` as hatch context fields, so put ad-hoc probes in a temp script under the gitignored `private/` directory, run it with `hatch run python private/<file>.py`, and delete it afterwards.

## Tasks

- [ ] 1. Git setup (per `git-strategy.md`)
  - [x] 1.1 Check the current branch
    - Run `git branch --show-current` and `git status`. The checkout should be on `main`. Never commit to `main`; the feature branch is created from `main` in 1.3
    - The untracked spec directory carries over when you switch branches
    - Leave unrelated untracked files (for example `.mcp.json`) unstaged
    - _Requirements: all (process)_
  - [x] 1.2 Find or draft the GitHub issue
    - Search with `gh issue list --search "python 3.13" --state all` and `gh issue list --search "python 3.14" --state all`
    - If nothing related turns up, draft the issue title and body from `requirements.md` (summary, version-site table, CI matrix, two-era smoke test, MCPB job, agent docs, out of scope) and show it to the user. Create it with `gh issue create` only after the user approves
    - _Requirements: all (process)_
  - [-] 1.3 Create the feature branch from `main` and commit the spec files
    - After approval, create the branch from the issue, based on `main`: `gh issue develop <n> --base main --checkout` (or `git switch -c <n>-python-version-upgrade main`)
    - First commit: stage only `.kiro/specs/python-version-upgrade/{.config.kiro,requirements.md,design.md,tasks.md}`. The message must include `#<issue>` and say "spec only, untested"
    - Every later commit includes `#<issue>`, says whether the code was tested, is preceded by `hatch run tests`, and stages only files this feature touched. Don't push
    - _Requirements: all (process)_

- [ ] 2. Version sites and dependency re-lock
  - [~] 2.1 Update root `pyproject.toml`
    - Set `requires-python = ">=3.13"`. Set classifiers to `3`, `3.13`, and `3.14`, removing `3.11` and `3.12`
    - In `[tool.hatch.envs.default]`, set `python = "3.14"`. In `[tool.ruff]`, set `target-version = "py313"`
    - Change the `update` script to `--python-version 3.13`. Set the `[[tool.hatch.envs.hatch-test.matrix]]` list to `python = ["3.13", "3.14"]`
    - _Requirements: 1.1, 1.2, 1.3, 2.1, 2.2, 3.1, 4.1, 11.1, 11.2_
  - [~] 2.2 Update the packaging and runtime sites
    - `conf/mcpb/pyproject.toml`: `requires-python = ">=3.13"`
    - `conf/mcpb/manifest.json`: `compatibility.runtimes.python = ">=3.13"`. Keep `manifest_version` `0.4` and `server.type` `uv`
    - `conf/agentcore/Dockerfile`: `FROM public.ecr.aws/docker/library/python:3.14-slim`
    - `mise.toml`: `python = "3.14"`
    - _Requirements: 5.1, 5.2, 5.3, 5.4_
  - [~] 2.3 Re-lock dependencies
    - Run `hatch run update`. Confirm the `requirements.txt` header contains `--universal --python-version 3.13`
    - Diff the direct-dependency pins (the `[project] dependencies` and `devtest` entries) against `git show HEAD:requirements.txt`, and record each version change for the commit and PR body
    - If a dependency has no 3.13 or 3.14 wheel and doesn't build from source, stop and ask the user
    - _Requirements: 4.2, 4.3, 4.4_
  - [~] 2.4 Confirm typing and lint pass on the new target
    - Run `hatch run typing`, `hatch check fmt`, and `hatch check code`
    - Fix each new ruff finding, or suppress it with a targeted `# noqa: RULE - reason` (the design expects none)
    - _Requirements: 2.4, 3.2, 3.3_

- [ ] 3. Add `.gitattributes`
  - [~] 3.1 Create `.gitattributes` from design component 2
    - Put `* text=auto eol=lf` first, then `-text` for `src/mcp_server_for_oscal/oscal_schemas/**`, `data/**`, and `tests/fixtures/**` (last match wins)
    - Run `git ls-files --eol` and `git add --renormalize --dry-run .` (or check `git status` after `git add --renormalize .`) to confirm nothing gets renormalized. If something would be, stop and report it
    - _Requirements: 8.2, 9.16_

- [ ] 4. Read and write JSON as UTF-8
  - [~] 4.1 Add `encoding="utf-8"` to text-mode file I/O in the product code
    - `tools/utils.py`: `open(schema_path)` and the `hashes.json` read
    - `tools/oscal_store.py`: `BUNDLED_HASHES_PATH.read_text()` and the `open(file_path)` JSON load
    - `tools/validate_oscal_content.py`: `open(lf)` and the `NamedTemporaryFile(mode="w")` passed to `oscal-cli`
    - `tools/get_schema.py`: `open_schema_file` (text mode)
    - Grep `src/` for any other text-mode `open(`, `read_text(`, or `write_text(` that has no encoding, and fix the same way
    - _Requirements: 8.2_
  - [~] 4.2 Add `encoding="utf-8"` to `bin/update_hashes.py`
    - Apply it to every read and write of `hashes.json` and any other text I/O
    - Run `hatch run python bin/update_hashes.py src/mcp_server_for_oscal/oscal_schemas` (and the same for `data/oscal_docs` and `data/component_definitions`), then confirm `git diff` shows no change to the committed manifests. Don't use `hatch run rehash` here, because it runs `git add`
    - _Requirements: 8.2_

- [ ] 5. Windows test portability
  - [~] 5.1 Skip the chmod-based tests on Windows in `tests/test_file_integrity.py`
    - Add `@pytest.mark.skipif(sys.platform == "win32", reason="requires POSIX file permission bits (chmod 0o000)")` to the four sites that use `chmod(0o000)`
    - Keep the existing `test_exact_filename_matching_behavior` skip and the symlink `OSError` skip
    - _Requirements: 8.5_
  - [~] 5.2 Make the path assertions in `tests/tools/test_get_schema.py` (around lines 232 and 252) separator-agnostic
    - Replace `str(path).endswith("oscal_schemas/schema.json")` with `Path(call_args).parts[-2:] == ("oscal_schemas", "schema.json")`
    - _Requirements: 8.2_
  - [~] 5.3 Close every `OscalStore` before its temp dir is removed
    - In `tests/test_build_oscal_db.py` (around lines 323 and 371) and similar sites, close the store in `finally` or use a fixture that closes it
    - Grep `tests/` for `OscalStore(` inside `TemporaryDirectory`/`tmp_path` blocks to find them
    - _Requirements: 8.2_

- [~] 6. Checkpoint: local suite after the version and portability changes
  - Run `hatch run tests`. Ensure all tests pass, and ask the user if questions arise
  - Commit tasks 2–5 with `#<issue>`, the recorded pin changes, and "tested locally on macOS 3.13/3.14", staging only the files changed above

- [ ] 7. Stdio smoke test module (`tests/test_integration_stdio_smoke.py`)
  - All sub-tasks edit this one file. Import protocol versions and `_meta` key names from `mcp_types` (`LATEST_MODERN_VERSION`, `LATEST_HANDSHAKE_VERSION`, `PROTOCOL_VERSION_META_KEY`, `CLIENT_INFO_META_KEY`, `CLIENT_CAPABILITIES_META_KEY`). Never hard-code a version string or meta key
  - [~] 7.1 Implement the launch and environment helpers
    - `SMOKE_CMD_ENV`, `SMOKE_BUNDLE_ENV`, the frozen `LaunchSpec` dataclass, and `resolve_launch` (precedence: CMD, then BUNDLE_DIR, then source). Resolve a relative BUNDLE_DIR to an absolute path before substitution, since the server runs with `cwd=tmp_path`
    - `bundle_launch`: substitute `${__dirname}` from `server.mcp_config`, leave `${user_config.*}` env values literal, resolve argv[0] with `shutil.which`
    - `source_launch`: the console script in `sysconfig.get_path("scripts")` (`.exe` on win32), falling back to `shutil.which`
    - `sanitized_env`: drop `AWS_*`, `BEDROCK_*`, `VIRTUAL_ENV`, and `UV_CONSTRAINT`, then force the offline keys listed in the design
    - `pytest.fail` messages for a bad CMD JSON, a missing `manifest.json`, and a missing console script
    - Set `pytestmark = pytest.mark.integration`
    - _Requirements: 9.15, 9.17, 9.18, 9.19_
  - [~] 7.2 Property test: bundle launch substitutes the directory faithfully
    - **Property 3: Bundle launch substitutes the directory faithfully**
    - Generate directories with spaces, non-ASCII characters, and backslashes
    - **Validates: Requirements 9.17, 9.18, 10.7**
  - [~] 7.3 Property test: the sanitized environment is offline and deterministic
    - **Property 4: Sanitized environment is offline and deterministic**
    - **Validates: Requirements 9.19**
  - [~] 7.4 Implement `stamp_modern_envelope`
    - Define `Era = Literal["modern", "handshake"]`, `SMOKE_CLIENT_INFO`, and `SMOKE_CLIENT_CAPABILITIES = {}`
    - `stamp_modern_envelope(params, *, version=LATEST_MODERN_VERSION, client_info=..., capabilities=...)` returns a new dict. Copy `params` and any caller `_meta`, then set the three reserved keys from the `mcp_types` constants, overwriting caller values for them. Non-reserved `_meta` keys and other params (for example `cursor`) pass through. Never mutate the input
    - _Requirements: 9.2, 9.3, 9.5_
  - [~] 7.5 Property test: envelope stamping preserves params and always sets the envelope
    - **Property 5: Modern envelope stamping preserves params and always sets the envelope**
    - Deep-copy the input before the call to check non-mutation
    - **Validates: Requirements 9.2, 9.3, 9.5, 9.7**
  - [~] 7.6 Implement `read_response`, `SmokeTimeoutError`, and `SmokeProtocolError`
    - Strip the trailing `\r\n`/`\n` from each line (Windows stdout uses CRLF). Skip notifications and responses with other ids. Raise on non-JSON stdout lines and on EOF. Raise on timeout using an injectable `clock`. Every error message includes stderr
    - _Requirements: 9.12_
  - [~] 7.7 Property test: the response reader returns exactly the matching response
    - **Property 1: Response reader returns exactly the matching response**
    - **Validates: Requirements 9.1, 9.3, 9.5, 9.9, 9.10**
  - [~] 7.8 Property test: reader failures always carry stderr
    - **Property 2: Reader failures always carry stderr**
    - Use a fake clock so the test doesn't actually wait
    - **Validates: Requirements 9.12**
  - [~] 7.9 Implement `check_result` and `SmokeRpcError`
    - `check_result(response, *, era, method, stderr)` returns `response["result"]`
    - Raise `SmokeProtocolError` if `jsonrpc != "2.0"` or if the response has neither or both of `result` and `error`
    - Raise `SmokeRpcError` on an `error` response. The message contains the era, the method, the error object as JSON (code, message, data), and stderr
    - _Requirements: 9.13_
  - [~] 7.10 Property test: RPC error reports carry era, method, error, and stderr
    - **Property 6: RPC error reports carry era, method, error, and stderr**
    - **Validates: Requirements 9.13**
  - [~] 7.11 Implement `collect_tool_names`
    - `collect_tool_names(fetch_page, *, max_pages=50)`: call `fetch_page(None)`, then `fetch_page(cursor)` while a page has a non-null `nextCursor`. Return names in order
    - Raise `SmokeProtocolError` on a repeated cursor or after `max_pages` pages, naming the cursor
    - _Requirements: 9.7_
  - [~] 7.12 Property test: pagination collects every page in order
    - **Property 7: Pagination collects every page in order**
    - Use a fake `fetch` that records its calls. Include repeated-cursor chains
    - **Validates: Requirements 9.7, 9.8, 9.11**
  - [~] 7.13 Implement the `StdioServer` context manager
    - `Popen` with all three pipes and `bufsize=0`. Use `start_new_session=True` on POSIX and `CREATE_NEW_PROCESS_GROUP` on Windows
    - Daemon reader threads feed a stdout `Queue` and a locked stderr buffer
    - Implement `send`, `notify`, `wait_for(req_id, method, era)` (wraps `read_response`, with era and method in timeout and EOF messages), and `stderr_text`
    - `request(method, params, *, era)` assigns an id, sends, waits for that response before returning (stdin EOF drops in-flight responses), and passes it through `check_result`
    - `modern_request(method, params=None)` calls `request(method, stamp_modern_envelope(params), era="modern")`. `handshake_request(method, params=None)` calls `request(method, params or {}, era="handshake")`. The wrapper doesn't enforce one era per instance; the server's own error comes back through `check_result`
    - `__exit__`: close stdin, then `wait(10)`. If the process is still alive, use `killpg` with SIGTERM then SIGKILL on POSIX, or `taskkill /T /F` on Windows. Then join the threads and close the pipes. If the process can't be killed, log a warning
    - _Requirements: 9.1, 9.12, 9.13, 9.14, 9.16_
  - [~] 7.14 Unit tests for the helpers and cleanup
    - `resolve_launch` precedence and its failure messages
    - `stamp_modern_envelope` with defaults uses `LATEST_MODERN_VERSION` and the exact `mcp_types` key strings
    - `StdioServer` cleanup: start a child with `sys.executable -c` that ignores EOF and sleeps, raise inside the `with` block, then assert `poll() is not None` and that the pipes are closed. Don't skip on any OS
    - _Requirements: 9.2, 9.14, 9.17_
  - [~] 7.15 Implement `test_stdio_modern_discover_and_list_tools` (primary check)
    - Add `expected_tool_set()` (`{fn.__name__ for fn in get_tool_list()} | {"about"}`), the `launch_spec` fixture (`resolve_launch(os.environ)`), and `assert_tool_names` (no duplicates, equals the expected set, and in bundle mode also equals the manifest's `tools` names)
    - On its own launch, the first request is `srv.modern_request("server/discover")`. Assert `LATEST_MODERN_VERSION` is in `supportedVersions` and `tools` is in `capabilities`
    - Then list tools through `collect_tool_names` with `srv.modern_request("tools/list", ...)`, asserting `resultType` is present on every modern page, then call `assert_tool_names`
    - Run it locally in source mode with `hatch test tests/test_integration_stdio_smoke.py -- -k modern`
    - _Requirements: 9.1, 9.2, 9.3, 9.4, 9.5, 9.6, 9.7, 9.8, 9.15, 9.17, 9.18, 9.19_
  - [~] 7.16 Implement `test_stdio_handshake_initialize_and_list_tools` (legacy-compat check)
    - Use a fresh `StdioServer` launch with the same `launch_spec`
    - Send `srv.handshake_request("initialize", ...)` with `protocolVersion` set to `LATEST_HANDSHAKE_VERSION`, empty `capabilities`, and `SMOKE_CLIENT_INFO`. Assert `serverInfo.name`, `tools` in `capabilities`, and `protocolVersion == LATEST_HANDSHAKE_VERSION`
    - Send `notifications/initialized`, list tools through `collect_tool_names` with `srv.handshake_request("tools/list", ...)` (no `resultType` assertion), then call `assert_tool_names`
    - Run the whole module locally with `hatch test tests/test_integration_stdio_smoke.py`
    - _Requirements: 9.1, 9.2, 9.7, 9.9, 9.10, 9.11, 9.15, 9.18, 9.19_

- [ ] 8. Version-site guard test (`tests/test_python_version_sites.py`)
  - [~] 8.1 Implement the guard test with the standard library only (`tomllib`, `json`, `re`)
    - Assert the root `pyproject.toml` fields, the `requirements.txt` header, the `conf/mcpb/*` fields (including `manifest_version` `0.4` and `server.type` `uv`), the Dockerfile `FROM` line, and `mise.toml`
    - Assert that every `python-version:` value in `.github/workflows/*.yml` is in `SUPPORTED`
    - Stale-claim scan with the design's `STALE` regex over every text Version_Site, the Docs_Set, `AGENTS.md`, and `sorted(Path(".agents/summary").glob("*.md"))`, excluding `requirements.txt`. Start `ALLOWED_STALE` empty, and list `path:line: text` for every hit
    - Regex self-test: matches each current stale line from the requirements table, and doesn't match `0.3.12`, `3.13`, `3.14`, `13.12`, or `py313`
    - `REQUIRED_PHRASES`, parametrized per file, for each Docs_Set and Agent_Docs_Set file (`AGENTS.md`, `codebase_info.md`, `dependencies.md`, `index.md`, `workflows.md`), using the phrases in design component 5
    - The workflow assertions pass only after task 10, and the docs and agent-docs assertions only after task 11. Expect those to fail until then. Don't mark them xfail
    - _Requirements: 1.1, 1.2, 1.3, 2.1, 2.2, 3.1, 4.1, 4.2, 5.1, 5.2, 5.3, 5.4, 6.1, 6.2, 6.3, 6.4, 6.5, 6.6, 6.7, 6.8, 7.1, 7.2, 11.1, 11.2_

- [~] 9. Checkpoint: smoke test and guard test locally
  - Run `hatch test tests/test_integration_stdio_smoke.py tests/test_python_version_sites.py`. Both smoke-test eras should pass. The guard test should pass except for its workflow, docs, and agent-docs assertions. Ask the user if questions arise

- [ ] 10. CI workflow changes
  - [~] 10.1 `build.yml`: workflow defaults and the build interpreter
    - Add `defaults: run: shell: bash` at the workflow level. Set the `build` job's `setup-python` to install `3.13` and `3.14` (multi-line `python-version`, `3.14` last so it's the default), because `hatch run release` runs `hatch test --all`
    - Keep the `server.json` validation, `hatch run release`, and the coverage, python-packages, and mcpb uploads unchanged
    - _Requirements: 7.1, 7.3_
  - [~] 10.2 `build.yml`: add the `test` matrix job
    - `strategy.fail-fast: false`, `os: [ubuntu-latest, macos-latest, windows-latest]`, `python: ["3.13", "3.14"]`
    - Steps: checkout (with `fetch-depth: 0` for hatch-vcs), setup-python for the matrix version, `pip install -U hatch`, then `hatch test -py ${{ matrix.python }}`. This runs both smoke-test eras in source mode
    - _Requirements: 8.1, 8.2, 8.3, 8.4, 9.16, 9.18, 11.2_
  - [~] 10.3 `build.yml`: add the `mcpb` job
    - `needs: build`, `fail-fast: false`, `os: [macos-latest, windows-latest]`
    - Steps: checkout (with `fetch-depth: 0` for hatch-vcs), setup-python 3.13, setup-uv@v6, setup-node@v4, `pip install -U hatch`, then download the `mcpb` artifact to `dist/`
    - Then: `npx @anthropic-ai/mcpb unpack dist/*.mcpb staged/`, `validate staged/manifest.json`, `pack staged/ repacked.mcpb`, `unpack repacked.mcpb bundle/`, and `uv sync --frozen --directory bundle --python 3.13`
    - Finally, run `OSCAL_SMOKE_BUNDLE_DIR=bundle hatch test -py 3.13 tests/test_integration_stdio_smoke.py`, which runs both eras in bundle mode. Use the same `@anthropic-ai/mcpb` version as `bin/build_mcpb.py`
    - _Requirements: 9.18, 10.1, 10.2, 10.3, 10.4, 10.5, 10.6, 10.7, 10.8, 10.9, 11.2_
  - [~] 10.4 `build.yml`: extend `draft-release.needs` to `[build, test, mcpb]`
    - _Requirements: 7.4, 8.4, 10.8_
  - [~] 10.5 `update-awesome-oscal.yml`: set `setup-python` to `"3.14"`
    - _Requirements: 7.2_
  - [~] 10.6 Validate the workflow files locally
    - Run `hatch test tests/test_python_version_sites.py` and confirm the workflow assertions now pass
    - If `actionlint` is installed, run it on both files. Otherwise, say it wasn't run
    - _Requirements: 7.1, 7.2, 8.1, 10.1_

- [ ] 11. Docs, steering, and agent docs updates
  - [~] 11.1 `README.md` prerequisites (around line 403)
    - State Python 3.13 or higher, `uv python install 3.14`, and "we test 3.13 & 3.14"
    - _Requirements: 6.1, 6.2_
  - [~] 11.2 `CONTRIBUTING.md` (around line 62): "pytest on Python 3.13 and 3.14"
    - _Requirements: 6.1, 6.3_
  - [~] 11.3 `conf/powers/oscal/POWER.md` (around lines 202 and 355): "Python 3.13 or higher"
    - _Requirements: 6.1_
  - [~] 11.4 `.kiro/steering/tech.md`
    - Lines 5 and 70: Python 3.13+, tested on 3.13 and 3.14, default dev environment 3.14, ruff target Python 3.13
    - _Requirements: 6.1, 6.4_
  - [~] 11.5 `.kiro/steering/hatch.md`
    - Change the `-py 3.12` example to `-py 3.14`. Update the notes to matrix 3.13, 3.14 and default Python 3.14
    - _Requirements: 6.1, 6.5_
  - [~] 11.6 Update the Agent_Docs_Set
    - `AGENTS.md`: line ~85 `(3.11 + 3.12)` → `(3.13 + 3.14)`. Line ~112 "on Python 3.12" → "on Python 3.14", and add that the `test` job runs `hatch test` on Linux, macOS, and Windows × 3.13/3.14 and the `mcpb` job checks the bundle on macOS and Windows
    - `.agents/summary/codebase_info.md` (~12): `>=3.13`, CI matrix 3.13 and 3.14 on Linux, macOS, Windows, default dev env 3.14, `mise.toml` pins 3.14
    - `.agents/summary/dependencies.md`: ~38 "pins python 3.14", ~48 `--python-version 3.13`
    - `.agents/summary/index.md` (~14): "Python 3.13+"
    - `.agents/summary/workflows.md`: ~48 "pytest matrix 3.13/3.14", ~54 "(universal, py3.13)"
    - Grep `.agents/summary/*.md` for any other `3.11`/`3.12` Python-version mention and fix it the same way
    - Use the exact phrases the guard test's `REQUIRED_PHRASES` expects
    - _Requirements: 6.6, 6.7, 6.8_

- [~] 12. Final checkpoint: full local verification
  - Run `hatch run tests`, `hatch check fmt`, and `hatch check code`. All must exit 0, including every guard-test assertion
  - Run `hatch build`, then read `METADATA` from the wheel in `dist/` (for example `unzip -p dist/*.whl '*/METADATA'`) and confirm it contains `Requires-Python: >=3.13`
  - Remove any temporary files, including probe scripts under `private/`. Commit tasks 7–11 with `#<issue>` and "tested locally on macOS; Windows/Linux CI unverified", staging only feature files
  - Ensure all tests pass, and ask the user if questions arise
  - _Requirements: 1.4, 2.3, 2.4, 3.2_

- [ ] 13. Cross-platform CI verification (needs user approval)
  - [~] 13.1 Ask the user for explicit approval to push the feature branch
    - The Linux and Windows matrix cells and both MCPB cells (CRLF handling, file locking, `taskkill` cleanup, `uv` on Windows, both smoke-test eras in bundle mode) can only be verified by a CI run on a pushed branch. Don't push without approval
    - After an approved push and CI run, triage Windows failures with the design-component-3 policy: fix code or assertions that assume POSIX, and skip a test only when it needs a POSIX-only facility, naming that facility in the reason. Each fix is a new commit with `#<issue>`
    - _Requirements: 8.1, 8.4, 8.5, 9.16, 9.18, 10.1, 10.8_

## Notes

- Tasks marked `*` are optional property and unit tests. The two smoke tests (7.15, 7.16) and the guard test (8.1) are required deliverables, so they aren't marked optional.
- All of task 7 edits `tests/test_integration_stdio_smoke.py`, and 10.1–10.4 all edit `build.yml`, so the graph puts each of those sub-tasks in its own wave.
- Tasks 1.2 (issue creation), 1.3 (branch and first commit), and 13.1 (push) block on user approval. No push happens without it.
- For ad-hoc probes, use a temp script under `private/` (not `hatch run python -c` with `{…}`), and delete it when done.

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0, "tasks": ["1.1"] },
    { "id": 1, "tasks": ["1.2"] },
    { "id": 2, "tasks": ["1.3"] },
    { "id": 3, "tasks": ["2.1", "2.2", "3.1", "4.1", "4.2", "5.1", "5.2", "5.3"] },
    { "id": 4, "tasks": ["2.3"] },
    { "id": 5, "tasks": ["2.4"] },
    { "id": 6, "tasks": ["7.1", "8.1"] },
    { "id": 7, "tasks": ["7.2", "10.1", "10.5", "11.1", "11.2", "11.3", "11.4", "11.5", "11.6"] },
    { "id": 8, "tasks": ["7.3", "10.2"] },
    { "id": 9, "tasks": ["7.4", "10.3"] },
    { "id": 10, "tasks": ["7.5", "10.4"] },
    { "id": 11, "tasks": ["7.6", "10.6"] },
    { "id": 12, "tasks": ["7.7"] },
    { "id": 13, "tasks": ["7.8"] },
    { "id": 14, "tasks": ["7.9"] },
    { "id": 15, "tasks": ["7.10"] },
    { "id": 16, "tasks": ["7.11"] },
    { "id": 17, "tasks": ["7.12"] },
    { "id": 18, "tasks": ["7.13"] },
    { "id": 19, "tasks": ["7.14"] },
    { "id": 20, "tasks": ["7.15"] },
    { "id": 21, "tasks": ["7.16"] },
    { "id": 22, "tasks": ["13.1"] }
  ]
}
```
