"""Property tests for tools/fts_query.py normalization output (search-fetch-pairs task 4.2).

Feature: search-fetch-pairs, Property 3: Query normalization never yields FTS5 syntax
(pure part). The store-level part (``text_search`` / ``search_documentation`` returning an
empty page exactly when the terms are empty) is covered with the store tasks.
"""

import sqlite3
from collections.abc import Iterator

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from mcp_server_for_oscal.tools.fts_query import (
    _TERM,
    MAX_SEARCH_TERMS,
    match_expressions,
    normalize_query,
)

# Literal copy of the fts_index DDL in OscalStore._init_schema (tools/oscal_store.py).
# Copied rather than imported: the DDL is inline in a private method, and building a full
# OscalStore would tie this pure test to temp files and the store's other tables.
_FTS_DDL = """
    CREATE VIRTUAL TABLE fts_index USING fts5(
        entity_type,
        entity_id,
        title,
        description,
        model_type
    )
"""

_ROWS = [
    ("child_element", "c1", "AC-2 Account Management", "back-matter resources", "catalog"),
    ("document", "d1", "Profile resolution", "how does profile resolution work", "profile"),
    ("child_element", "c2", "NOT AND OR NEAR", 'quote " star * colon : caret ^', "catalog"),
]

# FTS5-hostile fragments from Requirement 8.2/8.3, mixed with arbitrary Unicode text.
_HOSTILE = st.sampled_from([
    '"', "*", ":", "(", ")", "^", "{", "}", "+", "-", ".", "/", "'", "\u2019", "\\", "%", "_",
    "AND", "OR", "NOT", "NEAR", "NEAR(", "and", "or", "not", "near",
    "back-matter", "ac-2", "ac-2.1", "control-implementation", "urn:x", "a/b", "it's",
    "title:", "description:", "{title}", "-ac", "ac-", "--", "::", "the", "how", " ", "\t",
])  # fmt: skip
_QUERIES = st.one_of(
    st.text(),
    st.lists(st.one_of(_HOSTILE, st.text(max_size=5)), max_size=25).map("".join),
    st.lists(_HOSTILE, max_size=25).map(" ".join),
)


@pytest.fixture(scope="module")
def fts_conn() -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(":memory:")
    conn.execute(_FTS_DDL)
    conn.executemany("INSERT INTO fts_index VALUES (?, ?, ?, ?, ?)", _ROWS)
    yield conn
    conn.close()


class TestNormalizationNeverYieldsFts5Syntax:
    """Feature: search-fetch-pairs, Property 3 (pure part).

    **Validates: Requirements 8.1, 8.2, 8.3, 8.8**
    """

    @settings(max_examples=300, deadline=None)
    @given(text=_QUERIES)
    def test_terms_match_term_pattern_and_expressions_run(
        self, fts_conn: sqlite3.Connection, text: str
    ) -> None:
        terms = normalize_query(text)

        assert len(terms) <= MAX_SEARCH_TERMS
        for term in terms:
            assert _TERM.fullmatch(term), term

        match_any, match_all = match_expressions(terms)
        if not terms:
            # Requirement 8.8: no terms means no MATCH; the store returns an empty page.
            assert (match_any, match_all) == ("", "")
            return

        for expr in (match_any, match_all):
            # Raises sqlite3.OperationalError if any caller text leaked in as FTS5 syntax.
            fts_conn.execute(
                "SELECT entity_id FROM fts_index WHERE fts_index MATCH ? ORDER BY rank",
                (expr,),
            ).fetchall()
