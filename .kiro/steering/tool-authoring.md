---
inclusion: always
---

# Authoring MCP Tools

Every tool in `get_tool_list()` costs context in each client session: its description and `inputSchema` ship in every `tools/list` response. These rules keep that response small and accurate. Run commands through hatch and never prefix them with `cd` (see `hatch.md` and `no-cd-prefix.md`).

## How a tool is wired

1. Write a function decorated with Strands `@tool` in `tools/<module>.py`. Declare parameters with `ParamDoc` (see Parameter rules), put `ctx: Context | None = None` last, and use `offset`/`limit` plus `utils.paginate` or `utils.paginate_within_budget` (or the store's page response) for anything list-shaped.
2. Add it to `get_tool_list()`. The server (`mcp.add_tool`), the agent (through `oscal_agent.agent_tools()`), and the `.mcpb` manifest (`bin/build_mcpb.py`) all read this list.
3. The docstring is the tool description for clients and LLMs. Write it in the Description_Shape below; the MCPB manifest uses its summary sentence (`tool_budget.summary_sentence`). Write it for an LLM caller.
4. If the tool reads the store, use the module-level `_store` set by `init_store()`. Add that module's `init_store` call in `main._init_oscal_store()`, and make tests reset the singleton (the autouse `reset_oscal_store` only covers `query_component_definition`).
5. Report errors with `try_notify_client_error(msg, ctx)` and then raise or return an error dict.
6. Update the Sync_Targets below, and run `hatch run tests`: the Budget_Tests check the new tool with no test change.

## Description_Shape

- At most 3 sentences (`MAX_DESCRIPTION_SENTENCES`).
- Sentence 1 is the summary: it states what the tool returns and starts with one of the `SUMMARY_VERBS`: Return, List, Get, Find, Read, Look, Search, Validate, Query.
- Optional later sentences say when to use it and name related tools. A later sentence that starts with "Use" must start with "Use when".
- No `Args:`, `Returns:`, `Raises:`, `Yields:`, or `Parameters:` sections. Parameter text belongs in the `inputSchema` (the MCP SDK sends the whole docstring as the description).
- Never mention `ctx`.
- Every tool name you reference must exist in `get_tool_list()` (checked by `unknown_tool_refs`). A backticked snake_case name that is not a tool, parameter, or enum value (a result key, for example) must be in `EXTRA_IDENTIFIERS`; prefer camelCase or hyphenated keys instead.
- Cross-tool routing advice goes in the Routing_Guide (`ROUTING_GUIDE` in `tools/descriptions.py`), not in each tool.

## Parameter rules

- Describe every parameter with `Annotated[T, ParamDoc("...")]`. Use the shared aliases in `tools/descriptions.py` (`Offset`, `Limit`, `BudgetedLimit`, `ParentDocUuid`, `ModelName`, `OptionalModelName`, `TargetSelector`, `PerModelQueryType`, `QueryValue`) for any parameter that occurs in more than one tool; add a new alias there rather than repeating text.
- Don't use `pydantic.Field` inside `Annotated`: strands-agents 1.59.0 raises `NotImplementedError` at import. A plain `str` in `Annotated` is ignored by the MCP SDK; `ParamDoc` works in both registries.
- Define aliases as plain assignments (`Offset = Annotated[...]`), not PEP 695 `type` statements. Strands does not unwrap a `TypeAliasType`, so the Agent spec would lose the description.
- Keep defaults as plain values in the signature (`limit: Limit = 10`), never a `FieldInfo`, so direct Python calls keep working.
- Don't document `ctx`, and put it last. Keep the name `ctx` and the type `Context | None = None`: the MCP SDK hides it by type, and `oscal_agent.agent_tools()` hides it from the Agent by name.
- Use `Literal[...]` for closed value sets, so the schema has an `enum` and invalid values fail validation.
- Identifying parameters (UUIDs, names, query text, element IDs) are required: no default.
- List-shaped results take `offset: Offset = 0` and `limit: Limit = 10` (or `BudgetedLimit` with `paginate_within_budget`) and return the page envelope from `utils.paginate` or `utils.paginate_within_budget`.

## Limits

Source of truth: `src/mcp_server_for_oscal/tools/tool_budget.py`. The Budget_Tests in `tests/test_tool_budget.py` check the live `tools/list` response, and `hatch run tests` fails when a limit is exceeded.

| Limit | Value |
|---|---|
| Total_Budget (compact JSON of the `tools` array, `TOTAL_BUDGET`) | 38,979 characters |
| Tool_Description_Cap (`TOOL_DESCRIPTION_CAP`) | 1,200 characters |
| Parameter_Description_Cap (`PARAMETER_DESCRIPTION_CAP`) | 250 characters |
| Server instructions with Routing_Guide (`ROUTING_GUIDE_CAP`) | 3,000 characters |

Size-plan targets for descriptions (guidance; only the caps are tested):

- Per-model `query_*`, document `list_*`, child `list_*`, `get_child_element`, `text_search_oscal`: ≤ 180
- Component Definition tools: ≤ 300 (`query_component_definition` ≤ 450)
- Metaschema tools: ≤ 500
- Schema, models, validate, resources, documentation, `about`: ≤ 250

If a new tool doesn't fit the Total_Budget, shorten descriptions or reuse shared aliases before raising a limit. Changing a limit means editing `tool_budget.py` and this file together.

## Sync_Targets

Update these when you add, remove, or change a tool:

- `bin/build_mcpb.py`, which fills the tool list of the `conf/mcpb/manifest.json` template from `get_tool_list()` summaries
- `src/mcp_server_for_oscal/tools/README.md`
- `tests/test_tool_registry.py` and `tests/test_query_oscal_models_child_elements.py` (exact tool sets and counts)
- The Routing_Guide (`ROUTING_GUIDE`) in `src/mcp_server_for_oscal/tools/descriptions.py`
- `tests/test_tool_param_parity.py`, which checks that the Server and Agent expose the same parameters and descriptions
