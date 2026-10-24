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

from mcp_server_for_oscal.tools.descriptions import (
    BudgetedLimit,
    ModelName,
    Offset,
    TargetSelector,
)

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
    model: ModelName,
    target: TargetSelector,
    offset: Offset = 0,
    limit: BudgetedLimit = 25,
    ctx: Context | None = None,
) -> dict:
    """Return the values OSCAL Metaschema `allowed-values` constraints permit at a target, with
    `allow-other`, `namespace` and `conditions` for predicates, and source file and line. Use
    when the JSON schema does not list the values; use get_constraints for other constraint
    types. Results are paged within a response budget, and an empty page lists
    `suggested-targets`."""
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
