"""Query normalization for SQLite FTS5 searches.

Turns caller query text into Search_Terms and column-scoped, quoted FTS5 phrases so that
no caller character is read as FTS5 syntax. Pure module: no SQLite import.
"""

import re
from collections.abc import Sequence
from typing import Final

STOP_WORDS: Final[frozenset[str]] = frozenset({
    "a", "about", "an", "and", "are", "as", "at", "be", "but", "by", "can", "could",
    "did", "do", "does", "for", "from", "had", "has", "have", "how", "i", "if", "in",
    "into", "is", "it", "its", "me", "my", "of", "on", "or", "our", "should", "so",
    "than", "that", "the", "their", "them", "then", "there", "these", "they", "this",
    "those", "to", "was", "we", "were", "what", "when", "where", "which", "who", "whom",
    "why", "will", "with", "would", "you", "your",
})  # fmt: skip
MAX_SEARCH_TERMS: Final = 16
SEARCH_COLUMNS: Final = "{title description}"

# A Search_Term: word characters, optionally joined by - . / : ' or U+2019 (right single
# quotation mark) to more word characters.
_TERM: Final = re.compile(r"\w+(?:[-./:'\u2019]\w+)*")


def normalize_query(text: str) -> tuple[str, ...]:
    """Return the Search_Terms of ``text``, in order of first occurrence.

    Every character outside a ``_TERM`` match is a separator. Terms are deduplicated by
    ``casefold()`` (first spelling kept). Stop words are dropped when at least one other
    term remains. At most ``MAX_SEARCH_TERMS`` terms are returned.

    Raises:
        ValueError: If ``text`` is not a ``str``.
    """
    if not isinstance(text, str):
        msg = "Query text must be a string."
        raise ValueError(msg)  # noqa: TRY004 - callers handle ValueError for bad input

    terms: list[str] = []
    seen: set[str] = set()
    for match in _TERM.finditer(text):
        term = match.group(0)
        key = term.casefold()
        if key not in seen:
            seen.add(key)
            terms.append(term)

    content = [t for t in terms if t.casefold() not in STOP_WORDS]
    if content:
        terms = content
    return tuple(terms[:MAX_SEARCH_TERMS])


def phrase(term: str) -> str:
    """Return ``term`` as an FTS5 string literal, with embedded double quotes doubled."""
    return '"' + term.replace('"', '""') + '"'


def match_expressions(terms: Sequence[str]) -> tuple[str, str]:
    """Return the ``(match_any, match_all)`` FTS5 expressions for ``terms``.

    Each term becomes ``{title description} : "<term>"``; match-any joins them with
    ``OR`` and match-all with ``AND``. Both are empty strings when ``terms`` is empty.
    """
    scoped = [f"{SEARCH_COLUMNS} : {phrase(t)}" for t in terms]
    return " OR ".join(scoped), " AND ".join(scoped)


def like_pattern(term: str) -> str:
    """Return a ``LIKE`` pattern ``%term%`` with ``\\``, ``%``, and ``_`` escaped.

    Use with ``ESCAPE '\\'``.
    """
    escaped = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"
