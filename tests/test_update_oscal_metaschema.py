"""Tests for bin/update_oscal_metaschema.py (metaschema-constraints spec, Requirement 3).

Archives are built in memory and ``download`` is patched, so no test opens a network
connection. Spec: .kiro/specs/metaschema-constraints, GitHub issue #20.
"""

from __future__ import annotations

import contextlib
import io
import string
import tempfile
import zipfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Self
from unittest.mock import patch

from hypothesis import HealthCheck, event, given, settings
from hypothesis import strategies as st

from bin import update_oscal_metaschema as ums

METASCHEMA_NS = "http://csrc.nist.gov/ns/oscal/metaschema/1.0"

_HEX = "0123456789abcdef"

# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

valid_commits = st.text(alphabet=_HEX, min_size=40, max_size=40)

malformed_commits = st.one_of(
    st.text(alphabet=_HEX, min_size=0, max_size=39),  # too short
    st.text(alphabet=_HEX, min_size=41, max_size=50),  # too long
    # 40 characters with at least one uppercase hex digit
    st.tuples(
        st.text(alphabet=_HEX, min_size=39, max_size=39),
        st.sampled_from("ABCDEF"),
        st.integers(0, 39),
    ).map(lambda t: t[0][: t[2]] + t[1] + t[0][t[2] :]),
    # 40 characters with a non-hex character
    st.tuples(
        st.text(alphabet=_HEX, min_size=39, max_size=39),
        st.sampled_from(["g", "z", " ", "/", ".", "\n", "é"]),
        st.integers(0, 39),
    ).map(lambda t: t[0][: t[2]] + t[1] + t[0][t[2] :]),
    # a tag or branch name instead of a commit
    st.sampled_from(["v1.2.0", "main", "HEAD"]),
)

versions = st.from_regex(r"[0-9]{1,2}\.[0-9]{1,2}\.[0-9]{1,2}", fullmatch=True)

# Module and .ent base names: lowercase words joined by hyphens or underscores
_names = st.from_regex(r"[a-z][a-z0-9]{0,7}([-_][a-z0-9]{1,6}){0,2}", fullmatch=True)

# Filler text that is safe inside an XML comment (no "-", "<", "&")
_filler = st.text(alphabet=string.ascii_letters + string.digits + " .,;:()[]", max_size=40)

_newlines = st.sampled_from(["\n", "\r\n"])

# Bytes biased toward line endings so CRLF/LF/CR mixes are common
_arbitrary_bytes = st.lists(
    st.one_of(st.binary(min_size=1, max_size=16), st.sampled_from([b"\r\n", b"\n", b"\r"])),
    max_size=12,
).map(b"".join)


# True with probability 3/4 (Hypothesis also favours the first element)
_mostly_true = st.sampled_from([True, True, True, False])

# Empty with probability about 1/4, else 1-5 unique names
_maybe_empty_names = _mostly_true.flatmap(
    lambda nonempty: st.lists(_names, min_size=1, max_size=5, unique=True)
    if nonempty
    else st.just([])
)


@dataclass(frozen=True)
class Module:
    """A generated module: its bundle-relative name, bytes, and declared versions."""

    rel: str
    data: bytes
    declared: tuple[str, ...]


def _module_xml(declared: tuple[str, ...], filler: str, nl: str, *, namespaced: bool) -> bytes:
    ns = f' xmlns="{METASCHEMA_NS}"' if namespaced else ""
    lines = ['<?xml version="1.0" encoding="UTF-8"?>', f"<METASCHEMA{ns}>", f"  <!-- {filler} -->"]
    lines += [f"  <schema-version>{v}</schema-version>" for v in declared]
    lines += ["  <short-name>x</short-name>", "</METASCHEMA>", ""]
    return nl.join(lines).encode("utf-8")


@st.composite
def modules(draw: st.DrawFn, version: str) -> list[Module]:
    """0-5 modules, each declaring the requested version or a mismatching set."""
    names = draw(_maybe_empty_names)
    other = versions.filter(lambda v: v != version)
    mismatching = st.one_of(
        other.map(lambda v: (v,)),
        st.just(()),
        st.just((version, version)),
        other.map(lambda v: (version, v)),
    )
    # Mostly all-matching, so the success path is common; otherwise each module may mismatch
    all_match = draw(_mostly_true)
    out: list[Module] = []
    for name in names:
        if all_match or draw(st.booleans()):
            declared: tuple[str, ...] = (version,)
        else:
            declared = draw(mismatching)
        data = _module_xml(declared, draw(_filler), draw(_newlines), namespaced=draw(st.booleans()))
        out.append(Module(f"oscal_{name}_metaschema.xml", data, declared))
    return out


@st.composite
def ent_files(draw: st.DrawFn) -> dict[str, bytes]:
    """0-5 ``shared-constraints/*.ent`` files with arbitrary bytes."""
    names = draw(_maybe_empty_names)
    return {f"shared-constraints/{n}.ent": draw(_arbitrary_bytes) for n in names}


# Archive members (relative to OSCAL-<commit>/) that must never be selected
_EXTRA_MEMBERS = [
    "README.md",
    "src/metaschema/README.md",
    "src/metaschema/oscal_x_metaschema.json",
    "src/metaschema/sub/oscal_x_metaschema.xml",
    "src/metaschema/shared-constraints/x.xml",
    "src/metaschema/shared-constraints/sub/x.ent",
    "src/metaschema/x.ent",
    "src/oscal_x_metaschema.xml",
    "build/metaschema/oscal_y_metaschema.xml",
]

# Prior bundle files (bundle-relative); manifests are carried over on success
_PRIOR_FILES = [
    "README.md",
    "hashes.json",
    "oscal_old_metaschema.xml",
    "oscal_catalog_metaschema.xml",
    "shared-constraints/hashes.json",
    "shared-constraints/old.ent",
    "notes.txt",
]


@st.composite
def prior_bundles(draw: st.DrawFn) -> dict[str, bytes] | None:
    """Prior bundle contents, or None when ``dest`` does not exist yet."""
    if draw(st.integers(0, 9)) == 0:
        return None
    rels = draw(st.lists(st.sampled_from(_PRIOR_FILES), unique=True))
    return {rel: draw(_arbitrary_bytes) for rel in rels}


@dataclass(frozen=True)
class Scenario:
    version: str
    commit: str
    comment: bytes
    modules: list[Module]
    ents: dict[str, bytes]
    extras: dict[str, bytes]
    fault: str  # "none", "timeout", "http-error", "bad-zip"
    prior: dict[str, bytes] | None


@st.composite
def scenarios(draw: st.DrawFn) -> Scenario:
    version = draw(versions)
    commit = draw(valid_commits if draw(_mostly_true) else malformed_commits)
    if draw(_mostly_true):
        comment = commit.encode("utf-8")
    else:
        comment = draw(
            st.one_of(
                valid_commits.filter(lambda c: c != commit).map(str.encode),
                st.just(b""),  # empty or absent: zipfile writes no comment
                st.just(b"X" + commit.encode("utf-8")[1:]),  # one character differs
                st.just(commit.encode("utf-8") + b"\n"),
            )
        )
    extras = draw(st.dictionaries(st.sampled_from(_EXTRA_MEMBERS), _arbitrary_bytes, max_size=4))
    return Scenario(
        version=version,
        commit=commit,
        comment=comment,
        modules=draw(modules(version)),
        ents=draw(ent_files()),
        extras=extras,
        fault="none"
        if draw(_mostly_true)
        else draw(st.sampled_from(["timeout", "http-error", "bad-zip"])),
        prior=draw(prior_bundles()),
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def build_archive(s: Scenario) -> bytes:
    """Build a GitHub-style commit archive in memory."""
    root = f"OSCAL-{s.commit}/"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(root, b"")  # directory entry, as GitHub writes it
        zf.writestr(f"{root}src/metaschema/shared-constraints/", b"")
        for m in s.modules:
            zf.writestr(f"{root}src/metaschema/{m.rel}", m.data)
        for rel, data in s.ents.items():
            zf.writestr(f"{root}src/metaschema/{rel}", data)
        for rel, data in s.extras.items():
            zf.writestr(f"{root}{rel}", data)
        zf.comment = s.comment
    return buf.getvalue()


def snapshot(root: Path) -> dict[str, bytes]:
    """Map every file under ``root`` (POSIX relative path) to its bytes."""
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*") if p.is_file()}


def expected_failure(s: Scenario) -> tuple[str, str] | None:
    """Return (step, detail substring) of the first check that fails, or None."""
    if len(s.commit) != 40 or any(c not in _HEX for c in s.commit):
        return ("check commit", repr(s.commit))
    if s.fault in {"timeout", "http-error"}:
        return ("download", "")
    if s.fault == "bad-zip":
        return ("open archive", "")
    if s.comment != s.commit.encode("ascii"):
        return ("check zip comment", s.commit)
    if not s.modules:
        return ("select members", "oscal_*_metaschema.xml")
    if not s.ents:
        return ("select members", ".ent")
    bad = [m for m in s.modules if m.declared != (s.version,)]
    if bad:
        return ("check schema-version", bad[0].rel)
    return None


def run_main(s: Scenario, dest: Path) -> tuple[int, str]:
    """Run the CLI against ``dest`` with ``download`` patched; return (status, stderr)."""
    archive = build_archive(s)

    def fake_download(commit: str, **_kwargs: object) -> bytes:
        assert commit == s.commit
        if s.fault == "timeout":
            raise ums.UpdateError("download", "did not complete within 300 s")
        if s.fault == "http-error":
            raise ums.UpdateError("download", "HTTP Error 404: Not Found")
        if s.fault == "bad-zip":
            return b"PK\x03\x04 not a zip" + archive[:10]
        return archive

    err = io.StringIO()
    argv = ["--version", s.version, f"--commit={s.commit}", "--dest", str(dest)]
    with (
        patch.object(ums, "download", fake_download),
        contextlib.redirect_stderr(err),
        contextlib.redirect_stdout(io.StringIO()),
    ):
        status = ums.main(argv)
    return status, err.getvalue()


# ---------------------------------------------------------------------------
# Property 16
# ---------------------------------------------------------------------------


class TestUpdateMetaschemaProperty:
    """Feature: metaschema-constraints, Property 16: Metaschema update is all-or-nothing
    and byte-exact.

    **Validates: Requirements 3.1, 3.2, 3.3, 3.4, 3.5**
    """

    @settings(
        max_examples=200,
        deadline=None,
        suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large],
    )
    @given(s=scenarios())
    def test_update_is_all_or_nothing_and_byte_exact(self, s: Scenario) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            parent = Path(tmp)
            dest = parent / "oscal_metaschema"
            if s.prior is not None:
                dest.mkdir()
                for rel, data in s.prior.items():
                    (dest / rel).parent.mkdir(parents=True, exist_ok=True)
                    (dest / rel).write_bytes(data)
            before = snapshot(dest) if s.prior is not None else None

            day_before = datetime.now(UTC).date().isoformat()
            status, stderr = run_main(s, dest)
            day_after = datetime.now(UTC).date().isoformat()

            # No staging or backup directory is left next to dest
            assert sorted(p.name for p in parent.iterdir()) == (
                ["oscal_metaschema"] if dest.exists() else []
            )

            failure = expected_failure(s)
            event(f"outcome: {failure[0] if failure else 'success'}")
            if failure is not None:
                step, detail = failure
                assert status != 0
                assert stderr.startswith(f"ERROR: {step}: "), stderr
                assert detail in stderr
                if step == "check schema-version":
                    bad = next(m for m in s.modules if m.rel == detail)
                    assert repr(list(bad.declared)) in stderr
                # Every prior file byte-identical, none added or removed
                if before is None:
                    assert not dest.exists()
                else:
                    assert snapshot(dest) == before
                return

            assert status == 0, stderr
            assert stderr == ""
            after = snapshot(dest)

            expected = {m.rel: m.data for m in s.modules} | dict(s.ents)
            prior = s.prior or {}
            for rel in ("hashes.json", "shared-constraints/hashes.json"):
                if rel in prior:
                    expected[rel] = prior[rel]
            readme = after.pop("README.md").decode("utf-8")
            # Exactly the archive's matching files, byte-identical, plus carried manifests
            assert after == expected

            assert f"Tag: v{s.version}\n" in readme
            assert f"Commit: {s.commit}\n" in readme
            assert any(f"Retrieved: {d}\n" in readme for d in {day_before, day_after})


# ---------------------------------------------------------------------------
# Example tests
# ---------------------------------------------------------------------------

_COMMIT = "e061961c7702afeabffdf5e59894d35e748dade1"
_VERSION = "1.2.0"


class _FakeResponse:
    """Minimal stand-in for the object ``urlopen`` returns."""

    def __init__(self, body: bytes, headers: dict[str, str] | None = None) -> None:
        self._body = io.BytesIO(body)
        self.headers = headers or {}

    def read(self, n: int = -1) -> bytes:
        return self._body.read(n)

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None


class _FakeUrlopen:
    """Records each call and returns ``response`` (or raises ``error``)."""

    def __init__(self, response: _FakeResponse | None = None, error: Exception | None = None):
        self.response = response or _FakeResponse(b"")
        self.error = error
        self.calls: list[tuple[object, dict[str, object]]] = []

    def __call__(self, url: object, **kwargs: object) -> _FakeResponse:
        self.calls.append((url, kwargs))
        if self.error is not None:
            raise self.error
        return self.response


def _valid_scenario(**overrides: object) -> Scenario:
    """A scenario that passes every check unless fields are overridden."""
    module = Module(
        "oscal_catalog_metaschema.xml",
        _module_xml((_VERSION,), "catalog", "\n", namespaced=True),
        (_VERSION,),
    )
    fields: dict[str, object] = {
        "version": _VERSION,
        "commit": _COMMIT,
        "comment": _COMMIT.encode("ascii"),
        "modules": [module],
        "ents": {"shared-constraints/allowed-values.ent": b"<!ENTITY x 'y'>\n"},
        "extras": {},
        "fault": "none",
        "prior": None,
    }
    fields.update(overrides)
    return Scenario(**fields)  # type: ignore[arg-type]


def _make_prior(dest: Path) -> dict[str, bytes]:
    dest.mkdir()
    (dest / "shared-constraints").mkdir()
    (dest / "oscal_catalog_metaschema.xml").write_bytes(b"<old/>")
    (dest / "shared-constraints" / "old.ent").write_bytes(b"old")
    (dest / "hashes.json").write_bytes(b"{}")
    return snapshot(dest)


class TestCommitAndUrl:
    """Requirement 3.1: the archive URL is built from a validated commit only."""

    def test_archive_url_format(self) -> None:
        assert ums.archive_url(_COMMIT) == (
            f"https://github.com/usnistgov/OSCAL/archive/{_COMMIT}.zip"
        )

    def test_check_commit_accepts_lowercase_hex(self) -> None:
        ums.check_commit(_COMMIT)

    def test_check_commit_rejects_malformed(self) -> None:
        for bad in [_COMMIT.upper(), _COMMIT[:7], _COMMIT[:39], _COMMIT + "0", "v1.2.0", ""]:
            try:
                ums.check_commit(bad)
            except ums.UpdateError as e:
                assert e.step == "check commit"
                assert repr(bad) in e.detail
            else:
                raise AssertionError(f"accepted {bad!r}")

    def test_archive_url_rejects_tag(self) -> None:
        try:
            ums.archive_url("v1.2.0")
        except ums.UpdateError as e:
            assert e.step == "check commit"
        else:
            raise AssertionError("archive_url accepted a tag name")


class TestDownload:
    """Requirement 3.1: verified TLS, size cap, total deadline, errors named ``download``."""

    def test_url_from_commit_and_tls_verified(self) -> None:
        fake = _FakeUrlopen(_FakeResponse(b"payload"))
        with patch.object(ums.urllib.request, "urlopen", fake):
            assert ums.download(_COMMIT) == b"payload"

        assert len(fake.calls) == 1
        url, kwargs = fake.calls[0]
        assert url == f"https://github.com/usnistgov/OSCAL/archive/{_COMMIT}.zip"
        context = kwargs["context"]
        assert isinstance(context, ums.ssl.SSLContext)
        assert context.verify_mode == ums.ssl.CERT_REQUIRED
        assert context.check_hostname is True

    def test_malformed_commit_never_opens_url(self) -> None:
        fake = _FakeUrlopen()
        with patch.object(ums.urllib.request, "urlopen", fake):
            try:
                ums.download("main")
            except ums.UpdateError as e:
                assert e.step == "check commit"
            else:
                raise AssertionError("download accepted a branch name")
        assert fake.calls == []

    def _expect_download_error(self, fake: _FakeUrlopen, **kwargs: object) -> str:
        with patch.object(ums.urllib.request, "urlopen", fake):
            try:
                ums.download(_COMMIT, **kwargs)  # type: ignore[arg-type]
            except ums.UpdateError as e:
                assert e.step == "download"
                return e.detail
        raise AssertionError("download did not fail")

    def test_content_length_over_cap(self) -> None:
        fake = _FakeUrlopen(_FakeResponse(b"", {"Content-Length": "101"}))
        detail = self._expect_download_error(fake, max_bytes=100)
        assert "101" in detail
        assert "100" in detail

    def test_streamed_body_over_cap(self) -> None:
        # No Content-Length: the cap is enforced while reading
        fake = _FakeUrlopen(_FakeResponse(b"x" * 101))
        detail = self._expect_download_error(fake, max_bytes=100)
        assert "100-byte cap" in detail

    def test_body_at_cap_is_accepted(self) -> None:
        fake = _FakeUrlopen(_FakeResponse(b"x" * 100, {"Content-Length": "100"}))
        with patch.object(ums.urllib.request, "urlopen", fake):
            assert ums.download(_COMMIT, max_bytes=100) == b"x" * 100

    def test_deadline_exceeded(self) -> None:
        # The clock jumps past the deadline after the connection opens
        clock = iter([0.0, 301.0, 301.0, 301.0])
        fake_time = type("FakeTime", (), {"monotonic": staticmethod(lambda: next(clock))})
        fake = _FakeUrlopen(_FakeResponse(b"x" * 10))
        with patch.object(ums, "time", fake_time):
            detail = self._expect_download_error(fake, deadline_s=300.0)
        assert "300 s" in detail

    def test_socket_timeout(self) -> None:
        detail = self._expect_download_error(_FakeUrlopen(error=TimeoutError("timed out")))
        assert "300 s" in detail

    def test_url_error(self) -> None:
        err = ums.urllib.error.URLError("name resolution failed")
        detail = self._expect_download_error(_FakeUrlopen(error=err))
        assert "name resolution failed" in detail
        assert _COMMIT in detail


class TestSelectMembers:
    """Requirement 3.3: the zip comment must equal the commit."""

    def test_different_sha_comment_rejected_and_bundle_untouched(self) -> None:
        other = "0" * 40
        s = _valid_scenario(comment=other.encode("ascii"))
        try:
            ums.select_members(build_archive(s), _COMMIT)
        except ums.UpdateError as e:
            assert e.step == "check zip comment"
            assert _COMMIT in e.detail
            assert other in e.detail
        else:
            raise AssertionError("accepted a different-SHA zip comment")

        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "oscal_metaschema"
            before = _make_prior(dest)
            status, stderr = run_main(s, dest)
            assert status == 1
            assert stderr.startswith("ERROR: check zip comment: ")
            assert snapshot(dest) == before
            assert [p.name for p in Path(tmp).iterdir()] == ["oscal_metaschema"]


class TestCli:
    """Requirements 3.1, 3.3: the CLI exits 1 and names the failed step."""

    def _assert_fails(self, s: Scenario, step: str) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "oscal_metaschema"
            before = _make_prior(dest)
            status, stderr = run_main(s, dest)
            assert status == 1
            assert stderr.startswith(f"ERROR: {step}: "), stderr
            assert snapshot(dest) == before

    def test_check_commit(self) -> None:
        self._assert_fails(_valid_scenario(commit="v1.2.0"), "check commit")

    def test_download(self) -> None:
        self._assert_fails(_valid_scenario(fault="http-error"), "download")

    def test_open_archive(self) -> None:
        self._assert_fails(_valid_scenario(fault="bad-zip"), "open archive")

    def test_check_zip_comment(self) -> None:
        self._assert_fails(_valid_scenario(comment=b""), "check zip comment")

    def test_select_members_no_modules(self) -> None:
        self._assert_fails(_valid_scenario(modules=[]), "select members")

    def test_select_members_no_ents(self) -> None:
        self._assert_fails(_valid_scenario(ents={}), "select members")

    def test_check_schema_version(self) -> None:
        bad = Module(
            "oscal_catalog_metaschema.xml",
            _module_xml(("9.9.9",), "catalog", "\n", namespaced=True),
            ("9.9.9",),
        )
        self._assert_fails(_valid_scenario(modules=[bad]), "check schema-version")

    def test_success_prints_rehash_reminder(self) -> None:
        s = _valid_scenario()
        archive = build_archive(s)
        out, err = io.StringIO(), io.StringIO()
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "oscal_metaschema"
            argv = ["--version", _VERSION, "--commit", _COMMIT, "--dest", str(dest)]
            with (
                patch.object(ums, "download", lambda _commit, **_kw: archive),
                contextlib.redirect_stdout(out),
                contextlib.redirect_stderr(err),
            ):
                status = ums.main(argv)
            assert status == 0
            assert err.getvalue() == ""
            assert "hatch run rehash" in out.getvalue()
            assert (dest / "oscal_catalog_metaschema.xml").read_bytes() == s.modules[0].data
