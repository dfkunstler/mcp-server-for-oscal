"""Tests for tools/metaschema_paths.py (pure Metaschema target and selector algorithms).

Example tests follow the normalization table in the metaschema-constraints design.
Property 1 (target normalization) is TestNormalizeTargetProperty; Property 2 (selector
round trip) is TestSelectorRoundTripProperty.
"""

from dataclasses import dataclass
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

from mcp_server_for_oscal.tools.metaschema_paths import (
    MAX_SELECTOR_LENGTH,
    Condition,
    PathAlternative,
    compose_selector,
    normalize_target,
    parse_selector,
    validate_selector,
)

NS = "http://csrc.nist.gov/ns/oscal"


def _single(target: str | None) -> PathAlternative:
    alternatives = normalize_target(target)
    assert len(alternatives) == 1
    return alternatives[0]


class TestNormalizeTargetExamples:
    """Design table "Normalized_Path and Condition algorithm".

    Validates: Requirements 6.4, 6.5, 8.7, 9.5, 9.7
    """

    @pytest.mark.parametrize(
        ("target", "path", "namespace", "conditions"),
        [
            (f"prop[has-oscal-namespace('{NS}')]/@name", "prop/@name", NS, ()),
            (
                f"prop[has-oscal-namespace('{NS}') and @name='asset-type']/@value",
                "prop/@value",
                NS,
                (Condition("prop", "@name='asset-type'"),),
            ),
            (
                "(.)[@type='service']/link/@rel",
                "link/@rel",
                None,
                (Condition("(.)", "@type='service'"),),
            ),
            (f".[has-oscal-namespace('{NS}')]/@name", "@name", NS, ()),
            (".//by-component", "//by-component", None, ()),
            (".", "", None, ()),
            ("@type", "@type", None, ()),
            (f'prop[has-namespace("{NS}")]/@name', "prop/@name", NS, ()),
        ],
    )
    def test_table_rows(self, target, path, namespace, conditions):
        alt = _single(target)
        assert alt.raw == target
        assert alt.path == path
        assert alt.namespace == namespace
        assert alt.conditions == conditions
        assert alt.cross_document is False

    def test_absent_target_is_self(self):
        alt = _single(None)
        assert alt == PathAlternative(
            raw=".", path="", conditions=(), namespace=None, cross_document=False
        )

    @pytest.mark.parametrize(
        ("target", "expected"),
        [
            # Table row `a/@x|b/@x` verbatim: one alternative each, nothing removed.
            (
                "a/@x|b/@x",
                (
                    PathAlternative("a/@x", "a/@x", (), None, False),
                    PathAlternative("b/@x", "b/@x", (), None, False),
                ),
            ),
            # Namespace and conditions are per alternative; whitespace around "|" is stripped.
            (
                f" a/@x | b[has-oscal-namespace('{NS}') and @y='1']/@x ",
                (
                    PathAlternative("a/@x", "a/@x", (), None, False),
                    PathAlternative(
                        f"b[has-oscal-namespace('{NS}') and @y='1']/@x",
                        "b/@x",
                        (Condition("b", "@y='1'"),),
                        NS,
                        False,
                    ),
                ),
            ),
            # Table row with doc(...): same path twice, only the second is cross-document.
            (
                "metadata/role|doc(import-ssp/@href)/metadata/role",
                (
                    PathAlternative("metadata/role", "metadata/role", (), None, False),
                    PathAlternative(
                        "doc(import-ssp/@href)/metadata/role", "metadata/role", (), None, True
                    ),
                ),
            ),
        ],
    )
    def test_union_table_rows(self, target, expected):
        assert normalize_target(target) == expected

    def test_descendant_step_between_names_is_kept(self):
        alt = _single(f"part[has-oscal-namespace('{NS}') and @name='a']//part[@name='b']/@name")
        assert alt.path == "part//part/@name"
        assert alt.namespace == NS
        assert alt.conditions == (Condition("part", "@name='a'"), Condition("part", "@name='b'"))


class TestNamespaceConditionRules:
    """RC-4: what counts as a Namespace_Condition."""

    def test_namespace_from_last_step_earlier_ones_verbatim(self):
        first = f"has-oscal-namespace('{NS}')"
        alt = _single(f"a[{first}]/b[has-oscal-namespace('urn:x')]/@c")
        assert alt.path == "a/b/@c"
        assert alt.namespace == "urn:x"
        assert alt.conditions == (Condition("a", first),)

    def test_sequence_argument_is_not_namespace_condition(self):
        predicate = f"has-oscal-namespace(('{NS}', 'urn:x'))"
        alt = _single(f"prop[{predicate}]/@name")
        assert alt.namespace is None
        assert alt.conditions == (Condition("prop", predicate),)

    def test_top_level_or_keeps_predicate_verbatim(self):
        predicate = f"has-oscal-namespace('{NS}') or @name='x'"
        alt = _single(f"prop[{predicate}]/@name")
        assert alt.namespace is None
        assert alt.conditions == (Condition("prop", predicate),)

    def test_and_inside_names_and_strings_does_not_split(self):
        predicate = "@name='a and b' and @band-width='c'"
        alt = _single(f"prop[has-oscal-namespace('{NS}') and {predicate}]/@value")
        assert alt.namespace == NS
        assert alt.conditions == (Condition("prop", predicate),)


class TestScannerAndMalformedInput:
    def test_quoted_separators_are_not_top_level(self):
        alt = _single("prop[@name='a/b|c[d]']/@value")
        assert alt.path == "prop/@value"
        assert alt.conditions == (Condition("prop", "@name='a/b|c[d]'"),)

    @pytest.mark.parametrize(
        "target",
        ["prop[@name='x'/@value", "prop[@name='x]/@value", "a)/b", "a[(]/b", "a[)]", "a/", "a///b"],
    )
    def test_malformed_gives_none_path(self, target):
        alt = _single(target)
        assert alt.path is None
        assert alt.raw == target

    def test_bad_alternative_does_not_affect_others(self):
        alts = normalize_target("a/@x||b/@x")
        assert [a.path for a in alts] == ["a/@x", None, "b/@x"]

    @pytest.mark.parametrize("target", ["", " ", "|", "[", "]", "'", "doc(", "a[]", "a[x]b"])
    def test_never_raises(self, target):
        assert all(a.path is None for a in normalize_target(target))


RMF = "http://csrc.nist.gov/ns/rmf"
_HAS_NS = f"has-oscal-namespace('{NS}')"
_BUNDLE_DIR = Path(__file__).parents[2] / "src" / "mcp_server_for_oscal" / "oscal_metaschema"


@pytest.fixture(scope="module")
def bundle_text() -> str:
    files = [*_BUNDLE_DIR.glob("*.xml"), *_BUNDLE_DIR.glob("shared-constraints/*.ent")]
    return "\n".join(f.read_text(encoding="utf-8") for f in sorted(files))


class TestBundledTargetExamples:
    """Real Constraint_Targets copied verbatim from the bundled OSCAL 1.2.3 Metaschema.

    Each expected tuple holds ``(path, namespace, conditions, cross_document)`` per
    alternative, derived from the design rules (RC-3, RC-4, self-step stripping).

    Validates: Requirements 6.4, 6.5
    """

    @pytest.mark.parametrize(
        ("target", "expected"),
        [
            # Several "and" conjuncts besides the namespace stay joined in one Condition.
            (
                f"prop[{_HAS_NS} and @name='type' and @value='data-center']/@class",
                [("prop/@class", NS, [("prop", "@name='type' and @value='data-center'")], False)],
            ),
            # Sequence comparison in a "(.)" self-step predicate; namespace from a later step.
            (
                f"(.)[@type=('software', 'hardware', 'service')]/prop[{_HAS_NS}]/@name",
                [("prop/@name", NS, [("(.)", "@type=('software', 'hardware', 'service')")], False)],
            ),
            # A "." self step alone normalizes to the empty path.
            (f".[{_HAS_NS}]", [("", NS, [], False)]),
            # "//" inside a string literal is not a descendant step.
            (
                ".[@system='https://www.first.org/cvss/v4-0' and @name='v']/@value",
                [
                    (
                        "@value",
                        None,
                        [(".", "@system='https://www.first.org/cvss/v4-0' and @name='v'")],
                        False,
                    )
                ],
            ),
            # Function call conjunct without a namespace stays verbatim.
            (
                ".[@rel=('reference') and starts-with(@href,'#')]/@href",
                [("@href", None, [(".", "@rel=('reference') and starts-with(@href,'#')")], False)],
            ),
            # Namespace on both steps: the last step's wins, the earlier one stays verbatim.
            (
                f"part[{_HAS_NS} and @name='statement']//part[{_HAS_NS}]/@name",
                [("part//part/@name", NS, [("part", f"{_HAS_NS} and @name='statement'")], False)],
            ),
            # Last step's namespace is a different URI than the earlier step's.
            (
                f"part[{_HAS_NS} and @name=('assessment','assessment-method')]"
                f"/prop[has-oscal-namespace('{RMF}')]/@name",
                [
                    (
                        "part/prop/@name",
                        RMF,
                        [("part", f"{_HAS_NS} and @name=('assessment','assessment-method')")],
                        False,
                    )
                ],
            ),
            # A sequence argument on the last step is not a Namespace_Condition, so the
            # namespace comes from the earlier step and the later predicate stays verbatim.
            (
                f"part[{_HAS_NS} and @name=('assessment','assessment-method')]"
                f"/prop[has-oscal-namespace(('{NS}','{RMF}')) and @name='method']/@value",
                [
                    (
                        "part/prop/@value",
                        NS,
                        [
                            ("part", "@name=('assessment','assessment-method')"),
                            ("prop", f"has-oscal-namespace(('{NS}','{RMF}')) and @name='method'"),
                        ],
                        False,
                    )
                ],
            ),
            # Three alternatives, one with a leading ".//" self step.
            (
                "responsible-role[party-uuid]|statement/responsible-role[party-uuid]"
                "|.//by-component//responsible-role[party-uuid]",
                [
                    ("responsible-role", None, [("responsible-role", "party-uuid")], False),
                    (
                        "statement/responsible-role",
                        None,
                        [("responsible-role", "party-uuid")],
                        False,
                    ),
                    (
                        "//by-component//responsible-role",
                        None,
                        [("responsible-role", "party-uuid")],
                        False,
                    ),
                ],
            ),
            # A doc(...) argument with its own predicate and "/" steps is consumed whole.
            (
                "control-implementation/implemented-requirement//by-component"
                "|doc(system-implementation/leveraged-authorization/link[@rel='system-security-plan']"
                "/@href)/system-security-plan/control-implementation/implemented-requirement"
                "//by-component",
                [
                    (
                        "control-implementation/implemented-requirement//by-component",
                        None,
                        [],
                        False,
                    ),
                    (
                        "system-security-plan/control-implementation/implemented-requirement"
                        "//by-component",
                        None,
                        [],
                        True,
                    ),
                ],
            ),
            # A leading "//" is kept; a "|" inside parentheses is not a top-level union.
            ("//control", [("//control", None, [], False)]),
            ("//(control|group|part)", [("//(control|group|part)", None, [], False)]),
        ],
    )
    def test_real_target(self, bundle_text, target, expected):
        assert f'target="{target}"' in bundle_text, "example must be copied from the bundle"
        got = [
            (
                alt.path,
                alt.namespace,
                [(c.step, c.predicate) for c in alt.conditions],
                alt.cross_document,
            )
            for alt in normalize_target(target)
        ]
        assert got == expected


# ---------------------------------------------------------------------------
# Property 1: generated target structures and their expected normalization
# ---------------------------------------------------------------------------

# Names never equal an XPath keyword the scanner splits on.
_names = st.from_regex(r"[a-z][a-z0-9-]{0,7}", fullmatch=True).filter(
    lambda s: s not in {"and", "or"}
)
# String-literal content: separators, brackets, parens, and keywords that must stay quoted.
_literal_text = st.lists(
    st.sampled_from(
        ["a", "/", "//", "|", "[", "]", "(", ")", " and ", " or ", " ", "x-1", ":", "é"]
    ),
    max_size=6,
).map("".join)
_quote = st.sampled_from(["'", '"'])
_pad = st.sampled_from(["", " "])


@dataclass(frozen=True)
class _Atom:
    text: str
    namespace: str | None  # set only for a single-literal Namespace_Condition


@dataclass(frozen=True)
class _Predicate:
    atoms: tuple[_Atom, ...]
    joiner: str  # "and" or "or"
    pad: str  # whitespace inside the brackets

    @property
    def text(self) -> str:
        return f" {self.joiner} ".join(a.text for a in self.atoms)

    def namespace_split(self) -> tuple[str, str] | None:
        """Expected (uri, remaining condition text) per RC-4, or None."""
        if self.joiner == "or":
            return None
        chosen = [i for i, a in enumerate(self.atoms) if a.namespace is not None]
        if not chosen:
            return None
        last = chosen[-1]
        rest = " and ".join(a.text for i, a in enumerate(self.atoms) if i != last)
        return self.atoms[last].namespace or "", rest


@st.composite
def _quoted(draw) -> str:
    q = draw(_quote)
    return q + draw(_literal_text) + q


@st.composite
def _namespace_atom(draw) -> _Atom:
    func = draw(st.sampled_from(["has-oscal-namespace", "has-namespace"]))
    q = draw(_quote)
    uri = draw(_literal_text)
    pad = draw(_pad)
    return _Atom(f"{func}({pad}{q}{uri}{q}{pad})", uri)


@st.composite
def _namespace_sequence_atom(draw) -> _Atom:
    func = draw(st.sampled_from(["has-oscal-namespace", "has-namespace"]))
    uris = draw(st.lists(_quoted(), min_size=2, max_size=3))
    return _Atom(f"{func}(({', '.join(uris)}))", None)


@st.composite
def _comparison_atom(draw) -> _Atom:
    lhs = draw(st.one_of(_names.map(lambda n: "@" + n), _names, st.just(".")))
    if draw(st.booleans()):
        lhs = f"{draw(_names)}/{lhs}"
    return _Atom(f"{lhs}={draw(_quoted())}", None)


@st.composite
def _group_atom(draw) -> _Atom:
    left = draw(_comparison_atom())
    right = draw(st.one_of(_comparison_atom(), _namespace_atom()))
    return _Atom(f"({left.text} or {right.text})", None)


_atoms = st.one_of(_namespace_atom(), _namespace_sequence_atom(), _comparison_atom(), _group_atom())


@st.composite
def _predicates(draw) -> _Predicate:
    if draw(st.booleans()):
        atoms = draw(st.lists(_atoms, min_size=1, max_size=3))
        joiner = "and"
    else:
        atoms = draw(st.lists(_atoms, min_size=2, max_size=2))
        joiner = "or"
    return _Predicate(tuple(atoms), joiner, draw(_pad))


@dataclass(frozen=True)
class _Step:
    sep: str  # "", "/", or "//"
    base: str
    predicates: tuple[_Predicate, ...]

    @property
    def text(self) -> str:
        preds = "".join(f"[{p.pad}{p.text}{p.pad}]" for p in self.predicates)
        return f"{self.sep}{self.base}{preds}"


_step_predicates = st.lists(_predicates(), max_size=2).map(tuple)
_step_bases = st.one_of(_names, _names.map(lambda n: "@" + n))


@dataclass(frozen=True)
class _Alternative:
    doc: str | None  # rendered doc(...) call
    self_step: _Step | None
    steps: tuple[_Step, ...]

    def render(self) -> str:
        parts = [self.doc or ""]
        if self.self_step is not None:
            parts.append(self.self_step.text)
        parts.extend(s.text for s in self.steps)
        return "".join(parts)

    def expected_path(self) -> str:
        prefixed = self.doc is not None or self.self_step is not None
        parts = []
        for idx, step in enumerate(self.steps):
            drop_sep = idx == 0 and prefixed and step.sep == "/"
            parts.append(step.base if drop_sep else step.sep + step.base)
        return "".join(parts)

    def expected_conditions_and_namespace(self) -> tuple[tuple[Condition, ...], str | None]:
        steps = ([self.self_step] if self.self_step else []) + list(self.steps)
        chosen = None
        for s_idx, step in enumerate(steps):
            for p_idx, pred in enumerate(step.predicates):
                if pred.namespace_split() is not None:
                    chosen = (s_idx, p_idx)
        conditions: list[Condition] = []
        namespace = None
        for s_idx, step in enumerate(steps):
            for p_idx, pred in enumerate(step.predicates):
                if (s_idx, p_idx) == chosen:
                    namespace, rest = pred.namespace_split() or ("", "")
                    if rest:
                        conditions.append(Condition(step.base, rest))
                else:
                    conditions.append(Condition(step.base, pred.text))
        return tuple(conditions), namespace


@st.composite
def _alternatives(draw) -> _Alternative:
    doc = None
    if draw(st.booleans()):
        doc = f"doc({draw(_names)}/@{draw(_names)})"
    self_step = None
    if draw(st.booleans()):
        base = draw(st.sampled_from([".", "(.)"]))
        self_step = _Step("/" if doc else "", base, draw(_step_predicates))
    prefixed = doc is not None or self_step is not None
    count = draw(st.integers(min_value=0 if prefixed else 1, max_value=6))
    steps = []
    for idx in range(count):
        if idx == 0 and not prefixed:
            sep = draw(st.sampled_from(["", "/", "//"]))
        else:
            sep = draw(st.sampled_from(["/", "//"]))
        steps.append(_Step(sep, draw(_step_bases), draw(_step_predicates)))
    return _Alternative(doc, self_step, tuple(steps))


# Arbitrary text biased toward the scanner's special characters and keywords.
_target_fragments = st.lists(
    st.sampled_from([*"ab.@/|[]()'\" \t-:=", "and", "or", "doc(", "(.)"]),
    max_size=40,
)


class TestNormalizeTargetProperty:
    """Property 1: Target normalization recovers the structure it was rendered from.

    **Validates: Requirements 6.4, 6.5, 8.7, 9.5, 9.7**
    """

    @given(
        alternatives=st.lists(_alternatives(), min_size=1, max_size=4),
        separators=st.lists(st.sampled_from(["|", " | ", "| "]), min_size=3, max_size=3),
    )
    def test_normalization_recovers_structure(self, alternatives, separators):
        rendered = [alt.render() for alt in alternatives]
        target = rendered[0]
        for sep, text in zip(separators, rendered[1:], strict=False):
            target += sep + text

        result = normalize_target(target)

        assert len(result) == len(alternatives)
        for got, alt, raw in zip(result, alternatives, rendered, strict=True):
            conditions, namespace = alt.expected_conditions_and_namespace()
            assert got.raw == raw
            assert got.path == alt.expected_path()
            assert got.conditions == conditions
            assert got.namespace == namespace
            assert got.cross_document is (alt.doc is not None)

    @given(target=st.one_of(st.none(), st.text(max_size=60), _target_fragments.map("".join)))
    def test_never_raises(self, target):
        result = normalize_target(target)
        assert result
        assert all(isinstance(alt, PathAlternative) for alt in result)


class TestSelectorExamples:
    """Design "Target_Selector grammar".

    Validates: Requirements 5.2, 5.3, 5.11
    """

    @pytest.mark.parametrize(
        ("selector", "expected"),
        [
            ("x", ("x", "")),
            ("defined-component/@type", ("defined-component", "@type")),
            ("defined-component/prop/@name", ("defined-component", "prop/@name")),
            ("system-component/link/@rel", ("system-component", "link/@rel")),
            ("part//part/@name", ("part", "//part/@name")),
            ("a/b//c", ("a", "b//c")),
        ],
    )
    def test_parse_and_compose_round_trip(self, selector, expected):
        assert parse_selector(selector) == expected
        assert compose_selector(*expected) == selector

    @pytest.mark.parametrize(
        ("selector", "rule"),
        [
            ("", "empty"),
            ("a" * (MAX_SELECTOR_LENGTH + 1), "at most 512"),
            (" x", "whitespace"),
            ("x ", "whitespace"),
            ("a/b c", "whitespace"),
            ("a/\tb", "whitespace"),
            ("a\nb", "whitespace"),
            ("a\u00a0b", "whitespace"),
            ("/x", "start with '/'"),
            ("//x", "start with '/'"),
            ("x/", "end with '/'"),
            ("a///b", "'///'"),
            ("prop[@name='x']", "predicates or unions"),
            ("a[b", "found ["),
            ("a/b]", "found ]"),
            ("a(b", "found ("),
            ("(.)/x", "predicates or unions"),
            ("a)", "found )"),
            ("a|b", "found |"),
            ("metadata/role|doc", "predicates or unions"),
        ],
    )
    def test_invalid_selector_names_rule(self, selector, rule):
        with pytest.raises(ValueError, match="Invalid target selector") as exc:
            validate_selector(selector)
        message = str(exc.value)
        assert rule in message
        shown = selector if len(selector) <= 80 else selector[:80] + "..."
        assert f"'{shown}'" in message  # names the selector
        with pytest.raises(ValueError):
            parse_selector(selector)

    def test_max_length_accepted(self):
        validate_selector("a" * MAX_SELECTOR_LENGTH)

    def test_message_truncates_selector_to_80_chars(self):
        selector = "b" * 100 + " "
        with pytest.raises(ValueError) as exc:
            validate_selector(selector)
        message = str(exc.value)
        assert "b" * 80 + "..." in message
        assert "b" * 81 not in message


# ---------------------------------------------------------------------------
# Property 2: generated definition names and paths in the selector grammar
# ---------------------------------------------------------------------------

# A step is any non-empty run of characters other than "/", whitespace, and "[ ] ( ) |".
# Bias toward Metaschema-like names, but include other permitted characters too.
_selector_step_chars = st.one_of(
    st.sampled_from([*"abcxyz019-_.:@", "é", "ü"]),
    st.characters(
        blacklist_categories=("Z", "C"),
        blacklist_characters="/[]()|",
    ),
)
_selector_steps = st.one_of(
    _names,
    _names.map(lambda n: "@" + n),
    st.text(_selector_step_chars, min_size=1, max_size=8),
).filter(lambda s: not any(ch.isspace() for ch in s))


@st.composite
def _selector_paths(draw) -> str:
    """A remainder path: empty, or steps joined by "/" or "//", optionally led by "//"."""
    steps = draw(st.lists(_selector_steps, max_size=6))
    if not steps:
        return ""
    parts = [draw(st.sampled_from(["", "//"])) + steps[0]]
    parts.extend(draw(st.sampled_from(["/", "//"])) + step for step in steps[1:])
    return "".join(parts)


# Arbitrary selector-like text biased toward separators and forbidden characters.
_selector_like = st.lists(
    st.sampled_from([*"ab@-./", "/", "//", "///", " ", "[", "]", "(", ")", "|", "é"]),
    max_size=30,
).map("".join)


class TestSelectorRoundTripProperty:
    """Property 2: Target_Selector compose/parse round trip.

    **Validates: Requirements 5.2, 5.3**
    """

    @given(definition=_selector_steps, path=_selector_paths())
    def test_compose_then_parse_round_trips(self, definition, path):
        selector = compose_selector(definition, path)
        assert len(selector) <= MAX_SELECTOR_LENGTH  # generator stays within the length rule
        validate_selector(selector)
        assert parse_selector(selector) == (definition, path)

    @given(selector=st.one_of(_selector_like, st.text(max_size=40)))
    def test_parse_then_compose_round_trips(self, selector):
        try:
            parsed = parse_selector(selector)
        except ValueError:
            return
        assert compose_selector(*parsed) == selector
