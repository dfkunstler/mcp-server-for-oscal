"""Index of the bundled NIST OSCAL Metaschema sources.

All XML parsing in this module uses stdlib ``pyexpat`` directly. ``pyexpat`` performs no
I/O of its own: without an ``ExternalEntityRefHandler`` external entity references are
dropped, and with ``XML_PARAM_ENTITY_PARSING_NEVER`` no external DTD is ever loaded.

This module holds the data model of the index, the loader limits,
``resolve_bundle_path`` (the only way the loader turns a reference written in a module, an
entity ``SYSTEM`` identifier or an ``<import href>``, into a file to open), and the safe
pyexpat loader that turns one module into a ``_Node`` tree, the extraction of Definitions
and Constraints from those trees, the import graph, ``ModelScope``, ``MetaschemaIndex``, and
the thread-safe process-wide ``get_index()`` singleton.

Client-safe text (RC-13): every string stored in a record or carried by ``_Refused`` names
files only by bundle-relative POSIX path, module file name, or the reference as written in
the module, plus a fixed rule text. Absolute paths and OS exception text go to the server
log only.
"""

from __future__ import annotations

import json
import logging
import re
import threading
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Final, Literal, NamedTuple
from xml.parsers import expat

from mcp_server_for_oscal.tools.metaschema_paths import normalize_target
from mcp_server_for_oscal.tools.utils import OSCALModelType, schema_names

if TYPE_CHECKING:
    from collections.abc import Collection, Iterator, Mapping

    from mcp_server_for_oscal.tools.metaschema_paths import PathAlternative

logger = logging.getLogger(__name__)

METASCHEMA_NS: Final = "http://csrc.nist.gov/ns/oscal/metaschema/1.0"

BUNDLE_DIR: Final[Path] = Path(__file__).resolve().parent.parent / "oscal_metaschema"

# Loader resource limits (RC-10). Module constants so tests can lower them.
MAX_FILE_BYTES: Final = 2 * 1024 * 1024  # per-file read cap, checked with read(MAX + 1)
MAX_DEPTH: Final = 64  # element nesting cap, entity content counted at its reference point

CONSTRAINT_TYPES: Final[tuple[str, ...]] = (
    "allowed-values",
    "matches",
    "index",
    "index-has-key",
    "is-unique",
    "has-cardinality",
    "expect",
)

DefinitionKind = Literal["assembly", "field", "flag"]


class MetaschemaLoadError(ValueError):
    """The Metaschema bundle, or a module in the requested Model_Scope, failed to load.

    The message is client-safe (RC-13): it is built from ``LoadError`` fields only. The
    original exception, if any, is chained with ``raise ... from`` for server-side logging.
    """


def model_module(model: OSCALModelType) -> str:
    """Return the Metaschema module file name for a model type.

    Derived from ``utils.schema_names`` so the model-to-file mapping has one source, e.g.
    ``oscal_component_schema`` -> ``oscal_component_metaschema.xml``.
    """
    return schema_names[model].removesuffix("_schema") + "_metaschema.xml"


# --------------------------------------------------------------------------------------
# Data model. All records are frozen and slotted; collections are tuples and lookup maps
# are read-only (types.MappingProxyType), so the index is never mutated after load.
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SourceLocation:
    """Where an element's start tag was written."""

    source_file: str  # bundle-relative POSIX path, e.g. "shared-constraints/x.ent"
    line: int  # 1-based line of the start tag in that file


@dataclass(eq=False, slots=True)
class _Node:
    """Private parse-tree node for an element in the Metaschema namespace.

    Built by the loader; ``children`` holds child nodes and text in document order (entity
    content is attached at its reference point). Never mutated after the loader returns.
    """

    tag: str  # local name
    attrs: Mapping[str, str]
    source_file: str
    line: int
    children: list[_Node | str] = field(default_factory=list)


# --------------------------------------------------------------------------------------
# Markup rendering (RC-11, Requirements 6.7, 6.8, 10.4). Output is read by LLMs and may be
# shown as Markdown, so rendering keeps it structurally safe: C0 controls are removed,
# inline markup maps to a fixed Markdown subset, and links carry only safe schemes.
# --------------------------------------------------------------------------------------

# C0 controls other than tab (U+0009) and newline (U+000A).
_C0_CONTROLS: Final = re.compile("[\x00-\x08\x0b-\x1f]")

# Inline elements rendered as Markdown: tag -> (prefix, suffix).
_INLINE_MARKDOWN: Final[Mapping[str, tuple[str, str]]] = MappingProxyType(
    {
        "code": ("`", "`"),
        "q": ('"', '"'),
        "em": ("*", "*"),
        "strong": ("**", "**"),
    }
)

_SAFE_SCHEMES: Final = frozenset({"http", "https"})
_LINK_TEXT_SPECIALS: Final = re.compile(r"([\\\[\]])")
_HREF_ENCODED: Final = frozenset("()<>")

# Block-level elements of markup-multiline whose content is one inline block.
_LEAF_BLOCKS: Final = frozenset({"p", "h1", "h2", "h3", "h4", "h5", "h6", "pre"})
_LISTS: Final = frozenset({"ul", "ol"})
# Containers whose children are blocks (or inline runs between blocks).
_CONTAINER_BLOCKS: Final = frozenset({"blockquote", "table", "thead", "tbody", "tr", "li"})
_BLOCK_TAGS: Final = _LEAF_BLOCKS | _LISTS | _CONTAINER_BLOCKS | {"hr", "th", "td"}


def _strip_c0(text: str) -> str:
    """Remove C0 control characters except tab and newline (RC-11)."""
    return _C0_CONTROLS.sub("", text)


def _safe_href(href: str | None) -> str | None:
    """Return the Markdown-safe form of ``href``, or ``None`` if it must not be linked.

    Accepted: an absolute ``http``/``https`` URL (scheme case-insensitive), a fragment
    (``#...``), or a relative reference with no scheme (no ``:`` before the first ``/``,
    ``?``, or ``#``) that does not start with ``//``. In an accepted ``href``, ``(``, ``)``,
    ``<``, ``>``, and every character at or below U+0020 are percent-encoded. Other
    whitespace is encoded too, so whitespace collapse can never split the link target.
    """
    if not href:
        return None
    if not href.startswith("#"):
        if href.startswith("//"):
            return None
        end = min((i for i in (href.find(c) for c in "/?#") if i >= 0), default=len(href))
        scheme, sep, _ = href[:end].partition(":")
        if sep and scheme.lower() not in _SAFE_SCHEMES:
            return None
    return "".join(
        "".join(f"%{b:02X}" for b in c.encode())
        if c in _HREF_ENCODED or c <= " " or c.isspace()
        else c
        for c in href
    )


def _inline(parts: list[_Node | str]) -> str:
    """Render text and inline elements to Markdown, without whitespace handling."""
    out: list[str] = []
    for part in parts:
        if isinstance(part, str):
            out.append(_strip_c0(part))
            continue
        text = _inline(part.children)
        if part.tag in _INLINE_MARKDOWN:
            prefix, suffix = _INLINE_MARKDOWN[part.tag]
            out.append(f"{prefix}{text}{suffix}")
        elif part.tag == "a" and (href := _safe_href(part.attrs.get("href"))) is not None:
            escaped = _LINK_TEXT_SPECIALS.sub(r"\\\1", text)
            out.append(f"[{escaped}]({href})")
        else:
            out.append(text)
    return "".join(out)


def _collapse(text: str) -> str:
    return " ".join(text.split())


def _block_list(parts: list[_Node | str]) -> list[str]:
    """Render ``parts`` as blocks: paragraphs, headings, and list groups.

    Inline runs between block elements form their own block. Each list becomes one block of
    ``- `` lines; nested lists contribute their own ``- `` lines after the parent item.
    """
    blocks: list[str] = []
    run: list[_Node | str] = []

    def flush() -> None:
        if text := _collapse(_inline(run)):
            blocks.append(text)
        run.clear()

    for child in parts:
        if isinstance(child, str) or child.tag not in _BLOCK_TAGS:
            run.append(child)
            continue
        flush()
        if child.tag in _LISTS:
            if lines := _list_lines(child):
                blocks.append("\n".join(lines))
        elif child.tag in _CONTAINER_BLOCKS:
            blocks.extend(_block_list(child.children))
        elif text := _collapse(_inline(child.children)):  # leaf blocks, th, td
            blocks.append(text)
    flush()
    return blocks


def _list_lines(node: _Node) -> list[str]:
    """``- `` lines for the items of a ``ul``/``ol``, nested list items following their parent."""
    lines: list[str] = []
    for item in _nodes(node):
        if item.tag in _LISTS:
            lines.extend(_list_lines(item))
            continue
        parts = item.children if item.tag == "li" else [item]
        own = [p for p in parts if not (isinstance(p, _Node) and p.tag in _LISTS)]
        if text := " ".join(_block_list(own)):
            lines.append(f"- {text}")
        for nested in parts:
            if isinstance(nested, _Node) and nested.tag in _LISTS:
                lines.extend(_list_lines(nested))
    return lines


@dataclass(frozen=True, slots=True)
class Markup:
    """Captured ``markup-line`` / ``markup-multiline`` content, rendered on demand.

    ``node`` is the wrapper element (``description``, ``remarks``, ``message``, or ``enum``);
    rendering covers its children, not the wrapper tag.
    """

    node: _Node

    def plain(self) -> str:
        """Inline rendering (Requirement 6.7): Markdown inline markup, whitespace collapsed.

        Returns ``""`` for empty content (Requirement 6.8).
        """
        return _collapse(_inline(self.node.children))

    def blocks(self) -> str:
        """Block rendering for remarks (Requirement 10.4, RC-6).

        Paragraphs and other blocks are separated by ``\\n\\n``; each list item is one line
        starting with ``- ``. Text inside each block is rendered as in :meth:`plain`.
        """
        return "\n\n".join(_block_list(self.node.children))


@dataclass(frozen=True, slots=True)
class EnumValue:
    """One ``enum`` of an ``allowed-values`` constraint."""

    value: str
    description: Markup | None
    deprecated: str | None
    location: SourceLocation


@dataclass(frozen=True, slots=True)
class DefinitionKey:
    """Identifies a Definition; ``location`` is unique per Definition."""

    module: str
    kind: DefinitionKind
    name: str
    location: SourceLocation


@dataclass(frozen=True, slots=True)
class Constraint:
    """A constraint of one of ``CONSTRAINT_TYPES`` declared on a Definition."""

    type: str  # one of CONSTRAINT_TYPES
    id: str | None
    level: str  # "ERROR" if absent
    target: str  # verbatim; "." if absent
    alternatives: tuple[PathAlternative, ...]
    attrs: Mapping[str, str]  # allow-other, extensible, name, datatype, regex, test, ...
    message: Markup | None  # expect
    key_fields: tuple[str, ...]  # key-field/@target in document order
    enums: tuple[EnumValue, ...]  # allowed-values only
    definition_key: DefinitionKey  # declaring Definition
    location: SourceLocation


@dataclass(frozen=True, slots=True)
class FlagSlot:
    """A flag of a Definition: an inline ``define-flag`` or a ``flag ref``."""

    effective_name: str  # use-name, else name (inline) or ref
    inline: DefinitionKey | None  # inline define-flag
    ref: str | None  # flag ref target name, resolved in the Model_Scope at query time
    required: bool


@dataclass(frozen=True, slots=True)
class ModelChild:
    """A field or assembly in a Definition's ``model`` (reference or inline definition)."""

    kind: Literal["assembly", "field"]
    effective_name: str  # use-name, else ref (reference) or name (inline)
    ref: str | None  # referenced global Definition name
    inline: DefinitionKey | None  # inline define-assembly / define-field
    min_occurs: int  # Metaschema default 0
    max_occurs: str  # "1" by default, a number, or "unbounded"
    group_as: str | None
    in_choice: bool  # declared inside a choice
    location: SourceLocation


@dataclass(frozen=True, slots=True)
class Definition:
    """A ``define-assembly``, ``define-field``, or ``define-flag``."""

    key: DefinitionKey
    scope: Literal["global", "local"]
    parent: DefinitionKey | None
    formal_name: str | None
    description: Markup | None
    remarks: Markup | None
    use_name: str | None
    group_as: str | None
    flags: tuple[FlagSlot, ...]
    model_children: tuple[ModelChild, ...]  # used by get_definition (ext.)
    constraints: tuple[Constraint, ...]
    nested: tuple[DefinitionKey, ...]  # local definitions, for get_constraints bare mode


@dataclass(frozen=True, slots=True)
class LoadError:
    """A per-module load failure. Every field is client-safe text (RC-13)."""

    module: str  # module file name
    subject: str  # entity name, import href or SYSTEM id as written, file, or "xml"
    reason: str  # fixed rule text; never str(OSError) or a repr


# --------------------------------------------------------------------------------------
# Path resolution
# --------------------------------------------------------------------------------------


class _Refused(Exception):  # noqa: N818 - private signal, not an error type for callers
    """A reference, file, or construct was refused; nothing further is opened or parsed.

    ``reason`` is a fixed, client-safe rule text; it never contains a path. ``subject`` is
    the client-safe name of what was refused (entity name, reference as written, or
    bundle-relative file), when the raiser knows it; otherwise the caller pairs ``reason``
    with the reference as written to build a ``LoadError``.
    """

    def __init__(self, reason: str, subject: str | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.subject = subject


class BundlePath(NamedTuple):
    """A reference accepted by ``resolve_bundle_path``."""

    path: Path  # absolute, fully resolved path to open
    rel: str  # bundle-relative POSIX path (a manifest allowlist entry), for Source_Locations


# A URL scheme; also matches Windows drive letters ("C:", "c:foo").
_SCHEME_RE: Final = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")

REFUSED_NUL: Final = "reference contains a NUL character"
REFUSED_SCHEME: Final = "reference has a URL scheme or drive letter"
REFUSED_ABSOLUTE: Final = "reference is an absolute or UNC path"
REFUSED_UNRESOLVABLE: Final = "reference cannot be resolved"
REFUSED_OUTSIDE: Final = "reference resolves outside the Metaschema bundle"
REFUSED_MISSING: Final = "referenced file does not exist"
REFUSED_UNLISTED: Final = "referenced file is not listed in a bundle hashes.json manifest"


def resolve_bundle_path(
    base_dir: Path, ref: str, root: Path, allowlist: Collection[str]
) -> BundlePath:
    """Resolve a module reference to a manifest-listed file inside the bundle.

    ``ref`` is an entity ``SYSTEM`` identifier or ``<import href>`` as written, resolved
    relative to ``base_dir`` (the referencing module's directory). ``allowlist`` holds the
    bundle-relative POSIX paths listed in the bundle's ``hashes.json`` manifests (RC-5).

    Checks run in this order, and nothing is opened (only ``resolve`` and ``stat``):

    1. NUL byte; URL scheme or drive letter; leading ``/`` or ``\\`` (covers UNC paths).
    2. The fully resolved path (``..`` normalized, symlinks followed) must be inside
       ``root.resolve()``.
    3. The resolved path must exist (reported as a missing file, Requirement 4.6).
    4. Its bundle-relative POSIX form must be in ``allowlist``.

    Existence is checked before the allowlist so a reference to a deleted file is reported
    as missing rather than unlisted; both are refusals.

    Returns:
        ``BundlePath(path, rel)``: the resolved absolute path to open, and its
        bundle-relative POSIX form to use in Source_Locations and messages.

    Raises:
        _Refused: with a client-safe ``reason`` (one of the ``REFUSED_*`` texts).
    """
    if "\0" in ref:
        raise _Refused(REFUSED_NUL)
    if _SCHEME_RE.match(ref):
        raise _Refused(REFUSED_SCHEME)
    if ref.startswith(("/", "\\")):
        raise _Refused(REFUSED_ABSOLUTE)

    try:
        root_resolved = root.resolve()
        resolved = (base_dir / ref).resolve()
    except (OSError, RuntimeError, ValueError) as err:  # symlink loop, invalid name
        raise _Refused(REFUSED_UNRESOLVABLE) from err

    if not resolved.is_relative_to(root_resolved):
        raise _Refused(REFUSED_OUTSIDE)
    if not resolved.is_file():
        raise _Refused(REFUSED_MISSING)
    rel = resolved.relative_to(root_resolved).as_posix()
    if rel not in allowlist:
        raise _Refused(REFUSED_UNLISTED)
    return BundlePath(resolved, rel)


# pyexpat reports namespaced names as "<uri><sep><local>" when created with a separator.
_NS_SEP: Final = " "
_SCHEMA_VERSION_NAMES: Final = frozenset(
    {f"{METASCHEMA_NS}{_NS_SEP}schema-version", "schema-version"}
)


def read_schema_versions(data: bytes) -> list[str]:
    """Return the text of every ``<schema-version>`` element in a Metaschema module.

    Matches ``schema-version`` in the Metaschema namespace, and also in no namespace (a
    module that omits the namespace declaration). Elements with that local name in any
    other namespace are ignored. Values are whitespace-stripped and returned in document
    order; the caller decides whether the result is acceptable (exactly one, matching).

    The parse never opens a file or network location: no external entity handler is
    installed, so external entity references are skipped, and parameter entity parsing
    (external DTD loading) is disabled.

    Raises:
        xml.parsers.expat.ExpatError: if ``data`` is not well-formed XML.
    """
    parser = expat.ParserCreate(namespace_separator=_NS_SEP)
    parser.SetParamEntityParsing(expat.XML_PARAM_ENTITY_PARSING_NEVER)

    versions: list[str] = []
    depth = 0  # >0 while inside a schema-version element
    parts: list[str] = []

    def start(name: str, _attrs: dict[str, str]) -> None:
        nonlocal depth
        if depth:
            depth += 1
        elif name in _SCHEMA_VERSION_NAMES:
            depth = 1
            parts.clear()

    def end(_name: str) -> None:
        nonlocal depth
        if depth:
            depth -= 1
            if not depth:
                versions.append("".join(parts).strip())

    def chars(text: str) -> None:
        if depth:
            parts.append(text)

    parser.StartElementHandler = start
    parser.EndElementHandler = end
    parser.CharacterDataHandler = chars
    parser.Parse(data, True)
    return versions


# --------------------------------------------------------------------------------------
# Safe loader (RC-5, RC-10, RC-13)
#
# API for building the index (task 5.3):
#
#   allowlist = _read_allowlist(root)          # raises _Refused (bundle-level failure)
#   for module in _module_files(allowlist):
#       result = _load_module(root, module, allowlist)   # _Node | LoadError, never raises
#
# ``_load_module`` returns the ``METASCHEMA`` root ``_Node`` with external entity content
# attached at each reference point, or a client-safe ``LoadError`` (already logged with
# the original exception). Imports are left in the tree as ``import`` nodes; resolve each
# ``href`` with ``resolve_bundle_path(root, href, root, allowlist)``.
# --------------------------------------------------------------------------------------

MANIFEST: Final = "hashes.json"
SHARED_DIR: Final = "shared-constraints"

_MODULE_RE: Final = re.compile(r"^oscal_[A-Za-z0-9_-]+_metaschema\.xml$")
_METASCHEMA_PREFIX: Final = METASCHEMA_NS + _NS_SEP
_ROOT_TAG: Final = "METASCHEMA"

REFUSED_PARAM_ENTITY: Final = "parameter entities are not allowed"
REFUSED_ENTITY_IN_ENTITY: Final = "internal entity replacement text refers to another entity"
REFUSED_UNPARSED_ENTITY: Final = "unparsed entities are not allowed"
REFUSED_NESTED_EXTERNAL: Final = "an entity file must not reference an external entity"
REFUSED_UNDECLARED: Final = "entity is not declared in the module"
REFUSED_UNREADABLE: Final = "file cannot be read"
REFUSED_UNDECODABLE: Final = "file cannot be decoded"
REFUSED_NOT_METASCHEMA: Final = "root element is not METASCHEMA in the Metaschema namespace"
REFUSED_BAD_MANIFEST: Final = "manifest is not a valid hashes.json file"


def _too_large(limit: int) -> str:
    return f"file is larger than the limit of {limit} bytes"


def _too_deep(limit: int) -> str:
    return f"element nesting exceeds the limit of {limit}"


def _expat_reason(err: expat.ExpatError) -> str:
    """Client-safe text for a parse error: expat's fixed message plus position."""
    return f"{expat.ErrorString(err.code)}: line {err.lineno}, column {err.offset}"


def _read_bounded(path: Path, rel: str) -> bytes:
    """Read at most ``MAX_FILE_BYTES`` from ``path``; ``rel`` names it in refusals.

    Reads ``MAX_FILE_BYTES + 1`` bytes, so an oversized file is refused before any byte
    reaches a parser and without a separate ``stat`` (no check-then-use gap). The limit is
    read from the module global at call time, so tests can lower it.

    Raises:
        _Refused: subject ``rel``; the reason is the size limit, or ``OSError.strerror``
            (never ``str(OSError)``, which embeds the absolute path).
    """
    limit = MAX_FILE_BYTES
    try:
        with open(path, "rb") as f:
            data = f.read(limit + 1)
    except OSError as err:
        raise _Refused(err.strerror or REFUSED_UNREADABLE, rel) from err
    if len(data) > limit:
        raise _Refused(_too_large(limit), rel)
    return data


def _manifest_names(data: bytes) -> list[str]:
    """Return the ``file_hashes`` keys of a manifest written by ``bin/update_hashes.py``."""
    doc = json.loads(data)
    hashes = doc["file_hashes"]
    if not isinstance(hashes, dict):
        raise TypeError("file_hashes is not an object")
    names = list(hashes)
    for name in names:
        # update_hashes.py writes plain file names; anything else is not a manifest entry.
        if not isinstance(name, str) or name in {"", ".", ".."} or any(c in name for c in "/\\\0"):
            raise ValueError("file_hashes key is not a file name")
    return names


def _read_allowlist(root: Path) -> frozenset[str]:
    """Return the Manifest_Allowlist: bundle-relative POSIX paths the loader may open.

    The union of the ``file_hashes`` keys of ``root/hashes.json`` and of
    ``root/shared-constraints/hashes.json`` (keys prefixed with ``shared-constraints/``).
    Both manifests are required. The manifests themselves are not in the allowlist.

    Raises:
        _Refused: a manifest is missing, unreadable, too large, or malformed; ``subject``
            is the manifest's bundle-relative path. This is a bundle-level failure.
    """
    names: set[str] = set()
    for prefix in ("", f"{SHARED_DIR}/"):
        rel = f"{prefix}{MANIFEST}"
        data = _read_bounded(root / rel, rel)
        try:
            entries = _manifest_names(data)
        except (ValueError, KeyError, TypeError) as err:  # JSONDecodeError is a ValueError
            raise _Refused(REFUSED_BAD_MANIFEST, rel) from err
        names.update(prefix + name for name in entries)
    return frozenset(names)


def _module_files(allowlist: Collection[str]) -> tuple[str, ...]:
    """Return the allowlisted top-level ``oscal_*_metaschema.xml`` modules, sorted."""
    return tuple(sorted(rel for rel in allowlist if _MODULE_RE.match(rel)))


def _local_name(name: str) -> str | None:
    """Local name of a Metaschema-namespace or no-namespace element, else ``None``."""
    if name.startswith(_METASCHEMA_PREFIX):
        return name[len(_METASCHEMA_PREFIX) :]
    if _NS_SEP not in name:
        return name
    return None


class _TreeBuilder:
    """Parses one module and its external entities into a ``_Node`` tree.

    One instance per module load. The depth counter and node stack are shared by the
    module parser and every entity sub-parser, so entity content is nested (and counted
    against ``MAX_DEPTH``) at its reference point. Handlers are bound per parser so each
    reads its own ``CurrentLineNumber`` and records its own ``source_file``.
    """

    def __init__(self, root: Path, module: BundlePath, allowlist: Collection[str]) -> None:
        self._root = root
        self._module = module
        self._allowlist = allowlist
        self._entities: dict[str, str] = {}  # external entity name -> SYSTEM id as written
        self._files: list[str] = []  # files being parsed, module first
        self._depth = 0
        self._stack: list[_Node | None] = []  # None: element outside the Metaschema ns
        self._tree: _Node | None = None

    def parse(self, data: bytes) -> _Node:
        parser = expat.ParserCreate(namespace_separator=_NS_SEP)
        parser.SetParamEntityParsing(expat.XML_PARAM_ENTITY_PARSING_NEVER)
        parser.EntityDeclHandler = self._entity_decl
        parser.SkippedEntityHandler = self._skipped_entity
        self._bind(parser, self._module.rel)
        self._files.append(self._module.rel)
        parser.Parse(data, True)
        if self._tree is None or self._tree.tag != _ROOT_TAG:
            raise _Refused(REFUSED_NOT_METASCHEMA, self._module.rel)
        return self._tree

    def _bind(self, parser: expat.XMLParserType, rel: str) -> None:
        """Bind content handlers to ``parser`` (sub-parsers inherit the parent's copies)."""

        def start(name: str, attrs: dict[str, str]) -> None:
            self._depth += 1
            limit = MAX_DEPTH
            if self._depth > limit:
                raise _Refused(_too_deep(limit), rel)
            parent = self._stack[-1] if self._stack else None
            local = _local_name(name)
            if local is None or (self._stack and parent is None):
                self._stack.append(None)  # skip foreign-namespace subtree
                return
            node = _Node(
                tag=local,
                # Only no-namespace attributes; namespaced ones arrive as "<uri> <local>".
                attrs=MappingProxyType({k: v for k, v in attrs.items() if _NS_SEP not in k}),
                source_file=rel,
                line=parser.CurrentLineNumber,
            )
            if parent is None:
                self._tree = node
            else:
                parent.children.append(node)
            self._stack.append(node)

        def end(_name: str) -> None:
            self._depth -= 1
            self._stack.pop()

        def chars(text: str) -> None:
            node = self._stack[-1] if self._stack else None
            if node is None:
                return
            if node.children and isinstance(node.children[-1], str):
                node.children[-1] += text
            else:
                node.children.append(text)

        def external_ref(
            context: str | None, _base: str | None, system_id: str | None, _public: str | None
        ) -> int:
            return self._external_ref(parser, context, system_id or "")

        parser.StartElementHandler = start
        parser.EndElementHandler = end
        parser.CharacterDataHandler = chars
        parser.ExternalEntityRefHandler = external_ref

    def _entity_subject(self, system_id: str) -> str:
        """Entity name and SYSTEM id as written (Requirements 4.2, 4.6)."""
        names = [n for n, s in self._entities.items() if s == system_id]
        if len(names) == 1:
            return f'{names[0]} (SYSTEM "{system_id}")'
        return f'SYSTEM "{system_id}"'

    def _external_ref(self, parser: expat.XMLParserType, context: str | None, sid: str) -> int:
        if len(self._files) > 1:
            raise _Refused(REFUSED_NESTED_EXTERNAL, self._entity_subject(sid))
        try:
            # Entity SYSTEM ids resolve against the declaring module's directory.
            target = resolve_bundle_path(self._module.path.parent, sid, self._root, self._allowlist)
        except _Refused as err:
            raise _Refused(err.reason, self._entity_subject(sid)) from err
        data = _read_bounded(target.path, target.rel)
        sub = parser.ExternalEntityParserCreate(context)
        self._bind(sub, target.rel)
        self._files.append(target.rel)
        try:
            sub.Parse(data, True)
        except expat.ExpatError as err:
            raise _Refused(_expat_reason(err), target.rel) from err
        self._files.pop()
        return 1

    def _entity_decl(
        self,
        name: str,
        is_parameter_entity: int,
        value: str | None,
        _base: str | None,
        system_id: str | None,
        _public_id: str | None,
        notation_name: str | None,
    ) -> None:
        if is_parameter_entity:
            raise _Refused(REFUSED_PARAM_ENTITY, name)
        if notation_name is not None:
            raise _Refused(REFUSED_UNPARSED_ENTITY, name)
        if value is not None:
            # Character references are already expanded here, so "&#38;x;" shows as "&x;".
            if "&" in value:
                raise _Refused(REFUSED_ENTITY_IN_ENTITY, name)
            return
        if system_id is not None:
            self._entities.setdefault(name, system_id)  # XML: the first declaration wins

    def _skipped_entity(self, name: str, _is_parameter_entity: int) -> None:
        # expat skips undeclared entities when the DTD is incomplete (for example, an
        # external subset it never loads); refuse instead of silently dropping content.
        raise _Refused(REFUSED_UNDECLARED, name)


def _parse_module(root: Path, module_rel: str, allowlist: Collection[str]) -> _Node:
    """Parse one module into its ``METASCHEMA`` ``_Node`` tree.

    The module itself is resolved through ``resolve_bundle_path`` (so it must be in the
    allowlist and inside the bundle), read bounded, and parsed with ``pyexpat``:

    - Parameter entities, unparsed entities, undeclared (skipped) entities, and internal
      entities whose value contains ``&`` are refused.
    - An external entity is resolved with ``resolve_bundle_path`` against the module's
      directory, read bounded, and parsed by an ``ExternalEntityParserCreate`` sub-parser
      whose nodes carry the entity file's bundle-relative path and per-file line numbers.
      Its content is attached at the reference point. External references from inside an
      entity file are refused.
    - Element nesting, entity content included, is limited to ``MAX_DEPTH``.

    Only Metaschema-namespace (and no-namespace) elements become nodes; elements in other
    namespaces are skipped with their content. Comments and processing instructions are
    dropped. Text is merged into one string between adjacent elements.

    Raises:
        _Refused, expat.ExpatError, OSError, UnicodeDecodeError: see ``_load_module``.
    """
    try:
        module = resolve_bundle_path(root, module_rel, root, allowlist)
    except _Refused as err:
        raise _Refused(err.reason, module_rel) from err
    data = _read_bounded(module.path, module.rel)
    return _TreeBuilder(root, module, allowlist).parse(data)


def _failed(error: LoadError, exc: BaseException) -> LoadError:
    """Log ``exc`` with its traceback (server log only) and return ``error``."""
    logger.warning(
        "Metaschema module %s failed to load: %s: %s",
        error.module,
        error.subject,
        error.reason,
        exc_info=exc,
    )
    return error


def _load_module(root: Path, module_rel: str, allowlist: Collection[str]) -> _Node | LoadError:
    """Load one module; return its tree, or a client-safe ``LoadError`` (never raises).

    ``module_rel`` is the module's bundle-relative path (a top-level file name, as listed
    by ``_module_files``). On failure the ``LoadError`` names the module, a ``subject``
    (entity name with its SYSTEM id as written, the bundle-relative file where a limit or
    parse error was hit, or the module), and a fixed reason. The original exception, with
    absolute paths and traceback, goes to the server log only (RC-13).
    """
    try:
        return _parse_module(root, module_rel, allowlist)
    except _Refused as err:
        return _failed(LoadError(module_rel, err.subject or module_rel, err.reason), err)
    except expat.ExpatError as err:
        return _failed(LoadError(module_rel, module_rel, _expat_reason(err)), err)
    except OSError as err:
        reason = err.strerror or REFUSED_UNREADABLE
        return _failed(LoadError(module_rel, module_rel, reason), err)
    except UnicodeDecodeError as err:
        return _failed(LoadError(module_rel, module_rel, REFUSED_UNDECODABLE), err)


# --------------------------------------------------------------------------------------
# Extraction (Requirements 4.1, 4.5, 5.6)
#
# Walks a module's ``_Node`` tree. Entity content is already attached at its reference
# point, so document order after expansion is plain tree order, and every record takes its
# Source_Location from its own node (module or entity file, per-file line).
# --------------------------------------------------------------------------------------

_DEFINE_KINDS: Final[Mapping[str, DefinitionKind]] = MappingProxyType(
    {"define-assembly": "assembly", "define-field": "field", "define-flag": "flag"}
)
# Model entries: references (assembly/field ref) and inline local definitions.
_MODEL_KINDS: Final[Mapping[str, Literal["assembly", "field"]]] = MappingProxyType(
    {
        "assembly": "assembly",
        "field": "field",
        "define-assembly": "assembly",
        "define-field": "field",
    }
)
# Containers inside a ``model`` whose children are model entries.
_MODEL_CONTAINERS: Final = frozenset({"choice", "choice-group"})
# Constraint attributes held in dedicated ``Constraint`` fields rather than ``attrs``.
_CONSTRAINT_OWN_ATTRS: Final = frozenset({"id", "level", "target"})
_DIGITS_RE: Final = re.compile(r"[0-9]+")

REFUSED_BAD_OCCURS: Final = "min-occurs is not a non-negative integer"
REFUSED_NO_HREF: Final = "import has no href attribute"
REFUSED_NOT_MODULE: Final = "imported file is not a Metaschema module"
REFUSED_NO_MODULE: Final = "model module is not in the Metaschema bundle"


def _nodes(node: _Node, tag: str | None = None) -> Iterator[_Node]:
    """Element children of ``node`` in document order, optionally only those named ``tag``."""
    for child in node.children:
        if isinstance(child, _Node) and (tag is None or child.tag == tag):
            yield child


def _first(node: _Node, tag: str) -> _Node | None:
    return next(_nodes(node, tag), None)


def _text(node: _Node) -> str:
    """All text content of ``node`` and its descendants, in document order."""
    return "".join(c if isinstance(c, str) else _text(c) for c in node.children)


def _child_text(node: _Node, tag: str) -> str | None:
    """Whitespace-collapsed text of the first ``tag`` child, or ``None`` if there is none."""
    child = _first(node, tag)
    return None if child is None else " ".join(_text(child).split())


def _markup(node: _Node, tag: str) -> Markup | None:
    child = _first(node, tag)
    return None if child is None else Markup(child)


def _has_content(node: _Node) -> bool:
    return any(isinstance(c, _Node) or c.strip() for c in node.children)


def _location(node: _Node) -> SourceLocation:
    return SourceLocation(node.source_file, node.line)


def _group_as(node: _Node) -> str | None:
    group = _first(node, "group-as")
    return None if group is None else group.attrs.get("name")


def _required(node: _Node) -> bool:
    return node.attrs.get("required") == "yes"


def _min_occurs(node: _Node) -> int:
    raw = node.attrs.get("min-occurs")
    if raw is None:
        return 0  # Metaschema default
    if not _DIGITS_RE.fullmatch(raw.strip()):
        raise _Refused(REFUSED_BAD_OCCURS, f"{node.source_file} line {node.line}")
    return int(raw)


class _Extractor:
    """Builds the Definitions of one module, global and local, in document order."""

    def __init__(self, module: str) -> None:
        self._module = module
        # A slot is reserved before nested Definitions are built, so a parent precedes its
        # local Definitions and the result is in start-tag document order.
        self._slots: list[Definition | None] = []

    def run(self, tree: _Node) -> tuple[Definition, ...]:
        for child in _nodes(tree):
            if child.tag in _DEFINE_KINDS:
                self._definition(child, None)
        return tuple(d for d in self._slots if d is not None)

    def _definition(self, node: _Node, parent: DefinitionKey | None) -> DefinitionKey:
        key = DefinitionKey(
            module=self._module,
            kind=_DEFINE_KINDS[node.tag],
            name=node.attrs.get("name", ""),
            location=_location(node),
        )
        slot = len(self._slots)
        self._slots.append(None)

        flags: list[FlagSlot] = []
        children: list[ModelChild] = []
        constraints: list[Constraint] = []
        nested: list[DefinitionKey] = []
        for child in _nodes(node):
            if child.tag == "define-flag":
                inline = self._definition(child, key)
                nested.append(inline)
                name = _child_text(child, "use-name") or inline.name
                flags.append(FlagSlot(name, inline, None, _required(child)))
            elif child.tag == "flag":
                ref = child.attrs.get("ref", "")
                name = _child_text(child, "use-name") or ref
                flags.append(FlagSlot(name, None, ref, _required(child)))
            elif child.tag == "model":
                self._model(child, key, in_choice=False, children=children, nested=nested)
            elif child.tag == "constraint":
                constraints.extend(
                    self._constraint(c, key) for c in _nodes(child) if c.tag in CONSTRAINT_TYPES
                )

        scope: Literal["global", "local"] = "global" if parent is None else "local"
        self._slots[slot] = Definition(
            key=key,
            scope=scope,
            parent=parent,
            formal_name=_child_text(node, "formal-name"),
            description=_markup(node, "description"),
            remarks=_markup(node, "remarks"),
            use_name=_child_text(node, "use-name"),
            group_as=_group_as(node),
            flags=tuple(flags),
            model_children=tuple(children),
            constraints=tuple(constraints),
            nested=tuple(nested),
        )
        return key

    def _model(
        self,
        node: _Node,
        parent: DefinitionKey,
        *,
        in_choice: bool,
        children: list[ModelChild],
        nested: list[DefinitionKey],
    ) -> None:
        for child in _nodes(node):
            kind = _MODEL_KINDS.get(child.tag)
            if kind is not None:
                ref: str | None = None
                inline: DefinitionKey | None = None
                if child.tag in _DEFINE_KINDS:
                    inline = self._definition(child, parent)
                    nested.append(inline)
                    default_name = inline.name
                else:
                    ref = child.attrs.get("ref", "")
                    default_name = ref
                children.append(
                    ModelChild(
                        kind=kind,
                        effective_name=_child_text(child, "use-name") or default_name,
                        ref=ref,
                        inline=inline,
                        min_occurs=_min_occurs(child),
                        max_occurs=child.attrs.get("max-occurs", "1"),
                        group_as=_group_as(child),
                        in_choice=in_choice,
                        location=_location(child),
                    )
                )
            elif child.tag in _MODEL_CONTAINERS:
                self._model(
                    child,
                    parent,
                    in_choice=in_choice or child.tag == "choice",
                    children=children,
                    nested=nested,
                )

    @staticmethod
    def _constraint(node: _Node, owner: DefinitionKey) -> Constraint:
        target = node.attrs.get("target")
        enums: tuple[EnumValue, ...] = ()
        if node.tag == "allowed-values":
            enums = tuple(
                EnumValue(
                    value=e.attrs.get("value", ""),
                    description=Markup(e) if _has_content(e) else None,
                    deprecated=e.attrs.get("deprecated"),
                    location=_location(e),
                )
                for e in _nodes(node, "enum")
            )
        return Constraint(
            type=node.tag,
            id=node.attrs.get("id"),
            level=node.attrs.get("level", "ERROR"),
            target="." if target is None else target,
            alternatives=normalize_target(target),
            attrs=MappingProxyType(
                {k: v for k, v in node.attrs.items() if k not in _CONSTRAINT_OWN_ATTRS}
            ),
            message=_markup(node, "message"),
            key_fields=tuple(
                k.attrs["target"] for k in _nodes(node, "key-field") if "target" in k.attrs
            ),
            enums=enums,
            definition_key=owner,
            location=_location(node),
        )


# --------------------------------------------------------------------------------------
# Import graph and Model_Scope (Requirements 4.4, 4.7, 4.8)
# --------------------------------------------------------------------------------------


def _logged(error: LoadError) -> LoadError:
    logger.warning(
        "Metaschema module %s failed to load: %s: %s", error.module, error.subject, error.reason
    )
    return error


def _imports(
    root: Path, module: str, tree: _Node, allowlist: Collection[str], modules: Collection[str]
) -> tuple[tuple[str, ...], tuple[LoadError, ...]]:
    """Resolve the ``import`` elements of a module tree.

    Each ``href`` is resolved relative to the bundle root through ``resolve_bundle_path``
    (nothing is opened here). Returns the imported modules (distinct, document order) and a
    ``LoadError`` on ``module`` for every import that is refused or is not a module; the
    error's ``subject`` is the ``href`` as written.
    """
    found: list[str] = []
    errors: list[LoadError] = []
    for node in _nodes(tree, "import"):
        href = node.attrs.get("href")
        if href is None:
            errors.append(_logged(LoadError(module, "import", REFUSED_NO_HREF)))
            continue
        try:
            target = resolve_bundle_path(root, href, root, allowlist)
        except _Refused as err:
            errors.append(_failed(LoadError(module, href, err.reason), err))
            continue
        if target.rel not in modules:
            errors.append(_logged(LoadError(module, href, REFUSED_NOT_MODULE)))
        elif target.rel not in found:
            found.append(target.rel)
    return tuple(found), tuple(errors)


def _closure(start: str, imports: Mapping[str, tuple[str, ...]]) -> tuple[str, ...]:
    """Modules reachable from ``start`` through imports, each once, in BFS order."""
    order = [start]
    seen = {start}
    queue = deque(order)
    while queue:
        for nxt in imports.get(queue.popleft(), ()):
            if nxt not in seen:
                seen.add(nxt)
                order.append(nxt)
                queue.append(nxt)
    return tuple(order)


@dataclass(frozen=True, slots=True, eq=False)
class ModelScope:
    """The Definitions visible from one model's module: its import closure.

    Built only for scopes without load errors. Lookups preserve scope order: modules in BFS
    order from the model module, Definitions in document order within each module.
    """

    modules: tuple[str, ...]  # each once, BFS order
    _by_name: Mapping[str, tuple[Definition, ...]] = field(repr=False)
    _globals: Mapping[tuple[str, str], tuple[Definition, ...]] = field(repr=False)
    _names: tuple[str, ...] = field(repr=False)

    def definitions(self, name: str) -> tuple[Definition, ...]:
        """Every Definition named ``name`` in the scope, global and local (Requirement 5.6)."""
        return self._by_name.get(name, ())

    def definition_names(self) -> tuple[str, ...]:
        """The distinct Definition names in the scope, sorted."""
        return self._names

    def global_definition(self, kind: str, name: str) -> tuple[Definition, ...]:
        """The global (top-level) Definitions of ``kind`` named ``name``, e.g. a ``flag ref``."""
        return self._globals.get((kind, name), ())


# A scope is either built, or the load errors of the modules it reaches (scope order).
_ScopeEntry = ModelScope | tuple[LoadError, ...]

# Lines as counted by expat: LF, CRLF, and CR end a line (str.splitlines also splits on
# form feed, U+2028 and others, which would shift line numbers).
_LINE_RE: Final = re.compile(r"[^\r\n]*(?:\r\n|\r|\n)|[^\r\n]+\Z")
_MAX_ECHO: Final = 80


def _describe(error: LoadError) -> str:
    if error.subject == error.module:
        return f"{error.module}: {error.reason}"
    return f"{error.module}: {error.subject}: {error.reason}"


def _scope_entry(
    modules: tuple[str, ...],
    definitions: Mapping[str, tuple[Definition, ...]],
    errors: Mapping[str, tuple[LoadError, ...]],
) -> _ScopeEntry:
    failed = tuple(e for m in modules for e in errors.get(m, ()))
    return failed or _build_scope(modules, definitions)


def _build_scope(
    modules: tuple[str, ...], definitions: Mapping[str, tuple[Definition, ...]]
) -> ModelScope:
    """Index the Definitions of ``modules`` (in scope order) by name and by global kind."""
    by_name: dict[str, list[Definition]] = {}
    globals_: dict[tuple[str, str], list[Definition]] = {}
    for module in modules:
        for d in definitions.get(module, ()):
            by_name.setdefault(d.key.name, []).append(d)
            if d.scope == "global":
                globals_.setdefault((d.key.kind, d.key.name), []).append(d)
    return ModelScope(
        modules=modules,
        _by_name=MappingProxyType({k: tuple(v) for k, v in by_name.items()}),
        _globals=MappingProxyType({k: tuple(v) for k, v in globals_.items()}),
        _names=tuple(sorted(by_name)),
    )


class MetaschemaIndex:
    """Immutable index of the Metaschema bundle; build it with ``MetaschemaIndex.load``.

    Scopes for every ``OSCALModelType`` and for the whole bundle are computed in ``load``.
    The only state that changes after ``load`` is the ``source_lines`` cache, which holds
    immutable tuples and is filled under a lock.
    """

    __slots__ = (
        "_allowlist",
        "_lines",
        "_lines_lock",
        "_load_errors",
        "_root",
        "_scopes",
        "_sources",
    )

    def __init__(
        self,
        root: Path,
        allowlist: frozenset[str],
        scopes: Mapping[OSCALModelType | None, _ScopeEntry],
        load_errors: Mapping[str, tuple[LoadError, ...]],
    ) -> None:
        self._root = root
        self._allowlist = allowlist
        self._scopes = scopes
        self._load_errors = load_errors
        self._sources = tuple(
            sorted(
                rel
                for rel in allowlist
                if _MODULE_RE.match(rel)
                or (rel.startswith(f"{SHARED_DIR}/") and rel.endswith(".ent"))
            )
        )
        self._lines: dict[str, tuple[str, ...]] = {}
        self._lines_lock = threading.Lock()

    @classmethod
    def load(cls, root: Path = BUNDLE_DIR) -> MetaschemaIndex:
        """Parse every manifest-listed module under ``root`` and build all scopes.

        Per-module failures never raise: they are recorded in ``load_errors``, the module's
        Definitions are excluded, and every scope that reaches the module raises
        ``MetaschemaLoadError`` on access. An import that is refused or does not name a
        module is a load error of the importing module.

        Raises:
            MetaschemaLoadError: a bundle manifest is missing, unreadable, or malformed. The
                message names the manifest by its package-relative path only.
        """
        try:
            allowlist = _read_allowlist(root)
        except _Refused as err:
            manifest = f"{BUNDLE_DIR.name}/{err.subject or MANIFEST}"
            msg = (
                f"OSCAL Metaschema bundle cannot be loaded: {manifest}: {err.reason}. "
                "Reinstall the package, or run `hatch run rehash` in a checkout."
            )
            raise MetaschemaLoadError(msg) from err

        modules = _module_files(allowlist)
        module_set = frozenset(modules)
        errors: dict[str, tuple[LoadError, ...]] = {}
        definitions: dict[str, tuple[Definition, ...]] = {}
        imports: dict[str, tuple[str, ...]] = {}
        for module in modules:
            tree = _load_module(root, module, allowlist)
            if isinstance(tree, LoadError):
                errors[module] = (tree,)
                continue
            try:
                defs = _Extractor(module).run(tree)
            except _Refused as err:
                errors[module] = (
                    _failed(LoadError(module, err.subject or module, err.reason), err),
                )
                continue
            imports[module], import_errors = _imports(root, module, tree, allowlist, module_set)
            if import_errors:
                errors[module] = import_errors
                continue
            definitions[module] = defs

        frozen_errors = MappingProxyType(errors)
        scopes: dict[OSCALModelType | None, _ScopeEntry] = {}
        for model in OSCALModelType:
            start = model_module(model)
            if start in module_set:
                scopes[model] = _scope_entry(_closure(start, imports), definitions, frozen_errors)
            else:
                scopes[model] = (LoadError(start, start, REFUSED_NO_MODULE),)
        scopes[None] = _scope_entry(modules, definitions, frozen_errors)
        return cls(root, allowlist, MappingProxyType(scopes), frozen_errors)

    @property
    def load_errors(self) -> Mapping[str, tuple[LoadError, ...]]:
        """Per-module load errors, keyed by module file name (read-only)."""
        return self._load_errors

    def scope(self, model: OSCALModelType) -> ModelScope:
        """The Model_Scope of ``model``.

        Raises:
            MetaschemaLoadError: a module in the scope failed to load; the message names each
                failing module and the entity, import, file, or limit, from ``LoadError``
                fields only (RC-13).
        """
        model = OSCALModelType(model)
        return self._resolve(model, f"model '{model.value}'")

    def all_modules(self) -> ModelScope:
        """A scope over every module in the bundle (sorted module order).

        Raises:
            MetaschemaLoadError: any module in the bundle failed to load.
        """
        return self._resolve(None, "the bundle")

    def _resolve(self, key: OSCALModelType | None, label: str) -> ModelScope:
        entry = self._scopes[key]
        if isinstance(entry, ModelScope):
            return entry
        details = "; ".join(_describe(e) for e in entry)
        raise MetaschemaLoadError(f"OSCAL Metaschema for {label} failed to load: {details}")

    def source_files(self) -> tuple[str, ...]:
        """Manifest-listed Metaschema_Sources (modules and ``.ent`` files), sorted."""
        return self._sources

    def source_lines(self, rel: str) -> tuple[str, ...]:
        """The lines of a Metaschema_Source, with line endings kept (cached).

        ``rel`` must be exactly one of ``source_files()``; nothing is opened otherwise. The
        file is resolved and read through the same checks and size limit as the loader.

        Raises:
            ValueError: ``rel`` is not a Metaschema_Source.
            MetaschemaLoadError: the file cannot be read or decoded (client-safe message).
        """
        if rel not in self._sources:
            shown = rel if len(rel) <= _MAX_ECHO else rel[:_MAX_ECHO] + "…"
            msg = f"{shown!r} is not an OSCAL Metaschema source file"
            raise ValueError(msg)
        cached = self._lines.get(rel)
        if cached is None:
            with self._lines_lock:
                cached = self._lines.get(rel)
                if cached is None:
                    cached = self._read_lines(rel)
                    self._lines[rel] = cached
        return cached

    def _read_lines(self, rel: str) -> tuple[str, ...]:
        try:
            target = resolve_bundle_path(self._root, rel, self._root, self._allowlist)
            text = _read_bounded(target.path, target.rel).decode("utf-8")
        except _Refused as err:
            msg = f"OSCAL Metaschema source {rel} cannot be read: {err.reason}"
            raise MetaschemaLoadError(msg) from err
        except UnicodeDecodeError as err:
            msg = f"OSCAL Metaschema source {rel} cannot be read: {REFUSED_UNDECODABLE}"
            raise MetaschemaLoadError(msg) from err
        return tuple(_LINE_RE.findall(text))


# Process-wide index singleton. Read without the lock (the index is immutable once built);
# the lock only serializes the first load.
_index: MetaschemaIndex | None = None
_lock = threading.Lock()


def get_index() -> MetaschemaIndex:
    """Return the process-wide ``MetaschemaIndex``, loading it on first use (thread-safe).

    Double-checked locking: concurrent first callers share one ``load``. If ``load`` raises
    (``MetaschemaLoadError`` for a bundle-level failure such as a missing manifest), the
    singleton stays ``None`` and the next call retries.
    """
    global _index  # noqa: PLW0603 - process-wide singleton, same pattern as init_store()
    idx = _index
    if idx is None:
        with _lock:
            idx = _index
            if idx is None:
                # BUNDLE_DIR is read from module globals at call time so tests can patch it.
                idx = MetaschemaIndex.load(BUNDLE_DIR)
                _index = idx
    return idx
