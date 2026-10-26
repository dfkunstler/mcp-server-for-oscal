"""Example tests for tools/fts_query.py (Feature: search-fetch-pairs, task 4.1).

Requirements 8.1, 8.2, 8.3, 8.4, 8.8.
"""

import pytest

from mcp_server_for_oscal.tools import fts_query
from mcp_server_for_oscal.tools.fts_query import (
    MAX_SEARCH_TERMS,
    SEARCH_COLUMNS,
    STOP_WORDS,
    like_pattern,
    match_expressions,
    normalize_query,
    phrase,
)


class TestNormalizeQuery:
    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("back-matter", ("back-matter",)),
            ("ac-2", ("ac-2",)),
            ('"ac-2"', ("ac-2",)),
            ("ac-2.1", ("ac-2.1",)),
            ("urn:x", ("urn:x",)),
            ("control-implementation", ("control-implementation",)),
        ],
    )
    def test_joined_tokens_stay_one_term(self, text: str, expected: tuple[str, ...]) -> None:
        assert normalize_query(text) == expected

    def test_near_operator_is_split_into_words(self) -> None:
        assert normalize_query("NEAR(a b)") == ("NEAR", "b")

    def test_operators_and_unbalanced_quotes_are_separators(self) -> None:
        # "AND" is a stop word (casefold) and is dropped; quotes and parens separate.
        assert normalize_query('"profile resolution" AND (x') == ("profile", "resolution", "x")

    def test_fts_operator_characters_are_separators(self) -> None:
        assert normalize_query("x* ^y {z} w: q+r,s?") == ("x", "y", "z", "w", "q", "r", "s")

    def test_stop_words_dropped_when_other_word_remains(self) -> None:
        assert normalize_query("how does profile resolution work") == (
            "profile",
            "resolution",
            "work",
        )

    def test_all_stop_word_query_keeps_terms(self) -> None:
        assert normalize_query("What is the") == ("What", "is", "the")

    def test_dedupe_by_casefold_keeps_first_spelling(self) -> None:
        assert normalize_query("Control control CONTROL") == ("Control",)

    @pytest.mark.parametrize("text", ["", "   ", '"()*^"', "- . / :"])
    def test_no_terms(self, text: str) -> None:
        assert normalize_query(text) == ()

    def test_max_search_terms_cap(self) -> None:
        words = [f"w{i}" for i in range(MAX_SEARCH_TERMS + 5)]
        assert normalize_query(" ".join(words)) == tuple(words[:MAX_SEARCH_TERMS])

    def test_cap_applies_after_stop_word_removal(self) -> None:
        words = [f"w{i}" for i in range(MAX_SEARCH_TERMS)]
        text = " ".join(["the", *words, "extra"])
        assert normalize_query(text) == tuple(words)

    @pytest.mark.parametrize("bad", [None, 42, b"ac-2", ["ac-2"]])
    def test_non_str_raises_value_error(self, bad: object) -> None:
        with pytest.raises(ValueError, match="string"):
            normalize_query(bad)  # type: ignore[arg-type]


class TestPhraseAndExpressions:
    def test_phrase_quotes_and_doubles_quotes(self) -> None:
        assert phrase("ac-2") == '"ac-2"'
        assert phrase('a"b') == '"a""b"'

    def test_match_expressions(self) -> None:
        any_expr, all_expr = match_expressions(("ac-2", "AND"))
        assert any_expr == f'{SEARCH_COLUMNS} : "ac-2" OR {SEARCH_COLUMNS} : "AND"'
        assert all_expr == f'{SEARCH_COLUMNS} : "ac-2" AND {SEARCH_COLUMNS} : "AND"'

    def test_match_expressions_single_and_empty(self) -> None:
        assert match_expressions(["x"]) == (f'{SEARCH_COLUMNS} : "x"',) * 2
        assert match_expressions([]) == ("", "")


class TestLikePattern:
    @pytest.mark.parametrize(
        ("term", "expected"),
        [
            ("ac-2", "%ac-2%"),
            ("50%", "%50\\%%"),
            ("a_b", "%a\\_b%"),
            ("a\\b", "%a\\\\b%"),
            ("\\%_", "%\\\\\\%\\_%"),
        ],
    )
    def test_escaping(self, term: str, expected: str) -> None:
        assert like_pattern(term) == expected


def test_module_is_pure() -> None:
    assert "sqlite3" not in vars(fts_query)
    assert "how" in STOP_WORDS
    assert "near" not in STOP_WORDS
