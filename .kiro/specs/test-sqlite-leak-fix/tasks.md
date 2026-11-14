# Implementation Plan

GitHub issue #17. Rules for every task:

- Run all Python through hatch (`hatch test -- <pytest args>`, `hatch run typing`, `hatch run tests`, `hatch check fmt --fix`, `hatch check code --fix`). Never call `python`, `pytest`, `mypy`, `ruff`, or `bandit` directly. Put pytest flags after `--`.
- Never prefix a command with `cd`. Pass the repository root `/Users/dfk/workplace/mcp-server-for-oscal` as the tool's `cwd`.
- Repeat these two rules in the prompt of any subagent you delegate to.
- Do not delete the stale `./MagicMock/` directory. It is gitignored and its removal is the user's decision.
- Exploration and regression tests call `monkeypatch.chdir(tmp_path)` so a leak on unfixed code lands in `tmp_path`, not in the checkout.

- [x] 1. Write bug condition exploration test
  - **Property 1: Bug Condition** - Mock-derived database path reaches the filesystem
  - **Before starting**: confirm `git branch --show-current` is not `main`. If it is, create the feature branch from issue #17 with `gh issue develop 17 --checkout` (or ask the user), and make the first commit on that branch contain the spec files (`.kiro/specs/test-sqlite-leak-fix/`), staged by name. Do not commit to `main`. Do not push.
  - **CRITICAL**: This test MUST FAIL on unfixed code. Failure confirms the bug exists.
  - **DO NOT attempt to fix the test or the code when it fails**
  - **NOTE**: This test encodes the expected behavior. It validates the fix when it passes after implementation.
  - **GOAL**: Surface counterexamples that show a truthy non-`str` database path (a `MagicMock`) creating `MagicMock/<mock-name>/<id>` under the working directory
  - **Scoped PBT Approach**: The bug is deterministic. Scope the property to truthy non-`str` values (`MagicMock` instances, integers from 1, non-empty bytes, non-empty lists) for `OscalStore(db_path=value)`, plus the two concrete `MagicMock` cases below.
  - Bug Condition from design: `isBugCondition(input)` holds when `config_value` is truthy, not a `str`, has a relative `os.fspath`, and `OscalStore` is constructed with it
  - Add to `tests/tools/test_oscal_store.py` (new class, for example `TestRejectsNonStrDbPath`):
    - `OscalStore(db_path=MagicMock())` with `monkeypatch.chdir(tmp_path)`: expect `pytest.raises(TypeError)` and `list(tmp_path.iterdir()) == []`
    - Config fallback: `monkeypatch.setattr` the `config` used by `oscal_store` so `oscal_store_db_path` is a `MagicMock()`, then `OscalStore(db_path=None)`: same assertions
    - Hypothesis property over `st.one_of` of the truthy non-`str` values above: `OscalStore(db_path=value)` raises `TypeError` and leaves the chdir'd directory empty. Use a modest `max_examples` and a fresh temp directory per example, as Property 14 does.
  - Add to `tests/test_main.py` (new class `TestStoreInitWithMockedConfig`): `monkeypatch.chdir(tmp_path)`, `patch("mcp_server_for_oscal.main.config")` with no store attributes set, call `_init_oscal_store()`. Assert `list(tmp_path.iterdir()) == []`, one warning is logged (`caplog`), and `query_documentation._store` is unchanged. Restore the tool-module singletons the way `TestStartupStoreWiring` does.
  - Run on UNFIXED code: `hatch test -- tests/tools/test_oscal_store.py tests/test_main.py -k "NonStr or StoreInitWithMockedConfig"`
  - **EXPECTED OUTCOME**: Tests FAIL (this is correct, it proves the bug exists)
  - Document the counterexamples, for example `MagicMock/config.oscal_store_db_path/<id>` appearing in `tmp_path` and no `TypeError` raised
  - Mark the task complete when the tests are written, run, and the failure is documented. Record the result with `update_pbt_status`.
  - _Requirements: 1.1, 1.2, 2.1_

- [x] 2. Write preservation property tests (BEFORE implementing fix)
  - **Property 2: Preservation** - Store resolution and `main()` behavior for real paths
  - **IMPORTANT**: Follow the observation-first methodology
  - Observe on UNFIXED code, for inputs where `isBugCondition` is false (`db_path` is a `str`, `""`, or `None`):
    - `OscalStore(db_path=<str under tmp_path>)` with an existing SQLite file: mode `persistent`, path returned unchanged
    - `OscalStore(db_path=<str under tmp_path>)` with a missing file: seeded from the bundled database, or a fresh database when seeding is disabled or not possible
    - `OscalStore(db_path="")` and `db_path=None` with an empty `config.oscal_store_db_path`: bundled mode or ephemeral fallback, same as unset
    - `_init_oscal_store()` with `oscal_store_db_path` as a `str` or `None` (with `OscalStore` patched) wires the store as `TestStartupStoreWiring` and `TestStartupWiring` assert
  - Write property-based tests in `tests/tools/test_oscal_store.py` that capture the observed behavior:
    - For any `str` path under a fresh temp directory (existing empty SQLite file, or missing with `seed_from_bundled=False`), the resolved mode is `persistent` and the path is unchanged. This extends Property 14 (`test_mode_resolution_is_deterministic`) to the exact inputs the new check must not reject.
    - For `""` and `None` with empty config, mode resolution equals the observed bundled or ephemeral mode.
  - Run the existing baseline suites unchanged: `hatch test -- tests/tools/test_oscal_store.py tests/test_build_oscal_db.py tests/test_main.py tests/test_file_integrity_integration.py tests/tools/test_local_doc_search.py`
  - Run the new preservation tests on UNFIXED code
  - **EXPECTED OUTCOME**: Tests PASS (this confirms the baseline behavior to preserve)
  - Mark the task complete when the tests are written, run, and passing on unfixed code. Record the result with `update_pbt_status`.
  - _Requirements: 3.1, 3.2, 3.3, 3.4, 3.5_

- [x] 3. Fix for test-suite SQLite leak into `./MagicMock/`

  - [x] 3.1 Reject non-`str` database paths in `OscalStore`
    - In `src/mcp_server_for_oscal/tools/oscal_store.py`, `OscalStore._resolve_db_path`: after the `config.oscal_store_db_path` fallback and before `_resolve_persistent`, add `if effective_path and not isinstance(effective_path, str): raise TypeError(f"OscalStore db_path must be a str, got {type(effective_path).__name__}")`
    - Falsy values (`None`, `""`) keep their meaning "unset". Do not use `os.PathLike` as the test, because `MagicMock` is one. The message names the type and never the value, so no path text reaches a message.
    - Add `Raises: TypeError` to the `OscalStore.__init__` docstring and keep the existing `RuntimeError` entry
    - Run `hatch run typing`. `effective_path` is typed `str | None`, so the branch may be flagged unreachable. If so, annotate the check parameter as `object` or add a targeted `# type: ignore[unreachable]` with a reason.
    - No change to `src/mcp_server_for_oscal/main.py`. `_init_oscal_store` already catches the exception, logs a warning, and installs no store.
    - Add unit tests in `tests/tools/test_oscal_store.py`: `MagicMock()`, `123`, `b"x.db"`, `["a"]`, and `pathlib.Path("x.db")` raise `TypeError`; the message contains the type name and no path text; `""` and `None` do not raise
    - _Bug_Condition: isBugCondition(input) where config_value is truthy, not a str, and reaches OscalStore_
    - _Expected_Behavior: OscalStore raises TypeError before any filesystem access; _init_oscal_store logs a warning, creates no file, and installs no store_
    - _Preservation: str, "", and None paths resolve as before (3.1, 3.2); _init_oscal_store failure handling is unchanged (3.3)_
    - _Requirements: 2.1, 2.2, 3.1, 3.2, 3.3_

  - [x] 3.2 Patch `_init_oscal_store` in the 13 affected tests
    - `tests/test_main.py`, class `TestMain`: add an autouse fixture `skip_store_init` next to `skip_integrity_check` that yields inside `patch("mcp_server_for_oscal.main._init_oscal_store")`. Docstring: these tests cover CLI, transport, and logging; store wiring is covered by `TestStartupStoreWiring`.
    - `tests/test_file_integrity_integration.py`, class `TestFileIntegrityIntegration`: add the same autouse fixture, independent of `setup_method` and `teardown_method`
    - Do not change any test body or assertion. The 13 tests are nine in `TestMain` (`test_main_default_arguments`, `test_main_with_arguments`, `test_main_mcp_server_error`, `test_main_logger_configuration`, `test_main_argument_parser_setup`, `test_main_with_invalid_log_level`, `test_main_with_streamable_http_transport`, `test_main_logs_transport_on_startup`, `test_main_logs_specific_transport_method`) and four in `TestFileIntegrityIntegration` (`test_successful_server_startup_with_valid_packages`, `test_verification_of_oscal_schemas_directory_only`, `test_integrity_check_path_resolution_for_oscal_schemas`, `test_server_continues_after_successful_integrity_checks`).
    - Add one test that exercises initialization with a real store and a mocked config: `oscal_store_db_path = ""`, `oscal_documents_dir = ""`, chdir to `tmp_path`, assert bundled mode and that `tmp_path` stays empty
    - _Bug_Condition: isBugCondition(input) where main() runs with a bare MagicMock config and _init_oscal_store is unpatched_
    - _Expected_Behavior: no mock-derived value reaches OscalStore; nothing is created under the working directory_
    - _Preservation: the 13 tests keep their assertions (3.4); TestStartupStoreWiring, TestStartupWiring, and TestAboutUserDocumentsLoaded pass unedited (3.5)_
    - _Requirements: 2.1, 2.2, 3.4, 3.5_

  - [x] 3.3 Add the Leak_Guard
    - Create `tests/leak_guard.py` in the style of `tests/fixture_store.py`, with pure functions: `LEAK_DIR_NAME = "MagicMock"`; `fingerprint(root: Path) -> tuple` returning `(False, None, ())` when `root / "MagicMock"` is absent, else `(True, mtime_ns, sorted((child_name, child_mtime_ns) ...))` for immediate subdirectories only, using `os.scandir` and `entry.stat()`; `describe_change(before, after) -> str | None` returning `None` when equal, else a message that names `MagicMock` (created or modified) and ends with the cleanup hint to remove the gitignored `./MagicMock/` and to patch `main._init_oscal_store` or set `config.oscal_store_db_path` to a `tmp_path` string
    - In `tests/conftest.py`, add an autouse function-scoped fixture `no_mock_path_leak(request)`: read `root = request.config.invocation_params.dir` once, take the fingerprint before, yield, take it after, and `pytest.fail(message, pytrace=False)` when `describe_change` returns a message. The guard never deletes anything.
    - Create `tests/test_leak_guard.py`, against `tmp_path` as root: absent directory gives equal fingerprints; creating `MagicMock/x/file` changes it and the message contains `MagicMock`; a pre-existing `MagicMock/x/` with 50 files is unchanged across a no-op; adding a file to a pre-existing subdirectory changes it; removing the directory changes it; `describe_change(a, a)` is `None`; the teardown logic raises `pytest.fail.Exception` with `MagicMock` in the message for a changed fingerprint
    - **Property 3: Detection** - add a Hypothesis property: for any sequence of create, modify, and delete operations on a temp root under `MagicMock/<name>/`, `describe_change` is `None` exactly when the fingerprints are equal
    - Out of scope: a generic "no new file in the working directory" guard, deleting the existing `./MagicMock/`, and changes to `.gitignore`, `Config`, `main.py`, CI workflows, or the tool registry
    - _Requirements: 1.3, 2.3_

  - [x] 3.4 Verify bug condition exploration test now passes
    - **Property 1: Expected Behavior** - No mock-derived database path reaches the filesystem
    - **IMPORTANT**: Re-run the SAME tests from task 1. Do NOT write new tests.
    - Run `hatch test -- tests/tools/test_oscal_store.py tests/test_main.py -k "NonStr or StoreInitWithMockedConfig"`
    - **EXPECTED OUTCOME**: Tests PASS (confirms the bug is fixed). Record the result with `update_pbt_status`.
    - _Requirements: 2.1, 2.2_

  - [x] 3.5 Verify preservation tests still pass
    - **Property 2: Preservation** - Store resolution and `main()` behavior for real paths
    - **IMPORTANT**: Re-run the SAME tests from task 2. Do NOT write new tests.
    - Run `hatch test -- tests/tools/test_oscal_store.py tests/test_build_oscal_db.py tests/test_main.py tests/test_file_integrity_integration.py tests/tools/test_local_doc_search.py`
    - Confirm `TestStartupStoreWiring`, `TestStartupWiring`, and `TestAboutUserDocumentsLoaded` pass without any edit
    - **EXPECTED OUTCOME**: Tests PASS (confirms no regressions). Record the result with `update_pbt_status`.
    - _Requirements: 3.1, 3.2, 3.3, 3.4, 3.5_

  - [x] 3.6 Verify the guard and the production startup path
    - Guard check on the 13 tests: `find MagicMock -type f | wc -l` before and after `hatch test -- tests/test_main.py tests/test_file_integrity_integration.py`. The count must stay at its starting value (3,194 on this checkout unless the user has removed the directory). A stale directory must not fail the run.
    - Optional guard demonstration, only if the user agrees: temporarily remove the new autouse fixture from one class and confirm the guard reports an ERROR naming `MagicMock` on that test, then restore it. Do not leave the temporary change in place.
    - Run `hatch test -- tests/test_integration_stdio_smoke.py`. The real server still starts with the real `Config` (empty `OSCAL_STORE_DB_PATH`) and answers `tools/list`.
    - _Requirements: 2.2, 2.3, 3.3_

- [-] 4. Checkpoint - Ensure all tests pass
  - Run `hatch check fmt --fix` and `hatch check code --fix`, and review any changes they make
  - Run `hatch run tests` (mypy, `hatch test --exitfirst --all --cover` on 3.13 and 3.14, bandit). All must pass.
  - Re-check requirement 2.2: if `./MagicMock/` did not exist before the run, it must still be absent. Otherwise its file count must be unchanged. `git status --ignored --short` shows no new entries beyond `private/`, `.hypothesis/`, `.coverage*`, and caches.
  - Confirm the feature branch is not `main`. Stage only the files this fix created or modified: `src/mcp_server_for_oscal/tools/oscal_store.py`, `tests/conftest.py`, `tests/leak_guard.py`, `tests/test_leak_guard.py`, `tests/test_main.py`, `tests/test_file_integrity_integration.py`, `tests/tools/test_oscal_store.py`, and `.kiro/specs/test-sqlite-leak-fix/` (including this `tasks.md`).
  - Commit with `#17` in the message and state that the tests passed. Do not commit to `main`. Do not push without explicit user approval.
  - Tell the user that `./MagicMock/` still holds the stale files and can be removed with `rm -rf MagicMock` from the repository root when they are ready.
  - Ensure all tests pass, ask the user if questions arise.
