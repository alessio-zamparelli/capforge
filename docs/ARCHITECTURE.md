# Architecture

Status: describes capforge **0.7.1**, the git-authored architecture. If code and this document
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

- **`:8099/mcp`** — FastMCP, the ten tools, bearer-authenticated with
  `CAPFORGE_MCP_TOKEN`.
- **`:8080`** — the web starlette app: console, API, git and capability proxy.

| path | handler |
|---|---|
| `/ui/` | static React console (public, token-gated API) |
| `/api/*` | console + control API (session + CSRF; `CAPFORGE_WEB_TOKEN` gates it) |
| `/api/caps/<name>` | detail: tags, pipeline, served state, env status |
| `/api/caps/<name>/logs` | bounded pipeline/scaffold/served log tail |
| `/api/caps/<name>/rollback` | move the active tag |
| `/api/caps/<name>/delete` | delete the capability (exact-name confirm) |
| `/api/caps/<name>/env` | env status (GET) / set one value (POST, write-only) |
| `/git/<name>.git/info/refs` | git advertisement (upload-pack or receive-pack) |
| `/git/<name>.git/git-upload-pack` | clone/fetch |
| `/git/<name>.git/git-receive-pack` | push |
| `/cap/<name>/` | reverse proxy to the served uvicorn child |
| `/healthz` / `/readyz` | liveness / readiness |
| `/` | `308` to `/ui/`, or JSON index when the UI is disabled |

No root `/{name}` catch-all. `CAPFORGE_PUBLIC_URL` is the externally reachable base for clone,
capability and console URLs; a path-prefix proxy must strip the prefix before forwarding. TLS must
terminate in front for git tokens and the console.

`/cap` and `/api` share this origin. A served web capability's JavaScript can therefore reach the
console API with the operator's session. The proxy strips `Cookie`, `Authorization`, `X-CSRF` and
upstream `Set-Cookie`, but that is not a same-origin boundary: deployments that serve untrusted web
capabilities should expose `/cap` on a different origin at the reverse proxy (see `NEXT.md`).

## 4. Modules

```text
core.py        paths, validation, state JSON, hashing, locks, processes, uv venvs, execute,
               tests, data dirs, web boot probe, stray sweep, Landlock policy
sandbox.py     Landlock ruleset + exec wrapper
versioning.py  snapshot, promote, prune, rollback, list_tags, diff
gitremote.py   bare repo init/config/managed hook, archive extraction, refs, gc
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
   - builds a uv project (`pyproject.toml`, `.python-version`, `SKILL.md`, `deploy.py`, entrypoint,
     tests, `AGENTS.md`, `.gitignore`);
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
a push/pipeline is running, stops a served instance, and removes the draft, versions, venvs, its own
data (including `DATA = "shared"`), repo, state, staging, logs, locks and token records. It revokes
the capability's grants in cross-capability shared folders but keeps those folders and their data.
`capforge delete` is the operator equivalent.

## 6. Pipeline

```text
received -> extracting -> validating -> testing -> promoted | unchanged | failed | interrupted
```

- **Extracting.** `git archive refs/heads/main` into `<caps>/.staging/<name>-<id>/tree`, with
  guards: no symlinks/hardlinks/devices, no absolute/`..` members, no `.git`/`.venv`/
  `__pycache__`, bounded members/bytes/path length.
- **Validating.** `SKILL.md` (name/description), `deploy.py`, `pyproject.toml`, `uv.lock`,
  `.python-version`, entrypoint containment, `KIND`, optional `DATA`/`APP`/`SHARES` and the `Env`
  model. `deploy.py` is parsed with `ast`, never imported. Legacy frontmatter
  `kind`/`entrypoint`/`app`/`data`/`env`/`packages` is rejected with a migration message.
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
- values for the `Env` fields a version declares in `deploy.py`, read from
  `<state>/secrets/<name>.json`; optional fields fall back to their declared default, and only the
  active tag receives real values;
- `CAPFORGE_DATA_DIR` per version, `CAPFORGE_SHARED_DATA_DIR` when `DATA = "shared"`,
  `CAPFORGE_SHARES` for approved shared volumes, `CAPFORGE_VERSION`, venv-first `PATH`;
- hard timeout on the process group; the group is killed after wait; detached strays are swept;
- Landlock allowlist for filesystem access, including `ro` shares as read and `rw` shares as write.

`SHARES` is a request list, not authorization. Resolution reads `<state>/state/shares.json`; a
missing grant or a grant that differs from the request fails the run/serve closed. Tests and the web boot
probe get fresh empty temporary share directories with the requested access, so pipeline logs never
expose production shared data.

A draft is used only before the first promotion, with a warning. Web capabilities are refused by
`run_capability` and served instead. Tests and the web boot probe receive the declared default or a
type-shaped placeholder (`capforge-test-placeholder`, `0`, `0.0`, `false`) for env fields, so real
values never enter model-visible pipeline logs. A missing required active value fails the
run/serve with the missing names.

Web children are booted with uvicorn `--root-path /cap/<name>` and `CAPFORGE_ROOT_PATH`; the proxy
strips the prefix before forwarding, so app routes stay unprefixed. This replaces the deprecated
`FastAPI(root_path=...)` parameter: FastAPI derives `/docs` and `/openapi.json` URLs from the ASGI
scope instead.

## 8. Data

- `CAPFORGE_DATA_DIR=<state>/data/<name>/<tag>/` (or `draft`/`test`): writable per-version data,
  removed when the tag is pruned. A new tag starts empty.
- `DATA = "shared"` adds `CAPFORGE_SHARED_DATA_DIR=<state>/data/<name>/shared/`: stable across tags,
  not pruned or snapshotted, not in `tree_hash`. The capability owns concurrency, migrations and
  backup expectations.
- `SHARES` grants access to named, operator-owned folders under `<state>/shares/<name>/`. Grants
  are per capability, shared across its tags, and fixed at spawn for a web child. `ro` is read-only
  in Landlock; `rw` permits create/update/delete. The hub does not synchronize shared data, so the
  capability owns locking. A grant removal or change restarts a desired-running web child.
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
<state>/data/<name>/shared/                 optional durable data (per capability)
<state>/shares/<folder>/                    operator-owned cross-capability data
<state>/state/<name>.json                   active + tags + pruned
<state>/state/shares.json                   shared-folder census and grants
<state>/secrets/<name>.json                 operator-set env values (0600)
<state>/staging/<name>.json                 pipeline state
<state>/git/<name>.git/                     bare repo
<state>/logs/<name>/{pipeline,scaffold}.log
<state>/served/registry.json                web desired state
<state>/served/<name>/<tag>/server.log
<state>/locks/                              cap/venv/push/pipeline/share locks
```

State writes are atomic; corrupt files are quarantined. Tags are immutable; the active tag is never
pruned. Pruning removes per-version data and unused venvs, stops a pinned web instance, and never
touches shared data. There is no caps-root audit repository; the capability repo is the code
history and state plus `refs/capforge/processed` is the promotion record.

## 10. Security

- MCP `:8099` requires `CAPFORGE_MCP_TOKEN` for HTTP transport; clients send
  `Authorization: Bearer <token>`. `--transport stdio` is local and does not need it.
- `/api` needs the console token and CSRF on writes. `/ui` is public. `/cap` is public by design.
- `/cap` and `/api` share an origin. The proxy strips `Cookie`, `Authorization`, `X-CSRF`, incoming
  `x-capforge-*` and upstream `Set-Cookie`, but a served capability's own JavaScript can still call
  `/api` with the operator's session. Treat web capabilities as trusted, or expose `/cap` on a
  distinct origin at the reverse proxy.
- `/git` needs a per-capability TTL token; one token grants clone and push; Basic only. Tokens are hashed,
  request/byte-capped, never logged, and Basic usernames are bound to the actor; Bearer is not accepted.
- Capability environments are filtered; index settings are installer-only. Declared `Env` fields
  live under `<state>/secrets/` (mode `0600`) and are write-only through `/api`; the console
  validates the declared type before storing. They are shared across a capability's tags and
  injected only into its active run/serve process. Capability code can read what it is given at
  runtime, so this is a trust boundary around the operator-provided values, not a sandbox around
  model-authored code.
- Shared grants are operator-only. Capability code can request a folder in `deploy.py` but has no
  path to approve itself: `/api/shares` and `/api/caps/{name}/shares/{folder}` require the console
  session and CSRF, and no MCP tool exposes them. All grantees can read all contents; `rw` is
  mutual trust because any grantee can modify or delete data. Symlinks inside a share cannot escape
  Landlock because rules are path-beneath and resolved at access time.
- Landlock (`CAPFORGE_SANDBOX=require`) denies state, tokens, other capabilities, home, `/proc`,
  `/sys`, `/tmp` and anything else not explicitly allowed, apart from the exact approved share
  folders. It is filesystem-only; network, CPU, memory and ptrace are not confined.
- Git packfiles are untrusted. `git http-backend` runs with a sanitized env, `fsckObjects`,
  `maxInputSize`, a hard timeout and a process-group kill. Keep git current.
- `uv sync` executes third-party build backends. Sources are restricted; it runs with the install
  env and a timeout.
- Docker runs as a remapped unprivileged user; state and caps are separate volumes.

## 11. Tests

- `test_units.py` fast: parsing/state/hashes, share registry and census, routes, console auth,
  sandbox policy, ten tools, UI mount.
- `test_lifecycle.py` slow: scaffold, uv env, promote/rollback, push pipeline (green/red), data
  isolation/shared data, cross-capability `ro`/`rw` grants and Landlock denial, prune.
- `test_web.py` slow: real uvicorn children, `/cap` proxy, restart/prune/restore, process-group
  kills, console.
- CI/DoD: full suite with coverage floor 71% (measured 72.38%), ruff, mypy, ten-tool surface.
  `tools/mcp_probe.py` exercises real MCP HTTP plus clone/push and the web round trip.
