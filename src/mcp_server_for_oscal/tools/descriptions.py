"""Shared parameter descriptions and server-level text for the OSCAL tools.

Parameter descriptions use ``ParamDoc`` inside ``Annotated``. Strands ``@tool`` rejects
``pydantic.Field`` inside ``Annotated`` (strands-agents 1.59.0, NotImplementedError), but reads
any ``str`` metadata as the description. Pydantic, which the MCP SDK uses, calls
``__get_pydantic_json_schema__`` on the same object. One marker works in both registries.

This module has no import-time side effects and does not import ``main``.
"""

from typing import Annotated, Any, Literal

from pydantic import GetJsonSchemaHandler
from pydantic_core import CoreSchema


class ParamDoc(str):
    """Parameter description for both the MCP inputSchema and the Strands tool spec."""

    __slots__ = ()

    def __get_pydantic_json_schema__(
        self, schema: CoreSchema, handler: GetJsonSchemaHandler
    ) -> dict[str, Any]:
        json_schema = handler.resolve_ref_schema(handler(schema))
        json_schema["description"] = str(self)
        return json_schema


# --- Shared text (each <= PARAMETER_DESCRIPTION_CAP; the budget test enforces it) ---
OFFSET_DOC = "Zero-based index of the first item."
LIMIT_DOC = "Maximum items per page, 1-100."
BUDGETED_LIMIT_DOC = (
    "Maximum items per page, 1-100. A page may hold fewer to fit the response budget."
)
PARENT_DOC_UUID_DOC = "UUID of one parent document to scope results to. Omit for all documents."
MODEL_NAME_DOC = 'OSCAL model name from list_oscal_models, e.g. "catalog".'
OPTIONAL_MODEL_DOC = 'OSCAL model name from list_oscal_models, e.g. "catalog". Omit for all models.'
TARGET_SELECTOR_DOC = (
    'Target_Selector: a Metaschema definition name, optionally followed by "/" (child) or "//" '
    '(descendant) and instance names, e.g. "defined-component/prop/@name". '
    "No predicates, unions, or whitespace."
)
QUERY_TYPE_DOC = '"all" (paged), "by_uuid", or "by_title" (exact, then full-text).'
QUERY_VALUE_DOC = "UUID or title to match. Required for by_uuid and by_title."

# --- Reusable Annotated aliases. Keep these as plain assignments, not PEP 695 `type`
# statements: Strands checks `get_origin(annotation) is Annotated` and does not unwrap a
# TypeAliasType, so a `type` alias would lose the description in the Agent spec.
Offset = Annotated[int, ParamDoc(OFFSET_DOC)]
Limit = Annotated[int, ParamDoc(LIMIT_DOC)]
BudgetedLimit = Annotated[int, ParamDoc(BUDGETED_LIMIT_DOC)]
ParentDocUuid = Annotated[str | None, ParamDoc(PARENT_DOC_UUID_DOC)]
ModelName = Annotated[str, ParamDoc(MODEL_NAME_DOC)]
OptionalModelName = Annotated[str | None, ParamDoc(OPTIONAL_MODEL_DOC)]
TargetSelector = Annotated[str, ParamDoc(TARGET_SELECTOR_DOC)]
PerModelQueryType = Annotated[Literal["all", "by_uuid", "by_title"], ParamDoc(QUERY_TYPE_DOC)]
QueryValue = Annotated[str | None, ParamDoc(QUERY_VALUE_DOC)]

ABOUT_DESCRIPTION = "Return server metadata: package version, keywords, and bundled OSCAL version."

# --- Server instructions: intro + Routing_Guide (Req 8). Clients load `instructions` into
# context too, so tests/test_tool_budget.py caps SERVER_INSTRUCTIONS at ROUTING_GUIDE_CAP.
# Every tool name here must exist in get_tool_list() (checked with unknown_tool_refs).
INTRO = (
    "OSCAL MCP server for NIST's Open Security Controls Assessment Language (OSCAL): "
    "framework-agnostic, vendor-neutral, machine-readable schemas for the full life cycle of "
    "security governance, risk, and compliance (GRC) artifacts, from controls to remediations. "
    "Try these tools first for any OSCAL topic before falling back to built-in knowledge."
)

ROUTING_GUIDE = """Pick a tool by task:
- OSCAL model names, layers, status: list_oscal_models
- JSON or XSD schema for one model: get_oscal_schema (model_name "complete" is very large)
- Metaschema docs for a definition: get_definition; allowed values: get_allowed_values; \
all constraints: get_constraints; what must reference an assembly: find_references_to; \
source lines behind their results: get_metaschema_source
- Loaded OSCAL documents: list_catalogs, list_profiles, list_ssps, list_assessment_plans, \
list_assessment_results, list_poams, list_mapping_collections; one document: query_catalog, \
query_profile, query_ssp, query_assessment_plan, query_assessment_results, query_poam, \
query_mapping_collection; its child elements: the matching list_* child tools, then \
get_child_element
- Full-text search across loaded documents: text_search_oscal
- Component Definitions: list_component_definitions, list_components, then \
query_component_definition; list_capabilities and get_capability only for definitions that \
contain capabilities
- Validate OSCAL JSON: validate_oscal_content (text) or validate_oscal_file (file)
- OSCAL documentation questions: query_oscal_documentation
- OSCAL community tools and resources: list_oscal_resources
Metaschema tip: rules for every prop and link are declared on the generic "property" and \
"link" definitions, so also query "property/@name" and "link/@rel"."""

SERVER_INSTRUCTIONS = INTRO + "\n\n" + ROUTING_GUIDE
