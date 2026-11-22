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
        SKILL.md            name, description and instructions; no deployment fields
        deploy.py           deployment descriptor: KIND, ENTRYPOINT, APP, DATA, SHARES and Env
        pyproject.toml      dependencies; requires-python = ">=3.14,<3.15"
        uv.lock             committed; `uv sync --frozen` is the only install path
        .python-version     3.14
        main.py | app.py    entrypoint (`ENTRYPOINT`), ASGI object (`APP` for web)
        tests/              pytest; the dev group also has pytest-cov
        AGENTS.md           the loop, for the model

Add dependencies with `uv add` in the clone and commit the updated `uv.lock`. `uv sync` runs with
`--no-dev` for production and the `dev` group for tests. `requires-python` is fixed at 3.14.

## deploy.py

`deploy.py` is the machine-readable deployment descriptor. Capforge parses it statically (it never
imports capability code), so keep the assignments literal:

    KIND = "cli"            # "cli" or "web"
    ENTRYPOINT = "main.py"  # app.py for web
    DATA = "version"        # "version" or "shared"
    APP = "app"             # web only; the ASGI object
    SHARES = {              # cross-capability volumes; operator approval required
        "datasets": "ro",   # read-only
        "cache": "rw",      # read-write
    }

`DATA = "shared"` is durable data owned by this capability. `SHARES` is different: it requests
access to operator-owned folders that other capabilities may also use. See "Shared folders".

Environment variables are fields of a pydantic model:

    from pydantic import BaseModel, Field

    class Env(BaseModel):
        API_KEY: str = Field(min_length=1, description="Weather API key")
        TIMEOUT_SECONDS: int = 30

Annotate fields with `str`, `int`, `float` or `bool`; other annotations are treated as `str`.
`SecretStr`, `HttpUrl`, `AnyUrl` and `EmailStr` are stored as strings. A field without a default
is required. `Field(...)` may declare `description`, `min_length`, `max_length`, `ge` and `le`.

## Process contract

The CLI entrypoint is executed as a fresh subprocess, once per call:

    <venv>/bin/python main.py

* stdin  : one JSON object with the call arguments. Never argv. The environment is
           filtered; only fields declared in `deploy.py` receive operator-set values.
* stdout : one JSON value; the whole output or the last non-empty line.
* stderr : diagnostics, shown on failure.
* exit 0 : success; any other code is a failure.

Return `{"error": "..."}` for a handled failure the model should read. Raise only for bugs.

## Testing

`tests/` runs with pytest before promotion. pytest-cov traces the test process; a test that runs
`main.py` as a subprocess cannot be measured, so a coverage report may show `main.py` at 0% even
though the test exercised it. That is expected. Modules imported by the tests (including
`deploy.py`) are measured normally.

## Environment

Declare every variable your code reads as a field of `Env` in `deploy.py`:

    class Env(BaseModel):
        WEATHER_API_KEY: str
        TIMEOUT_SECONDS: int = 30

The operator sets the values in the console (`/ui/`). You cannot read or write them and
must never commit them. Declarations are per capability and shared across tags. Only
declared names are injected, and only into the active tag; a required value that is missing
makes `run_capability`/`serve_capability` fail with the names. Fields with a default are
optional. Tests and the web boot probe receive the declared default, or a type-shaped
placeholder (`capforge-test-placeholder`, `0`, `0.0`, `false`), so never require a real secret
at import time or in a test.

## Shared folders

`SHARES` in `deploy.py` requests named cross-capability folders:

    SHARES = {"datasets": "ro", "cache": "rw"}

* `ro` is read-only, `rw` allows create, update and delete. The hub enforces this with Landlock.
* A declaration is a request, not a permission. The operator approves, changes or revokes grants
  in the console at `/ui/`; you cannot approve your own access.
* If a declared folder is not approved, or is approved with access that differs from the request,
  `run_capability`/`serve_capability` fail closed with a pending-approval error. Ask the operator.
* Approved folders are exposed as one JSON environment variable:

      CAPFORGE_SHARES={"datasets":{"path":"/.../shares/datasets","access":"ro"}}

  Parse it, read `path` and `access`, and honour the access mode. Paths are stable across tags.
* Tests and the web boot probe get fresh empty temporary directories, never production shared
  data. Do not require pre-existing content at import time.
* Shared folders are unversioned and not synchronized by the hub. Use `flock`, SQLite or another
  explicit lock when concurrent CLI calls or a served web child can race. Backups are the
  operator's responsibility.
* `DATA = "shared"` is separate: it is this capability's own `CAPFORGE_SHARED_DATA_DIR`. Other
  capabilities cannot see it.

## Rules

1. Validate every argument. It came from a language model.
2. No network by default; declare a real need in SKILL.md.
3. Write runtime files only under `CAPFORGE_DATA_DIR` (per version), `CAPFORGE_SHARED_DATA_DIR`
   when `DATA = "shared"`, or an approved `CAPFORGE_SHARES` folder. The code tree is read-only.
   `DATA = "shared"` and `SHARES` are unversioned; the capability owns concurrency, migrations
   and backup.
4. Credentials are operator-managed. Declare fields in `Env`; never commit a value or invent
   one. The environment is filtered otherwise; index settings are visible only to uv.
5. Be fast. Calls time out (CAPFORGE_RUN_TIMEOUT, default 120s).
6. Be deterministic. Same args plus the same data directory should give the same result.
7. Leave nothing running. A resident service is `KIND = "web"` served with `serve_capability`.
8. Filesystem access is confined by Landlock: own code/venv/system read, own data read/write,
   approved share folders only, everything else denied. Network/CPU/memory are not confined.

## Web capabilities

`KIND = "web"` in `deploy.py` turns the project into a long-lived HTTP service. Put `fastapi`,
`uvicorn` and `httpx` in `[project].dependencies`; `uvicorn` is required. `ENTRYPOINT = "app.py"`
names the ASGI module; `APP` selects the object (default `app`). After promotion,
`serve_capability` boots the active tag at `/cap/<name>/`; a promotion does not restart it until
`restart`.

The server boots the app with uvicorn `--root-path /cap/<name>` and exports
`CAPFORGE_ROOT_PATH`. Do not pass `root_path=` to `FastAPI(...)`; it is deprecated. Use
`CAPFORGE_ROOT_PATH` if the code needs the prefix to build absolute links.

## Versioning

The pushed tree is tested before the draft swap. A green tree is snapshotted into an immutable tag,
the active pointer moves, and old tags beyond CAPFORGE_KEEP_TAGS (default 5) are pruned with their
per-version data and unused venvs. `rollback_capability` moves the pointer without touching the
draft or the repository. Promotion is automatic; operator recovery is `capforge promote`/`test`.

"""

AGENTS_TEMPLATE = """\
# __CAPABILITY__

This repository is the source for one capforge capability. The server owns the capability
directory; you work in a clone, commit, and push. There is no shared terminal mount.

## The loop

1. `get_contract()` once.
2. `list_capabilities()` for names, active tags and repository URLs.
3. `scaffold_capability(name, description)`; pass `kind="web"` for an HTTP service.
4. `staging(name, action="token")` returns a short-lived token, `git_url`, `basic_username` and
   `auth: "basic"`.
5. Clone the repository and work in that clone. Use Basic auth with username `basic_username` and
   the token as the password. `git --no-pager` or `GIT_PAGER=cat` avoids the pager.
6. Edit the uv project; add dependencies with `uv add`, then `uv lock`; commit.
7. `git push origin HEAD:main`.
8. Poll `staging(name, action="latest")`; read `staging(name, action="logs")` on failure.
9. On `promoted`, call `run_capability(name, args)` or `serve_capability(name, action="start")`.

Promotion is automatic on a green pipeline. `install_packages`, `test_capability` and
`promote_capability` are not MCP tools. `capforge promote`/`test` are operator recovery commands.

## Layout

    SKILL.md            name, description and instructions; no deployment fields
    deploy.py           deployment descriptor: KIND, ENTRYPOINT, APP, DATA, SHARES and Env
    pyproject.toml      requires-python = ">=3.14,<3.15"; dependencies
    uv.lock             committed; uv sync --frozen is the only install path
    .python-version     3.14
    main.py | app.py    CLI entrypoint / ASGI app
    tests/              pytest; dev group also has pytest-cov
    AGENTS.md           this file

## deploy.py

`deploy.py` is the machine-readable contract. Capforge parses it statically, so keep the
assignments literal:

    KIND = "cli"            # "cli" or "web"
    ENTRYPOINT = "main.py"  # app.py for web
    DATA = "version"        # "version" or "shared"
    APP = "app"             # web only; the ASGI object
    SHARES = {              # cross-capability volumes; operator approval required
        "datasets": "ro",   # read-only
        "cache": "rw",      # read-write
    }

`DATA = "shared"` is durable data owned by this capability. `SHARES` is different: it requests
access to operator-owned folders that other capabilities may also use. See "Shared folders".

Environment variables are fields of a pydantic model:

    from pydantic import BaseModel, Field

    class Env(BaseModel):
        API_KEY: str = Field(min_length=1, description="Weather API key")
        TIMEOUT_SECONDS: int = 30

Capforge reads field names, types and defaults; it never imports deploy.py. Annotate with
`str`, `int`, `float` or `bool`; other annotations are treated as `str`. A field without a default
is required. The operator sets values in the console (`/ui/`); you cannot read or write them and
must never commit them.

## Process contract (CLI)

- stdin: one JSON object with the call arguments. Never argv.
- stdout: one JSON value; the whole output or its last non-empty line.
- stderr: diagnostics, shown to the caller on failure.
- exit 0: success; any other exit is a failure.
- Return `{"error": "..."}` for a handled failure the model should read; raise only for bugs.

## Web

`KIND = "web"` requires `uvicorn` in `[project].dependencies` and an ASGI object
(`ENTRYPOINT = "app.py"`, `APP = "app"`). After promotion,
`serve_capability(name, action="start")` exposes it at `/cap/<name>/`. A later promotion does not
restart it; use `restart`. Validate every request: anyone who can reach the proxy is a caller.

The server boots uvicorn with `--root-path /cap/<name>` and exports `CAPFORGE_ROOT_PATH`. Do not
pass `root_path=` to `FastAPI(...)`; it is deprecated. Read the env value if code needs the
prefix for absolute links.

## Data

Write runtime files only under `CAPFORGE_DATA_DIR` (per version). Set `DATA = "shared"` in
`deploy.py` for a stable `CAPFORGE_SHARED_DATA_DIR` across tags. It is unversioned; you own
concurrency, schema migrations and backup expectations. Never write beside `main.py`/`app.py`;
the tag is an immutable snapshot and the draft is derived.

## Shared folders

`SHARES = {"datasets": "ro", "cache": "rw"}` requests operator-owned folders shared with other
capabilities. `ro` is read-only; `rw` permits writes and deletes. The operator approves or revokes
each grant in the console (`/ui/`); you cannot approve your own access. An unapproved or mismatched
grant fails the run/serve closed. Approved folders arrive as JSON in `CAPFORGE_SHARES`:

    {"datasets": {"path": "/.../shares/datasets", "access": "ro"}}

Use the `path` and honour `access`. Tests and the boot probe receive empty temporary directories,
not production data. Shared folders are unversioned and unsynchronized: use `flock`/SQLite when
concurrent calls can race, and let the operator handle backups.

`DATA = "shared"` is separate: it is this capability's own `CAPFORGE_SHARED_DATA_DIR`.

## Testing

`tests/` runs with pytest before promotion. pytest-cov traces the test process, so a test that
runs `main.py` as a subprocess cannot be measured: `main.py` may show 0% coverage even though it
ran. That is expected; modules the tests import (including `deploy.py`) are measured normally.

Tests and the web boot probe receive non-secret placeholders: the declared default, or
`capforge-test-placeholder`, `0`, `0.0` or `false` by type. Never require a real secret at import
time.

## Versioning

A green push is snapshotted into an immutable tag and made active. Older tags are pruned beyond
the keep limit, with their per-version data and unused venvs. `rollback_capability` moves the
pointer without touching the draft or repository. The push pipeline is the only promotion path.

## Rules

- Validate every argument; it came from a language model.
- No network by default; declare a real need in `SKILL.md`.
- Be fast: calls time out (default 120s).
- Be deterministic for `DATA = "version"`: same args plus the same data, same result.
- Leave nothing running; a resident service is `KIND = "web"` served with `serve_capability`.
- Landlock confines filesystem access to your code/venv, system libraries, your data dirs and
  approved share folders; network/CPU/memory are not confined.
"""
