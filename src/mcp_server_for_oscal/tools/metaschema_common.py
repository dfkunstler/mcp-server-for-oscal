"""
Shared helpers for the Metaschema tools (``get_allowed_values``, ``get_constraints``, ...).

Everything here is pure: helpers raise ``ValueError`` (or return data) and never notify the
client or touch the ``get_index()`` singleton. Each tool module calls ``get_index()`` and
``try_notify_client_error`` itself, so tests can patch those names per tool module.
"""

import bisect
from collections.abc import Collection
from dataclasses import dataclass, field

from mcp_server_for_oscal.tools.metaschema_index import (
    Constraint,
    Definition,
    ModelScope,
)
from mcp_server_for_oscal.tools.metaschema_paths import (
    Condition,
    PathAlternative,
    normalize_target,
    parse_selector,
    validate_selector,
)
from mcp_server_for_oscal.tools.utils import OSCALModelType, paginate

# Unknown-definition suggestions (Requirement 5.10, RC-12).
MAX_SUGGESTIONS = 10
MAX_SUGGEST_CHARS = 64  # edit distance uses only this many characters of the segment
MAX_SHOWN_CHARS = 80  # the segment (or model) is named in messages up to this length

MODEL_VALUES = frozenset(m.value for m in OSCALModelType)


# --------------------------------------------------------------------------------------
# Argument checks (Requirements 5.9, 5.11, 5.12, 8.10, 8.11)
# --------------------------------------------------------------------------------------


def shown(text: str) -> str:
    """``text`` cut to ``MAX_SHOWN_CHARS`` for a message, marked when cut."""
    if len(text) <= MAX_SHOWN_CHARS:
        return text
    return text[:MAX_SHOWN_CHARS] + "..."


def check_model(model: object) -> OSCALModelType:
    """Return the ``OSCALModelType`` for ``model``, else raise ``ValueError`` (5.9)."""
    if isinstance(model, str) and model in MODEL_VALUES:
        return OSCALModelType(model)
    name = shown(model) if isinstance(model, str) else type(model).__name__
    raise ValueError(
        f"Invalid model: '{name}'. Use the tool list_oscal_models to get valid model names."
    )


def check_selector(target: object) -> tuple[str, str]:
    """Validate and parse a Target_Selector into ``(definition, remainder)`` (5.11)."""
    if not isinstance(target, str):
        msg = f"Invalid target selector: expected a string, got {type(target).__name__}"
        raise ValueError(msg)  # noqa: TRY004 - tools raise ValueError for every bad argument
    validate_selector(target)
    return parse_selector(target)


def check_page(offset: int, limit: int) -> None:
    """Raise ``utils.paginate``'s own ``ValueError`` for an out-of-range offset or limit."""
    paginate([], offset, limit)


# --------------------------------------------------------------------------------------
# Unknown-definition suggestions (bounded Levenshtein, RC-12)
# --------------------------------------------------------------------------------------


def bounded_levenshtein(a: str, b: str, cutoff: int) -> int:
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
        distance = bounded_levenshtein(query, name.lower(), cutoff)
        if distance > cutoff:
            continue
        candidate = (distance, name)
        if len(best) < MAX_SUGGESTIONS:
            bisect.insort(best, candidate)
        elif candidate < best[-1]:
            best.pop()
            bisect.insort(best, candidate)
    return [name for _, name in best]


def unknown_definition_message(segment: str, scope: ModelScope) -> str:
    """Client-safe message for a first selector segment that names no Definition (5.10)."""
    names = suggest_definition_names(segment, scope.definition_names())
    return (
        f"Unknown definition '{shown(segment)}': no Metaschema definition with that name in "
        f"this model. Nearest definition names: {', '.join(names)}"
    )


# --------------------------------------------------------------------------------------
# Matching (Requirements 5.2-5.6, 8.2, RC-2)
# --------------------------------------------------------------------------------------


@dataclass(slots=True)
class Match:
    """One matched Constraint and the alternatives it matched through."""

    constraint: Constraint
    alternatives: list[PathAlternative] = field(default_factory=list)
    flag_definition: str | None = None  # set when matched through a Flag_Slot (5.4)


def matching_alternatives(constraint: Constraint, path: str) -> list[PathAlternative]:
    """Non-cross-document alternatives of ``constraint`` whose Normalized_Path is ``path``."""
    return [alt for alt in constraint.alternatives if not alt.cross_document and alt.path == path]


def flag_definitions(scope: ModelScope, definition: Definition, flag: str) -> list[Definition]:
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


def sort_matches(matches: Collection[Match]) -> list[Match]:
    """Matches ordered by the Constraint's Source_Location (5.7, 8.5).

    Callers pass matches in discovery order (a dict preserves it), so the stable sort is
    deterministic on equal keys.
    """
    return sorted(
        matches,
        key=lambda m: (m.constraint.location.source_file, m.constraint.location.line),
    )


def collect_matches(
    scope: ModelScope,
    definitions: tuple[Definition, ...],
    remainder: str,
    types: Collection[str],
) -> list[Match]:
    """Constraints of ``types`` matching ``remainder``, deduplicated, sorted (5.2-5.7).

    A Constraint on one of ``definitions`` matches when a non-cross-document alternative's
    path equals ``remainder``. When ``remainder`` is ``@<flag>``, Constraints with an empty
    path on the flag Definitions of matching Flag_Slots also match (5.4, RC-2).
    """
    matches: dict[int, Match] = {}

    def add(constraint: Constraint, alts: list[PathAlternative], flag_def: str | None) -> None:
        match = matches.get(id(constraint))
        if match is None:
            match = matches[id(constraint)] = Match(constraint, flag_definition=flag_def)
        match.alternatives.extend(a for a in alts if a not in match.alternatives)

    flag = remainder[1:] if remainder.startswith("@") and "/" not in remainder else None
    for definition in definitions:
        for constraint in definition.constraints:
            if constraint.type in types and (alts := matching_alternatives(constraint, remainder)):
                add(constraint, alts, None)
        if flag is None:
            continue
        for flag_def in flag_definitions(scope, definition, flag):
            for constraint in flag_def.constraints:
                if constraint.type in types and (alts := matching_alternatives(constraint, "")):
                    add(constraint, alts, flag_def.key.name)

    return sort_matches(matches.values())


# --------------------------------------------------------------------------------------
# Item content shared by every tool (Requirements 6.4, 6.5)
# --------------------------------------------------------------------------------------


def namespace_and_conditions(
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


def add_namespace_and_conditions(item: dict, alternatives: list[PathAlternative]) -> None:
    """Set ``namespace`` and ``conditions`` on ``item`` when they exist (6.4, 6.5)."""
    namespace, conditions = namespace_and_conditions(alternatives)
    if namespace is not None:
        item["namespace"] = namespace
    if conditions:
        item["conditions"] = [{"step": c.step, "predicate": c.predicate} for c in conditions]
