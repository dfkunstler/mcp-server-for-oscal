"""
Tool for listing the OSCAL Metaschema constraints that apply at a point in an OSCAL model.

Generalizes ``get_allowed_values``' structural matcher (design OQ1) from ``allowed-values`` to
all seven constraint types. A bare definition name lists every constraint declared on that
Definition and on all local Definitions nested in it (Requirement 8.3). All Metaschema data
comes from ``metaschema_index`` (Requirement 7.1); shared checks and matching come from
``metaschema_common``.
"""

import logging
from typing import Annotated, Literal

from mcp.server.mcpserver import Context
from strands import tool

from mcp_server_for_oscal.tools.descriptions import (
    BudgetedLimit,
    ModelName,
    Offset,
    ParamDoc,
    TargetSelector,
)
from mcp_server_for_oscal.tools.metaschema_common import (
    Match,
    add_namespace_and_conditions,
    check_model,
    check_page,
    check_selector,
    collect_matches,
    shown,
    sort_matches,
    unknown_definition_message,
)
from mcp_server_for_oscal.tools.metaschema_index import (
    CONSTRAINT_TYPES,
    Definition,
    DefinitionKey,
    MetaschemaLoadError,
    ModelScope,
    get_index,
)
from mcp_server_for_oscal.tools.utils import paginate_within_budget, try_notify_client_error

logger = logging.getLogger(__name__)

# Type-specific attributes copied verbatim into items, per constraint type (Requirement 8.6).
# ``allowed-values`` items also get ``allow-other`` and ``value-count``; ``index``,
# ``index-has-key``, and ``is-unique`` items get ``key-fields``; ``expect`` items get
# ``message``. Those are built in ``_constraint_item``.
_TYPE_ATTRS: dict[str, tuple[str, ...]] = {
    "allowed-values": ("extensible",),
    "matches": ("datatype", "regex"),
    "index": ("name",),
    "index-has-key": ("name",),
    "is-unique": (),
    "has-cardinality": ("min-occurs", "max-occurs"),
    "expect": ("test",),
}
_KEY_FIELD_TYPES = frozenset({"index", "index-has-key", "is-unique"})

# The closed set of constraint types as a schema enum; a test keeps it equal to
# ``CONSTRAINT_TYPES``. ``_check_constraint_types`` still checks direct Python calls.
ConstraintType = Literal[
    "allowed-values",
    "matches",
    "index",
    "index-has-key",
    "is-unique",
    "has-cardinality",
    "expect",
]


@tool()
def get_constraints(
    model: ModelName,
    target: TargetSelector,
    constraint_types: Annotated[
        list[ConstraintType] | None,
        ParamDoc("Constraint types to keep. Omit or pass an empty list for all types."),
    ] = None,
    offset: Offset = 0,
    limit: BudgetedLimit = 25,
    ctx: Context | None = None,
) -> dict:
    """List the OSCAL Metaschema constraints at a target (`allowed-values`, `matches`, `index`,
    `index-has-key`, `is-unique`, `has-cardinality`, `expect`) with type-specific details and
    source file and line; a bare definition name returns every constraint declared on it and
    its local definitions. Use when you need rules the JSON schema does not express; use
    get_allowed_values for the values themselves. Results are paged within a response
    budget."""
    logger.debug(
        "get_constraints(model: %s, target: %s, constraint_types: %s, offset: %s, limit: %s)",
        model,
        target,
        constraint_types,
        offset,
        limit,
    )

    # Argument checks, all before any index access (Requirements 8.9, 8.10, 8.11).
    try:
        model_type = check_model(model)
        definition_name, remainder = check_selector(target)
        types = _check_constraint_types(constraint_types)
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

    if remainder:
        matches = collect_matches(scope, definitions, remainder, types)
    else:
        matches = _declared_matches(scope, definitions, types)
    items = [_constraint_item(match, definition_name) for match in matches]
    return paginate_within_budget(items, offset, limit, truncatable=("message", "test"))


# --------------------------------------------------------------------------------------
# Argument checks
# --------------------------------------------------------------------------------------


def _check_constraint_types(constraint_types: object) -> frozenset[str]:
    """The type filter as a set; all of ``CONSTRAINT_TYPES`` when omitted or empty (8.4, 8.9)."""
    valid = ", ".join(CONSTRAINT_TYPES)
    if constraint_types is None:
        return frozenset(CONSTRAINT_TYPES)
    if not isinstance(constraint_types, list | tuple):
        msg = (
            "Invalid constraint_types: expected a list of constraint types, got "
            f"{type(constraint_types).__name__}. Valid constraint types: {valid}"
        )
        raise ValueError(msg)  # noqa: TRY004 - tools raise ValueError for every bad argument
    invalid: list[str] = []
    for value in constraint_types:
        if isinstance(value, str) and value in CONSTRAINT_TYPES:
            continue
        name = f"'{shown(value)}'" if isinstance(value, str) else type(value).__name__
        if name not in invalid:
            invalid.append(name)
    if invalid:
        msg = f"Invalid constraint_types: {', '.join(invalid)}. Valid constraint types: {valid}"
        raise ValueError(msg)
    return frozenset(constraint_types) or frozenset(CONSTRAINT_TYPES)


# --------------------------------------------------------------------------------------
# Bare-name mode (Requirement 8.3)
# --------------------------------------------------------------------------------------


def _resolve(scope: ModelScope, key: DefinitionKey) -> tuple[Definition, ...]:
    """The Definition identified by ``key`` (empty if it is not in the scope)."""
    return tuple(d for d in scope.definitions(key.name) if d.key == key)


def _declared_matches(
    scope: ModelScope, definitions: tuple[Definition, ...], types: frozenset[str]
) -> list[Match]:
    """Constraints of ``types`` on ``definitions`` and their nested local Definitions.

    Walks ``Definition.nested`` (direct children only) recursively, visiting each Definition
    once, so a local Definition that shares the selector's name is not listed twice. Every
    alternative of a constraint counts as matched, since the target is not filtered.
    """
    seen: set[DefinitionKey] = set()
    found: dict[int, Match] = {}
    stack = list(reversed(definitions))
    while stack:
        definition = stack.pop()
        if definition.key in seen:
            continue
        seen.add(definition.key)
        for constraint in definition.constraints:
            if constraint.type in types and id(constraint) not in found:
                found[id(constraint)] = Match(constraint, list(constraint.alternatives))
        for key in reversed(definition.nested):
            stack.extend(_resolve(scope, key))
    return sort_matches(found.values())


# --------------------------------------------------------------------------------------
# Item building (Requirements 8.6, 8.7)
# --------------------------------------------------------------------------------------


def _constraint_item(match: Match, definition_name: str) -> dict:
    """One result item. ``definition`` follows Requirement 6.1.

    Through a Flag_Slot it is the selector's Definition (``flag-definition`` names the flag
    Definition); otherwise it is the declaring Definition, which in bare-name mode may be a
    nested local Definition.
    """
    constraint = match.constraint
    declaring = constraint.definition_key.name
    item: dict = {
        "type": constraint.type,
        "definition": definition_name if match.flag_definition is not None else declaring,
        "target": constraint.target,
        "level": constraint.level,
        "source-file": constraint.location.source_file,
        "line": constraint.location.line,
    }
    if constraint.id is not None:
        item["constraint-id"] = constraint.id
    if constraint.type == "allowed-values":
        item["allow-other"] = constraint.attrs.get("allow-other") == "yes"
        item["value-count"] = len(constraint.enums)
    for attr in _TYPE_ATTRS.get(constraint.type, ()):
        if (value := constraint.attrs.get(attr)) is not None:
            item[attr] = value
    if constraint.type in _KEY_FIELD_TYPES and constraint.key_fields:
        item["key-fields"] = list(constraint.key_fields)
    if constraint.type == "expect" and constraint.message is not None:
        item["message"] = constraint.message.plain()
    add_namespace_and_conditions(item, match.alternatives)
    if match.flag_definition is not None:
        item["flag-definition"] = match.flag_definition
    return item
