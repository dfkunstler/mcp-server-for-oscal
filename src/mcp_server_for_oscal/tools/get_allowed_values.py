"""
Tool for looking up the values OSCAL Metaschema ``allowed-values`` constraints permit.

Matching is structural (design OQ1): a constraint matches when one of its non-cross-document
Normalized_Path alternatives equals the remainder of the caller's Target_Selector. All
Metaschema data comes from ``metaschema_index`` (Requirement 7.1); selector parsing comes from
``metaschema_paths``.
"""

import bisect
import logging
from dataclasses import dataclass, field

from mcp.server.mcpserver import Context
from strands import tool

from mcp_server_for_oscal.tools.metaschema_index import (
    Constraint,
    Definition,
    EnumValue,
    MetaschemaLoadError,
    ModelScope,
    get_index,
)
from mcp_server_for_oscal.tools.metaschema_paths import (
    Condition,
    PathAlternative,
    compose_selector,
    normalize_target,
    parse_selector,
    validate_selector,
)
from mcp_server_for_oscal.tools.utils import (
    OSCALModelType,
    paginate,
    paginate_within_budget,
    try_notify_client_error,
)

logger = logging.getLogger(__name__)

ALLOWED_VALUES = "allowed-values"

# Unknown-definition suggestions (Requirement 5.10, RC-12).
MAX_SUGGESTIONS = 10
MAX_SUGGEST_CHARS = 64  # edit distance uses only this many characters of the segment
MAX_SHOWN_CHARS = 80  # the segment (or model) is named in messages up to this length

# No-match suggestions (Requirement 5.8).
MAX_SUGGESTED_TARGETS = 20

_MODEL_VALUES = frozenset(m.value for m in OSCALModelType)


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
    model_type = _check_model(model, ctx)
    definition_name, remainder = _check_selector(target, ctx)
    _check_page(offset, limit, ctx)

    try:
        scope = get_index().scope(model_type)
    except MetaschemaLoadError as err:
        # The message is client-safe by construction (RC-13).
        try_notify_client_error(str(err), ctx)
        raise

    definitions = scope.definitions(definition_name)
    if not definitions:
        msg = _unknown_definition_message(definition_name, scope)
        try_notify_client_error(msg, ctx)
        raise ValueError(msg)

    matches = _collect_matches(scope, definitions, remainder)
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
# Argument checks
# --------------------------------------------------------------------------------------


def _shown(text: str) -> str:
    """``text`` cut to ``MAX_SHOWN_CHARS`` for a message, marked when cut."""
    if len(text) <= MAX_SHOWN_CHARS:
        return text
    return text[:MAX_SHOWN_CHARS] + "..."


def _check_model(model: object, ctx: Context | None) -> OSCALModelType:
    if isinstance(model, str) and model in _MODEL_VALUES:
        return OSCALModelType(model)
    shown = _shown(model) if isinstance(model, str) else type(model).__name__
    msg = f"Invalid model: '{shown}'. Use the tool list_oscal_models to get valid model names."
    try_notify_client_error(msg, ctx)
    raise ValueError(msg)


def _check_selector(target: object, ctx: Context | None) -> tuple[str, str]:
    if not isinstance(target, str):
        msg = f"Invalid target selector: expected a string, got {type(target).__name__}"
        try_notify_client_error(msg, ctx)
        raise ValueError(msg)  # noqa: TRY004 - tools raise ValueError for every bad argument
    try:
        validate_selector(target)
        return parse_selector(target)
    except ValueError as err:
        try_notify_client_error(str(err), ctx)
        raise


def _check_page(offset: int, limit: int, ctx: Context | None) -> None:
    """Raise ``utils.paginate``'s own ``ValueError`` for an out-of-range offset or limit."""
    try:
        paginate([], offset, limit)
    except ValueError as err:
        try_notify_client_error(str(err), ctx)
        raise


# --------------------------------------------------------------------------------------
# Unknown-definition suggestions (bounded Levenshtein, RC-12)
# --------------------------------------------------------------------------------------


def _bounded_levenshtein(a: str, b: str, cutoff: int) -> int:
    """Levenshtein distance of ``a`` and ``b`` if it is at most ``cutoff``, else ``cutoff + 1``.

    Computes only the diagonal band of width ``2 * cutoff + 1`` and stops as soon as a whole
    row exceeds ``cutoff``. Cells outside the band are at least ``cutoff + 1`` in the full
    matrix, so the result equals the full-matrix distance whenever that is ``<= cutoff``.
    """
    over = cutoff + 1
    la, lb = len(a), len(b)
    if abs(la - lb) > cutoff:
        return over
    if la == 0 or lb == 0:
        return max(la, lb)
    prev = [j if j <= cutoff else over for j in range(lb + 1)]
    for i in range(1, la + 1):
        cur = [over] * (lb + 1)
        cur[0] = i if i <= cutoff else over
        row_min = cur[0]
        ch = a[i - 1]
        for j in range(max(1, i - cutoff), min(lb, i + cutoff) + 1):
            cost = 0 if ch == b[j - 1] else 1
            value = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + cost, over)
            cur[j] = value
            row_min = min(row_min, value)
        if row_min > cutoff:
            return over
        prev = cur
    return min(prev[lb], over)


def suggest_definition_names(segment: str, names: tuple[str, ...]) -> list[str]:
    """Up to ``MAX_SUGGESTIONS`` of ``names`` nearest to ``segment`` (Requirement 5.10).

    Ordered by (case-insensitive edit distance to the first ``MAX_SUGGEST_CHARS`` characters
    of ``segment``, name). Keeps the best candidates in a bounded sorted list; once it is
    full, its worst distance is the cutoff for the banded distance, so far-away names cost
    little. Names at exactly the cutoff are still computed exactly, so ties sort by name.
    """
    query = segment[:MAX_SUGGEST_CHARS].lower()
    best: list[tuple[int, str]] = []
    # Lowercasing can lengthen a string (e.g. "İ"), so bound by the compared lengths.
    initial_cutoff = max([len(query), *(len(n.lower()) for n in names)])
    for name in names:
        cutoff = best[-1][0] if len(best) == MAX_SUGGESTIONS else initial_cutoff
        distance = _bounded_levenshtein(query, name.lower(), cutoff)
        if distance > cutoff:
            continue
        candidate = (distance, name)
        if len(best) < MAX_SUGGESTIONS:
            bisect.insort(best, candidate)
        elif candidate < best[-1]:
            best.pop()
            bisect.insort(best, candidate)
    return [name for _, name in best]


def _unknown_definition_message(segment: str, scope: ModelScope) -> str:
    names = suggest_definition_names(segment, scope.definition_names())
    return (
        f"Unknown definition '{_shown(segment)}': no Metaschema definition with that name in "
        f"this model. Nearest definition names: {', '.join(names)}"
    )


# --------------------------------------------------------------------------------------
# Matching (Requirements 5.2-5.6, RC-2)
# --------------------------------------------------------------------------------------


@dataclass(slots=True)
class _Match:
    """One matched ``allowed-values`` Constraint and the alternatives it matched through."""

    constraint: Constraint
    alternatives: list[PathAlternative] = field(default_factory=list)
    flag_definition: str | None = None  # set when matched through a Flag_Slot (5.4)


def _matching_alternatives(constraint: Constraint, path: str) -> list[PathAlternative]:
    return [alt for alt in constraint.alternatives if not alt.cross_document and alt.path == path]


def _flag_definitions(scope: ModelScope, definition: Definition, flag: str) -> list[Definition]:
    """Flag Definitions behind ``definition``'s Flag_Slots whose effective name is ``flag``."""
    found: list[Definition] = []
    for slot in definition.flags:
        if slot.effective_name != flag:
            continue
        if slot.inline is not None:
            found.extend(d for d in scope.definitions(slot.inline.name) if d.key == slot.inline)
        elif slot.ref is not None:
            found.extend(scope.global_definition("flag", slot.ref))
    return found


def _collect_matches(
    scope: ModelScope, definitions: tuple[Definition, ...], remainder: str
) -> list[_Match]:
    """Matched constraints, deduplicated by identity, sorted by Source_Location (5.5, 5.7)."""
    matches: dict[int, _Match] = {}

    def add(constraint: Constraint, alts: list[PathAlternative], flag_def: str | None) -> None:
        match = matches.get(id(constraint))
        if match is None:
            match = matches[id(constraint)] = _Match(constraint, flag_definition=flag_def)
        match.alternatives.extend(a for a in alts if a not in match.alternatives)

    flag = remainder[1:] if remainder.startswith("@") and "/" not in remainder else None
    for definition in definitions:
        for constraint in definition.constraints:
            if constraint.type == ALLOWED_VALUES and (
                alts := _matching_alternatives(constraint, remainder)
            ):
                add(constraint, alts, None)
        if flag is None:
            continue
        for flag_def in _flag_definitions(scope, definition, flag):
            for constraint in flag_def.constraints:
                if constraint.type == ALLOWED_VALUES and (
                    alts := _matching_alternatives(constraint, "")
                ):
                    add(constraint, alts, flag_def.key.name)

    # dict preserves discovery order, so the stable sort is deterministic on equal keys.
    return sorted(
        matches.values(),
        key=lambda m: (m.constraint.location.source_file, m.constraint.location.line),
    )


# --------------------------------------------------------------------------------------
# Item building (Requirement 6)
# --------------------------------------------------------------------------------------


def _namespace_and_conditions(
    alternatives: list[PathAlternative],
) -> tuple[str | None, list[Condition]]:
    """``namespace`` and the ordered distinct Conditions across matched alternatives (6.4, 6.5).

    With exactly one distinct namespace, it is reported once and its Namespace_Conditions are
    left out of ``conditions``. Otherwise every predicate, including Namespace_Conditions, is
    reported verbatim.
    """
    namespaces = {alt.namespace for alt in alternatives if alt.namespace is not None}
    namespace = next(iter(namespaces)) if len(namespaces) == 1 else None
    conditions: list[Condition] = []
    for alt in alternatives:
        source = alt
        if namespace is None and alt.namespace is not None:
            # alt.raw has no top-level "|", so it normalizes to exactly one alternative.
            source = normalize_target(alt.raw, extract_namespace=False)[0]
        for condition in source.conditions:
            if condition not in conditions:
                conditions.append(condition)
    return namespace, conditions


def _allowed_value_item(match: _Match, enum: EnumValue, definition_name: str) -> dict:
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
    namespace, conditions = _namespace_and_conditions(match.alternatives)
    if namespace is not None:
        item["namespace"] = namespace
    if conditions:
        item["conditions"] = [{"step": c.step, "predicate": c.predicate} for c in conditions]
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
                _has_values(c) and _matching_alternatives(c, "")
                for flag_def in _flag_definitions(scope, definition, flag)
                for c in flag_def.constraints
            ):
                candidates[f"{name}/@{flag}"] = f"@{flag}"
    productive = [
        selector
        for selector, remainder in candidates.items()
        if _is_valid_selector(selector) and parse_selector(selector) == (name, remainder)
    ]
    return sorted(productive)[:MAX_SUGGESTED_TARGETS]
