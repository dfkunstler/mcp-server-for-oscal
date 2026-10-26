"""Path_Leak_Test: no registered tool leaks a Storage_Location (Requirements 3, 4, 5).

Every tool in the live ``tools/list`` (``get_tool_list()`` plus ``about``) is called through
the production registration (``main.build_server()`` + ``main._setup_tools``) and an
in-memory MCP ``Client`` against the Fixture_Store from ``build_leak_store``. Each result
and every Client_Facing_Message is searched for the fixture's storage locations, the home,
temp, and package directories, ``s3://``, ``arn:aws``, and the stubbed Knowledge Base
location and metadata values.

Client_Facing_Messages are captured before redaction: every module binding of
``safe_log_mcp`` and ``try_notify_client_error`` records the raw message, so the test
enforces fixed-text call sites and does not rely on ``redact_storage_locations`` (D8). The
text of any exception a tool raises is recorded as well.

The guard that there is one model fixture per ``OSCALModelType`` is
``tests/test_fixtures_models.py::test_one_fixture_per_model_type``.
"""

import asyncio
import contextlib
import json
import os
import sys
import tempfile
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import pytest
from botocore.exceptions import ClientError
from mcp.client import Client
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.utilities.func_metadata import FuncMetadata
from mcp.types import CallToolResult

import mcp_server_for_oscal
from mcp_server_for_oscal import main
from mcp_server_for_oscal.tools import (
    list_oscal_resources,
    query_component_definition,
    query_documentation,
    query_oscal_models,
    utils,
    validate_oscal_content,
)
from mcp_server_for_oscal.tools.metaschema_index import get_index
from mcp_server_for_oscal.tools.utils import OSCALModelType

from .fixture_store import LeakFixture, build_leak_store

# Originals, captured before any test patches them, to find every module binding.
_SAFE_LOG_MCP = utils.safe_log_mcp
_TRY_NOTIFY = utils.try_notify_client_error

# The four store singletons that main._install_store sets (module, attribute).
_STORE_SINGLETONS = (
    (list_oscal_resources, "_store"),
    (query_component_definition, "_oscal_store"),
    (query_documentation, "_store"),
    (query_oscal_models, "_store"),
)

UNKNOWN_UUID = "00000000-0000-4000-8000-00000000dead"
UNKNOWN_NAME = "zzqx-no-such-name"
UNKNOWN_TEXT = "zzqx qqvv nonexistent"

# --- Knowledge Base stub (Requirement 5.4) ---------------------------------------------------

KB_ID = "leak-kb-id"
KB_ARN = "arn:aws:bedrock:us-east-1:123456789012:knowledge-base/LEAKKB0001"
KB_WEB_URL = "https://leak.example/private/kb-page.html"
KB_METADATA_VALUES = ("leak-chunk-5b1e", "LEAKDATASRC9")


def _kb_s3_uri(fixture: LeakFixture) -> str:
    return f"s3://leak-bucket/{fixture.root.name}/doc.md"


def _kb_response(fixture: LeakFixture) -> dict:
    """A ``retrieve`` response whose KB_Hits carry locations, metadata, and an ARN."""
    uri = _kb_s3_uri(fixture)
    metadata = {
        "x-amz-bedrock-kb-source-uri": uri,
        "x-amz-bedrock-kb-chunk-id": KB_METADATA_VALUES[0],
        "x-amz-bedrock-kb-data-source-id": KB_METADATA_VALUES[1],
        "x-amz-bedrock-kb-arn": KB_ARN,
    }
    return {
        "retrievalResults": [
            {
                # Title from the heading.
                "content": {"text": "# Stub Overview\n\nKB passage about the OSCAL overview."},
                "location": {"type": "S3", "s3Location": {"uri": uri}},
                "metadata": metadata,
                "score": 0.9,
            },
            {
                # No heading: title from the location's file stem.
                "content": {"text": "Another KB passage about OSCAL layers."},
                "location": {"type": "S3", "s3Location": {"uri": uri}},
                "metadata": metadata,
                "score": 0.8,
            },
            {
                "content": {"text": "A web-crawled KB passage about OSCAL."},
                "location": {"type": "WEB", "webLocation": {"url": KB_WEB_URL}},
                "metadata": metadata,
                "score": 0.7,
            },
        ],
        "ResponseMetadata": {"RequestId": "leak-request", "HTTPHeaders": {"x-arn": KB_ARN}},
    }


@dataclass
class _StubBedrock:
    """Stand-in for ``boto3.Session`` and its ``bedrock-agent-runtime`` client."""

    mode: Literal["stub", "fail"]
    fixture: LeakFixture
    calls: list[dict] = field(default_factory=list)

    def session(self, *_args: object, **_kwargs: object) -> "_StubBedrock":
        return self

    def client(self, *_args: object, **_kwargs: object) -> "_StubBedrock":
        return self

    def retrieve(self, **kwargs: object) -> dict:
        self.calls.append(kwargs)
        if self.mode == "fail":
            message = (
                f"User is not authorized to perform bedrock:Retrieve on {KB_ARN}; "
                f"source {_kb_s3_uri(self.fixture)}"
            )
            raise ClientError(
                {"Error": {"Code": "AccessDeniedException", "Message": message}}, "Retrieve"
            )
        return _kb_response(self.fixture)


# --- Argument table (Requirements 5.1, 5.2, 5.6) ---------------------------------------------


@dataclass(frozen=True)
class Call:
    """One Path_Leak_Test call: fixture → arguments, expected kind, and KB mode."""

    args: Callable[[LeakFixture], dict[str, Any]]
    kind: Literal["ok", "unknown"]  # "ok" must not be an error result
    kb: Literal["off", "stub", "fail"] = "off"  # query_oscal_documentation modes


def _fixed(**kwargs: Any) -> Callable[[LeakFixture], dict[str, Any]]:
    """Arguments that do not depend on the fixture."""

    def args(_f: LeakFixture) -> dict[str, Any]:
        return dict(kwargs)

    return args


_no_args = _fixed()


def _query_model_calls(model_type: OSCALModelType) -> tuple[Call, ...]:
    """ok: all and by_uuid; unknown: by_uuid and by_title with unknown values."""
    return (
        Call(_no_args, "ok"),
        Call(lambda f: {"query_type": "by_uuid", "query_value": f.doc_uuids[model_type]}, "ok"),
        Call(_fixed(query_type="by_uuid", query_value=UNKNOWN_UUID), "unknown"),
        Call(_fixed(query_type="by_title", query_value=UNKNOWN_NAME), "unknown"),
    )


def _child_list_calls(element_type: str) -> tuple[Call, ...]:
    """ok: all parents and the fixture parent; unknown: an unknown parent UUID."""
    return (
        Call(_no_args, "ok"),
        Call(lambda f: {"parent_doc_uuid": f.children[element_type].parent_doc_uuid}, "ok"),
        Call(_fixed(parent_doc_uuid=UNKNOWN_UUID), "unknown"),
    )


def _first_source_file(_f: LeakFixture) -> dict[str, Any]:
    return {"file": get_index().source_files()[0], "line_count": 50}


CALLS: dict[str, tuple[Call, ...]] = {
    "about": (Call(_no_args, "ok"),),
    "list_oscal_models": (Call(_no_args, "ok"),),
    "get_oscal_schema": (
        Call(_fixed(model_name="catalog"), "ok"),
        Call(_fixed(model_name="catalog", schema_type="xsd"), "ok"),
        Call(_fixed(model_name=UNKNOWN_NAME), "unknown"),
    ),
    "get_allowed_values": (
        Call(_fixed(model="component-definition", target="defined-component/@type"), "ok"),
        Call(_fixed(model="component-definition", target="defined-component"), "ok"),
        Call(_fixed(model="catalog", target=f"{UNKNOWN_NAME}/@name"), "unknown"),
        Call(_fixed(model=UNKNOWN_NAME, target="control"), "unknown"),
    ),
    "get_constraints": (
        Call(_fixed(model="catalog", target="control"), "ok"),
        Call(_fixed(model="catalog", target=UNKNOWN_NAME), "unknown"),
    ),
    "find_references_to": (
        Call(_fixed(assembly="component"), "ok"),
        Call(_fixed(assembly=UNKNOWN_NAME), "unknown"),
        Call(_fixed(assembly="component", model=UNKNOWN_NAME), "unknown"),
    ),
    "get_definition": (
        Call(_fixed(name="control"), "ok"),
        Call(_fixed(name=UNKNOWN_NAME), "unknown"),
    ),
    "get_metaschema_source": (
        Call(_no_args, "ok"),
        Call(_first_source_file, "ok"),
        Call(_fixed(file=UNKNOWN_NAME + ".xml"), "unknown"),
        Call(lambda f: {"file": str(f.root / "oscal_catalog_metaschema.xml")}, "unknown"),
    ),
    "list_oscal_resources": (Call(_no_args, "ok"),),
    "query_component_definition": (
        Call(_no_args, "ok"),
        Call(lambda f: {"query_type": "by_uuid", "query_value": f.component_uuid}, "ok"),
        Call(lambda f: {"component_definition_filter": f.user_cdef_uuids[0]}, "ok"),
        Call(_fixed(query_type="by_uuid", query_value=UNKNOWN_UUID), "unknown"),
        Call(_fixed(component_definition_filter=UNKNOWN_UUID), "unknown"),
    ),
    "list_component_definitions": (Call(_no_args, "ok"),),
    "list_components": (Call(_no_args, "ok"),),
    "list_capabilities": (Call(_no_args, "ok"),),
    "get_capability": (
        Call(lambda f: {"uuid": f.capability_uuid}, "ok"),
        Call(_fixed(uuid=UNKNOWN_UUID), "unknown"),
    ),
    "validate_oscal_content": (
        Call(lambda f: {"content": f.valid_json_file.read_text(encoding="utf-8")}, "ok"),
        Call(_fixed(content="{not json"), "unknown"),
        Call(_fixed(content=json.dumps({UNKNOWN_NAME: {}})), "unknown"),
        Call(_fixed(content="{}", model_type=UNKNOWN_NAME), "unknown"),
    ),
    "validate_oscal_file": (
        Call(lambda f: {"file_uri": str(f.valid_json_file)}, "ok"),
        Call(lambda f: {"file_uri": f.valid_json_file.as_uri()}, "ok"),
        Call(lambda f: {"file_uri": str(f.root / "missing.json")}, "unknown"),
        Call(lambda f: {"file_uri": str(f.root)}, "unknown"),
        Call(_fixed(file_uri="s3://bucket/key.json"), "unknown"),
    ),
    "query_catalog": _query_model_calls(OSCALModelType.CATALOG),
    "list_catalogs": (Call(_no_args, "ok"),),
    "query_ssp": _query_model_calls(OSCALModelType.SYSTEM_SECURITY_PLAN),
    "list_ssps": (Call(_no_args, "ok"),),
    "query_profile": _query_model_calls(OSCALModelType.PROFILE),
    "list_profiles": (Call(_no_args, "ok"),),
    "query_assessment_plan": _query_model_calls(OSCALModelType.ASSESSMENT_PLAN),
    "list_assessment_plans": (Call(_no_args, "ok"),),
    "query_assessment_results": _query_model_calls(OSCALModelType.ASSESSMENT_RESULTS),
    "list_assessment_results": (Call(_no_args, "ok"),),
    "query_poam": _query_model_calls(OSCALModelType.PLAN_OF_ACTION_AND_MILESTONES),
    "list_poams": (Call(_no_args, "ok"),),
    "query_mapping_collection": _query_model_calls(OSCALModelType.MAPPING),
    "list_mapping_collections": (Call(_no_args, "ok"),),
    "text_search_oscal": (
        Call(lambda f: {"query_text": f.text_query}, "ok"),
        Call(lambda f: {"query_text": f.control.element_id}, "ok"),
        Call(lambda f: {"query_text": f.text_query, "oscal_model_type": "catalog"}, "ok"),
        Call(_fixed(query_text=UNKNOWN_TEXT), "unknown"),
        Call(lambda f: {"query_text": str(f.root)}, "unknown"),
    ),
    "list_catalog_controls": _child_list_calls("control"),
    "list_catalog_groups": _child_list_calls("group"),
    "list_ssp_control_implementations": _child_list_calls("control-implementation"),
    "list_ssp_system_components": _child_list_calls("system-component"),
    "list_profile_imports": _child_list_calls("import"),
    "list_profile_modify": _child_list_calls("modify"),
    "list_assessment_plan_tasks": _child_list_calls("task"),
    "list_assessment_plan_activities": _child_list_calls("activity"),
    "list_assessment_results_results": _child_list_calls("result"),
    "list_assessment_results_findings": _child_list_calls("finding"),
    "list_poam_items": _child_list_calls("poam-item"),
    "list_mapping_collection_mappings": _child_list_calls("mapping"),
    "get_child_element": (
        Call(
            lambda f: {
                "element_id": f.control.element_id,
                "parent_doc_uuid": f.control.parent_doc_uuid,
            },
            "ok",
        ),
        Call(lambda f: {"element_id": f.children["poam-item"].element_id}, "ok"),
        Call(lambda f: {"element_id": "zz-99", "parent_doc_uuid": f.catalog_uuid}, "unknown"),
        Call(_fixed(element_id="ac-2", parent_doc_uuid=UNKNOWN_UUID), "unknown"),
    ),
    "query_oscal_documentation": (
        Call(lambda f: {"query": f.doc_query}, "ok"),
        Call(lambda f: {"query": f.doc_query}, "ok", kb="stub"),
        Call(lambda f: {"query": f.doc_query}, "ok", kb="fail"),
        Call(_fixed(query=UNKNOWN_TEXT), "unknown"),
        Call(_fixed(query=UNKNOWN_TEXT), "unknown", kb="stub"),
    ),
    "get_oscal_documentation": (
        Call(lambda f: {"doc_id": f.user_doc_id}, "ok"),
        Call(lambda f: {"doc_id": f.bundled_doc_id}, "ok"),
        Call(lambda f: {"doc_id": f.awesome_doc_id, "line_count": 3}, "ok"),
        Call(lambda f: {"doc_id": str(f.root / "x.md")}, "unknown"),
        Call(lambda f: {"doc_id": f.catalog_uuid}, "unknown"),
        Call(_fixed(doc_id=UNKNOWN_UUID), "unknown"),
    ),
}

METASCHEMA_TOOLS = (
    "get_allowed_values",
    "get_constraints",
    "get_definition",
    "find_references_to",
    "get_metaschema_source",
)

# Optional parameters that identify an entity (design: Path_Leak_Test, test 2).
EXTRA_IDENTIFYING_PARAMS = frozenset(
    {"query_value", "parent_doc_uuid", "file", "doc_id", "file_uri"}
)

CASES = [
    pytest.param(tool, index, call, id=f"{tool}-{index}-{call.kind}-{call.kb}")
    for tool, calls in CALLS.items()
    for index, call in enumerate(calls)
]


def _label(tool: str, index: int, call: Call) -> str:
    return f"tool {tool!r}, call #{index} ({call.kind}, kb={call.kb})"


# --- Fixtures --------------------------------------------------------------------------------


@pytest.fixture(scope="module")
def leak(tmp_path_factory: pytest.TempPathFactory) -> Iterator[LeakFixture]:
    fixture = build_leak_store(tmp_path_factory.mktemp("leak"))
    try:
        yield fixture
    finally:
        fixture.store.close()


@pytest.fixture(scope="module")
def server() -> MCPServer:
    srv = main.build_server()
    main._setup_tools(srv)
    return srv


@pytest.fixture(scope="module")
def tool_schemas(server: MCPServer) -> dict[str, dict]:
    """Name → inputSchema of the live ``tools/list`` response."""

    async def list_tools() -> dict[str, dict]:
        async with Client(server) as client:
            result = await client.list_tools()
        return {t.name: t.input_schema for t in result.tools}

    return asyncio.run(list_tools())


@dataclass
class Messages:
    """Raw Client_Facing_Messages recorded by the patched notification bindings."""

    items: list[str] = field(default_factory=list)


@pytest.fixture
def messages(monkeypatch: pytest.MonkeyPatch) -> Messages:
    """Patch every module binding of ``safe_log_mcp`` / ``try_notify_client_error``.

    The recorder keeps the message as the call site passed it (before redaction).
    """
    recorded = Messages()

    def record_safe_log(msg: object, _ctx: object = None, *_args: object, **_kw: object) -> None:
        recorded.items.append(str(msg))

    def record_notify(msg: object, _ctx: object = None, *_args: object, **_kw: object) -> None:
        recorded.items.append(str(msg))

    patched = set()
    for name, module in list(sys.modules.items()):
        if not name.startswith("mcp_server_for_oscal") or module is None:
            continue
        for attr, value in list(vars(module).items()):
            if value is _SAFE_LOG_MCP:
                monkeypatch.setattr(module, attr, record_safe_log)
                patched.add(f"{name}.{attr}")
            elif value is _TRY_NOTIFY:
                monkeypatch.setattr(module, attr, record_notify)
                patched.add(f"{name}.{attr}")
    # The KB failure notice and the doc-fetch error must both be captured.
    assert "mcp_server_for_oscal.tools.query_documentation.safe_log_mcp" in patched
    assert "mcp_server_for_oscal.tools.utils.try_notify_client_error" in patched

    # The MCP SDK replaces an unexpected exception's text with a generic message on the
    # wire, but the Agent passes it to the model, so the raw text is a Client_Facing_Message
    # too. Record it where the SDK calls the tool function.
    original_call_fn = FuncMetadata.call_fn

    async def recording_call_fn(self: FuncMetadata, *args: Any, **kwargs: Any) -> Any:
        try:
            return await original_call_fn(self, *args, **kwargs)
        except Exception as exc:
            recorded.items.append(f"{type(exc).__name__}: {exc}")
            raise

    monkeypatch.setattr(FuncMetadata, "call_fn", recording_call_fn)
    return recorded


@pytest.fixture
def installed(leak: LeakFixture, monkeypatch: pytest.MonkeyPatch) -> LeakFixture:
    """Install the Fixture_Store on every tool module; monkeypatch restores the singletons."""
    for module, attr in _STORE_SINGLETONS:
        monkeypatch.setattr(module, attr, getattr(module, attr))
    main._install_store(leak.store)
    # No network: remote URIs stay disabled; KB mode is set per call.
    monkeypatch.setattr(validate_oscal_content.config, "allow_remote_uris", False)
    return leak


# --- Helpers ---------------------------------------------------------------------------------


def _path_forms(path: Path | str) -> set[str]:
    """Native, POSIX, realpath, and JSON-escaped forms of ``path``."""
    forms: set[str] = set()
    for text in {str(path), os.path.realpath(path)}:
        for form in (text, Path(text).as_posix()):
            forms.add(form)
            forms.add(json.dumps(form)[1:-1])
    return forms


def _needles(fixture: LeakFixture) -> list[tuple[str, str]]:
    """(kind, value) pairs that must not appear in any result or message."""
    paths: list[tuple[str, Path | str]] = [
        ("tmp root", fixture.root),
        ("DB path", fixture.db_path),
        ("bundled dir", fixture.bundled_dir),
        ("user-docs dir", fixture.user_docs_dir),
        ("package dir", Path(mcp_server_for_oscal.__file__).parent),
        ("temp dir", tempfile.gettempdir()),
    ]
    home = Path.home()
    if len(str(home)) > 3:
        paths.append(("home dir", home))
    needles = [(kind, form) for kind, path in paths for form in sorted(_path_forms(path))]
    needles += [
        ("s3 URI", "s3://"),
        ("ARN", "arn:aws"),
        ("KB stub location", _kb_s3_uri(fixture)),
        ("KB stub location", KB_WEB_URL),
        ("KB stub metadata", KB_ARN),
    ]
    needles += [("KB stub metadata", value) for value in KB_METADATA_VALUES]
    return needles


def _find_leaks(needles: list[tuple[str, str]], haystacks: list[tuple[str, str]]) -> list[str]:
    """Sorted ``"<needle kind> in <where>"`` for each needle found (case-insensitive)."""
    return sorted(
        {
            f"{kind} in {where}"
            for kind, needle in needles
            for where, text in haystacks
            if needle.casefold() in text.casefold()
        }
    )


def _strings(value: object) -> Iterator[str]:
    """Every string (keys and values) inside a JSON-like value."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, Mapping):
        for key, item in value.items():
            yield str(key)
            yield from _strings(item)
    elif isinstance(value, list | tuple):
        for item in value:
            yield from _strings(item)


def _payloads(result: CallToolResult) -> list[object]:
    """Parsed JSON of each text content item (or the raw text) plus structured content."""
    payloads: list[object] = []
    for item in result.content:
        text = getattr(item, "text", None)
        if not isinstance(text, str):
            continue
        payloads.append(text)
        with contextlib.suppress(ValueError):  # error texts are plain, not JSON
            payloads.append(json.loads(text))
    if result.structured_content is not None:
        payloads.append(json.dumps(result.structured_content))
        payloads.append(result.structured_content)
    return payloads


def _call_tool(server: MCPServer, name: str, args: dict[str, Any]) -> CallToolResult:
    async def call() -> CallToolResult:
        async with Client(server) as client:
            return await client.call_tool(name, args)

    return asyncio.run(call())


def _run(
    tool: str,
    call: Call,
    fixture: LeakFixture,
    server: MCPServer,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[CallToolResult, _StubBedrock | None]:
    """Run one CALLS entry in its KB mode."""
    stub = None
    if call.kb == "off":
        monkeypatch.setattr(query_documentation.config, "knowledge_base_id", "")
    else:
        stub = _StubBedrock(call.kb, fixture)
        monkeypatch.setattr(query_documentation.config, "knowledge_base_id", KB_ID)
        monkeypatch.setattr(query_documentation.config, "aws_profile", None)
        monkeypatch.setattr(query_documentation, "Session", stub.session)
    return _call_tool(server, tool, call.args(fixture)), stub


def _error_text(result: CallToolResult) -> str:
    return " ".join(getattr(item, "text", "") or "" for item in result.content)


# --- Tests -----------------------------------------------------------------------------------


class TestCallsTable:
    def test_coverage(self, tool_schemas: dict[str, dict]) -> None:
        """Requirement 5.6: every registered tool has CALLS entries, and none are stale."""
        live = set(tool_schemas)
        missing = sorted(live - set(CALLS))
        stale = sorted(set(CALLS) - live)
        assert not missing, f"add Path_Leak_Test arguments for: {', '.join(missing)}"
        assert not stale, f"remove stale CALLS entries: {', '.join(stale)}"

    def test_every_tool_has_ok_call(self) -> None:
        """Requirement 5.1."""
        no_ok = sorted(t for t, calls in CALLS.items() if not any(c.kind == "ok" for c in calls))
        assert not no_ok, f"tools without an 'ok' call: {', '.join(no_ok)}"

    def test_identifying_params_have_unknown_call(self, tool_schemas: dict[str, dict]) -> None:
        """Requirement 5.2: required params plus the extra identifying ones."""
        lacking = []
        for name, schema in sorted(tool_schemas.items()):
            params = set(schema.get("properties", {}))
            identifying = set(schema.get("required", [])) | (params & EXTRA_IDENTIFYING_PARAMS)
            if identifying and not any(c.kind == "unknown" for c in CALLS.get(name, ())):
                lacking.append(f"{name} ({', '.join(sorted(identifying))})")
        assert not lacking, f"tools without an 'unknown' call: {'; '.join(lacking)}"


@pytest.mark.parametrize(("tool", "index", "call"), CASES)
def test_no_storage_location_leaks(
    tool: str,
    index: int,
    call: Call,
    installed: LeakFixture,
    server: MCPServer,
    messages: Messages,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Requirements 3.1, 3.4, 4.1, 4.3, 5.1-5.4: results and messages hold no needle."""
    label = _label(tool, index, call)
    result, stub = _run(tool, call, installed, server, monkeypatch)
    if call.kind == "ok":
        assert not result.is_error, f"{label}: expected success, got {_error_text(result)}"
    if stub is not None:
        assert stub.calls, f"{label}: the Knowledge Base stub was not called"
    if result.is_error:
        assert messages.items, f"{label}: the error's Client_Facing_Message was not captured"

    haystacks = [(f"result {i}", s) for i, p in enumerate(_payloads(result)) for s in _strings(p)]
    haystacks += [(f"message {i}", m) for i, m in enumerate(messages.items)]
    leaks = _find_leaks(_needles(installed), haystacks)
    assert not leaks, f"{label} leaks a Storage_Location: {'; '.join(leaks)}"


def test_leak_detector_flags_every_needle(leak: LeakFixture) -> None:
    """The detector is not vacuous: each needle, embedded in text, is reported by kind."""
    needles = _needles(leak)
    for kind, needle in needles:
        found = _find_leaks(needles, [("message 0", f"failed at {needle}/x.json")])
        assert f"{kind} in message 0" in found, (kind, needle)
    assert not _find_leaks(needles, [("result 0", "catalog.xml shared-constraints/a.ent")])


def _cited_files(value: object) -> Iterator[str]:
    """Every ``source-file`` and ``file`` string value inside a JSON-like value."""
    if isinstance(value, Mapping):
        for key, item in value.items():
            if key in ("source-file", "file") and isinstance(item, str):
                yield item
            else:
                yield from _cited_files(item)
    elif isinstance(value, list | tuple):
        for item in value:
            yield from _cited_files(item)


@pytest.mark.parametrize("tool", METASCHEMA_TOOLS)
def test_metaschema_source_files_are_manifest_members(
    tool: str,
    installed: LeakFixture,
    server: MCPServer,
    messages: Messages,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Requirements 3.5, 5.5: cited files are Metaschema_Source_Files, not paths."""
    allowed = set(get_index().source_files())
    cited_any = False
    for index, call in enumerate(CALLS[tool]):
        if call.kind != "ok":
            continue
        label = _label(tool, index, call)
        result, _ = _run(tool, call, installed, server, monkeypatch)
        assert not result.is_error, f"{label}: expected success, got {_error_text(result)}"
        cited = {f for p in _payloads(result) for f in _cited_files(p)}
        cited_any = cited_any or bool(cited)
        unknown = sorted(cited - allowed)
        assert not unknown, f"{label}: not Metaschema_Source_Files: {unknown}"
    assert cited_any, f"tool {tool!r}: no 'ok' call returned a source-file or file value"
