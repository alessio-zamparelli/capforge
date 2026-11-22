"""MCP surface: ten stable tools; capabilities live behind a git repository."""

from __future__ import annotations

import hmac
import logging
import shutil
from pathlib import Path
from typing import Any

from fastmcp import Context, FastMCP
from fastmcp.server.auth import AccessToken, TokenVerifier

from . import __version__, core, scaffold, versioning
from . import staging as staging_service
from .contract import CONTRACT
from .core import (
    WEB_KINDS,
    CapError,
    cap_path,
    caps_dir,
    data_dir,
    env_status,
    execute,
    git_timeout,
    install_timeout,
    keep_tags,
    list_files,
    load_capability,
    load_capability_at,
    log_max_bytes,
    log_tail_bytes,
    run_timeout,
    sandbox_mode,
    serve_boot_timeout,
    state_dir,
    test_timeout,
    tree_hash,
    try_file_lock,
)
from .core import list_capabilities as discover

log = logging.getLogger("capforge")

RUNTIME_TRANSPORT = "http"
TOOL_NAMES = [
    "get_contract",
    "list_capabilities",
    "get_capability",
    "scaffold_capability",
    "run_capability",
    "serve_capability",
    "staging",
    "delete_capability",
    "rollback_capability",
    "list_tags",
]


class _McpTokenVerifier(TokenVerifier):
    """Constant-time bearer check for the HTTP MCP listener.

    The token is read once at import; the CLI refuses to start HTTP without
    one. It is never part of a capability environment.
    """

    def __init__(self, token: str) -> None:
        super().__init__()
        self._token = token

    async def verify_token(self, token: str) -> AccessToken | None:
        if not token or not hmac.compare_digest(token.encode(), self._token.encode()):
            return None
        return AccessToken(token=token, client_id="capforge", scopes=[])


_mcp_token = core.mcp_token()
mcp = FastMCP(
    "capforge",
    instructions=(
        "Git-authored capabilities. Scaffold a uv project, clone its repository, edit, "
        "push, and poll staging(latest). The pipeline tests and promotes on green."
    ),
    auth=_McpTokenVerifier(_mcp_token) if _mcp_token else None,
)


def _fail(message: str) -> dict[str, Any]:
    return {"ok": False, "error": message}


def _cap_or_error(name: str):
    try:
        return load_capability(name), None
    except CapError as exc:
        return None, _fail(str(exc))


def _git_url(name: str) -> str:
    from . import web

    return f"{web.public_base()}/git/{name}.git"


def _env_caps(cap) -> list:
    """Draft and active declarations, so status survives a draft-only edit."""
    caps = [cap]
    state = versioning.read_state(cap.name)
    tag = (state.get("active") or {}).get("tag")
    if tag:
        source = versioning.version_path(cap.name, tag)
        if source.is_dir():
            active = load_capability_at(cap.name, source)
            if not active.error:
                caps.append(active)
    return caps


def _actor(ctx: Context | None) -> dict[str, Any]:
    if ctx is None:
        return {}
    try:
        request_context = getattr(ctx, "request_context", None)
        request = getattr(request_context, "request", None)
        if request is None:
            return {}
        headers = request.headers
    except Exception:
        return {}
    mapping = {
        "id": "x-openwebui-user-id",
        "email": "x-openwebui-user-email",
        "name": "x-openwebui-user-name",
        "role": "x-openwebui-user-role",
        "chat_id": "x-openwebui-chat-id",
        "message_id": "x-openwebui-message-id",
    }
    return {
        key: str(value)[:300]
        for key, header in mapping.items()
        if (value := headers.get(header))
    }


@mcp.tool
def get_contract() -> dict:
    """Read this first: the git clone/push loop, the uv project contract, limits and paths."""
    from . import web

    return {
        "ok": True,
        "version": __version__,
        "caps_dir": str(caps_dir()),
        "state_dir": str(state_dir()),
        "caps_owner": "server; not shared with the terminal",
        "tools": TOOL_NAMES,
        "keep_tags": keep_tags(),
        "limits": {
            "run_timeout_s": run_timeout(),
            "test_timeout_s": test_timeout(),
            "install_timeout_s": install_timeout(),
            "git_timeout_s": git_timeout(),
            "serve_boot_timeout_s": serve_boot_timeout(),
            "log_max_bytes": log_max_bytes(),
            "log_tail_bytes": log_tail_bytes(),
            "sandbox": sandbox_mode(),
        },
        "web": {
            "host": web.web_host(),
            "port": web.web_port(),
            "ui": f"{web.public_base()}/ui/",
            "api": f"{web.public_base()}/api/",
            "git": f"{web.public_base()}/git/<name>.git",
            "cap": f"{web.public_base()}/cap/<name>/",
        },
        "mcp": {
            "transport": RUNTIME_TRANSPORT,
            "auth": "bearer" if core.mcp_token() else "stdio-or-unconfigured",
        },
        "loop": (
            "scaffold_capability -> staging(action='token') -> git clone -> edit/uv add/uv lock "
            "-> commit -> git push -> staging(action='latest'); promotion is automatic on green."
        ),
        "contract": CONTRACT,
    }


@mcp.tool
def list_capabilities() -> dict:
    """List capabilities, their active tag, draft state and repository URL."""
    out = []
    for cap in discover():
        state = versioning.read_state(cap.name)
        active = state.get("active") or {}
        caps_for = _env_caps(cap)
        out.append(
            {
                "name": cap.name,
                "description": cap.description,
                "kind": cap.kind,
                "data": cap.data,
                "active": active.get("tag"),
                "draft_ready": cap.draft_ready,
                "draft_error": cap.error,
                "draft_changed": bool(
                    active.get("content_hash")
                    and cap.draft_ready
                    and active["content_hash"] != tree_hash(cap.path)
                ),
                "tags": len(state.get("tags") or []),
                "git_url": _git_url(cap.name),
                "env": env_status(cap.name, caps_for),
                "shares": core.share_status(cap.name, caps_for),
            }
        )
    return {"ok": True, "caps_dir": str(caps_dir()), "count": len(out), "capabilities": out}


@mcp.tool
def get_capability(name: str) -> dict:
    """Full detail for one capability: the materialised draft, active tag, repo and data mode."""
    cap, err = _cap_or_error(name)
    if err:
        return err
    state = versioning.read_state(cap.name)
    caps_for = _env_caps(cap)
    return {
        "ok": True,
        "name": cap.name,
        "path": str(cap.path),
        "git_url": _git_url(cap.name),
        "description": cap.description,
        "kind": cap.kind,
        "data": cap.data,
        "entrypoint": cap.entrypoint,
        "env": env_status(cap.name, caps_for),
        "shares": core.share_status(cap.name, caps_for),
        "draft_ready": cap.draft_ready,
        "draft_error": cap.error,
        "draft_hash": tree_hash(cap.path) if cap.path.is_dir() else None,
        "files": list_files(cap.path),
        "skill_md": (cap.path / "SKILL.md").read_text(encoding="utf-8", errors="replace")
        if (cap.path / "SKILL.md").is_file()
        else None,
        "active": state.get("active"),
        "tags": state.get("tags") or [],
    }


@mcp.tool
def scaffold_capability(name: str, description: str, kind: str = "cli") -> dict:
    """Create a uv base project, publish `main` in a new bare repo, and materialise the draft.
    The model then mints a token with staging(action='token') and clones the repository."""
    kind = str(kind).strip().lower() or "cli"
    if kind not in WEB_KINDS:
        return _fail(f"kind must be one of {list(WEB_KINDS)}, got {kind!r}")
    try:
        result = scaffold.create(name, description, kind)
    except CapError as exc:
        return _fail(str(exc))
    except OSError as exc:
        return _fail(f"Could not scaffold {name!r}: {exc}")
    result["ok"] = True
    result["next"] = (
        "Call staging(name, action='token'), clone git_url with Basic auth, edit, "
        "uv add/uv lock, commit, push HEAD:main, then poll staging(name, action='latest'). "
        "The scaffold includes tests/test_scaffold.py; keep or replace it."
    )
    return result


def test_capability(name: str, args: dict | None = None) -> dict:
    """Internal/operator helper (not an MCP tool): test the server draft."""
    cap, err = _cap_or_error(name)
    if err:
        return err
    if not cap.draft_ready:
        return _fail(f"Draft is not runnable: {cap.error}")
    from .core import run_tests

    try:
        tests = run_tests(cap, cap.path)
    except CapError as exc:
        return _fail(f"Tests could not run: {exc}")
    payload: dict[str, Any] = {"ok": tests.ok, "tests": tests.as_dict()}
    if args is not None:
        run = execute(cap, cap.path, args, tag=None)
        payload["run"] = run.as_dict()
        payload["ok"] = bool(payload["ok"] and run.ok)
    return payload


def promote_capability(
    name: str,
    tag: str | None = None,
    note: str = "",
    skip_tests: bool = False,
    force: bool = False,
) -> dict:
    """Internal/operator helper (not an MCP tool): test the server draft and promote."""
    cap, err = _cap_or_error(name)
    if err:
        return err
    if not cap.draft_ready:
        return _fail(f"Draft is not runnable: {cap.error}")
    if not skip_tests:
        from .core import run_tests

        try:
            tests = run_tests(cap, cap.path)
        except CapError as exc:
            return _fail(f"Tests could not run: {exc}")
        if not tests.ok:
            return {
                "ok": False,
                "error": "Tests failed; promotion refused.",
                "exit_code": tests.exit_code,
                "stdout": tests.stdout[-3000:],
                "stderr": tests.stderr[-3000:],
            }
    try:
        result = versioning.promote(cap.name, tag, note=note, force=force)
    except CapError as exc:
        return _fail(str(exc))
    result["ok"] = True
    result["tests_skipped"] = skip_tests
    return result


@mcp.tool
def run_capability(name: str, args: dict | None = None) -> dict:
    """Execute the active tag (or the scaffold draft before the first promotion)."""
    try:
        path = cap_path(name)
    except CapError as exc:
        return _fail(str(exc))
    if not path.is_dir():
        return _fail(f"No capability named {name!r} in {caps_dir()}; scaffold it first.")

    state = versioning.read_state(name)
    active = state.get("active") or {}
    tag = active.get("tag")
    if tag:
        source = versioning.version_path(name, tag)
        if not source.is_dir():
            return _fail(
                f"Active tag {tag!r} is recorded but its files are gone. Roll back or push again."
            )
        source_cap = load_capability_at(name, source)
        if source_cap.error:
            return _fail(f"Active tag {tag!r} is not runnable: {source_cap.error}")
        mode = "active"
    else:
        source_cap = load_capability(name)
        if not source_cap.draft_ready:
            return _fail(
                f"{name!r} has never been promoted and the draft is not runnable "
                f"({source_cap.error}). Push a green commit first."
            )
        source, tag, mode = source_cap.path, None, "draft"

    try:
        result = execute(source_cap, source, args or {}, tag=tag)
    except CapError as exc:
        return _fail(str(exc))

    out = result.as_dict()
    out.update(
        {
            "name": name,
            "version": tag or "draft",
            "mode": mode,
            "entrypoint": source_cap.entrypoint,
            "env": "uv",
            "content_hash": str((active.get("content_hash") if tag else tree_hash(source)) or "")[:12],
            "data_dir": str(data_dir(name, tag)),
        }
    )
    if mode == "draft":
        out["warning"] = "Running the unpromoted scaffold draft; push a green commit to promote."
        if source_cap.env:
            out["env_warning"] = (
                "Draft runs use placeholder values for declared env; promote, then set real "
                "values in the console."
            )
    return out


@mcp.tool
def serve_capability(name: str, action: str = "status", tail: int | None = None) -> dict:
    """Start, stop, restart, inspect or read logs for a web capability's ACTIVE tag."""
    if RUNTIME_TRANSPORT == "stdio":
        return _fail(
            "web serving requires HTTP transport: run `capforge serve` without "
            "--transport stdio (the web listener is skipped in stdio mode)."
        )
    action = str(action).strip().lower() or "status"
    if action not in ("start", "stop", "restart", "status", "logs"):
        return _fail(f"action must be one of start, stop, restart, status, logs; got {action!r}")
    try:
        from . import web

        result = web.logs(name, tail) if action == "logs" else getattr(web, action)(name)
    except CapError as exc:
        return _fail(str(exc))
    result["ok"] = True
    result["action"] = action
    if action == "start" and result.get("status") == "running":
        result.setdefault(
            "hint",
            "Restart after the next promotion to move the instance to the new tag.",
        )
    return result


@mcp.tool
def staging(
    name: str,
    action: str = "latest",
    kind: str = "pipeline",
    tail: int | None = None,
    ctx: Context | None = None,
) -> dict:
    """Git credentials and push feedback. action='token' mints one short-lived token for
    clone and push; action='latest' returns the pipeline state, tag and log tail;
    action='logs' returns the bounded pipeline or scaffold log."""
    action = str(action).strip().lower() or "latest"
    try:
        cap_path(name)
        if action == "token":
            minted = staging_service.mint(name, _actor(ctx))
            minted["ok"] = True
            minted["git_url"] = _git_url(name)
            user = minted.get("basic_username") or "git"
            minted["hint"] = (
                f"Use Basic auth only: username {user!r}, password the token. "
                "git -c credential.helper='!f() { echo username=$BASIC_USERNAME; "
                "echo password=$TOKEN; }; f' clone \"$GIT_URL\""
            )
            return minted
        if action == "latest":
            data = staging_service.latest(name)
            data.update({"ok": True, "name": name, "git_url": _git_url(name)})
            return data
        if action == "logs":
            data = staging_service.read_log(name, kind, tail)
            data.update({"ok": True, "name": name, "kind": kind})
            return data
        return _fail(f"action must be one of token, latest, logs; got {action!r}")
    except CapError as exc:
        return _fail(str(exc))


def _delete_targets(name: str) -> list[Path]:
    root = core.cap_path(name)
    state = state_dir()
    return [
        root,
        state / "versions" / name,
        state / "venvs" / name,
        state / "data" / name,
        state / "logs" / name,
        state / "git" / f"{name}.git",
        state / "served" / name,
        state / "state" / f"{name}.json",
        state / "staging" / f"{name}.json",
    ]


@mcp.tool
def delete_capability(name: str, confirm: str) -> dict:
    """Delete a capability and all of its server-side state. `confirm` must equal `name`.
    Stops a served instance first, refuses while a push or pipeline is running, and is
    idempotent when the capability is already gone."""
    from . import staging as staging_service

    try:
        validated = cap_path(name).name
    except CapError as exc:
        return _fail(str(exc))
    if confirm != validated:
        return _fail("confirm must equal the capability name exactly")
    targets = _delete_targets(validated)
    exists = (
        any(path.exists() for path in targets)
        or bool(staging_service.read_state(validated))
        or bool(core.approved_shares(validated))
    )
    if not exists:
        return {
            "ok": True,
            "deleted": False,
            "name": validated,
            "reason": "not found",
            "shares_revoked": 0,
        }

    current = staging_service.read_state(validated)
    if current.get("state") and current.get("state") not in {
        "promoted", "unchanged", "failed", "interrupted",
    }:
        return _fail("a push pipeline is in progress; wait for it to finish")

    push = state_dir() / "locks" / f"{validated}-git-push.lock"
    pipeline = state_dir() / "locks" / f"{validated}-pipeline.lock"
    stopped = False
    with try_file_lock(push) as push_ok:
        if not push_ok:
            return _fail("a push is in progress; try again after it finishes")
        with try_file_lock(pipeline) as pipeline_ok:
            if not pipeline_ok:
                return _fail("a pipeline is in progress; try again after it finishes")
            from . import web

            web.forget(validated)
            stopped = True
            with core.cap_lock(validated):
                revoked = core.revoke_all_grants(validated)
                staging_service.forget(validated)
                staging_service.cleanup_backups(validated)
                for target in targets:
                    if target.is_dir():
                        shutil.rmtree(target, ignore_errors=True)
                    else:
                        target.unlink(missing_ok=True)
                staging_root = core.cap_path(validated).parent / ".staging"
                for leftover in staging_root.glob(f"{validated}-*"):
                    shutil.rmtree(leftover, ignore_errors=True)
                for lock in (state_dir() / "locks").glob(f"{validated}*.lock"):
                    try:
                        lock.unlink(missing_ok=True)
                    except OSError:
                        pass
    log.info(
        "delete cap=%s stopped=%s deleted=true shares_revoked=%d",
        validated,
        stopped,
        revoked,
    )
    return {
        "ok": True,
        "deleted": True,
        "name": validated,
        "stopped": stopped,
        "shares_revoked": revoked,
    }


@mcp.tool
def rollback_capability(name: str, tag: str) -> dict:
    """Make an older tag active again. Does not touch the draft or the repository."""
    try:
        result = versioning.rollback(cap_path(name).name, tag)
    except CapError as exc:
        return _fail(str(exc))
    result["ok"] = True
    return result


@mcp.tool
def list_tags(name: str, diff_against_active: bool = False) -> dict:
    """Tags for a capability, newest last, with the active one marked."""
    cap, err = _cap_or_error(name)
    if err:
        return err
    payload = versioning.list_tags(cap.name)
    payload["ok"] = True
    if diff_against_active:
        try:
            payload["diff"] = versioning.diff_against_active(cap.name)
        except CapError as exc:
            payload["diff"] = f"(unavailable: {exc})"
    return payload


if __name__ == "__main__":
    mcp.run()
