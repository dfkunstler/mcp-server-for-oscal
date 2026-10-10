"""Stdio smoke test: run the real server process and talk raw JSON-RPC to it over stdio.

The test covers both MCP protocol eras the SDK serves, one subprocess launch per era,
because the SDK locks the era on the first request of a connection:

- Modern exchange (primary): ``server/discover`` then paged ``tools/list``, every request
  stamped with the modern per-request ``_meta`` envelope.
- Handshake exchange (legacy compat): ``initialize``, ``notifications/initialized``, then
  paged ``tools/list``.

Launch modes, in precedence order (see ``resolve_launch``):

- ``cmd``: ``OSCAL_SMOKE_SERVER_CMD`` holds a JSON array of strings, used verbatim as argv.
- ``bundle``: ``OSCAL_SMOKE_BUNDLE_DIR`` points at an unpacked MCP Bundle. The argv and env
  come from the bundle manifest's ``server.mcp_config``, the way a desktop host launches it.
- ``source``: the ``mcp-server-for-oscal`` console script installed in the running
  interpreter's environment (the default under ``hatch test``).

The server always runs with a sanitized environment (``sanitized_env``) so the test needs
no network access and no AWS credentials.
"""

from __future__ import annotations

import contextlib
import copy
import json
import logging
import os
import queue
import re
import shutil
import signal
import subprocess  # nosec B404 # launches the server under test with a fixed argv, no shell
import sys
import sysconfig
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, Literal, NoReturn, Self

import pytest
from hypothesis import assume, given, settings
from hypothesis import strategies as st
from mcp_types import (
    CLIENT_CAPABILITIES_META_KEY,
    CLIENT_INFO_META_KEY,
    PROTOCOL_VERSION_META_KEY,
)
from mcp_types.version import LATEST_HANDSHAKE_VERSION, LATEST_MODERN_VERSION

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping
    from types import TracebackType

pytestmark = pytest.mark.integration
logger = logging.getLogger(__name__)

SMOKE_CMD_ENV = "OSCAL_SMOKE_SERVER_CMD"  # JSON array, used verbatim
SMOKE_BUNDLE_ENV = "OSCAL_SMOKE_BUNDLE_DIR"  # Unpacked_Bundle_Dir

CONSOLE_SCRIPT = "mcp-server-for-oscal"
BUNDLE_DIRNAME_PLACEHOLDER = "${__dirname}"

# Prefixes and keys removed from the inherited environment. VIRTUAL_ENV and UV_CONSTRAINT
# leak in from the hatch-test env; a desktop host wouldn't set them, and `uv run` in bundle
# mode warns on a mismatched VIRTUAL_ENV.
_DROPPED_ENV_PREFIXES = ("AWS_", "BEDROCK_")
_DROPPED_ENV_KEYS = frozenset({"VIRTUAL_ENV", "UV_CONSTRAINT"})

# Forced values. Empty strings (rather than absent keys) stop a developer's repo `.env`
# from leaking in, because load_dotenv() doesn't override keys that already exist.
# Names match src/mcp_server_for_oscal/config.py.
FORCED_ENV: Mapping[str, str] = {
    "OSCAL_KB_ID": "",
    "OSCAL_DOCUMENTS_DIR": "",
    "OSCAL_STORE_DB_PATH": "",
    "OSCAL_MCP_TRANSPORT": "stdio",
    "OSCAL_ALLOW_REMOTE_URIS": "false",
    "AWS_EC2_METADATA_DISABLED": "true",
    "LOG_LEVEL": "INFO",
    "PYTHONUNBUFFERED": "1",
}


@dataclass(frozen=True)
class LaunchSpec:
    """How to start the server subprocess."""

    argv: list[str]
    env_overrides: dict[str, str]  # merged over the sanitized base env
    mode: Literal["cmd", "bundle", "source"]
    bundle_manifest: dict[str, Any] | None = None


def resolve_launch(environ: Mapping[str, str]) -> LaunchSpec:
    """Pick the server launch command. Precedence: CMD > BUNDLE_DIR > console script.

    An empty value counts as unset. BUNDLE_DIR may be relative (CI passes ``bundle``), so it
    is resolved against the pytest process cwd before substitution; the server itself runs
    with ``cwd=tmp_path``. Resolving in Python also avoids Git Bash ``$PWD`` values like
    ``/d/a/...`` on Windows.
    """
    raw_cmd = environ.get(SMOKE_CMD_ENV, "")
    if raw_cmd:
        return LaunchSpec(argv=_parse_cmd(raw_cmd), env_overrides={}, mode="cmd")

    raw_bundle = environ.get(SMOKE_BUNDLE_ENV, "")
    if raw_bundle:
        bundle_dir = Path(raw_bundle).resolve()
        manifest_path = bundle_dir / "manifest.json"
        if not manifest_path.is_file():
            pytest.fail(
                f"{SMOKE_BUNDLE_ENV}={raw_bundle!r} resolves to {bundle_dir}, "
                f"which has no manifest.json; point it at an unpacked MCP Bundle directory"
            )
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        return bundle_launch(bundle_dir, manifest)

    return source_launch()


def _parse_cmd(raw: str) -> list[str]:
    """Parse the CMD env var: a non-empty JSON array of strings."""
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as e:
        pytest.fail(f"{SMOKE_CMD_ENV} is not valid JSON ({e}); expected a JSON array of strings")
    if not (isinstance(value, list) and value and all(isinstance(v, str) for v in value)):
        pytest.fail(f"{SMOKE_CMD_ENV} must be a non-empty JSON array of strings, got {raw!r}")
    return list(value)


def bundle_launch(bundle_dir: Path, manifest: Mapping[str, Any]) -> LaunchSpec:
    """Build argv/env from the manifest's ``server.mcp_config`` the way an MCPB host does.

    ``bundle_dir`` must be absolute. ``${__dirname}`` in command and args is replaced with
    ``str(bundle_dir)``, with no shell splitting, so paths with spaces stay one argument.
    ``${user_config.*}`` env values are left literal so the bundle's ``src/server.py``
    unset-placeholder stripping is exercised. argv[0] is resolved with ``shutil.which``
    (``uv`` -> ``uv.exe`` on Windows), falling back to the literal command.
    """
    mcp_config = manifest["server"]["mcp_config"]
    dirname = str(bundle_dir)
    command = str(mcp_config["command"]).replace(BUNDLE_DIRNAME_PLACEHOLDER, dirname)
    args = [str(a).replace(BUNDLE_DIRNAME_PLACEHOLDER, dirname) for a in mcp_config.get("args", [])]
    env = {str(k): str(v) for k, v in mcp_config.get("env", {}).items()}
    return LaunchSpec(
        argv=[shutil.which(command) or command, *args],
        env_overrides=env,
        mode="bundle",
        bundle_manifest=dict(manifest),
    )


def source_launch() -> LaunchSpec:
    """Launch the installed console script from the running interpreter's scripts dir.

    Falls back to ``shutil.which``. The hatch-test env installs the project, so the script
    exists without calling hatch.
    """
    name = CONSOLE_SCRIPT + (".exe" if sys.platform == "win32" else "")
    candidate = Path(sysconfig.get_path("scripts")) / name
    if candidate.is_file():
        return LaunchSpec(argv=[str(candidate)], env_overrides={}, mode="source")
    found = shutil.which(CONSOLE_SCRIPT)
    if found:
        return LaunchSpec(argv=[found], env_overrides={}, mode="source")
    pytest.fail(
        f"console script {CONSOLE_SCRIPT!r} not found in {candidate.parent} or on PATH; "
        f"run under `hatch test` (which installs the project) or set {SMOKE_CMD_ENV} "
        f"or {SMOKE_BUNDLE_ENV}"
    )


def sanitized_env(base: Mapping[str, str]) -> dict[str, str]:
    """Return a copy of ``base`` that is offline and deterministic for the server.

    Drops ``AWS_*``, ``BEDROCK_*``, ``VIRTUAL_ENV``, and ``UV_CONSTRAINT``, then forces the
    values in ``FORCED_ENV``. ``base`` is never mutated.
    """
    env = {
        k: v
        for k, v in base.items()
        if not k.startswith(_DROPPED_ENV_PREFIXES) and k not in _DROPPED_ENV_KEYS
    }
    env.update(FORCED_ENV)
    return env


# --- Quick example tests for the launch helpers -------------------------------------------
# Property tests for bundle substitution and the sanitized env live further down (tasks
# 7.2, 7.3); these only pin the precedence and the failure messages.


def test_resolve_launch_cmd_takes_precedence(tmp_path: Path) -> None:
    spec = resolve_launch({SMOKE_CMD_ENV: '["srv", "--x"]', SMOKE_BUNDLE_ENV: str(tmp_path)})
    assert spec == LaunchSpec(argv=["srv", "--x"], env_overrides={}, mode="cmd")


@pytest.mark.parametrize("raw", ["not json", '"srv"', "[]", '["srv", 1]'])
def test_resolve_launch_rejects_bad_cmd(raw: str) -> None:
    with pytest.raises(pytest.fail.Exception, match=SMOKE_CMD_ENV):
        resolve_launch({SMOKE_CMD_ENV: raw})


def test_resolve_launch_bundle_without_manifest_fails(tmp_path: Path) -> None:
    with pytest.raises(pytest.fail.Exception, match=r"no manifest\.json"):
        resolve_launch({SMOKE_BUNDLE_ENV: str(tmp_path)})


def test_resolve_launch_defaults_to_source() -> None:
    spec = resolve_launch({})
    assert spec.mode == "source"
    assert Path(spec.argv[0]).name.startswith(CONSOLE_SCRIPT)


def test_resolve_launch_bundle_beats_source_and_resolves_relative_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle_dir = tmp_path / "bundle"
    bundle_dir.mkdir()
    manifest = {
        "server": {
            "mcp_config": {
                "command": _NONEXISTENT_COMMAND,
                "args": ["run", f"{BUNDLE_DIRNAME_PLACEHOLDER}/src/server.py"],
                "env": {"OSCAL_KB_ID": "${user_config.kb_id}", "LOG_LEVEL": "INFO"},
            }
        }
    }
    (bundle_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    spec = resolve_launch({SMOKE_BUNDLE_ENV: "bundle"})

    absolute = bundle_dir.resolve()  # tmp_path may sit behind a symlink (macOS /var)
    assert absolute.is_absolute()
    assert spec.mode == "bundle"
    assert spec.argv == [_NONEXISTENT_COMMAND, "run", f"{absolute}/src/server.py"]
    assert spec.bundle_manifest == manifest
    assert spec.env_overrides == manifest["server"]["mcp_config"]["env"]


def test_resolve_launch_empty_values_count_as_unset(tmp_path: Path) -> None:
    assert resolve_launch({SMOKE_CMD_ENV: "", SMOKE_BUNDLE_ENV: ""}).mode == "source"
    # An empty CMD doesn't shadow a set BUNDLE_DIR (which then fails on the missing manifest).
    with pytest.raises(pytest.fail.Exception, match=r"no manifest\.json"):
        resolve_launch({SMOKE_CMD_ENV: "", SMOKE_BUNDLE_ENV: str(tmp_path)})


def test_resolve_launch_missing_console_script_names_script_and_env_vars(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    empty_scripts = tmp_path / "no-scripts"
    monkeypatch.setattr(sysconfig, "get_path", lambda *_a, **_k: str(empty_scripts))
    monkeypatch.setattr(shutil, "which", lambda *_a, **_k: None)

    with pytest.raises(pytest.fail.Exception) as excinfo:
        resolve_launch({})

    message = str(excinfo.value)
    assert repr(CONSOLE_SCRIPT) in message
    assert str(empty_scripts) in message
    assert SMOKE_CMD_ENV in message
    assert SMOKE_BUNDLE_ENV in message


# --- Property tests for the launch helpers ------------------------------------------------

_NONEXISTENT_COMMAND = "definitely-not-a-real-cmd-xyz"  # shutil.which misses, argv[0] stays literal
_BUNDLE_ARG_LITERALS = (
    "run",
    "--frozen",
    "--directory",
    "src/server.py",
    BUNDLE_DIRNAME_PLACEHOLDER,
    f"{BUNDLE_DIRNAME_PLACEHOLDER}/src/server.py",
)
_USER_CONFIG_ENV = {
    "OSCAL_KB_ID": "${user_config.kb_id}",
    "AWS_PROFILE": "${user_config.aws_profile}",
    "LOG_LEVEL": "INFO",
}


@settings(max_examples=100)
@given(
    dir_text=st.text(
        # The utf-8 codec excludes lone surrogates.
        alphabet=st.characters(codec="utf-8", exclude_characters="\x00"),
        min_size=1,
    ),
    args=st.lists(st.sampled_from(_BUNDLE_ARG_LITERALS)),
)
def test_bundle_launch_substitutes_dirname_faithfully(dir_text: str, args: list[str]) -> None:
    """Feature: python-version-upgrade, Property 3: Bundle launch substitutes the directory faithfully

    bundle_launch never touches the filesystem except shutil.which on the command, so a bare
    Path built from arbitrary text (spaces, non-ASCII, backslashes) is enough.

    **Validates: Requirements 9.17, 9.18, 10.7**
    """
    bundle_dir = Path(dir_text)
    dirname = str(bundle_dir)
    assume(BUNDLE_DIRNAME_PLACEHOLDER not in dirname)
    manifest = {
        "server": {
            "mcp_config": {
                "command": _NONEXISTENT_COMMAND,
                "args": list(args),
                "env": dict(_USER_CONFIG_ENV),
            }
        }
    }

    spec = bundle_launch(bundle_dir, manifest)

    assert spec.mode == "bundle"
    assert spec.argv[0] == _NONEXISTENT_COMMAND
    # Spelled out per literal rather than mirroring the helper's str.replace.
    substituted = {
        BUNDLE_DIRNAME_PLACEHOLDER: dirname,
        f"{BUNDLE_DIRNAME_PLACEHOLDER}/src/server.py": dirname + "/src/server.py",
    }
    expected = [substituted.get(a, a) for a in args]
    assert spec.argv[1:] == expected
    assert len(spec.argv) == len(args) + 1
    assert all(BUNDLE_DIRNAME_PLACEHOLDER not in a for a in spec.argv[1:])
    assert spec.env_overrides == _USER_CONFIG_ENV


_SENSITIVE_ENV_KEYS = (
    "AWS_SECRET_ACCESS_KEY",
    "AWS_ACCESS_KEY_ID",
    "AWS_SESSION_TOKEN",
    "AWS_PROFILE",
    "AWS_REGION",
    "AWS_EC2_METADATA_DISABLED",
    "BEDROCK_MODEL_ID",
    "OSCAL_KB_ID",
    "OSCAL_DOCUMENTS_DIR",
    "OSCAL_STORE_DB_PATH",
    "OSCAL_MCP_TRANSPORT",
    "VIRTUAL_ENV",
    "UV_CONSTRAINT",
    "LOG_LEVEL",
)
_env_text = st.text(max_size=20)


@st.composite
def _base_envs(draw: st.DrawFn) -> dict[str, str]:
    """Arbitrary env mappings, always seeded with some sensitive and forced keys."""
    base = draw(st.dictionaries(_env_text, _env_text, max_size=10))
    sensitive = draw(st.dictionaries(st.sampled_from(_SENSITIVE_ENV_KEYS), _env_text, min_size=1))
    prefixed = draw(
        st.dictionaries(
            st.builds("{}{}".format, st.sampled_from(("AWS_", "BEDROCK_")), _env_text),
            _env_text,
            max_size=5,
        )
    )
    return {**base, **prefixed, **sensitive}


@settings(max_examples=100)
@given(base=_base_envs())
def test_sanitized_env_is_offline_and_deterministic(base: dict[str, str]) -> None:
    """Feature: python-version-upgrade, Property 4: Sanitized environment is offline and deterministic

    **Validates: Requirements 9.19**
    """
    snapshot = dict(base)

    env = sanitized_env(base)

    assert base == snapshot  # not mutated
    leaked = [
        k
        for k in env
        if (k.startswith("AWS_") and k != "AWS_EC2_METADATA_DISABLED")
        or k.startswith("BEDROCK_")
        or k in {"VIRTUAL_ENV", "UV_CONSTRAINT"}
    ]
    assert leaked == []
    assert env["OSCAL_KB_ID"] == ""
    assert env["OSCAL_DOCUMENTS_DIR"] == ""
    assert env["OSCAL_MCP_TRANSPORT"] == "stdio"
    for key, value in FORCED_ENV.items():
        assert env[key] == value
    kept = {
        k: v
        for k, v in base.items()
        if k not in FORCED_ENV
        and not k.startswith(("AWS_", "BEDROCK_"))
        and k not in {"VIRTUAL_ENV", "UV_CONSTRAINT"}
    }
    assert {k: v for k, v in env.items() if k not in FORCED_ENV} == kept


# --- Modern envelope stamping (pure) ------------------------------------------------------

Era = Literal["modern", "handshake"]
SMOKE_CLIENT_INFO = {"name": "oscal-smoke-test", "version": "0"}
SMOKE_CLIENT_CAPABILITIES: dict[str, Any] = {}  # what ClientCapabilities() dumps to


def stamp_modern_envelope(
    params: Mapping[str, Any] | None,
    *,
    version: str = LATEST_MODERN_VERSION,
    client_info: Mapping[str, Any] = SMOKE_CLIENT_INFO,
    capabilities: Mapping[str, Any] = SMOKE_CLIENT_CAPABILITIES,
) -> dict[str, Any]:
    """Return a new params dict carrying the modern per-request envelope.

    Mirrors ``mcp.client.session._make_modern_stamp``: copy params (never mutate the
    caller's dict), copy any caller ``_meta``, then set the three reserved keys, overwriting
    caller values for them. Non-reserved ``_meta`` keys (for example ``progressToken``) and
    all other params (for example ``cursor``) pass through unchanged. ``client_info`` and
    ``capabilities`` are copied so a caller can't alias the module constants.
    """
    stamped: dict[str, Any] = dict(params or {})
    meta: dict[str, Any] = dict(stamped.get("_meta") or {})
    meta[PROTOCOL_VERSION_META_KEY] = version
    meta[CLIENT_INFO_META_KEY] = dict(client_info)
    meta[CLIENT_CAPABILITIES_META_KEY] = dict(capabilities)
    stamped["_meta"] = meta
    return stamped


def test_stamp_modern_envelope_defaults() -> None:
    stamped = stamp_modern_envelope(None)
    assert stamped == {
        "_meta": {
            PROTOCOL_VERSION_META_KEY: LATEST_MODERN_VERSION,
            CLIENT_INFO_META_KEY: SMOKE_CLIENT_INFO,
            CLIENT_CAPABILITIES_META_KEY: SMOKE_CLIENT_CAPABILITIES,
        }
    }
    assert stamped["_meta"][CLIENT_INFO_META_KEY] is not SMOKE_CLIENT_INFO


def test_stamp_modern_envelope_matches_sdk_client_stamp() -> None:
    """The envelope equals what the SDK's own client stamps, with no hard-coded strings.

    The SDK client negotiates the newest mutual version from ``MODERN_PROTOCOL_VERSIONS``
    (ordered oldest to newest), so against a current server it stamps the last entry.
    """
    import mcp_types
    from mcp.client import session as sdk_session
    from mcp_types.version import MODERN_PROTOCOL_VERSIONS

    assert PROTOCOL_VERSION_META_KEY is mcp_types.PROTOCOL_VERSION_META_KEY
    assert CLIENT_INFO_META_KEY is mcp_types.CLIENT_INFO_META_KEY
    assert CLIENT_CAPABILITIES_META_KEY is mcp_types.CLIENT_CAPABILITIES_META_KEY
    assert sdk_session.PROTOCOL_VERSION_META_KEY == PROTOCOL_VERSION_META_KEY
    assert sdk_session.CLIENT_INFO_META_KEY == CLIENT_INFO_META_KEY
    assert sdk_session.CLIENT_CAPABILITIES_META_KEY == CLIENT_CAPABILITIES_META_KEY
    assert MODERN_PROTOCOL_VERSIONS[-1] == LATEST_MODERN_VERSION

    sdk_stamp = sdk_session._make_modern_stamp(
        LATEST_MODERN_VERSION,
        dict(SMOKE_CLIENT_INFO),
        dict(SMOKE_CLIENT_CAPABILITIES),
        lambda _name, _args: {},
    )
    data: dict[str, Any] = {"method": "tools/list", "params": {"cursor": "c1"}}
    sdk_stamp(data, {})  # type: ignore[arg-type] # CallOptions is a TypedDict; {} is valid

    assert stamp_modern_envelope({"cursor": "c1"}) == data["params"]


def test_stamp_modern_envelope_preserves_params_and_overrides_reserved() -> None:
    params = {"cursor": "c1", "_meta": {"progressToken": 7, PROTOCOL_VERSION_META_KEY: "old"}}
    snapshot = json.loads(json.dumps(params))

    stamped = stamp_modern_envelope(params, version="v-test")

    assert params == snapshot  # not mutated
    assert stamped["cursor"] == "c1"
    assert stamped["_meta"]["progressToken"] == 7
    assert stamped["_meta"][PROTOCOL_VERSION_META_KEY] == "v-test"
    assert stamped["_meta"] is not params["_meta"]


_RESERVED_META_KEYS = (
    PROTOCOL_VERSION_META_KEY,
    CLIENT_INFO_META_KEY,
    CLIENT_CAPABILITIES_META_KEY,
)
_json_values = st.recursive(
    st.none() | st.booleans() | st.integers() | st.text(max_size=10),
    lambda children: (
        st.lists(children, max_size=3) | st.dictionaries(st.text(max_size=5), children, max_size=3)
    ),
    max_leaves=8,
)
_json_objects = st.dictionaries(st.text(max_size=10), _json_values, max_size=4)


@st.composite
def _envelope_params(draw: st.DrawFn) -> dict[str, Any] | None:
    """None, or JSON-like params with an optional caller ``_meta`` that may hit reserved keys."""
    if draw(st.booleans()):
        return None
    params: dict[str, Any] = draw(
        st.dictionaries(st.text(max_size=10).filter(lambda k: k != "_meta"), _json_values)
    )
    if draw(st.booleans()):
        params["cursor"] = draw(st.text(max_size=10))
    if draw(st.booleans()):
        meta = draw(_json_objects)
        meta.update(draw(st.dictionaries(st.sampled_from(_RESERVED_META_KEYS), _json_values)))
        params["_meta"] = meta
    return params


@settings(max_examples=100)
@given(
    params=_envelope_params(),
    version=st.none() | st.text(max_size=20),
    client_info=st.none() | _json_objects,
    capabilities=st.none() | _json_objects,
)
def test_stamp_modern_envelope_preserves_params_and_sets_envelope(
    params: dict[str, Any] | None,
    version: str | None,
    client_info: dict[str, Any] | None,
    capabilities: dict[str, Any] | None,
) -> None:
    """Feature: python-version-upgrade, Property 5: Modern envelope stamping preserves params and always sets the envelope

    A None draw for version, client_info, or capabilities omits that argument, so the
    defaults are exercised too.

    **Validates: Requirements 9.2, 9.3, 9.5, 9.7**
    """
    snapshot = copy.deepcopy(params)
    kwargs: dict[str, Any] = {}
    if version is not None:
        kwargs["version"] = version
    if client_info is not None:
        kwargs["client_info"] = client_info
    if capabilities is not None:
        kwargs["capabilities"] = capabilities

    stamped = stamp_modern_envelope(params, **kwargs)

    assert params == snapshot  # not mutated, including the caller's _meta
    source = snapshot or {}
    assert {k: v for k, v in stamped.items() if k != "_meta"} == {
        k: v for k, v in source.items() if k != "_meta"
    }
    caller_meta = source.get("_meta") or {}
    meta = stamped["_meta"]
    assert {k: v for k, v in meta.items() if k not in _RESERVED_META_KEYS} == {
        k: v for k, v in caller_meta.items() if k not in _RESERVED_META_KEYS
    }
    assert meta[PROTOCOL_VERSION_META_KEY] == (
        LATEST_MODERN_VERSION if version is None else version
    )
    assert meta[CLIENT_INFO_META_KEY] == (SMOKE_CLIENT_INFO if client_info is None else client_info)
    assert meta[CLIENT_CAPABILITIES_META_KEY] == (
        SMOKE_CLIENT_CAPABILITIES if capabilities is None else capabilities
    )


# --- Pure response reader -----------------------------------------------------------------


class SmokeTimeoutError(AssertionError):
    """No matching response arrived within the timeout."""


class SmokeProtocolError(AssertionError):
    """The server broke the stdio contract: non-JSON stdout, or EOF before the response."""


class _Eof:
    """Type of the ``EOF`` sentinel the stdout reader thread enqueues when stdout closes."""

    __slots__ = ()

    def __repr__(self) -> str:
        return "EOF"


EOF: Final = _Eof()
StdoutQueue = queue.Queue[bytes | _Eof]


def _with_stderr(message: str, stderr_text: str) -> str:
    """Append the server's stderr to a failure message, under a fixed separator line."""
    return f"{message}\n--- server stderr ---\n{stderr_text}"


def _failure(context: str, detail: str, stderr: Callable[[], str]) -> str:
    """Format a reader failure: optional context prefix, detail, then the server's stderr."""
    prefix = f"{context}: " if context else ""
    return _with_stderr(f"{prefix}{detail}", stderr())


def read_response(
    lines: StdoutQueue,
    req_id: int,
    timeout: float,
    stderr: Callable[[], str],
    clock: Callable[[], float] = time.monotonic,
    *,
    context: str = "",
) -> dict[str, Any]:
    """Return the JSON-RPC message whose ``id`` equals ``req_id``.

    - Each item is one raw stdout line. The trailing ``\\r\\n`` or ``\\n`` is stripped first
      (on Windows the server's stdout TextIOWrapper writes ``\\r\\n``), then the line is
      decoded as UTF-8.
    - Blank lines are skipped. They carry no JSON-RPC message and the SDK doesn't emit
      them, so tolerating one costs nothing and avoids a spurious failure.
    - Server notifications (no ``id``) and responses with other ids are skipped. A boolean
      ``id`` never matches, even though ``True == 1`` in Python.
    - Raises ``SmokeProtocolError`` if a line isn't a UTF-8 JSON object (stdout must carry
      only JSON-RPC) or if ``EOF`` arrives first.
    - Raises ``SmokeTimeoutError`` once the total wait, measured with ``clock``, reaches
      ``timeout``. The deadline is checked before every ``get``, so a clock that has jumped
      past it raises without blocking.

    Every error message starts with ``context`` (for example ``"modern tools/list (id 2)"``)
    when given, and ends with the stderr text read by calling ``stderr()`` at raise time.
    """
    start = clock()
    deadline = start + timeout
    while True:
        remaining = deadline - clock()
        if remaining <= 0:
            raise SmokeTimeoutError(
                _failure(
                    context,
                    f"no response for id {req_id} after {clock() - start:.1f} s "
                    f"(timeout {timeout:.1f} s)",
                    stderr,
                )
            )
        try:
            item = lines.get(timeout=remaining)
        except queue.Empty:
            continue  # loop back to the deadline check, which raises
        if isinstance(item, _Eof):
            raise SmokeProtocolError(
                _failure(context, f"server closed stdout before responding to id {req_id}", stderr)
            )
        raw = item.removesuffix(b"\n").removesuffix(b"\r")
        if not raw.strip():
            continue
        try:
            message = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as e:
            raise SmokeProtocolError(
                _failure(context, f"stdout line is not JSON ({e}): {raw!r}", stderr)
            ) from e
        if not isinstance(message, dict):
            raise SmokeProtocolError(
                _failure(context, f"stdout line is not a JSON object: {raw!r}", stderr)
            )
        msg_id = message.get("id")
        if isinstance(msg_id, int) and not isinstance(msg_id, bool) and msg_id == req_id:
            return message


# --- Quick example tests for the response reader ------------------------------------------
# Property tests for the reader are tasks 7.7 and 7.8.

_STDERR_TEXT = "boom: tampered schema\n"


def _queue_of(*items: bytes | _Eof) -> StdoutQueue:
    q: StdoutQueue = queue.Queue()
    for item in items:
        q.put(item)
    return q


def _line(message: Mapping[str, Any], ending: bytes = b"\n") -> bytes:
    return json.dumps(message).encode("utf-8") + ending


def test_read_response_skips_notifications_and_other_ids() -> None:
    target = {"jsonrpc": "2.0", "id": 2, "result": {"tools": []}}
    lines = _queue_of(
        _line({"jsonrpc": "2.0", "method": "notifications/message", "params": {}}),
        b"\r\n",
        _line({"jsonrpc": "2.0", "id": 1, "result": {}}, b"\r\n"),
        _line({"jsonrpc": "2.0", "id": True, "result": {}}),
        _line(target, b"\r\n"),
    )

    assert read_response(lines, 2, 5.0, lambda: _STDERR_TEXT) == target


def test_read_response_eof_raises_with_stderr_and_context() -> None:
    lines = _queue_of(_line({"jsonrpc": "2.0", "id": 1, "result": {}}), EOF)

    with pytest.raises(SmokeProtocolError, match="closed stdout") as excinfo:
        read_response(lines, 2, 5.0, lambda: _STDERR_TEXT, context="modern tools/list (id 2)")

    assert _STDERR_TEXT in str(excinfo.value)
    assert str(excinfo.value).startswith("modern tools/list (id 2): ")


def test_read_response_non_json_line_raises() -> None:
    with pytest.raises(SmokeProtocolError, match="not JSON") as excinfo:
        read_response(_queue_of(b"INFO starting server\n"), 1, 5.0, lambda: _STDERR_TEXT)
    assert _STDERR_TEXT in str(excinfo.value)

    with pytest.raises(SmokeProtocolError, match="not a JSON object"):
        read_response(_queue_of(b"[1, 2]\n"), 1, 5.0, lambda: _STDERR_TEXT)


def test_read_response_timeout_with_fake_clock_does_not_wait() -> None:
    ticks = iter([0.0, 100.0, 100.0])  # start, deadline check, elapsed in the message
    lines = _queue_of(_line({"jsonrpc": "2.0", "id": 1, "result": {}}))

    real_start = time.monotonic()
    with pytest.raises(SmokeTimeoutError, match="no response for id 2") as excinfo:
        read_response(lines, 2, 60.0, lambda: _STDERR_TEXT, clock=lambda: next(ticks))

    assert time.monotonic() - real_start < 1.0
    assert _STDERR_TEXT in str(excinfo.value)
    assert lines.qsize() == 1  # raised before reading anything


# --- Property tests for the response reader -----------------------------------------------

_line_endings = st.sampled_from((b"\n", b"\r\n"))
_blank_lines = st.sampled_from((b"\n", b"\r\n", b"  \n", b"\t\r\n"))


def _notifications() -> st.SearchStrategy[dict[str, Any]]:
    return st.builds(
        lambda method, params: {"jsonrpc": "2.0", "method": method, "params": params},
        st.text(max_size=20),
        _json_objects,
    )


def _responses(ids: st.SearchStrategy[Any]) -> st.SearchStrategy[dict[str, Any]]:
    """Result or error responses. Bool ids are left out (they never match by design)."""
    body = st.one_of(
        st.builds(lambda r: {"result": r}, _json_values),
        st.builds(lambda c, m: {"error": {"code": c, "message": m}}, st.integers(), st.text()),
    )
    return st.builds(lambda i, b: {"jsonrpc": "2.0", "id": i, **b}, ids, body)


@st.composite
def _stdout_with_one_match(draw: st.DrawFn) -> tuple[int, dict[str, Any], list[bytes]]:
    """A request id, its response, and stdout lines holding that response exactly once."""
    req_id = draw(st.integers(min_value=0, max_value=2**31))
    target = draw(_responses(st.just(req_id)))
    other_ids = st.one_of(
        st.integers().filter(lambda i: i != req_id),
        st.text(max_size=10),  # string ids, including str(req_id), never match an int
    )
    noise = draw(
        st.lists(
            st.one_of(
                st.tuples(_notifications(), _line_endings).map(lambda t: _line(*t)),
                st.tuples(_responses(other_ids), _line_endings).map(lambda t: _line(*t)),
                _blank_lines,
            ),
            max_size=12,
        )
    )
    position = draw(st.integers(min_value=0, max_value=len(noise)))
    lines = [*noise[:position], _line(target, draw(_line_endings)), *noise[position:]]
    return req_id, target, lines


@settings(max_examples=100)
@given(case=_stdout_with_one_match())
def test_read_response_returns_exactly_the_matching_response(
    case: tuple[int, dict[str, Any], list[bytes]],
) -> None:
    """Feature: python-version-upgrade, Property 1: Response reader returns exactly the matching response

    The target is always queued, so the 5 s real timeout never elapses.

    **Validates: Requirements 9.1, 9.3, 9.5, 9.9, 9.10**
    """
    req_id, target, lines = case
    expected = copy.deepcopy(target)

    message = read_response(_queue_of(*lines), req_id, 5.0, lambda: _STDERR_TEXT)

    assert message == expected


@st.composite
def _stdout_without_match(draw: st.DrawFn) -> tuple[int, list[bytes]]:
    """A request id and stdout noise lines that never answer it."""
    req_id = draw(st.integers(min_value=0, max_value=2**31))
    other_ids = st.one_of(st.integers().filter(lambda i: i != req_id), st.text(max_size=10))
    noise = draw(
        st.lists(
            st.one_of(
                st.tuples(_notifications(), _line_endings).map(lambda t: _line(*t)),
                st.tuples(_responses(other_ids), _line_endings).map(lambda t: _line(*t)),
                _blank_lines,
            ),
            max_size=12,
        )
    )
    return req_id, noise


_PAST_DEADLINE = 1e9


@settings(max_examples=100, deadline=None)
@given(
    case=_stdout_without_match(),
    ends_in_eof=st.booleans(),
    stderr_text=st.text(),
    context=st.one_of(st.just(""), st.text(min_size=1)),
)
def test_read_response_failures_carry_stderr(
    case: tuple[int, list[bytes]],
    ends_in_eof: bool,  # noqa: FBT001 - hypothesis passes drawn values positionally by name
    stderr_text: str,
    context: str,
) -> None:
    """Feature: python-version-upgrade, Property 2: Reader failures always carry stderr

    The fake clock reads 0.0 at start and while lines remain queued, then jumps past the
    deadline once the queue is empty. So the no-EOF case drains all the noise and times out
    on the next deadline check without ever blocking in ``get``. The EOF case never reaches
    the deadline.

    **Validates: Requirements 9.12**
    """
    req_id, noise = case
    lines = _queue_of(*noise, *([EOF] if ends_in_eof else []))

    calls = 0

    def clock() -> float:
        # The first call is the start time; it must read 0.0 even for an empty queue, or
        # the deadline moves out with it and get() would block for the full timeout.
        nonlocal calls
        calls += 1
        return 0.0 if calls == 1 or not lines.empty() else _PAST_DEADLINE

    expected = SmokeProtocolError if ends_in_eof else SmokeTimeoutError
    real_start = time.monotonic()
    with pytest.raises(expected) as excinfo:
        read_response(lines, req_id, 60.0, lambda: stderr_text, clock, context=context)

    assert time.monotonic() - real_start < 1.0
    assert type(excinfo.value) is expected
    message = str(excinfo.value)
    assert stderr_text in message
    assert message.endswith(stderr_text)
    if context:
        assert message.startswith(f"{context}: ")
    assert lines.empty()  # all noise was read before failing


# --- Result checking (pure) ---------------------------------------------------------------


class SmokeRpcError(AssertionError):
    """The server answered a request with a JSON-RPC ``error`` response."""


def _as_json(value: object) -> str:
    """Render a decoded JSON value for a failure message; ``repr`` anything non-JSON."""
    return json.dumps(value, ensure_ascii=False, default=repr)


def check_result(
    response: Mapping[str, Any], *, era: Era, method: str, stderr: str
) -> dict[str, Any]:
    """Return ``response["result"]``, or raise with the era, method, and server stderr.

    - ``jsonrpc`` isn't ``"2.0"``, the response has neither or both of ``result`` and
      ``error``, or ``result`` isn't a JSON object -> ``SmokeProtocolError`` with the raw
      response.
    - ``error`` present -> ``SmokeRpcError`` whose first line reads like
      ``"handshake initialize returned JSON-RPC error -32022: connection is serving ..."``,
      followed by the error object as JSON (code, message, data). A malformed error (not
      an object, or missing ``code``/``message``) is still reported, with ``?`` for the
      missing parts.

    Every message ends with ``stderr`` under the same separator ``read_response`` uses.
    """
    where = f"{era} {method}"

    def protocol_error(detail: str) -> SmokeProtocolError:
        return SmokeProtocolError(
            _with_stderr(f"{where}: {detail}\nresponse: {_as_json(response)}", stderr)
        )

    if response.get("jsonrpc") != "2.0":
        version = _as_json(response.get("jsonrpc"))
        raise protocol_error(f'response has jsonrpc {version}, expected "2.0"')
    has_result, has_error = "result" in response, "error" in response
    if has_result == has_error:
        which = "both" if has_result else "neither"
        raise protocol_error(f"response has {which} of result and error")
    if has_error:
        error = response["error"]
        code = error.get("code", "?") if isinstance(error, dict) else "?"
        text = error.get("message", "?") if isinstance(error, dict) else "?"
        raise SmokeRpcError(
            _with_stderr(
                f"{where} returned JSON-RPC error {code}: {text}\nerror: {_as_json(error)}",
                stderr,
            )
        )
    result = response["result"]
    if not isinstance(result, dict):
        raise protocol_error(f"result is not a JSON object: {_as_json(result)}")
    return result


# --- Quick example tests for result checking ----------------------------------------------
# The property test for RPC error reports (Property 6) follows the examples.


def test_check_result_returns_result() -> None:
    result = {"tools": [{"name": "about"}], "nextCursor": "c1"}
    response = {"jsonrpc": "2.0", "id": 3, "result": result}

    assert check_result(response, era="modern", method="tools/list", stderr="") is result


def test_check_result_error_raises_with_era_method_code_and_stderr() -> None:
    error = {
        "code": -32022,
        "message": "connection is serving the modern era",
        "data": {"supported": ["2025-11-25"]},
    }
    response = {"jsonrpc": "2.0", "id": 1, "error": error}

    with pytest.raises(SmokeRpcError) as excinfo:
        check_result(response, era="handshake", method="initialize", stderr=_STDERR_TEXT)

    message = str(excinfo.value)
    assert message.splitlines()[0] == (
        "handshake initialize returned JSON-RPC error -32022: connection is serving the modern era"
    )
    assert json.dumps(error) in message
    assert message.endswith(_STDERR_TEXT)


@pytest.mark.parametrize("error", ["not an object", {"message": "no code"}, None])
def test_check_result_malformed_error_still_raises_rpc_error(error: object) -> None:
    response = {"jsonrpc": "2.0", "id": 1, "error": error}

    with pytest.raises(SmokeRpcError, match=r"^modern server/discover returned JSON-RPC error"):
        check_result(response, era="modern", method="server/discover", stderr=_STDERR_TEXT)


@pytest.mark.parametrize(
    ("response", "detail"),
    [
        ({"jsonrpc": "2.0", "id": 1}, "neither of result and error"),
        ({"jsonrpc": "2.0", "id": 1, "result": {}, "error": {}}, "both of result and error"),
        ({"jsonrpc": "1.0", "id": 1, "result": {}}, 'jsonrpc "1.0"'),
        ({"id": 1, "result": {}}, "jsonrpc null"),
        ({"jsonrpc": "2.0", "id": 1, "result": [1]}, "result is not a JSON object"),
    ],
)
def test_check_result_protocol_errors(response: dict[str, Any], detail: str) -> None:
    with pytest.raises(SmokeProtocolError) as excinfo:
        check_result(response, era="modern", method="tools/list", stderr=_STDERR_TEXT)

    message = str(excinfo.value)
    assert message.startswith("modern tools/list: ")
    assert detail in message
    assert json.dumps(response) in message
    assert message.endswith(_STDERR_TEXT)


@st.composite
def _rpc_errors(draw: st.DrawFn) -> dict[str, Any]:
    """Well-formed JSON-RPC error objects: int ``code``, str ``message``, optional ``data``."""
    error: dict[str, Any] = {"code": draw(st.integers()), "message": draw(st.text())}
    if draw(st.booleans()):
        error["data"] = draw(_json_values)
    return error


_request_ids = st.integers() | st.text(max_size=10)


@settings(max_examples=100)
@given(
    era=st.sampled_from(["modern", "handshake"]),
    method=st.text(),
    error=_rpc_errors(),
    request_id=_request_ids,
    stderr=st.text(),
    result=_json_objects,
)
def test_check_result_rpc_error_reports_carry_context(
    era: Era,
    method: str,
    error: dict[str, Any],
    request_id: int | str,
    stderr: str,
    result: dict[str, Any],
) -> None:
    """Feature: python-version-upgrade, Property 6: RPC error reports carry era, method, error, and stderr

    **Validates: Requirements 9.13**
    """
    response = {"jsonrpc": "2.0", "id": request_id, "error": error}

    with pytest.raises(SmokeRpcError) as excinfo:
        check_result(response, era=era, method=method, stderr=stderr)

    message = str(excinfo.value)
    assert era in message
    assert method in message
    assert str(error["code"]) in message
    assert error["message"] in message
    assert _as_json(error) in message
    assert message.endswith(stderr)

    # Companion: a well-formed result response returns exactly its result.
    ok = {"jsonrpc": "2.0", "id": request_id, "result": result}
    assert check_result(ok, era=era, method=method, stderr=stderr) is result


# --- Pagination (pure) --------------------------------------------------------------------

DEFAULT_MAX_PAGES = 50


def collect_tool_names(
    fetch_page: Callable[[str | None], Mapping[str, Any]],
    *,
    max_pages: int = DEFAULT_MAX_PAGES,
) -> list[str]:
    """Follow ``nextCursor`` across ``tools/list`` pages and return the names in order.

    ``fetch_page(None)`` fetches the first page and ``fetch_page(cursor)`` each later one.
    The function doesn't know the protocol era; callers bind it in the closure, for example
    ``lambda c: srv.handshake_request("tools/list", {} if c is None else {"cursor": c})``.
    Paging stops at the first page whose ``nextCursor`` is absent or null. Any other string,
    including ``""``, is an opaque cursor per the MCP spec and is followed like any other.

    Raises ``SmokeProtocolError`` (pages are numbered from 1 in messages) when:

    - a page has no ``tools`` list, a tool isn't an object with a string ``name``, or
      ``nextCursor`` is neither null nor a string;
    - a page returns a cursor already seen, so a server bug can't loop forever;
    - ``max_pages`` pages were fetched and the last still has a ``nextCursor`` (the message
      names the page count and that unfollowed cursor).

    RPC failures surface from ``fetch_page`` itself; the callers' request helpers already
    attach the server's stderr to those.
    """
    names: list[str] = []
    seen: set[str] = set()
    cursor: str | None = None
    for page_no in range(1, max_pages + 1):
        page = fetch_page(cursor)
        tools = page.get("tools")
        if not isinstance(tools, list):
            raise SmokeProtocolError(
                f"tools/list page {page_no} has no tools list: {_as_json(dict(page))}"
            )
        for index, tool in enumerate(tools):
            name = tool.get("name") if isinstance(tool, dict) else None
            if not isinstance(name, str):
                raise SmokeProtocolError(
                    f"tools/list page {page_no}, tool {index} has no string name: {_as_json(tool)}"
                )
            names.append(name)
        next_cursor = page.get("nextCursor")
        if next_cursor is None:
            return names
        if not isinstance(next_cursor, str):
            raise SmokeProtocolError(
                f"tools/list page {page_no} has a non-string nextCursor: {_as_json(next_cursor)}"
            )
        if next_cursor in seen:
            raise SmokeProtocolError(
                f"tools/list page {page_no} repeated cursor {_as_json(next_cursor)}; "
                f"the server is paging in a loop"
            )
        seen.add(next_cursor)
        cursor = next_cursor
    raise SmokeProtocolError(
        f"tools/list still has a nextCursor after {max_pages} pages "
        f"(last nextCursor {_as_json(cursor)}, not followed)"
    )


# --- Quick example tests for pagination ---------------------------------------------------


def _paged_fetch(
    pages: Mapping[str | None, Mapping[str, Any]],
) -> tuple[Callable[[str | None], Mapping[str, Any]], list[str | None]]:
    """A fake ``fetch_page`` serving ``pages`` by cursor, plus the list of cursors it got."""
    calls: list[str | None] = []

    def fetch(cursor: str | None) -> Mapping[str, Any]:
        calls.append(cursor)
        return pages[cursor]

    return fetch, calls


def _tools(*names: str) -> list[dict[str, Any]]:
    return [{"name": n, "inputSchema": {"type": "object"}} for n in names]


def test_collect_tool_names_single_page() -> None:
    fetch, calls = _paged_fetch({None: {"tools": _tools("about", "list_oscal_models")}})

    assert collect_tool_names(fetch) == ["about", "list_oscal_models"]
    assert calls == [None]


def test_collect_tool_names_follows_cursors_in_order() -> None:
    fetch, calls = _paged_fetch(
        {
            None: {"tools": _tools("a", "b"), "nextCursor": "c1"},
            "c1": {"tools": [], "nextCursor": ""},  # empty string is a real cursor
            "": {"tools": _tools("c"), "nextCursor": "c3"},
            "c3": {"tools": _tools("d"), "nextCursor": None, "resultType": "complete"},
        }
    )

    assert collect_tool_names(fetch) == ["a", "b", "c", "d"]
    assert calls == [None, "c1", "", "c3"]


def test_collect_tool_names_repeated_cursor_raises() -> None:
    fetch, calls = _paged_fetch(
        {
            None: {"tools": _tools("a"), "nextCursor": "c1"},
            "c1": {"tools": _tools("b"), "nextCursor": "c2"},
            "c2": {"tools": _tools("c"), "nextCursor": "c1"},
        }
    )

    with pytest.raises(SmokeProtocolError, match=r'page 3 repeated cursor "c1"'):
        collect_tool_names(fetch)
    assert calls == [None, "c1", "c2"]


def test_collect_tool_names_max_pages_raises() -> None:
    pages: dict[str | None, dict[str, Any]] = {None: {"tools": _tools("t0"), "nextCursor": "c1"}}
    pages |= {f"c{i}": {"tools": _tools(f"t{i}"), "nextCursor": f"c{i + 1}"} for i in range(1, 9)}
    fetch, calls = _paged_fetch(pages)

    with pytest.raises(
        SmokeProtocolError, match=r'after 3 pages \(last nextCursor "c3", not followed\)'
    ):
        collect_tool_names(fetch, max_pages=3)
    assert calls == [None, "c1", "c2"]


@pytest.mark.parametrize(
    ("page", "detail"),
    [
        ({"nextCursor": "c1"}, "page 1 has no tools list"),
        ({"tools": {"name": "a"}}, "page 1 has no tools list"),
        ({"tools": [{"name": "a"}, {"title": "no name"}]}, "page 1, tool 1 has no string name"),
        ({"tools": ["about"]}, "page 1, tool 0 has no string name"),
        ({"tools": [], "nextCursor": 7}, "page 1 has a non-string nextCursor: 7"),
    ],
)
def test_collect_tool_names_malformed_page_raises(page: dict[str, Any], detail: str) -> None:
    fetch, _ = _paged_fetch({None: page})

    with pytest.raises(SmokeProtocolError, match=re.escape(detail)):
        collect_tool_names(fetch)


# --- Property test for pagination (Property 7) ---------------------------------------------

_CURSOR = st.text(max_size=8)  # "" is a real cursor
_PAGE_NAMES = st.lists(st.text(), max_size=3)


def _chain(
    cursors: list[str], page_names: list[list[str]], last_next: str | None, *, omit_last: bool
) -> dict[str | None, dict[str, Any]]:
    """Pages keyed ``None, c1, ..., c_k``; page i links to ``cursors[i]``, the last to ``last_next``.

    ``len(page_names) == len(cursors) + 1``. With ``omit_last`` and ``last_next is None``, the
    last page has no ``nextCursor`` key at all.
    """
    keys: list[str | None] = [None, *cursors]
    pages: dict[str | None, dict[str, Any]] = {}
    for i, (key, names) in enumerate(zip(keys, page_names, strict=True)):
        page: dict[str, Any] = {"tools": _tools(*names)}
        next_cursor = cursors[i] if i < len(cursors) else last_next
        if next_cursor is not None or not omit_last:
            page["nextCursor"] = next_cursor
        pages[key] = page
    return pages


@settings(max_examples=100)
@given(
    data=st.data(),
    cursors=st.lists(_CURSOR, unique=True, max_size=DEFAULT_MAX_PAGES - 1),
    omit_last=st.booleans(),
)
def test_collect_tool_names_property_complete_chain(
    data: st.DataObject,
    cursors: list[str],
    omit_last: bool,  # noqa: FBT001 - hypothesis passes drawn values positionally by name
) -> None:
    """Feature: python-version-upgrade, Property 7: Pagination collects every page in order

    A chain of N <= max_pages pages yields every name in page order, fetching ``None`` then
    each cursor exactly once; the last page's ``nextCursor`` may be absent or null.

    **Validates: Requirements 9.7, 9.8, 9.11**
    """
    page_names = data.draw(
        st.lists(_PAGE_NAMES, min_size=len(cursors) + 1, max_size=len(cursors) + 1)
    )
    max_pages = data.draw(st.integers(min_value=len(cursors) + 1, max_value=DEFAULT_MAX_PAGES))
    fetch, calls = _paged_fetch(_chain(cursors, page_names, None, omit_last=omit_last))

    assert collect_tool_names(fetch, max_pages=max_pages) == [n for p in page_names for n in p]
    assert calls == [None, *cursors]


@settings(max_examples=100)
@given(data=st.data(), cursors=st.lists(_CURSOR, unique=True, min_size=1, max_size=10))
def test_collect_tool_names_property_repeated_cursor(
    data: st.DataObject, cursors: list[str]
) -> None:
    """Feature: python-version-upgrade, Property 7: Pagination collects every page in order

    A chain whose last page returns an already-seen cursor raises ``SmokeProtocolError``
    naming that page and cursor, after fetching only up to that page.

    **Validates: Requirements 9.7, 9.8, 9.11**
    """
    page_names = data.draw(
        st.lists(_PAGE_NAMES, min_size=len(cursors) + 1, max_size=len(cursors) + 1)
    )
    repeated = data.draw(st.sampled_from(cursors))
    fetch, calls = _paged_fetch(_chain(cursors, page_names, repeated, omit_last=False))

    with pytest.raises(SmokeProtocolError) as excinfo:
        collect_tool_names(fetch)
    assert f"page {len(cursors) + 1} repeated cursor {_as_json(repeated)}" in str(excinfo.value)
    assert calls == [None, *cursors]


@settings(max_examples=100)
@given(data=st.data(), max_pages=st.integers(min_value=1, max_value=5))
def test_collect_tool_names_property_max_pages(data: st.DataObject, max_pages: int) -> None:
    """Feature: python-version-upgrade, Property 7: Pagination collects every page in order

    A chain longer than ``max_pages`` raises ``SmokeProtocolError`` after exactly
    ``max_pages`` fetches, naming the unfollowed cursor.

    **Validates: Requirements 9.7, 9.8, 9.11**
    """
    cursors = data.draw(st.lists(_CURSOR, unique=True, min_size=max_pages, max_size=max_pages + 3))
    page_names = data.draw(
        st.lists(_PAGE_NAMES, min_size=len(cursors) + 1, max_size=len(cursors) + 1)
    )
    fetch, calls = _paged_fetch(_chain(cursors, page_names, None, omit_last=True))

    with pytest.raises(SmokeProtocolError) as excinfo:
        collect_tool_names(fetch, max_pages=max_pages)
    assert (
        f"after {max_pages} pages (last nextCursor {_as_json(cursors[max_pages - 1])}, "
        "not followed)"
    ) in str(excinfo.value)
    assert calls == [None, *cursors[: max_pages - 1]]


# --- Server process wrapper ---------------------------------------------------------------

_EXIT_WAIT_S = 10.0  # after stdin EOF, the server should exit on its own within this
_KILL_WAIT_S = 5.0  # per kill step (SIGTERM -> SIGKILL, or taskkill)
_JOIN_WAIT_S = 5.0  # reader threads, after the process tree is gone
_STDERR_CHUNK = 4096


def _popen(argv: list[str], env: dict[str, str], cwd: Path) -> subprocess.Popen[bytes]:
    """Start ``argv`` with all three pipes, unbuffered, in its own process group/session.

    The group lets ``__exit__`` kill the whole tree (``uv run`` -> python in bundle mode).
    """
    common: dict[str, Any] = {
        "stdin": subprocess.PIPE,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "bufsize": 0,
        "env": env,
        "cwd": cwd,
    }
    # argv comes from the test's own launch resolution (console script, bundle manifest, or
    # the developer-set CMD env var) and is never passed through a shell.
    if sys.platform == "win32":
        return subprocess.Popen(
            argv,
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
            **common,
        )  # nosec B603
    return subprocess.Popen(argv, start_new_session=True, **common)  # nosec B603


class StdioServer:
    """Context manager around the server subprocess, speaking raw JSON-RPC over stdio.

    One instance is one connection. The wrapper doesn't enforce an era: mixing
    ``modern_request`` and ``handshake_request`` on one instance gets the server's own error
    back, reported through ``check_result``.

    ``__exit__`` always reaps the process tree and closes the pipes, on pass and on failure,
    and never suppresses the body's exception.
    """

    def __init__(self, spec: LaunchSpec, cwd: Path, timeout: float = 60.0) -> None:
        self.spec = spec
        self.cwd = cwd
        self.timeout = timeout
        self.proc: subprocess.Popen[bytes] | None = None
        self._out: StdoutQueue = queue.Queue()
        self._err: list[bytes] = []
        self._err_lock = threading.Lock()
        self._threads: list[threading.Thread] = []
        self._next_id = 0

    @property
    def pid(self) -> int | None:
        return self.proc.pid if self.proc is not None else None

    # -- lifecycle --------------------------------------------------------------------------

    def __enter__(self) -> Self:
        # Manifest env (bundle mode) overrides the forced values; the bundle's server.py
        # strips unset ${user_config.*} placeholders itself.
        env = {**sanitized_env(os.environ), **self.spec.env_overrides}
        try:
            self.proc = _popen(self.spec.argv, env, self.cwd)
        except OSError as e:
            pytest.fail(
                f"could not start server ({self.spec.mode} mode) argv={self.spec.argv!r} "
                f"in {self.cwd}: {e}"
            )
        try:
            self._start_reader("smoke-stdout", self._read_stdout)
            self._start_reader("smoke-stderr", self._read_stderr)
        except BaseException:
            self._cleanup()
            raise
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self._cleanup()

    def _start_reader(self, name: str, target: Callable[[], None]) -> None:
        thread = threading.Thread(target=target, name=name, daemon=True)
        thread.start()
        self._threads.append(thread)

    def _read_stdout(self) -> None:
        """Enqueue raw stdout lines, then ``EOF`` once stdout closes (or the pipe errors)."""
        pipe = self._require_proc().stdout
        try:
            if pipe is not None:
                for line in iter(pipe.readline, b""):
                    self._out.put(line)
        except (OSError, ValueError):  # pipe closed under us during cleanup
            pass
        finally:
            self._out.put(EOF)

    def _read_stderr(self) -> None:
        """Append raw stderr chunks to the locked buffer until stderr closes."""
        pipe = self._require_proc().stderr
        if pipe is None:
            return
        try:
            for chunk in iter(lambda: pipe.read(_STDERR_CHUNK), b""):
                with self._err_lock:
                    self._err.append(chunk)
        except (OSError, ValueError):  # pipe closed under us during cleanup
            pass

    def _cleanup(self) -> None:
        """Close stdin, reap (or kill) the process tree, join readers, close the pipes."""
        proc = self.proc
        if proc is None:
            return
        if proc.stdin is not None:
            with contextlib.suppress(OSError):
                proc.stdin.close()  # the server exits on stdin EOF
        try:
            proc.wait(_EXIT_WAIT_S)
        except subprocess.TimeoutExpired:
            self._kill_tree(proc)
        for thread in self._threads:
            thread.join(_JOIN_WAIT_S)
        for pipe in (proc.stdout, proc.stderr):
            if pipe is not None:
                with contextlib.suppress(OSError):
                    pipe.close()

    def _kill_tree(self, proc: subprocess.Popen[bytes]) -> None:
        """Kill the process group/tree, then reap. Logs a warning if it won't die."""
        if sys.platform == "win32":
            # /T kills the uv -> python tree; terminating only uv would orphan the server
            # and keep the pipe handles open.
            subprocess.run(
                ["taskkill", "/T", "/F", "/PID", str(proc.pid)],  # noqa: S607 - Windows system tool
                capture_output=True,
                check=False,
            )  # nosec B603 B607
        else:
            # start_new_session=True made the child its own group leader, so its pid is the
            # pgid. Using it directly avoids getpgid() failing on an already-reaped leader.
            for sig in (signal.SIGTERM, signal.SIGKILL):
                try:
                    os.killpg(proc.pid, sig)
                except (ProcessLookupError, PermissionError):
                    break
                try:
                    proc.wait(_KILL_WAIT_S)
                    break
                except subprocess.TimeoutExpired:
                    continue
        try:
            proc.wait(_KILL_WAIT_S)
        except subprocess.TimeoutExpired:
            logger.warning(
                "smoke server pid %s (argv %r) did not exit after kill; closing pipes anyway",
                proc.pid,
                self.spec.argv,
            )

    def _require_proc(self) -> subprocess.Popen[bytes]:
        if self.proc is None:
            raise RuntimeError("StdioServer used outside its `with` block")
        return self.proc

    # -- I/O --------------------------------------------------------------------------------

    def stderr_text(self) -> str:
        """Everything the server has written to stderr so far, decoded leniently."""
        with self._err_lock:
            data = b"".join(self._err)
        return data.decode("utf-8", errors="replace")

    def send(self, msg: Mapping[str, Any]) -> None:
        """Write one JSON-RPC message as a single newline-terminated line."""
        stdin = self._require_proc().stdin
        if stdin is None:
            raise RuntimeError("server stdin is not a pipe")
        data = memoryview(json.dumps(msg).encode("utf-8") + b"\n")
        try:
            while data:  # unbuffered raw writes may be partial
                written = stdin.write(data)
                data = data[written or 0 :]
            stdin.flush()
        except (BrokenPipeError, OSError, ValueError) as e:
            raise SmokeProtocolError(
                _with_stderr(
                    f"could not write {msg.get('method', '?')!r} to server stdin ({e})",
                    self.stderr_text(),
                )
            ) from e

    def notify(self, method: str, params: Mapping[str, Any] | None = None) -> None:
        """Send a JSON-RPC notification (no id; ``params`` omitted when None)."""
        msg: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            msg["params"] = dict(params)
        self.send(msg)

    def wait_for(self, req_id: int, method: str, era: Era) -> dict[str, Any]:
        """Return the response to ``req_id``; timeouts and EOF name the era and method."""
        return read_response(
            self._out,
            req_id,
            self.timeout,
            self.stderr_text,
            context=f"{era} {method} (id {req_id})",
        )

    def request(self, method: str, params: Mapping[str, Any], *, era: Era) -> dict[str, Any]:
        """Send a request and return its ``result``, raising on any JSON-RPC error.

        Waits for each response before returning, because the server drops in-flight
        responses once stdin hits EOF.
        """
        self._next_id += 1
        req_id = self._next_id
        self.send({"jsonrpc": "2.0", "id": req_id, "method": method, "params": dict(params)})
        response = self.wait_for(req_id, method, era)
        return check_result(response, era=era, method=method, stderr=self.stderr_text())

    def modern_request(
        self, method: str, params: Mapping[str, Any] | None = None
    ) -> dict[str, Any]:
        """A request stamped with the modern per-request ``_meta`` envelope."""
        return self.request(method, stamp_modern_envelope(params), era="modern")

    def handshake_request(
        self, method: str, params: Mapping[str, Any] | None = None
    ) -> dict[str, Any]:
        """A request on a handshake-era connection (no envelope)."""
        return self.request(method, dict(params or {}), era="handshake")


# --- Live sanity check for the wrapper (echo child, no server) ----------------------------
# A tiny JSON-RPC echo child exercises send/request/notify/wait_for end to end on every OS.
# The dedicated cleanup test is task 7.14; the real-server tests are 7.15 and 7.16.

_ECHO_CHILD = """
import json, sys
sys.stderr.write("echo child ready\\n"); sys.stderr.flush()
for raw in sys.stdin.buffer:
    msg = json.loads(raw)
    if "id" not in msg:
        continue
    out = sys.stdout
    out.write(json.dumps({"jsonrpc": "2.0", "method": "notifications/message"}) + "\\n")
    if msg["method"] == "fail":
        reply = {"jsonrpc": "2.0", "id": msg["id"], "error": {"code": -32601, "message": "nope"}}
    else:
        reply = {"jsonrpc": "2.0", "id": msg["id"], "result": {"echo": msg}}
    out.write(json.dumps(reply) + "\\n"); out.flush()
"""


def _echo_spec() -> LaunchSpec:
    return LaunchSpec(argv=[sys.executable, "-c", _ECHO_CHILD], env_overrides={}, mode="cmd")


def test_stdio_server_round_trips_with_echo_child(tmp_path: Path) -> None:
    with StdioServer(_echo_spec(), tmp_path, timeout=30.0) as srv:
        srv.notify("notifications/initialized")
        first = srv.handshake_request("initialize", {"protocolVersion": "x"})
        second = srv.modern_request("tools/list", {"cursor": "c1"})

        with pytest.raises(SmokeRpcError) as excinfo:
            srv.handshake_request("fail")

    assert first["echo"]["id"] == 1
    assert first["echo"]["params"] == {"protocolVersion": "x"}
    assert second["echo"]["id"] == 2
    assert second["echo"]["params"]["cursor"] == "c1"
    assert PROTOCOL_VERSION_META_KEY in second["echo"]["params"]["_meta"]
    assert "handshake fail returned JSON-RPC error -32601: nope" in str(excinfo.value)
    assert srv.proc is not None
    assert srv.proc.poll() is not None
    assert "echo child ready" in srv.stderr_text()


def test_stdio_server_missing_command_fails_naming_argv(tmp_path: Path) -> None:
    spec = LaunchSpec(argv=[str(tmp_path / _NONEXISTENT_COMMAND)], env_overrides={}, mode="cmd")
    with pytest.raises(pytest.fail.Exception, match=_NONEXISTENT_COMMAND):
        StdioServer(spec, tmp_path).__enter__()


# --- Cleanup tests for the wrapper --------------------------------------------------------

_READY = "cleanup child ready"
# Never reads stdin, so stdin EOF doesn't end it. The POSIX variant also ignores SIGTERM,
# forcing the SIGKILL step; on Windows taskkill /F ends either variant.
_STUBBORN_CHILD = """
import signal, sys, time
if {ignore_sigterm} and sys.platform != "win32":
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
sys.stderr.write({ready!r} + "\\n"); sys.stderr.flush()
time.sleep(300)
"""
_EOF_CHILD = f"""
import sys
sys.stderr.write({_READY!r} + "\\n"); sys.stderr.flush()
sys.stdin.buffer.read()
"""


class _BodyError(Exception):
    """Raised inside the ``with`` block to check it propagates through ``__exit__``."""

    def __init__(self, srv: StdioServer) -> None:
        super().__init__("body failed")
        self.srv = srv


def _python_spec(code: str) -> LaunchSpec:
    return LaunchSpec(argv=[sys.executable, "-c", code], env_overrides={}, mode="cmd")


def _wait_ready(srv: StdioServer, timeout: float = 30.0) -> None:
    """Block until the child has written its ready line (signal handler installed)."""
    deadline = time.monotonic() + timeout
    while _READY not in srv.stderr_text():
        assert time.monotonic() < deadline, f"child never got ready: {srv.stderr_text()!r}"
        time.sleep(0.02)


def _run_failing_body(spec: LaunchSpec, cwd: Path) -> NoReturn:
    """Enter the wrapper, wait for the child, raise ``_BodyError`` inside the ``with``."""
    with StdioServer(spec, cwd) as srv:
        _wait_ready(srv)
        raise _BodyError(srv)


def _assert_reaped_and_closed(srv: StdioServer) -> subprocess.Popen[bytes]:
    proc = srv.proc
    assert proc is not None
    assert proc.poll() is not None
    assert proc.stdin is not None
    assert proc.stdout is not None
    assert proc.stderr is not None
    assert proc.stdin.closed
    assert proc.stdout.closed
    assert proc.stderr.closed
    return proc


@pytest.mark.parametrize("ignore_sigterm", [False, True], ids=["plain", "ignores-sigterm"])
def test_stdio_server_kills_child_that_ignores_stdin_eof(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    ignore_sigterm: bool,  # noqa: FBT001 - pytest passes parametrized values by name
) -> None:
    # _cleanup and _kill_tree read these module globals at call time.
    module = sys.modules[__name__]
    monkeypatch.setattr(module, "_EXIT_WAIT_S", 0.5)
    monkeypatch.setattr(module, "_KILL_WAIT_S", 0.5)
    code = _STUBBORN_CHILD.format(ignore_sigterm=ignore_sigterm, ready=_READY)

    start = time.monotonic()
    with pytest.raises(_BodyError) as excinfo:
        _run_failing_body(_python_spec(code), tmp_path)

    proc = _assert_reaped_and_closed(excinfo.value.srv)
    if sys.platform != "win32":
        expected = signal.SIGKILL if ignore_sigterm else signal.SIGTERM
        assert proc.returncode == -expected
    else:
        assert proc.returncode != 0
    assert time.monotonic() - start < 30.0


def test_stdio_server_reaps_child_that_exits_on_stdin_eof(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    kills: list[int] = []
    monkeypatch.setattr(StdioServer, "_kill_tree", lambda _self, proc: kills.append(proc.pid))

    with pytest.raises(_BodyError) as excinfo:
        _run_failing_body(_python_spec(_EOF_CHILD), tmp_path)

    proc = _assert_reaped_and_closed(excinfo.value.srv)
    assert proc.returncode == 0
    assert kills == []


# --- Real-server smoke tests --------------------------------------------------------------
# One subprocess launch per protocol era, because the SDK locks the era on the first
# request of a connection. Both eras use the same ``launch_spec`` (source, bundle, or cmd).


def expected_tool_set() -> set[str]:
    """The tool names the server registers, mirroring ``main._setup_tools()``.

    ``_setup_tools`` passes each ``get_tool_list()`` entry to ``mcp.add_tool`` with no
    ``name``, so the SDK registers it under ``fn.__name__``; then it adds ``about``.
    ``get_tool_list()`` always includes ``query_oscal_documentation`` (only its backend
    depends on ``OSCAL_KB_ID``), so the set doesn't depend on configuration.
    """
    from mcp_server_for_oscal.tools import get_tool_list

    return {fn.__name__ for fn in get_tool_list()} | {"about"}


@pytest.fixture
def launch_spec() -> LaunchSpec:
    return resolve_launch(os.environ)


def _set_diff(actual: set[str], expected: set[str]) -> str:
    return f"missing {sorted(expected - actual)}, extra {sorted(actual - expected)}"


def assert_tool_names(names: list[str], spec: LaunchSpec) -> None:
    """Assert the listed tools are unique and equal the Expected_Tool_Set.

    In bundle mode the names must also equal the manifest's ``tools`` names. Every shipped
    bundle's manifest is filled by ``bin/build_mcpb.py`` from the same ``get_tool_list()``
    (the repo template's ``"tools": []`` is overwritten), so an empty or stale list there is
    a real packaging defect and is compared, not skipped.
    """
    duplicates = sorted({n for n in names if names.count(n) > 1})
    assert not duplicates, f"duplicate tool names: {duplicates}"
    actual = set(names)
    expected = expected_tool_set()
    assert actual == expected, f"tool set mismatch: {_set_diff(actual, expected)}"
    if spec.bundle_manifest is not None:
        manifest_names = {str(t["name"]) for t in spec.bundle_manifest.get("tools", [])}
        assert actual == manifest_names, (
            f"tool set differs from the bundle manifest's tools: "
            f"{_set_diff(actual, manifest_names)}"
        )


def test_stdio_modern_discover_and_list_tools(launch_spec: LaunchSpec, tmp_path: Path) -> None:
    """Primary check: the modern era over a real server subprocess (Req 9.2-9.8).

    The first request on the connection is an enveloped ``server/discover`` (Req 9.2-9.4),
    then every ``tools/list`` page is requested with the envelope (Req 9.5, 9.7), must carry
    ``resultType`` (Req 9.6), and the collected names must equal the Expected_Tool_Set
    (Req 9.8). Versions and ``_meta`` keys come from ``mcp_types``, never literals.
    """
    with StdioServer(launch_spec, cwd=tmp_path) as srv:
        discover = srv.modern_request("server/discover")
        assert LATEST_MODERN_VERSION in discover["supportedVersions"], (
            f"server/discover doesn't advertise {LATEST_MODERN_VERSION}: {_as_json(discover)}"
        )
        assert "tools" in discover["capabilities"], (
            f"server/discover capabilities lack tools: {_as_json(discover)}"
        )

        def modern_page(cursor: str | None) -> dict[str, Any]:
            page = srv.modern_request("tools/list", {} if cursor is None else {"cursor": cursor})
            assert "resultType" in page, (
                f"modern tools/list result lacks resultType: {_as_json(page)}"
            )
            return page

        names = collect_tool_names(modern_page)
        assert_tool_names(names, launch_spec)


def test_stdio_handshake_initialize_and_list_tools(launch_spec: LaunchSpec, tmp_path: Path) -> None:
    """Legacy-compat check: the handshake era over a real server subprocess (Req 9.9-9.11).

    This is a separate launch from the modern test because the SDK locks the protocol era
    per connection on its first request. ``initialize`` must answer with the requested
    handshake version, a server name, and a tools capability (Req 9.9, 9.10); after
    ``notifications/initialized``, the paged ``tools/list`` names must equal the
    Expected_Tool_Set (Req 9.11). The version comes from ``mcp_types``, never a literal.
    """
    with StdioServer(launch_spec, cwd=tmp_path) as srv:
        init = srv.handshake_request(
            "initialize",
            {
                "protocolVersion": LATEST_HANDSHAKE_VERSION,
                "capabilities": {},
                "clientInfo": dict(SMOKE_CLIENT_INFO),
            },
        )
        assert init["serverInfo"]["name"], f"initialize lacks serverInfo.name: {_as_json(init)}"
        assert "tools" in init["capabilities"], (
            f"initialize capabilities lack tools: {_as_json(init)}"
        )
        assert init["protocolVersion"] == LATEST_HANDSHAKE_VERSION, (
            f"initialize negotiated {init['protocolVersion']!r}, "
            f"expected {LATEST_HANDSHAKE_VERSION}: {_as_json(init)}"
        )
        srv.notify("notifications/initialized")

        # No resultType assertion here: the SDK sieves it out of handshake-era results.
        names = collect_tool_names(
            lambda c: srv.handshake_request("tools/list", {} if c is None else {"cursor": c})
        )
        assert_tool_names(names, launch_spec)
