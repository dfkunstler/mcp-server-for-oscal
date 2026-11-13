"""Shared data for the consolidate-tools feature (GitHub issue #40).

Holds the old-to-new mapping of the 29 Removed_Tools and the tool name sets of the
Tool_Registry after the removal. Data and one formatter only; the tests live in
``tests/test_removed_tools.py`` and the equivalence test modules.
"""

from dataclasses import dataclass
from typing import Final


@dataclass(frozen=True)
class Replacement:
    """The Generic_Tool call that replaces one Removed_Tool.

    ``args`` holds the fixed replacement arguments as ordered ``(name, value)`` pairs.
    Other arguments (``query_type``, ``query_value``, ``parent_doc_uuid``, ``offset``,
    ``limit``) pass through unchanged and are not listed.
    """

    tool: str
    args: tuple[tuple[str, str], ...]

    def kwargs(self) -> dict[str, str]:
        """Return the fixed arguments as keyword arguments for the replacement tool."""
        return dict(self.args)


def render_args(replacement: Replacement) -> str:
    """Render the arguments as the Mapping_Table cell text: ``name="value"`` joined by ``, ``."""
    return ", ".join(f'{name}="{value}"' for name, value in replacement.args)


def _query(model_type: str) -> Replacement:
    return Replacement("query_documents", (("model_type", model_type),))


def _list_docs(model_type: str) -> Replacement:
    return Replacement("list_documents", (("model_type", model_type),))


def _children(element_type: str) -> Replacement:
    return Replacement("list_child_elements", (("element_type", element_type),))


# The 29 rows of the design's old-to-new mapping table, in table order.
REMOVED_TOOLS: Final[dict[str, Replacement]] = {
    "query_catalog": _query("catalog"),
    "query_profile": _query("profile"),
    "query_ssp": _query("system-security-plan"),
    "query_assessment_plan": _query("assessment-plan"),
    "query_assessment_results": _query("assessment-results"),
    "query_poam": _query("plan-of-action-and-milestones"),
    "query_mapping_collection": _query("mapping-collection"),
    "list_catalogs": _list_docs("catalog"),
    "list_profiles": _list_docs("profile"),
    "list_ssps": _list_docs("system-security-plan"),
    "list_assessment_plans": _list_docs("assessment-plan"),
    "list_assessment_results": _list_docs("assessment-results"),
    "list_poams": _list_docs("plan-of-action-and-milestones"),
    "list_mapping_collections": _list_docs("mapping-collection"),
    "list_catalog_controls": _children("control"),
    "list_catalog_groups": _children("group"),
    "list_profile_imports": _children("import"),
    "list_profile_modify": _children("modify"),
    "list_ssp_control_implementations": _children("control-implementation"),
    "list_ssp_system_components": _children("system-component"),
    "list_assessment_plan_tasks": _children("task"),
    "list_assessment_plan_activities": _children("activity"),
    "list_assessment_results_results": _children("result"),
    "list_assessment_results_findings": _children("finding"),
    "list_poam_items": _children("poam-item"),
    "list_mapping_collection_mappings": _children("mapping"),
    "list_component_definitions": _list_docs("component-definition"),
    "list_components": _children("component"),
    "list_capabilities": _children("capability"),
}

# The Generic_Tools, in registry order.
GENERIC_TOOLS: Final[frozenset[str]] = frozenset(
    {"list_documents", "query_documents", "list_child_elements"}
)

# Tool_Registry tools that survive the removal (`about` is server-only and not in the registry).
RETAINED_TOOLS: Final[frozenset[str]] = frozenset(
    {
        "list_oscal_models",
        "get_oscal_schema",
        "get_allowed_values",
        "get_constraints",
        "find_references_to",
        "get_definition",
        "get_metaschema_source",
        "list_oscal_resources",
        "query_component_definition",
        "get_capability",
        "validate_oscal_content",
        "validate_oscal_file",
        "text_search_oscal",
        "get_child_element",
        "query_oscal_documentation",
        "get_oscal_documentation",
    }
)
