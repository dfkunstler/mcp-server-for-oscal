"""Guard tests for the 29 Removed_Tools of the consolidate-tools feature (GitHub issue #40).

Feature: consolidate-tools.

**Property 11: Removed names appear nowhere.** For every name in ``REMOVED_TOOLS``, the name is
absent from the Tool_Registry, the live ``tools/list``, the Agent tool specs, the module
attributes, every tool and parameter description, the server instructions, and every
Sync_Targets file. The Release_Note (``docs/releases/v0.8.0.md``), this file, and
``.agents/summary/review_notes.md`` are excluded because they legitimately name the old tools.

**Validates: Requirements 8.1, 8.2, 8.3, 8.4, 8.5, 8.6, 14.1, 14.2, 14.4**

**Property 12: The Mapping_Table is exactly the removal set.** The Mapping_Table of
``docs/releases/v0.8.0.md`` has one row per Removed_Tool, each replacement is a registry tool,
each argument string is ``render_args`` of the expected replacement, each argument value is in
the replacement's live ``inputSchema`` ``enum``, and the note records the facts (key renames,
behavior changes, sizes, versions) that the Budget_Module constants hold. Mutation tests run the
checker over modified copies of the note and require it to reject them.

**Validates: Requirements 7.6, 7.7, 12.1, 15.1, 15.2, 15.3, 15.4, 15.5, 15.6, 15.7**

Names are matched as whole identifiers: a match must not be preceded or followed by a letter,
digit, or underscore, so a removed name is never found inside a longer, legitimate identifier.
"""

import asyncio
import importlib.util
import json
import re
from collections.abc import Callable, Iterator, Mapping, Sequence
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from mcp_server_for_oscal import main, oscal_agent, tools
from mcp_server_for_oscal.tools import get_tool_list, tool_budget
from mcp_server_for_oscal.tools import query_component_definition as qcd
from mcp_server_for_oscal.tools import query_oscal_models as qom

from .removed_tools import GENERIC_TOOLS, REMOVED_TOOLS, RETAINED_TOOLS, render_args

REPO_ROOT = Path(__file__).resolve().parent.parent

# Sync_Targets (requirements Glossary) plus the other `.agents/summary` pages. The Release_Note,
# this test file, and `.agents/summary/review_notes.md` are deliberately absent.
SYNC_TARGET_FILES = (
    "bin/build_mcpb.py",
    "conf/mcpb/manifest.json",
    "src/mcp_server_for_oscal/tools/README.md",
    "conf/powers/oscal/POWER.md",
    "README.md",
    "AGENTS.md",
    ".kiro/steering/product.md",
    ".kiro/steering/structure.md",
    ".kiro/steering/tool-authoring.md",
    ".agents/summary/architecture.md",
    ".agents/summary/codebase_info.md",
    ".agents/summary/components.md",
    ".agents/summary/data_models.md",
    ".agents/summary/dependencies.md",
    ".agents/summary/index.md",
    ".agents/summary/interfaces.md",
    ".agents/summary/workflows.md",
    "bin/eval_tool_selection_prompts.json",
    "tests/test_tool_registry.py",
    "tests/tools/test_query_oscal_models_child_elements.py",
    "tests/test_tool_param_parity.py",
)
EVAL_PROMPTS = REPO_ROOT / "bin" / "eval_tool_selection_prompts.json"
BUILD_MCPB_PATH = REPO_ROOT / "bin" / "build_mcpb.py"

_IDENTIFIER_CHARS = "A-Za-z0-9_"


def find_name(name: str, text: str) -> list[int]:
    """Return the line numbers (1-based) where ``name`` occurs as a whole identifier."""
    pattern = re.compile(rf"(?<![{_IDENTIFIER_CHARS}]){re.escape(name)}(?![{_IDENTIFIER_CHARS}])")
    return [i for i, line in enumerate(text.splitlines(), start=1) if pattern.search(line)]


def _strings(value: Any) -> Iterator[str]:
    """Yield every string (keys and values) in a JSON-like structure."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, item in value.items():
            yield str(key)
            yield from _strings(item)
    elif isinstance(value, list | tuple):
        for item in value:
            yield from _strings(item)


def _joined(value: Any) -> str:
    return "\n".join(_strings(value))


@pytest.fixture(scope="module")
def live_tools() -> list[dict[str, Any]]:
    """Live ``tools/list`` entries from a server built by the production path."""
    server = main.build_server()
    main._setup_tools(server)
    listed = asyncio.run(server.list_tools())
    return [t.model_dump(by_alias=True, exclude_none=True, mode="json") for t in listed]


@pytest.fixture(scope="module")
def searchable_texts(live_tools: list[dict[str, Any]]) -> dict[str, str]:
    """Map a location label to the text in which no Removed_Tool name may appear."""
    registry = get_tool_list()
    texts: dict[str, str] = {
        "get_tool_list() names": "\n".join(fn.__name__ for fn in registry),
        "tools/list (names, descriptions, input schemas)": _joined(live_tools),
        "agent_tools() specs": _joined([t.tool_spec for t in oscal_agent.agent_tools(registry)]),
        "tool docstrings": "\n".join(fn.__doc__ or "" for fn in registry),
        "server instructions": main.build_server().instructions or "",
    }
    for module in (qom, qcd, tools, main):
        texts[f"attributes of {module.__name__}"] = "\n".join(dir(module))
    for rel in SYNC_TARGET_FILES:
        path = REPO_ROOT / rel
        assert path.is_file(), f"Sync_Target {rel} does not exist or is not a file"
        texts[rel] = path.read_text(encoding="utf-8")
    return texts


@pytest.mark.unit
class TestRemovedToolSet:
    def test_there_are_29_removed_tools(self):
        assert len(REMOVED_TOOLS) == 29

    def test_registry_equals_generic_plus_retained(self):
        names = [fn.__name__ for fn in get_tool_list()]
        assert len(names) == len(set(names))
        assert set(names) == GENERIC_TOOLS | RETAINED_TOOLS

    def test_live_tools_list_is_registry_plus_about(self, live_tools: list[dict[str, Any]]):
        live = {t["name"] for t in live_tools}
        assert live == GENERIC_TOOLS | RETAINED_TOOLS | {"about"}

    def test_agent_tools_are_the_registry(self):
        agent_names = {t.tool_name for t in oscal_agent.agent_tools(get_tool_list())}
        assert agent_names == GENERIC_TOOLS | RETAINED_TOOLS

    def test_replacements_are_registry_tools(self):
        assert {r.tool for r in REMOVED_TOOLS.values()} <= GENERIC_TOOLS

    def test_removed_names_are_disjoint_from_kept_names(self):
        assert not set(REMOVED_TOOLS) & (GENERIC_TOOLS | RETAINED_TOOLS | {"about"})

    @pytest.mark.parametrize("removed", sorted(REMOVED_TOOLS))
    def test_removed_name_is_not_inside_a_kept_name(self, removed: str):
        """Whole-identifier matching must not confuse a removed name with a kept tool."""
        for kept in GENERIC_TOOLS | RETAINED_TOOLS | {"about"}:
            assert removed not in kept, f"{removed} is a substring of {kept}"
            assert kept not in removed, f"{kept} is a substring of {removed}"

    def test_removed_name_matcher_is_exact(self):
        assert find_name("list_components", "use list_components now") == [1]
        assert find_name("list_components", "`list_components`\nx") == [1]
        assert find_name("list_components", "list_components_x") == []
        assert find_name("list_components", "my_list_components") == []
        assert find_name("list_documents", "list_catalogs") == []


@pytest.mark.unit
class TestRemovedNamesAppearNowhere:
    """Property 11: Removed names appear nowhere."""

    @pytest.mark.parametrize("removed", sorted(REMOVED_TOOLS))
    def test_absent_from_registry_and_attributes(self, removed: str):
        assert removed not in {fn.__name__ for fn in get_tool_list()}
        for module in (qom, qcd, tools, main):
            assert not hasattr(module, removed), f"{module.__name__}.{removed} still exists"

    @pytest.mark.parametrize("removed", sorted(REMOVED_TOOLS))
    def test_absent_from_live_agent_description_and_doc_locations(
        self, removed: str, searchable_texts: dict[str, str]
    ):
        hits = {
            label: find_name(removed, text)
            for label, text in searchable_texts.items()
            if find_name(removed, text)
        }
        assert not hits, f"{removed} found (location: line numbers): {hits}"

    def test_every_sync_target_was_read(self, searchable_texts: dict[str, str]):
        for rel in SYNC_TARGET_FILES:
            assert searchable_texts[rel].strip(), f"{rel} is empty"


def _eval_names() -> list[tuple[str, str, str]]:
    entries = json.loads(EVAL_PROMPTS.read_text(encoding="utf-8"))
    return [
        (entry["id"], field, name)
        for entry in entries
        for field in ("expect", "ok")
        for name in entry.get(field, [])
    ]


@pytest.mark.unit
class TestEvalPrompts:
    def test_eval_file_has_names(self):
        assert _eval_names()

    @pytest.mark.parametrize(("prompt_id", "field", "name"), _eval_names())
    def test_every_eval_name_is_a_registry_tool_or_about(
        self, prompt_id: str, field: str, name: str
    ):
        valid = {fn.__name__ for fn in get_tool_list()} | {"about"}
        assert name in valid, f"{prompt_id}.{field}: {name} is not a tool"
        assert name not in REMOVED_TOOLS

    def test_expect_is_never_empty(self):
        entries = json.loads(EVAL_PROMPTS.read_text(encoding="utf-8"))
        assert all(entry["expect"] for entry in entries)


@pytest.fixture(scope="module")
def build_mcpb() -> ModuleType:
    """Load ``bin/build_mcpb.py`` as a module; its ``main()`` is behind a ``__main__`` guard."""
    spec = importlib.util.spec_from_file_location("_build_mcpb_removed_check", BUILD_MCPB_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.unit
class TestMcpbManifestTools:
    def test_manifest_lists_exactly_the_registry_tools(self, build_mcpb: ModuleType):
        names = [entry["name"] for entry in build_mcpb.tool_list()]
        assert len(names) == len(set(names))
        assert set(names) == GENERIC_TOOLS | RETAINED_TOOLS | {"about"}

    def test_manifest_has_no_removed_name(self, build_mcpb: ModuleType):
        text = _joined(build_mcpb.tool_list())
        for removed in REMOVED_TOOLS:
            assert not find_name(removed, text), removed


# ---------------------------------------------------------------------------------------------
# Mapping_Table and Release_Note (Property 12)
# ---------------------------------------------------------------------------------------------

RELEASE_NOTE = REPO_ROOT / "docs" / "releases" / "v0.8.0.md"
TOOL_BUDGET_SOURCE = Path(tool_budget.__file__)

_TABLE_HEADING = "### Mapping table"
_TABLE_ROW = re.compile(r"^\|\s*`([^`]+)`\s*\|\s*`([^`]+)`\s*\|\s*`([^`]*)`\s*\|\s*$")
_ARG = re.compile(r'(\w+)="([^"]*)"')

# Result key renames of the three Component Definition listers (Requirement 7.6).
KEY_RENAMES = (
    ("componentCount", "childCount"),
    ("uuid", "id"),
    ("name", "title"),
    ("parentComponentDefinitionTitle", "parentDocumentTitle"),
    ("parentComponentDefinitionUuid", "parentDocumentUuid"),
)
BEHAVIOR_PHRASES = ("importedComponentDefinitionsCount", "Components plus Capabilities")
NO_ALIAS_STATEMENT = "No aliases and no compatibility flag exist"


def mapping_table_rows(note: str) -> tuple[list[tuple[str, str, str]], list[str]]:
    """Return ``(rows, problems)`` for the Mapping_Table section of the Release_Note.

    A row is ``(old name, replacement, arguments)`` from the backticked cells. A table line that
    does not have the fixed three-cell format is reported as a problem, never skipped.
    """
    lines = note.splitlines()
    if _TABLE_HEADING not in lines:
        return [], [f"heading {_TABLE_HEADING!r} is missing"]
    section: list[str] = []
    for line in lines[lines.index(_TABLE_HEADING) + 1 :]:
        if line.startswith("#"):
            break
        section.append(line)
    table_lines = [line for line in section if line.startswith("|")]
    # The first two table lines are the header and the separator.
    body = table_lines[2:]
    rows: list[tuple[str, str, str]] = []
    problems: list[str] = []
    for line in body:
        match = _TABLE_ROW.match(line)
        if match is None:
            problems.append(f"malformed row: {line!r}")
        else:
            rows.append((match.group(1), match.group(2), match.group(3)))
    return rows, problems


def _schema_enum(prop: Mapping[str, Any]) -> list[str] | None:
    """Return the string ``enum`` of an input schema property (also inside ``anyOf``)."""
    if "enum" in prop:
        return list(prop["enum"])
    for branch in prop.get("anyOf", []):
        if "enum" in branch:
            return list(branch["enum"])
    return None


def mapping_table_problems(
    note: str, live_tools: Sequence[Mapping[str, Any]], registry: set[str]
) -> list[str]:
    """Return every way the Mapping_Table differs from the removal set (empty when it matches)."""
    rows, problems = mapping_table_rows(note)
    olds = [old for old, _, _ in rows]
    for old in sorted(set(olds)):
        if olds.count(old) > 1:
            problems.append(f"{old} appears {olds.count(old)} times")
    problems.extend(
        f"{old} is not a Removed_Tool" for old in sorted(set(olds) - set(REMOVED_TOOLS))
    )
    problems.extend(f"{old} has no row" for old in sorted(set(REMOVED_TOOLS) - set(olds)))
    if len(rows) != len(REMOVED_TOOLS):
        problems.append(f"{len(rows)} rows, expected {len(REMOVED_TOOLS)}")
    schemas = {t["name"]: t.get("inputSchema", {}) for t in live_tools}
    for old, replacement, args in rows:
        expected = REMOVED_TOOLS.get(old)
        if expected is None:
            continue
        if replacement not in registry:
            problems.append(f"{old}: replacement {replacement} is not a registry tool")
        elif replacement != expected.tool:
            problems.append(f"{old}: replacement {replacement}, expected {expected.tool}")
        if args != render_args(expected):
            problems.append(f"{old}: arguments {args!r}, expected {render_args(expected)!r}")
        properties = schemas.get(replacement, {}).get("properties", {})
        for name, value in _ARG.findall(args):
            if name not in properties:
                problems.append(f"{old}: {replacement} has no parameter {name}")
                continue
            allowed = _schema_enum(properties[name])
            if allowed is not None and value not in allowed:
                problems.append(f"{old}: {name}={value!r} is not in the {replacement} enum")
    return problems


def _budget_versions() -> tuple[set[str], set[str]]:
    """Return the Python and mcp versions that the Budget_Module comments record."""
    source = TOOL_BUDGET_SOURCE.read_text(encoding="utf-8")
    pairs = re.findall(r"Python\s+(\d+\.\d+\.\d+)\s+and\s+mcp\s+(\d+\.\d+\.\d+)", source)
    return {py for py, _ in pairs}, {mcp for _, mcp in pairs}


def release_note_problems(note: str) -> list[str]:
    """Return every required fact the Release_Note lacks (empty when complete)."""
    problems: list[str] = []
    if "0.8.0" not in note:
        problems.append("version 0.8.0 is missing")
    if "breaking" not in note.lower():
        problems.append("the word 'breaking' is missing")
    if not re.search(r"(?<![\w#])#40\b|\[#40\]", note):
        problems.append("the issue reference #40 is missing")
    problems.extend(
        f"key rename {old} -> {new} is missing"
        for old, new in KEY_RENAMES
        if f"`{old}` is now `{new}`" not in note
    )
    if NO_ALIAS_STATEMENT not in note:
        problems.append("the no-alias statement is missing")
    problems.extend(f"behavior change {p!r} is missing" for p in BEHAVIOR_PHRASES if p not in note)
    expected_facts = (
        f"Before_Size {tool_budget.BEFORE_CONSOLIDATION_SIZE:,}",
        f"After_Size {tool_budget.AFTER_CONSOLIDATION_SIZE:,}",
        f"{tool_budget.BEFORE_CONSOLIDATION_TOOLS} before, "
        f"{tool_budget.AFTER_CONSOLIDATION_TOOLS} after",
    )
    problems.extend(
        f"measurement {fact!r} is missing" for fact in expected_facts if fact not in note
    )
    pythons, mcps = _budget_versions()
    if not pythons or not mcps:
        problems.append("the Budget_Module records no Python or mcp version")
    problems.extend(f"Python {v} is missing" for v in sorted(pythons) if f"Python {v}" not in note)
    problems.extend(f"mcp {v} is missing" for v in sorted(mcps) if f"`mcp` {v}" not in note)
    return problems


@pytest.fixture(scope="module")
def release_note() -> str:
    assert RELEASE_NOTE.is_file(), "docs/releases/v0.8.0.md does not exist"
    return RELEASE_NOTE.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def registry_names() -> set[str]:
    return {fn.__name__ for fn in get_tool_list()}


def _replace_row(note: str, old: str, transform: Callable[[str], str]) -> str:
    """Return ``note`` with the Mapping_Table line of ``old`` rewritten by ``transform``."""
    prefix = f"| `{old}` |"
    lines = note.splitlines()
    index = next(i for i, line in enumerate(lines) if line.startswith(prefix))
    lines[index] = transform(lines[index])
    return "\n".join(lines) + "\n"


def _drop_row(note: str, old: str) -> str:
    prefix = f"| `{old}` |"
    return "\n".join(line for line in note.splitlines() if not line.startswith(prefix)) + "\n"


@pytest.mark.unit
class TestMappingTable:
    """Property 12: The Mapping_Table is exactly the removal set."""

    def test_table_has_29_rows(self, release_note: str):
        rows, problems = mapping_table_rows(release_note)
        assert not problems
        assert len(rows) == 29

    def test_each_removed_tool_appears_exactly_once(self, release_note: str):
        olds = [old for old, _, _ in mapping_table_rows(release_note)[0]]
        assert sorted(olds) == sorted(REMOVED_TOOLS)

    def test_each_replacement_is_a_registry_tool(self, release_note: str, registry_names: set[str]):
        for old, replacement, _ in mapping_table_rows(release_note)[0]:
            assert replacement in registry_names, old

    def test_each_argument_string_is_the_rendered_replacement(self, release_note: str):
        for old, _, args in mapping_table_rows(release_note)[0]:
            assert args == render_args(REMOVED_TOOLS[old]), old

    def test_each_argument_value_is_in_the_schema_enum(
        self, release_note: str, live_tools: list[dict[str, Any]]
    ):
        schemas = {t["name"]: t["inputSchema"]["properties"] for t in live_tools}
        checked = 0
        for old, replacement, args in mapping_table_rows(release_note)[0]:
            for name, value in _ARG.findall(args):
                enum = _schema_enum(schemas[replacement][name])
                assert enum is not None, f"{old}: {replacement}.{name} has no enum"
                assert value in enum, f"{old}: {value!r} not in {replacement}.{name}"
                checked += 1
        assert checked == 29

    def test_no_problems_overall(
        self, release_note: str, live_tools: list[dict[str, Any]], registry_names: set[str]
    ):
        assert mapping_table_problems(release_note, live_tools, registry_names) == []


@pytest.mark.unit
class TestMappingTableMutations:
    """The checker rejects a Mapping_Table that drifts from the removal set."""

    @pytest.mark.parametrize("old", sorted(REMOVED_TOOLS))
    def test_dropped_row_is_rejected(
        self,
        old: str,
        release_note: str,
        live_tools: list[dict[str, Any]],
        registry_names: set[str],
    ):
        mutated = _drop_row(release_note, old)
        assert mutated != release_note
        problems = mapping_table_problems(mutated, live_tools, registry_names)
        assert any(f"{old} has no row" in p for p in problems), problems

    @pytest.mark.parametrize("old", sorted(REMOVED_TOOLS))
    def test_non_registry_replacement_is_rejected(
        self,
        old: str,
        release_note: str,
        live_tools: list[dict[str, Any]],
        registry_names: set[str],
    ):
        replacement = REMOVED_TOOLS[old].tool
        mutated = _replace_row(
            release_note, old, lambda line: line.replace(f"`{replacement}`", "`list_catalogs`", 1)
        )
        problems = mapping_table_problems(mutated, live_tools, registry_names)
        assert any(
            f"{old}: replacement list_catalogs is not a registry tool" in p for p in problems
        )

    @pytest.mark.parametrize("old", sorted(REMOVED_TOOLS))
    def test_changed_argument_is_rejected(
        self,
        old: str,
        release_note: str,
        live_tools: list[dict[str, Any]],
        registry_names: set[str],
    ):
        args = render_args(REMOVED_TOOLS[old])
        mutated = _replace_row(
            release_note, old, lambda line: line.replace(args, args.replace('"', '"x', 1), 1)
        )
        problems = mapping_table_problems(mutated, live_tools, registry_names)
        assert any(p.startswith(f"{old}: arguments") for p in problems), problems
        assert any(p.startswith(f"{old}: ") and "enum" in p for p in problems), problems

    def test_swapped_replacement_tool_is_rejected(
        self, release_note: str, live_tools: list[dict[str, Any]], registry_names: set[str]
    ):
        mutated = _replace_row(
            release_note,
            "query_catalog",
            lambda line: line.replace("`query_documents`", "`list_child_elements`", 1),
        )
        problems = mapping_table_problems(mutated, live_tools, registry_names)
        assert any("query_catalog: replacement list_child_elements" in p for p in problems)
        assert any("has no parameter model_type" in p for p in problems)

    def test_duplicate_and_unknown_rows_are_rejected(
        self, release_note: str, live_tools: list[dict[str, Any]], registry_names: set[str]
    ):
        row = next(
            line for line in release_note.splitlines() if line.startswith("| `query_catalog` |")
        )
        duplicated = release_note.replace(row, f"{row}\n{row}", 1)
        problems = mapping_table_problems(duplicated, live_tools, registry_names)
        assert any("query_catalog appears 2 times" in p for p in problems)
        assert any("30 rows" in p for p in problems)
        unknown = release_note.replace(row, row.replace("`query_catalog`", "`query_widgets`", 1), 1)
        problems = mapping_table_problems(unknown, live_tools, registry_names)
        assert any("query_widgets is not a Removed_Tool" in p for p in problems)
        assert any("query_catalog has no row" in p for p in problems)

    def test_malformed_row_is_reported_not_skipped(
        self, release_note: str, live_tools: list[dict[str, Any]], registry_names: set[str]
    ):
        mutated = _replace_row(
            release_note,
            "query_ssp",
            lambda line: line.replace("`query_documents`", "query_documents"),
        )
        problems = mapping_table_problems(mutated, live_tools, registry_names)
        assert any(p.startswith("malformed row") for p in problems)

    def test_missing_heading_is_rejected(
        self, release_note: str, live_tools: list[dict[str, Any]], registry_names: set[str]
    ):
        mutated = release_note.replace(_TABLE_HEADING, "### Table")
        assert mapping_table_problems(mutated, live_tools, registry_names)


@pytest.mark.unit
class TestReleaseNote:
    """Property 12 (Release_Note facts): the note is complete and matches the Budget_Module."""

    def test_note_is_complete(self, release_note: str):
        assert release_note_problems(release_note) == []

    def test_budget_constants_are_the_recorded_figures(self):
        assert tool_budget.BEFORE_CONSOLIDATION_TOOLS > tool_budget.AFTER_CONSOLIDATION_TOOLS
        assert tool_budget.BEFORE_CONSOLIDATION_SIZE > tool_budget.AFTER_CONSOLIDATION_SIZE

    def test_budget_module_records_versions(self):
        pythons, mcps = _budget_versions()
        assert len(pythons) == 1
        assert len(mcps) == 1

    def test_missing_breaking_label_is_rejected(self, release_note: str):
        mutated = re.sub("(?i)breaking", "disruptive", release_note)
        assert mutated != release_note
        assert any("breaking" in p for p in release_note_problems(mutated))

    @pytest.mark.parametrize(
        ("original", "replacement", "expected"),
        [
            ("0.8.0", "0.9.9", "version 0.8.0"),
            ("[#40]", "[#41]", "#40"),
            ("`uuid` is now `id`", "`uuid` is `id`", "key rename uuid -> id"),
            (NO_ALIAS_STATEMENT, "Aliases exist", "no-alias"),
            (
                "importedComponentDefinitionsCount",
                "importedCount",
                "importedComponentDefinitionsCount",
            ),
            ("Components plus Capabilities", "Components", "Components plus Capabilities"),
            ("Before_Size 34,938", "Before_Size 55,685", "Before_Size"),
            ("After_Size 18,391", "After_Size 18,000", "After_Size"),
            ("46 before, 20 after", "45 before, 19 after", "before"),
            ("Python 3.14.8", "Python 3.14.0", "Python 3.14.8"),
            ("`mcp` 2.1.1", "`mcp` 2.0.0", "mcp 2.1.1"),
        ],
    )
    def test_missing_fact_is_rejected(
        self, release_note: str, original: str, replacement: str, expected: str
    ):
        assert original in release_note
        mutated = release_note.replace(original, replacement)
        problems = release_note_problems(mutated)
        assert any(expected in p for p in problems), problems
