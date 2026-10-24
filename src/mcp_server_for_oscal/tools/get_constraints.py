"""
Tool for listing the OSCAL Metaschema constraints that apply at a point in an OSCAL model.

Generalizes ``get_allowed_values``' structural matcher (design OQ1) from ``allowed-values`` to
all seven constraint types. A bare definition name lists every constraint declared on that
Definition and on all local Definitions nested in it (Requirement 8.3). All Metaschema data
comes from ``metaschema_index`` (Requirement 7.1); shared checks and matching come from
``metaschema_common``.
"""

import logging

from mcp.server.mcpserver import Context
from strands import tool

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


@tool()
def get_constraints(
    model: str,
    target: str,
    constraint_types: list[str] | None = None,
    offset: int = 0,
    limit: int = 25,
    ctx: Context | None = None,
) -> dict:
    """List the OSCAL Metaschema constraints that apply at a point in an OSCAL model, e.g. "Which rules does OSCAL Metaschema place on a defined-component?", "Which datatype must a defined-component's release-date prop value have?", or "How many addresses must a metadata location have?".

    Answers come from the Metaschema constraints bundled with the server: `allowed-values`,
    `matches` (datatype or regex), `index` and `index-has-key` (UUID and ID references),
    `is-unique`, `has-cardinality`, and `expect` (co-occurrence rules). The generated JSON and
    XML schemas do not express most of these.

    `target` is a Target_Selector: a Metaschema definition name, optionally followed by `/`
    (child) or `//` (descendant) and a path in instance names. Examples:
    "defined-component/prop/@name", "system-component/@type",
    "defined-component/responsible-role", "part//part/@name". With a path, only constraints whose target is that path are returned.
    A bare definition name such as "defined-component" returns every constraint declared on
    that definition and on the local definitions nested in it, whatever their targets. Do not
    include predicates, unions, or whitespace: Metaschema predicates are returned per item as
    `namespace` and `conditions` instead.

    Only constraints declared on the named definition (and on its flags' definitions) are
    returned. Constraints on generic definitions such as `property` and `link` apply too;
    query those names as well.

    Args:
        model: OSCAL model name, e.g. "component-definition" or "system-security-plan". Use
            list_oscal_models for valid names.
        target: Target_Selector, 1-512 characters, as described above.
        constraint_types: Optional list of constraint types to keep, from "allowed-values",
            "matches", "index", "index-has-key", "is-unique", "has-cardinality", "expect".
            Omitted or empty means all types.
        offset: Zero-based index of the first item to return (default 0).
        limit: Maximum number of items to return, 1-100 (default 25). A page may hold fewer
            items to stay within the response size budget; `limit` then reports the reduced
            size.
        ctx: MCP server context (injected automatically by the MCP server).

    Returns:
        dict: Page_Response with `items`, `total`, `offset`, `limit`, and `hasMore`, ordered by
        where each constraint is declared. Each item has `type`, `definition`, `target` (the
        constraint's Metapath target), `level`, `source-file`, and `line` (where the
        constraint is declared in the bundled Metaschema), and when applicable
        `constraint-id`, `namespace`, `conditions`, `flag-definition`, and `truncated`, plus
        per type: `allow-other`, `extensible`, `value-count` (allowed-values; use
        get_allowed_values for the values); `datatype`, `regex` (matches); `name`,
        `key-fields` (index, index-has-key, is-unique); `min-occurs`, `max-occurs`
        (has-cardinality); `test`, `message` (expect).
    """
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
