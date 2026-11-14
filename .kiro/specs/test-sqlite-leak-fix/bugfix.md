# Bugfix Requirements Document

## Introduction

Running the test suite writes SQLite database files into `./MagicMock/<mock-name>/<id>` under the working directory, outside `tmp_path`. Several tests that call `main()` replace `mcp_server_for_oscal.main.config` with a bare `MagicMock` and leave `_init_oscal_store()` unpatched. `_init_oscal_store()` then reads `config.oscal_store_db_path`, which is an auto-created, truthy `MagicMock` attribute. A `MagicMock` implements `os.PathLike`, and its path form is the relative string `MagicMock/<mock-name>/<id>`. `OscalStore` takes this as an explicit persistent path, so it creates the directories and copies the bundled database (about 8 MB) into them. The `<id>` differs on every run, so files accumulate. The folder is gitignored, so nothing is committed. One local checkout holds 3,194 files totalling about 24 GB.

The bug condition is: a test runs `_init_oscal_store()` (directly or through `main()`) while `config.oscal_store_db_path` is not a real string, so `OscalStore` resolves it to a relative path under the working directory. Tests that build a store with a real `tmp_path` string, tests that patch `_init_oscal_store`, and production runs with a real `Config` are not affected.

This is GitHub issue #17.

## Bug Analysis

### Current Behavior (Defect)

1.1 WHEN a test patches `mcp_server_for_oscal.main.config` with a bare `MagicMock` and calls `main()` without patching `_init_oscal_store` THEN the system passes the `MagicMock` value of `oscal_store_db_path` to `OscalStore` as a persistent database path, creates `./MagicMock/<mock-name>/` under the working directory, and copies the bundled database into it.

1.2 WHEN the test suite is run repeatedly THEN the system creates a new database file in `./MagicMock/` on every affected test run, because the mock's `id` changes, so the directory grows without bound (3,194 files, about 24 GB on one checkout).

1.3 WHEN a test writes a file outside `tmp_path` and the system temporary directory THEN the system does not detect or report it, so the leak goes unnoticed until disk space runs out.

### Expected Behavior (Correct)

2.1 WHEN a test calls `main()` with a mocked `config` THEN the system SHALL NOT pass a mock-derived value as the database path, so `_init_oscal_store()` creates no file or directory under the working directory. A test that does not exercise store initialization SHALL NOT build a real store. A test that does exercise it SHALL set `oscal_store_db_path` to a string under `tmp_path`, or to `""` so that store resolution does not use a persistent path.

2.2 WHEN the full test suite has run to completion from a checkout where `./MagicMock/` did not exist THEN the system SHALL leave no `./MagicMock/` directory and no other new files under the working directory outside the test's own output paths (`private/docs/`, `.hypothesis/`, `.coverage`, caches).

2.3 WHEN any test causes `./MagicMock/` to be created THEN the test suite SHALL fail with a message naming the directory, so a regression is caught in CI instead of accumulating silently.

### Unchanged Behavior (Regression Prevention)

3.1 WHEN `OscalStore` is given an explicit string `db_path`, or `config.oscal_store_db_path` is a non-empty string, THEN the system SHALL CONTINUE TO open an existing file as a persistent database, seed a missing file from the verified bundled database, and create a fresh database when seeding is not possible.

3.2 WHEN `OscalStore` is given no `db_path` and `config.oscal_store_db_path` is empty THEN the system SHALL CONTINUE TO use bundled mode (verified copy in a temporary directory) or fall back to ephemeral mode.

3.3 WHEN `_init_oscal_store()` runs with the real `config` THEN the system SHALL CONTINUE TO create the store from `oscal_store_db_path` and `oscal_store_cache_size`, scan `oscal_documents_dir` with Origin `user`, install the store on every tool module, and log a warning without exiting if initialization fails.

3.4 WHEN the `main()` tests run THEN the system SHALL CONTINUE TO verify the same behavior as before: `update_from_args` arguments, transport validation, logging configuration, integrity verification calls and exit code 2 on failure, and `mcp.run` arguments.

3.5 WHEN the startup wiring tests (`TestStartupStoreWiring` in `tests/test_main.py` and `TestStartupWiring` in `tests/tools/test_local_doc_search.py`) run THEN the system SHALL CONTINUE TO pass without change to what they assert.
