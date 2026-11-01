# capforge

Deterministic, versioned capabilities exposed over MCP, for Open WebUI + Open Terminal.

Design and internals: [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).

The model writes a capability folder in a directory it can also reach through the terminal,
tests the draft, promotes it to an immutable tag, and from then on `run_capability` executes
that tag. Rollback is a pointer move; the working copy is never touched.

## Why the shape is what it is

Three constraints from Open WebUI 0.12.x drove the design, all verified in its source:

1. **The tool list is fixed for the duration of a turn.** `process_chat_payload` resolves MCP
   servers and calls `tools/list` on every message, so a new tool appears on the *next* turn,
   never within the one that created it. A dispatcher (`run_capability(name, args)`) resolves
   the target *inside* execution, which closes the loop within a single turn.
2. **A changing tool list destroys the provider prompt cache.** Tools reach the model as
   `<server_id>_<tool_name>`; if the set or its descriptions change, the prefix changes and the
   whole context is re-billed. So the MCP surface here is fixed at ten tools and capabilities
   live behind it.
3. **Terminal skills are read every turn from `.agents/skills`** (also `.cptr`, `.claude`,
   `.codex`) under the terminal's home, and `AGENTS.md` is re-read from the same home. Pointing
   `CAPFORGE_CAPS_DIR` there gives the model a per-turn manifest of every capability for free,
   and the folder is the same one the terminal can edit.

## Install

    uv sync    # installs from uv.lock (fastmcp + pytest/httpx dev tools)

`uv sync` installs the runtime dependency (fastmcp) and the default `dev` group. Without uv,
`pip install -e . pytest httpx` still works, but the lockfile is the source of truth.

## Run

    uv run capforge doctor                  # paths, permissions, fastmcp
    uv run capforge serve                   # MCP over streamable HTTP on :8099/mcp
    uv run capforge serve --transport stdio # for a local MCP client

Environment:

| variable | default | meaning |
|---|---|---|
| `CAPFORGE_CAPS_DIR` | `~/.agents/skills` | folder the model edits; must be a path Open WebUI scans |
| `CAPFORGE_STATE_DIR` | `~/.capforge` | versions, venvs, per-version data, state JSON |
| `CAPFORGE_KEEP_TAGS` | `5` | tags kept per capability; the active one is never pruned |
| `CAPFORGE_RUN_TIMEOUT` | `120` | seconds per `run_capability` |
| `CAPFORGE_TEST_TIMEOUT` | `300` | seconds per test run |
| `CAPFORGE_INSTALL_TIMEOUT` | `240` | seconds per pip install |
| `CAPFORGE_HOST` / `CAPFORGE_PORT` / `CAPFORGE_PATH` | `0.0.0.0` / `8099` / `/mcp` | transport |

## Deploy with Docker

    docker build -t capforge .
    docker run --rm -p 8099:8099 \
      --env-file .env.example \
      -v capforge-caps:/home/capforge/.agents/skills \
      -v capforge-state:/var/lib/capforge \
      capforge

The image is built with uv (`uv sync --locked --no-dev`), so a stale
`uv.lock` fails the build instead of silently resolving new versions. It runs as UID 10001,
listens on `:8099/mcp`, and declares a TCP healthcheck.
`.env.example` documents every variable. Mounted host folders must be writable by that
UID. Keep the state directory on its own volume, and mount the caps directory at the
same host path the Open Terminal container scans (see the compose example below).

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

Both services must share the caps directory. In compose, mount the *same host path* into the
terminal container and into capforge, and let `CAPFORGE_CAPS_DIR` point at the terminal's home
so the paths agree:

    services:
      open-terminal:
        volumes: [ "/srv/caps:/home/user/.agents/skills" ]
      capforge:
        volumes: [ "/srv/caps:/home/user/.agents/skills" ]
        environment:
          CAPFORGE_CAPS_DIR: /home/user/.agents/skills
          CAPFORGE_STATE_DIR: /var/lib/capforge

`CAPFORGE_STATE_DIR` should be its own volume: it holds the tags, the venvs and the data.

## The ten tools

| tool | purpose |
|---|---|
| `get_contract` | the folder layout and the stdin/stdout rules; the model should read it first |
| `list_capabilities` | name, description, active tag, whether the draft differs from it |
| `get_capability` | SKILL.md as written, frontmatter, hashes, tags, active version |
| `scaffold_capability` | create `SKILL.md` + a working `main.py` + a passing test |
| `install_packages` | per-capability venv, recorded in SKILL.md |
| `test_capability` | pytest (or a smoke run) on the draft, optionally one run with `args` |
| `run_capability` | execute the **active tag**; falls back to the draft only if never promoted |
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

A capability is executed as a fresh subprocess, once per call, with a filtered environment
(`PATH`, locale, no `*_KEY`/`*_TOKEN`/`*_SECRET`), a per-version data directory in
`CAPFORGE_DATA_DIR`, and a hard timeout. stdout must end with one JSON value; `{"error": "..."}`
signals a handled failure the model should read.

## Layout on disk

    <CAPFORGE_CAPS_DIR>/<name>/            the draft: the model's working copy
    <CAPFORGE_STATE_DIR>/versions/<name>/<tag>/   immutable snapshots
    <CAPFORGE_STATE_DIR>/venvs/<name>/<reqhash>/  one venv per requirement set
    <CAPFORGE_STATE_DIR>/data/<name>/<tag>/       per-version writable data
    <CAPFORGE_STATE_DIR>/state/<name>.json        active tag + tag index

`CAPFORGE_CAPS_DIR` is also a git repo, initialised on first promotion; every promotion is a
commit named `<name>@<tag>`, so `git log` is the audit trail and `git revert` is a second way
back.

## Tests

    uv sync
    uv run pytest -q   # 24 tests, ~65s (they build real venvs)
    uv run capforge serve &
    CAPFORGE_URL=http://127.0.0.1:8099/mcp uv run python tools/mcp_probe.py
