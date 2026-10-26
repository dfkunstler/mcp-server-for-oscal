"""
Tests for the query_documentation tool.
"""

import asyncio
import inspect
import json
from collections.abc import Iterator
from unittest.mock import AsyncMock, Mock, patch

import pytest
from botocore.exceptions import ClientError, NoCredentialsError
from mcp.server.mcpserver import MCPServer

from mcp_server_for_oscal.tools import query_documentation, tool_budget
from mcp_server_for_oscal.tools.descriptions import BUDGETED_LIMIT_DOC, OFFSET_DOC
from mcp_server_for_oscal.tools.oscal_store import ORIGIN_BUNDLED, ORIGIN_USER, OscalStore
from mcp_server_for_oscal.tools.query_documentation import (
    KB_FAILED_MESSAGE,
    KB_MAX_RESULTS,
    UNKNOWN_DOC_ID_MESSAGE,
    get_oscal_documentation,
    interleave,
    kb_result_items,
    kb_title,
    merge_page,
    query_oscal_documentation,
)
from mcp_server_for_oscal.tools.utils import RESPONSE_BUDGET

_MODULE = "mcp_server_for_oscal.tools.query_documentation"
_RETRIEVAL_CONFIGURATION = {"vectorSearchConfiguration": {"numberOfResults": KB_MAX_RESULTS}}
_KB_ARN = "arn:aws:bedrock:us-east-1:123456789012:knowledge-base/test-kb-id"


@pytest.fixture
def ctx() -> AsyncMock:
    """MCP context double; its logging methods are awaitable like the real Context."""
    context = AsyncMock()
    context.session = Mock()
    context.session.client_params = {}
    return context


@pytest.fixture
def kb_config() -> Iterator[Mock]:
    """KB_Mode with the default AWS credentials chain."""
    with patch(f"{_MODULE}.config") as cfg:
        cfg.aws_profile = None
        cfg.knowledge_base_id = "test-kb-id"
        cfg.log_level = "INFO"
        yield cfg


@pytest.fixture
def local_config() -> Iterator[Mock]:
    """Local_Mode: no Knowledge Base configured."""
    with patch(f"{_MODULE}.config") as cfg:
        cfg.aws_profile = None
        cfg.knowledge_base_id = ""
        yield cfg


@pytest.fixture
def no_store() -> Iterator[None]:
    with patch.object(query_documentation, "_store", None):
        yield


@pytest.fixture
def doc_store(tmp_path) -> Iterator[OscalStore]:
    """A store with one bundled and two user Documentation_Documents about profiles."""
    bundled = tmp_path / "bundled"
    user = tmp_path / "user"
    bundled.mkdir()
    user.mkdir()
    (bundled / "profile-resolution.md").write_text(
        "# Profile Resolution\n\nBundled guide to profile resolution in OSCAL.\n",
        encoding="utf-8",
    )
    (user / "team-notes.md").write_text(
        "# Team Notes\n\nOur zebrafish profile resolution checklist.\n", encoding="utf-8"
    )
    (user / "widget-notes.md").write_text(
        "# Widget Notes\n\nProfile resolution for widgets.\n", encoding="utf-8"
    )
    store = OscalStore(db_path=str(tmp_path / "docs.db"), seed_from_bundled=False)
    store.scan_directory(bundled, origin=ORIGIN_BUNDLED)
    store.scan_directory(user, origin=ORIGIN_USER)
    with patch.object(query_documentation, "_store", store):
        yield store
    store.close()


def _stub_session(session_class: Mock, response: object = None) -> Mock:
    """Wire ``Session().client().retrieve`` to return ``response``; return ``retrieve``."""
    retrieve = session_class.return_value.client.return_value.retrieve
    retrieve.return_value = response
    return retrieve


def _kb_response(*texts: str) -> dict:
    return {
        "retrievalResults": [
            {
                "content": {"text": text},
                "location": {"s3Location": {"uri": f"s3://kb-bucket/docs/kb-page-{i}.md"}},
                "score": 0.9,
                "metadata": {"x-amz-bedrock-kb-source-uri": f"s3://kb-bucket/docs/{i}.md"},
            }
            for i, text in enumerate(texts)
        ]
    }


class TestKnowledgeBaseMode:
    """KB_Mode: normalized KB_Hits merged with user documentation."""

    @patch(f"{_MODULE}.Session")
    def test_success_with_profile(self, session_class, kb_config, no_store, ctx):
        kb_config.aws_profile = "test-profile"
        retrieve = _stub_session(session_class, {"retrievalResults": []})

        query_oscal_documentation("What is OSCAL?", ctx=ctx)

        session_class.assert_called_once_with(profile_name="test-profile")
        session_class.return_value.client.assert_called_once_with("bedrock-agent-runtime")
        retrieve.assert_called_once_with(
            knowledgeBaseId="test-kb-id",
            retrievalQuery={"text": "What is OSCAL?"},
            retrievalConfiguration=_RETRIEVAL_CONFIGURATION,
        )

    @patch(f"{_MODULE}.Session")
    def test_items_carry_no_kb_location_or_metadata(
        self, session_class, kb_config, no_store, ctx, sample_bedrock_response
    ):
        """Requirements 3.4, 10.2, 10.4: KB_Hits become {title, snippet} items."""
        _stub_session(session_class, sample_bedrock_response)

        result = query_oscal_documentation("What is OSCAL?", ctx=ctx)

        session_class.assert_called_once_with()
        texts = [r["content"]["text"] for r in sample_bedrock_response["retrievalResults"]]
        assert result["items"] == [
            {"title": "oscal overview", "snippet": texts[0]},
            {"title": "oscal implementation", "snippet": texts[1]},
        ]
        assert result["total"] == 2
        assert result["hasMore"] is False
        dumped = json.dumps(result)
        assert "s3://" not in dumped
        assert "NIST OSCAL Documentation" not in dumped
        assert "nextToken" not in dumped

    @patch(f"{_MODULE}.Session")
    def test_merges_user_documentation_only(self, session_class, kb_config, doc_store, ctx):
        """Requirements 11.1-11.4, 11.9: KB, user, KB, user; bundled docs excluded."""
        _stub_session(session_class, _kb_response("first passage", "second passage"))

        result = query_oscal_documentation("profile resolution", ctx=ctx)

        titles = [item["title"] for item in result["items"]]
        assert titles[0] == "kb page 0"
        assert titles[2] == "kb page 1"
        assert sorted(titles[1::2]) == ["Team Notes", "Widget Notes"]
        assert "Profile Resolution" not in titles
        assert result["total"] == 4
        assert all("docId" in item for item in result["items"][1::2])
        assert all("docId" not in item for item in result["items"][::2])

    @patch(f"{_MODULE}.Session")
    def test_pages_walk_merged_sequence_once(self, session_class, kb_config, doc_store, ctx):
        """Requirement 11.6: consecutive pages neither repeat nor skip an item."""
        _stub_session(session_class, _kb_response("a", "b", "c"))
        full = query_oscal_documentation("profile resolution", limit=10, ctx=ctx)

        paged: list[dict] = []
        offset = 0
        while True:
            page = query_oscal_documentation("profile resolution", offset=offset, limit=2, ctx=ctx)
            paged.extend(page["items"])
            offset += len(page["items"])
            if not page["hasMore"]:
                break

        assert paged == full["items"]
        assert len(paged) == full["total"] == 5

    @patch(f"{_MODULE}.Session")
    def test_unique_user_token_found(self, session_class, kb_config, doc_store, ctx):
        """Requirement 11.9: a user document matching the query is returned in KB_Mode."""
        _stub_session(session_class, {"retrievalResults": []})

        result = query_oscal_documentation("zebrafish", ctx=ctx)

        assert [item["title"] for item in result["items"]] == ["Team Notes"]

    @patch(f"{_MODULE}.Session")
    def test_large_passages_fit_response_budget(self, session_class, kb_config, no_store, ctx):
        """D7: long KB passages are paged within RESPONSE_BUDGET."""
        _stub_session(session_class, _kb_response(*["x" * 5_000] * 10))

        result = query_oscal_documentation("big", limit=10, ctx=ctx)

        assert len(json.dumps(result)) <= RESPONSE_BUDGET
        assert 1 <= len(result["items"]) < 10
        assert result["limit"] == len(result["items"])
        assert result["hasMore"] is True

    @patch(f"{_MODULE}.Session")
    def test_empty_response(self, session_class, kb_config, no_store, ctx):
        _stub_session(session_class, {"retrievalResults": [], "nextToken": None})

        result = query_oscal_documentation("Nonexistent topic", ctx=ctx)

        assert result["items"] == []
        assert result["total"] == 0

    @patch(f"{_MODULE}.Session")
    @patch(f"{_MODULE}.logger")
    def test_logging(self, mock_logger, session_class, kb_config, no_store, ctx):
        kb_config.aws_profile = "test-profile"
        _stub_session(session_class, {"retrievalResults": []})

        query_oscal_documentation("What is OSCAL?", ctx=ctx)

        debug_calls = [c.args for c in mock_logger.debug.call_args_list]
        assert ("Using AWS profile: %s", "test-profile") in debug_calls


class TestKnowledgeBaseFailure:
    """Requirements 4.3, 11.8: KB failures warn with fixed text and fall back locally."""

    @patch(f"{_MODULE}.Session")
    def test_client_error_falls_back_to_both_origins(
        self, session_class, kb_config, doc_store, ctx, caplog
    ):
        error = {"Error": {"Code": "ResourceNotFoundException", "Message": f"{_KB_ARN} missing"}}
        session_class.return_value.client.return_value.retrieve.side_effect = ClientError(
            error, "Retrieve"
        )

        result = query_oscal_documentation("profile resolution", ctx=ctx)

        ctx.warning.assert_called_once_with(KB_FAILED_MESSAGE)
        ctx.error.assert_not_called()
        titles = sorted(item["title"] for item in result["items"])
        assert titles == ["Profile Resolution", "Team Notes", "Widget Notes"]
        (warning,) = [r for r in caplog.records if r.name == _MODULE and r.levelname == "WARNING"]
        assert warning.exc_info is not None
        assert _KB_ARN not in json.dumps(result)

    @patch(f"{_MODULE}.Session")
    def test_no_credentials_error_falls_back(self, session_class, kb_config, no_store, ctx):
        session_class.side_effect = NoCredentialsError()

        result = query_oscal_documentation("What is OSCAL?", ctx=ctx)

        ctx.warning.assert_called_once_with(KB_FAILED_MESSAGE)
        assert result == {"error": "OscalStore has not been initialized"}


class TestLocalMode:
    def test_searches_both_origins(self, local_config, doc_store, ctx):
        """Requirements 10.2, 10.3, 11.7: Doc_Result_Items with docId over both Origins."""
        result = query_oscal_documentation("profile resolution", ctx=ctx)

        assert result["total"] == 3
        for item in result["items"]:
            assert set(item) == {"docId", "title", "snippet"}
            assert len(item["snippet"]) <= 200

    def test_unique_user_token_found(self, local_config, doc_store, ctx):
        """Requirement 11.9: a user document matching the query is returned in Local_Mode."""
        result = query_oscal_documentation("zebrafish", ctx=ctx)

        assert [item["title"] for item in result["items"]] == ["Team Notes"]
        assert result["total"] == 1
        assert doc_store.get_documentation(result["items"][0]["docId"]) is not None

    def test_no_store_returns_error_dict(self, local_config, no_store, ctx):
        assert query_oscal_documentation("anything", ctx=ctx) == {
            "error": "OscalStore has not been initialized"
        }


class TestArguments:
    @pytest.mark.parametrize("query", ["", "   \n\t"])
    @patch(f"{_MODULE}.Session")
    def test_blank_query_returns_empty_page_without_kb_call(
        self, session_class, kb_config, query, ctx
    ):
        """Requirement 10.7."""
        result = query_oscal_documentation(query, ctx=ctx)

        session_class.assert_not_called()
        assert result["items"] == []
        assert result["total"] == 0
        assert result["hasMore"] is False

    @pytest.mark.parametrize(
        ("kwargs", "name"),
        [({"offset": -1}, "offset"), ({"limit": 0}, "limit"), ({"limit": 101}, "limit")],
    )
    @patch(f"{_MODULE}.Session")
    def test_out_of_range_page_names_parameter(self, session_class, kb_config, kwargs, name, ctx):
        """Requirement 10.6: notify and raise, naming the parameter."""
        with pytest.raises(ValueError, match=name):
            query_oscal_documentation("OSCAL", ctx=ctx, **kwargs)

        ctx.error.assert_called_once()
        assert name in ctx.error.call_args.args[0]
        session_class.assert_not_called()

    @pytest.mark.parametrize("query", ["", "   \n\t"])
    def test_blank_query_returns_empty_page_in_local_mode(self, local_config, doc_store, query):
        """Requirement 10.7: Local_Mode also returns an empty page for a blank query."""
        result = query_oscal_documentation(query)

        assert result["items"] == []
        assert result["total"] == 0
        assert result["hasMore"] is False

    @pytest.mark.parametrize(
        ("kwargs", "range_text"),
        [
            ({"offset": -1}, "offset must be non-negative"),
            ({"limit": 0}, "limit must be between 1 and 100"),
            ({"limit": 101}, "limit must be between 1 and 100"),
        ],
    )
    @pytest.mark.parametrize("mode", ["kb_config", "local_config"])
    def test_out_of_range_message_states_valid_range(
        self, request, no_store, mode, kwargs, range_text, ctx
    ):
        """Requirement 10.6: the Client_Facing_Message gives the valid range in both modes."""
        request.getfixturevalue(mode)
        with pytest.raises(ValueError, match=range_text):
            query_oscal_documentation("OSCAL", ctx=ctx, **kwargs)

        assert range_text in ctx.error.call_args.args[0]

    @patch(f"{_MODULE}.Session")
    def test_kb_request_asks_for_100_results(self, session_class, kb_config, no_store):
        """Requirement 11.5: the KB call requests the Bedrock maximum of 100 KB_Hits."""
        retrieve = _stub_session(session_class, {"retrievalResults": []})

        query_oscal_documentation("OSCAL", offset=40, limit=5)

        config = retrieve.call_args.kwargs["retrievalConfiguration"]
        assert config == {"vectorSearchConfiguration": {"numberOfResults": 100}}


class TestPureHelpers:
    def test_kb_title_prefers_first_heading(self):
        result = {
            "content": {"text": "intro\n## Profile Resolution ##\n# Later"},
            "location": {"s3Location": {"uri": "s3://bucket/a/other-name.md"}},
        }
        assert kb_title(result) == "Profile Resolution"

    @pytest.mark.parametrize(
        ("location", "expected"),
        [
            (
                {"s3Location": {"uri": "s3://bucket/a/oscal_profile-guide.md"}},
                "oscal profile guide",
            ),
            ({"webLocation": {"url": "https://example.com/docs/ssp-intro.html?x=1"}}, "ssp intro"),
            ({"customDocumentLocation": {"id": _KB_ARN}}, ""),
            # Opaque custom IDs are whole location values, so they give no title.
            ({"customDocumentLocation": {"id": "doc-0001"}}, ""),
            ({"customDocumentLocation": {"id": "oscal_profile-guide.md"}}, ""),
            ({"customDocumentLocation": {"id": "opaque/"}}, ""),
            # Path-like custom IDs give the stem of the last segment.
            ({"customDocumentLocation": {"id": "guides/ssp-intro.md"}}, "ssp intro"),
            ({"customDocumentLocation": {"id": "guides\\poam_basics.txt"}}, "poam basics"),
            # A bucket or host with no path segment after it gives no title.
            ({"s3Location": {"uri": "s3://bucket/"}}, ""),
            ({"s3Location": {"uri": "s3://bucket"}}, ""),
            ({"webLocation": {"url": "https://example.com/?x=1"}}, ""),
            ({"s3Location": {"uri": "s3://bucket/top-level.md"}}, "top level"),
            ({}, ""),
            (None, ""),
        ],
    )
    def test_kb_title_from_location_stem(self, location, expected):
        assert kb_title({"content": {"text": "no heading"}, "location": location}) == expected

    @pytest.mark.parametrize("heading", ["/Users/someone/doc.md", "s3://bucket/key", "arn:aws:x"])
    def test_kb_title_rejects_storage_location_heading(self, heading):
        result = {"content": {"text": f"# {heading}"}, "location": None}
        assert kb_title(result) == ""

    def test_kb_result_items_reads_only_text_and_title(self):
        response = {
            "retrievalResults": [
                {"content": {"text": "keep"}, "metadata": {"title": "meta title"}},
                {"content": {"text": 42}},
                {"content": None},
                "not a mapping",
            ],
            "ResponseMetadata": {"RequestId": "r"},
        }
        assert kb_result_items(response) == [{"title": "", "snippet": "keep"}]
        assert kb_result_items(None) == []
        assert kb_result_items({"retrievalResults": None}) == []

    def test_interleave(self):
        assert interleave([1, 2, 3], ["a"]) == [1, "a", 2, 3]
        assert interleave([], ["a", "b"]) == ["a", "b"]
        assert interleave([1], []) == [1]

    def test_merge_page(self):
        kb = [{"k": 0}, {"k": 1}]
        local = [{"l": 0}, {"l": 1}, {"l": 2}]
        page = merge_page(kb, local, local_total=5, offset=1, limit=3)
        assert page == {
            "items": [{"l": 0}, {"k": 1}, {"l": 1}],
            "total": 7,
            "offset": 1,
            "limit": 3,
            "hasMore": True,
        }


class TestQueryDocumentationDescription:
    """tool-context-optimization Req 2.1-2.4, 3.1: Description_Shape and parameter docs."""

    def test_docstring_shape(self):
        doc = inspect.getdoc(query_oscal_documentation) or ""
        assert tool_budget.description_shape_problems(doc) == []
        assert len(" ".join(doc.split())) <= 250
        assert "get_schema tool" not in doc
        assert "get_oscal_schema" in doc
        assert "Knowledge Base" in doc
        assert "Pass a hit's `docId` to get_oscal_documentation" in doc

    def test_parameter_descriptions_in_both_registries(self):
        """Requirement 10.1: shared Offset and BudgetedLimit descriptions."""
        props = query_oscal_documentation.tool_spec["inputSchema"]["json"]["properties"]
        assert props["query"]["description"] == "Question or search terms about OSCAL."
        assert props["offset"]["description"] == OFFSET_DOC
        assert props["limit"]["description"] == BUDGETED_LIMIT_DOC

        server = MCPServer("test")  # fresh instance, never main.mcp
        server.add_tool(query_oscal_documentation)
        (mcp_tool,) = asyncio.run(server.list_tools())
        schema = mcp_tool.input_schema
        assert schema["properties"]["query"]["description"] == (
            "Question or search terms about OSCAL."
        )
        assert schema["properties"]["offset"]["description"] == OFFSET_DOC
        assert schema["properties"]["limit"]["description"] == BUDGETED_LIMIT_DOC
        assert schema["properties"]["offset"]["default"] == 0
        assert schema["properties"]["limit"]["default"] == 10
        assert schema["required"] == ["query"]
        assert "ctx" not in schema["properties"]


class TestGetOscalDocumentation:
    """search-fetch-pairs Requirement 12: minimal checks (full suite is task 12.5)."""

    def test_fetches_both_origins_verbatim(self, doc_store):
        """Requirement 12.3: every hit (one bundled, two user docs) fetches by its docId."""
        items = doc_store.search_documentation("profile resolution", 0, 10)["items"]
        assert len(items) == 3
        for item in items:
            doc_id = item["docId"]
            expected = doc_store.get_documentation(doc_id)
            result = get_oscal_documentation(doc_id)
            assert result["docId"] == doc_id
            assert result["title"] == expected.title == item["title"]
            assert result["text"] == "".join(expected.lines)
            assert result["start-line"] == 1
            assert result["end-line"] == result["total-lines"] == len(expected.lines)
            assert result["has-more"] is False

    @pytest.mark.parametrize("bad", ["nope", "/etc/passwd", 42, None])
    def test_unknown_doc_id_is_not_echoed(self, doc_store, ctx, bad):
        with pytest.raises(ValueError, match="Unknown documentation ID") as exc:
            get_oscal_documentation(bad, ctx=ctx)
        assert str(exc.value) == UNKNOWN_DOC_ID_MESSAGE
        ctx.error.assert_called_once_with(UNKNOWN_DOC_ID_MESSAGE)

    def test_line_range_errors_name_parameter(self, doc_store):
        doc_id = doc_store.search_documentation("zebrafish", 0, 1)["items"][0]["docId"]
        total = len(doc_store.get_documentation(doc_id).lines)
        with pytest.raises(ValueError, match=r"line_count.*1 to 500"):
            get_oscal_documentation(doc_id, line_count=0)
        with pytest.raises(ValueError, match=rf"start_line.*this document: 1 to {total}"):
            get_oscal_documentation(doc_id, start_line=total + 1)

    def test_store_not_initialized(self, no_store):
        with pytest.raises(RuntimeError, match="not been initialised"):
            get_oscal_documentation("anything")
