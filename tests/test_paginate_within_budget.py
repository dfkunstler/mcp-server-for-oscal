"""Example tests for utils.paginate_within_budget (Requirement 12)."""

import json

import pytest

from mcp_server_for_oscal.tools.utils import (
    RESPONSE_BUDGET,
    TRUNCATION_MARK,
    budget_page,
    paginate,
    paginate_within_budget,
)


def _item(i: int, description: str = "short") -> dict:
    return {"value": f"v{i}", "description": description, "source-file": "m.xml", "line": i}


def _size(page: dict) -> int:
    return len(json.dumps(page))


class TestPaginateWithinBudget:
    def test_response_budget_is_20000(self):
        assert RESPONSE_BUDGET == 20_000

    def test_small_page_equals_paginate(self):
        items = [_item(i) for i in range(30)]
        assert paginate_within_budget(items, 5, 10) == paginate(items, 5, 10)

    @pytest.mark.parametrize(("offset", "limit"), [(-1, 10), (0, 0), (0, 101)])
    def test_invalid_arguments_raise_paginate_error(self, offset, limit):
        with pytest.raises(ValueError, match=r"offset|limit"):
            paginate_within_budget([_item(0)], offset, limit)

    def test_offset_at_or_past_total_returns_empty_page(self):
        items = [_item(i) for i in range(3)]
        for offset in (3, 50):
            page = paginate_within_budget(items, offset, 10, budget=10)
            assert page["items"] == []
            assert page["total"] == 3
            assert page["hasMore"] is False

    def test_extra_is_merged_without_overriding_envelope(self):
        page = paginate_within_budget(
            [], 0, 10, extra={"suggested-targets": ["a", "b"], "total": 99}
        )
        assert page["suggested-targets"] == ["a", "b"]
        assert page["total"] == 0

    def test_oversized_page_returns_longest_fitting_prefix(self):
        items = [_item(i, "x" * 3_000) for i in range(20)]
        page = paginate_within_budget(items, 2, 10, budget=10_000)
        k = len(page["items"])
        assert 1 <= k < 10
        assert page["items"] == items[2 : 2 + k]
        assert page["limit"] == k
        assert page["total"] == 20
        assert page["hasMore"] is True
        assert _size(page) <= 10_000
        # One more item would not fit.
        bigger = dict(page, items=items[2 : 3 + k], limit=k + 1)
        assert _size(bigger) > 10_000

    def test_has_more_false_when_prefix_reaches_end(self):
        items = [_item(i, "x" * 3_000) for i in range(5)]
        page = paginate_within_budget(items, 4, 10, budget=5_000)
        assert page["items"] == items[4:]
        assert page["hasMore"] is False

    def test_single_oversized_item_is_truncated(self):
        description = "é€" * 20_000  # non-ASCII: 6 chars per code point when escaped
        items = [_item(0, description), _item(1)]
        page = paginate_within_budget(items, 0, 10)
        assert _size(page) <= RESPONSE_BUDGET
        assert page["limit"] == 1
        assert page["hasMore"] is True
        (item,) = page["items"]
        assert item["truncated"] is True
        assert item["source-file"] == "m.xml"
        assert item["line"] == 0
        assert item["value"] == "v0"
        assert item["description"].endswith(TRUNCATION_MARK)
        prefix = item["description"].removesuffix(TRUNCATION_MARK)
        assert description.startswith(prefix)
        assert len(prefix) > 0
        # The input item is not mutated.
        assert items[0]["description"] == description
        assert "truncated" not in items[0]

    def test_multiple_truncatable_fields_longest_first(self):
        item = {"message": "m" * 30_000, "test": "t" * 100, "source-file": "f", "line": 1}
        page = paginate_within_budget([item], 0, 10, truncatable=("message", "test"))
        (out,) = page["items"]
        assert _size(page) <= RESPONSE_BUDGET
        assert out["message"].endswith(TRUNCATION_MARK)
        assert out["test"] == "t" * 100

    def test_unfittable_item_raises(self):
        item = {"value": "x" * 5_000, "description": "d" * 5_000}
        with pytest.raises(RuntimeError, match="response budget"):
            paginate_within_budget([item], 0, 10, budget=2_000)


class TestBudgetPage:
    """Example tests for utils.budget_page (search-fetch-pairs Requirement 7.5)."""

    def test_page_that_fits_is_returned_whole(self):
        items = [_item(i) for i in range(5)]
        page = budget_page(items, 12, 3)
        assert page == {"items": items, "total": 12, "offset": 3, "limit": 5, "hasMore": True}

    def test_has_more_false_when_page_reaches_total(self):
        items = [_item(i) for i in range(3)]
        page = budget_page(items, 8, 5)
        assert page["limit"] == 3
        assert page["hasMore"] is False

    def test_empty_list(self):
        page = budget_page([], 0, 0)
        assert page == {"items": [], "total": 0, "offset": 0, "limit": 0, "hasMore": False}
        # Past the end of a non-empty result: nothing more to fetch from here.
        assert budget_page([], 4, 10)["hasMore"] is False

    def test_oversized_page_returns_longest_fitting_prefix(self):
        items = [_item(i, "x" * 3_000) for i in range(10)]
        page = budget_page(items, 40, 20, budget=10_000)
        k = len(page["items"])
        assert 1 <= k < 10
        assert page["items"] == items[:k]
        assert page["limit"] == k
        assert page["offset"] == 20
        assert page["hasMore"] is True
        assert _size(page) <= 10_000
        bigger = dict(page, items=items[: k + 1], limit=k + 1)
        assert _size(bigger) > 10_000

    def test_single_over_budget_item_is_truncated(self):
        description = "é€" * 20_000
        items = [_item(0, description), _item(1)]
        page = budget_page(items, 2, 0)
        assert _size(page) <= RESPONSE_BUDGET
        assert page["limit"] == 1
        assert page["hasMore"] is True
        (item,) = page["items"]
        assert item["truncated"] is True
        assert item["value"] == "v0"
        assert item["source-file"] == "m.xml"
        assert item["description"].endswith(TRUNCATION_MARK)
        prefix = item["description"].removesuffix(TRUNCATION_MARK)
        assert prefix
        assert description.startswith(prefix)
        assert "truncated" not in items[0]
        assert items[0]["description"] == description

    def test_extra_keys_are_merged(self):
        page = budget_page([_item(0)], 1, 0, extra={"suggested-targets": ["a/@b"]})
        assert page["suggested-targets"] == ["a/@b"]

    def test_envelope_keys_win_over_colliding_extra(self):
        extra = {
            "items": ["bogus"],
            "total": 99,
            "offset": 99,
            "limit": 99,
            "hasMore": True,
            "note": "kept",
        }
        items = [_item(0)]
        page = budget_page(items, 1, 0, extra=extra)
        assert page == {
            "items": items,
            "total": 1,
            "offset": 0,
            "limit": 1,
            "hasMore": False,
            "note": "kept",
        }

    def test_extra_counts_toward_budget(self):
        items = [_item(i) for i in range(10)]
        extra = {"padding": "p" * 500}
        budget = _size(budget_page(items[:3], 10, 0, extra=extra))
        page = budget_page(items, 10, 0, budget=budget, extra=extra)
        assert page["limit"] == 3
        assert page["padding"] == "p" * 500
        assert _size(page) <= budget

    def test_unfittable_item_raises(self):
        item = {"value": "x" * 5_000, "description": "d" * 5_000}
        with pytest.raises(RuntimeError, match="response budget"):
            budget_page([item], 1, 0, budget=2_000)
