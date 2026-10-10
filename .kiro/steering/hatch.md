# Run Everything Through Hatch

All Python execution in this project goes through `hatch`. Never invoke a Python
interpreter or Python tool directly.

**Why:** hatch guarantees the correct, locked environment (`UV_CONSTRAINT` pins
deps to `requirements.txt`, the right Python version, the `devtest` group). It
also keeps shell permissions narrow: `hatch ...` commands are allowlisted, while
arbitrary `python`/`python3`/`.venv/bin/python` invocations are not and will
require manual approval.

## Do / Don't

| Task | Do | Don't |
|---|---|---|
| Full suite (typing + tests + coverage + bandit) | `hatch run tests` | chaining mypy/pytest/bandit by hand |
| Run tests | `hatch test` | `pytest`, `python -m pytest`, `.venv/bin/pytest` |
| Run a specific test | `hatch test tests/tools/test_get_schema.py::TestX::test_y` | `python -m pytest tests/...` |
| Pass pytest flags | `hatch test -- -x -k "schema"` | `pytest -x -k "schema"` |
| Single Python version | `hatch test -py 3.14 tests/...` | switching interpreters manually |
| Type checking | `hatch run typing` | `mypy src`, `python -m mypy` |
| Format | `hatch check fmt --fix` (drop `--fix` to check only) | `ruff format`, `hatch fmt` (deprecated) |
| Lint | `hatch check code --fix` (drop `--fix` to check only) | `ruff check`, `hatch fmt` (deprecated) |
| Security scan | `hatch run bandito` | `bandit -r src` |
| Run a script | `hatch run python bin/some_script.py` | `python3 bin/some_script.py`, `.venv/bin/python ...` |
| One-off snippet | `hatch run python -c "import oscal_bindings; print(oscal_bindings.__oscal_schema_version__)"` | `python3 -c ...` |
| Run a module | `hatch run python -m mcp_server_for_oscal.main` | `python -m mcp_server_for_oscal.main` |
| Project scripts | `hatch run <script>` (see `[tool.hatch.envs.default.scripts]` in `pyproject.toml`) | re-implementing the script's steps inline |
| Install / sync deps | edit `pyproject.toml`, then `hatch run update` or let hatch sync on next run | `pip install`, `uv pip install` |

## Notes

- `hatch test` uses the `hatch-test` environment (matrix: 3.13, 3.14; includes
  `pytest-asyncio` and `hypothesis`). `hatch run` uses the `default` environment
  (Python 3.14, `devtest` group). Use `hatch test` for tests and `hatch run` for
  everything else.
- Hatch doesn't recreate an existing env when its configured Python changes.
  After the `default` env Python changes, run `hatch env remove default` once so
  the next `hatch run` rebuilds it.
- Always put pytest flags after `--`. Several short flags collide with
  `hatch test`'s own options (`-x` is matrix exclude, `-p` is parallel, `-c` is
  cover, `-r` is randomize), so `hatch test -x` will not do what pytest's `-x`
  does.
- Prefer an existing `hatch run <script>` over composing the equivalent commands
  yourself. Check `pyproject.toml` before writing a new command line.
- If a needed tool isn't available in a hatch environment, don't fall back to a
  global or `.venv` interpreter. Stop and propose adding it to the appropriate
  dependency group or hatch env.
- Don't prefix hatch commands with `cd`; use the tool's `cwd` parameter.
- When delegating to a subagent, restate this rule in the prompt.
