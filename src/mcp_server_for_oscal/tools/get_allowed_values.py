"""
Tool for looking up the values OSCAL Metaschema ``allowed-values`` constraints permit.

Matching is structural (design OQ1): a constraint matches when one of its non-cross-document
Normalized_Path alternatives equals the remainder of the caller's Target_Selector. All
Metaschema data comes from ``metaschema_index`` (Requirement 7.1); selector parsing comes from
``metaschema_paths``; argument checks, suggestions, and the matcher are shared with the other
Metaschema tools through ``metaschema_common``.
"""

import logging

from mcp.server.mcpserver import Context
from strands import tool

# Re-exported: callers and tests import the suggestion helpers from this module.
from mcp_server_for_oscal.tools.metaschema_common import (
    Match,
    add_namespace_and_conditions,
    check_model,
    check_page,
    check_selector,
    collect_matches,
    flag_definitions,
    matching_alternatives,
    suggest_definition_names,
    unknown_definition_message,
)
from mcp_server_for_oscal.tools.metaschema_common import (
    bounded_levenshtein as _bounded_levenshtein,  # noqa: F401 - re-exported for tests
)
from mcp_server_for_oscal.tools.metaschema_index import (
    Constraint,
    Definition,
    EnumValue,
    MetaschemaLoadError,
    ModelScope,
    get_index,
)
from mcp_server_for_oscal.tools.metaschema_paths import (
    compose_selector,
    parse_selector,
    validate_selector,
)
from mcp_server_for_oscal.tools.utils import (
    paginate,
    paginate_within_budget,
    try_notify_client_error,
)

__all__ = ["get_allowed_values", "suggest_definition_names"]

logger = logging.getLogger(__name__)

ALLOWED_VALUES = "allowed-values"

# No-match suggestions (Requirement 5.8).
MAX_SUGGESTED_TARGETS = 20


@tool()
def get_allowed_values(
    model: str,
    target: str,
    offset: int = 0,
    limit: int = 25,
    ctx: Context | None = None,
) -> dict:
    """Look up the values OSCAL Metaschema allows at a point in an OSCAL model, e.g. "Is asset-type an OSCAL prop name on defined-component, and are other names allowed?", "Which component types does an SSP accept?", or "Which link rel values can a service component use?".

    Answers come from the Metaschema `allowed-values` constraints bundled with the server (the
    primary source behind the OSCAL JSON and XML schemas, which do not list these values).

    `target` is a Target_Selector: a Metaschema definition name, optionally followed by `/`
    (child) or `//` (descendant) and a path in instance names. Examples:
    "defined-component/prop/@name", "system-component/@type", "defined-component/link/@rel",
    "property/@name", "part//part/@name". A bare definition name such as "defined-component"
    returns the values constrained on that definition itself. Do not include predicates,
    unions, or whitespace: Metaschema predicates are returned per value as `namespace` and
    `conditions` instead (e.g. a `conditions` entry `{"step": "(.)", "predicate":
    "@type='service'"}` means the value applies only to service components).

    Only constraints declared on the named definition (and on its flags' definitions) are
    returned. Values that apply to every prop or link are declared on the generic `property`
    and `link` definitions, so also query "property/@name" and "link/@rel" for those.

    Args:
        model: OSCAL model name, e.g. "component-definition" or "system-security-plan". Use
            list_oscal_models for valid names.
        target: Target_Selector, 1-512 characters, as described above.
        offset: Zero-based index of the first item to return (default 0).
        limit: Maximum number of items to return, 1-100 (default 25). A page may hold fewer
            items to stay within the response size budget; `limit` then reports the reduced
            size.
        ctx: MCP server context (injected automatically by the MCP server).

    Returns:
        dict: Page_Response with `items`, `total`, `offset`, `limit`, and `hasMore`. Each item
        has `value`, `description`, `allow-other` (true if values outside the list are
        allowed), `definition`, `target` (the constraint's Metapath target), `level`,
        `source-file`, and `line` (where the value is declared in the bundled Metaschema), and
        when applicable `namespace`, `conditions`, `extensible`, `deprecated`,
        `constraint-id`, `flag-definition`, and `truncated`. When nothing matches, the
        response also has `suggested-targets`: selectors on the same definition that do
        return values.
    """
    logger.debug(
        "get_allowed_values(model: %s, target: %s, offset: %s, limit: %s)",
        model,
        target,
        offset,
        limit,
    )

    # Argument checks, all before any index access (Requirements 5.9, 5.11, 5.12).
    try:
        model_type = check_model(model)
        definition_name, remainder = check_selector(target)
        check_page(offset, limit)
    except ValueError as err:
        try_notify_client_error(str(err), ctx)
        raise

    try:
        scope = get_index().scope(model_type)
    except MetaschemaLoadError as err:
        # The message is client-safe by construction (RC-13).
        try_notify_client_error(str(err), ctx)
        raise

    definitions = scope.definitions(definition_name)
    if not definitions:
        msg = unknown_definition_message(definition_name, scope)
        try_notify_client_error(msg, ctx)
        raise ValueError(msg)

    matches = collect_matches(scope, definitions, remainder, (ALLOWED_VALUES,))
    items = [
        _allowed_value_item(match, enum, definition_name)
        for match in matches
        for enum in match.constraint.enums
    ]
    if not items:
        page = paginate([], offset, limit)
        page["suggested-targets"] = _suggested_targets(scope, definition_name, definitions)
        return page
    return paginate_within_budget(items, offset, limit, truncatable=("description",))


# --------------------------------------------------------------------------------------
# Item building (Requirement 6)
# --------------------------------------------------------------------------------------


def _allowed_value_item(match: Match, enum: EnumValue, definition_name: str) -> dict:
    constraint = match.constraint
    item: dict = {
        "value": enum.value,
        "description": enum.description.plain() if enum.description is not None else "",
        "allow-other": constraint.attrs.get("allow-other") == "yes",
        "definition": definition_name,
        "target": constraint.target,
        "level": constraint.level,
        "source-file": enum.location.source_file,
        "line": enum.location.line,
    }
    if (extensible := constraint.attrs.get("extensible")) is not None:
        item["extensible"] = extensible
    add_namespace_and_conditions(item, match.alternatives)
    if enum.deprecated is not None:
        item["deprecated"] = enum.deprecated
    if constraint.id is not None:
        item["constraint-id"] = constraint.id
    if match.flag_definition is not None:
        item["flag-definition"] = match.flag_definition
    return item


# --------------------------------------------------------------------------------------
# No-match suggestions (Requirement 5.8)
# --------------------------------------------------------------------------------------


def _is_valid_selector(selector: str) -> bool:
    try:
        validate_selector(selector)
    except ValueError:
        return False
    return True


def _has_values(constraint: Constraint) -> bool:
    return constraint.type == ALLOWED_VALUES and bool(constraint.enums)


def _suggested_targets(
    scope: ModelScope, name: str, definitions: tuple[Definition, ...]
) -> list[str]:
    """Up to 20 distinct, sorted selectors on ``name`` that each return at least one item.

    Candidates come from the same rules the matcher uses, so each one is productive as long
    as it is a valid selector that parses back to the same ``(name, remainder)`` (checked)
    and its constraint has at least one enum.
    """
    candidates: dict[str, str] = {}  # selector -> remainder it must parse back to
    for definition in definitions:
        for constraint in definition.constraints:
            if not _has_values(constraint):
                continue
            for alt in constraint.alternatives:
                if alt.cross_document or alt.path is None:
                    continue
                candidates[compose_selector(name, alt.path)] = alt.path
        for slot in definition.flags:
            flag = slot.effective_name
            if any(
                _has_values(c) and matching_alternatives(c, "")
                for flag_def in flag_definitions(scope, definition, flag)
                for c in flag_def.constraints
            ):
                candidates[f"{name}/@{flag}"] = f"@{flag}"
    productive = [
        selector
        for selector, remainder in candidates.items()
        if _is_valid_selector(selector) and parse_selector(selector) == (name, remainder)
    ]
    return sorted(productive)[:MAX_SUGGESTED_TARGETS]
