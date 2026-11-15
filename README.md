<!-- mcp-name: io.github.dfkunstler/mcp-server-for-oscal -->
<div align="center">

![Build Status](https://github.com/dfkunstler/mcp-server-for-oscal/workflows/CI/badge.svg)
![Version](https://img.shields.io/github/v/release/dfkunstler/mcp-server-for-oscal)
![PyPI - Python Version](https://img.shields.io/pypi/pyversions/mcp-server-for-oscal)
[![PyPI][pypi-badge]][pypi-url]
[![PyPI - Downloads](https://img.shields.io/pypi/dm/mcp-server-for-oscal)][pypi-url]
[![OSCAL](https://img.shields.io/badge/dynamic/regex?url=https%3A%2F%2Fraw.githubusercontent.com%2Fdfkunstler%2Fmcp-server-for-oscal%2Frefs%2Fheads%2Fmain%2Fbin%2Fupdate-oscal-schemas.sh&search=CURRENT_RELEASE_VERSION%3D%22(%5Cd.%5Cd.%5Cd)%22&replace=%241&label=OSCAL)][nist-oscal-url]
![GitHub License](https://img.shields.io/github/license/dfkunstler/mcp-server-for-oscal)

</div>

[pypi-badge]: https://img.shields.io/pypi/v/mcp-server-for-oscal.svg
[pypi-url]: https://pypi.org/project/mcp-server-for-oscal/
[nist-oscal-url]: https://pages.nist.gov/OSCAL/
[mcp-spec-url]: https://modelcontextprotocol.io/docs/getting-started/intro
[issues-url]: https://github.com/dfkunstler/mcp-server-for-oscal/issues/
[new-issue-url]: https://github.com/dfkunstler/mcp-server-for-oscal/issues/new

# MCP Server for OSCAL
<strong>
Instant OSCAL expertise for your favorite AI agent
</strong>

A Model Context Protocol (MCP) server that provides AI assistants (Claude, Cline, Kiro, Claude Code, etc.) with tools to work with NIST's Open Security Controls Assessment Language (OSCAL). Like many early adopters, we needed help implementing OSCAL proofs-of-concept to demonstrate value to business stakeholders. Perhaps due to limited availability of examples in the public domain, we found that most AI agents/LLMs alone produced inconsistent results related to OSCAL. The tools in this MCP server minimized that problem for our use-case and we hope it does the same for you. 

> [!TIP]
> To get started, see [Installation](#installation) below.

## Contents

- [Features](#features): what the tools cover, local-only operation, and integrity checks
- [What is OSCAL?](#what-is-oscal) and [What is MCP?](#what-is-mcp): background
- [How to use / examples](#how-to-use--examples): what to ask, plus walkthroughs of control tracing, validation and repair, Metaschema constraints, and cross-references
- [Installation](#installation): one-click MCP Bundle (`.mcpb`) for Claude Desktop, or the PyPI package with configuration for Kiro, Claude Desktop, and VS Code
- [Using your own OSCAL Content](#using-your-own-oscal-content): `OSCAL_DOCUMENTS_DIR` and store settings
- [OSCAL Agent](#oscal-agent): the standalone Strands/Bedrock agent, sessions, and conversation management
- [Development](#development), [Security](#security), [Known limitations](#known-limitations), [Related projects](#related-projects), [License](#license)

## Features
<!-- tags: features, overview, security -->
Together, the tools provided by this MCP server are meant to enable your preferred AI assistant to provide accurate, authoritative guidance about OSCAL architecture, models, use-cases, requirements, and implementation. You don't need to understand the tools to use them, but details are in the [tools](src/mcp_server_for_oscal/tools/) directory. 

The server is lightweight and meant to run locally without additional setup. By default, it uses `stdio` protocol for MCP transport. Do not attempt to use the server with `streamable-http` transport, as we've not yet implemented transport security or authentication. 

The default tools should not connect to any remote services or resources - all required content is bundled with the server. As a security measure, we've implemented basic file integrity verification for bundled content. At build-time we generate manifests including SHA-256 hashes of the bundled OSCAL schemas and the pre-built content database. Each time the server starts, both are verified against their manifests. A schema mismatch produces an error and prevents startup; a database mismatch causes the server to discard the bundled database and start with an empty one.

The tools cover OSCAL schemas and model metadata, Metaschema allowed-values, constraint, reference, and definition lookup plus source reading (`get_allowed_values`, `get_constraints`, `find_references_to`, `get_definition`, `get_metaschema_source`), multi-level content validation, Component Definition navigation, generic list and query tools for every OSCAL model type (`list_documents`, `query_documents`, and `list_child_elements`, covering catalogs, profiles, component definitions, SSPs, assessment plans and results, POA&Ms, mapping collections), full-text search, community resources, and documentation. Search tools come in search/fetch pairs: `text_search_oscal` returns element IDs plus `parentDocumentUuid` that `get_child_element` resolves, and `query_oscal_documentation` returns paged `{title, snippet}` passages, with a `docId` on local pages that `get_oscal_documentation` reads as a line range. Results never include filesystem paths. See the [tools README](src/mcp_server_for_oscal/tools/README.md) for the full list.

In addition to the MCP server, the package includes a standalone OSCAL agent built with [Strands Agents](https://github.com/strands-agents/sdk-python). See the [OSCAL Agent](#oscal-agent) section below.


## What is OSCAL?

OSCAL (Open Security Controls Assessment Language) is a set of framework-agnostic, vendor-neutral, machine-readable schemas developed by NIST that describe the full life cycle of GRC (governance, risk, compliance) artifacts, from controls to remediation plans. OSCAL enables automation of GRC workflows by replacing digital paper (spreadsheets, PDFs, etc.) with a standard-based structured data format. To learn more about OSCAL, [install this MCP server](#installation) then ask your AI. Or see the [official OSCAL website][nist-oscal-url].

## What is MCP?
MCP (Model Context Protocol) is an [open-source standard][mcp-spec-url] for connecting AI applications to external systems. Think of MCP like a USB-C port for AI applications. Just as USB-C provides a standardized way to connect electronic devices, MCP provides a standardized way to connect AI applications to external systems. 

## How to use / examples
You don't call the tools yourself. Ask your AI assistant a question in plain language and it picks the tools. The table shows the kinds of questions the server answers well; the walkthroughs after it show real tool calls and results (abridged) from the bundled content.

| You want to... | Ask something like | Tools the assistant uses |
|---|---|---|
| Learn OSCAL | "What's the difference between a component definition and an SSP?" "Which OSCAL models are GA?" | `query_oscal_documentation`, `get_oscal_documentation`, `list_oscal_models` |
| Author a document | "Generate a minimal valid assessment plan that tests AC-2 with one interview and one examine activity, then validate it." | `get_oscal_schema`, `get_definition`, `validate_oscal_content` |
| Fix a document | "Why won't `ssp.json` validate? Fix it." | `validate_oscal_file`, `get_allowed_values`, `get_constraints` |
| Know what's legal where the schema is silent | "What values can `prop/@name` take on an SSP component?" "What `link/@rel` values are allowed?" | `get_allowed_values`, `get_constraints` |
| Keep references intact | "What in an SSP has to point at a party UUID?" "If I delete this component, what breaks?" | `find_references_to` |
| Go to the source | "Show me the Metaschema lines that define `implemented-requirement`." | `get_definition`, `get_metaschema_source` |
| Map cloud services to controls | "Which AWS Config rules implement Security Hub control S3.17?" "What does RDS.10 check?" | `text_search_oscal`, `get_child_element`, `query_component_definition` |
| Explore your own content | "List the findings in my latest assessment results that aren't satisfied." "Which controls in my SSP mention MFA?" | `list_documents`, `list_child_elements`, `text_search_oscal` (with [`OSCAL_DOCUMENTS_DIR`](#using-your-own-oscal-content)) |
| Find community content | "Are there OSCAL catalogs for CIS or the Australian ISM?" | `list_oscal_resources` |

### Example 1: Trace an AWS Config rule to the control it implements
The bundled content includes AWS service component definitions and the AWS Security Hub controls catalog. The assistant searches both, then follows the link from a component's `control-implementations` to the catalog control.

```markdown
> Which AWS Config rule checks that S3 buckets use KMS encryption, and which Security Hub control does it satisfy?

Running tool text_search_oscal
 ⋮  { "query_text": "S3 bucket encryption" }
    → 133 hits; top hit: component "s3-default-encryption-kms" in "Amazon Simple Storage Service"

Running tool get_child_element
 ⋮  { "element_id": "bb20a1e2-7a95-4925-8425-663c99f5594b",
 ⋮    "parent_doc_uuid": "2241b1e6-e268-4190-a6c5-0eaa5a9d961a" }

> The rule is S3_DEFAULT_ENCRYPTION_KMS. Its component in the Amazon S3 component definition:
  - Evaluates AWS::S3::Bucket, AWS::KMS::Key, and AWS::KMS::Alias resources
  - asset-type: automated-inspection
  - control-implementations → implemented-requirement control-id "S3.17"
    ("Implemented via AWS Config Rule")

  So a NON_COMPLIANT result from this rule is evidence against Security Hub control S3.17.
```

Follow-ups that work well: "What does RDS.10 check and how severe is it?" (`get_child_element` returns the control's overview, `SeverityLabel` MEDIUM, its assessment-method Config rule, and the remediation link), or "List every Config rule that implements an S3 control."

### Example 2: Validate and repair an SSP
`validate_oscal_content` and `validate_oscal_file` check four levels: JSON syntax, JSON Schema, the OSCAL model (via [oscal-bindings](https://pypi.org/project/oscal-bindings/)), and `oscal-cli` if it's installed. The assistant gets every error at once, so it can fix them in one pass.

```markdown
> Validate my draft SSP and fix whatever's wrong.

Running tool validate_oscal_content
 ⋮  { "content": "{\"system-security-plan\": {\"uuid\": \"not-a-uuid\", ...}}" }

 well_formedness  ✓
 json_schema      ✗ 6 errors
 model            ✗ 6 errors for SystemSecurityPlan:
   uuid                                          String should match the UUID v4/v5 pattern
   metadata.last-modified                        Input should have timezone info
   system-characteristics.status.state           'live' is not one of operational, under-development, ...
   system-characteristics.system-information.information-types   List should have at least 1 item
   system-implementation.users                   List should have at least 1 item
   control-implementation.implemented-requirements  List should have at least 1 item
 oscal_cli        skipped (not installed)

> Six problems. I'll generate a v4 UUID, set last-modified to "2026-11-15T00:00:00Z",
  change status to "operational", and add one information type, one user, and one
  implemented requirement as placeholders. Re-validating... all levels pass
  (oscal-cli skipped).
```

### Example 3: Answer "what's allowed here?" when the JSON Schema doesn't say
Many OSCAL rules live only in the NIST Metaschema constraints, not the generated JSON Schema. The Metaschema tools read the bundled Metaschema modules directly and cite the source file and line for every answer.

```markdown
> What values can I use for a component's type in an SSP? Can I make up my own?

Running tool get_allowed_values
 ⋮  { "model": "system-security-plan", "target": "system-component/@type" }
    → 17 values, constraint "oscal-component-type-values", allow-other: true

> 17 values are defined, including:
  - this-system      The system as a whole.
  - system           An external system (leveraged, or the other side of an interconnection).
  - interconnection  A connection to something outside this system.
  - software         Any software, operating system, or firmware.
  - hardware         A physical device.
  ...
  allow-other is true, so a custom value is legal, but tools that consume your SSP
  won't know what it means. (Source: oscal_implementation-common_metaschema.xml:242
  and shared-constraints/allowed-values-component-type.ent.)
```

Tip: rules for every `prop` and `link` are declared on the generic definitions, so ask about `property/@name` and `link/@rel` too.

### Example 4: Keep cross-references intact
`find_references_to` lists everything that Metaschema `index-has-key` constraints require to point at an assembly, including props and links that a search for `*-uuid` fields would miss.

```markdown
> I'm removing a party from my SSP's metadata. What else might reference it?

Running tool find_references_to
 ⋮  { "assembly": "party", "model": "system-security-plan" }

> Five places must resolve to a party UUID in metadata/party:
  - implemented-requirement → responsible-role[party-uuid], including inside
    statements and by-component          (oscal_ssp_metaschema.xml:736)
  - inventory-item → responsible-party/party-uuid
                                         (oscal_implementation-common_metaschema.xml:589)
  - action → responsible-party/party-uuid  (oscal_metadata_metaschema.xml:886)
  - party-uuid fields in general           (oscal_metadata_metaschema.xml:471)
  - party → member-of-organization, which must reference a party of type "organization"
                                         (oscal_metadata_metaschema.xml:331)
  Search your SSP for the party's UUID in each of these before deleting it.
```

### More things to try
- "Generate a minimal valid mapping collection that maps two controls, and validate it before you show me."
- "Show me the Metaschema definition of `by-component`, including the remarks the JSON Schema drops."
- "Which Security Hub controls are HIGH severity for EC2?"
- "Which AWS regions is EC2 available in, according to its component definition?"
- "Read the OSCAL docs page on profile resolution and summarize the merge options."
- With your own content loaded: "Which tasks in my assessment plan have no activities?" "Which controls in my assessment results have findings with no linked observations?" "Compare the components in my two component definitions."

## Installation
Follow these instructions to setup the MCP server for use with your preferred IDE, AI agent, chatbot, etc. The server is distributed two ways:

- **MCP Bundle (`.mcpb`)**: a single-file, one-click install for MCPB-compatible desktop apps such as Claude Desktop. Attached to every [GitHub release](https://github.com/dfkunstler/mcp-server-for-oscal/releases/latest). See [MCP Bundle](#mcp-bundle) below.
- **Python package on PyPI**: works with any MCP client. The `uvx` command (shown below) automatically downloads the latest version of the server from PyPI on startup.

> [!NOTE] 
> To setup for development instead, see [DEVELOPING.md](DEVELOPING.md)

### MCP Bundle

An [MCP Bundle](https://github.com/modelcontextprotocol/mcpb) packages the server, its manifest, and locked dependency versions into one file. The host app installs Python and the dependencies for you, so the prerequisites below don't apply.

1. Download `mcp-server-for-oscal-<version>.mcpb` from the [latest release](https://github.com/dfkunstler/mcp-server-for-oscal/releases/latest).
2. Open the file with your MCPB-compatible app. In Claude Desktop, double-click it or drag it onto **Settings > Extensions**, then click **Install**.
3. Optionally, configure the extension's settings. All are optional; the defaults run fully locally with the bundled content.

| Setting | Environment variable | Description |
|---|---|---|
| OSCAL documents directory | `OSCAL_DOCUMENTS_DIR` | Directory of your own OSCAL JSON files to index alongside the bundled content. See [Using your own OSCAL Content](#using-your-own-oscal-content). |
| Allow remote URIs | `OSCAL_ALLOW_REMOTE_URIS` | Allow tools to fetch OSCAL content from `http`/`https` URIs. Off by default. |
| Bedrock Knowledge Base ID | `OSCAL_KB_ID` | Amazon Bedrock Knowledge Base for documentation queries. Leave empty to use the bundled local documentation search. |
| AWS profile / AWS region | `AWS_PROFILE` / `AWS_REGION` | Credentials and region for Bedrock Knowledge Base queries. |
| Log level | `LOG_LEVEL` | `DEBUG`, `INFO` (default), `WARNING`, or `ERROR`. |

Unlike the `uvx` configurations below, which fetch the latest PyPI release on startup, a bundle stays at the version you installed. To upgrade, download and install the `.mcpb` from a newer release.

### Prerequisites

These apply to the PyPI package (every configuration below that uses `uvx`), not to the MCP Bundle.

- `uv` package manager for Python ([Installation instructions](https://docs.astral.sh/uv/getting-started/installation/))
- Python 3.13 or higher ([`uv python install 3.14`](https://docs.astral.sh/uv/guides/install-python/)). The server may work with later versions of Python, but we only test 3.13 & 3.14 for now.

### Configuring IDEs and AI Tools

This server communicates via stdio (standard input/output) and can be integrated with various IDEs and agentic tools that support the Model Context Protocol. The configuration examples shown below are for your preferred IDE / chatbot / agent.

#### Configuration Format

Most MCP-compatible tools use a JSON configuration format described in the [FastMCP documentation](https://gofastmcp.com/integrations/mcp-json-configuration). Here's the basic structure:

```json
{
  "mcpServers": {
    "oscal": {
      "command": "uvx",
      "args": ["mcp-server-for-oscal@latest"]
    }
  }
}
```

 For typical, runtime use of the MCP server, additional configuration should not be required. If needed, runtime environment variables can be configured in an `"env": {}` object as described in the [FastMCP documentation](https://gofastmcp.com/integrations/mcp-json-configuration#env-optional). See the file [dotenv.example](dotenv.example) for available options. 

> [!Note]
> A dotenv file is only needed in a development environment. 

#### Kiro Configuration

See [Kiro's MCP documentation](https://kiro.dev/docs/mcp/configuration/) for additional options. Add to your `.kiro/settings/mcp.json`:

```json
{
  "mcpServers": {
    "oscal": {
      "command": "uvx",
      "args": ["mcp-server-for-oscal@latest"],
      "env": {},
      "disabled": false,
      "autoApprove": [
        "*"
      ]
    }
  }
}
```

#### Claude Desktop
The easiest way to install is with the [MCP Bundle](#mcp-bundle). Claude Desktop manages Python and dependencies for you, and lets you configure optional settings such as a directory of your own OSCAL documents.

Alternatively, open **Settings > Developer > Edit Config** in Claude Desktop and add the following to `claude_desktop_config.json` (located at `~/Library/Application Support/Claude/claude_desktop_config.json` on macOS or `%APPDATA%\Claude\claude_desktop_config.json` on Windows):

```json
{
  "mcpServers": {
    "oscal": {
      "command": "uvx",
      "args": ["mcp-server-for-oscal@latest"]
    }
  }
}
```

#### VS Code
Run the `MCP: Open User Configuration` command, which opens the mcp.json file in your user profile. You can then manually add the server configuration to the file. See the [VSCode/Copilot docs](https://code.visualstudio.com/docs/copilot/customization/mcp-servers#_add-an-mcp-server) for additional options and details.

```json
{
  "servers": {
    "oscal": {
      "command": "uvx",
      "args": ["mcp-server-for-oscal@latest"]
    }
  }
}
```

## Using your own OSCAL Content

By default, the server ships with bundled OSCAL content (AWS component definitions, OSCAL documentation, and schemas). You can also point the server at a directory of your own OSCAL JSON files — catalogs, SSPs, profiles, assessment plans, assessment results, POA&Ms, or mapping collections. The server indexes them at startup and makes them available through all query and list tools.

Set the `OSCAL_DOCUMENTS_DIR` environment variable to the path of your OSCAL content directory. The path can be absolute or relative to the server's package directory. For example, in your MCP configuration:

```json
{
  "mcpServers": {
    "oscal": {
      "command": "uvx",
      "args": ["mcp-server-for-oscal@latest"],
      "env": {
        "OSCAL_DOCUMENTS_DIR": "/path/to/your/oscal/files"
      }
    }
  }
}
```

The server scans the directory recursively for `.json` files and indexes any valid OSCAL documents it finds. Your content is merged with the bundled content — both are queryable in the same session. Markdown (`.md`) files in the directory are indexed as documentation: `query_oscal_documentation` searches them (also when a Bedrock Knowledge Base is configured), and `get_oscal_documentation` reads them by `docId`.

These environment variables control the OSCAL store behavior:

| Variable | Default | Description |
|---|---|---|
| `OSCAL_DOCUMENTS_DIR` | _(empty)_ | Directory containing your OSCAL JSON files. Not scanned when empty. |
| `OSCAL_STORE_DB_PATH` | _(empty)_ | Path to a persistent SQLite database. When set, the index is reused across server restarts instead of being rebuilt each time. |
| `OSCAL_STORE_CACHE_SIZE` | `100` | Maximum number of parsed OSCAL documents to keep in the in-memory LRU cache. |

> [!TIP]
> Setting `OSCAL_STORE_DB_PATH` is recommended if you have a large collection of OSCAL documents. The server only needs to index new or changed files on subsequent startups. A database created by an older server version is upgraded in place on first start.

## OSCAL Agent

In addition to the MCP server, the package includes a standalone OSCAL agent built with [Strands Agents](https://github.com/strands-agents/sdk-python). The agent uses the same tools as the MCP server and can be run directly:

```bash
# Via entry point
oscal-agent

# Or via module
python -m mcp_server_for_oscal.oscal_agent
```

The agent requires AWS Bedrock access and uses the same configuration environment variables as the MCP server.

### Session Persistence

The agent supports session persistence so that conversation history and agent state survive across invocations. Choose between local filesystem or Amazon S3 storage:

```bash
# File-based session storage (new session)
oscal-agent --session-storage file

# Resume a previous session
oscal-agent --session-storage file --session-id <your-session-id>

# S3-based session storage
oscal-agent --session-storage s3 --session-s3-bucket my-bucket
```

When session storage is enabled without a `--session-id`, a UUID is auto-generated and displayed so you can resume later. Session defaults can also be set via environment variables (see [DEVELOPING.md](DEVELOPING.md)).

### Conversation Management

For long-running workflows, you can select a conversation manager to control how the agent's context window is maintained:

```bash
# Summarize older messages to keep context manageable
oscal-agent --session-storage file --conversation-manager summarizing

# Sliding window (SDK default behavior)
oscal-agent --conversation-manager sliding-window

# No conversation management
oscal-agent --conversation-manager null
```

Existing tools and features cover a variety of use-cases but are _far from_ comprehensive. Please share your feedback, feature requests, questions, or bug reports in a [GitHub issue][new-issue-url]. Direct [contributions](CONTRIBUTING.md) are wanted and welcome. 

## Development
See [DEVELOPING](DEVELOPING.md) to get started.

## Security
See [CONTRIBUTING](CONTRIBUTING.md#security-issue-notifications) for more information.

## Known limitations
- It's not yet possible to use this MCP server with ChatGPT due to limited MCP support by ChatGPT.
- The `streamable-http` transport does not yet implement authentication or transport security. Use `stdio` for production workloads.

## Related projects
Experimental Component Definitions for AWS services are bundled with this MCP server. You can find that content in the AWS Labs project [OSCAL Content for AWS Services](https://github.com/awslabs/oscal-content-for-aws-services).

MCP Server for OSCAL uses [oscal-bindings](https://pypi.org/project/oscal-bindings/) for its typed OSCAL models, including model-level content validation.

## License
This project is licensed under the [Apache-2.0](LICENSE) License.
