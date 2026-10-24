"""Guard tests for the bundled OSCAL Metaschema sources (``oscal_metaschema/``).

Covers metaschema-constraints Requirements 1.1-1.5, 1.7, 2.1, 2.2, 2.6, and 3.6-3.8.
Each structural check is a helper that takes a bundle directory, so the failing
cases are exercised against small bundles built in ``tmp_path``.

Hidden OS files (e.g. ``.DS_Store``) get no exemption here. ``verify_package_integrity``
and ``bin/update_hashes.py`` don't skip them either, so a stray one would fail the
startup check; these tests fail the same way and name the file.
"""

import json
import re
import shutil
import subprocess
from pathlib import Path
from xml.parsers import expat

import pytest

from mcp_server_for_oscal.tools.metaschema_index import read_schema_versions
from mcp_server_for_oscal.tools.utils import get_bundled_oscal_version, verify_package_integrity

REPO_ROOT = Path(__file__).resolve().parent.parent
BUNDLE = REPO_ROOT / "src" / "mcp_server_for_oscal" / "oscal_metaschema"
SHARED = "shared-constraints"
MANIFEST = "hashes.json"
README = "README.md"
UPDATE_SCRIPT = REPO_ROOT / "bin" / "update-oscal-schemas.sh"

EXPECTED_MODULES = 14
EXPECTED_ENTITIES = 12

MODULE_RE = re.compile(r"oscal_[^/]+_metaschema\.xml")
ENTITY_FILE_RE = re.compile(r"[^/]+\.ent")
COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
ENTITY_DECL_RE = re.compile(r"<!ENTITY\s+([\w.-]+)\s+SYSTEM\s+[\"']([^\"']*)[\"']\s*>")
METASCHEMA_NS = "http://csrc.nist.gov/ns/oscal/metaschema/1.0"
# Expat names with namespace_separator=" "; also accept a module without the namespace.
METASCHEMA_IMPORT_NAMES = frozenset({f"{METASCHEMA_NS} import", "import"})
SHA_RE = re.compile(r"[0-9a-f]{40}")


# ---------------------------------------------------------------------------
# Helpers (each returns a list of problems; empty means the check passes)
# ---------------------------------------------------------------------------


def module_paths(root: Path) -> list[Path]:
    return sorted(p for p in root.iterdir() if p.is_file() and MODULE_RE.fullmatch(p.name))


def file_list_problems(
    root: Path,
    expected_modules: int = EXPECTED_MODULES,
    expected_entities: int = EXPECTED_ENTITIES,
) -> list[str]:
    """Check the bundle holds only modules, ``.ent`` files, README, and two manifests."""
    problems: list[str] = []
    shared = root / SHARED
    modules = entities = 0

    for path in sorted(root.iterdir()):
        if path.is_dir():
            if path.name != SHARED:
                problems.append(f"unexpected directory: {path.name}")
        elif MODULE_RE.fullmatch(path.name):
            modules += 1
        elif path.name not in {README, MANIFEST}:
            problems.append(f"unexpected file: {path.name}")

    if not shared.is_dir():
        problems.append(f"missing directory: {SHARED}")
    else:
        for path in sorted(shared.iterdir()):
            rel = f"{SHARED}/{path.name}"
            if path.is_dir():
                problems.append(f"unexpected directory: {rel}")
            elif ENTITY_FILE_RE.fullmatch(path.name):
                entities += 1
            elif path.name != MANIFEST:
                problems.append(f"unexpected file: {rel}")
        if not (shared / MANIFEST).is_file():
            problems.append(f"missing file: {SHARED}/{MANIFEST}")

    for name in (README, MANIFEST):
        if not (root / name).is_file():
            problems.append(f"missing file: {name}")
    if modules != expected_modules:
        problems.append(f"expected {expected_modules} modules, found {modules}")
    if entities != expected_entities:
        problems.append(f"expected {expected_entities} .ent files, found {entities}")
    return problems


def _resolves_inside(root: Path, base: Path, ref: str) -> bool:
    if not ref or "://" in ref or ref.startswith(("/", "\\")) or re.match(r"[A-Za-z]:", ref):
        return False
    target = (base / ref).resolve()
    return target.is_relative_to(root.resolve()) and target.is_file()


def metaschema_imports(data: bytes) -> list[str]:
    """Return the ``href`` of every Metaschema-namespace ``<import>`` in a module.

    A regex would also match ``<import>`` elements in other namespaces, such as the
    OSCAL profile example inside ``oscal_profile_metaschema.xml``'s ``<example>``.
    Parsed like ``read_schema_versions``: no external entity handler, no DTD loading,
    so no file or network location is opened.
    """
    parser = expat.ParserCreate(namespace_separator=" ")
    parser.SetParamEntityParsing(expat.XML_PARAM_ENTITY_PARSING_NEVER)
    hrefs: list[str] = []

    def start(name: str, attrs: dict[str, str]) -> None:
        if name in METASCHEMA_IMPORT_NAMES:
            hrefs.append(attrs.get("href", ""))

    parser.StartElementHandler = start
    parser.Parse(data, True)
    return hrefs


def reference_problems(root: Path) -> list[str]:
    """Check every entity ``SYSTEM`` id and ``<import href>`` resolves to a bundle file.

    Entity ids resolve against the declaring module's directory and imports against
    the bundle root (the modules' directory). Comments are ignored: stripped before the
    entity regex runs, and skipped by the import parser.
    """
    problems: list[str] = []
    for module in module_paths(root):
        data = module.read_bytes()
        text = COMMENT_RE.sub("", data.decode("utf-8"))
        for name, system_id in ENTITY_DECL_RE.findall(text):
            if not _resolves_inside(root, module.parent, system_id):
                problems.append(f"{module.name}: entity {name} SYSTEM {system_id!r} unresolved")
        for href in metaschema_imports(data):
            if not _resolves_inside(root, root, href):
                problems.append(f"{module.name}: import href {href!r} unresolved")
    return problems


def schema_version_problems(root: Path, expected: str) -> list[str]:
    """Check every module declares exactly one ``<schema-version>`` equal to ``expected``."""
    modules = module_paths(root)
    if not modules:
        return [f"no oscal_*_metaschema.xml modules in {root.name}"]
    problems: list[str] = []
    for module in modules:
        versions = read_schema_versions(module.read_bytes())
        if versions != [expected]:
            problems.append(f"{module.name}: declares {versions!r}, expected [{expected!r}]")
    return problems


def manifest_problems(directory: Path) -> list[str]:
    """Check ``hashes.json`` lists exactly the regular files in its directory."""
    manifest = json.loads((directory / MANIFEST).read_text(encoding="utf-8"))
    listed = set(manifest["file_hashes"])
    present = {p.name for p in directory.iterdir() if p.is_file() and p.name != MANIFEST}
    problems = [f"{directory.name}: unlisted file {n}" for n in sorted(present - listed)]
    problems += [f"{directory.name}: listed but missing {n}" for n in sorted(listed - present)]
    return problems


def readme_field(name: str) -> str:
    match = re.search(
        rf"^{re.escape(name)}:\s*(\S+)\s*$",
        (BUNDLE / README).read_text(encoding="utf-8"),
        re.MULTILINE,
    )
    assert match, f"{name}: line not found in {README}"
    return match.group(1)


def _make_bundle(root: Path, modules: dict[str, str], entities: tuple[str, ...] = ()) -> Path:
    """Write a minimal bundle; module values are the full XML text."""
    (root / SHARED).mkdir(parents=True)
    for name, text in modules.items():
        (root / name).write_text(text, encoding="utf-8")
    for name in entities:
        (root / SHARED / name).write_text("<x/>", encoding="utf-8")
    for name in (README, MANIFEST):
        (root / name).write_text("{}", encoding="utf-8")
    (root / SHARED / MANIFEST).write_text("{}", encoding="utf-8")
    return root


def _module_xml(*versions: str, body: str = "") -> str:
    tags = "".join(f"<schema-version>{v}</schema-version>" for v in versions)
    return (
        '<METASCHEMA xmlns="http://csrc.nist.gov/ns/oscal/metaschema/1.0">'
        f"{tags}{body}</METASCHEMA>"
    )


# ---------------------------------------------------------------------------
# Bundle contents and references (1.1, 1.2, 1.3, 1.7)
# ---------------------------------------------------------------------------


class TestBundleFileList:
    def test_bundle_holds_exactly_the_expected_files(self):
        assert file_list_problems(BUNDLE) == []

    def test_every_entity_and_import_resolves(self):
        assert reference_problems(BUNDLE) == []

    def test_bundle_declares_entities_and_imports(self):
        """Guard the regexes: the real bundle must yield references to check."""
        texts = [COMMENT_RE.sub("", m.read_text(encoding="utf-8")) for m in module_paths(BUNDLE)]
        system_ids = {sid for t in texts for _, sid in ENTITY_DECL_RE.findall(t)}
        hrefs = {h for m in module_paths(BUNDLE) for h in metaschema_imports(m.read_bytes())}
        resolved = {(BUNDLE / s).resolve().name for s in system_ids}
        entity_files = {p.name for p in (BUNDLE / SHARED).glob("*.ent")}
        assert resolved == entity_files
        assert hrefs
        assert "catalog.xml" not in hrefs  # example-namespace <import> in the profile module

    def test_unlisted_file_is_named(self, tmp_path):
        root = _make_bundle(tmp_path / "b", {"oscal_a_metaschema.xml": _module_xml("1")})
        (root / ".DS_Store").write_bytes(b"\0")
        (root / SHARED / "notes.txt").write_text("x", encoding="utf-8")
        problems = file_list_problems(root, expected_modules=1, expected_entities=0)
        assert "unexpected file: .DS_Store" in problems
        assert f"unexpected file: {SHARED}/notes.txt" in problems

    def test_wrong_counts_and_missing_parts_fail(self, tmp_path):
        root = _make_bundle(tmp_path / "b", {}, entities=("a.ent",))
        (root / README).unlink()
        (root / "extra").mkdir()
        problems = file_list_problems(root)
        assert f"missing file: {README}" in problems
        assert "unexpected directory: extra" in problems
        assert f"expected {EXPECTED_MODULES} modules, found 0" in problems
        assert f"expected {EXPECTED_ENTITIES} .ent files, found 1" in problems

    @pytest.mark.parametrize(
        ("decl", "fragment"),
        [
            ('<!ENTITY e SYSTEM "shared-constraints/missing.ent">', "entity e"),
            ('<!ENTITY e SYSTEM "../outside.ent">', "entity e"),
            ('<!ENTITY e SYSTEM "https://example.com/a.ent">', "entity e"),
            ('<!ENTITY e SYSTEM "/etc/passwd">', "entity e"),
        ],
    )
    def test_unresolved_entity_is_named(self, tmp_path, decl, fragment):
        (tmp_path / "outside.ent").write_text("<x/>", encoding="utf-8")
        text = f"<!DOCTYPE METASCHEMA [{decl}]>" + _module_xml("1")
        root = _make_bundle(tmp_path / "b", {"oscal_a_metaschema.xml": text})
        problems = reference_problems(root)
        assert len(problems) == 1
        assert problems[0].startswith("oscal_a_metaschema.xml:")
        assert fragment in problems[0]

    def test_unresolved_import_is_named_and_comments_ignored(self, tmp_path):
        body = (
            '<import href="oscal_b_metaschema.xml"/>'
            '<!-- <import href="oscal_gone_metaschema.xml"/> -->'
            '<import href="oscal_missing_metaschema.xml"/>'
            '<import xmlns="http://csrc.nist.gov/ns/oscal/example" href="catalog.xml"/>'
        )
        root = _make_bundle(
            tmp_path / "b",
            {
                "oscal_a_metaschema.xml": _module_xml("1", body=body),
                "oscal_b_metaschema.xml": _module_xml("1"),
            },
        )
        assert reference_problems(root) == [
            "oscal_a_metaschema.xml: import href 'oscal_missing_metaschema.xml' unresolved"
        ]


# ---------------------------------------------------------------------------
# Provenance and version match (1.4, 3.6, 3.7, 3.8)
# ---------------------------------------------------------------------------


class TestBundleVersion:
    def test_readme_states_provenance(self):
        assert readme_field("Source repository") == "usnistgov/OSCAL"
        assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", readme_field("Retrieved"))

    def test_readme_tag_matches_bundled_version(self):
        assert readme_field("Tag") == f"v{get_bundled_oscal_version()}"

    def test_release_commit_matches_readme(self):
        match = re.search(
            r'^CURRENT_RELEASE_COMMIT="([^"]*)"', UPDATE_SCRIPT.read_text(), re.MULTILINE
        )
        assert match, "CURRENT_RELEASE_COMMIT not found in update script"
        commit = match.group(1)
        assert SHA_RE.fullmatch(commit), f"not 40 lowercase hex characters: {commit!r}"
        assert readme_field("Commit") == commit

    def test_every_module_declares_bundled_version(self):
        assert schema_version_problems(BUNDLE, get_bundled_oscal_version()) == []

    def test_zero_modules_fails(self, tmp_path):
        root = _make_bundle(tmp_path / "b", {})
        assert schema_version_problems(root, "1.2.3") == ["no oscal_*_metaschema.xml modules in b"]

    def test_offenders_named_with_their_versions(self, tmp_path):
        root = _make_bundle(
            tmp_path / "b",
            {
                "oscal_ok_metaschema.xml": _module_xml("1.2.3"),
                "oscal_old_metaschema.xml": _module_xml("1.1.0"),
                "oscal_none_metaschema.xml": _module_xml(),
                "oscal_two_metaschema.xml": _module_xml("1.2.3", "1.2.3"),
            },
        )
        problems = schema_version_problems(root, "1.2.3")
        assert len(problems) == 3
        joined = "\n".join(problems)
        assert "oscal_old_metaschema.xml: declares ['1.1.0']" in joined
        assert "oscal_none_metaschema.xml: declares []" in joined
        assert "oscal_two_metaschema.xml: declares ['1.2.3', '1.2.3']" in joined
        assert "oscal_ok_metaschema.xml" not in joined


# ---------------------------------------------------------------------------
# Integrity manifests, rehash, and line endings (1.5, 2.1, 2.2, 2.6)
# ---------------------------------------------------------------------------


class TestBundleIntegrity:
    @pytest.mark.parametrize("directory", [BUNDLE, BUNDLE / SHARED], ids=["top", SHARED])
    def test_manifest_lists_exactly_its_files(self, directory):
        assert manifest_problems(directory) == []

    def test_manifest_problems_detects_drift(self, tmp_path):
        directory = tmp_path / "d"
        directory.mkdir()
        (directory / "a.ent").write_text("x", encoding="utf-8")
        (directory / MANIFEST).write_text(
            json.dumps({"commit": "", "file_hashes": {"b.ent": "0"}}), encoding="utf-8"
        )
        assert manifest_problems(directory) == [
            "d: unlisted file a.ent",
            "d: listed but missing b.ent",
        ]

    def test_verify_package_integrity_passes_on_bundle(self, tmp_path):
        verify_package_integrity(BUNDLE)
        # Same result on a copy, as the startup check would see it elsewhere.
        copy = tmp_path / "oscal_metaschema"
        shutil.copytree(BUNDLE, copy)
        verify_package_integrity(copy)

    def test_rehash_covers_both_manifests(self):
        text = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
        rel = "src/mcp_server_for_oscal/oscal_metaschema"
        for line in (
            f'"hatch run bin/update_hashes.py {rel}"',
            f'"git add {rel}/hashes.json"',
            f'"git add {rel}/{SHARED}/hashes.json"',
        ):
            assert line in text, f"rehash script line missing: {line}"

    @pytest.mark.parametrize(
        "rel",
        [
            "oscal_catalog_metaschema.xml",
            f"{SHARED}/allowed-values-component-type.ent",
            README,
        ],
    )
    def test_git_does_not_convert_line_endings(self, rel):
        git = shutil.which("git")
        if git is None:
            pytest.skip("git is not available")
        path = (BUNDLE / rel).relative_to(REPO_ROOT).as_posix()
        result = subprocess.run(
            [git, "check-attr", "text", "--", path],
            capture_output=True,
            text=True,
            cwd=REPO_ROOT,
            check=False,
        )
        if result.returncode != 0:
            pytest.skip(f"git check-attr failed: {result.stderr.strip()}")
        assert result.stdout.strip() == f"{path}: text: unset"
