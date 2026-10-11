# KB ID Empty Routing Fix Bugfix Design

## Overview

`query_oscal_documentation` (`src/mcp_server_for_oscal/tools/query_documentation.py`) picks the Bedrock Knowledge Base path with `if config.knowledge_base_id is not None:`. `Config.knowledge_base_id` is typed `str` and defaults to `""` (`os.getenv("OSCAL_KB_ID", "")`), and `Config.update_from_args` only overwrites it with truthy values, so it is never `None` at runtime. Every documentation query from a user without a KB therefore builds a boto3 `Session`, calls Bedrock `retrieve` with `knowledgeBaseId=""`, logs an exception, notifies the MCP client of an error, logs a fallback warning, and only then runs local search (GitHub issue #14).

The fix is a one-line change to the branch condition: take the KB path only when the configured ID has at least one non-whitespace character (`if config.knowledge_base_id.strip():`). The configured value is still passed to `query_kb` unchanged. `Config` defaults and `update_from_args` stay as they are: the `str`-typed `""` default is correct, and normalizing it in `Config` would widen the change to every consumer of the field without adding protection the routing check doesn't already give.

The bug survived because the routing tests simulate "unset" with `None`, which is impossible at runtime. The test changes replace those with `""` and whitespace values, add a no-AWS/no-notification assertion, and add one test that runs routing against a real `Config()` built with `OSCAL_KB_ID` unset.

## Glossary

- **Bug_Condition (C)**: The configured KB ID contains no non-whitespace character (`""` or whitespace-only), yet the tool takes the Bedrock KB path.
- **Property (P)**: For such KB IDs, the tool goes straight to `query_local`, creates no AWS session, sends no client error notification, and logs only the local-path message.
- **Preservation**: For KB IDs with at least one non-whitespace character, behavior is identical to today: KB path, unchanged ID passed to Bedrock, same logging, notification and fallback on failure.
- **F / F'**: `query_oscal_documentation` before / after the fix.
- **`query_oscal_documentation`**: Strands `@tool` in `tools/query_documentation.py`; routes between `query_kb` and `query_local`.
- **`query_kb`**: Builds `boto3.Session` (with `config.aws_profile` if set), calls `bedrock-agent-runtime.retrieve(knowledgeBaseId=config.knowledge_base_id, ...)`; on error logs the exception, calls `try_notify_client_error`, and re-raises.
- **`query_local`**: FTS search over the store's `documentation` rows; returns `{"error": "OscalStore has not been initialized"}` if `_store` is `None`.
- **`config.knowledge_base_id`**: `str`, from `OSCAL_KB_ID` (default `""`), overridden by a truthy `--knowledge-base-id`.

## Bug Details

### Bug Condition

The bug manifests whenever the KB ID is blank. The condition `is not None` is a type-level check on a field that is always a `str`, so it is constantly true and never distinguishes configured from unconfigured.

**Formal Specification:**
```
FUNCTION isBugCondition(input)
  INPUT: input = (kb_id: str, query: str, ctx: Context | None)
  OUTPUT: boolean

  RETURN kb_id.strip() == ""            // "" or only whitespace (space, \t, \n, \r, \f, \v, Unicode spaces)
         AND F(input) invokes query_kb  // true for every such input on unfixed code
END FUNCTION
```

On unfixed code the second clause always holds when the first does, so the effective bug domain is `{ kb_id : kb_id.strip() == "" }`.

### Examples

- `OSCAL_KB_ID` unset, no CLI arg (`kb_id = ""`), `query_oscal_documentation("What is a profile?", ctx)`. Expected: local results, one INFO "Using local documentation search path". Actual: "Using Knowledge Base search path (KB ID: )", `Session()` created, `retrieve(knowledgeBaseId="")` fails (ParamValidation or NoCredentials, or a stall on credential resolution), exception logged, `ctx` gets an error notification, warning "Knowledge Base query failed; falling back to local search", then local results.
- `.env` contains `OSCAL_KB_ID=" "` (`kb_id = " "`). Expected: same as above. Actual: same defect.
- `kb_id = "\t\n"`. Expected: local path. Actual: KB path.
- `kb_id = "ABCD1234"` (edge, not buggy). Expected and actual: KB path, `retrieve(knowledgeBaseId="ABCD1234")`.
- `kb_id = " ABCD1234 "` (edge, not buggy). Expected: KB path with `" ABCD1234 "` passed unchanged (req 3.1); the fix does not trim configured values.

## Expected Behavior

### Preservation Requirements

**Unchanged Behaviors:**
- Non-blank KB ID routes to `query_kb`, and Bedrock receives exactly the configured string (3.1).
- On Bedrock failure: exception logged, `try_notify_client_error` called, fallback warning logged, local results returned (3.2).
- INFO log "Using Knowledge Base search path (KB ID: …)" for non-blank IDs (3.3).
- `Session(profile_name=aws_profile)` when a profile is set; `Session()` otherwise (3.4). `query_kb` itself is not edited.
- `query_local` returns the "not initialized" error dict when `_store` is `None` (3.5).
- `get_tool_list()` includes `query_oscal_documentation` regardless of KB ID (3.6). `tools/__init__.py` is not edited.
- CLI `--knowledge-base-id` overrides env only when non-empty (3.7). `config.py` and `main.py` are not edited.

**Scope:**
All inputs whose KB ID has at least one non-whitespace character are unaffected. This includes:
- IDs set via `OSCAL_KB_ID` or `--knowledge-base-id`
- IDs with surrounding whitespace (routed to KB, passed through untouched)
- All `query` strings and `ctx` values (the query text plays no part in routing)

## Hypothesized Root Cause

1. **Type/sentinel mismatch (confirmed by reading the code)**: The routing check tests for `None`, but the field is `str` with a `""` sentinel. `config.py` line 25 (`os.getenv("OSCAL_KB_ID", "")`) and `update_from_args` (`if knowledge_base_id:`) guarantee a `str`. So `"" is not None` is always `True`.
2. **Tests encode the impossible value**: `TestQueryOscalDocumentationRouting.test_local_path_when_knowledge_base_id_not_set`, `test_local_path_when_kb_id_empty_string` (whose comment says "Empty string is falsy but not None" but then sets `None`), `TestSearchPathLogging.test_logs_local_path_when_kb_id_not_set`, and `TestUnconditionalToolRegistration.test_tool_present_without_kb_id` all set `mock_config.knowledge_base_id = None`. No test drives routing from a real `Config`, so the gap between the mock and the runtime default was never exercised.
3. **Fallback masks the defect functionally**: Because a KB failure falls back to local search, users still get results, so the symptom shows only as latency, logs, a spurious client notification, and an outbound AWS call.
4. **No blank-value handling**: Even a truthiness check (`if config.knowledge_base_id:`, as `review_notes.md` suggested) would still route `" "` to Bedrock. Requirement 2.5 needs whitespace-only treated as unset, hence `.strip()`.

## Correctness Properties

Property 1: Bug Condition - Blank KB ID routes to local search without AWS

_For any_ KB ID where isBugCondition holds (the ID is `""` or consists only of whitespace) and any query string and context, the fixed `query_oscal_documentation` SHALL return the result of `query_local(query, ctx)`, SHALL NOT call `query_kb`, SHALL NOT construct a boto3 `Session`, SHALL NOT call `try_notify_client_error`, and SHALL log "Using local documentation search path" without logging a Knowledge Base path message or a fallback warning.

**Validates: Requirements 2.1, 2.2, 2.3, 2.4, 2.5**

Property 2: Preservation - Non-blank KB ID keeps the Knowledge Base path

_For any_ KB ID where isBugCondition does NOT hold (the ID contains at least one non-whitespace character) and any query string, the fixed `query_oscal_documentation` SHALL behave exactly as the original: call `query_kb`, pass the configured ID to Bedrock `retrieve` unchanged, log the Knowledge Base path message including the ID, use the configured AWS profile for the session, and on a Bedrock error notify the client, log the fallback warning, and return `query_local` results.

**Validates: Requirements 3.1, 3.2, 3.3, 3.4, 3.5, 3.6, 3.7**

## Fix Implementation

### Changes Required

**File**: `src/mcp_server_for_oscal/tools/query_documentation.py`

**Function**: `query_oscal_documentation`

**Specific Changes**:
1. **Branch condition**: Replace `if config.knowledge_base_id is not None:` with `if config.knowledge_base_id.strip():`. The KB-path log line and the `query_kb(query, ctx)` call stay as they are, so `query_kb` still reads the untrimmed `config.knowledge_base_id`.
   - mypy: `knowledge_base_id` is `str`, so `.strip()` type-checks.
   - Mock compatibility: tests that patch `config` with a `MagicMock` without setting `knowledge_base_id` get a truthy `MagicMock` from `.strip()`, matching today's KB-path behavior for them.
2. **Docstring**: Update the `Returns` section so LLM callers know the result is a Bedrock `RetrieveResponse` when a KB is configured and a local page response (`items`, `total`, `offset`, `limit`, `hasMore`) otherwise. Optionally add a short comment above the condition noting that blank means unset (#14).

No changes to `config.py`, `main.py`, `oscal_agent.py`, `tools/__init__.py`, or `query_kb`.

**Tests**: `tests/tools/test_local_doc_search.py`
1. `TestQueryOscalDocumentationRouting.test_local_path_when_knowledge_base_id_not_set`: set `""` instead of `None`.
2. `test_local_path_when_kb_id_empty_string`: set `""`, fix the misleading comment, and assert `query_kb` is not called.
3. Add a parametrized test over `" "`, `"\t"`, `"\n"`, `" \t\r\n "`, `"\u00a0"`, `"\u2003"` asserting the local path and no `query_kb` call.
4. Add a test that patches `query_documentation.Session` and `query_documentation.try_notify_client_error`, sets `knowledge_base_id = ""`, calls with a `MagicMock` ctx, and asserts neither was called and the result came from `query_local` (2.2, 2.4). Patching `Session` keeps the test free of real AWS calls even on unfixed code.
5. `TestSearchPathLogging.test_logs_local_path_when_kb_id_not_set`: set `""`; also assert no message contains "Knowledge Base" and no WARNING records exist (2.3).
6. `TestUnconditionalToolRegistration.test_tool_present_without_kb_id`: set `""`.
7. Add a real-`Config` test: under `patch.dict(os.environ, {"PYTHON_DOTENV_DISABLED": "1"}, clear=True)` (the pattern from `tests/test_config.py`, which keeps a developer's `.env` out), build `Config()`, patch `query_documentation.config` with that instance, patch `Session`, `try_notify_client_error`, and `query_local`, call the tool, and assert the local path with no `Session` call. This also covers `update_from_args(knowledge_base_id=None)` and `""` keeping the blank default.
8. Add the Hypothesis property tests described below.

**Docs**:
- `.agents/summary/review_notes.md` row B2 (#14): rewrite in the B1 style: "Resolved by #14: `query_oscal_documentation` called Bedrock even when no KB was configured", keep the cause (condition was `is not None`, default `""`, tests used `None`), and set the fix column to "Fixed: routing uses `knowledge_base_id.strip()`, so blank or whitespace-only IDs go straight to local search; tests use `""`/whitespace and a real `Config` default".
- `.agents/summary/components.md` `query_documentation.py` row: replace "The branch condition is `knowledge_base_id is not None`, see review_notes" with "KB path only when `knowledge_base_id` has non-whitespace characters; otherwise local search with no AWS call".
- `.agents/summary/workflows.md` flowchart: decision node `{config.knowledge_base_id.strip non-empty}`, edges `true --> KB` and `false, including '' and whitespace --> L`; replace the paragraph below it with one sentence saying the default `""` goes straight to local search and only a KB failure triggers the fallback.
- `AGENTS.md` "Known bugs" bullet: drop #14 and keep #15, e.g. "Known bugs (the awesome-oscal workflow path #15) are listed in …".

## Testing Strategy

### Validation Approach

First run the corrected tests against unfixed code to confirm they fail for the reason in the root cause analysis, then apply the one-line fix and confirm the fix and preservation tests pass along with the existing KB-path suites (`tests/tools/test_query_documentation.py`, `tests/test_integration.py`, `tests/test_tool_registry.py`, `tests/test_config.py`).

### Exploratory Bug Condition Checking

**Goal**: Surface counterexamples on unfixed code and confirm the `is not None` hypothesis.

**Test Plan**: With `query_kb`, `query_local`, `Session`, and `try_notify_client_error` patched, set blank KB IDs and call the tool. Run `hatch test tests/tools/test_local_doc_search.py -- -k "Routing or SearchPathLogging or BlankKbId"` on unfixed code.

**Test Cases**:
1. **Empty string routing**: `kb_id = ""` asserts `query_kb` not called (will fail on unfixed code)
2. **Whitespace routing**: `kb_id = " "` and `"\t\n"` (will fail on unfixed code)
3. **No AWS, no notification**: `kb_id = ""` with real `query_kb` and patched `Session` asserts `Session` and `try_notify_client_error` not called (will fail on unfixed code)
4. **Real Config default**: `Config()` with `OSCAL_KB_ID` unset (will fail on unfixed code)
5. **Logging**: `kb_id = ""` asserts no "Knowledge Base" message (will fail on unfixed code)

**Expected Counterexamples**:
- `query_kb` called once with `kb_id = ""`; `Session` constructed; log "Using Knowledge Base search path (KB ID: )".
- If any of these pass on unfixed code, the hypothesis is wrong (for example routing reads a different attribute) and needs re-analysis.

### Fix Checking

**Pseudocode:**
```
FOR ALL kb_id IN whitespace_only_strings() ∪ {""}, query IN text() DO
  result := query_oscal_documentation'(query, ctx)   // with query_kb, query_local, Session, try_notify_client_error patched
  ASSERT result == query_local.return_value
  ASSERT query_kb.not_called AND Session.not_called AND try_notify_client_error.not_called
END FOR
```

### Preservation Checking

**Pseudocode:**
```
FOR ALL kb_id WHERE kb_id.strip() != "", query IN text() DO
  ASSERT query_oscal_documentation(query) = query_oscal_documentation'(query)
  // concretely: query_kb called once; Bedrock retrieve gets knowledgeBaseId == kb_id (unchanged)
END FOR
```

**Testing Approach**: Property-based testing fits preservation here because the input domain is "any string with a non-whitespace character", including leading/trailing whitespace and Unicode, which hand-picked examples undersample. Hypothesis respects the `OSCAL_TEST_MAX_EXAMPLES` cap from `tests/conftest.py`.

**Test Plan**: The existing KB-path tests already describe today's behavior with non-empty IDs; they must pass unmodified. Add property tests on top.

**Test Cases**:
1. **KB routing preservation**: any non-blank ID calls `query_kb` and returns its value
2. **ID passthrough**: any non-blank ID (including `" kb "`), with `Session` patched and the real `query_kb`, results in `retrieve(knowledgeBaseId=kb_id, ...)` with the exact string
3. **Failure fallback preservation**: existing `test_kb_failure_falls_back_to_local`, `test_logs_fallback_on_kb_failure`, `test_query_documentation_client_error`, and `test_aws_error_handling_integration` keep passing
4. **Profile and registration preservation**: existing `test_query_documentation_success_with_profile` and `tests/test_tool_registry.py` keep passing

### Unit Tests

- Blank KB ID routing (`""`, parametrized whitespace incl. `\u00a0`, `\u2003`) with `query_kb` not called
- No `Session` and no `try_notify_client_error` for a blank ID with a real ctx mock
- Logging: local message present, no "Knowledge Base" message, no WARNING for a blank ID
- Real `Config()` default (`OSCAL_KB_ID` unset, dotenv disabled) routes locally
- Non-blank ID with surrounding whitespace is passed through untrimmed

### Property-Based Tests

- `@given(kb_id=st.text(alphabet=st.characters(categories=["Zs", "Cc"])).filter(lambda s: not s.strip()))`: blank IDs always route to `query_local` with no `query_kb`/`Session` call (Property 1). Use a strategy restricted to characters `str.strip` removes, then filter, so the filter rarely rejects.
- `@given(kb_id=st.text(min_size=1).filter(lambda s: s.strip()), query=st.text())`: non-blank IDs always call `query_kb` and return its result (Property 2).
- `@given(kb_id=<non-blank text>)` with the real `query_kb` and patched `Session`: `retrieve` receives `knowledgeBaseId == kb_id` exactly (Property 2, req 3.1).
- Use `deadline=None` as other store-adjacent property tests do; no explicit `max_examples` beyond the project default so the conftest cap applies.

### Integration Tests

- Real `Config()` with `OSCAL_KB_ID` unset plus an initialized `OscalStore` (`tmp_path` with one markdown file, `init_store`) returns a page response with hits and never touches `Session`.
- Existing `tests/test_integration.py` KB-path tests (`"test-kb-id"`, `"invalid-kb-id"`) remain unchanged and passing.
- Full gate before commit: `hatch run tests` (mypy, `hatch test --all --cover` on 3.13 and 3.14, bandit), plus `hatch check fmt` and `hatch check code`.
