# Architecture

Status: describes capforge **0.4.0**, the git-authored architecture. If code and this document
disagree, the code wins; update this file in the same change.

Related: [`README.md`](../README.md), [`AGENTS.md`](../AGENTS.md),
[`AGENTS.md.example`](../AGENTS.md.example), [`NEXT.md`](NEXT.md).

## 1. What it is

Capabilities are **uv projects in git repositories**. The server owns the caps directory; the model
scaffolds a base project, clones the repository into its own workspace, edits, and pushes. The push
pipeline materialises the tree into a staging directory, tests it in a state-owned uv environment,
swaps it into the draft, promotes it to an immutable tag on green, and records
`refs/capforge/processed`.

There is no shared terminal mount. The model sees code through its clone or `get_capability`; it
cannot read state, tokens, venvs or other capabilities through a filesystem path.

## 2. Constraints

| Constraint | Consequence |
|---|---|
| `tools/list` runs every message and a changing set/description breaks the prompt cache. | The MCP surface is fixed at **ten** tools. Capabilities live behind them. |
| A capability created this turn must be callable this turn. | `staging` resolves names at execution time; `run_capability` reads the active tag. |
| No shared terminal skills directory. | Scaffold publishes a git repo; the model clones. Discovery is MCP + the repo's `AGENTS.md`. |
| Capability code is semi-trusted. | uv project pinning, a promotion gate, filtered env, Landlock, timeouts, process-group kills. |
| Python is fixed at 3.14. | `requires-python = ">=3.14,<3.15"`, `.python-version`, `uv sync --python 3.14`. |

## 3. Listeners and routes

Two uvicorn servers in one process, one signal set:

- **` :8099/mcp`** — FastMCP, the ten tools.
- **`:8080`** — starlette:

| path | handler |
|---|---|
| `/ui/` | static React console (public) |
| `/api/*` | console + control API (session + CSRF; `CAPFORGE_WEB_TOKEN` gates it) |
| `/api/caps/<name>/env` | env status (GET) / set one value (POST, write-only) |
| `/git/<name>.git/info/refs` | git advertisement (upload-pack or receive-pack) |
| `/git/<name>.git/git-upload-pack` | clone/fetch |
| `/git/<name>.git/git-receive-pack` | push |
| `/cap/<name>/` | reverse proxy to the served uvicorn child |
| `/` | `308` to `/ui/`, or JSON index when the UI is disabled |

No root `/{name}` catch-all. `CAPFORGE_PUBLIC_URL` is the externally reachable base used to build
clone/cap/UI URLs; a path-prefix proxy must strip the prefix before forwarding. TLS must terminate
in front for git tokens.

## 4. Modules

```text
core.py        paths, validation, state JSON, hashing, locks, processes, uv venvs, execute,
               tests, data dirs, web boot probe, stray sweep, Landlock policy
sandbox.py     Landlock ruleset + exec wrapper
versioning.py  snapshot, promote, prune, rollback, list_tags, diff
gitremote.py   bare repo init/config/managed hook, archive extraction, refs, gc/quota
scaffold.py    base uv project, uv lock, initial commit, publish main
staging.py     git tokens, staging state, bounded logs, async push pipeline
contract.py    CONTRACT text only
web.py         /ui /api /git /cap, console, git CGI bridge, serve engine
server.py      FastMCP and the ten tools
cli.py         serve | doctor | tools | promote | test
```

Dependency rules: `core`, `versioning`, `contract`, `staging`, `scaffold`, `gitremote` are
stdlib-only (gitremote uses the git binary). `web.py` owns starlette/httpx; `server.py` owns
fastmcp. `versioning` imports `web` lazily for prune/venv GC. `server.py` is an adapter; business
logic stays in the modules.

## 5. Authoring flow

1. `scaffold_capability(name, description, kind)`:
   - builds a uv project (`pyproject.toml`, `.python-version`, `SKILL.md`, entrypoint, tests,
     `AGENTS.md`, `.gitignore`);
   - runs `uv lock`;
   - commits and publishes `main` in `<state>/git/<name>.git` via a local fetch (no receive hook);
   - materialises the same tree as the server draft. An existing name/repo is refused; there is no
     overwrite mode.
2. `staging(name, action="token")` mints `cfg_<id>.<secret>`; only the hash is stored. One token
   grants clone and push for that capability; TTL `CAPFORGE_GIT_TOKEN_TTL` (600 s).
3. The model clones `CAPFORGE_PUBLIC_URL/git/<name>.git` with Basic auth, edits, runs
   `uv add`/`uv lock`, commits, and pushes `HEAD:main`.
4. `staging(action="latest")` polls the pipeline; `staging(action="logs")` returns the bounded
   pipeline/scaffold log.

The git routes authenticate before reading the body. `receive-pack` takes a per-name push lock,
checks the pipeline state, spools the body, runs `git http-backend` in a tracked process group, and
compares `rev-parse` before/after. A changed sha starts the worker; the response is git's own
report (HTTP 200 even for `ng`). `upload-pack` is read-only and does not take the push lock.
`refs/heads/main` only; deletes and non-fast-forwards are refused.

The managed `pre-receive` hook checks the pipeline lock and the ref policy. It is a backstop for a
non-HTTP local push; the route is authoritative for HTTP.

Deletion is explicit: `delete_capability(name, confirm)` requires `confirm == name`, refuses while
a push/pipeline is running, stops a served instance, and removes the draft, versions, venvs, data
(including shared), repo, state, staging, logs, locks and token records. `capforge delete` is the
operator equivalent.

## 6. Pipeline

```text
received -> extracting -> validating -> testing -> promoted | unchanged | failed | interrupted
```

- **Extracting.** `git archive refs/heads/main` into `<caps>/.staging/<name>-<id>/tree`, with
  guards: no symlinks/hardlinks/devices, no absolute/`..` members, no `.git`/`.venv`/
  `__pycache__`, bounded members/bytes/path length.
- **Validating.** `SKILL.md`, `pyproject.toml`, `uv.lock`, `.python-version`, entrypoint
  containment, `kind`, optional `data`; `packages` is rejected.
- **Testing.** `uv sync --frozen --group dev` into the test venv, pytest with capability coverage,
  then a prod smoke run (or web boot probe). Red leaves the draft/active untouched.
- **Swapping/promoting.** One `cap_lock` critical section: verify hash, archive old draft with
  `os.rename`, rename the tree into place, snapshot, record `env: "uv"`, `lock_hash`, `python`,
  `git_source`, `pushed_by`, set `active`, prune, write state. Failure restores the old draft.
  `versioning.promote(locked=True)` is used because the worker already holds `cap_lock`.
- **Unchanged.** Same content hash: no tag; `refs/capforge/processed` still advances.
- **Crash recovery.** Any non-terminal state with a stale heartbeat becomes `interrupted`; startup
  reconciles `refs/heads/main` with `refs/capforge/processed`.

A failed push leaves the old draft and active tag. A promotion does not restart a served web
instance; `serve_capability(action="restart")` is explicit.

## 7. Execution

`run_capability` loads the active tag's own `SKILL.md`, ensures the prod uv venv for its lock hash,
and runs one subprocess:

- JSON on stdin, JSON on stdout; bounded tails;
- filtered environment (`PATH`, locale, TLS; no `*_KEY`/`*_TOKEN`/`*_SECRET`, no index credentials);
- values for the names a version declares under `env:` in `SKILL.md`, read from
  `<state>/secrets/<name>.json`; only the active tag receives real values;
- `CAPFORGE_DATA_DIR` per version, `CAPFORGE_SHARED_DATA_DIR` when `data: shared`,
  `CAPFORGE_VERSION`, venv-first `PATH`;
- hard timeout on the process group; the group is killed after wait; detached strays are swept;
- Landlock allowlist for filesystem access.

A draft is used only before the first promotion, with a warning. Web capabilities are refused by
`run_capability` and served instead. Tests and the web boot probe receive
`capforge-test-placeholder` for declared env names, so real values never enter model-visible
pipeline logs. A missing active value fails the run/serve with the missing names.

## 8. Data

- `CAPFORGE_DATA_DIR=<state>/data/<name>/<tag>/` (or `draft`/`test`): writable per-version data,
  removed when the tag is pruned. A new tag starts empty.
- `data: shared` adds `CAPFORGE_SHARED_DATA_DIR=<state>/data/<name>/shared/`: stable across tags,
  not pruned or snapshotted, not in `tree_hash`. The capability owns concurrency, migrations and
  backup expectations.
- Code trees are never data stores. Fixtures/templates committed in the repo are read-only at
  runtime.
- The model cannot read `<state>`. CLI capabilities return file content/URLs in JSON; web
  capabilities serve downloads through their own routes; an optional
  `get_capability(action="data")` can read a contained tail.

## 9. Versioning and storage

```text
<caps>/<name>/                              derived draft
<state>/versions/<name>/<tag>/              immutable snapshot
<state>/venvs/<name>/<lock_hash>-py314[-test]/
<state>/data/<name>/<tag>/                  per-version data
<state>/data/<name>/shared/                 optional durable data
<state>/state/<name>.json                   active + tags + pruned
<state>/secrets/<name>.json                 operator-set env values (0600)
<state>/staging/<name>.json                 pipeline state
<state>/git/<name>.git/                     bare repo
<state>/logs/<name>/{pipeline,scaffold}.log
<state>/served/registry.json                web desired state
<state>/served/<name>/<tag>/server.log
<state>/locks/                              cap/venv/push/pipeline locks
```

State writes are atomic; corrupt files are quarantined. Tags are immutable; the active tag is never
pruned. Pruning removes per-version data and unused venvs, stops a pinned web instance, and never
touches shared data. There is no caps-root audit repository; the capability repo is the code
history and state plus `refs/capforge/processed` is the promotion record.

## 10. Security

- MCP `:8099` is unauthenticated by default; treat it as perimeter-only.
- `/api` needs the console token and CSRF on writes. `/ui` is public. `/cap` is public by design.
- `/git` needs a per-capability TTL token; one token grants clone and push; Basic only. Tokens are hashed,
  request/byte-capped, never logged, and Basic usernames are bound to the actor; Bearer is not accepted.
- Capability environments are filtered; index settings are installer-only. Declared env values
  live under `<state>/secrets/` (mode `0600`) and are write-only through `/api`; they are shared
  across a capability's tags and injected only into its active run/serve process. Capability code
  can read what it is given at runtime, so this is a trust boundary around the operator-provided
  values, not a sandbox around model-authored code.
- Landlock (`CAPFORGE_SANDBOX=require`) denies state, tokens, other capabilities, home, `/proc`,
  `/sys`, `/tmp` and anything else not explicitly allowed. It is filesystem-only; network, CPU,
  memory and ptrace are not confined.
- Git packfiles are untrusted. `git http-backend` runs with a sanitized env, `fsckObjects`,
  `maxInputSize`, a hard timeout and a process-group kill. Keep git current.
- `uv sync` executes third-party build backends. Sources are restricted; it runs with the install
  env and a timeout.
- Docker runs as a remapped unprivileged user; state and caps are separate volumes.

## 11. Tests

- `test_units.py` fast: parsing/state/hashes, routes, console auth, sandbox policy, ten tools, UI
  mount.
- `test_lifecycle.py` slow: scaffold, uv env, promote/rollback, push pipeline (green/red), data
  isolation/shared data, Landlock denial, prune.
- `test_web.py` slow: real uvicorn children, `/cap` proxy, restart/prune/restore, process-group
  kills, console.
- CI/DoD: full suite with coverage floor 69% (measured 70.39%), ruff, mypy, ten-tool surface.
  `tools/mcp_probe.py` exercises real MCP HTTP plus clone/push and the web round trip.
