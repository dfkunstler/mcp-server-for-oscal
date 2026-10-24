"""
Documentation and data-access guards for the Metaschema tools.

Feature: metaschema-constraints (#20)

- Every registered Metaschema_Tool is named in each doc that enumerates MCP tools
  (Requirement 7.5).
- Every Metaschema_Tool module reads Definitions and Constraints only through the
  shared ``metaschema_index`` module and never opens files itself (Requirement 7.1).
"""

import ast
import importlib
import inspect
from pathlib import Path
from types import ModuleType

import pytest

from mcp_server_for_oscal.tools import get_tool_list

REPO_ROOT = Path(__file__).resolve().parent.parent

# Metaschema_Tool names from the requirements glossary. Only the registered subset
# is checked, so later tools are covered as soon as they join get_tool_list().
METASCHEMA_TOOL_NAMES = frozenset(
    {
        "get_allowed_values",
        "get_constraints",
        "find_references_to",
        "get_definition",
        "get_metaschema_source",
    }
)

DOC_PATHS = (
    REPO_ROOT / "src" / "mcp_server_for_oscal" / "tools" / "README.md",
    REPO_ROOT / "README.md",
    REPO_ROOT / "AGENTS.md",
    REPO_ROOT / "conf" / "powers" / "oscal" / "POWER.md",
)

INDEX_MODULE_SUFFIX = "metaschema_index"
FORBIDDEN_FUNCS = frozenset({"open"})
FORBIDDEN_ATTRS = frozenset({"open", "read_text", "read_bytes"})


def _registered_metaschema_tools() -> dict[str, ModuleType]:
    """Map each registered Metaschema_Tool name to the module that defines it."""
    found: dict[str, ModuleType] = {}
    for tool_fn in get_tool_list():
        name = tool_fn.__name__
        if name in METASCHEMA_TOOL_NAMES:
            # Strands DecoratedFunctionTool keeps the wrapped function's __module__.
            found[name] = importlib.import_module(tool_fn.__module__)
    return found


def _imports_index(tree: ast.AST) -> bool:
    """True if the module imports from (or imports) a ``metaschema_index`` module."""
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.ImportFrom)
            and node.module
            and node.module.split(".")[-1] == INDEX_MODULE_SUFFIX
        ):
            return True
        if isinstance(node, ast.Import) and any(
            a.name.split(".")[-1] == INDEX_MODULE_SUFFIX for a in node.names
        ):
            return True
    return False


def _forbidden_calls(tree: ast.AST) -> list[str]:
    """Return a description of each file-reading call (open, x.open, read_text, read_bytes)."""
    hits: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name) and func.id in FORBIDDEN_FUNCS:
            hits.append(f"line {node.lineno}: {func.id}()")
        elif isinstance(func, ast.Attribute) and func.attr in FORBIDDEN_ATTRS:
            hits.append(f"line {node.lineno}: .{func.attr}()")
    return hits


REGISTERED = _registered_metaschema_tools()


@pytest.mark.unit
def test_at_least_one_metaschema_tool_registered():
    """Guard: the parametrized checks below must not pass vacuously."""
    assert REGISTERED, "no Metaschema_Tool found in get_tool_list()"
    assert "get_allowed_values" in REGISTERED


@pytest.mark.unit
@pytest.mark.parametrize("doc_path", DOC_PATHS, ids=lambda p: str(p.relative_to(REPO_ROOT)))
@pytest.mark.parametrize("tool_name", sorted(REGISTERED))
def test_metaschema_tool_named_in_docs(tool_name: str, doc_path: Path):
    text = doc_path.read_text(encoding="utf-8")
    assert f"`{tool_name}`" in text, (
        f"{doc_path.relative_to(REPO_ROOT)} does not name Metaschema tool `{tool_name}`"
    )


@pytest.mark.unit
@pytest.mark.parametrize("tool_name", sorted(REGISTERED))
def test_metaschema_tool_module_uses_index_only(tool_name: str):
    module = REGISTERED[tool_name]
    source_file = inspect.getsourcefile(module)
    assert source_file is not None
    tree = ast.parse(Path(source_file).read_text(encoding="utf-8"), filename=source_file)
    assert _imports_index(tree), f"{module.__name__} does not import metaschema_index"
    assert _forbidden_calls(tree) == [], f"{module.__name__} reads files directly"


@pytest.mark.unit
@pytest.mark.parametrize(
    "source",
    [
        "with open('x.xml') as f:\n    pass\n",
        "import io\nio.open('x.xml')\n",
        "from pathlib import Path\nPath('x').open()\n",
        "from pathlib import Path\nPath('x').read_text()\n",
        "from pathlib import Path\nPath('x').read_bytes()\n",
    ],
)
def test_checker_flags_file_reads(source: str):
    """Negative self-test: the AST checker detects each forbidden call form."""
    assert _forbidden_calls(ast.parse(source))


@pytest.mark.unit
def test_checker_accepts_index_import_without_file_reads():
    tree = ast.parse(
        "from mcp_server_for_oscal.tools.metaschema_index import get_index\nget_index()\n"
    )
    assert _imports_index(tree)
    assert _forbidden_calls(tree) == []
    assert not _imports_index(ast.parse("import json\n"))
