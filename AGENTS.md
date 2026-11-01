# AGENTS.md

Guidance for agents and humans working **on this repository**. This is not the same document as
`AGENTS.md.example`: that file is the template deployed into a capability workspace (the caps
root), and is read by the model at runtime. Keep the two in sync when the capability contract
changes.

## What this is

`capforge` turns small folders of Python into deterministic, versioned tools exposed over MCP, for
Open WebUI + Open Terminal. The model writes a capability folder through its terminal, tests the
draft, promotes it to an immutable tag, and calls the active tag through one fixed dispatcher
tool, `run_capability`.

Three product constraints drive the whole design. Do not "fix" them:

1. **The MCP tool list is fixed at ten.** Open WebUI calls `tools/list` every message, and a
   changing tool set invalidates the provider prompt cache for the entire conversation. Capabilities
   live behind the surface, never as individual tools.
2. **There is one dispatcher, not one tool per capability.** `run_capability(name, args)` resolves
   its target at execution time, so a capability created in this turn is callable in the same turn.
3. **Paths are shared with the terminal.** `CAPFORGE_CAPS_DIR` is the terminal's skills home, so
   Open WebUI shows the folder contents to the model for free each turn, and the same files are
   what the server executes.

## Layout

```
capforge/
    __init__.py     __version__ (must match pyproject.toml)
    core.py         paths + env, validation, SKILL.md parsing, state JSON, hashing, venvs,
                    execute(), run_tests()
    versioning.py   snapshot, promote, prune, rollback, list_tags, diff, best-effort git audit
    contract.py     CONTRACT text and the scaffold templates (main/SKILL/test/.gitignore)
    server.py       FastMCP instance and the ten @mcp.tool functions (the public API)
    cli.py          capforge serve | doctor | tools
docs/
    ARCHITECTURE.md     design, data model, security posture, deployment, change map
    NEXT.md             known limitations and later work
tests/
    test_lifecycle.py   end-to-end: scaffold -> test -> promote -> run -> edit -> rollback
    test_units.py       fast: parsing, validation, hashes, state coercion, tool surface
tools/
    mcp_probe.py        real MCP-over-HTTP exercise of the server, like Open WebUI
AGENTS.md.example       template for the capability workspace, not this file
uv.lock                 resolved dependency graph; CI and Docker build with --locked
Dockerfile              uv multi-stage image: Python 3.14 + git, uid 10001, TCP healthcheck
docker-compose.yml      local example: bind-mounted caps/state, uid 10001, :8099
.env.example            deployment variables; no credential variables by design
.dockerignore
.github/workflows/deploy.yml   test gate -> build/push GHCR + provenance attestation
.github/dependabot.yml         weekly updates for Actions, pip and the Docker base image
```

Data flow: `server.py` is a thin shell over `core.py` and `versioning.py`. Business logic belongs
in those modules so it stays testable without MCP. `contract.py` holds only strings.

## The ten tools

`get_contract`, `list_capabilities`, `get_capability`, `scaffold_capability`, `install_packages`,
`test_capability`, `run_capability`, `promote_capability`, `rollback_capability`, `list_tags`.

Treat their names, signatures and descriptions as a public API. Renaming or adding a tool is a
cache-invalidating break for every connected client. Update the README table, `tools/mcp_probe.py`
(which asserts exactly ten names) and the tests together if you ever do.

Every tool returns a dict. Failures that the model should read are `{"ok": False, "error": "..."}`
via `_fail(...)`; never let a `CapError` escape to the MCP layer. Unknown bugs may raise.

## Capability contract

```
<caps>/<name>/
    SKILL.md            YAML-ish frontmatter + instructions
    main.py             entrypoint (overridable with `entrypoint:`)
    tests/test_main.py  optional; pytest runs when any test_*.py exists
```

Frontmatter keys that matter: `name`, `description` (the only field in the per-turn manifest),
`entrypoint` and `packages`. There is no `python:` field: execution always goes through the
per-capability venv's interpreter. The parser in `core.parse_skill_md` is deliberately tolerant
and does not use a YAML library: do not add a dependency for it.

Packages are validated with `PKG_RE` before they reach pip or `SKILL.md`. Specs are plain
requirements (no spaces, commas, quotes, URLs or leading `-`), max 50 per capability. This is a
security boundary, not cosmetics: an unvalidated token such as `--target=...` is a pip option.

`entrypoint` is resolved and contained inside the capability folder. A `run_capability` that
targets an active tag reads the tag's **own** `SKILL.md`, so editing the draft's `entrypoint` or
`packages` cannot change or break the live version.

Execution rules, enforced in `core.execute`:

- one JSON object on **stdin** (never argv); result as JSON on **stdout**; exit 0 for success;
- `execute()` parses the whole trimmed stdout first, then falls back to the **last non-empty
  line**, so diagnostics on stdout are tolerated while pretty-printed JSON still works;
- diagnostics on stderr, returned to the caller on failure;
- a filtered environment (`ENV_ALLOWLIST` only: PATH, locale, TZ, TERM, SSL certs). pip index
  settings are exposed to the installer through `_install_env()`, never to capability code;
- `CAPFORGE_DATA_DIR` is per capability **and per version**; `CAPFORGE_VERSION` names the tag;
- `PATH` is prefixed with the capability's venv; the venv's Python runs the entrypoint;
- a hard timeout (`CAPFORGE_RUN_TIMEOUT`, default 120s), enforced on the child's **process group**;
- stdout/stderr capture is bounded (256 KB tail per stream) so a runaway child cannot exhaust RAM.

The smoke path in `run_tests` (no `tests/test_*.py`) calls `execute()` itself, so a capability that
cannot emit valid JSON fails `test_capability` and is refused promotion.

## State and versioning model

- The folder under `CAPFORGE_CAPS_DIR` is always the **draft**. `test_capability` runs the draft.
- Tags are immutable copies under `<state>/versions/<name>/<tag>/`; an existing tag can never be
  overwritten. Promotion snapshots, records state, then makes the tag active.
- `run_capability` executes the active tag. It falls back to the draft only when nothing was ever
  promoted, and adds a `warning`. A broken draft does not stop an already-promoted tag from running.
- `rollback_capability` only moves the active pointer; it never edits the draft or copies files.
- State lives in `<state>/state/<name>.json` (`active` entry + `tags` + `pruned`), written
  atomically via a temp file and `Path.replace`. Corrupt files are renamed to
  `<name>.corrupt-<ns>.json` and treated as empty rather than silently overwritten.
- State mutations hold a per-capability `flock` (`<state>/locks/<name>.lock`); venv builds hold a
  per-requirements lock. Do not nest the same lock.
- Pruning keeps `CAPFORGE_KEEP_TAGS` (default 5) tags and **never** drops the active one, even when
  it is the oldest. A pruned tag also loses its per-version data and its unused venvs; the
  `pruned` audit list is capped at 100 entries.
- The caps root is best-effort git-initialised on first promotion; each promotion commits
  `<name>@<tag>`. Git failures are non-fatal and must stay that way.

`tree_hash` ignores `__pycache__`, `.git`, `.pytest_cache`, `.venv`, `venv`, `.mypy_cache` and
`.ruff_cache`. Keep that list aligned with `versioning.SKIP`; a mismatch makes "draft unchanged"
and snapshots disagree.

Order matters in `versioning.promote`: any new `packages` are written into `SKILL.md` and the draft
hash is recomputed **before** `snapshot()`, so the tag carries the frontmatter its venv is built
from. Do not reorder that.

Venvs are keyed by `requirements_hash(sorted(set(packages)))`, one per requirement set under
`<state>/venvs/<name>/<hash>/`. A new set builds a fresh venv; venvs no surviving tag needs are
garbage-collected on prune. Test runs use the capability's packages **plus pytest**, so they get a
different hash than run-time venvs (both hashes are treated as live). stdlib-only uses
`--without-pip`. Treat venv creation as slow and real: tests actually build them.

## Setup and commands

Requires **Python 3.14** and uv (`.python-version` pins 3.14; `requires-python = ">=3.14"`).
Do not add a CI version matrix.

```sh
uv sync                                # fastmcp + dev tools, exactly from uv.lock
uv run pytest -q -m "not slow"         # fast unit tests, no venvs
uv run pytest -q                       # full suite; builds real venvs
uv run ruff check .                    # lint
uv run mypy capforge                   # type check
uv run capforge doctor                 # paths, writability, scanned-skill-path warnings, fastmcp
uv run capforge serve                  # MCP over streamable HTTP on 0.0.0.0:8099/mcp
uv run capforge serve --transport stdio   # local MCP client
uv run capforge tools                  # dump tool schemas as JSON
uv run capforge serve &
CAPFORGE_URL=http://127.0.0.1:8099/mcp uv run python tools/mcp_probe.py
```

`uv.lock` is committed. After changing dependencies in `pyproject.toml`, run `uv lock` and commit
the result; CI and the Docker image use `--locked` and fail on drift. Runtime dependencies belong in
`[project].dependencies` (fastmcp), dev-only tools in `[dependency-groups].dev` (pytest, httpx); the
image installs with `--no-dev`.

`capforge doctor` must keep working when the `fastmcp` install is missing or broken: it reports the
problem instead of crashing. That is why `cli.py` imports `server` lazily inside command handlers.
Keep the import of fastmcp confined to `server.py`; `core.py`, `versioning.py` and `contract.py`
remain stdlib-only even though the distribution now depends on fastmcp.

Environment variables are documented in the README table; when adding one, also expose it through
`get_contract()` if it is a limit, and cover it in the tests. Current ones: `CAPFORGE_CAPS_DIR`,
`CAPFORGE_STATE_DIR`, `CAPFORGE_KEEP_TAGS`, `CAPFORGE_RUN_TIMEOUT`, `CAPFORGE_TEST_TIMEOUT`,
`CAPFORGE_INSTALL_TIMEOUT`, `CAPFORGE_HOST`, `CAPFORGE_PORT`, `CAPFORGE_PATH`,
`CAPFORGE_TRANSPORT`, `CAPFORGE_LOG_LEVEL`.

## Conventions

- Standard library only in `core.py`, `versioning.py` and `contract.py`. `fastmcp` is confined to
  `server.py`. Do not add dependencies without a clear reason.
- Type hints on public functions; short docstrings explaining *why*, not restating the signature.
- Double quotes, ~100-column lines, module-level UPPERCASE constants.
- Raise `CapError` for anything the model should read verbatim; use plain exceptions only for bugs.
- Validate and contain every path. Names match `NAME_RE` (`^[a-z][a-z0-9_]{0,63}$`), tags match
  `TAG_RE`, packages match `PKG_RE`, and anything derived from a name/tag/package must pass
  through `_contained(...)` before touching disk. Never build a path from unsanitised model input.
- Log runs and promotions at INFO with stable `key=value` fields; use `CAPFORGE_LOG_LEVEL` to
  change verbosity. Never log capability output bodies or environment values.
- Keep `__version__` in `capforge/__init__.py` and `[project].version` in `pyproject.toml` equal.
- Capabilities themselves stay deterministic: sorted output, pinned formats, no invented
  timestamps, no secrets, no assumed network, writes only under `CAPFORGE_DATA_DIR`.

## Testing

`tests/test_lifecycle.py` is the reference for behaviour and is marked `slow`. Its `env` fixture
creates a temp caps and state dir, sets env vars, then `importlib.reload`s `core`, `versioning` and
`server`. Follow that pattern for new tests: state and module globals must not leak between tests,
and env changes need a reload only if code captured the value at import time (most accessors read
env per call). `tests/test_units.py` is the fast suite: parsing, validation, hashes, state
coercion and the ten-tool surface.

Do not mock subprocesses or venvs. The slow suite deliberately exercises real Python processes, the
stdin/stdout JSON contract, timeouts, environment filtering and per-version data directories.
Coverage worth preserving when refactoring: name validation, duplicate tags, promotion refusing
failed tests, unchanged drafts (`force`), pruning the active tag, rollback, draft-vs-active
isolation (including `entrypoint` and empty `packages`), bad JSON output, smoke-test JSON
validation, package-spec rejection, timeout process-group kill, env filtering (including
`PIP_INDEX_URL`), corrupt state quarantine, overwrite archiving, retention, and listing a broken
capability without failing the whole listing.

## Definition of done

- `uv run --locked pytest -q` passes, and `uv run --locked pytest -q -m "not slow"` is quick.
- `uv run --locked ruff check .` and `uv run --locked mypy capforge` pass.
- `uv run --locked capforge tools` still reports exactly ten tools with stable signatures.
- `uv run --locked capforge doctor` is clean against a real caps dir (it warns when the caps dir is
  not under `~/.agents/skills`, `~/.cptr/skills`, `~/.claude/skills` or `~/.codex/skills`).
- `tools/mcp_probe.py` completes against a running server.
- `uv lock --check` passes and `uv.lock` is committed when dependencies changed.
- If the capability contract changed: `contract.py`, `README.md` and `AGENTS.md.example` were
  updated together.
