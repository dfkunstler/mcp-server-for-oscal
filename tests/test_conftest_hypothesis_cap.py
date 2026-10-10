"""Guard tests for the Windows CI Hypothesis example cap in ``tests/conftest.py``."""

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from tests.conftest import MAX_EXAMPLES_ENV, cap_hypothesis_examples, hypothesis_example_cap


def _make_test(max_examples: int):
    calls: list[int] = []

    @settings(max_examples=max_examples, database=None)
    @given(st.integers())
    def prop(x: int) -> None:
        calls.append(x)

    return prop, calls


def test_cap_lowers_explicit_max_examples_and_is_honored() -> None:
    """Explicit @settings(max_examples=100) is capped, and Hypothesis runs at most the cap."""
    prop, calls = _make_test(100)

    assert cap_hypothesis_examples(prop, 5) is True
    prop()

    assert 1 <= len(calls) <= 5


def test_cap_leaves_smaller_counts_alone() -> None:
    prop, _ = _make_test(3)

    assert cap_hypothesis_examples(prop, 5) is False
    assert prop._hypothesis_internal_use_settings.max_examples == 3


def test_cap_ignores_non_hypothesis_functions() -> None:
    def plain() -> None: ...

    assert cap_hypothesis_examples(plain, 5) is False
    assert cap_hypothesis_examples(None, 5) is False


@pytest.mark.parametrize(("raw", "expected"), [("", None), ("  ", None), ("10", 10), (" 7 ", 7)])
def test_example_cap_parsing(
    monkeypatch: pytest.MonkeyPatch, raw: str, expected: int | None
) -> None:
    monkeypatch.setenv(MAX_EXAMPLES_ENV, raw)
    assert hypothesis_example_cap() == expected


@pytest.mark.parametrize("raw", ["0", "-3", "ten"])
def test_example_cap_rejects_bad_values(monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
    monkeypatch.setenv(MAX_EXAMPLES_ENV, raw)
    with pytest.raises(pytest.UsageError, match=MAX_EXAMPLES_ENV):
        hypothesis_example_cap()
