# Test SQLite Leak Fix Bugfix Design

## Overview

GitHub issue #17. Thirteen tests call `main()` with `mcp_server_for_oscal.main.config` replaced by a bare `MagicMock` and leave `_init_oscal_store()` unpatched. The function reads `config.oscal_store_db_path`, which is an auto-created, truthy `MagicMock` attribute. `main._init_oscal_store` passes it to `OscalStore(db_path=...)`. `OscalStore._resolve_db_path` treats any truthy value as an explicit persistent path, and `Path(MagicMock)` works because `MagicMock` implements `os.PathLike`. The path form is `MagicMock/<mock-name>/<id>`, relative to the working directory. The store creates the directories and copies the 8 MB bundled database into them, once per affected test per run.

The fix has three layers, each independent of the others:

1. **Remove the cause in the tests (primary).** Each affected test class gets an autouse fixture that patches `mcp_server_for_oscal.main._init_oscal_store`, the pattern `TestStartupIntegrity` and `TestDeprecatedSettings` already use. These tests cover CLI, transport, logging, and integrity handling, not store initialization (requirement 2.1).
2. **Reject non-string database paths in `OscalStore` (defense in depth).** A truthy `db_path` that is not a `str` raises `TypeError` before any filesystem access. `_init_oscal_store` already catches any exception, logs a warning, and continues, so a future test with a bare mocked config cannot leak even if it forgets to patch.
3. **Detect regressions (requirement 2.3).** An autouse fixture in `tests/conftest.py` records the state of `./MagicMock/` before each test and fails the test at teardown, naming the directory, if the state changed.

Facts verified during design:

- `os.fspath(MagicMock())` returns `MagicMock/<mock-name>/<id>`. `MagicMock` is an `os.PathLike`; a plain `Mock` is not, so a bare `Mock` config cannot leak this way. (`hatch run python -c`, `mock.a` gave `MagicMock/mock.a/4412934192`.)
- `./MagicMock/config.oscal_store_db_path/` currently holds 3,194 files and is the only subdirectory, which matches the report in `bugfix.md`.
- Thirteen tests reach `_init_oscal_store()` through a mocked `config`: nine in `tests/test_main.py::TestMain` (`test_main_default_arguments`, `test_main_with_arguments`, `test_main_mcp_server_error`, `test_main_logger_configuration`, `test_main_argument_parser_setup`, `test_main_with_invalid_log_level`, `test_main_with_streamable_http_transport`, `test_main_logs_transport_on_startup`, `test_main_logs_specific_transport_method`) and four in `tests/test_file_integrity_integration.py::TestFileIntegrityIntegration` (`test_successful_server_startup_with_valid_packages`, `test_verification_of_oscal_schemas_directory_only`, `test_integrity_check_path_resolution_for_oscal_schemas`, `test_server_continues_after_successful_integrity_checks`). The other tests in those classes exit before the store step (`--help`, invalid transport, integrity failure).
- Every other test that touches `_init_oscal_store` either patches it (`TestStartupIntegrity`, `TestDeprecatedSettings`, `test_oscal_agent.py`, `test_metaschema_integrity.py`) or sets `oscal_store_db_path` to a `str` (`TestStartupStoreWiring`, `TestStartupWiring`, `TestAboutUserDocumentsLoaded`) or to `None` with `OscalStore` patched (`TestStartupWiring`).
- Side effect of the bug: these tests also install a real store into `list_oscal_resources._store` and `query_documentation._store`, which the autouse `reset_oscal_store` fixture does not reset. Patching `_init_oscal_store` removes that cross-test state.
- Directory modification time changes when entries are added or removed, including SQLite's `-wal` and `-shm` files. This lets the guard detect a leak without listing the 3,194 stale files.

## Glossary

- **Bug_Condition (C)**: `_init_oscal_store()` runs while `config.oscal_store_db_path` is truthy and not a `str`, and `os.fspath` of it is a relative path. Today only a `MagicMock` produces this.
- **Property (P)**: No file or directory is created under the working directory, and no store is built from a mock-derived path.
- **Preservation**: Behavior of `OscalStore` and `_init_oscal_store` for `str`, empty, and `None` paths, and everything the `main()` tests assert.
- **`_init_oscal_store`**: function in `src/mcp_server_for_oscal/main.py` that builds the `OscalStore`, scans `oscal_documents_dir`, and installs the store on the tool modules. It catches every exception and logs a warning.
- **`OscalStore._resolve_db_path`**: method in `src/mcp_server_for_oscal/tools/oscal_store.py` that picks persistent, bundled, or ephemeral mode from `db_path` and `config.oscal_store_db_path`.
- **Leak_Guard**: the autouse fixture and helper that fail a test when it creates or modifies `./MagicMock/`.
- **Leak_Root**: the pytest invocation directory (`request.config.invocation_params.dir`), where a relative leak lands.
- **Fingerprint**: for a Leak_Root, the tuple `(exists, mtime_ns of MagicMock, {child dir name: mtime_ns})`. It costs one `stat` and one `scandir` of a short list.

## Bug Details

### Bug Condition

The bug manifests when a test runs `_init_oscal_store()`, directly or through `main()`, with `config` replaced by a `MagicMock` whose `oscal_store_db_path` was never assigned. `config.oscal_store_db_path or None` keeps the truthy mock, and `OscalStore._resolve_db_path` takes the `if effective_path:` branch into `_resolve_persistent`. `Path(db_path)` accepts the `PathLike` mock, `p.exists()` is false for the fresh `id`, `p.parent.mkdir(parents=True)` creates `MagicMock/<mock-name>/`, and `shutil.copy2` writes the bundled database there.

**Formal Specification:**
```
FUNCTION isBugCondition(input)
  INPUT: input of type StoreInitCall
         (config_value: any, cwd: Path, init_via: "_init_oscal_store" | "main")
  OUTPUT: boolean

  RETURN input.config_value is truthy
         AND NOT isinstance(input.config_value, str)
         AND fspath_is_relative(input.config_value)
         AND OscalStore is constructed with input.config_value
END FUNCTION
```

### Examples

- `main()` with `patch("mcp_server_for_oscal.main.config")` and `oscal_store_db_path` unset: creates `./MagicMock/config.oscal_store_db_path/<id>` (8 MB copy). Expected: nothing created.
- Running the 13 affected tests 10 times: 130 new files, about 1 GB. Expected: zero.
- `_init_oscal_store()` with a bare `MagicMock` config and `OscalStore` not patched. Today: a store backed by the leaked file is installed on four tool modules. Expected: no file, a logged warning, no store installed.
- Edge case, `oscal_store_db_path = None` with `OscalStore` patched (`TestStartupWiring`): `None or None` is `None`, no filesystem access. Expected: unchanged.
- Edge case, `oscal_store_db_path = str(tmp_path / "store.db")`: persistent mode under `tmp_path`. Expected: unchanged.
- Edge case, a test chdirs into `tmp_path` and leaks there: harmless and out of the guard's scope.

## Expected Behavior

### Preservation Requirements

**Unchanged Behaviors:**
- `OscalStore(db_path=<str>)` opens an existing file as a persistent database, seeds a missing file from the verified bundled database, and creates a fresh database when seeding is not possible (3.1).
- `OscalStore(db_path=None)` with an empty `config.oscal_store_db_path` uses bundled mode or falls back to ephemeral mode. `db_path=""` stays equivalent to unset (3.2).
- `_init_oscal_store()` with the real `Config` builds the store from `oscal_store_db_path` and `oscal_store_cache_size`, scans `oscal_documents_dir` with Origin `user`, installs the store on every tool module, and logs a warning without exiting on failure (3.3).
- The 13 tests keep asserting the same things: `update_from_args` arguments, transport validation, logging configuration, integrity calls and exit code 2, and `mcp.run` arguments (3.4).
- `TestStartupStoreWiring`, `TestStartupWiring`, and `TestAboutUserDocumentsLoaded` pass without any edit (3.5).

**Scope:**
All inputs where `config.oscal_store_db_path` is a `str`, `""`, or `None` are unaffected. This includes:
- Production runs, where `Config.oscal_store_db_path` is always a `str` (`os.getenv(..., "")`).
- Tests that build a store from a `tmp_path` string.
- Tests that patch `_init_oscal_store` or `OscalStore`.

## Hypothesized Root Cause

The cause is confirmed by the directory name `MagicMock/config.oscal_store_db_path/` and the numeric file names (`id()` values of the mock). Contributing factors:

1. **Test setup**: `patch("mcp_server_for_oscal.main.config")` yields a `MagicMock` whose unassigned attributes are truthy `MagicMock`s. The 13 tests set `aws_profile`, `log_level`, and `transport`, never the store attributes, because they test CLI and transport handling and do not intend to initialize a store.
2. **No type check at the filesystem boundary**: `OscalStore._resolve_db_path` tests only truthiness, and `Path(...)` accepts any `os.PathLike`. A `MagicMock` passes both.
3. **No leak detection**: nothing compares the working directory before and after a test, and `MagicMock` is gitignored, so `git status` stays clean while the disk fills.
4. **Rejected hypothesis, `oscal_documents_dir`**: the same mock gives `Path(config.oscal_documents_dir)` a relative path, but it is joined onto the package directory, does not exist, and is only logged. It creates nothing. Tests that do exercise store initialization should still set it to `""` so the intent is explicit.

## Correctness Properties

Property 1: Bug Condition - No mock-derived database path reaches the filesystem

_For any_ call to `_init_oscal_store()` or `OscalStore(...)` where `isBugCondition` holds (the database path is a truthy non-`str`, such as a `MagicMock`), the fixed code SHALL raise `TypeError` from `OscalStore` (caught and logged by `_init_oscal_store`), create no file or directory under the working directory, and install no store. Every test that calls `main()` with a mocked `config` SHALL either patch `_init_oscal_store` or set `oscal_store_db_path` to a `str` under `tmp_path` or to `""`.

**Validates: Requirements 2.1, 2.2**

Property 2: Preservation - Store resolution and `main()` behavior for real paths

_For any_ input where the bug condition does NOT hold (`oscal_store_db_path` is a `str`, `""`, or `None`), the fixed `OscalStore` and `_init_oscal_store` SHALL produce the same database mode, path, and installed stores as the original code, and every `main()` test SHALL assert the same call arguments and exit codes as before.

**Validates: Requirements 3.1, 3.2, 3.3, 3.4, 3.5**

Property 3: Detection - Leaks into `./MagicMock/` fail the suite

_For any_ test that creates `./MagicMock/` or adds, removes, or modifies an entry in it (measured by directory modification times of `MagicMock/` and its immediate subdirectories), the Leak_Guard SHALL fail that test at teardown with a message that names `MagicMock`. _For any_ test that leaves those modification times unchanged, including when a stale `./MagicMock/` already exists, the guard SHALL pass.

**Validates: Requirements 2.3, 1.3**

## Fix Implementation

### Design Decisions

**Decision 1: guard timing. Per-test, not session end.**

| Criterion | Per-test (autouse fixture) | Session end (`pytest_sessionfinish`) |
|---|---|---|
| Names the offending test | Yes, the error is attached to it | No, only "something leaked" |
| Stale `./MagicMock/` already on disk (this checkout has 3,194 files) | Compares before/after each test, so a stale directory does not fail the run | Needs a session-start baseline and a way to report the culprit |
| Visibility in CI | Test ERROR line with the nodeid | Exit-status change or a late message that is easy to miss |
| Cost | Two `stat` plus two short `scandir` calls per test, microseconds | One |
| Fits `-k`, single-test, and `hatch test` selections | Yes | Yes |

Chosen: per-test. The cost is negligible and attribution turns a disk-fill into a one-line CI failure naming the test. The guard only fails the test; it never deletes anything. A session-end check adds nothing once every test is checked.

**Decision 2: reject non-string paths in `OscalStore`, once, at the choke point.**

| Option | Result |
|---|---|
| A. Tests only (patch `_init_oscal_store`) plus guard | Fixes today's 13 tests. A new test with a bare mocked config reproduces the leak and is caught only after the fact, by the guard. |
| B. Type check in `_init_oscal_store` only | Covers `main()` and the agent, not other `OscalStore` callers. Needs `str \| None` special-casing to keep `TestStartupWiring` (`oscal_store_db_path = None`) unchanged. |
| C. Type check in `OscalStore._resolve_db_path` (chosen) | One check where the path first touches the filesystem. Covers the `db_path` argument and the `config.oscal_store_db_path` fallback, and therefore `main()`, the agent, and any future caller. |

Chosen: A plus C. The check is three lines. It does not change production behavior, because `Config.oscal_store_db_path` is always a `str`. It accepts `str` only, which matches the annotation `db_path: str | None`; `_resolve_persistent` returns `db_path` unchanged as `str`, so a `Path` was never supported. `os.PathLike` cannot be the test, because `MagicMock` is one. The error text names the type, never the value, to keep the no-path-in-messages rule. Option B is subsumed by C. This goes slightly beyond the literal text of requirements 2.1, so the task list should record it explicitly; no requirement conflicts with it.

### Changes Required

**File**: `src/mcp_server_for_oscal/tools/oscal_store.py`

**Function**: `OscalStore._resolve_db_path`

**Specific Changes**:
1. **Validate the effective path** after the config fallback and before `_resolve_persistent`:
   ```python
   if effective_path and not isinstance(effective_path, str):
       raise TypeError(
           f"OscalStore db_path must be a str, got {type(effective_path).__name__}"
       )
   ```
   Falsy values (`None`, `""`) keep the current meaning "unset". `_resolve_persistent` and `_resolve_auto` are untouched.
2. **Docstring**: add `Raises: TypeError` to `OscalStore.__init__` and keep the existing `RuntimeError` entry. This is an internal class, not an MCP tool, so the Description_Shape rules do not apply.
3. **mypy**: `effective_path` is typed `str | None`, so the `isinstance` branch is statically unreachable. Confirm with `hatch run typing`; if the project's mypy settings flag it, annotate the parameter as `object` for the check or add a targeted `# type: ignore[unreachable]` with a reason.

**File**: `src/mcp_server_for_oscal/main.py`

No change. `_init_oscal_store` already catches `Exception`, logs a warning with the traceback, and leaves the tool modules without a store, which is the specified failure behavior (3.3).

**File**: `tests/test_main.py` (class `TestMain`)

1. **Add an autouse fixture** next to `skip_integrity_check`:
   ```python
   @pytest.fixture(autouse=True)
   def skip_store_init(self):
       """These tests cover CLI, transport, and logging, not store initialization.

       main() runs with a mocked config, whose oscal_store_db_path would be a
       MagicMock. Store wiring is covered by TestStartupStoreWiring.
       """
       with patch("mcp_server_for_oscal.main._init_oscal_store"):
           yield
   ```
2. No test body changes. Assertions are unaffected because none of the 13 tests assert on store initialization.

**File**: `tests/test_file_integrity_integration.py` (class `TestFileIntegrityIntegration`)

1. **Add the same autouse fixture**, with a docstring that points to `TestStartupStoreWiring`. The class uses `setup_method` and `teardown_method`; the fixture is independent of them.

**File**: `tests/leak_guard.py` (new)

A helper module in the style of `tests/fixture_store.py`, with pure functions so they can be tested against `tmp_path`:

1. `LEAK_DIR_NAME = "MagicMock"`.
2. `fingerprint(root: Path) -> tuple`: returns `(False, None, ())` when `root / "MagicMock"` is absent; otherwise `(True, mtime_ns, sorted((child_name, child_mtime_ns) for each immediate subdirectory))`. Uses `os.scandir` and `entry.stat()`, never lists the files inside the subdirectories.
3. `describe_change(before, after) -> str | None`: returns `None` when equal, else a message such as `"test created ./MagicMock/ (a mock object was used as a filesystem path)"` or `"test modified ./MagicMock/<name>/ ..."`, followed by the cleanup hint `"Remove ./MagicMock/ (gitignored) and patch main._init_oscal_store or set config.oscal_store_db_path to a tmp_path string."`.

**File**: `tests/conftest.py`

1. **Add an autouse function-scoped fixture** `no_mock_path_leak(request)`:
   ```python
   @pytest.fixture(autouse=True)
   def no_mock_path_leak(request):
       root = request.config.invocation_params.dir
       before = leak_guard.fingerprint(root)
       yield
       message = leak_guard.describe_change(before, leak_guard.fingerprint(root))
       if message:
           pytest.fail(message, pytrace=False)
   ```
   It reads `root` once, so a test that changes directory cannot change what is measured. `pytest.fail` in teardown reports an ERROR on the offending test only; the next test starts from a fresh fingerprint.

**File**: `tests/test_leak_guard.py` (new), `tests/tools/test_oscal_store.py` (new test class), `tests/test_main.py` (new regression test)

See Testing Strategy.

**Out of scope**
- A generic "no new file in the working directory" guard for requirement 2.2. A top-level snapshot would need an allowlist (`private/`, `.hypothesis/`, `.coverage.*` parallel files, caches, `.DS_Store`) and risks false failures. Requirement 2.2 is verified once by the post-run check below. The guard is a named, narrow check for the one known leak.
- Deleting the existing `./MagicMock/`. It is gitignored and safe to remove, but removal is the user's call. Run `rm -rf MagicMock` from the repository root when ready.
- Changes to `.gitignore`, `Config`, `main.py`, CI workflows, or the tool registry. The Sync_Targets in `tool-authoring.md` do not apply because no MCP tool changes.

## Testing Strategy

### Validation Approach

All commands go through hatch and run from the repository root through the tool's `cwd` parameter, never with a `cd` prefix. Pytest flags go after `--`. Subagents must be told the same.

Exploratory and regression tests chdir into `tmp_path` (`monkeypatch.chdir`), so on unfixed code the leak lands in a temporary directory and the checkout's `./MagicMock/` does not grow.

### Exploratory Bug Condition Checking

**Goal**: Show the bug on unfixed code and confirm the root cause (truthy `PathLike` mock reaches `Path(...)` and `mkdir`).

**Test Plan**: Write the tests below first, run them against the unfixed code, and expect failures. Then add the guard and run `hatch test -- tests/test_main.py tests/test_file_integrity_integration.py` to see the 13 tests report a teardown error naming `MagicMock`. Do this after removing the stale directory, or accept about 100 MB of new files in it, because the guard in a stale directory still detects the change through directory modification times.

**Test Cases**:
1. **`_init_oscal_store` with bare mock config** (`tests/test_main.py`, new class `TestStoreInitWithMockedConfig`): `monkeypatch.chdir(tmp_path)`, `patch("mcp_server_for_oscal.main.config")` with no store attributes, call `_init_oscal_store()`. Assert `list(tmp_path.iterdir()) == []`. Fails on unfixed code with `MagicMock/` in `tmp_path`.
2. **`OscalStore(db_path=MagicMock())`** (`tests/tools/test_oscal_store.py`): chdir to `tmp_path`, expect `pytest.raises(TypeError)` and an empty `tmp_path`. Fails on unfixed code (no exception, directory created).
3. **Config fallback**: `monkeypatch.setattr(oscal_store.config, "oscal_store_db_path", MagicMock())`, then `OscalStore(db_path=None)`. Same assertions. Fails on unfixed code.
4. **Guard sees an existing 13-test leak**: run the 13 tests on unfixed code with the guard installed (manual step, not a committed test).

**Expected Counterexamples**:
- `MagicMock/config.oscal_store_db_path/<id>` appears under the chdir target.
- Causes confirmed if these fail: truthiness-only check in `_resolve_db_path`, `PathLike` mock accepted by `Path`. Refuted if only test 4 fails, in which case recheck whether `config` is imported by name elsewhere.

### Fix Checking

**Goal**: For all inputs where the bug condition holds, the fixed code creates nothing and installs no store.

**Pseudocode:**
```
FOR ALL input WHERE isBugCondition(input) DO
  result := _init_oscal_store_fixed(input)       // or OscalStore_fixed(input.config_value)
  ASSERT raises TypeError from OscalStore
         AND listdir(input.cwd) == listdir_before
         AND no store installed on any tool module
END FOR
```

### Preservation Checking

**Goal**: For all inputs where the bug condition does NOT hold, the fixed code behaves as the original.

**Pseudocode:**
```
FOR ALL input WHERE NOT isBugCondition(input) DO
  ASSERT OscalStore_original(input) = OscalStore_fixed(input)   // same db_mode and path
END FOR
```

**Testing Approach**: Property-based testing fits because the input domain (path strings, bundled DB present or absent, existing or missing target) is large, and two existing properties already cover most of it.

**Test Plan**: Run the existing suites unchanged before and after the fix. They already observe the original behavior:
- `tests/tools/test_oscal_store.py` Property 14 (`test_mode_resolution_is_deterministic`) and the persistent-mode tests at lines 3133-3230.
- `tests/test_build_oscal_db.py::test_existing_db_opens_without_reseeding`.
- `TestStartupStoreWiring` (`tests/test_main.py`), `TestStartupWiring` and `TestAboutUserDocumentsLoaded` (`tests/tools/test_local_doc_search.py`). These must pass without edits (3.5).

**Test Cases**:
1. **Mode resolution preserved**: Property 14 keeps passing for `db_path` of `None` and `str`.
2. **Empty and `None` stay "unset"**: `OscalStore(db_path="")` and `db_path=None` with empty config resolve to bundled or ephemeral, as before.
3. **`main()` assertions preserved**: the 13 tests still pass with the same assertions; `mock_mcp.run` and `update_from_args` call arguments are unchanged.

### Unit Tests

- `tests/test_leak_guard.py`, against `tmp_path` as root: absent directory gives an equal fingerprint; creating `MagicMock/x/file` changes it and the message contains `MagicMock`; a pre-existing `MagicMock/x/` with 50 files is unchanged across a no-op; adding a file to a pre-existing subdirectory changes it; removing the directory changes it; `describe_change(a, a)` is `None`.
- A fixture-level check that the failure is a test failure: call the teardown logic with a changed fingerprint and assert `pytest.fail.Exception` is raised with `MagicMock` in the message.
- `OscalStore` type check: `MagicMock()`, `123`, `b"x.db"`, `["a"]`, and `pathlib.Path("x.db")` raise `TypeError`; the message contains the type name and no path text; `""` and `None` do not raise.
- `_init_oscal_store()` with a bare `MagicMock` config logs one warning and leaves `query_documentation._store` unchanged (use `caplog`; restore singletons the way `TestStartupStoreWiring` does).
- `_init_oscal_store()` with `oscal_store_db_path=""`, `oscal_documents_dir=""`, and a real `OscalStore`: bundled mode in a temp dir, no file under the working directory (for tests that do exercise initialization, per 2.1).

### Property-Based Tests

- For any truthy non-`str` value (`st.one_of` of integers from 1, non-empty bytes, non-empty lists, `MagicMock` instances), `OscalStore(db_path=value)` raises `TypeError` and leaves a chdir'd `tmp_path` empty. Use `tmp_path_factory` per example, as Property 14 does, and keep `max_examples` modest (a rejected value does no I/O).
- For any `str` path under a fresh temp directory (existing empty SQLite file, or missing with `seed_from_bundled=False`), the resolved mode is `persistent` and the path is returned unchanged. Extends the preservation coverage of Property 14 to the exact set of inputs the new check must not reject.
- For any sequence of operations on a temp root (create, modify, delete under `MagicMock/<name>/`), `describe_change` is `None` exactly when the fingerprints are equal.

### Integration Tests

- Run the full suite once with `hatch run tests` (mypy, `hatch test --exitfirst --all --cover` on 3.13 and 3.14, bandit). On a checkout where `./MagicMock/` did not exist (a fresh clone, or after the user removes it), verify afterward that the directory is still absent and that `git status --ignored --short` shows no new entries beyond `private/`, `.hypothesis/`, `.coverage*`, and caches (requirement 2.2). Because this checkout already holds a stale `./MagicMock/`, compare `find MagicMock -type f | wc -l` before and after: it must stay 3,194 unless the user has removed the directory first.
- Run `hatch test -- tests/test_integration_stdio_smoke.py`: the real server over stdio still starts with the real `Config` (empty `OSCAL_STORE_DB_PATH`) and answers `tools/list`, which shows the type check does not affect production startup.
- Commit policy for the later implementation: work on a feature branch created from issue #17, put `#17` in commit messages, run `hatch run tests` before committing, stage only the files listed above plus the spec files, and do not commit to `main` or push without approval.
