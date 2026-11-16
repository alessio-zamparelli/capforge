# capforge

Deterministic, versioned capabilities exposed over MCP, for Open WebUI + Open Terminal.

Design and internals: [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md). Known limitations and later
work: [`docs/NEXT.md`](docs/NEXT.md).

The model writes a capability folder in a directory it can also reach through the terminal,
tests the draft, promotes it to an immutable tag, and from then on `run_capability` executes
that tag. Rollback is a pointer move; the working copy is never touched. A capability with
`kind: web` is instead a long-lived HTTP service, reverse-proxied at `http://<host>:8080/<name>/`,
with a token-gated web console to start and stop it.

## Why the shape is what it is

Three constraints from Open WebUI 0.12.x drove the design, all verified in its source:

1. **The tool list is fixed for the duration of a turn.** `process_chat_payload` resolves MCP
   servers and calls `tools/list` on every message, so a new tool appears on the *next* turn,
   never within the one that created it. A dispatcher (`run_capability(name, args)`) resolves
   the target *inside* execution, which closes the loop within a single turn.
2. **A changing tool list destroys the provider prompt cache.** Tools reach the model as
   `<server_id>_<tool_name>`; if the set or its descriptions change, the prefix changes and the
   whole context is re-billed. So the MCP surface here is fixed at eleven tools (one of which,
   `serve_capability`, is itself a dispatcher over actions) and capabilities live behind it.
3. **Terminal skills are read every turn from `.agents/skills`** (also `.cptr`, `.claude`,
   `.codex`) under the terminal's home, and `AGENTS.md` is re-read from the same home. Pointing
   `CAPFORGE_CAPS_DIR` there gives the model a per-turn manifest of every capability for free,
   and the folder is the same one the terminal can edit.

## Install

Requires **Python 3.14** (pinned by `.python-version`), **uv**, and — only if you want to
rebuild the console bundle — **bun**.

    uv sync    # installs from uv.lock (fastmcp + starlette/httpx + pytest/ruff/mypy dev tools)

`uv sync` installs the runtime dependencies and the default `dev` group. uv is the
supported toolchain; the lockfile is the source of truth. The console bundle ships
pre-built in the Docker image; locally, serve it after `cd ui && bun install --frozen-lockfile
&& bun run build`, or set `CAPFORGE_WEB_UI=0` to use the JSON index instead.

## Run

    uv run capforge doctor                  # paths, permissions, fastmcp, web config
    uv run capforge serve                   # MCP on :8099/mcp + web listener on :8080
    uv run capforge serve --transport stdio # for a local MCP client (web listener skipped)

Environment:

| variable | default | meaning |
|---|---|---|
| `CAPFORGE_CAPS_DIR` | `~/.agents/skills` | folder the model edits; must be a path Open WebUI scans |
| `CAPFORGE_STATE_DIR` | `~/.capforge` | versions, venvs, per-version data, state JSON |
| `CAPFORGE_KEEP_TAGS` | `5` | tags kept per capability; the active one is never pruned |
| `CAPFORGE_RUN_TIMEOUT` | `120` | seconds per `run_capability` |
| `CAPFORGE_TEST_TIMEOUT` | `300` | seconds per test run |
| `CAPFORGE_INSTALL_TIMEOUT` | `240` | seconds per pip install |
| `CAPFORGE_HOST` / `CAPFORGE_PORT` / `CAPFORGE_PATH` | `0.0.0.0` / `8099` / `/mcp` | MCP transport |
| `CAPFORGE_WEB_HOST` / `CAPFORGE_WEB_PORT` | `0.0.0.0` / `8080` | reverse proxy + console; port `0` disables the whole listener |
| `CAPFORGE_WEB_TOKEN` | unset | console/control-API access token; **unset = console disabled**, the value is never exposed or logged |
| `CAPFORGE_WEB_UI` / `CAPFORGE_UI_DIR` | `1` / `ui/dist` | serve the built console bundle (0 = JSON index) |
| `CAPFORGE_SERVE_BOOT_TIMEOUT` | `60` | seconds a `kind: web` capability gets to bind and answer HTTP (promotion gate and serve start) |
| `CAPFORGE_PUBLIC_URL` | unset | overrides the host:port part of served URLs (e.g. `https://tools.example.com/caps`) |
| `CAPFORGE_LOG_LEVEL` | `INFO` | server log verbosity (`DEBUG`, `INFO`, `WARNING`, `ERROR`) |
| `CAPFORGE_GUIDE` | `1` | install the generated `_capforge` guide skill on serve; set `0` to disable |
| `PUID` / `PGID` | `10001` / `10001` | remap the container user to the host owner; the entrypoint chowns the mounts and drops privileges |

## Deploy with Docker

    mkdir -p caps state
    PUID=$(id -u) PGID=$(id -g) docker compose up --build

`docker-compose.yml` builds the local image, publishes `:8099` (MCP) and `:8080` (web proxy +
console), and bind-mounts `./caps` (the shared draft) and `./state`. Set `CAPFORGE_CAPS_HOST`
to the host directory Open Terminal scans (in `.env` or the shell), and `CAPFORGE_STATE_HOST`
if state should live elsewhere. To use the image published to GHCR, remove the `build` block
and set `image: ghcr.io/<owner>/capforge:latest`.

The entrypoint starts as root only long enough to remap the app user to `PUID`/`PGID`,
recursively chown the caps and state mounts, and then drop privileges with `setpriv`. With
`docker run --user` or rootless Docker the remap is skipped, so pre-chown the mounts:

    sudo chown -R "$(id -u):$(id -g)" caps state

Or without Compose:

    docker build -t capforge .
    docker run --rm -p 8099:8099 -p 8080:8080 \
      -e PUID="$(id -u)" -e PGID="$(id -g)" \
      --env-file .env.example \
      -v "$PWD/caps:/home/capforge/.agents/skills" \
      -v "$PWD/state:/var/lib/capforge" \
      capforge

The image is built with uv (`uv sync --locked --no-dev`), so a stale
`uv.lock` fails the build instead of silently resolving new versions. The console bundle is
built in a `oven/bun` stage (`bun install --frozen-lockfile && bun run build`) and copied in.
The image defaults to UID/GID 10001, remaps to `PUID`/`PGID` when started as root, listens on
`:8099/mcp` and `:8080`, and declares a TCP healthcheck. `.env.example` documents every
variable, including `CAPFORGE_WEB_TOKEN` (commented out — the console is disabled by default).

CI runs the test suite, then builds and pushes the image to GHCR on pushes to `main`
and on `v*.*.*` tags: `ghcr.io/<owner>/capforge`. The workflow is
`.github/workflows/deploy.yml`; Dependabot updates Actions, pip dependencies and the
base image weekly via `.github/dependabot.yml`.

## Wire it into Open WebUI

Settings > Admin > Integrations > External Tool Servers > **+ Add Connection**:

* Type: **MCP (Streamable HTTP)**
* URL: `http://capforge:8099/mcp`
* Auth: **None** (it is on an internal network) or **Bearer** with a key you terminate at the proxy

Keep the connection ID short: tool names become `<server_id>_<tool_name>`, and providers cap
them at 64 characters. `capforge` is fine; `alessio-capabilities-prod` is not.

Both services must see the *same host directory*. `docker-compose.yml` mounts
`${CAPFORGE_CAPS_HOST:-./caps}` at `/home/capforge/.agents/skills`; mount that same host
directory into the terminal container at its skills home. Only the host path has to match,
but `CAPFORGE_CAPS_DIR` must point at capforge's mount:

    services:
      open-terminal:
        volumes: [ "/srv/caps:/home/user/.agents/skills" ]
      capforge:
        volumes: [ "/srv/caps:/home/capforge/.agents/skills" ]
        environment:
          CAPFORGE_CAPS_DIR: /home/capforge/.agents/skills
          CAPFORGE_STATE_DIR: /var/lib/capforge

`CAPFORGE_STATE_DIR` should be its own volume: it holds the tags, the venvs and the data.

## The eleven tools

On startup, `capforge serve` installs a generated guide at `<caps>/_capforge/SKILL.md` so the
model's per-turn skill manifest explains the MCP loop. The leading `_` reserves the name: it is
not listed as a capability, and `NAME_RE` rejects it as a capability name. A file without the
managed marker is never overwritten; set `CAPFORGE_GUIDE=0` to skip the injection.

| tool | purpose |
|---|---|
| `get_contract` | the folder layout and the stdin/stdout rules; the model should read it first |
| `list_capabilities` | name, description, active tag, whether the draft differs from it |
| `get_capability` | SKILL.md as written, frontmatter, hashes, tags, active version |
| `scaffold_capability` | create `SKILL.md` + a working `main.py` + a passing test |
| `install_packages` | per-capability venv, recorded in SKILL.md |
| `test_capability` | pytest (or a smoke run) on the draft, optionally one run with `args` |
| `run_capability` | execute the **active tag**; falls back to the draft only if never promoted |
| `serve_capability` | `start / stop / restart / status` the long-lived server for a `kind: web` capability's **active tag**; drafts are never served |
| `promote_capability` | tests, then snapshot to an immutable tag, then make it active |
| `rollback_capability` | point at an older tag; the working copy is untouched |
| `list_tags` | tags, active marker, optional diff of the draft against the active version |

## Capability contract

    <caps>/<name>/
        SKILL.md            frontmatter + instructions, read by Open WebUI every turn
        main.py             entrypoint; `entrypoint:` in frontmatter overrides
        tests/test_main.py  optional; pytest runs when test_*.py exists

    python main.py  <  JSON args on stdin  ->  JSON on stdout, exit 0

Frontmatter fields that matter: `name`, `description` (the only thing in the manifest),
`entrypoint`, `packages: [pypdf>=5, httpx]`.

A capability is executed as a fresh subprocess, once per call, in its own process group, with a
filtered environment (`PATH`, locale and TLS only, no `*_KEY`/`*_TOKEN`/`*_SECRET`, no pip index
credentials), a per-version data directory in `CAPFORGE_DATA_DIR`, a hard timeout that kills the
whole process tree, and bounded output capture. stdout may be one JSON value or end with one;
`{"error": "..."}` signals a handled failure the model should read.

An active tag runs its **own** `SKILL.md`: editing the draft's `entrypoint:` or `packages:` never
changes what the live version executes. State and venv builds are serialised with per-capability
locks, and pruning a tag also removes its per-version data and unused venvs.

## Web capabilities (`kind: web`)

Frontmatter `kind: web` turns a capability into a long-lived HTTP service instead of a one-shot
call:

    ---
    name: notes_api
    description: CRUD for small notes, over HTTP.
    kind: web
    entrypoint: app.py
    packages: [fastapi, uvicorn]
    ---

`entrypoint` names the file holding the ASGI `app` object (optional `app:` overrides the object
name), and `uvicorn` must be in `packages`. `scaffold_capability(..., kind="web")` creates this
skeleton with a FastAPI app, a `/health` route and a test. The promotion gate is a real boot:
`test_capability` starts the app with uvicorn on a throwaway port and probes it over HTTP, so a
web capability that cannot start is never promoted.

After promotion, `serve_capability(name, action="start")` boots the **active tag** as a uvicorn
child bound to `127.0.0.1` and reverse-proxies it at `http://<host>:8080/<name>/<path>`.
Endpoints are auto-detected from the child's `/openapi.json` when present. `action="restart"`
moves a running instance to a newly promoted tag; `stop` stops it. Instances are pinned to a tag
— a promotion alone does not touch a running instance, and pruning a tag stops the instance
pinned to it first. The desired state is persisted, so a server restart restores instances whose
latest state was `start`.

Guarantees carried over from CLI capabilities: filtered environment, per-version data directory
(`CAPFORGE_DATA_DIR`, plus `CAPFORGE_SERVE_PORT` and `CAPFORGE_PUBLIC_URL` for the child), boot
budget instead of run timeout, and — because a call must leave nothing running — a call that
forks a daemon loses it: detached descendants are re-parented to the server via
`PR_SET_CHILD_SUBREAPER` and swept; in-group children die with the process group. The run
timeout does not apply to a served instance; it is a resident service, by design.

## Console

`http://<host>:8080/` serves a React (Vite + Tailwind, zinc dark theme) console listing all
capabilities: kind, active tag, draft state, and — for served web capabilities — a collapsible
endpoint list (auto-detected from `/openapi.json`) with Start/Stop/Restart controls, polling
every ~3s.

The console and its control API require a token login: set `CAPFORGE_WEB_TOKEN` to a long random
string and sign in with it. Sessions are HTTP-only cookies; state-changing calls additionally
require a `X-CSRF: 1` header; failed logins back off after five tries. **Unset token = console
disabled** (login and `/api/*` return 403) while the capability proxy keeps working — that is
the safe default when `:8080` is exposed. The token value is never exposed through any API,
schema, error message or log line.

Without the token the web root returns a compact JSON index of served capabilities instead, so
callers and scripts can still discover what is being served. WebSockets/streaming upgrades are
not proxied (see `docs/NEXT.md`).

## Layout on disk

    <CAPFORGE_CAPS_DIR>/<name>/                    the draft: the model's working copy
    <CAPFORGE_STATE_DIR>/versions/<name>/<tag>/     immutable snapshots
    <CAPFORGE_STATE_DIR>/venvs/<name>/<reqhash>/    one venv per requirement set
    <CAPFORGE_STATE_DIR>/data/<name>/<tag>/         per-version writable data
    <CAPFORGE_STATE_DIR>/state/<name>.json          active tag + tag index
    <CAPFORGE_STATE_DIR>/served/registry.json       serve registry (desired state per capability)
    <CAPFORGE_STATE_DIR>/served/<name>/<tag>/server.log  uvicorn child log, rotated
    <CAPFORGE_STATE_DIR>/backups/<name>/<stamp>/    drafts archived by scaffold(overwrite)
    <CAPFORGE_STATE_DIR>/locks/<name>.lock          per-capability mutation lock

`CAPFORGE_CAPS_DIR` is also a git repo, initialised on first promotion; every promotion is a
commit named `<name>@<tag>`, so `git log` is the audit trail and `git revert` is a second way
back.

## Tests

    uv sync
    uv run pytest -q -m "not slow"   # fast unit tests, no venvs
    uv run pytest -q                 # full suite (builds real venvs, slower)
    uv run pytest -q -n auto         # the same, parallel (pytest-xdist)
    uv run ruff check . && uv run mypy capforge
    uv run capforge serve &
    CAPFORGE_URL=http://127.0.0.1:8099/mcp uv run python tools/mcp_probe.py

`tools/mcp_probe.py` uses a unique capability name and cleans up its caps, tags, venvs, data and
locks afterwards; its web round trip also starts and stops a served instance and drops its
registry entry.

## License

Apache-2.0. See [`LICENSE`](LICENSE).
