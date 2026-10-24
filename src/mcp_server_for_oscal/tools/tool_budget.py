"""Context budget for the tools/list response: limits and pure checks (single source of truth).

The Budget_Tests (``tests/test_tool_budget.py``) run these checks against the live ``tools/list``
response, and ``bin/build_mcpb.py`` reuses ``summary_sentence``. This module has no import-time
side effects and imports only the standard library.
"""

import json
import re
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

# Measured on main at c973674 (45 tools, Python 3.14, mcp 2.1.1) with tools_list_size().
BASELINE_SIZE = 55_685
TOTAL_BUDGET = BASELINE_SIZE * 70 // 100  # 38_979 (Req 1.2: at most 70%)
TOOL_DESCRIPTION_CAP = 1_200
PARAMETER_DESCRIPTION_CAP = 250
ROUTING_GUIDE_CAP = 3_000  # design addition, keeps the savings from moving into instructions
MAX_DESCRIPTION_SENTENCES = 3
SUMMARY_VERBS = frozenset(
    {"Return", "List", "Get", "Find", "Read", "Look", "Search", "Validate", "Query"}
)
# Backticked snake_case names in descriptions that are neither tools, parameters, nor enum
# values (e.g. result keys). Keep this list short; prefer camelCase or hyphenated keys.
EXTRA_IDENTIFIERS = frozenset({"raw_json", "ambiguous_element_id"})

ALL_TOOLS = "<all>"

KIND_TOOL_DESCRIPTION = "tool-description"
KIND_PARAMETER_DESCRIPTION = "parameter-description"
KIND_MISSING_PARAMETER_DESCRIPTION = "missing-parameter-description"
KIND_TOTAL_SIZE = "total-size"


def tools_list_size(tools: Sequence[Mapping[str, Any]]) -> int:
    """Characters in the compact JSON of a tools/list ``tools`` array."""
    return len(json.dumps(list(tools), separators=(",", ":"), ensure_ascii=False))


@dataclass(frozen=True)
class Violation:
    """One budget violation: what was measured, against which limit, and where."""

    tool: str
    parameter: str | None
    kind: str
    measured: int | None
    limit: int | None
    detail: str = ""

    def message(self) -> str:
        """Return a one-line message, e.g. ``tool=t parameter=p: kind length 312 > limit 250``."""
        where = f"tool={self.tool}"
        if self.parameter is not None:
            where += f" parameter={self.parameter}"
        text = f"{where}: {self.kind}"
        if self.measured is not None and self.limit is not None:
            text += f" length {self.measured} > limit {self.limit}"
        if self.detail:
            text += f" ({self.detail})"
        return text


def _properties(tool: Mapping[str, Any]) -> Mapping[str, Any]:
    schema = tool.get("inputSchema") or {}
    props = schema.get("properties") if isinstance(schema, Mapping) else None
    return props if isinstance(props, Mapping) else {}


def check_limits(tools: Sequence[Mapping[str, Any]]) -> list[Violation]:
    """Report each over-cap tool description and each missing, blank, or over-cap parameter
    description."""
    violations: list[Violation] = []
    for tool in tools:
        name = str(tool.get("name", ""))
        description = tool.get("description") or ""
        if len(description) > TOOL_DESCRIPTION_CAP:
            violations.append(
                Violation(name, None, KIND_TOOL_DESCRIPTION, len(description), TOOL_DESCRIPTION_CAP)
            )
        for param, prop in _properties(tool).items():
            param_desc = prop.get("description") if isinstance(prop, Mapping) else None
            if not isinstance(param_desc, str) or not param_desc.strip():
                violations.append(
                    Violation(
                        name,
                        param,
                        KIND_MISSING_PARAMETER_DESCRIPTION,
                        None,
                        None,
                        "description is missing or blank",
                    )
                )
            elif len(param_desc) > PARAMETER_DESCRIPTION_CAP:
                violations.append(
                    Violation(
                        name,
                        param,
                        KIND_PARAMETER_DESCRIPTION,
                        len(param_desc),
                        PARAMETER_DESCRIPTION_CAP,
                    )
                )
    return violations


def check_total(tools: Sequence[Mapping[str, Any]], budget: int = TOTAL_BUDGET) -> list[Violation]:
    """Report one violation when the compact JSON of ``tools`` is longer than ``budget``."""
    size = tools_list_size(tools)
    if size > budget:
        return [Violation(ALL_TOOLS, None, KIND_TOTAL_SIZE, size, budget)]
    return []


# --- Description shape -----------------------------------------------------------------------

_ABBREVIATION = re.compile(r"\b(?:e\.g|i\.e|etc)\.", re.IGNORECASE)
_PLACEHOLDER = "\x00"  # stands in for the dots of protected abbreviations
_SENTENCE_BREAK = re.compile(r"(?<=[.?!])\s+(?=[A-Z\"`(])")
_DOCSTRING_SECTION = re.compile(
    r"^\s*(Args|Arguments|Parameters|Returns|Raises|Yields):", re.MULTILINE
)
_CTX = re.compile(r"\bctx\b")
_FIRST_WORD = re.compile(r"[A-Za-z]+")


def split_sentences(text: str) -> list[str]:
    """Split text into sentences, keeping ``e.g.``, ``i.e.``, and ``etc.`` inside a sentence."""
    protected = _ABBREVIATION.sub(lambda m: m.group(0).replace(".", _PLACEHOLDER), text)
    collapsed = " ".join(protected.split())
    if not collapsed:
        return []
    return [s.replace(_PLACEHOLDER, ".") for s in _SENTENCE_BREAK.split(collapsed)]


def summary_sentence(description: str) -> str:
    """Return the first sentence of a description (empty string for an empty description)."""
    sentences = split_sentences(description)
    return sentences[0] if sentences else ""


def description_shape_problems(description: str) -> list[str]:
    """Return the ways a description breaks the Description_Shape (empty list when it fits)."""
    problems: list[str] = []
    section = _DOCSTRING_SECTION.search(description)
    if section:
        problems.append(f"contains a docstring section {section.group(1)}:")
    if _CTX.search(description):
        problems.append("mentions ctx")
    sentences = split_sentences(description)
    if not sentences:
        problems.append("description is empty")
        return problems
    if len(sentences) > MAX_DESCRIPTION_SENTENCES:
        problems.append(f"has {len(sentences)} sentences, more than {MAX_DESCRIPTION_SENTENCES}")
    first_word = _FIRST_WORD.match(sentences[0])
    if first_word is None or first_word.group(0) not in SUMMARY_VERBS:
        word = first_word.group(0) if first_word else sentences[0][:20]
        problems.append(
            f"summary sentence starts with {word!r}, not one of {sorted(SUMMARY_VERBS)}"
        )
    for sentence in sentences[1:]:
        if sentence.startswith("Use ") and not sentence.startswith("Use when"):
            problems.append(f"sentence starts with 'Use' but not 'Use when': {sentence[:40]!r}")
    return problems


# --- Unknown tool names ----------------------------------------------------------------------

_NAME = r"[a-z][a-z0-9]*(?:_[a-z0-9]+)+"
_BACKTICKED = re.compile(rf"`({_NAME})(?:\(\))?`")
_CALL_FORM = re.compile(rf"\b({_NAME})\(")
_BARE_VERB = re.compile(
    r"\b((?:list|get|query|validate|find|search|text_search)_[a-z0-9]+(?:_[a-z0-9]+)*)\b"
)


def _schema_strings(node: Any) -> Iterator[str]:
    """Yield every string ``enum`` member and ``const`` value anywhere in a JSON schema."""
    if isinstance(node, Mapping):
        for key, value in node.items():
            if key == "enum" and isinstance(value, list):
                yield from (v for v in value if isinstance(v, str))
            elif key == "const" and isinstance(value, str):
                yield value
            else:
                yield from _schema_strings(value)
    elif isinstance(node, list):
        for item in node:
            yield from _schema_strings(item)


def known_identifiers(tools: Sequence[Mapping[str, Any]]) -> frozenset[str]:
    """Names a description may reference: tool names, parameter names, string enum values, and
    ``EXTRA_IDENTIFIERS``."""
    names: set[str] = set(EXTRA_IDENTIFIERS)
    for tool in tools:
        names.add(str(tool.get("name", "")))
        names.update(_properties(tool).keys())
        names.update(_schema_strings(tool.get("inputSchema") or {}))
    return frozenset(names)


def unknown_tool_refs(text: str, allowed: frozenset[str]) -> list[str]:
    """Return the sorted distinct tool-name-format references in ``text`` not in ``allowed``.

    Candidates are backticked names (`` `name` `` or `` `name()` ``), call forms (``name(``), and
    bare words with a list_/get_/query_/validate_/find_/search_/text_search_ prefix.
    """
    found: set[str] = set()
    for pattern in (_BACKTICKED, _CALL_FORM, _BARE_VERB):
        found.update(m.group(1) for m in pattern.finditer(text))
    return sorted(found - allowed)
