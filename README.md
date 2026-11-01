# capforge

Deterministic, versioned capabilities exposed over MCP, for Open WebUI + Open Terminal.

Design and internals: [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md). Known limitations and later
work: [`docs/NEXT.md`](docs/NEXT.md).

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

Requires **Python 3.14** (pinned by `.python-version`) and **uv**.

    uv sync    # installs from uv.lock (fastmcp + pytest/httpx/ruff/mypy dev tools)

`uv sync` installs the runtime dependency (fastmcp) and the default `dev` group. uv is the
supported toolchain; the lockfile is the source of truth.

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
| `CAPFORGE_LOG_LEVEL` | `INFO` | server log verbosity (`DEBUG`, `INFO`, `WARNING`, `ERROR`) |

## Deploy with Docker

    mkdir -p caps state
    sudo chown -R 10001:10001 caps state
    docker compose up --build

`docker-compose.yml` builds the local image, publishes `:8099`, and bind-mounts `./caps`
(the shared draft) and `./state`. Set `CAPFORGE_CAPS_HOST` to the host directory Open
Terminal scans (in `.env` or the shell), and `CAPFORGE_STATE_HOST` if state should live
elsewhere. To use the
image published to GHCR, remove the `build` block and set
`image: ghcr.io/<owner>/capforge:latest`.

Or without Compose:

    docker build -t capforge .
    docker run --rm -p 8099:8099 \
      --env-file .env.example \
      -v "$PWD/caps:/home/capforge/.agents/skills" \
      -v "$PWD/state:/var/lib/capforge" \
      capforge

The image is built with uv (`uv sync --locked --no-dev`), so a stale
`uv.lock` fails the build instead of silently resolving new versions. It runs as UID 10001,
listens on `:8099/mcp`, and declares a TCP healthcheck. `.env.example` documents every
variable. Mounted host folders must be writable by that UID.

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

A capability is executed as a fresh subprocess, once per call, in its own process group, with a
filtered environment (`PATH`, locale and TLS only, no `*_KEY`/`*_TOKEN`/`*_SECRET`, no pip index
credentials), a per-version data directory in `CAPFORGE_DATA_DIR`, a hard timeout that kills the
whole process tree, and bounded output capture. stdout may be one JSON value or end with one;
`{"error": "..."}` signals a handled failure the model should read.

An active tag runs its **own** `SKILL.md`: editing the draft's `entrypoint:` or `packages:` never
changes what the live version executes. State and venv builds are serialised with per-capability
locks, and pruning a tag also removes its per-version data and unused venvs.

## Layout on disk

    <CAPFORGE_CAPS_DIR>/<name>/                    the draft: the model's working copy
    <CAPFORGE_STATE_DIR>/versions/<name>/<tag>/     immutable snapshots
    <CAPFORGE_STATE_DIR>/venvs/<name>/<reqhash>/    one venv per requirement set
    <CAPFORGE_STATE_DIR>/data/<name>/<tag>/         per-version writable data
    <CAPFORGE_STATE_DIR>/state/<name>.json          active tag + tag index
    <CAPFORGE_STATE_DIR>/backups/<name>/<stamp>/    drafts archived by scaffold(overwrite)
    <CAPFORGE_STATE_DIR>/locks/<name>.lock          per-capability mutation lock

`CAPFORGE_CAPS_DIR` is also a git repo, initialised on first promotion; every promotion is a
commit named `<name>@<tag>`, so `git log` is the audit trail and `git revert` is a second way
back.

## Tests

    uv sync
    uv run pytest -q -m "not slow"   # fast unit tests, no venvs
    uv run pytest -q                 # full suite (builds real venvs, slower)
    uv run ruff check . && uv run mypy capforge
    uv run capforge serve &
    CAPFORGE_URL=http://127.0.0.1:8099/mcp uv run python tools/mcp_probe.py

`tools/mcp_probe.py` uses a unique capability name and cleans up its caps, tags, venvs, data and
locks afterwards.

## License

Apache-2.0. See [`LICENSE`](LICENSE).
