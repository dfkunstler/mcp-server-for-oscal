"""
Tool for reading one OSCAL Metaschema definition with its documentation and structure.

Returns every Definition with a given name (and optionally kind) in a model's Model_Scope or
in the whole bundle, as a budget-bounded Page_Response (Requirement 10, RC-6). All Metaschema
data comes from ``metaschema_index`` (Requirement 7.1); shared checks come from
``metaschema_common``.
"""

import logging
from typing import Annotated, Literal

from mcp.server.mcpserver import Context
from strands import tool

from mcp_server_for_oscal.tools.descriptions import (
    BudgetedLimit,
    Offset,
    OptionalModelName,
    ParamDoc,
)
from mcp_server_for_oscal.tools.metaschema_common import (
    check_model,
    check_page,
    shown,
    suggest_definition_names,
)
from mcp_server_for_oscal.tools.metaschema_index import (
    Definition,
    FlagSlot,
    MetaschemaLoadError,
    ModelChild,
    ModelScope,
    get_index,
)
from mcp_server_for_oscal.tools.utils import paginate_within_budget, try_notify_client_error

logger = logging.getLogger(__name__)

MAX_NAME_CHARS = 200  # Requirement 10.1
KINDS = ("assembly", "field", "flag")  # Requirement 10.9
# Metaschema's default as-type for fields and flags when none is declared.
DEFAULT_DATATYPE = "string"


@tool()
def get_definition(
    name: Annotated[
        str,
        ParamDoc(
            "Metaschema definition name, 1-200 characters, matched exactly (case-sensitive), "
            'e.g. "defined-component" or "uuid".'
        ),
    ],
    model: OptionalModelName = None,
    kind: Annotated[
        Literal["assembly", "field", "flag"] | None,
        ParamDoc("Definition kind to keep. Omit for all kinds."),
    ] = None,
    offset: Offset = 0,
    limit: BudgetedLimit = 10,
    ctx: Context | None = None,
) -> dict:
    """Return every OSCAL Metaschema definition with a given name, with formal name,
    description, remarks, flags, model children, constraint summaries, and source file and
    line. Use when you need documentation or structure that the generated JSON and XML schemas
    shorten or drop. Results are paged within a response budget, and long remarks are marked
    `truncated`."""
    logger.debug(
        "get_definition(name: %s, model: %s, kind: %s, offset: %s, limit: %s)",
        name,
        model,
        kind,
        offset,
        limit,
    )

    # Argument checks that need no index (Requirements 10.8, 10.9, 12.2).
    try:
        model_type = None if model is None else check_model(model)
        _check_kind(kind)
        check_page(offset, limit)
        if not isinstance(name, str):
            msg = f"Invalid definition name: expected a string, got {type(name).__name__}"
            raise ValueError(msg)  # noqa: TRY004 - tools raise ValueError for every bad argument
    except ValueError as err:
        try_notify_client_error(str(err), ctx)
        raise

    try:
        index = get_index()
        scope = index.all_modules() if model_type is None else index.scope(model_type)
    except MetaschemaLoadError as err:
        # The message is client-safe by construction (RC-13).
        try_notify_client_error(str(err), ctx)
        raise

    # Name checks list the nearest names, so they need the scope (Requirement 10.7).
    definitions: list[Definition] = []
    if 1 <= len(name) <= MAX_NAME_CHARS:
        definitions = [d for d in scope.definitions(name) if kind is None or d.key.kind == kind]
    if not definitions:
        msg = _no_match_message(name, kind, scope, searched_all=model_type is None)
        try_notify_client_error(msg, ctx)
        raise ValueError(msg)

    definitions.sort(key=lambda d: (d.key.location.source_file, d.key.location.line))
    items = [_definition_item(scope, d) for d in definitions]
    return paginate_within_budget(items, offset, limit, truncatable=("remarks",))


# --------------------------------------------------------------------------------------
# Argument checks and errors
# --------------------------------------------------------------------------------------


def _check_kind(kind: object) -> None:
    """Raise ``ValueError`` listing the valid kinds unless ``kind`` is omitted or valid (10.9)."""
    if kind is None or (isinstance(kind, str) and kind in KINDS):
        return
    shown_kind = f"'{shown(kind)}'" if isinstance(kind, str) else type(kind).__name__
    msg = f"Invalid kind: {shown_kind}. Valid kinds: {', '.join(KINDS)}"
    raise ValueError(msg)


def _candidate_names(scope: ModelScope, kind: str | None) -> tuple[str, ...]:
    """Sorted distinct Definition names in ``scope``, limited to ``kind`` when given."""
    names = scope.definition_names()
    if kind is None:
        return names
    return tuple(n for n in names if any(d.key.kind == kind for d in scope.definitions(n)))


def _no_match_message(name: str, kind: str | None, scope: ModelScope, *, searched_all: bool) -> str:
    """Client-safe message for an invalid or unmatched name, with nearest names (10.7)."""
    suggestions = ", ".join(suggest_definition_names(name, _candidate_names(scope, kind)))
    if not 1 <= len(name) <= MAX_NAME_CHARS:
        return (
            f"Invalid definition name: expected 1-{MAX_NAME_CHARS} characters, got "
            f"{len(name)}. Nearest definition names: {suggestions}"
        )
    where = "the bundled OSCAL Metaschema" if searched_all else "this model"
    what = f"{kind} definition" if kind is not None else "definition"
    return (
        f"Unknown definition '{shown(name)}': no Metaschema {what} with that name in {where}. "
        f"Nearest definition names: {suggestions}"
    )


# --------------------------------------------------------------------------------------
# Item building (Requirements 10.3, 10.4, 10.5)
# --------------------------------------------------------------------------------------


def _definition_item(scope: ModelScope, definition: Definition) -> dict:
    """One match; keys the Metaschema does not declare are omitted, so there are no nulls."""
    key = definition.key
    item: dict = {"kind": key.kind, "name": key.name, "scope": definition.scope}
    if definition.parent is not None:
        item["parent-definition"] = definition.parent.name
    if definition.formal_name:
        item["formal-name"] = definition.formal_name
    if definition.description is not None and (text := definition.description.plain()):
        item["description"] = text
    if definition.use_name:
        item["use-name"] = definition.use_name
    if definition.group_as:
        item["group-as"] = definition.group_as
    if definition.flags:
        item["flags"] = [_flag_item(scope, slot) for slot in definition.flags]
    if definition.model_children:
        item["model"] = [_child_item(scope, child) for child in definition.model_children]
    if definition.constraints:
        constraints = []
        for constraint in definition.constraints:
            summary = {"type": constraint.type, "target": constraint.target}
            if constraint.id is not None:
                summary["constraint-id"] = constraint.id
            constraints.append(summary)
        item["constraints"] = constraints
    # Remarks last, so a long value is easy to spot; truncation keeps every other key.
    if definition.remarks is not None and (text := definition.remarks.blocks()):
        item["remarks"] = text
    item["source-file"] = key.location.source_file
    item["line"] = key.location.line
    return item


def _flag_item(scope: ModelScope, slot: FlagSlot) -> dict:
    """``name``, ``required``, ``datatype`` (when resolvable), and ``ref`` or ``inline``."""
    item: dict = {"name": slot.effective_name, "required": slot.required}
    flag_def: Definition | None = None
    if slot.inline is not None:
        flag_def = next(
            (d for d in scope.definitions(slot.inline.name) if d.key == slot.inline), None
        )
        item["inline"] = True
    elif slot.ref is not None:
        flag_def = next(iter(scope.global_definition("flag", slot.ref)), None)
        item["name"] = _instance_name(slot.effective_name, slot.ref, flag_def)
        item["ref"] = slot.ref
    if flag_def is not None:
        item["datatype"] = flag_def.as_type or DEFAULT_DATATYPE
    return item


def _instance_name(effective_name: str, ref: str, target: Definition | None) -> str:
    """The instance name of a reference: its own use-name, else the target's use-name.

    The index records a reference's effective name as its ``use-name`` or, if none, its
    ``ref``. In the latter case the referenced global Definition's ``use-name`` applies
    (e.g. ``assembly ref="property"`` is named ``prop``).
    """
    if effective_name == ref and target is not None and target.use_name:
        return target.use_name
    return effective_name


def _child_item(scope: ModelScope, child: ModelChild) -> dict:
    """One model child: name, kind, occurrence bounds, and how it is declared."""
    name = child.effective_name
    if child.ref is not None:
        target = next(iter(scope.global_definition(child.kind, child.ref)), None)
        name = _instance_name(name, child.ref, target)
    item: dict = {
        "name": name,
        "kind": child.kind,
        "min-occurs": child.min_occurs,
        "max-occurs": child.max_occurs,
    }
    if child.group_as:
        item["group-as"] = child.group_as
    if child.ref is not None:
        item["ref"] = child.ref
    elif child.inline is not None:
        item["inline"] = True
    if child.in_choice:
        item["in-choice"] = True
    return item
