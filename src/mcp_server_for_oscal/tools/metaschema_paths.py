"""Pure string algorithms for Metaschema constraint targets (no I/O, no index dependency).

``normalize_target`` turns a Constraint_Target (a Metapath expression) into structural
Normalized_Path alternatives. It does not evaluate Metapath; it splits on top-level
``|`` and ``/``, removes predicates (reporting them as Conditions), strips leading self
steps, and extracts a single-literal Namespace_Condition. See the design section
"Normalized_Path and Condition algorithm".

``validate_selector``, ``parse_selector``, and ``compose_selector`` handle the
Target_Selector a caller passes to a Metaschema_Tool (design "Target_Selector grammar").
"""

from __future__ import annotations

import re
from dataclasses import dataclass

__all__ = [
    "MAX_SELECTOR_LENGTH",
    "Condition",
    "PathAlternative",
    "compose_selector",
    "normalize_target",
    "parse_selector",
    "validate_selector",
]

MAX_SELECTOR_LENGTH = 512
_SELECTOR_SHOWN_CHARS = 80
_SELECTOR_FORBIDDEN = frozenset("[]()|")


@dataclass(frozen=True, slots=True)
class Condition:
    """A predicate removed from a step while computing a Normalized_Path."""

    step: str  # step text without predicates, e.g. "prop", "(.)", "."
    predicate: str  # predicate text verbatim (after removing a Namespace_Condition conjunct)


@dataclass(frozen=True, slots=True)
class PathAlternative:
    """One top-level ``|`` alternative of a Constraint_Target."""

    raw: str  # alternative text as written (stripped)
    path: str | None  # Normalized_Path; None if unparseable
    conditions: tuple[Condition, ...]
    namespace: str | None  # from the last step with a Namespace_Condition
    cross_document: bool  # starts with doc(...)


# Depth marker for characters inside a string literal (including the quote characters).
_IN_QUOTE = -1
_OPENERS = {"(": ")", "[": "]"}
_CLOSERS = frozenset(_OPENERS.values())

_NAMESPACE_CALL = re.compile(
    r"(?:has-oscal-namespace|has-namespace)\s*\(\s*(?:'([^']*)'|\"([^\"]*)\")\s*\)",
    re.DOTALL,
)
_DOC_PREFIX = re.compile(r"doc\s*\(")
_SELF_STEPS = frozenset({".", "(.)"})

# Characters that make an adjacent "and"/"or" part of a name, variable, or attribute.
_NAME_CHARS = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_.")
_NOT_BEFORE_KEYWORD = _NAME_CHARS | frozenset("@$:/")
_NOT_AFTER_KEYWORD = _NAME_CHARS | frozenset(":")


def _scan(text: str) -> list[int] | None:
    """Return the bracket depth before each character, or None if unbalanced.

    Characters inside a quoted string (and the quotes themselves) get ``_IN_QUOTE``.
    XPath escapes a quote by doubling it, which this toggle handles naturally.
    Parentheses and brackets must nest correctly (``[)`` is unbalanced).
    """
    depths: list[int] = []
    stack: list[str] = []
    quote: str | None = None
    for ch in text:
        if quote is not None:
            depths.append(_IN_QUOTE)
            if ch == quote:
                quote = None
            continue
        if ch in {"'", '"'}:
            depths.append(_IN_QUOTE)
            quote = ch
            continue
        depths.append(len(stack))
        if ch in _OPENERS:
            stack.append(_OPENERS[ch])
        elif ch in _CLOSERS and (not stack or stack.pop() != ch):
            return None
    if quote is not None or stack:
        return None
    return depths


def _split_top(text: str, depths: list[int], sep: str) -> list[str]:
    """Split ``text`` on a single-character separator that occurs at top level."""
    parts: list[str] = []
    start = 0
    for i, ch in enumerate(text):
        if ch == sep and depths[i] == 0:
            parts.append(text[start:i])
            start = i + 1
    parts.append(text[start:])
    return parts


def _keyword_positions(text: str, depths: list[int], word: str) -> list[int]:
    """Indexes where ``word`` occurs as a top-level, word-bounded keyword."""
    positions: list[int] = []
    n, w = len(text), len(word)
    for i in range(n - w + 1):
        if depths[i] != 0 or text[i : i + w] != word:
            continue
        if any(depths[j] != 0 for j in range(i, i + w)):
            continue
        if i > 0 and text[i - 1] in _NOT_BEFORE_KEYWORD:
            continue
        if i + w < n and text[i + w] in _NOT_AFTER_KEYWORD:
            continue
        positions.append(i)
    return positions


def _tokenize_steps(text: str, depths: list[int]) -> list[tuple[str, str]] | None:
    """Split an alternative into ``(separator, step)`` tokens on top-level ``/``.

    The separator is ``""`` for a relative first step, ``"/"`` or ``"//"`` otherwise.
    Returns None for an empty step (trailing ``/``, ``///``, or ``a/ /b``).
    """
    tokens: list[tuple[str, str]] = []
    sep = ""
    start = 0
    i = 0
    n = len(text)
    while i < n:
        if text[i] != "/" or depths[i] != 0:
            i += 1
            continue
        segment = text[start:i].strip()
        if segment:
            tokens.append((sep, segment))
        elif tokens or sep:
            return None  # empty step between separators
        if i + 1 < n and text[i + 1] == "/":
            sep, i = "//", i + 2
        else:
            sep, i = "/", i + 1
        if i < n and text[i] == "/":
            return None  # "///"
        start = i
    segment = text[start:].strip()
    if segment:
        tokens.append((sep, segment))
    elif tokens or sep:
        return None  # trailing separator
    return tokens


def _parse_step(step: str) -> tuple[str, list[str]] | None:
    """Split a step into its base text and its top-level ``[...]`` predicates."""
    depths = _scan(step)
    if depths is None:
        return None
    first = next((i for i, ch in enumerate(step) if ch == "[" and depths[i] == 0), len(step))
    base = step[:first].strip()
    if not base:
        return None
    predicates: list[str] = []
    i = first
    n = len(step)
    while i < n:
        if step[i].isspace():
            i += 1
            continue
        if step[i] != "[" or depths[i] != 0:
            return None  # text after a predicate, e.g. "a[x]b"
        # The matching "]" is the first closer back at depth 1 (nesting is checked by _scan).
        close = next(j for j in range(i + 1, n) if step[j] == "]" and depths[j] == 1)
        content = step[i + 1 : close].strip()
        if not content:
            return None  # empty predicate "[]"
        predicates.append(content)
        i = close + 1
    return base, predicates


def _is_doc_call(base: str) -> bool:
    """True if ``base`` is exactly one balanced ``doc(...)`` call."""
    match = _DOC_PREFIX.match(base)
    if match is None or not base.endswith(")"):
        return False
    depths = _scan(base)
    if depths is None:
        return False
    open_at = match.end() - 1
    # The call's "(" must close at the last character, not earlier ("doc(a)(b)").
    # Characters inside string literals carry _IN_QUOTE, which is also "not top level".
    return all(depths[j] != 0 for j in range(open_at + 1, len(base) - 1))


def _namespace_split(predicate: str) -> tuple[str, list[str]] | None:
    """If the predicate has a Namespace_Condition, return (uri, remaining conjuncts).

    Conjuncts are top-level ``and`` operands. A predicate with a top-level ``or`` has no
    Namespace_Condition. If several conjuncts qualify, the last one supplies the URI.
    """
    depths = _scan(predicate)
    if depths is None or _keyword_positions(predicate, depths, "or"):
        return None
    conjuncts: list[str] = []
    start = 0
    for pos in _keyword_positions(predicate, depths, "and"):
        conjuncts.append(predicate[start:pos].strip())
        start = pos + len("and")
    conjuncts.append(predicate[start:].strip())
    chosen: int | None = None
    uri = ""
    for idx, conjunct in enumerate(conjuncts):
        match = _NAMESPACE_CALL.fullmatch(conjunct)
        if match is not None:
            chosen = idx
            uri = match.group(1) if match.group(1) is not None else match.group(2)
    if chosen is None:
        return None
    return uri, [c for idx, c in enumerate(conjuncts) if idx != chosen]


def _unparseable(raw: str) -> PathAlternative:
    return PathAlternative(
        raw=raw,
        path=None,
        conditions=(),
        namespace=None,
        cross_document=_DOC_PREFIX.match(raw) is not None,
    )


def _normalize_alternative(raw: str, *, extract_namespace: bool = True) -> PathAlternative:
    depths = _scan(raw)
    if depths is None:
        return _unparseable(raw)
    tokens = _tokenize_steps(raw, depths)
    if tokens is None or not tokens:
        return _unparseable(raw)

    steps: list[tuple[str, str, list[str]]] = []  # (separator, base, predicates)
    for sep, text in tokens:
        parsed = _parse_step(text)
        if parsed is None:
            return _unparseable(raw)
        steps.append((sep, parsed[0], parsed[1]))

    cross_document = False
    leading = 0  # number of leading steps (doc call, self steps) dropped from the path
    if steps[0][0] == "" and _is_doc_call(steps[0][1]):
        cross_document = True
        leading = 1
    while (
        leading < len(steps)
        and steps[leading][0] == ("" if leading == 0 else "/")
        and steps[leading][1].replace(" ", "") in _SELF_STEPS
    ):
        leading += 1

    # Namespace: the last Namespace_Condition (by step, then predicate) in the alternative.
    chosen: tuple[int, int] | None = None
    namespace: str | None = None
    remaining: list[str] = []
    for s_idx, (_, _, predicates) in enumerate(steps):
        for p_idx, predicate in enumerate(predicates):
            split = _namespace_split(predicate) if extract_namespace else None
            if split is not None:
                chosen = (s_idx, p_idx)
                namespace, remaining = split

    conditions: list[Condition] = []
    for s_idx, (_, base, predicates) in enumerate(steps):
        for p_idx, predicate in enumerate(predicates):
            if (s_idx, p_idx) == chosen:
                if remaining:
                    conditions.append(Condition(step=base, predicate=" and ".join(remaining)))
            else:
                conditions.append(Condition(step=base, predicate=predicate))

    kept = steps[leading:]
    parts: list[str] = []
    for idx, (sep, base, _) in enumerate(kept):
        # A single "/" after a dropped leading step disappears; "//" is kept.
        drop_sep = idx == 0 and leading and sep == "/"
        parts.append(base if drop_sep else sep + base)
    return PathAlternative(
        raw=raw,
        path="".join(parts),
        conditions=tuple(conditions),
        namespace=namespace,
        cross_document=cross_document,
    )


def normalize_target(
    target: str | None, *, extract_namespace: bool = True
) -> tuple[PathAlternative, ...]:
    """Normalize a Constraint_Target into its Normalized_Path alternatives.

    ``None`` (no ``target`` attribute) is treated as ``.``. Never raises: an alternative
    with unbalanced quotes, parentheses, or brackets, or with an empty step or predicate,
    gets ``path=None``. If the whole target is unbalanced, the result is one such
    alternative holding the stripped target text.

    With ``extract_namespace=False``, no Namespace_Condition is extracted: ``namespace`` is
    ``None`` and every predicate is a Condition verbatim. Callers use this to report
    Namespace_Conditions as Conditions when matched alternatives disagree on the
    namespace (Requirement 6.4).
    """
    text = "." if target is None else target
    depths = _scan(text)
    if depths is None:
        return (_unparseable(text.strip()),)
    return tuple(
        _normalize_alternative(alt.strip(), extract_namespace=extract_namespace)
        for alt in _split_top(text, depths, "|")
    )


def _selector_error(selector: str, rule: str) -> ValueError:
    shown = selector[:_SELECTOR_SHOWN_CHARS]
    if len(selector) > _SELECTOR_SHOWN_CHARS:
        shown += "..."
    return ValueError(f"Invalid target selector '{shown}': {rule}")


def validate_selector(selector: str) -> None:
    """Raise ValueError if ``selector`` is not a valid Target_Selector.

    The message names the selector (truncated to 80 characters) and the rule broken.
    Rules: not empty; at most ``MAX_SELECTOR_LENGTH`` characters; no whitespace; no
    leading or trailing ``/``; no ``///``; none of ``[ ] ( ) |`` (predicates and unions
    are never part of a selector).
    """
    if not selector:
        raise _selector_error(selector, "must not be empty")
    if len(selector) > MAX_SELECTOR_LENGTH:
        raise _selector_error(
            selector, f"must be at most {MAX_SELECTOR_LENGTH} characters (got {len(selector)})"
        )
    if any(ch.isspace() for ch in selector):
        raise _selector_error(selector, "must not contain whitespace")
    if selector.startswith("/"):
        raise _selector_error(selector, "must not start with '/'; begin with a definition name")
    if selector.endswith("/"):
        raise _selector_error(selector, "must not end with '/'")
    if "///" in selector:
        raise _selector_error(selector, "must not contain '///'; use '/' or '//'")
    forbidden = sorted({ch for ch in selector if ch in _SELECTOR_FORBIDDEN})
    if forbidden:
        chars = " ".join(forbidden)
        raise _selector_error(
            selector,
            f"must not contain predicates or unions (found {chars}); "
            "use a plain path such as 'defined-component/prop/@name'",
        )


def parse_selector(selector: str) -> tuple[str, str]:
    """Split a Target_Selector into ``(definition, remainder)``.

    Validates first (``ValueError`` per ``validate_selector``). The split is at the first
    ``/``. A descendant step keeps its ``//`` in the remainder; a single ``/`` is dropped:
    ``part//part/@name`` -> ``("part", "//part/@name")``,
    ``defined-component/@type`` -> ``("defined-component", "@type")``, ``x`` -> ``("x", "")``.
    """
    validate_selector(selector)
    definition, sep, rest = selector.partition("/")
    if not sep:
        return definition, ""
    if rest.startswith("/"):
        return definition, "/" + rest
    return definition, rest


def compose_selector(definition: str, path: str) -> str:
    """Inverse of ``parse_selector``: join a definition name and a remainder path."""
    if not path:
        return definition
    if path.startswith("//"):
        return definition + path
    return f"{definition}/{path}"
