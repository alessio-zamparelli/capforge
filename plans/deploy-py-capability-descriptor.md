# deploy.py: capability deployment descriptor

Status: implemented in 0.5.0. This is a deliberate breaking change to the capability contract.

## Goal

Move the machine-readable deployment metadata out of `SKILL.md` frontmatter and into a
`deploy.py` module in every scaffolded capability. `deploy.py` declares:

- the app kind (`"cli"` or `"web"`);
- the entrypoint module (and the ASGI object for web);
- the data mode (`"version"` or `"shared"`);
- the operator-set environment variables as a pydantic `Env` model (names, types, defaults and
  basic constraints).

The scaffolded `AGENTS.md` stops duplicating those rules and instead teaches the agent to read and
edit `deploy.py`. The scaffold ships a full `AGENTS.md` covering both CLI and web capabilities, so
the same file can instruct future agents.

## Decisions

1. **Hard cut.** `deploy.py` is required. `SKILL.md` keeps `name`, `description` and the prose body;
   `kind`, `entrypoint`, `app`, `data`, `env` and `packages` in frontmatter are refused with a
   migration message. Pre-0.5 tags have no `deploy.py` and cannot be loaded or rolled back to.
2. **Static AST parsing.** Capforge reads `deploy.py` with the standard-library `ast` module. It
   never imports or executes capability code in the hub. Pydantic is used by the capability at
   runtime and is declared in the scaffold dependencies.
3. **Typed env, phase 1.** The console validates values against the declared type and basic
   constraints, renders typed inputs and shows the default. Test/draft placeholders are
   type-aware. Values remain write-only.

## deploy.py

CLI:

```python
"""Deployment descriptor for <name>. Capforge reads this file; keep it declarative."""

from __future__ import annotations

from pydantic import BaseModel


class Env(BaseModel):
    """Operator-set values, injected only into the active tag."""

    # API_KEY: str = Field(min_length=1, description="What this key is for")
    # TIMEOUT_SECONDS: int = 30


KIND = "cli"            # "cli" | "web"
ENTRYPOINT = "main.py"
DATA = "version"        # "version" | "shared"
```

Web adds `KIND = "web"`, `ENTRYPOINT = "app.py"` and `APP = "app"`.

Rules:

- `KIND` and `ENTRYPOINT` are required. `APP` defaults to `"app"`; `DATA` defaults to `"version"`.
- Module assignments must be literals.
- `Env` is optional. Fields are `AnnAssign` nodes; `_*` and `model_config` are ignored.
- Annotations map to `str`, `int`, `float` or `bool`. Unknown annotations fall back to `str`.
  `SecretStr`, `HttpUrl`, `AnyUrl` and `EmailStr` are treated as `str`.
- `Annotated[T, ...]`, `Optional[T]` and `T | None` are unwrapped; only a default makes a field
  optional.
- Defaults are literals or `Field(default=..., description=..., min_length=..., max_length=...,
  ge=..., le=...)`. Unsupported `Field` arguments are ignored.
- Names pass `valid_env_name`, are unique, and respect `MAX_ENV_VARS`.
- `kind: web` still requires `uvicorn` in `[project].dependencies` and an identifier `APP`.

## Env semantics

- `cap_env_missing` reports only required fields (no default).
- `cap_env_values` returns stored values and declared defaults for optional fields.
- `cap_env_placeholders` returns the declared default when present, otherwise
  `capforge-test-placeholder` for strings, `0` for integers, `0.0` for floats and `false` for
  booleans.
- `set_cap_env` validates before storing: integer/float parsing, boolean spellings
  (`true/false/1/0/yes/no/on/off`), `min_length`/`max_length` for strings and `ge`/`le` for numbers.
  Undeclared names are still accepted as strings so the operator can clean them up.
- `env_status` adds `type`, `required`, `default` and `description` to the existing
  `name`/`declared`/`configured`/`updated_at`. Values are never returned.

## Scaffold

- Write `deploy.py` for both kinds.
- CLI dependencies become `["pydantic"]`; web becomes
  `["fastapi", "uvicorn", "httpx", "pydantic"]`.
- `SKILL.md` loses `entrypoint`, `kind`, `data`, `env` and the commented `# env:` line.
- `contract.py` gains `AGENTS_TEMPLATE` (strings only) with a `__CAPABILITY__` token. The scaffold
  renders it with a plain `replace`, replacing the thin inline `AGENTS` constant.
- The template covers both kinds: loop, layout, `deploy.py`, env/placeholders, CLI process
  contract, web serving, data, testing, versioning and sandbox rules.
- `AGENTS.md.example` is the same text; a unit test enforces that it never drifts.

## Console and API

- `ui/src/api.ts`: `EnvInfo` gains `type`, `required`, `default`, `description`.
- `ui/src/Dashboard.tsx`: number inputs for `int`/`float`, a boolean control, password input for
  strings, a description line and a default hint; `missing` is shown only for required unset
  values.
- `web.py` keeps the existing routes; the response shape is additive.

## Migration

Hard cut makes the existing active tags unloadable, so migrate immediately after the restart:

1. Mint a token with `staging(name, action="token")`.
2. Clone `git_url` over HTTP with Basic auth.
3. Add `deploy.py` derived from the old `SKILL.md` (`KIND`/`ENTRYPOINT`/`APP`/`DATA`, and `Env`
   fields as `str` for the old `env:` names).
4. Strip the moved keys from `SKILL.md` and replace `AGENTS.md` with the new template.
5. Commit, push, wait for `staging(latest) == promoted`.

A local push to the bare repository does not run the pipeline; the push must use the HTTP route.
Record in `docs/NEXT.md` that pre-0.5 tags are not loadable.

## Files

- `capforge/core.py`: `EnvSpec`, `DeploySpec`, `parse_deploy_py`, `_load_from`, env helpers,
  `set_cap_env`, `env_status`.
- `capforge/contract.py`: contract text and `AGENTS_TEMPLATE`.
- `capforge/scaffold.py`: `deploy.py` templates, dependencies, `SKILL.md`, `AGENTS.md`.
- `capforge/server.py`, `capforge/web.py`: additive env status only.
- `ui/src/api.ts`, `ui/src/Dashboard.tsx`: typed env inputs.
- `tests/test_units.py`, `tests/test_lifecycle.py`, `tests/test_web.py`, `tools/mcp_probe.py`.
- `README.md`, `AGENTS.md`, `AGENTS.md.example`, `docs/ARCHITECTURE.md`, `docs/NEXT.md`.
- `pyproject.toml`, `capforge/__init__.py`, `uv.lock`: version 0.5.0.

## Verification

```sh
uv run --locked pytest -q --cov=capforge --cov-branch --cov-fail-under=69
uv run --locked ruff check .
uv run --locked mypy capforge
uv run --locked capforge doctor
uv run --locked capforge tools          # still exactly ten
tools/mcp_probe.py
cd ui && bun install --frozen-lockfile && bun run build
```
