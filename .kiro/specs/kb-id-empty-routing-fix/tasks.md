# Implementation Plan

## Ground rules (apply to every task)

- Run all Python through hatch: `hatch test <path>`, `hatch run tests`, `hatch run typing`, `hatch check fmt`, `hatch check code`. Never call `python`, `pytest`, `mypy`, `ruff`, or `bandit` directly.
- Pass pytest flags after `--`, e.g. `hatch test tests/tools/test_local_doc_search.py -- -k "BlankKbId" -x`.
- Never prefix shell commands with `cd`; use the tool's `cwd` parameter. Restate this and the hatch rule to any subagent.
- Work only on the feature branch for issue #14 (created by the orchestrator before implementation). Never commit to `main`.
- Run `hatch run tests` before each commit. Commit messages include `#14` and state whether tests passed or failed.
- Stage only files changed for this fix, by name. Never push without explicit user approval.
- All new tests patch `query_documentation.Session` (or `query_kb`) so no test makes a real AWS call, even on unfixed code.

- [-] 0. Confirm the feature branch and commit the spec
  - `git branch --show-current` must be the #14 feature branch, not `main`; stop and ask if it isn't
  - `git status`: confirm no unrelated staged changes
  - Run `hatch run tests` (baseline; record the result)
  - Stage only `.kiro/specs/kb-id-empty-routing-fix/{.config.kiro,bugfix.md,design.md,tasks.md}`
  - Commit: `docs(spec): add KB ID empty routing bugfix spec (#14) - tests passed|failed`
  - Do not push

- [~] 1. Write bug condition exploration tests
  - **Property 1: Bug Condition** - Blank KB ID routes to local search without AWS
  - **CRITICAL**: These tests MUST FAIL on unfixed code. Failure confirms the bug exists
  - **DO NOT attempt to fix the test or the code when it fails**
  - **NOTE**: The tests encode the expected behavior and will validate the fix once they pass
  - **GOAL**: Surface counterexamples that demonstrate the bug exists
  - **Scoped PBT Approach**: The bug is deterministic, so pair concrete cases (`""`, parametrized whitespace) with one Hypothesis property over blank strings
  - Bug condition: `isBugCondition(input) = input.kb_id.strip() == ""` (on unfixed code every such input invokes `query_kb`)
  - Expected behavior: result is `query_local(query, ctx)`; `query_kb`, `Session`, and `try_notify_client_error` not called; only the local-path INFO log; no "Knowledge Base" message and no WARNING
  - Add a new class `TestBlankKbIdRouting` in `tests/tools/test_local_doc_search.py` (do not edit existing tests yet; that is task 3.2):
    - Empty string: `mock_config.knowledge_base_id = ""` → `query_local` result returned, `query_kb.assert_not_called()`
    - Parametrized whitespace over `" "`, `"\t"`, `"\n"`, `" \t\r\n "`, `"\u00a0"`, `"\u2003"` → local path, `query_kb` not called
    - No AWS / no notification: real `query_kb`, patch `query_documentation.Session`, `query_documentation.try_notify_client_error`, and `query_local`; `knowledge_base_id = ""`, `ctx = MagicMock()` → `Session` and `try_notify_client_error` not called, result from `query_local` (2.2, 2.4)
    - Logging with `caplog`: `knowledge_base_id = ""` → a message containing "local documentation search path", no message containing "Knowledge Base", no WARNING records (2.3)
    - Real `Config()` default: under `patch.dict(os.environ, {"PYTHON_DOTENV_DISABLED": "1"}, clear=True)` (pattern from `tests/test_config.py`), build `Config()`, also call `update_from_args(knowledge_base_id=None)` and `update_from_args(knowledge_base_id="")` and confirm the field stays `""`; patch `query_documentation.config` with that instance plus `Session`, `try_notify_client_error`, `query_local` → local path, `Session` not called
    - Hypothesis property: `@given(kb_id=st.text(alphabet=st.characters(categories=["Zs", "Cc"])).filter(lambda s: not s.strip()), query=st.text())`, `@settings(deadline=None)` (no explicit `max_examples`, so the `OSCAL_TEST_MAX_EXAMPLES` cap applies); create mocks inside the test body → `query_local` result returned, `query_kb` and `Session` not called
  - Run on UNFIXED code: `hatch test tests/tools/test_local_doc_search.py -- -k "BlankKbId"`
  - **EXPECTED OUTCOME**: Tests FAIL. Expected counterexamples: `query_kb` called once with `kb_id=""`; `Session` constructed; `try_notify_client_error` called; log "Using Knowledge Base search path (KB ID: )" and fallback WARNING
  - If any test passes on unfixed code, the `is not None` hypothesis is wrong; revisit the root cause in design.md before continuing
  - Record the counterexamples in this task when marking it complete
  - _Requirements: 1.1, 1.2, 1.3, 1.4, 1.5, 2.1, 2.2, 2.3, 2.4, 2.5_

- [~] 2. Write preservation property tests (BEFORE implementing the fix)
  - **Property 2: Preservation** - Non-blank KB ID keeps the Knowledge Base path
  - **IMPORTANT**: Follow observation-first methodology. Observe unfixed behavior for inputs where `isBugCondition` is false, then encode it
  - Observe on unfixed code: `kb_id="ABCD1234"` calls `query_kb` and returns its value; with real `query_kb` and patched `Session`, `retrieve` gets `knowledgeBaseId="ABCD1234"`; `kb_id=" ABCD1234 "` is passed untrimmed; Bedrock failure logs, notifies the client, warns, and returns `query_local` results
  - Add a new class `TestNonBlankKbIdPreservation` in `tests/tools/test_local_doc_search.py`:
    - Hypothesis: `@given(kb_id=st.text(min_size=1).filter(lambda s: s.strip()), query=st.text())`, `deadline=None` → `query_kb` called once with `(query, ctx)` and its return value returned; `query_local` not called
    - Hypothesis passthrough: `@given(kb_id=<non-blank text>)` with real `query_kb`, patched `Session` → `Session.return_value.client.return_value.retrieve` called with `knowledgeBaseId == kb_id` exactly (3.1)
    - Concrete: `" ABCD1234 "` passed to `retrieve` untrimmed (3.1)
    - Logging: non-blank ID → INFO message contains "Knowledge Base" and the ID (3.3)
    - Create mocks inside Hypothesis test bodies (no function-scoped fixtures)
  - Existing suites that already describe preserved behavior must pass unmodified: `tests/tools/test_query_documentation.py` (incl. `test_query_documentation_success_with_profile`, `test_query_documentation_client_error`), `tests/test_integration.py` (incl. `test_aws_error_handling_integration`), `tests/test_tool_registry.py`, `tests/test_config.py`, and in `test_local_doc_search.py`: `test_kb_path_when_knowledge_base_id_set`, `test_kb_failure_falls_back_to_local`, `test_logs_kb_path_when_kb_id_set`, `test_logs_fallback_on_kb_failure`, `TestQueryLocalErrorHandling`
  - Run on UNFIXED code: `hatch test tests/tools/test_local_doc_search.py tests/tools/test_query_documentation.py tests/test_integration.py tests/test_tool_registry.py tests/test_config.py -- -k "not BlankKbId"`
  - **EXPECTED OUTCOME**: Tests PASS (baseline confirmed)
  - _Requirements: 3.1, 3.2, 3.3, 3.4, 3.5, 3.6, 3.7_

- [ ] 3. Fix blank KB ID routing to Bedrock (#14)

  - [~] 3.1 Implement the fix in `src/mcp_server_for_oscal/tools/query_documentation.py`
    - In `query_oscal_documentation`, replace `if config.knowledge_base_id is not None:` with `if config.knowledge_base_id.strip():`
    - Optional one-line comment above it: blank or whitespace-only KB ID means unset (#14)
    - Keep the KB-path log line and the `query_kb(query, ctx)` call unchanged, so `query_kb` still reads the untrimmed `config.knowledge_base_id`
    - Docstring `Returns`: Bedrock `RetrieveResponse` when a KB is configured; otherwise a local page response (`items`, `total`, `offset`, `limit`, `hasMore`). Leave the first paragraph untouched (used by the MCPB manifest)
    - Do not edit `config.py`, `main.py`, `oscal_agent.py`, `tools/__init__.py`, or `query_kb`
    - Run `hatch run typing` (`knowledge_base_id` is `str`, so `.strip()` type-checks)
    - _Bug_Condition: isBugCondition(input) where input.kb_id.strip() == ""_
    - _Expected_Behavior: result == query_local(query, ctx); no query_kb, Session, or try_notify_client_error call; only the local-path log (Property 1 in design)_
    - _Preservation: non-blank IDs keep the KB path, untrimmed ID to Bedrock, same logging, profile session, failure notification and fallback (Property 2 in design)_
    - _Requirements: 2.1, 2.2, 2.3, 2.4, 2.5, 3.1, 3.2, 3.3, 3.4, 3.5, 3.6, 3.7_

  - [~] 3.2 Fix the existing `None`-based tests in `tests/tools/test_local_doc_search.py`
    - `TestQueryOscalDocumentationRouting.test_local_path_when_knowledge_base_id_not_set`: set `knowledge_base_id = ""` instead of `None`
    - `TestQueryOscalDocumentationRouting.test_local_path_when_kb_id_empty_string`: set `""`, replace the misleading "falsy but not None" comment, add `query_kb.assert_not_called()` (patch `query_kb` if not already patched)
    - `TestSearchPathLogging.test_logs_local_path_when_kb_id_not_set`: set `""`; also assert no message contains "Knowledge Base" and no WARNING records
    - `TestUnconditionalToolRegistration.test_tool_present_without_kb_id`: set `""`
    - Grep the test tree for any remaining `knowledge_base_id = None` and fix it the same way
    - Run: `hatch test tests/tools/test_local_doc_search.py`
    - _Requirements: 2.1, 2.3, 3.6_

  - [~] 3.3 Update docs and known-bug references
    - `.agents/summary/review_notes.md` row B2 (#14): rewrite in the B1 style, "Resolved by #14: `query_oscal_documentation` called Bedrock even when no KB was configured"; keep the cause (condition was `is not None`, default `""`, tests used `None`); fix column "Fixed: routing uses `knowledge_base_id.strip()`, so blank or whitespace-only IDs go straight to local search; tests use `""`/whitespace and a real `Config` default"
    - `.agents/summary/components.md` `query_documentation.py` row: replace "The branch condition is `knowledge_base_id is not None`, see review_notes" with "KB path only when `knowledge_base_id` has non-whitespace characters; otherwise local search with no AWS call"
    - `.agents/summary/workflows.md` flowchart: decision node `{config.knowledge_base_id.strip non-empty}`, edges `true --> KB` and `false, including '' and whitespace --> L`; replace the paragraph below with one sentence: the default `""` goes straight to local search, and only a KB failure triggers the fallback
    - `AGENTS.md` "Known bugs" bullet: drop #14 and keep #15, e.g. "Known bugs (the awesome-oscal workflow path #15) are listed in `.agents/summary/review_notes.md`. …"
    - _Requirements: 2.1, 2.2_

  - [~] 3.4 Verify bug condition exploration tests now pass
    - **Property 1: Expected Behavior** - Blank KB ID routes to local search without AWS
    - **IMPORTANT**: Re-run the SAME tests from task 1. Do NOT write new tests
    - Run: `hatch test tests/tools/test_local_doc_search.py -- -k "BlankKbId"`
    - **EXPECTED OUTCOME**: Tests PASS (bug fixed)
    - _Requirements: 2.1, 2.2, 2.3, 2.4, 2.5_

  - [~] 3.5 Verify preservation tests still pass
    - **Property 2: Preservation** - Non-blank KB ID keeps the Knowledge Base path
    - **IMPORTANT**: Re-run the SAME tests from task 2. Do NOT write new tests
    - Run: `hatch test tests/tools/test_local_doc_search.py tests/tools/test_query_documentation.py tests/test_integration.py tests/test_tool_registry.py tests/test_config.py`
    - **EXPECTED OUTCOME**: Tests PASS (no regressions)
    - _Requirements: 3.1, 3.2, 3.3, 3.4, 3.5, 3.6, 3.7_

- [~] 4. Checkpoint - Ensure all tests pass, then commit
  - `hatch check fmt` and `hatch check code` must pass (use `--fix` if needed, then re-run without it); use a targeted `# noqa: RULE - reason` only if justified
  - `hatch run tests` (mypy, `hatch test --all --cover` on 3.13 + 3.14, bandit) must pass
  - Confirm `git branch --show-current` is the #14 feature branch, not `main`
  - Stage only files changed for this fix, by name: `src/mcp_server_for_oscal/tools/query_documentation.py`, `tests/tools/test_local_doc_search.py`, `.agents/summary/review_notes.md`, `.agents/summary/components.md`, `.agents/summary/workflows.md`, `AGENTS.md`, `.kiro/specs/kb-id-empty-routing-fix/tasks.md`
  - Commit: `fix(query_documentation): route blank KB ID to local search without AWS (#14) - tests passed`
  - Do not push. Ask the user before pushing or opening a PR
  - Ask the user if questions arise
