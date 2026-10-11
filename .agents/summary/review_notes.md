# Review Notes

<!-- tags: review, consistency, completeness, bugs, recommendations -->

Generated alongside the summary. Items marked "verified" were confirmed by running code or reading the source. The others are findings from reading the docs against the code.

## Verified code bugs

| # | Issue | Evidence | Suggested fix |
|---|---|---|---|
| B1 (#13) | Resolved by #13: `get_oscal_schema(schema_type="xsd")` always failed | `get_schema.py` called `json.load()` on the XSD file, giving `JSONDecodeError`. `test_get_schema_success_xsd_schema` mocked `json.load`, which hid the bug | Fixed: `xsd` returns the raw file text and schema file handles are closed; the mocked test was replaced by tests that read the real bundled files |
| B2 (#14) | `query_oscal_documentation` calls Bedrock even when no KB is configured | The condition is `config.knowledge_base_id is not None`, but the default is `""` (verified `'' is not None → True`). Each call tries boto3, fails, logs a warning, then falls back. Tests set `knowledge_base_id = None`, which never happens at runtime | Use `if config.knowledge_base_id:`; make tests use `""` |
| B3 (#15) | Nightly awesome-oscal workflow never opens a PR | `update-awesome-oscal.yml` diffs and `add-paths` on `src/mcp_server_for_oscal/oscal_docs/awesome-oscal.md`, but the script writes `data/oscal_docs/awesome-oscal.md` | Point both paths at `data/oscal_docs/awesome-oscal.md` (and consider running `rehash` for `data/oscal_docs/hashes.json`) |
| B4 (#16) | `hatch run checkout-oscal-steering` cannot work | References the submodule `conf/powers/oscal/steering/oscal-pages`; the repo has no `.gitmodules` or `steering/` dir. It also uses `cd` inside a hatch script | Remove the script or restore the submodule |
| B5 (#17) | Tests leak SQLite files into `./MagicMock/config.oscal_store_db_path/` | Hundreds of files there; `str(MagicMock().oscal_store_db_path)` becomes a relative path. Gitignored, so it only wastes disk | Set `mock_config.oscal_store_db_path = ""` (or `tmp_path`) in the offending tests |

## Dead or unused code and configuration

- `OscalStore.load_external_component_definition`: no callers in `src/`, `tests/`, or `bin/`. Its docstring still references the removed `ComponentDefinitionStore`.
- `OSCAL_MAX_URI_DEPTH` / `config.max_uri_depth`: never read.
- `main --aws-profile` is parsed but never applied (`update_from_args` has no `aws_profile` parameter).

## Documentation inconsistencies

| Where | Says | Reality |
|---|---|---|
| `src/.../tools/README.md`, `conf/powers/oscal/POWER.md`, `.kiro/steering/product.md`, `.kiro/steering/tech.md` | `query_oscal_documentation` is "conditionally registered" / "only registered when a KB ID is configured" | Always registered; local FTS fallback exists (`test_tool_registry.py::TestProperty1UnconditionalToolInclusion`) |
| DEVELOPING.md env table | `OSCAL_KB_ID` default "tool not registered"; `OSCAL_STORE_DB_PATH` default "in-memory only" | Tool is always present; the default DB is a temp-file copy of the bundled DB |
| tools/README `about` | `oscal-version` "currently 1.2.1" | Derived from the schemas (1.2.3 in `update-oscal-schemas.sh`) |
| tools/README validation | "`mapping-collection` skips Level 3" (written when compliance-trestle was the model library) | `MappingCollection` is mapped in `utils.MODEL_MAP` (oscal-bindings, #26); `test_mapping_collection_validated` exists |
| tools/README `query_component_definition` | "Loads … from local directory (including zip files)", "Supports remote URI loading", "Maintains global indexes" | Legacy store behavior; reads exclusively from `OscalStore` |
| tools/README list tools | `list_component_definitions` / `list_components` / `list_capabilities` "return list of dictionaries" | Return a paginated page response |
| tools/README | "Optional: Set `OSCAL_AWS_PROFILE`" | Variable does not exist; it is `AWS_PROFILE` |
| README "Using your own OSCAL Content" | "Two additional environment variables" | Table lists three |
| README Features | "Each time the server starts, all content files are verified" | Only `oscal_schemas/` (fatal) and the bundled DB (non-fatal fallback) are verified |
| `.kiro/steering/structure.md` | `oscal_docs/` and `component_definitions/` under `src/`; no `oscal_store.py`, `query_oscal_models.py`, `data/`, `conf/mcpb` | Moved to `data/`; new modules exist (DEVELOPING.md has the current tree) |
| `tests/README.md` | Test tree lists 4 tool tests; recommends `python -m pytest`, `bandit`, `mypy --install-types` directly | Far more test files; the hatch steering file forbids direct invocation |
| `utils.verify_package_integrity` docstring | Example calls on `oscal_docs` | That directory no longer exists in the package |
| `server.json` env vars | 5 variables | Server honors many more (`OSCAL_DOCUMENTS_DIR`, `OSCAL_STORE_*`, `OSCAL_ALLOW_REMOTE_URIS`, ...); MCPB manifest exposes `OSCAL_DOCUMENTS_DIR`, which the registry entry lacks |
| DEVELOPING.md | "Setup Development Environment" block contains only `# Or using hatch` / `hatch shell` | Leftover fragment |

## Completeness gaps

- No architectural doc for `OscalStore` (mode resolution, lazy indexing, FTS fallback) outside code docstrings and `.kiro/specs/scalable-oscal-store`. This summary is the first consolidated description.
- Nothing explains that every CI build pulls OSCAL-Pages from unpinned `main`, so builds are not reproducible for bundled documentation.
- Resolved by #26: the model map was duplicated in `oscal_store.py` and `validate_oscal_content.py` under compliance-trestle; it is now the single `utils.MODEL_MAP` of oscal-bindings classes.
- The `anyio` import relies on a transitive dependency (via `mcp`), with nothing guarding against it disappearing. `python-dotenv`, `jsonschema`, and `requests` were declared directly in #26.
- `.kiro/specs/*` (17 feature specs) are the design history but are not linked from any human-facing doc.
- Language-support limits: shell scripts, workflows, and the Dockerfile were read manually and not symbol-analyzed. Their behavior (for example the scripts' `mkdir tmp` failing if `tmp/` already exists) was not exercised.

## Recommendations

1. Fix B2–B3 (small, user-visible; B1 was resolved by #13), each with a test that does not mock the failure away.
2. Make `src/.../tools/README.md` the single tool reference: regenerate its tables from `get_tool_list()` docstrings, as `bin/build_mcpb.py` already does, and have POWER.md and steering link to it instead of copying it.
3. Update or remove `.kiro/steering/structure.md` and `product.md`, since agents load them on every request and they are stale. AGENTS.md now carries the current map.
4. Declare `anyio` in `pyproject.toml` (the other directly-imported transitive packages were declared in #26).
5. Pin OSCAL-Pages to a commit, or record the fetched SHA in `data/oscal_docs/hashes.json`'s `commit` field.
6. Re-run this summary after structural changes; keep the `Custom Instructions` section of AGENTS.md hand-maintained.
