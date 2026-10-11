# XSD Schema Retrieval Fix Bugfix Design

## Overview

`get_oscal_schema` (`src/mcp_server_for_oscal/tools/get_schema.py`) runs every schema file through `json.load` and `json.dumps`, whatever `schema_type` is. XSD files are XML, so every `schema_type="xsd"` request raises `JSONDecodeError`, and the client gets a generic "failed to open schema" error (GitHub #13). The tool also never closes the handle returned by `open_schema_file`.

The fix is small and stays inside `get_oscal_schema`:

- Open the file with a `with` block so the handle closes on success and on error.
- Branch on `schema_type`. For `xsd`, return the file text as-is. For `json`, keep `json.dumps(json.load(f))` exactly as it is now.
- Keep validation order, error messages, client notification, and re-raise behavior.
- Fix the docstring `Returns` section (and the inaccurate `schema_type` arg line).

`open_schema_file` keeps its signature and behavior. Grep shows it is used only in `get_schema.py` and its tests, but the change doesn't need it to change.

The test suite currently hides the bug: `test_get_schema_success_xsd_schema` mocks `json.load`. We replace it with tests that read the real bundled files, and we add exhaustive and property-based checks.

## Glossary

- **Bug_Condition (C)**: a call with a valid `model_name` (an `OSCALModelType` value or `"complete"`) and `schema_type="xsd"`. For the handle-leak part, any call that reaches the file-open step.
- **Property (P)**: for XSD, the return value equals the bundled `.xsd` file's text exactly, and the file handle is closed after the call. For JSON, the handle is closed and the output is unchanged.
- **Preservation**: everything outside C behaves exactly as before: JSON output, default arguments, input validation errors, and file-open/parse error reporting.
- **F / F'**: `get_oscal_schema` before and after the fix.
- **`get_oscal_schema`**: Strands `@tool` in `tools/get_schema.py`. Validates inputs, resolves `"{schema_names[model_name]}.{schema_type}"`, and returns the schema as a string.
- **`open_schema_file`**: helper in the same module. Returns an open UTF-8 text handle for a file in `oscal_schemas/`, after stripping leading `./\` characters.
- **`schema_names`**: mapping in `tools/utils.py` from model type (plus `"complete"`) to the schema file base name, e.g. `system-security-plan → oscal_ssp_schema`.
- **Bundled schemas**: `src/mcp_server_for_oscal/oscal_schemas/*.{json,xsd}`. That covers 9 names × 2 types = 18 files. `.gitattributes` marks them `-text`, so they are byte-identical on every OS. All XSDs are ASCII with LF line endings and no BOM (checked with `file` and `grep -c $'\r'`).

## Bug Details

### Bug Condition

The bug shows up whenever an XSD schema is requested for a valid model. `get_oscal_schema` has one code path for both types: `json.load(open_schema_file(name))`. The first byte of an XSD file is `<`, so `json.load` raises `JSONDecodeError`. The broad `except Exception` catches it, logs it, sends "failed to open schema oscal_<x>_schema.xsd" to the client, and re-raises. The secondary defect affects every request that reaches the open: the handle is passed straight into `json.load` and never closed. CPython refcounting usually closes it when it's collected, but the code doesn't guarantee that, and other runtimes or a held reference keep it open.

**Formal Specification:**
```
VALID_MODELS := { m.value FOR m IN OSCALModelType } ∪ { "complete" }

FUNCTION isBugCondition(input)
  INPUT: input of type (model_name: str, schema_type: str)
  OUTPUT: boolean

  // Primary defect (Req 1.1, 1.2): XSD can't be retrieved
  RETURN input.model_name IN VALID_MODELS
         AND input.schema_type = "xsd"
END FUNCTION

FUNCTION isHandleLeakCondition(input)
  INPUT: input of type (model_name: str, schema_type: str)
  OUTPUT: boolean

  // Secondary defect (Req 1.3): any call that opens a file
  RETURN input.model_name IN VALID_MODELS
         AND input.schema_type IN { "json", "xsd" }
END FUNCTION
```

### Examples

- `get_oscal_schema(model_name="catalog", schema_type="xsd")`. Expected: the contents of `oscal_catalog_schema.xsd`, starting with `<xs:schema`. Actual: `JSONDecodeError: Expecting value: line 1 column 1 (char 0)`, and the client gets "failed to open schema oscal_catalog_schema.xsd".
- `get_oscal_schema(model_name="system-security-plan", schema_type="xsd")`. Expected: contents of `oscal_ssp_schema.xsd`. Actual: `JSONDecodeError`.
- `get_oscal_schema(model_name="complete", schema_type="xsd")`. Expected: contents of `oscal_complete_schema.xsd`. Actual: `JSONDecodeError`.
- `get_oscal_schema(model_name="catalog", schema_type="json")`. Output is correct, but the handle from `open_schema_file` is not closed by the time the function returns (it can be seen if a reference to it is held).
- Edge case: `get_oscal_schema(model_name="catalog", schema_type="xsd")` when the XSD file is missing or unreadable. Expected: client gets "failed to open schema oscal_catalog_schema.xsd" and the original `OSError` is re-raised. This is the same contract as JSON.

## Expected Behavior

### Preservation Requirements

**Unchanged Behaviors:**
- JSON requests return `json.dumps(json.load(file))`: compact, default separators, key order kept. The returned string is byte-identical to the current output, not just semantically equal (Req 3.1).
- `get_oscal_schema()` with no arguments returns the `complete` JSON schema (Req 3.2).
- Invalid `schema_type` is checked first. It sends `"Invalid schema type: <value>."` to the client and raises `ValueError` without opening a file (Req 3.3).
- Invalid `model_name` sends the existing "Invalid model: ... Use the tool list_oscal_models to get valid model names." message and raises `ValueError` without opening a file (Req 3.4).
- A JSON file that fails to open or parse sends `"failed to open schema <file name>"`, logs with `logger.exception`, and re-raises the original exception type (Req 3.5).
- File names are still `f"{schema_names.get(model_name)}.{schema_type}"`, opened through `open_schema_file` from the bundled `oscal_schemas/` directory (Req 3.6).
- The `logger.debug` call at entry is unchanged.
- The docstring's first paragraph is unchanged. `bin/build_mcpb.py` uses it as the MCPB manifest tool description.

**Scope:**
Inputs outside C should see no change. That includes:
- `schema_type="json"` (or the default) for every valid model. Output is identical; the only difference is that the handle is now closed.
- Any `schema_type` other than `json` or `xsd`.
- Any `model_name` outside `VALID_MODELS`.
- Failures opening or parsing a JSON schema file.

## Hypothesized Root Cause

1. **Single parse path for two formats (confirmed by reading the code)**: `schema = json.load(open_schema_file(schema_file_name))` runs for both types, followed by `return json.dumps(schema)`. Nothing branches on `schema_type` after validation. This is the direct cause of 1.1.

2. **Over-broad error wrapping hides the cause (confirmed)**: one `try/except Exception` wraps both the open and the parse. The client message says "failed to open" even though the file opened fine and parsing failed (1.2). The message stays the same (Req 2.2, 3.5); it becomes accurate for XSD because XSD requests no longer go through a parser.

3. **Handle ownership is unclear (confirmed)**: `open_schema_file` returns an open handle, and the caller passes it inline to `json.load` without a `with`. Nothing owns closing it (1.3).

4. **Tests mock the defect away (confirmed)**: `test_get_schema_success_xsd_schema` patches `json.load` to return the XSD string, so it asserts the buggy path works. Every other success test also patches `open_schema_file` with a plain `Mock()` and patches `json.load`, so no test reads a real schema file.

5. **Docstring describes one format (confirmed)**: `Returns: str: The requested schema as JSON string` (1.4). The `schema_type` arg line says "Otherwise, return its XSD", which is also wrong: values other than `json`/`xsd` raise.

## Correctness Properties

Property 1: Bug Condition - XSD requests return the bundled XSD text

_For any_ `(model_name, schema_type)` where `isBugCondition` is true (`model_name` in `VALID_MODELS`, `schema_type == "xsd"`), the fixed `get_oscal_schema` SHALL return a `str` equal to the bundled file `oscal_schemas/{schema_names[model_name]}.xsd` decoded as UTF-8 from its raw bytes. It SHALL NOT raise or notify the client of an error. If the XSD file can't be opened or read, it SHALL send "failed to open schema <file name>" to the client and re-raise the original error.

**Validates: Requirements 2.1, 2.2**

Property 2: Bug Condition - Schema file handle is always closed

_For any_ `(model_name, schema_type)` where `isHandleLeakCondition` is true, every handle opened by `get_oscal_schema` SHALL be closed when the call returns or raises. That includes a JSON parse error and a read error on the open handle.

**Validates: Requirements 2.3**

Property 3: Preservation - JSON output and error contracts unchanged

_For any_ `(model_name, schema_type)` where `isBugCondition` is false, the fixed `get_oscal_schema` SHALL produce the same result as the original. For valid JSON requests: the identical string `json.dumps(json.load(file))`, which parses to the same object as the bundled JSON file. For an invalid `schema_type` or `model_name`: the same `ValueError` and client message, with no file opened. For JSON open/parse failures: the same "failed to open schema <file name>" message and the same re-raised exception type.

**Validates: Requirements 3.1, 3.2, 3.3, 3.4, 3.5, 3.6**

Property 4: Tool description documents both formats

The `get_oscal_schema` docstring's `Returns` section SHALL say that `json` requests return a JSON string and `xsd` requests return XML (XSD) text. Its first paragraph SHALL be unchanged.

**Validates: Requirements 2.4**

## Fix Implementation

### Changes Required

**File**: `src/mcp_server_for_oscal/tools/get_schema.py`

**Function**: `get_oscal_schema`

**Specific Changes**:
1. **Context-managed read with a type branch**: replace the body of the `try` block. Assign the result inside `try` and return after it, so there's no `return` inside `try` (avoids ruff `TRY300`):
   ```python
   try:
       with open_schema_file(schema_file_name) as schema_file:
           if schema_type == "xsd":
               schema_text = schema_file.read()
           else:
               schema_text = json.dumps(json.load(schema_file))
   except Exception:
       msg = f"failed to open schema {schema_file_name}"
       logger.exception(msg)
       try_notify_client_error(msg, ctx)
       raise

   return schema_text
   ```
   - The `except` block is unchanged, so open, read, and parse errors get the same message and re-raise for both types.
   - `json.dumps` moves inside the `try`. It can't fail on output from `json.load`, so error behavior doesn't change, and the handle closes before the function returns.
   - Text mode with UTF-8 (already set in `open_schema_file`) is right for XSD. The files are LF-only and ASCII, so universal-newline translation doesn't change the content.

2. **Docstring**:
   - `schema_type`: "`json` (default) returns the JSON schema; `xsd` returns the XML Schema (XSD). Other values are rejected."
   - `Returns`: "str: For `json`, the schema as a JSON string. For `xsd`, the schema as XML (XSD) text."
   - Leave the first paragraph alone (it's used in the MCPB manifest).

3. **`open_schema_file`**: no change. It already returns a context-manager-capable `TextIO`. Tightening its `-> Any` annotation to `-> TextIO` is optional and left out to keep the diff minimal.

**File**: `src/mcp_server_for_oscal/tools/README.md`

4. **Returns line** under `get_oscal_schema`: change "Schema as JSON string" to "JSON string for `json`; raw XSD (XML) text for `xsd`".

**Files**: `.agents/summary/review_notes.md`, `.agents/summary/components.md`, `AGENTS.md`

5. **Known-bug references**: mark B1 (#13) resolved in `review_notes.md`. Remove "XSD path is broken (always `json.load`)" from `components.md`. Drop "XSD schema retrieval #13" from the known-bugs bullet in `AGENTS.md`. These files tell agents not to "fix" tested behavior, so leaving them stale would mislead future work.

**Test files**: see Testing Strategy. Existing tests break mechanically because a plain `Mock()` doesn't support the context-manager protocol. That's expected and handled there.

## Testing Strategy

### Validation Approach

Two phases. First, write tests against real bundled files and handle-tracking, and run them on the unfixed code to confirm they fail for the reasons above. Then apply the fix and confirm they pass while the preservation tests still pass. The input domain for valid calls is finite (9 model names × 2 schema types), so we cover it exhaustively with `pytest.mark.parametrize` instead of sampling it. Hypothesis is for the infinite domain of invalid inputs. Note that `OSCAL_TEST_MAX_EXAMPLES=10` on Windows CI would cap a Hypothesis `sampled_from` over 18 cases and might skip some, which is another reason not to sample the valid domain.

All commands go through hatch, e.g. `hatch test tests/tools/test_get_schema.py`, `hatch test -- -k xsd`, and `hatch run tests` before committing.

Shared test helpers (in `tests/tools/test_get_schema.py`):
- `SCHEMA_DIR = Path(mcp_server_for_oscal.tools.get_schema.__file__).parent.parent / "oscal_schemas"`.
- `VALID_MODELS = [*OSCALModelType, "complete"]`.
- `expected_text(name, type) = (SCHEMA_DIR / f"{schema_names[name]}.{type}").read_bytes().decode("utf-8")`. Using raw bytes as the oracle means it would catch any newline translation or encoding change, not just agree with the implementation's own reading method.
- A handle-tracking fixture that wraps `open_schema_file` with `side_effect` calling the real function, records each returned handle, and lets tests assert `handle.closed`. The test holds a reference, so it doesn't depend on CPython GC timing.

### Exploratory Bug Condition Checking

**Goal**: show counterexamples on the UNFIXED code, confirming root causes 1 and 3.

**Test Plan**: call `get_oscal_schema` with real files (no `json.load` or `open_schema_file` mocking, except the pass-through tracker) and run before the fix.

**Test Cases**:
1. **XSD catalog**: `get_oscal_schema(None, "catalog", "xsd") == expected_text("catalog", "xsd")`. Fails on unfixed code with `JSONDecodeError`.
2. **XSD all models**: parametrized over `VALID_MODELS`, including `complete` and the mapped names `system-security-plan` and `plan-of-action-and-milestones`. All 9 fail on unfixed code.
3. **Handle closed, JSON success**: tracked handle for `("catalog", "json")` is closed after return. Fails on unfixed code: the held reference keeps it open.
4. **Handle closed, JSON parse error**: tracker returns a real handle to a `tmp_path` file containing `not json`. `json.JSONDecodeError` is raised and the handle is closed. Fails on unfixed code.
5. **XSD client notification**: with a mock `ctx`, `ctx.error` is not called for a valid XSD request. Fails on unfixed code ("failed to open schema ..." is sent).

**Expected Counterexamples**:
- `JSONDecodeError: Expecting value: line 1 column 1 (char 0)` for every XSD request, along with a client "failed to open schema" notification.
- `handle.closed is False` after a JSON call returns or raises.
- If test 1 fails for some other reason (e.g., a wrong file name), the root-cause hypothesis needs revisiting before going further.

### Fix Checking

**Goal**: for all inputs in C, F' returns the expected result.

**Pseudocode:**
```
FOR ALL model_name IN VALID_MODELS DO            // exhaustive, 9 cases
  result := get_oscal_schema_fixed(None, model_name, "xsd")
  ASSERT result = bytes(SCHEMA_DIR / schema_names[model_name] + ".xsd").decode("utf-8")
END FOR

FOR ALL model_name IN VALID_MODELS, schema_type IN {"json","xsd"} DO   // 18 cases
  handles := track(open_schema_file)
  get_oscal_schema_fixed(None, model_name, schema_type)
  ASSERT len(handles) = 1 AND handles[0].closed
END FOR

FOR XSD read/open failure (open_schema_file side_effect = FileNotFoundError) DO
  ASSERT raises FileNotFoundError
  ASSERT ctx.error called once with "failed to open schema oscal_catalog_schema.xsd"
END FOR
```

### Preservation Checking

**Goal**: for all inputs outside C, F' gives the same result as F.

**Pseudocode:**
```
FOR ALL model_name IN VALID_MODELS DO            // exhaustive JSON
  raw := bytes(SCHEMA_DIR / schema_names[model_name] + ".json")
  result := get_oscal_schema_fixed(None, model_name, "json")
  ASSERT result = json.dumps(json.loads(raw))     // byte-identical to F's output
  ASSERT json.loads(result) = json.loads(raw)
END FOR

FOR ALL schema_type IN text() WHERE schema_type NOT IN {"json","xsd"} DO   // Hypothesis
  ASSERT raises ValueError("Invalid schema type: " + schema_type + ".")
  ASSERT open_schema_file not called
END FOR

FOR ALL model_name IN text() WHERE model_name NOT IN VALID_MODELS,
        schema_type IN {"json","xsd"} DO                                   // Hypothesis
  ASSERT raises ValueError matching "Invalid model: "
  ASSERT open_schema_file not called
END FOR
```

**Testing Approach**: property-based testing fits the invalid-input preservation checks. The domain is arbitrary text, and Hypothesis will try edge cases (empty string, whitespace, case variants like `"JSON"` or `"Catalog"`, Unicode) that hand-written tests usually miss. The valid-input domain is finite, so exhaustive parametrization gives a stronger guarantee than sampling there. Expected JSON output is computed independently as `json.dumps(json.loads(raw_bytes))`, which reproduces F's exact output without running F.

**Test Plan**: run the preservation tests on the UNFIXED code first. They should pass, apart from the handle-closed assertions, which aren't part of preservation. Then confirm they still pass after the fix.

**Test Cases**:
1. **JSON output identity**: exhaustive over `VALID_MODELS`. Passes before and after.
2. **Default arguments**: `get_oscal_schema()` equals the expected `complete` JSON output.
3. **Invalid schema_type** (Hypothesis): `ValueError`, exact client message, no file opened.
4. **Invalid model_name** (Hypothesis): `ValueError`, existing message, no file opened.
5. **JSON open/parse failure**: existing `test_get_schema_file_not_found` and `test_get_schema_json_parse_error` keep their assertions.

### Unit Tests

Changes to `tests/tools/test_get_schema.py`:
- **Replace** `test_get_schema_success_xsd_schema` with real-file XSD tests (exploration cases 1–2 and 5). Remove all `json.load` mocking for XSD.
- **Update mock-based tests** that use `mock_open_schema_file.return_value = Mock()`. These are `test_get_schema_success_default_params`, `_catalog_model`, `_ssp_model`, `_poam_model`, `test_get_schema_json_parse_error`, `test_get_schema_logging`, and `test_get_schema_all_valid_models`. A plain `Mock` raises `TypeError` in a `with` statement.
  - Preferred: change them to read real bundled files, and keep their `open_schema_file.assert_called_once_with(<file name>)` checks by wrapping the real function (`side_effect=open_schema_file`). That removes the mocking pattern that hid this bug.
  - For `test_get_schema_json_parse_error`, which must force a parse failure, use `mock_open(read_data="not json")` or a real handle to a `tmp_path` file. Both support `with`. Assert the `JSONDecodeError` re-raise and the client message as before.
  - `test_get_schema_success_default_params` asserts `json.load` was called with `mock_file`. With a real file, drop that assertion and assert the output matches the expected `complete` JSON instead.
- **Add** an XSD open-failure test (`FileNotFoundError` with `schema_type="xsd"`): exact message `"failed to open schema oscal_catalog_schema.xsd"` and the exception is re-raised (Req 2.2).
- **Add** handle-closed tests: JSON success, XSD success, JSON parse error.
- **Add** a docstring test: `get_oscal_schema`'s description/docstring mentions both JSON and XSD/XML in `Returns` (Property 4). Use the Strands tool's spec or the original function's `__doc__`, whichever the existing tool-registry tests use.
- **Keep** the `open_schema_file` tests (`_success`, `_with_path_cleaning`, `_not_found`) unchanged. The helper doesn't change.

### Property-Based Tests

- Exhaustive parametrized property over `VALID_MODELS × {"json","xsd"}` (18 cases): XSD output equals the raw file text; JSON output equals `json.dumps(json.loads(raw))` and parses equal to the file; exactly one handle opened, and it's closed (Properties 1–3).
- Hypothesis: `st.text().filter(lambda s: s not in {"json", "xsd"})` for `schema_type` → `ValueError` with the exact message, no file opened (Property 3).
- Hypothesis: `st.text().filter(lambda s: s not in VALID_MODELS)` × `st.sampled_from(["json", "xsd"])` → `ValueError` "Invalid model", no file opened (Property 3).
- Pass `ctx=None` in property tests. `try_notify_client_error` accepts `None`, so the tests don't need per-example mock setup. Hypothesis tests must not use function-scoped pytest fixtures; create mocks inside the test body.

### Integration Tests

`tests/test_integration.py` needs updating, because both schema tests use a plain `Mock()` as the file handle and would fail under `with`:
- `test_get_schema_tool_integration`: currently mocks both `open_schema_file` and `json.load`, so it doesn't test integration at all. Remove both mocks and call `get_oscal_schema(mock_context, "catalog", "json")` against the real bundled file. Assert the parsed result equals the bundled catalog JSON and has `"$schema"`. Add an XSD counterpart that asserts the result starts with `<xs:schema` (or equals the file text) and that the client was not notified of an error.
- `test_schema_file_integration`: checks file-name resolution for every model with mocked I/O. Switch the mock to a pass-through wrapper (`side_effect=open_schema_file`) so the real files are read, keep the `assert_called_with(expected_filename)` checks, and extend the loop to `schema_type` in `{"json","xsd"}`.
- `tests/test_integration_stdio_smoke.py` (real server over stdio): optionally add one `get_oscal_schema` call with `schema_type="xsd"` to confirm the fix end to end through MCP serialization. Do this only if the smoke test already calls tools in a way that makes it a one-line addition; otherwise the unit and integration tests above cover it.
