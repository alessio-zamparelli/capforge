# AGENTS.md

Guidance for agents and humans working **on this repository**. The runtime guide deployed into a
capability workspace is `AGENTS.md.example`.

## What this is

`capforge` turns small folders of Python into deterministic, versioned tools exposed over MCP, for
Open WebUI + Open Terminal. Capabilities are **git-authored uv projects**: the model scaffolds a
base project, clones its repository, edits, pushes, and the pipeline tests and promotes on green.

Constraints that drive the design; do not "fix" them casually:

1. **The MCP tool list is fixed at ten.** Open WebUI calls `tools/list` every message, and a
   changing set or description invalidates the provider prompt cache for the conversation.
2. **There is no shared caps directory.** The server owns drafts, versions and state. The model
   sees code only through its git clone (or `get_capability`), never through a terminal mount.
3. **Git is the authoring transport.** `scaffold_capability` creates the base repo; clone/push are
   the only edits. `test_capability`/`promote_capability`/`install_packages` are not MCP tools.
4. **Python is fixed at 3.14**, and every capability is a uv project with `pyproject.toml`,
   `uv.lock` and `.python-version`. `uv sync --frozen` is the only install path.
5. **Capability code is semi-trusted.** Landlock confines filesystem access; network/CPU/memory
   are not confined. Do not add the hub's own secrets to a capability environment; operator-set
   values are opt-in per capability via `Env` fields in `deploy.py` and `<state>/secrets/`.
   Cross-capability folders are opt-in via `SHARES`, approved only in the console, and mapped to
   exact `ro`/`rw` Landlock rules.
6. **HTTP MCP requires `CAPFORGE_MCP_TOKEN`.** `/cap` and `/api` share the web origin, so treat
   served web capabilities as trusted or expose `/cap` on a different origin at the reverse proxy.

## Layout

```text
capforge/
    __init__.py     __version__ (must match pyproject.toml)
    core.py         paths, validation, state, hashing, locks, processes, uv venvs, execute,
                    tests, web boot probe, stray sweep, Landlock policy
    sandbox.py      Landlock ruleset + exec wrapper
    versioning.py   snapshot, promote, prune, rollback, list_tags, diff
    gitremote.py    bare repo init/config/hook, archive materialisation, refs, gc
    scaffold.py     uv base project, uv lock, initial commit, publish main
    staging.py      git tokens, staging state, bounded logs, async push pipeline
    llm.py          LLM providers, per-cap model selection, loopback gateway
    oidc.py         OIDC providers, discovery, PKCE flow, signed sessions
    contract.py     CONTRACT text and the scaffolded AGENTS template (strings only)
    web.py          :8080 /ui /api /git /cap, console, git CGI bridge, serve engine,
                    OIDC routes
    server.py       FastMCP instance and the ten tools
    cli.py          serve | doctor | tools | promote | test
docs/ARCHITECTURE.md, docs/NEXT.md
tests/test_llm_oidc.py, test_lifecycle.py, test_web.py, test_units.py
tools/mcp_probe.py
ui/                  React + Vite + shadcn/Tailwind console
AGENTS.md.example    guide shipped inside a scaffolded repo
Dockerfile, docker-compose.yml, .env.example
```

Data flow: `server.py` is a thin adapter over `core`, `versioning`, `scaffold`, `staging`,
`gitremote`; `web.py` owns transport and the serve engine. `core`, `versioning`, `contract`,
`staging`, `scaffold`, `gitremote` are stdlib-only (gitremote uses the git binary). `web.py` owns
starlette/httpx; `llm.py` and `oidc.py` own httpx and are imported lazily from `core`; `server.py`
owns fastmcp. `versioning` imports `web` only lazily.

## The ten tools

`get_contract`, `list_capabilities`, `get_capability`, `scaffold_capability`, `run_capability`,
`serve_capability`, `staging`, `delete_capability`, `rollback_capability`, `list_tags`.

Treat names, signatures and descriptions as public API. Adding/removing/renaming a tool is a
cache-invalidating break; update README, `tools/mcp_probe.py` (asserts the ten names), `AGENTS.md`
and tests together.

Every tool returns a dict. Anticipated failures are `{"ok": False, "error": "..."}` via `_fail`;
never let `CapError` escape to MCP.

## Capability contract

```text
<caps>/<name>/
    SKILL.md            name, description and instructions; no deployment fields
    deploy.py           deployment descriptor: KIND, CLI_ENTRYPOINT,
                        WEB_ENTRYPOINT, APP, DATA, SHARES, LLM, AUTH and Env
                        (pydantic) fields; parsed with ast, never imported
    pyproject.toml      requires-python = ">=3.14,<3.15"; dependencies
    uv.lock             committed
    .python-version     3.14
    main.py | app.py    CLI entrypoint (`CLI_ENTRYPOINT`) and/or ASGI app
                        (`WEB_ENTRYPOINT`, `APP`)
    tests/              optional; pytest runs when test_*.py exists
                        (the scaffold writes tests/test_scaffold.py)
    AGENTS.md           the full loop for the model; covers CLI and web
```

`SKILL.md` frontmatter is `name` + `description`; the body is instructions. `deploy.py` is the
machine-readable contract: literal `KIND` (`"cli"`, `"web"`, or a list of both), the matching
`CLI_ENTRYPOINT`/`WEB_ENTRYPOINT`, optional `APP`, `DATA` (`"version"`/`"shared"`), a literal
`SHARES` dict of folder → `ro`/`rw` requests, plus an
optional pydantic `Env` model whose fields declare operator-set variables. `LLM = True` (any
kind) opts into the hub gateway; `AUTH = {...}` (web only) requests OIDC and accepts a provider
name shorthand, allow lists and `public_paths`. `core.parse_deploy_py` parses it with `ast` (no
import, no execution);
supported annotations are `str`, `int`, `float`, `bool` (unknown → `str`), and `Field(...)` may
add `description`, `min_length`, `max_length`, `ge`, `le`. A field without a default is required.
Frontmatter `kind`/`entrypoint`/`app`/`data`/`env`/`packages` is refused with a migration error;
a deploy.py `ENTRYPOINT` is refused in favor of `CLI_ENTRYPOINT`/`WEB_ENTRYPOINT`. A hybrid
(`KIND = ["cli", "web"]`) must declare both entrypoints.
Env names are validated by `valid_env_name`; runtime-owned names (`PATH`, `HOME`, `CAPFORGE_*`,
`PYTHON*`, `LD_*`, `UV_*`, …) are refused.

Execution rules (`core.execute`):

- one JSON object on **stdin**; result as JSON on **stdout**; exit 0 for success;
- parse the whole trimmed stdout, then the last non-empty line;
- diagnostics on stderr; bounded capture (256 KiB tail);
- filtered environment (`ENV_ALLOWLIST`), `CAPFORGE_DATA_DIR`, `CAPFORGE_VERSION`,
  `CAPFORGE_SHARED_DATA_DIR` when enabled, `CAPFORGE_SHARES` for approved volumes, venv-first
  `PATH`;
- `Env` fields declared in `deploy.py` injected only for the active tag (`cap_env_values`); optional
  fields fall back to their declared default; tests, drafts and the web boot probe get defaults or
  type-shaped placeholders (`capforge-test-placeholder`, `0`, `0.0`, `false`), never real values;
- with `LLM = True`, the active run/web child gets five `CAPFORGE_LLM_*` values from the loopback
  gateway (base URL, per-run token, operator-selected model, provider, `openai`); tests, drafts and
  the boot probe get placeholders. The provider key never enters the capability environment;
- a hard timeout on the process group; nothing survives the call; detached strays are swept;
- Landlock: system/own-code/venv read, own data read/write, approved `ro` shares read and `rw`
  shares write, everything else denied;
- `SHARES` declarations are requests; `resolve_shares` fails closed without a grant or with a
  grant that differs from the request, and tests/probes get empty temporary share dirs.

Promotion is pipeline-only. `staging_latest`/`staging_logs` expose state and diagnostics.
`capforge promote`/`test` are operator recovery/legacy commands, not model-facing tools.

## State and versioning

- Draft under `CAPFORGE_CAPS_DIR/<name>` is **derived** from the last accepted push; the repo is
  the source of truth.
- Tags are immutable copies under `<state>/versions/<name>/<tag>/`; promotion snapshots the draft,
  records `env: "uv"`, `lock_hash`, `python`, `git_source`, `pushed_by`, then moves `active`.
- `rollback_capability` only moves the pointer.
- State is `<state>/state/<name>.json`; writes are atomic (temp + `Path.replace`), corrupt files
  are quarantined as `<name>.corrupt-<ns>.json`.
- LLM config is `<state>/llm.json`, keys `<state>/secrets/llm/<provider>.json`, per-cap selection
  `<state>/llm/caps/<name>.json`. OIDC config is `<state>/oidc.json`, client secrets
  `<state>/secrets/oidc/<provider>.json`, session key `<state>/auth.key` (`0600`). Only the console
  writes keys; they are never in state JSON, tags, hashes or MCP output.
- OIDC sessions are signed cookies scoped to `/cap/<name>/`; the proxy forwards
  `X-Capforge-User*` and strips client-supplied `x-capforge-*` headers. A missing provider fails
  `serve`/start closed; a missing LLM model/key fails `run`/`serve` closed.
- Operator env values are `<state>/secrets/<name>.json` (mode `0600`, atomic, per capability and
  shared across tags). They are never in state JSON, tags, hashes or MCP output; only the console
  can write them, and a web instance restarts after a change.
- Shared folders are `<state>/shares/<folder>/`; the census and grants are
  `<state>/state/shares.json` (atomic, corrupt files quarantined). Grants are per capability and
  shared across tags. Deleting a capability revokes its grants but keeps the folder; a folder is
  deletable only with no grants. The `share_lock` lock covers registry mutations.
- Locks: per-capability `cap_lock`, per-lockfile venv locks, per-name push and pipeline locks,
  and `shares.lock`.
  **Never nest the same flock.** The draft swap and promotion are one `cap_lock` critical section;
  the worker uses `versioning.promote(locked=True)`.
- Pruning keeps `CAPFORGE_KEEP_TAGS` (default 5) and never the active tag; it stops a pinned web
  instance, removes per-version data and unused venvs.
- Git is a best-effort audit through the capability repository and
  `refs/capforge/processed`; there is no caps-root audit repo.
- Venvs live under `<state>/venvs/<name>/<lock_hash>-py314[-test]/`; `uv sync --frozen` builds
  them. Test runs use the `dev` group (pytest, pytest-cov); production uses `--no-dev`.

## Sandboxing

`capforge/sandbox.py` implements Landlock with `ctypes`; the server runs a small helper that applies
the ruleset and `execv`s the target. Policies are built in `core._sandbox_policy`; the default is
`CAPFORGE_SANDBOX=require` and fails closed when the kernel lacks Landlock. Policy: read
system/venv/own code, read/write exact data dirs and per-run temp; deny state, tokens, other
capabilities, home, `/proc`, `/sys`, `/tmp`, `/etc/shadow`. Network/CPU/memory/ptrace are not
confined. Do not broaden the allowlist to make a test pass without documenting why.

## Conventions

- Standard library only in `core`, `versioning`, `contract`, `staging`, `scaffold`, `gitremote`.
  `fastmcp` only in `server.py`; starlette/httpx only in `web.py`.
- Type hints on public functions; short docstrings explaining *why*.
- Double quotes, ~100-column lines, module-level UPPERCASE constants.
- Validate and contain every path. Names match `NAME_RE`, tags `TAG_RE`; derived paths pass
  `_contained` before touching disk. Never build a path from unsanitised model input.
- Log runs/promotions at INFO with stable `key=value` fields. Never log tokens, `Authorization`,
  environment values or capability output bodies beyond the bounded log.
- Keep `__version__` and `[project].version` equal (0.8.1).
- Capabilities stay deterministic when `data: version`; `DATA = "shared"` and `SHARES` relax that by
  design and leave concurrency/backup to the capability and operator.
- Use `track_child()` for every direct git/pytest/uv child so a concurrent stray sweep cannot kill
  it.

## Commands

Requires Python 3.14 and uv (`.python-version` pins 3.14).

```sh
uv sync
uv run pytest -q -m "not slow"
uv run pytest -q --cov=capforge --cov-branch --cov-fail-under=71
uv run pytest -q            # full suite, real uv/git/uvicorn
uv run ruff check .
uv run mypy capforge
uv run capforge doctor
CAPFORGE_MCP_TOKEN=dev-token uv run capforge serve
uv run capforge tools
uv run capforge promote NAME [--tag T] [--note N] [--force]
uv run capforge test NAME
cd ui && bun install --frozen-lockfile && bun run build
```

pytest runs under xdist (`-n auto`) by default; pass `-n0` for a serial run. The suite is safe
this way because every test owns its temp caps/state dirs and the stray sweep is per-process.

`uv.lock` and `ui/bun.lock` are committed. After dependency changes run `uv lock` (or `bun
install`) and commit the lock. CI and the image use `--locked`/`--frozen-lockfile`.

## Tests

`test_units.py` is fast: parsing/validation, state/quarantine, hashes, share registry and census,
routes, console auth, sandbox policy, ten tools, UI mount. `test_llm_oidc.py` is fast: deploy
parsing, provider/selection stores, the gateway (fake upstream on loopback), OIDC discovery/PKCE
against a local provider, signed sessions and allow lists, and the console APIs. `test_lifecycle.py`
(slow) scaffolds real
uv projects, runs the pipeline, promotes, rolls back, tests data isolation/shared data,
cross-capability `ro`/`rw` grants and Landlock denial.
`test_web.py` (slow) boots real uvicorn children through `/cap`, tests restart/prune/restore,
process-group kills and console routes. Do not mock subprocesses or uv; every test owns a temp
caps/state. `-n auto` is safe because each test has its own dirs and the sweep is per-process.

Coverage floor is 71% (measured full suite 72.38%); ratchet it upward, never add blanket
`# pragma: no cover`.

## Definition of done

- `uv run --locked pytest -q --cov=capforge --cov-branch --cov-fail-under=71` passes.
- `uv run --locked ruff check .` and `uv run --locked mypy capforge` pass.
- `uv run --locked capforge tools` reports exactly ten tools.
- `uv run --locked capforge doctor` is clean; reports uv, git, Landlock, paths.
- `tools/mcp_probe.py` completes against a running server, including clone/push and the web round
  trip.
- If the contract changed: `contract.py`, README, `AGENTS.md.example` and this file together.
- No new runtime dependency without a clear reason; `pytest-cov` is dev-only.

## Browser Automation

Use `agent-browser` for web automation. Run `agent-browser --help` for all commands.

Core workflow:
1. `agent-browser open <url>` - Navigate to page
2. `agent-browser snapshot -i` - Get interactive elements with refs (@e1, @e2)
3. `agent-browser click @e1` / `fill @e2 "text"` - Interact using refs
4. Re-snapshot after page changes
