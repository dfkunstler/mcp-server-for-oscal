"""
Tests for the utils module.
"""

import json
import os
import re
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from hypothesis import HealthCheck, event, given, settings
from hypothesis import strategies as st
from oscal_bindings import models

from mcp_server_for_oscal.tools import utils as utils_module
from mcp_server_for_oscal.tools.utils import (
    MAX_LINE_COUNT,
    MAX_PAGE_LIMIT,
    MAX_SHOWN_CHARS,
    MODEL_MAP,
    RESPONSE_BUDGET,
    TRUNCATION_MARK,
    OSCALModelType,
    check_line_count,
    check_line_range,
    get_bundled_oscal_version,
    lines_within_budget,
    load_oscal_json_schema,
    local_source_path,
    oscal_version_from_schema,
    paginate_within_budget,
    redact_storage_locations,
    schema_names,
    shown,
    try_notify_client_error,
)


class TestOSCALModelType:
    """Test cases for the OSCALModelType enum."""

    def test_oscal_model_type_is_str_enum(self):
        """Test that OSCALModelType is a string enum."""
        # Test that enum values are strings
        for model_type in OSCALModelType:
            assert isinstance(model_type.value, str)
            assert isinstance(model_type, str)

    def test_oscal_model_type_values(self):
        """Test that all expected OSCAL model types are defined."""
        expected_values = {
            "catalog",
            "profile",
            "component-definition",
            "system-security-plan",
            "assessment-plan",
            "assessment-results",
            "plan-of-action-and-milestones",
            "mapping-collection",
        }

        actual_values = {model_type.value for model_type in OSCALModelType}
        assert actual_values == expected_values

    def test_oscal_model_type_catalog(self):
        """Test the CATALOG model type."""
        assert OSCALModelType.CATALOG == "catalog"
        assert OSCALModelType.CATALOG.value == "catalog"

    def test_oscal_model_type_profile(self):
        """Test the PROFILE model type."""
        assert OSCALModelType.PROFILE == "profile"
        assert OSCALModelType.PROFILE.value == "profile"

    def test_oscal_model_type_component_definition(self):
        """Test the COMPONENT_DEFINITION model type."""
        assert OSCALModelType.COMPONENT_DEFINITION == "component-definition"
        assert OSCALModelType.COMPONENT_DEFINITION.value == "component-definition"

    def test_oscal_model_type_system_security_plan(self):
        """Test the SYSTEM_SECURITY_PLAN model type."""
        assert OSCALModelType.SYSTEM_SECURITY_PLAN == "system-security-plan"
        assert OSCALModelType.SYSTEM_SECURITY_PLAN.value == "system-security-plan"

    def test_oscal_model_type_assessment_plan(self):
        """Test the ASSESSMENT_PLAN model type."""
        assert OSCALModelType.ASSESSMENT_PLAN == "assessment-plan"
        assert OSCALModelType.ASSESSMENT_PLAN.value == "assessment-plan"

    def test_oscal_model_type_assessment_results(self):
        """Test the ASSESSMENT_RESULTS model type."""
        assert OSCALModelType.ASSESSMENT_RESULTS == "assessment-results"
        assert OSCALModelType.ASSESSMENT_RESULTS.value == "assessment-results"

    def test_oscal_model_type_plan_of_action_and_milestones(self):
        """Test the PLAN_OF_ACTION_AND_MILESTONES model type."""
        assert OSCALModelType.PLAN_OF_ACTION_AND_MILESTONES == "plan-of-action-and-milestones"
        assert OSCALModelType.PLAN_OF_ACTION_AND_MILESTONES.value == "plan-of-action-and-milestones"

    def test_oscal_model_type_mapping(self):
        """Test the MAPPING model type."""
        assert OSCALModelType.MAPPING == "mapping-collection"
        assert OSCALModelType.MAPPING.value == "mapping-collection"

    def test_oscal_model_type_iteration(self):
        """Test that we can iterate over all model types."""
        model_types = list(OSCALModelType)
        assert len(model_types) == 8  # Update if more model types are added

        # Verify all expected types are present
        expected_types = [
            OSCALModelType.CATALOG,
            OSCALModelType.PROFILE,
            OSCALModelType.COMPONENT_DEFINITION,
            OSCALModelType.SYSTEM_SECURITY_PLAN,
            OSCALModelType.ASSESSMENT_PLAN,
            OSCALModelType.ASSESSMENT_RESULTS,
            OSCALModelType.PLAN_OF_ACTION_AND_MILESTONES,
            OSCALModelType.MAPPING,
        ]

        for expected_type in expected_types:
            assert expected_type in model_types

    def test_oscal_model_type_membership(self):
        """Test membership testing with the enum."""
        # Test valid values
        assert "catalog" in OSCALModelType.__members__.values()
        assert "profile" in OSCALModelType.__members__.values()
        assert "component-definition" in OSCALModelType.__members__.values()

        # Test invalid values
        assert "invalid-model" not in OSCALModelType.__members__.values()
        assert "unknown" not in OSCALModelType.__members__.values()

    def test_oscal_model_type_string_comparison(self):
        """Test that enum values can be compared with strings."""
        assert OSCALModelType.CATALOG == "catalog"
        assert OSCALModelType.PROFILE == "profile"
        assert OSCALModelType.COMPONENT_DEFINITION == "component-definition"

        # Test inequality
        assert OSCALModelType.CATALOG != "profile"
        assert OSCALModelType.CATALOG != "invalid"

    def test_oscal_model_type_case_sensitivity(self):
        """Test that enum values are case sensitive."""
        assert OSCALModelType.CATALOG != "CATALOG"
        assert OSCALModelType.CATALOG != "Catalog"
        assert OSCALModelType.PROFILE != "PROFILE"

    def test_oscal_model_type_hyphenation(self):
        """Test that multi-word model types use hyphens."""
        multi_word_types = [
            OSCALModelType.COMPONENT_DEFINITION,
            OSCALModelType.SYSTEM_SECURITY_PLAN,
            OSCALModelType.ASSESSMENT_PLAN,
            OSCALModelType.ASSESSMENT_RESULTS,
            OSCALModelType.PLAN_OF_ACTION_AND_MILESTONES,
            OSCALModelType.MAPPING,
        ]

        for model_type in multi_word_types:
            assert "-" in model_type.value, f"Multi-word type {model_type} should contain hyphens"

    def test_oscal_model_type_no_spaces(self):
        """Test that no model type values contain spaces."""
        for model_type in OSCALModelType:
            assert " " not in model_type.value, f"Model type {model_type} should not contain spaces"

    def test_oscal_model_type_lowercase(self):
        """Test that all model type values are lowercase."""
        for model_type in OSCALModelType:
            assert model_type.value.islower(), f"Model type {model_type} should be lowercase"

    def test_oscal_model_type_unique_values(self):
        """Test that all model type values are unique."""
        values = [model_type.value for model_type in OSCALModelType]
        unique_values = set(values)
        assert len(values) == len(unique_values), "All model type values should be unique"

    def test_oscal_model_type_repr(self):
        """Test the string representation of enum values."""
        # Test that the enum can be converted to string
        catalog_str = str(OSCALModelType.CATALOG)
        assert catalog_str == "catalog"

        profile_str = str(OSCALModelType.PROFILE)
        assert profile_str == "profile"

    def test_oscal_model_type_in_collections(self):
        """Test that enum values work properly in collections."""
        # Test in list
        model_list = [OSCALModelType.CATALOG, OSCALModelType.PROFILE]
        assert OSCALModelType.CATALOG in model_list
        assert OSCALModelType.COMPONENT_DEFINITION not in model_list

        # Test in set
        model_set = {OSCALModelType.CATALOG, OSCALModelType.PROFILE}
        assert OSCALModelType.CATALOG in model_set
        assert OSCALModelType.COMPONENT_DEFINITION not in model_set

        # Test in dict keys
        model_dict = {OSCALModelType.CATALOG: "catalog_data"}
        assert OSCALModelType.CATALOG in model_dict
        assert model_dict[OSCALModelType.CATALOG] == "catalog_data"

    def test_oscal_model_type_sorting(self):
        """Test that enum values can be sorted."""
        model_types = list(OSCALModelType)
        sorted_types = sorted(model_types)

        # Should be sorted alphabetically by value
        expected_order = sorted([mt.value for mt in model_types])
        actual_order = [mt.value for mt in sorted_types]

        assert actual_order == expected_order


class TestModelMap:
    """MODEL_MAP must map every OSCALModelType to its oscal_bindings model class."""

    def test_keys_match_model_types(self):
        assert set(MODEL_MAP) == set(OSCALModelType)

    @pytest.mark.parametrize(
        ("model_type", "expected_cls"),
        [
            (OSCALModelType.CATALOG, models.Catalog),
            (OSCALModelType.PROFILE, models.Profile),
            (OSCALModelType.COMPONENT_DEFINITION, models.ComponentDefinition),
            (OSCALModelType.SYSTEM_SECURITY_PLAN, models.SystemSecurityPlan),
            (OSCALModelType.ASSESSMENT_PLAN, models.AssessmentPlan),
            (OSCALModelType.ASSESSMENT_RESULTS, models.AssessmentResults),
            (OSCALModelType.PLAN_OF_ACTION_AND_MILESTONES, models.PlanOfActionAndMilestones),
            (OSCALModelType.MAPPING, models.MappingCollection),
        ],
    )
    def test_value_is_bindings_class(self, model_type, expected_cls):
        assert MODEL_MAP[model_type] is expected_cls


class TestBundledOscalVersion:
    """The reported OSCAL version must come from, and agree with, the bundled schemas."""

    def test_parses_version_from_schema_id(self):
        schema = {"$id": "http://csrc.nist.gov/ns/oscal/9.8.7/oscal-catalog-schema.json"}
        assert oscal_version_from_schema(schema) == "9.8.7"

    def test_parses_version_from_complete_schema_id(self):
        schema = {"$id": "http://csrc.nist.gov/ns/oscal/1.0/9.8.7/oscal-complete-schema.json"}
        assert oscal_version_from_schema(schema) == "9.8.7"

    def test_rejects_schema_id_without_version(self):
        with pytest.raises(ValueError):
            oscal_version_from_schema({"$id": "http://example.com/schema.json"})

    def test_all_bundled_schemas_share_one_version(self):
        versions = {
            name: oscal_version_from_schema(load_oscal_json_schema(name)) for name in schema_names
        }
        assert set(versions.values()) == {get_bundled_oscal_version()}, versions

    def test_matches_update_script_release_version(self):
        script = Path(__file__).parent.parent / "bin" / "update-oscal-schemas.sh"
        match = re.search(r'^CURRENT_RELEASE_VERSION="([^"]+)"', script.read_text(), re.MULTILINE)
        assert match, "CURRENT_RELEASE_VERSION not found in update script"
        assert get_bundled_oscal_version() == match.group(1)

    def test_matches_oscal_bindings_schema_version(self):
        """oscal-bindings must be generated from the same OSCAL release as the bundled schemas."""
        import oscal_bindings

        assert oscal_bindings.__oscal_schema_version__ == get_bundled_oscal_version()

    def test_about_tool_reports_bundled_version(self):
        from mcp_server_for_oscal import main

        if main.mcp._tool_manager.get_tool("about") is None:
            main._setup_tools()
        about = main.mcp._tool_manager.get_tool("about")
        result = about.fn()
        assert result["oscal-version"] == get_bundled_oscal_version()
        # The about output is unchanged by the bindings switch: no bindings-version field
        assert set(result) == {"version", "keywords", "oscal-version", "user-documents-loaded"}
        assert not any("bindings" in key.lower() for key in result)


# --- Property 10: paginate_within_budget -------------------------------------

_TRUNCATABLE = ("description", "message")
_MIN_BUDGET = 2_000

# Mix ASCII, Latin-1, CJK, an astral emoji (12 chars under ensure_ascii), and
# characters JSON escapes (quote, backslash, newline, control).
_ALPHABET = st.sampled_from(["a", "Z", " ", "é", "ß", "漢", "😀", '"', "\\", "\n", "\x01"])


@st.composite
def _long_text(draw: st.DrawFn) -> str:
    """Draw a string of 0-30,000 characters built by repeating a short motif."""
    motif = draw(st.text(alphabet=_ALPHABET, min_size=1, max_size=6))
    # Small, mid-size (several per page), and oversized (truncation) values.
    length = draw(st.integers(0, 200) | st.integers(200, 3_000) | st.integers(0, 30_000))
    return (motif * (length // len(motif) + 1))[:length]


@st.composite
def _budget_item(draw: st.DrawFn) -> dict:
    """Draw an item whose non-truncatable fields stay small enough for any budget >= 2,000."""
    item: dict = {
        "value": draw(st.text(alphabet=_ALPHABET, max_size=40)),
        "source-file": "oscal_metaschema/src/metaschema/oscal_catalog_metaschema.xml",
        "line": draw(st.integers(1, 100_000)),
        "description": draw(_long_text()),
    }
    if draw(st.booleans()):
        item["message"] = draw(_long_text())
    return item


def _envelope(window: list[dict], offset: int, total: int, extra: dict) -> dict:
    """Page with ``limit`` reduced to ``len(window)``, as the pager builds it."""
    k = len(window)
    return {
        "items": window,
        "total": total,
        "offset": offset,
        "limit": k,
        "hasMore": offset + k < total,
        **extra,
    }


class TestPaginateWithinBudgetProperty:
    """Feature: metaschema-constraints, Property 10: Budget pager returns the longest
    fitting prefix.

    **Validates: Requirements 12.3, 12.4, 12.5**
    """

    @settings(
        max_examples=150,
        deadline=None,
        suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large],
    )
    @given(
        items=st.lists(_budget_item(), max_size=40),
        data=st.data(),
        limit=st.integers(1, MAX_PAGE_LIMIT),
        budget=st.integers(_MIN_BUDGET, RESPONSE_BUDGET) | st.integers(_MIN_BUDGET, 3_000),
        extra=st.sampled_from([{}, {"suggested-targets": ["a/@b", "c/@d"]}]),
    )
    def test_longest_fitting_prefix(self, items, data, limit, budget, extra):
        # Mostly in range; occasionally at or past the end (Requirement 12.7 shape).
        offset = data.draw(st.integers(0, max(len(items) - 1, 0)) | st.integers(0, 45))
        page = paginate_within_budget(
            items, offset, limit, budget=budget, truncatable=_TRUNCATABLE, extra=extra
        )
        total = len(items)
        window = items[offset : offset + limit]
        returned = page["items"]
        k = len(returned)

        # 12.3: serialized size within budget; envelope facts unchanged.
        assert len(json.dumps(page)) <= budget
        assert page["total"] == total
        assert page["offset"] == offset
        for key, value in extra.items():
            assert page[key] == value
        assert page["hasMore"] == (offset + k < total)

        if offset >= total:
            event("empty page")
            assert returned == []
            return

        assert k >= 1

        if returned[0].get("truncated") is True and "truncated" not in window[0]:
            # 12.5: one shortened item, because the untouched item alone did not fit.
            event("single truncated item")
            assert k == 1
            assert page["limit"] == 1
            assert len(json.dumps(_envelope(window[:1], offset, total, extra))) > budget
            got, original = returned[0], window[0]
            assert set(got) == set(original) | {"truncated"}
            shortened_any = False
            for key, value in original.items():
                if key not in _TRUNCATABLE:
                    assert got[key] == value, key
                elif got[key] != value:
                    shortened_any = True
                    assert got[key].endswith(TRUNCATION_MARK)
                    prefix = got[key][: -len(TRUNCATION_MARK)]
                    assert len(prefix) < len(value)
                    assert value.startswith(prefix)
            assert shortened_any
            return

        # 12.4: an unmodified prefix of the requested window.
        assert returned == window[:k]
        if k < len(window):
            event("reduced prefix")
            assert page["limit"] == k
            one_more = _envelope(window[: k + 1], offset, total, extra)
            assert len(json.dumps(one_more)) > budget
        else:
            event("full window")
            assert page["limit"] == limit


class TestShown:
    def test_short_text_unchanged(self):
        assert shown("abc") == "abc"
        assert shown("x" * MAX_SHOWN_CHARS) == "x" * MAX_SHOWN_CHARS

    def test_long_text_cut_and_marked(self):
        assert shown("x" * (MAX_SHOWN_CHARS + 1)) == "x" * MAX_SHOWN_CHARS + "..."


class TestCheckLineCount:
    """search-fetch-pairs Requirement 12.7: line_count is an int from 1 to 500."""

    def test_max_line_count_is_500(self):
        assert MAX_LINE_COUNT == 500

    @pytest.mark.parametrize("value", [1, 250, MAX_LINE_COUNT])
    def test_accepts_in_range(self, value):
        check_line_count(value)

    @pytest.mark.parametrize("value", [0, -1, MAX_LINE_COUNT + 1, True, False, 1.0, "5", None])
    def test_rejects_out_of_range_and_non_int(self, value):
        with pytest.raises(ValueError, match="line_count") as exc:
            check_line_count(value)
        assert f"Valid range: 1 to {MAX_LINE_COUNT}" in str(exc.value)

    def test_long_value_is_shortened_in_message(self):
        with pytest.raises(ValueError, match=r"\.\.\.") as exc:
            check_line_count("z" * 1_000)
        assert "z" * 1_000 not in str(exc.value)


class TestCheckLineRange:
    """search-fetch-pairs Requirement 12.7: messages name the parameter and range."""

    def test_accepts_valid_range(self):
        check_line_range(1, 1, 1)
        check_line_range(10, MAX_LINE_COUNT, 10, noun="document")

    @pytest.mark.parametrize("start", [0, 11, -3, True, "1", None])
    def test_bad_start_line_names_parameter_and_noun(self, start):
        with pytest.raises(ValueError, match="start_line") as exc:
            check_line_range(start, 10, 10, noun="document")
        assert "Valid range for this document: 1 to 10" in str(exc.value)

    def test_default_noun_is_file(self):
        with pytest.raises(ValueError, match="Valid range for this file: 1 to 3"):
            check_line_range(4, 1, 3)

    def test_line_count_checked_first(self):
        with pytest.raises(ValueError, match="line_count"):
            check_line_range(0, 0, 10)

    def test_empty_document_rejects_every_start(self):
        with pytest.raises(ValueError, match="1 to 0"):
            check_line_range(1, 1, 0, noun="document")


def _lines_size(response: dict) -> int:
    return len(json.dumps(response))


class TestLinesWithinBudget:
    """search-fetch-pairs Requirements 12.8, 12.9."""

    BASE = {"docId": "d1", "title": "T"}

    def test_fitting_range_returned_whole(self):
        lines = ["a\n", "b\n", "c\n", "d"]
        response = lines_within_budget(self.BASE, lines, 2, 2)
        assert response == {
            "docId": "d1",
            "title": "T",
            "start-line": 2,
            "end-line": 3,
            "total-lines": 4,
            "has-more": True,
            "text": "b\nc\n",
        }

    def test_count_clamped_to_end(self):
        lines = ["a\n", "b\n", "c"]
        response = lines_within_budget(self.BASE, lines, 2, MAX_LINE_COUNT)
        assert response["end-line"] == 3
        assert response["has-more"] is False
        assert response["text"] == "b\nc"
        assert "truncated" not in response

    def test_base_key_order_preserved(self):
        base = {"z": 1, "a": 2, "m": 3}
        response = lines_within_budget(base, ["x\n"], 1, 1)
        assert list(response) == [
            "z",
            "a",
            "m",
            "start-line",
            "end-line",
            "total-lines",
            "has-more",
            "text",
        ]

    def test_over_budget_range_returns_whole_lines(self):
        lines = [f"{i:04d} " + "x" * 95 + "\n" for i in range(50)]
        budget = 1_000
        response = lines_within_budget(self.BASE, lines, 3, 50, budget=budget)
        k = response["end-line"] - 3 + 1
        assert 1 <= k < 48
        assert response["text"] == "".join(lines[2 : 2 + k])
        assert response["has-more"] is True
        assert "truncated" not in response
        assert _lines_size(response) <= budget
        # One more whole line would not fit.
        bigger = dict(response, **{"end-line": 3 + k, "text": "".join(lines[2 : 3 + k])})
        assert _lines_size(bigger) > budget

    def test_single_over_budget_line_truncated_at_code_point(self):
        # Astral characters serialize as 12-character surrogate-pair escapes.
        long_line = "😀é" * 5_000 + "\n"
        lines = ["short\n", long_line, "after\n"]
        response = lines_within_budget(self.BASE, lines, 2, 3)
        assert response["start-line"] == 2
        assert response["end-line"] == 2
        assert response["has-more"] is True
        assert response["truncated"] is True
        assert _lines_size(response) <= RESPONSE_BUDGET
        text = response["text"]
        assert text.endswith(TRUNCATION_MARK)
        prefix = text.removesuffix(TRUNCATION_MARK)
        assert prefix
        assert long_line.startswith(prefix)
        # Cut on a code point: no lone surrogate, and one more code point would not fit.
        assert prefix.encode("utf-8").decode("utf-8") == prefix
        longer = dict(response, text=long_line[: len(prefix) + 1] + TRUNCATION_MARK)
        assert _lines_size(longer) > RESPONSE_BUDGET

    def test_always_returns_at_least_one_line(self):
        lines = ["y" * 500 + "\n"] * 3
        response = lines_within_budget(self.BASE, lines, 1, 3, budget=300)
        assert response["end-line"] == 1
        assert response["truncated"] is True
        assert response["text"].endswith(TRUNCATION_MARK)
        assert _lines_size(response) <= 300


class TestRedactStorageLocations:
    """search-fetch-pairs Requirements 4.1, 4.6 (D8): defense-in-depth redactor."""

    def test_home_and_package_dirs_redacted(self):
        pkg = Path(utils_module.__file__).resolve().parent.parent
        msg = f"failed at {Path.home() / 'x.json'} and {pkg / 'oscal_store.db'}"
        sep = os.sep
        assert redact_storage_locations(msg) == (
            f"failed at <redacted>{sep}x.json and <redacted>{sep}oscal_store.db"
        )

    def test_temp_dir_realpath_redacted(self, tmp_path):
        out = redact_storage_locations(f"bad {tmp_path} and {tmp_path.resolve()}")
        assert str(tmp_path) not in out
        assert str(tmp_path.resolve()) not in out

    def test_s3_and_arn_redacted(self):
        out = redact_storage_locations(
            "see s3://bucket/key/doc.md for arn:aws:bedrock:us-east-1:123456789012:kb/ABC."
        )
        assert out == "see <redacted> for <redacted>"

    def test_segment_boundary(self):
        home = str(Path.home())
        assert redact_storage_locations(f"{home}xyz") == f"{home}xyz"

    @pytest.mark.parametrize(
        "msg",
        [
            "shared-constraints/oscal_ssp_constraints.ent",
            "oscal_component_metaschema.xml",
            "Unknown docId",
            "Invalid limit: must be an integer from 1 to 100",
            "",
        ],
    )
    def test_legitimate_messages_unchanged(self, msg):
        assert redact_storage_locations(msg) == msg

    def test_package_dir_inside_home_replaced_whole(self, monkeypatch):
        """The longer package dir wins over its ancestor home dir, so no package-relative
        segments (which still name the deployment layout) survive."""
        pkg = Path(utils_module.__file__).resolve().parent.parent
        monkeypatch.setattr(Path, "home", classmethod(lambda _cls: pkg.parent.parent))
        out = redact_storage_locations(f"bad {pkg.as_posix()}/oscal_store.db")
        assert out == "bad <redacted>/oscal_store.db"

    def test_windows_style_temp_dir_forms(self, monkeypatch):
        """A Windows temp dir is redacted in its native and forward-slash forms."""
        win_tmp = "C:\\Users\\alice\\AppData\\Local\\Temp"
        monkeypatch.setattr(utils_module.tempfile, "gettempdir", lambda: win_tmp)
        out = redact_storage_locations(
            f"a {win_tmp}\\tmpab12\\x.json b C:/Users/alice/AppData/Local/Temp/y.json"
        )
        assert out == "a <redacted>\\tmpab12\\x.json b <redacted>/y.json"
        assert "alice" not in out

    def test_realpath_temp_form_redacted(self):
        real = os.path.realpath(tempfile.gettempdir())
        assert redact_storage_locations(f"{real}{os.sep}x.json") == f"<redacted>{os.sep}x.json"

    def test_file_uri_home_path_redacted(self):
        home = Path.home().as_posix()
        assert redact_storage_locations(f"file://{home}/x.json") == "file://<redacted>/x.json"

    @pytest.mark.parametrize(
        "arn",
        [
            "arn:aws-cn:bedrock:cn-north-1:123456789012:knowledge-base/ABC",
            "arn:aws-us-gov:s3:::my-bucket/key",
            "arn:aws-iso-b:bedrock:us-isob-east-1:123456789012:knowledge-base/XYZ",
        ],
    )
    def test_arn_in_other_partitions_redacted(self, arn):
        assert redact_storage_locations(f"KB {arn} failed") == "KB <redacted> failed"

    def test_try_notify_client_error_sends_redacted_text(self):
        ctx = AsyncMock()
        msg = f"cannot open {Path.home() / 'secret.json'} from s3://bucket/key"
        try_notify_client_error(msg, ctx)
        ctx.error.assert_awaited_once()
        sent = ctx.error.call_args[0][0]
        assert sent == f"cannot open <redacted>{os.sep}secret.json from <redacted>"
        assert str(Path.home()) not in sent


class TestLocalSourcePath:
    """local_source_path classifies a source as local (returns a Path) or remote (None).

    Expectations are written as ``Path`` values, so they hold on Windows and POSIX alike.
    """

    @pytest.mark.parametrize(
        ("source", "expected"),
        [
            # Windows drive-letter paths: urlparse would read "C" as the scheme.
            ("C:\\Users\\a\\x.json", Path("C:\\Users\\a\\x.json")),
            ("C:/Users/a/x.json", Path("C:/Users/a/x.json")),
            ("d:\\deep\\inbox\\pack.zip", Path("d:\\deep\\inbox\\pack.zip")),
            # file: URIs, including a drive letter and percent-encoding.
            ("file:///C:/Users/a/x.json", Path("C:/Users/a/x.json")),
            ("file:///c:/Users/a%20b/x%23.json", Path("c:/Users/a b/x#.json")),
            ("file:///tmp/x.json", Path("/tmp/x.json")),
            ("file:///tmp/a%20b.json", Path("/tmp/a b.json")),
            ("file://localhost/tmp/x.json", Path("/tmp/x.json")),
            ("FILE:///tmp/x.json", Path("/tmp/x.json")),
            # Plain paths, absolute and relative.
            ("/tmp/x.json", Path("/tmp/x.json")),
            ("x.json", Path("x.json")),
            ("sub/dir/x.json", Path("sub/dir/x.json")),
        ],
    )
    def test_local(self, source, expected):
        assert local_source_path(source) == expected

    def test_file_uri_with_host_is_unc(self):
        result = local_source_path("file://server/share/x.json")
        assert result is not None
        assert result.as_posix() == "//server/share/x.json"

    @pytest.mark.parametrize(
        "source",
        [
            "http://example.com/x.json",
            "https://example.com/C:/x.json",
            "s3://bucket/key/x.json",
            "ftp://example.com/x.json",
        ],
    )
    def test_remote(self, source):
        assert local_source_path(source) is None

    def test_as_uri_round_trip(self, tmp_path):
        target = tmp_path / "a b#c.json"
        assert local_source_path(target.as_uri()) == target
        assert local_source_path(str(target)) == target
