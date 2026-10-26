"""Property test for stop-word handling in ``tools/fts_query.normalize_query``."""

# Feature: search-fetch-pairs, Property 4: Stop words are dropped only when another word remains

from hypothesis import given, settings
from hypothesis import strategies as st

from mcp_server_for_oscal.tools.fts_query import MAX_SEARCH_TERMS, STOP_WORDS, normalize_query

# Separators: characters outside every ``_TERM`` match. Joiners (- . / : ' U+2019) are
# excluded so that adjacent words never fuse into one term.
_SEPARATORS = st.text(alphabet=' \t\n"*()^{}+,?', min_size=1, max_size=3)


def _random_case(word: str, data: st.DataObject) -> str:
    """Return ``word`` with each letter's case chosen by Hypothesis."""
    flips = data.draw(st.lists(st.booleans(), min_size=len(word), max_size=len(word)))
    return "".join(c.upper() if f else c for c, f in zip(word, flips, strict=True))


_stop_words = st.sampled_from(sorted(STOP_WORDS))
# ASCII-only so casefold() is plain lowercasing and no word casefolds into a stop word.
_content_words = st.text(
    alphabet="abcdefghijklmnopqrstuvwxyz0123456789", min_size=1, max_size=8
).filter(lambda w: w not in STOP_WORDS)
_words = st.one_of(_stop_words, _content_words)


def _distinct(words: list[str]) -> list[str]:
    """Return ``words`` deduplicated by casefold, first spelling kept."""
    seen: set[str] = set()
    out: list[str] = []
    for w in words:
        if w.casefold() not in seen:
            seen.add(w.casefold())
            out.append(w)
    return out


class TestStopWordProperty:
    """Property 4: Stop words are dropped only when another word remains.

    **Validates: Requirements 8.4**
    """

    @settings(max_examples=100)
    @given(base=st.lists(_words, min_size=1, max_size=30), data=st.data())
    def test_stop_words_dropped_only_when_content_remains(self, base, data):
        words = [_random_case(w, data) for w in base]
        seps = data.draw(st.lists(_SEPARATORS, min_size=len(words) + 1, max_size=len(words) + 1))
        text = seps[0] + "".join(w + s for w, s in zip(words, seps[1:], strict=True))

        distinct = _distinct(words)
        content = [w for w in distinct if w.casefold() not in STOP_WORDS]
        expected = content or distinct

        assert normalize_query(text) == tuple(expected[:MAX_SEARCH_TERMS])


class TestStopWordExamples:
    """Edge cases for Property 4."""

    def test_all_stop_words_are_kept(self):
        assert normalize_query("What is the") == ("What", "is", "the")

    def test_cap_applies_after_stop_word_removal(self):
        stops = " ".join(sorted(STOP_WORDS)[:MAX_SEARCH_TERMS])
        content = [f"term{i}" for i in range(MAX_SEARCH_TERMS)]
        assert normalize_query(f"{stops} {' '.join(content)}") == tuple(content)

    def test_case_variant_stop_word_is_dropped(self):
        assert normalize_query("THE Control") == ("Control",)
