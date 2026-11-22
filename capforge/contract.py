"""The contract the model reads before writing a capability, and the scaffolds it starts from."""

from __future__ import annotations

CONTRACT = """\
# Capability contract (git-authored)

Capabilities are uv projects in a git repository. The server owns the caps directory; you do not
have a shared filesystem mount. Work in a clone.

    scaffold_capability(name, description)
    staging(name, action="token")      # short-lived clone/push token + git_url
    git clone <git_url>                # your workspace
    edit, uv add, uv lock, commit
    git push origin HEAD:main
    staging(name, action="latest")     # received -> ... -> promoted | failed
    staging(name, action="logs")       # bounded pipeline/test log

The pipeline tests the pushed tree in a state-owned uv environment. Only a green push is swapped
into the server draft and promoted. There is no manual promote/test/install tool.

## Project layout

    <name>/
        SKILL.md            frontmatter + instructions; env: [NAME, ...] declares
                            operator-set values; no packages field
        pyproject.toml      dependencies; requires-python = ">=3.14,<3.15"
        uv.lock             committed; `uv sync --frozen` is the only install path
        .python-version     3.14
        main.py | app.py    entrypoint (`entrypoint:`, `app:`)
        tests/              pytest; the dev group also has pytest-cov
        AGENTS.md           the loop, for the model

Add dependencies with `uv add` in the clone and commit the updated `uv.lock`. `uv sync` runs with
`--no-dev` for production and the `dev` group for tests. `requires-python` is fixed at 3.14.

## Process contract

The CLI entrypoint is executed as a fresh subprocess, once per call:

    <venv>/bin/python main.py

* stdin  : one JSON object with the call arguments. Never argv. The environment is
           filtered; only names declared under `env:` receive operator-set values.
* stdout : one JSON value; the whole output or the last non-empty line.
* stderr : diagnostics, shown on failure.
* exit 0 : success; any other code is a failure.

Return `{"error": "..."}` for a handled failure the model should read. Raise only for bugs.

## Environment

Declare every variable your code reads in `SKILL.md`:

    env: [WEATHER_API_KEY, SMTP_PASSWORD]

The operator sets the values in the console (`/ui/`). You cannot read or write them and
must never commit them. Declarations are per capability and shared across tags. Only
declared names are injected, and only into the active tag; a missing value makes
`run_capability`/`serve_capability` fail with the names. Tests and the web boot probe
receive the placeholder `capforge-test-placeholder` for each declared name, so never
require a real secret at import time or in a test.

## Rules

1. Validate every argument. It came from a language model.
2. No network by default; declare a real need in SKILL.md.
3. Write runtime files only under `CAPFORGE_DATA_DIR` (per version). Add `data: shared` to
   SKILL.md for a stable `CAPFORGE_SHARED_DATA_DIR` across tags; it is unversioned and the
   capability owns concurrency, migrations and backup requirements. The code tree is read-only.
4. Credentials are operator-managed. Declare names under `env:`; never commit a
   value or invent one. The environment is filtered otherwise; index settings are
   visible only to uv.
5. Be fast. Calls time out (CAPFORGE_RUN_TIMEOUT, default 120s).
6. Be deterministic. Same args plus the same data directory should give the same result.
7. Leave nothing running. A resident service is `kind: web` served with `serve_capability`.
8. Filesystem access is confined by Landlock: own code/venv/system read, own data read/write,
   everything else denied. Network/CPU/memory are not confined.

## Web capabilities

`kind: web` turns the project into a long-lived HTTP service. Put `fastapi`, `uvicorn` and
`httpx` in `[project].dependencies`; `uvicorn` is required. `entrypoint: app.py` names the ASGI
module; `app:` selects the object (default `app`). After promotion, `serve_capability` boots the
active tag at `/cap/<name>/`; a promotion does not restart it until `restart`.

## Versioning

The pushed tree is tested before the draft swap. A green tree is snapshotted into an immutable tag,
the active pointer moves, and old tags beyond CAPFORGE_KEEP_TAGS (default 5) are pruned with their
per-version data and unused venvs. `rollback_capability` moves the pointer without touching the
draft or the repository. Promotion is automatic; operator recovery is `capforge promote`/`test`.

"""
