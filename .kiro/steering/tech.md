# Tech Stack & Build System

## Language & Runtime

- Python 3.11+ (tested on 3.11 and 3.12, default dev environment is 3.12)
- Package: `mcp-server-for-oscal`

## Build System

- Hatch (build system, environment management, scripts, test runner)
- Hatchling + hatch-vcs (build backend, version from git tags)
- uv (dependency resolution, pip operations)

## Core Dependencies

- `mcp` — Model Context Protocol SDK (FastMCP server)
- `oscal-bindings` (imported as `oscal_bindings`) — typed OSCAL Pydantic v2 models generated from the NIST JSON Schema (parsing, validation, serialization)
- `boto3` — AWS SDK (Bedrock Knowledge Base queries)
- `strands-agents` — agent framework
- `regex` — enhanced regex support

## Dev/Test Dependencies (devtest group)

- `pytest` + `pytest-asyncio` — test framework
- `hypothesis` — property-based testing
- `mypy` + `boto3-stubs` — static type checking
- `bandit` — security scanning
- `ruff` — linting and formatting (line-length 100, double quotes, space indent)

## Common Commands

```bash
# Full test suite (typing + pytest + coverage + bandit security scan)
hatch run tests

# Run a specific test
hatch test tests/tools/test_validate_oscal_content.py::TestValidateOscalContent

# Type checking only
hatch run typing

# Format and lint (drop --fix to check only; `hatch fmt` is deprecated)
hatch check fmt --fix
hatch check code --fix

# Run a script within the hatch environment
hatch run <path/to/script>

# Regenerate hash manifests for bundled content
hatch run rehash

# Update bundled OSCAL schemas from NIST
hatch run update-oscal-schemas

# Build for release (runs tests first)
hatch run release

# Start HTTP transport server (dev only)
hatch run http-server
```

## Configuration

- Runtime config via environment variables or CLI args (see `dotenv.example`)
- Config loaded in `src/mcp_server_for_oscal/config.py` using `python-dotenv`
- Key env vars: `BEDROCK_MODEL_ID`, `OSCAL_KB_ID`, `AWS_PROFILE`, `LOG_LEVEL`, `OSCAL_MCP_TRANSPORT`

## Code Quality Settings (ruff)

- Target: Python 3.11
- Line length: 100
- Double quotes, space indentation
- Lint rules are hatch's default ruleset plus the ignores in `[tool.ruff.lint]`; each ignore has a rationale comment in `pyproject.toml`
- Tests additionally exempt from: magic values, asserts, relative imports, private access, unused fixture args, and a few pytest-style rules (see `per-file-ignores`)
- Prefer a targeted `# noqa: RULE - reason` over a new global ignore
