"""
Tool for finding what in an OSCAL model must refer to a given assembly, from the OSCAL Metaschema.

An ``index`` Constraint collects the keys (usually UUIDs or IDs) of the instances its target
selects; an ``index-has-key`` Constraint with the same ``name`` requires a value elsewhere to be
one of those keys. Pairing the two answers "what refers to a ``component``?" including
prop- and link-based references that field-name matching misses (Requirement 9).

``index`` Constraints are always resolved across every bundled module (9.6); ``model`` only
restricts where the ``index-has-key`` Constraint is declared (9.3). All Metaschema data comes
from ``metaschema_index`` (Requirement 7.1); shared checks come from ``metaschema_common``.
"""

import logging
from collections.abc import Iterator
from dataclasses import dataclass

from mcp.server.mcpserver import Context
from strands import tool

from mcp_server_for_oscal.tools.metaschema_common import (
    add_namespace_and_conditions,
    check_model,
    check_page,
)
from mcp_server_for_oscal.tools.metaschema_index import (
    Constraint,
    MetaschemaLoadError,
    ModelScope,
    get_index,
)
from mcp_server_for_oscal.tools.metaschema_paths import PathAlternative
from mcp_server_for_oscal.tools.utils import paginate_within_budget, try_notify_client_error

logger = logging.getLogger(__name__)


@tool()
def find_references_to(
    assembly: str,
    model: str | None = None,
    offset: int = 0,
    limit: int = 25,
    ctx: Context | None = None,
) -> dict:
    """Find every field, flag, prop, or link that OSCAL Metaschema requires to reference a given assembly by key (UUID or ID), e.g. "What in an SSP refers to a system-component by UUID?", "Which props must name a location?", or "What may point to a back-matter resource?".

    Answers come from the Metaschema `index` / `index-has-key` constraint pairs bundled with the
    server. The generated JSON and XML schemas do not express these references, and many of
    them are props or links (e.g. `prop[@name='physical-location']/@value`) that a search for
    a `*-uuid` field misses.

    `assembly` is matched exactly (case-sensitive) against the last step of each `index`
    constraint's target, e.g. "component", "party", "location", "resource", "by-component".
    A Metaschema definition name also works when the definition is used under another name,
    e.g. "system-component" (used as "component") or "property" (used as "prop"). No match
    returns an empty page.

    Args:
        assembly: Instance name or definition name of the referenced assembly, as above.
        model: Optional OSCAL model name, e.g. "system-security-plan". When given, only
            references declared in that model's Metaschema modules are returned; the indexes
            they point to are always resolved across all modules. Use list_oscal_models for
            valid names. Omit to search every bundled module.
        offset: Zero-based index of the first item to return (default 0).
        limit: Maximum number of items to return, 1-100 (default 25). A page may hold fewer
            items to stay within the response size budget; `limit` then reports the reduced
            size.
        ctx: MCP server context (injected automatically by the MCP server).

    Returns:
        dict: Page_Response with `items`, `total`, `offset`, `limit`, and `hasMore`, ordered by
        where each reference is declared. Each item describes one reference: `index-name`;
        the referencing `index-has-key` constraint's `definition`, `target`, `key-fields`
        (the fields or flags holding the key), `source-file`, and `line`, plus `namespace`
        and `conditions` when its target has predicates; the referenced index's
        `index-definition`, `index-target`, `index-source-file`, and `index-line`; and
        `cross-document` (true when the index also collects keys from another document, such
        as a leveraged SSP).
    """
    logger.debug(
        "find_references_to(assembly: %s, model: %s, offset: %s, limit: %s)",
        assembly,
        model,
        offset,
        limit,
    )

    # Argument checks, all before any index access (Requirements 9.9, 9.10).
    try:
        if not isinstance(assembly, str):
            msg = f"Invalid assembly: expected a string, got {type(assembly).__name__}"
            raise ValueError(msg)  # noqa: TRY004 - tools raise ValueError for every bad argument
        model_type = check_model(model) if model is not None else None
        check_page(offset, limit)
    except ValueError as err:
        try_notify_client_error(str(err), ctx)
        raise

    try:
        index = get_index()
        bundle = index.all_modules()
        modules = frozenset(index.scope(model_type).modules) if model_type is not None else None
    except MetaschemaLoadError as err:
        # The message is client-safe by construction (RC-13).
        try_notify_client_error(str(err), ctx)
        raise

    pairs = find_reference_pairs(bundle, assembly, modules)
    items = [_reference_item(pair) for pair in pairs]
    return paginate_within_budget(items, offset, limit, truncatable=())


# --------------------------------------------------------------------------------------
# Matching (Requirements 9.2, 9.3, 9.6, 9.11)
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ReferencePair:
    """One ``index`` Constraint and one ``index-has-key`` Constraint with the same ``name``."""

    index: Constraint
    has_key: Constraint


def iter_constraints(scope: ModelScope) -> Iterator[Constraint]:
    """Every Constraint declared on a Definition in ``scope``, each once."""
    seen: set[int] = set()
    for name in scope.definition_names():
        for definition in scope.definitions(name):
            for constraint in definition.constraints:
                if id(constraint) not in seen:
                    seen.add(id(constraint))
                    yield constraint


def reference_names(scope: ModelScope, assembly: str) -> frozenset[str]:
    """Instance names that ``assembly`` stands for in an ``index`` target (9.1, 9.2).

    ``assembly`` itself, the ``use-name`` declared on any Definition named ``assembly``, and the
    ``use-name`` under which any model reference (``<assembly ref="...">``) uses that
    Definition, e.g. ``system-component`` is used as ``component``. Empty for an empty string.
    """
    if not assembly:
        return frozenset()
    names = {assembly}
    for definition in scope.definitions(assembly):
        if definition.use_name is not None:
            names.add(definition.use_name)
    for name in scope.definition_names():
        for definition in scope.definitions(name):
            for child in definition.model_children:
                if child.ref == assembly:
                    names.add(child.effective_name)
    return frozenset(names)


def last_step_names(alternative: PathAlternative) -> frozenset[str]:
    """Names the last step of ``alternative``'s Normalized_Path selects.

    The last step is the text after the final ``/``. A parenthesized union step such as
    ``(control|group|part)`` selects each of its members. Unparseable targets select nothing.
    """
    if alternative.path is None:
        return frozenset()
    step = alternative.path.rsplit("/", 1)[-1]
    if step.startswith("(") and step.endswith(")"):
        return frozenset(part.strip() for part in step[1:-1].split("|") if part.strip())
    return frozenset({step}) if step else frozenset()


def index_matches(index: Constraint, names: frozenset[str]) -> bool:
    """True if any alternative of ``index`` (cross-document included) ends in one of ``names``."""
    return any(last_step_names(alt) & names for alt in index.alternatives)


def find_reference_pairs(
    bundle: ModelScope, assembly: str, modules: frozenset[str] | None = None
) -> list[ReferencePair]:
    """Every (matching ``index``, same-named ``index-has-key``) pair, ordered (9.2-9.11).

    ``index`` Constraints come from ``bundle`` (all modules, 9.6). ``index-has-key``
    Constraints are kept only when declared in ``modules`` (a Model_Scope's modules), unless
    ``modules`` is ``None``. Ordered by the ``index-has-key`` Source_Location, then
    ``index-name``, then the ``index`` Source_Location so equal keys stay deterministic.
    """
    names = reference_names(bundle, assembly)
    if not names:
        return []
    indexes: dict[str, list[Constraint]] = {}
    has_keys: list[Constraint] = []
    for constraint in iter_constraints(bundle):
        name = constraint.attrs.get("name")
        if name is None:
            continue
        if constraint.type == "index" and index_matches(constraint, names):
            indexes.setdefault(name, []).append(constraint)
        elif constraint.type == "index-has-key" and (
            modules is None or constraint.definition_key.module in modules
        ):
            has_keys.append(constraint)
    pairs = [
        ReferencePair(index, has_key)
        for has_key in has_keys
        for index in indexes.get(has_key.attrs["name"], ())
    ]
    return sorted(
        pairs,
        key=lambda p: (
            p.has_key.location.source_file,
            p.has_key.location.line,
            p.index.attrs["name"],
            p.index.location.source_file,
            p.index.location.line,
        ),
    )


# --------------------------------------------------------------------------------------
# Item building (Requirements 9.4, 9.5, 9.7)
# --------------------------------------------------------------------------------------


def _reference_item(pair: ReferencePair) -> dict:
    """One result item: hyphenated keys, no null values."""
    has_key, index = pair.has_key, pair.index
    item: dict = {
        "index-name": index.attrs["name"],
        "definition": has_key.definition_key.name,
        "target": has_key.target,
        "key-fields": list(has_key.key_fields),
        "source-file": has_key.location.source_file,
        "line": has_key.location.line,
        "index-definition": index.definition_key.name,
        "index-target": index.target,
        "index-source-file": index.location.source_file,
        "index-line": index.location.line,
        "cross-document": any(alt.cross_document for alt in index.alternatives),
    }
    add_namespace_and_conditions(item, list(has_key.alternatives))
    return item
