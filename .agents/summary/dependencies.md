# Dependencies

<!-- tags: dependencies, packages, toolchain, external-services -->

## Runtime (`[project].dependencies`, minimum versions; locked in `requirements.txt`)

| Package | Used for | Where |
|---|---|---|
| `mcp` (≥2.1.1) | `MCPServer`, `Context`, `MCPDeprecationWarning` | `main.py`, every tool (ctx type), `utils.py` |
| `strands-agents` (≥1.55.0) | `@tool` decorator on all tools; `Agent`, `BedrockModel`, hooks, retry, session/conversation managers | `tools/*`, `oscal_agent.py` |
| `compliance-trestle` (≥5.1.0) | OSCAL pydantic models (parse/validate), level-3 validation | `oscal_store.py`, `validate_oscal_content.py`, `query_component_definition.py` |
| `boto3` (≥1.42.7) | Bedrock KB retrieve, Bedrock model session | `query_documentation.py`, `oscal_agent.py` |
| `regex` | ECMA-262 Unicode property escapes in OSCAL schema patterns | `validate_oscal_content.py` |

Used directly but only installed as transitive dependencies:

| Package | Comes via | Used in |
|---|---|---|
| `python-dotenv` | mcp / strands | `config.py` |
| `jsonschema` | trestle / mcp | `validate_oscal_content.py` (lazy import) |
| `requests` | trestle / boto ecosystem | `validate_oscal_file` remote fetch |
| `anyio` | mcp | `utils.safe_log_mcp` |

A change to any of these upstream projects can drop them silently. See review_notes.

## Dev/test (`[dependency-groups].devtest` and `hatch-test` env)

`pytest`, `pytest-asyncio`, `hypothesis` (property tests, with `.hypothesis/` gitignored), `mypy` + stubs (`boto3-stubs`, `types-requests`, `types-PyYAML`, `types-regex`, `types-jsonschema`), `bandit`, `bedrock-agentcore-starter-toolkit`. Ruff is provided by hatch (`hatch check fmt` / `hatch check code`) and is not declared.

## Toolchain

| Tool | Role |
|---|---|
| hatch + hatchling + hatch-vcs | envs, scripts, build, test matrix, git-tag versioning |
| uv | installer for hatch envs; `uv pip compile` lock; `uv lock` inside the MCPB bundle |
| mise | pins python 3.12, uv, hatch 1.18.1 |
| Node.js / npx | `@anthropic-ai/mcpb@2` CLI for bundle validate/pack |
| jq, curl, unzip, zip | content update scripts |
| finch | `build-agentcore-container` |
| mcp-publisher | server.json validation (CI) and registry publish |
| oscal-cli (optional) | validation level 4 if on PATH |

## Dependency management

- `UV_CONSTRAINT={root}/requirements.txt` pins every hatch env, including `hatch-test`, to the lock.
- `hatch run update` re-locks with `--upgrade --universal --python-version 3.11`.
- Dependabot (`uv` ecosystem) runs weekly, groups minor and patch updates, ignores major versions, and has a cooldown (3 days by default, 30 for semver-major).

## External content sources

| Content | Upstream | Pinned by |
|---|---|---|
| OSCAL schemas | `usnistgov/OSCAL` release zip | `CURRENT_RELEASE_VERSION` in `bin/update-oscal-schemas.sh` |
| AWS component definitions | `awslabs/oscal-content-for-aws-services` tag | `CURRENT_RELEASE_VERSION` in `bin/update-aws-cdefs.sh` |
| OSCAL concept docs | `usnistgov/OSCAL-Pages` `main` | not pinned (fetched every release build) |
| Awesome OSCAL list | `oscal-club/awesome-oscal` `main` README | nightly workflow |
