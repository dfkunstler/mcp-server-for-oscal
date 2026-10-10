"""Guard test: every Python version declaration site matches the supported range.

Supported CPython versions are 3.13 (minimum) and 3.14 (default dev interpreter).
This module checks the machine-readable version sites (package metadata, hatch,
ruff, the lock header, the MCPB bundle, the AgentCore image, mise, CI workflows),
scans the text sites and docs for stale 3.11/3.12 claims, and checks that the docs
state the current versions with the exact phrases listed in REQUIRED_PHRASES.

It uses only the standard library: the hatch-test env has no PyYAML, so workflows
are checked by regex rather than parsed as YAML.

REQUIRED_PHRASES (each file must contain every listed substring, case-sensitive):

    README.md                          "Python 3.13 or higher"
                                       "uv python install 3.14"
                                       "3.13 & 3.14"
    CONTRIBUTING.md                    "pytest on Python 3.13 and 3.14"
    conf/powers/oscal/POWER.md         "Python 3.13 or higher"
    .kiro/steering/tech.md             "Python 3.13+"
                                       "tested on 3.13 and 3.14"
                                       "default dev environment is 3.14"
                                       "Target: Python 3.13"
    .kiro/steering/hatch.md            "hatch test -py 3.14"
                                       "matrix: 3.13, 3.14"
                                       "(Python 3.14"
    AGENTS.md                          "(3.13 + 3.14)"
                                       "on Python 3.14"
    .agents/summary/codebase_info.md   ">=3.13"
                                       "3.13 and 3.14"
                                       "default dev env 3.14"
                                       "pins 3.14"
    .agents/summary/dependencies.md    "pins python 3.14"
                                       "--python-version 3.13"
    .agents/summary/index.md           "Python 3.13+"
    .agents/summary/workflows.md       "pytest matrix 3.13/3.14"
                                       "py3.13"
"""

import json
import re
import tomllib
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[1]

SUPPORTED = ("3.13", "3.14")
MINIMUM = "3.13"
DEFAULT = "3.14"

DOCS_SET = (
    "README.md",
    "CONTRIBUTING.md",
    "conf/powers/oscal/POWER.md",
    ".kiro/steering/tech.md",
    ".kiro/steering/hatch.md",
)

# Text Version_Sites. requirements.txt is deliberately excluded from the stale scan:
# its package pins may contain 3.11/3.12 as non-Python versions. test_lock_header
# covers it instead.
TEXT_VERSION_SITES = (
    "pyproject.toml",
    "conf/mcpb/pyproject.toml",
    "conf/mcpb/manifest.json",
    "conf/agentcore/Dockerfile",
    "mise.toml",
)


def _workflow_files() -> list[Path]:
    return sorted((ROOT / ".github" / "workflows").glob("*.yml"))


def _stale_scan_files() -> list[Path]:
    files = [ROOT / p for p in (*TEXT_VERSION_SITES, *DOCS_SET, "AGENTS.md")]
    files += _workflow_files()
    files += sorted((ROOT / ".agents" / "summary").glob("*.md"))
    return files


# A bare-number match: 3.11 or 3.12 not preceded by a digit or dot (so 0.3.12 and
# 13.12 don't match) and not followed by a digit (so 3.120 doesn't), plus ruff's
# py311/py312 form.
STALE = re.compile(r"(?<![\d.])3\.1[12](?!\d)|\bpy31[12]\b")

# (repo-relative path, line substring) pairs for legitimate non-Python-version hits.
ALLOWED_STALE: set[tuple[str, str]] = set()

REQUIRED_PHRASES: dict[str, list[str]] = {
    "README.md": ["Python 3.13 or higher", "uv python install 3.14", "3.13 & 3.14"],
    "CONTRIBUTING.md": ["pytest on Python 3.13 and 3.14"],
    "conf/powers/oscal/POWER.md": ["Python 3.13 or higher"],
    ".kiro/steering/tech.md": [
        "Python 3.13+",
        "tested on 3.13 and 3.14",
        "default dev environment is 3.14",
        "Target: Python 3.13",
    ],
    ".kiro/steering/hatch.md": ["hatch test -py 3.14", "matrix: 3.13, 3.14", "(Python 3.14"],
    "AGENTS.md": ["(3.13 + 3.14)", "on Python 3.14"],
    ".agents/summary/codebase_info.md": [
        ">=3.13",
        "3.13 and 3.14",
        "default dev env 3.14",
        "pins 3.14",
    ],
    ".agents/summary/dependencies.md": ["pins python 3.14", "--python-version 3.13"],
    ".agents/summary/index.md": ["Python 3.13+"],
    ".agents/summary/workflows.md": ["pytest matrix 3.13/3.14", "py3.13"],
}


def _read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def _toml(rel: str) -> dict[str, Any]:
    return tomllib.loads(_read(rel))


@pytest.fixture(scope="module")
def pyproject() -> dict[str, Any]:
    return _toml("pyproject.toml")


class TestRootPyproject:
    """Root pyproject.toml (Req 1.1-1.3, 2.1, 2.2, 3.1, 4.1, 11.1, 11.2)."""

    def test_requires_python(self, pyproject):
        assert pyproject["project"]["requires-python"] == f">={MINIMUM}"

    def test_classifiers(self, pyproject):
        pattern = re.compile(r"^Programming Language :: Python :: (3(?:\.\d+)?)$")
        versions = {
            m.group(1) for c in pyproject["project"]["classifiers"] if (m := pattern.match(c))
        }
        assert versions == {"3", *SUPPORTED}

    def test_default_env_python(self, pyproject):
        assert pyproject["tool"]["hatch"]["envs"]["default"]["python"] == DEFAULT

    def test_hatch_test_matrix(self, pyproject):
        matrix = pyproject["tool"]["hatch"]["envs"]["hatch-test"]["matrix"]
        pythons = [entry["python"] for entry in matrix if "python" in entry]
        assert pythons == [list(SUPPORTED)]

    def test_ruff_target_version(self, pyproject):
        assert pyproject["tool"]["ruff"]["target-version"] == "py" + MINIMUM.replace(".", "")

    def test_update_script_python_version(self, pyproject):
        script = pyproject["tool"]["hatch"]["envs"]["default"]["scripts"]["update"]
        text = " ".join(script) if isinstance(script, list) else script
        assert f"--python-version {MINIMUM}" in text


def test_lock_header():
    """requirements.txt is a universal lock for the minimum version (Req 4.2)."""
    header = "\n".join(_read("requirements.txt").splitlines()[:5])
    assert "--universal" in header
    assert f"--python-version {MINIMUM}" in header


class TestMcpbBundle:
    """conf/mcpb/* (Req 5.2-5.4)."""

    def test_pyproject_requires_python(self):
        assert _toml("conf/mcpb/pyproject.toml")["project"]["requires-python"] == f">={MINIMUM}"

    def test_manifest(self):
        manifest = json.loads(_read("conf/mcpb/manifest.json"))
        assert manifest["compatibility"]["runtimes"]["python"] == f">={MINIMUM}"
        assert manifest["manifest_version"] == "0.4"
        assert manifest["server"]["type"] == "uv"


def test_agentcore_dockerfile_base_image():
    """AgentCore image uses the default dev version (Req 5.1)."""
    from_lines = [
        line.split()[1]
        for line in _read("conf/agentcore/Dockerfile").splitlines()
        if line.strip().upper().startswith("FROM ")
    ]
    assert from_lines == [f"public.ecr.aws/docker/library/python:{DEFAULT}-slim"]


def test_mise_python():
    assert _toml("mise.toml")["tools"]["python"] == DEFAULT


# --- CI workflows -----------------------------------------------------------------

_PY_VERSION_KEY = re.compile(r"^(?P<indent>\s*)(?:-\s+)?python-version:\s*(?P<value>.*?)\s*$")


def _strip_comment_and_quotes(value: str) -> str:
    value = re.sub(r"\s+#.*$", "", value).strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        value = value[1:-1]
    return value


def workflow_python_versions(text: str) -> list[tuple[int, str]]:
    """Return (line number, literal version) for every `python-version:` value.

    Handles quoted and bare scalars and `|`/`>` block scalars (one version per
    line). Values that are GitHub expressions (`${{ ... }}`) are skipped.
    """
    lines = text.splitlines()
    found: list[tuple[int, str]] = []
    i = 0
    while i < len(lines):
        m = _PY_VERSION_KEY.match(lines[i])
        i += 1
        if not m:
            continue
        value = _strip_comment_and_quotes(m.group("value"))
        if value[:1] in ("|", ">"):
            indent = len(m.group("indent"))
            while i < len(lines):
                line = lines[i]
                if line.strip() and len(line) - len(line.lstrip()) <= indent:
                    break
                item = _strip_comment_and_quotes(line)
                if item and "${{" not in item:
                    found.append((i + 1, item))
                i += 1
        elif value and "${{" not in value:
            found.append((i, value))
    return found


class TestWorkflowParser:
    def test_forms(self):
        text = (
            "steps:\n"
            "  - uses: actions/setup-python@v6\n"
            "    with:\n"
            '      python-version: "3.14"\n'
            "  - with:\n"
            "      python-version: '3.13'  # comment\n"
            "  - with:\n"
            "      python-version: ${{ matrix.python }}\n"
            "  - with:\n"
            "      python-version: |\n"
            "        3.13\n"
            "        3.14\n"
            "      cache: pip\n"
            "  - with:\n"
            "      python-version: 3.12\n"
        )
        assert workflow_python_versions(text) == [
            (4, "3.14"),
            (6, "3.13"),
            (11, "3.13"),
            (12, "3.14"),
            (15, "3.12"),
        ]


def test_workflow_python_versions_supported():
    """Every literal setup-python version in CI is supported (Req 7.1, 7.2, 11.2)."""
    bad = []
    by_file: dict[str, set[str]] = {}
    for path in _workflow_files():
        rel = path.relative_to(ROOT).as_posix()
        versions = workflow_python_versions(path.read_text(encoding="utf-8"))
        by_file[rel] = {v for _, v in versions}
        bad += [f"{rel}:{n}: {v}" for n, v in versions if v not in SUPPORTED]
    assert not bad, "Unsupported python-version values:\n" + "\n".join(bad)
    for rel in (".github/workflows/build.yml", ".github/workflows/update-awesome-oscal.yml"):
        assert DEFAULT in by_file.get(rel, set()), f"{rel} does not install Python {DEFAULT}"


def test_no_prerelease_interpreters():
    """No 3.15, 3.15t, or 3.16 interpreter in hatch or CI (Req 11.1, 11.2)."""
    prerelease = re.compile(r"(?<![\d.])3\.1[5-9](?!\d)")
    hits = []
    for path in [ROOT / "pyproject.toml", *_workflow_files()]:
        rel = path.relative_to(ROOT).as_posix()
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if prerelease.search(line):
                hits.append(f"{rel}:{n}: {line.strip()}")
    assert not hits, "Pre-release interpreters found:\n" + "\n".join(hits)


# --- Stale-claim scan (Req 6.8) ---------------------------------------------------


class TestStaleRegex:
    @pytest.mark.parametrize(
        "text",
        [
            'requires-python = ">=3.11"',
            "Programming Language :: Python :: 3.12",
            'python = "3.12"',
            'target-version = "py311"',
            "--python-version 3.11",
            'python = ["3.11", "3.12"]',
            "FROM public.ecr.aws/docker/library/python:3.12-slim",
            'python-version: "3.12"',
            "Python 3.11 or higher",
            "uv install python 3.12",
            "we only test 3.11 & 3.12 for now",
            "pytest on Python 3.11 and 3.12",
            "Python 3.11+ (tested on 3.11 and 3.12, default dev environment is 3.12)",
            "Target: Python 3.11",
            "hatch test -py 3.12 tests/...",
            "matrix: 3.11, 3.12",
            "(3.11 + 3.12)",
            "runs `hatch run release` on Python 3.12",
            "`>=3.11`; CI matrix 3.11 and 3.12",
            "`mise.toml` pins 3.12",
            "pins python 3.12, uv, hatch 1.18.1",
            "pytest matrix 3.11/3.12",
            "(universal, py3.11)",
        ],
    )
    def test_matches_stale(self, text):
        assert STALE.search(text)

    @pytest.mark.parametrize(
        "text", ["0.3.12", "3.13", "3.14", "13.12", "py313", "3.120", "hatch 1.18.1"]
    )
    def test_ignores_non_stale(self, text):
        assert not STALE.search(text)


def test_no_stale_python_versions():
    hits = []
    for path in _stale_scan_files():
        rel = path.relative_to(ROOT).as_posix()
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if not STALE.search(line):
                continue
            if any(p == rel and s in line for p, s in ALLOWED_STALE):
                continue
            hits.append(f"{rel}:{n}: {line.strip()}")
    assert not hits, "Stale Python 3.11/3.12 references:\n" + "\n".join(hits)


# --- Required phrases (Req 6.1-6.7) -----------------------------------------------


def test_required_phrases_cover_doc_sets():
    agent_docs = {
        "AGENTS.md",
        ".agents/summary/codebase_info.md",
        ".agents/summary/dependencies.md",
        ".agents/summary/index.md",
        ".agents/summary/workflows.md",
    }
    assert set(REQUIRED_PHRASES) == set(DOCS_SET) | agent_docs


@pytest.mark.parametrize("rel", sorted(REQUIRED_PHRASES))
def test_required_phrases(rel):
    text = _read(rel)
    missing = [p for p in REQUIRED_PHRASES[rel] if p not in text]
    assert not missing, f"{rel} is missing: {missing}"
