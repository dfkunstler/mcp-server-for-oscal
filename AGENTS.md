# AGENTS.md

<!-- tags: agents, navigation, entry-point -->

Starting point for AI agents working in this repo. Detailed generated docs are in `.agents/summary/` (start with `index.md`). For product usage see [README.md](README.md); for setup and the env var table see [DEVELOPING.md](DEVELOPING.md); for process see [CONTRIBUTING.md](CONTRIBUTING.md).

## Contents

- [Rules that always apply](#rules-that-always-apply): hatch-only execution, no `cd`, oscal-bindings-first, git and branch policy (pointers to `.kiro/steering`)
- [Code map](#code-map): where each subsystem lives and its entry points
- [How a tool is wired](#how-a-tool-is-wired): what to touch when you add or change an MCP tool
- [OscalStore essentials](#oscalstore-essentials): DB modes, lazy indexing, integrity
- [Repo-specific scripts](#repo-specific-scripts): hatch scripts that are not obvious
- [Non-obvious conventions and gotchas](#non-obvious-conventions-and-gotchas)
- [CI and release facts](#ci-and-release-facts)
- [Custom Instructions](#custom-instructions)

## Rules that always apply

<!-- tags: steering, policy, hatch, git -->

These rules are defined in `.kiro/steering/`. Read the files for detail; they take precedence over this summary.

| Rule | Source |
|---|---|
| Run all Python through `hatch` (`hatch test`, `hatch run <script>`, `hatch run python ...`). Never call `python`, `pytest`, `mypy`, `ruff`, or `bandit` directly, and pass pytest flags after `--` | `hatch.md` |
| Never prefix commands with `cd`; set the working directory instead. Repeat this rule to any subagent you delegate to | `~/.kiro/steering/no-cd-prefix.md` |
| Use `oscal-bindings` (`oscal_bindings.models` classes via `MODEL_MAP`, `model_validate`) for OSCAL models, validation, and serialization; never the union `parse_*` helpers; document why if you don't | `oscal-bindings.md` |
| Work on a feature branch tied to a GitHub issue, put `#<issue>` in commits, run `hatch run tests` before committing, and stage only your own files. Never commit to `main` or push without explicit approval | `git-strategy.md` |
| `structure.md`, `product.md`, and `tech.md` are partly stale (see `.agents/summary/review_notes.md`). Prefer this file and the code | |

## Code map

<!-- tags: layout, components, entry-points -->

| Path | What lives there | Entry point |
|---|---|---|
| `src/mcp_server_for_oscal/main.py` | MCP server (`MCPServer`, MCP SDK v2), CLI args, startup integrity check, store init, tool registration, `about` tool | `main()`; console scripts `mcp-server-for-oscal`, `server` |
| `src/mcp_server_for_oscal/oscal_agent.py` | Strands + Bedrock agent: `create_oscal_agent()`, session (file/S3) and conversation managers, observability hook | `main()`; `oscal-agent` |
| `src/mcp_server_for_oscal/config.py` | `Config` singleton `config`, read from env and `.env` at import | |
| `src/mcp_server_for_oscal/tools/__init__.py` | `get_tool_list()`: the single tool registry used by both server and agent | |
| `tools/oscal_store.py` | `OscalStore`: SQLite + FTS5 index of all OSCAL docs, lazy child extraction, oscal-bindings model parsing with an LRU cache | |
| `tools/query_component_definition.py` | Component Definition tools (capability-first, scoped by cdef UUID or exact title) | |
| `tools/query_oscal_models.py` | `query_*`/`list_*` for the other 7 model types, child-element listers, `text_search_oscal`, `get_child_element` | |
| `tools/validate_oscal_content.py` | 4-level validation: JSON → JSON Schema (`regex` pattern handler) → model (oscal-bindings via `MODEL_MAP`) → `oscal-cli` if on PATH | |
| `tools/query_documentation.py`, `tools/list_oscal_resources.py` | Bedrock KB query with a local FTS fallback; awesome-oscal list read from the DB | |
| `tools/utils.py` | `OSCALModelType`, `MODEL_MAP` (the single source of oscal-bindings model classes per model type), schema loading, bundled OSCAL version, `verify_package_integrity`, `paginate`, MCP client logging | |
| `src/mcp_server_for_oscal/oscal_schemas/` | NIST JSON/XSD schemas + `hashes.json` (verified at startup; exit 2 on mismatch) | |
| `data/` | Source content for the bundled DB (AWS cdef zip, awesome-oscal, fetched OSCAL-Pages). Not shipped in the wheel | |
| `bin/` | DB build, hash, MCPB, and content-update scripts | |
| `conf/mcpb/`, `conf/agentcore/`, `conf/powers/oscal/` | MCP Bundle template, AgentCore Dockerfile, Kiro Power | |
| `tests/` | pytest + hypothesis; `tools/` mirrors `src/.../tools`; shared fixtures in `conftest.py` and `fixture_store.py` | |
| `.kiro/specs/<feature>/` | Requirements/design/tasks history for past features; read before reworking a feature | |

## How a tool is wired

<!-- tags: mcp-tools, registration, extension -->

1. Write a function decorated with Strands `@tool` in `tools/<module>.py`. Take `ctx: Context | None = None` and use `offset`/`limit` plus `utils.paginate` (or the store's page response) for anything list-shaped.
2. Add it to `get_tool_list()`. The server (`mcp.add_tool`), the agent, and the `.mcpb` manifest (`bin/build_mcpb.py`) all read this list.
3. The docstring is the tool description for clients and LLMs; the MCPB manifest uses its first paragraph. Write it for an LLM caller.
4. If the tool reads the store, use the module-level `_store` set by `init_store()`. Add that module's `init_store` call in `main._init_oscal_store()`, and make tests reset the singleton (the autouse `reset_oscal_store` only covers `query_component_definition`).
5. Report errors with `try_notify_client_error(msg, ctx)` and then raise or return an error dict.
6. Update `src/mcp_server_for_oscal/tools/README.md`. `test_tool_registry.py` and `test_query_oscal_models_child_elements.py` assert the exact tool sets and counts.

## OscalStore essentials

<!-- tags: store, sqlite, integrity, data -->

- DB modes: `bundled` (default; the verified `oscal_store.db` is copied to a temp dir), `persistent` (`OSCAL_STORE_DB_PATH`; seeded from the bundled DB if the file is missing), `ephemeral` (empty temp DB when the bundled DB is missing or fails its hash check). A hash mismatch is not fatal; it only falls back.
- The bundled DB and its manifest (`src/mcp_server_for_oscal/oscal_store.db`, `src/mcp_server_for_oscal/hashes.json`) are gitignored build artifacts. Run `hatch run build-db` to create them locally. Without them the server starts with an empty store.
- Scanning (`scan_directory`) ingests `.json`, `.zip` members, and `.md` (as `model_type='documentation'`), validates through the oscal-bindings model class from `MODEL_MAP`, and stores `raw_json` with `indexed=0`. Child elements and FTS rows are extracted on first access (`_ensure_indexed`).
- Child types per model live in `CHILD_ELEMENT_TYPES`. Catalog controls are extracted recursively through nested groups.
- SQLite connections are thread-local (MCP runs sync tools on worker threads). Use the public store API; tests reject private-attribute access from cdef tools.
- Dynamic SQL composes internal fragments only, with values bound. Each site has `# nosec B608`.

## Repo-specific scripts

<!-- tags: hatch-scripts, build, content -->

All of these are defined under `[tool.hatch.envs.default.scripts]` in `pyproject.toml`.

| Script | Use when |
|---|---|
| `hatch run tests` | Required before commit: mypy, `hatch test --exitfirst --all --cover` (3.13 + 3.14), bandit. Reports go to `private/docs/` |
| `hatch run build-db` | After changing `data/` content or store schema/extraction logic; rebuilds `oscal_store.db` and its hash |
| `hatch run rehash` | After changing files in `oscal_schemas/` or `data/*`; regenerates `hashes.json` and runs `git add` on the manifests |
| `hatch run update-oscal-schemas` | After bumping `CURRENT_RELEASE_VERSION` in `bin/update-oscal-schemas.sh`; then `rehash`. That variable also drives the README OSCAL badge and `TestBundledOscalVersion`. Also requires a matching `oscal-bindings` release: `TestBundledOscalVersion` fails unless `oscal_bindings.__oscal_schema_version__` equals the bundled schema version |
| `hatch run update` | Re-lock `requirements.txt` after editing dependencies (`UV_CONSTRAINT` pins every env to it) |
| `hatch run release` | Full CI pipeline locally: tests, fetch NIST docs, build-db, build, build-mcpb (needs `uv` and `npx`) |
| `hatch run http-server` / `hatch run oscal-agent` | Dev-only streamable-http server (no auth) / agent CLI (needs Bedrock) |

## Non-obvious conventions and gotchas

<!-- tags: conventions, gotchas, lint, testing -->

- Ruff: line length 100, double quotes. The ignored rules are listed with rationale in `pyproject.toml` (broad excepts, lazy imports, and f-string logging are deliberate). Prefer a targeted `# noqa: RULE - reason` over a new global ignore.
- `.git-blame-ignore-revs` lists the repo-wide ruff format commit.
- The `MCPServer` and `config` are created at import time, so tests patch `mcp_server_for_oscal.<module>.config` rather than environment variables.
- `MODEL_MAP` in `tools/utils.py` is the single source of OSCAL model classes; the store and validator both read it. Don't map or import model classes elsewhere.
- Tools return stored OSCAL JSON (child `raw_json`) with hyphenated keys. Never return Pydantic models from tools; when you must serialize, pass `by_alias=True, exclude_none=True` explicitly.
- The OSCAL version reported by `about` comes from the schema `$id` and is never hard-coded.
- `README.md` must keep its first-line `<!-- mcp-name: io.github.dfkunstler/mcp-server-for-oscal -->`, which must match `server.json` `name` (tested).
- Env vars are declared in `config.py`, `dotenv.example`, DEVELOPING.md, and partially in `server.json` and `conf/mcpb/manifest.json` + `conf/mcpb/src/server.py` (`USER_CONFIG_ENV`). Keep them in sync.
- `OSCAL_COMPONENT_DEFINITIONS_DIR` is deprecated and has no effect; use `OSCAL_DOCUMENTS_DIR`.
- Known bugs (XSD schema retrieval #13, KB routing with empty `OSCAL_KB_ID` #14, the awesome-oscal workflow path #15) are listed in `.agents/summary/review_notes.md`. Check there before "fixing" behavior that tests appear to rely on.
- `tests/test_integration_stdio_smoke.py` starts the real server over stdio. By default it launches the installed console script; `OSCAL_SMOKE_SERVER_CMD` (JSON argv array) overrides the command, and `OSCAL_SMOKE_BUNDLE_DIR` launches an unpacked `.mcpb` bundle from its manifest (the CI `mcpb` job uses this).
- `tests/test_python_version_sites.py` guards every Python-version site (pyproject, CI, docs). Update it when bumping the supported versions.
- After the default env moved to Python 3.14, existing local envs need `hatch env remove default` once.
- `OSCAL_TEST_MAX_EXAMPLES=<n>` caps every Hypothesis test's `max_examples` (including explicit `@settings`) via `tests/conftest.py`. CI sets it to 10 on Windows only, where each example's temp files and SQLite setup are slow; leave it unset locally and on Linux/macOS.

## CI and release facts

<!-- tags: ci, release, github-actions -->

- `build.yml` runs on pushes to `main`, `v*` tags, and PRs to `main`. It validates `server.json` with `mcp-publisher`, then runs `hatch run release` on Python 3.14 (`build` job). The `test` job runs `hatch test` on Linux, macOS, and Windows × 3.13/3.14. The `mcpb` job checks the bundle on macOS and Windows: it downloads the build's `.mcpb`, validates/packs/unpacks it, runs `uv sync`, and runs the stdio smoke test. Tag builds create a draft GitHub release (`draft-release` needs `build`, `test`, and `mcpb`); the tag must be on `main`.
- Publishing the draft triggers `release.yml`: PyPI trusted publishing, then the MCP Registry (version rewritten from the tag; `continue-on-error`).
- Every build re-downloads OSCAL-Pages from unpinned `main`, so bundled docs can change between builds.
- Dependabot (uv) runs weekly: minor and patch grouped, majors ignored.

## Custom Instructions
<!-- This section is for human and agent-maintained operational knowledge.
     Add repo-specific conventions, gotchas, and workflow rules here.
     This section is preserved exactly as-is when re-running codebase-summary. -->
