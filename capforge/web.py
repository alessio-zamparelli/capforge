"""Serving engine and web listener for capabilities whose `KIND` includes `"web"`.

One instance per capability, pinned to its active tag, spawned as a uvicorn
child bound to 127.0.0.1 and reverse-proxied at `http://<host>:<web port>/cap/<name>/`.
A persisted registry records the desired state so a restarted server can
restore instances.

One web listener hosts `/ui`, `/api`, `/git` and `/cap`. The capability proxy
and the operator console therefore share an origin; deployments that serve
untrusted web capabilities should put `/cap` on a distinct origin at the
reverse proxy (see docs/NEXT.md).

Non-stdlib imports (starlette, httpx) are confined to this module: core,
versioning and contract stay stdlib-only, and versioning imports back into
this module only lazily, inside prune/venv-GC.
"""

from __future__ import annotations

import base64
import hmac
import json
import logging
import os
import re
import secrets
import shutil
import subprocess
import tempfile
import threading
import time
from collections import deque
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
from starlette.applications import Starlette
from starlette.background import BackgroundTask
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, Response, StreamingResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from . import __version__, core, versioning
from .core import CapError

log = logging.getLogger("capforge")

DEFAULT_WEB_PORT = 8080
MAX_LOG_BYTES = 5 * 1024 * 1024
MAX_LOG_TAIL = 2000
MAX_OPENAPI_BYTES = 256 * 1024
MAX_PROXY_BODY = 32 * 1024 * 1024
SESSION_TTL = 7 * 24 * 3600
MAX_FAILED_LOGINS = 5
LOGIN_BACKOFF = 600

ALL_METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"]
HOP_BY_HOP = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
}
# Headers that belong to the console/API and must never reach capability code.
PROXY_DROP_REQUEST = HOP_BY_HOP | {"cookie", "authorization", "x-csrf"}
# Cookies and cache controls set by a capability must not escape the proxy origin.
PROXY_DROP_RESPONSE = HOP_BY_HOP | {"content-length", "set-cookie", "clear-site-data"}

SESSION_COOKIE = "capforge_session"
CSRF_HEADER = "x-csrf"

_INSTANCES: dict[str, _Instance] = {}
_engine_lock = threading.Lock()
_sessions: dict[str, float] = {}
_failed_logins: dict[str, deque[float]] = {}
_session_lock = threading.Lock()


# --------------------------------------------------------------------------- config


def web_host() -> str:
    """Bind address of the :8080 listener. Capability children stay on loopback."""
    return os.environ.get("CAPFORGE_WEB_HOST") or "0.0.0.0"


def web_port() -> int:
    """0 disables the whole listener: no proxy, console, clone/push or serving."""
    try:
        return int(os.environ.get("CAPFORGE_WEB_PORT", DEFAULT_WEB_PORT))
    except (TypeError, ValueError):
        return DEFAULT_WEB_PORT


def web_token() -> str | None:
    """Console token. Unset means the console and control API are disabled."""
    raw = os.environ.get("CAPFORGE_WEB_TOKEN", "").strip()
    return raw or None


def ui_dir() -> Path | None:
    raw = os.environ.get("CAPFORGE_UI_DIR")
    base = Path(raw) if raw else Path(__file__).resolve().parent.parent / "ui" / "dist"
    return base if (base / "index.html").is_file() else None


def ui_enabled() -> bool:
    raw = os.environ.get("CAPFORGE_WEB_UI", "1").strip().lower()
    if raw in {"0", "false", "no", "off"}:
        return False
    return ui_dir() is not None


def _display_host(host: str | None = None) -> str:
    resolved = host if host is not None else web_host()
    if resolved in ("0.0.0.0", "::", ""):
        return "127.0.0.1"
    return resolved


def public_base() -> str:
    """Externally reachable base; CAPFORGE_PUBLIC_URL overrides host:port."""
    base = os.environ.get("CAPFORGE_PUBLIC_URL", "").strip().rstrip("/")
    if base:
        return base
    return f"http://{_display_host()}:{web_port()}"


def public_url(name: str) -> str:
    """Caller-facing URL of a served capability under /cap/<name>/."""
    return f"{public_base()}{core.cap_root_path(name)}/"


def git_url(name: str) -> str:
    return f"{public_base()}/git/{name}.git"


# --------------------------------------------------------------------------- registry


def _registry_file() -> Path:
    base = core.state_dir()
    return core._contained(base, base / "served" / "registry.json")


def _quarantine_registry(path: Path) -> None:
    dest = path.with_name(f"registry.corrupt-{time.time_ns()}.json")
    try:
        path.replace(dest)
        log.warning("quarantined corrupt serve registry %s -> %s", path, dest)
    except OSError:
        log.exception("could not quarantine corrupt serve registry %s", path)


def read_registry() -> dict[str, dict[str, Any]]:
    """Registry contents, dropping entries that fail name/tag/desired checks.

    Reads are safe without a lock: writers replace the file atomically."""
    data = _read_registry_unlocked()
    clean: dict[str, dict[str, Any]] = {}
    for name, record in data.items():
        if (
            isinstance(name, str)
            and core.NAME_RE.match(name)
            and isinstance(record, dict)
            and core.TAG_RE.match(str(record.get("tag") or ""))
            and record.get("desired") in ("start", "stop")
        ):
            clean[name] = record
    return clean


def _read_registry_unlocked() -> dict[str, dict[str, Any]]:
    path = _registry_file()
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        _quarantine_registry(path)
        return {}
    if not isinstance(data, dict):
        _quarantine_registry(path)
        return {}
    return data


def _persist_record(name: str, record: dict[str, Any] | None) -> None:
    """Read-modify-write one registry entry. Callers hold cap_lock(name)."""
    path = _registry_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    with core._file_lock(path.parent / "registry.lock"):
        data = _read_registry_unlocked()
        if record is None:
            data.pop(name, None)
        else:
            data[name] = record
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
        tmp.replace(path)


def _registry_record(name: str) -> dict[str, Any] | None:
    return read_registry().get(name)


# --------------------------------------------------------------------------- engine


@dataclass
class _Instance:
    name: str
    tag: str
    web_entrypoint: str
    app_name: str
    packages: list[str]
    proc: subprocess.Popen[Any]
    port: int
    log_handle: Any
    started_at: int
    endpoints: list[dict[str, Any]] = field(default_factory=list)
    desired: str = "start"

    def record(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "tag": self.tag,
            "web_entrypoint": self.web_entrypoint,
            "app": self.app_name,
            "packages": list(self.packages),
            "endpoints": self.endpoints,
            "started_at": self.started_at,
            "desired": self.desired,
            "pid": self.proc.pid,
            "port": self.port,
        }


def _log_path(name: str, tag: str) -> Path:
    return core._contained(
        serve_dir := core.state_dir(), serve_dir / "served" / name / tag / "server.log"
    )


def _rotate_log(path: Path) -> None:
    """One generation kept; a chatty web cap must not grow the disk forever."""
    try:
        if path.is_file() and path.stat().st_size > MAX_LOG_BYTES:
            path.replace(path.with_name("server.log.1"))
    except OSError:
        log.warning("could not rotate %s", path, exc_info=True)


def _log_tail(path: Path, limit: int = MAX_LOG_TAIL) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")[-limit:]
    except OSError:
        return ""


def _truncate_log_if_huge(path: Path) -> None:
    """Bound a running instance's log from status(): keep only the recent tail.

    The child holds the fd in append mode, so truncating is safe; rotation at
    next boot keeps a full generation."""
    try:
        if path.is_file() and path.stat().st_size > MAX_LOG_BYTES:
            with path.open("rb") as handle:
                handle.seek(-MAX_LOG_TAIL, 2)
                tail = handle.read()
            path.write_bytes(tail)
    except OSError:
        log.warning("could not bound log size at %s", path, exc_info=True)


def _fetch_endpoints(port: int) -> list[dict[str, Any]]:
    """OpenAPI cache for the console. Non-FastAPI apps simply have none."""
    try:
        resp = httpx.get(f"http://127.0.0.1:{port}/openapi.json", timeout=5)
        if resp.status_code != 200 or len(resp.content) > MAX_OPENAPI_BYTES:
            return []
        spec = resp.json()
        paths = spec.get("paths")
        if not isinstance(paths, dict):
            return []
        out: list[dict[str, Any]] = []
        for path, ops in sorted(paths.items()):
            if not isinstance(ops, dict):
                continue
            for method, op in sorted(ops.items()):
                if method.lower() not in ("get", "post", "put", "patch", "delete"):
                    continue
                summary = ""
                if isinstance(op, dict):
                    summary = str(op.get("summary") or op.get("operationId") or "")
                out.append({"method": method.upper(), "path": str(path), "summary": summary})
        return out
    except (httpx.HTTPError, ValueError):
        return []


def _active_or_raise(name: str) -> tuple[str, dict[str, Any], Any]:
    """Resolve the active tag exactly like run_capability, but refuse drafts."""
    state = versioning.read_state(name)
    active = state.get("active") or {}
    tag = active.get("tag")
    if not tag:
        raise CapError(
            f"{name!r} has no active version to serve. Promote it first with "
            "promote_capability; drafts are never served."
        )
    source = versioning.version_path(name, tag)
    if not source.is_dir():
        raise CapError(
            f"Active tag {tag!r} is recorded but its files are gone. Roll back or promote again."
        )
    cap = core.load_capability_at(name, source)
    if cap.error:
        raise CapError(f"Active tag {tag!r} is not runnable: {cap.error}")
    if not cap.serves_web or cap.web_entrypoint is None:
        raise CapError(
            f"{name!r} does not serve web (kind: {cap.kind}). "
            'Only capabilities whose KIND includes "web" can be served.'
        )
    return tag, {"web_entrypoint": cap.web_entrypoint, "app": cap.app_name}, source


def _spawn(
    name: str,
    tag: str,
    web_entrypoint: str,
    app_name: str,
    *,
    public_url: str | None = None,
) -> _Instance:
    """Boot one uvicorn child and wait for it to answer HTTP. Raises CapError
    with the child's log tail on failure. Caller holds cap_lock(name)."""
    source = versioning.version_path(name, tag)
    if not source.is_dir():
        raise CapError(f"Version {tag!r} for {name!r} is missing from {source}")
    cap = core.load_capability_at(name, source)
    if cap.error:
        raise CapError(f"Version {tag!r} is not runnable: {cap.error}")
    if not cap.serves_web:
        raise CapError(f"Version {tag!r} does not serve web (kind: {cap.kind})")
    venv = core.ensure_venv(name, source, "prod")
    port = core._free_port()
    log_file = _log_path(name, tag)
    log_file.parent.mkdir(parents=True, exist_ok=True)
    _rotate_log(log_file)
    mounts = core.resolve_shares(name, cap)
    env = core.web_child_env(
        cap,
        tag=tag,
        venv=venv,
        port=port,
        public_url=public_url or public_url_of(name),
        share_mounts=mounts,
    )
    env["CAPFORGE_SERVE"] = "1"
    data = core.data_dir(name, tag)
    shared = core.shared_data_dir(name) if cap.data == "shared" else None
    cmd = core._sandbox_command(
        [str(venv / "bin" / "python"), *core.web_child_argv(cap, source, port)],
        core._sandbox_policy(source, venv, data, shared=shared, shares=mounts),
        name,
    )
    log.info("starting web cap=%s tag=%s port=%d", name, tag, port)
    handle = log_file.open("ab")
    try:
        proc = subprocess.Popen(
            cmd,
            cwd=str(source),
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=handle,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    except OSError as exc:
        handle.close()
        raise CapError(f"Could not start uvicorn for {name!r}: {exc}") from exc
    core.register_owned(proc.pid)
    instance = _Instance(
        name=name,
        tag=tag,
        web_entrypoint=web_entrypoint,
        app_name=app_name,
        packages=[],
        proc=proc,
        port=port,
        log_handle=handle,
        started_at=int(time.time()),
    )
    deadline = time.monotonic() + core.serve_boot_timeout()
    while True:
        if proc.poll() is not None:
            core.unregister_owned(proc.pid)
            handle.close()
            raise CapError(
                f"uvicorn for {name!r} exited during boot (code {proc.returncode}):\n"
                f"{_log_tail(log_file)}"
            )
        bound = _parse_bound_port(log_file)
        if bound is not None:
            up, detail = core._http_probe(
                bound, min(deadline, time.monotonic() + 10), proc=proc
            )
            if up:
                instance.port = bound
                instance.endpoints = _fetch_endpoints(bound)
                break
        if time.monotonic() > deadline:
            core._kill_group(proc)
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
            core.unregister_owned(proc.pid)
            handle.close()
            raise CapError(
                f"{name!r} did not answer HTTP within {core.serve_boot_timeout()}s "
                f"(boot budget CAPFORGE_SERVE_BOOT_TIMEOUT):\n{_log_tail(log_file)}"
            )
        time.sleep(0.1)
    with _engine_lock:
        _INSTANCES[name] = instance
    core.sweep_stray_children()
    log.info(
        "serving cap=%s tag=%s port=%d pid=%d endpoints=%d",
        name,
        tag,
        instance.port,
        proc.pid,
        len(instance.endpoints),
    )
    return instance


def _parse_bound_port(path: Path) -> int | None:
    """The uvicorn child logs 'running on http://127.0.0.1:<port>'; that line is
    the authoritative confirmation of what it actually bound."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    found = re.findall(r"running on http://127\.0\.0\.1:(\d+)", text)
    return int(found[-1]) if found else None


def public_url_of(name: str) -> str:
    return public_url(name)


def forget(name: str) -> dict[str, Any]:
    """Stop a served instance and remove its registry/log entry for deletion."""
    stopped = False
    try:
        result = stop(name)
        stopped = bool(result.get("status") == "stopped")
    except CapError:
        pass
    _persist_record(name, None)
    shutil.rmtree(core.state_dir() / "served" / name, ignore_errors=True)
    return {"stopped": stopped}


def _stop_instance(instance: _Instance) -> None:
    """SIGTERM the group, then SIGKILL after a grace period. Reaps the child and
    sweeps anything the instance had already re-parented to us, so nothing of it
    survives the stop (a killed grandchild can otherwise linger as a zombie)."""
    proc = instance.proc
    try:
        os.killpg(proc.pid, 15)
    except OSError:
        pass
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        core._kill_group(proc)
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass
    core.unregister_owned(proc.pid)
    try:
        instance.log_handle.close()
    except OSError:
        pass
    with _engine_lock:
        _INSTANCES.pop(instance.name, None)
    core.sweep_stray_children()
    log.info("stopped web cap=%s tag=%s pid=%s", instance.name, instance.tag, proc.pid)


def _start_locked(name: str) -> dict[str, Any]:
    if name in _INSTANCES and _INSTANCES[name].proc.poll() is None:
        return {**_status_of(name), "warning": "already running"}
    tag, meta, _source = _active_or_raise(name)
    if web_port() == 0:
        raise CapError(
            "web serving is disabled: CAPFORGE_WEB_PORT=0 turns off the reverse proxy"
        )
    instance = _spawn(name, tag, meta["web_entrypoint"], meta["app"])
    _persist_record(name, instance.record())
    return _status_of(name)


def _stop_locked(name: str) -> dict[str, Any]:
    instance = _INSTANCES.get(name)
    if instance is not None:
        _stop_instance(instance)
    record = _read_registry_unlocked().get(name)
    if record is not None:
        record["desired"] = "stop"
        _persist_record(name, record)
    return _status_of(name)


def start(name: str) -> dict[str, Any]:
    """Start (or report) the long-lived instance for a capability's active tag."""
    core.cap_path(name)
    with core.cap_lock(name):
        result = _start_locked(name)
    log.info("serve start cap=%s status=%s", name, result.get("status"))
    return result


def stop(name: str) -> dict[str, Any]:
    core.cap_path(name)
    with core.cap_lock(name):
        result = _stop_locked(name)
    log.info("serve stop cap=%s status=%s", name, result.get("status"))
    return result


def restart(name: str) -> dict[str, Any]:
    """Stop, then start against the currently active tag: the 'pick up the new
    promotion' move."""
    core.cap_path(name)
    with core.cap_lock(name):
        _stop_locked(name)
        result = _start_locked(name)
    log.info("serve restart cap=%s status=%s", name, result.get("status"))
    return result


def _status_of(name: str) -> dict[str, Any]:
    with _engine_lock:
        instance = _INSTANCES.get(name)
    record = _registry_record(name)
    status = "stopped"
    url = None
    version = None
    port = None
    pid = None
    endpoints: list[dict[str, Any]] = []
    if instance is not None:
        version = instance.tag
        endpoints = instance.endpoints
        if instance.proc.poll() is None:
            status = "running"
            port = instance.port
            pid = instance.proc.pid
            url = public_url(name)
        else:
            status = "failed"
            port = instance.port
            pid = instance.proc.pid
            url = public_url(name)
            core.unregister_owned(instance.proc.pid)
            try:
                instance.log_handle.close()
            except OSError:
                pass
            with _engine_lock:
                _INSTANCES.pop(name, None)
    elif record is not None and record.get("desired") == "start":
        status = "failed"
        version = record.get("tag")
        port = record.get("port")
        pid = record.get("pid")
        endpoints = record.get("endpoints") or []
        url = public_url(name)
    elif record is not None:
        version = record.get("tag")
        endpoints = record.get("endpoints") or []
    active = (versioning.read_state(name).get("active") or {}).get("tag")
    log_file = _log_path(name, version) if version else None
    if log_file is not None:
        _truncate_log_if_huge(log_file)
    return {
        "name": name,
        "status": status,
        "url": url,
        "version": version,
        "port": port,
        "pid": pid,
        "endpoints": endpoints,
        "stale": bool(version and active and version != active),
        "desired": (record or {}).get("desired") or ("start" if instance else "stop"),
        "log_tail": _log_tail(log_file) if log_file else "",
    }


def status(name: str) -> dict[str, Any]:
    core.cap_path(name)
    return _status_of(name)


def stop_pinned(name: str, tag: str) -> None:
    """Stop the instance only if it is pinned to `tag`.

    Called from versioning._prune while the caller holds cap_lock(name): this
    function must NOT reacquire it (a nested flock on a second fd in the same
    process deadlocks). Also clears a persisted desired=start so a server
    restart never respawns a deleted version.
    """
    with _engine_lock:
        instance = _INSTANCES.get(name)
    if instance is not None and instance.tag == tag:
        _stop_instance(instance)
        record = _read_registry_unlocked().get(name)
        if record is not None:
            record["desired"] = "stop"
            _persist_record(name, record)
        log.info("stopped pinned instance cap=%s tag=%s before prune", name, tag)
        return
    record = _read_registry_unlocked().get(name)
    if record is not None and record.get("tag") == tag and record.get("desired") == "start":
        record["desired"] = "stop"
        _persist_record(name, record)


def restore() -> int:
    """Re-spawn registry entries whose desired state is start and whose pinned
    version still exists. Failures are logged and marked stop. Called once at
    serve startup, before the listeners come up."""
    core.set_subreaper()
    restored = 0
    for name, record in read_registry().items():
        if record.get("desired") != "start":
            continue
        tag = str(record.get("tag"))
        try:
            source = versioning.version_path(name, tag)
        except CapError:
            continue
        if not source.is_dir():
            log.warning("not restoring cap=%s tag=%s: version files are gone", name, tag)
            record["desired"] = "stop"
            _persist_record(name, record)
            continue
        try:
            with core.cap_lock(name):
                if name in _INSTANCES:
                    continue
                _spawn(
                    name,
                    tag,
                    str(
                        record.get("web_entrypoint")
                        or record.get("entrypoint")
                        or "app.py"
                    ),
                    str(record.get("app") or "app"),
                    public_url=public_url(name),
                )
            restored += 1
        except CapError as exc:
            log.warning("could not restore cap=%s tag=%s: %s", name, tag, exc)
            record["desired"] = "stop"
            _persist_record(name, record)
        except Exception:  # a broken child must not block the server start
            log.exception("unexpected failure restoring cap=%s tag=%s", name, tag)
            record["desired"] = "stop"
            _persist_record(name, record)
    if restored:
        log.info("restored %d served capabilit%s", restored, "y" if restored == 1 else "ies")
    return restored


def shutdown() -> None:
    """Kill every child; leaves desired=start so the next boot restores them."""
    with _engine_lock:
        instances = list(_INSTANCES.values())
    for instance in instances:
        try:
            os.killpg(instance.proc.pid, 9)
        except OSError:
            pass
        try:
            instance.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass
        core.unregister_owned(instance.proc.pid)
        try:
            instance.log_handle.close()
        except OSError:
            pass
    if instances:
        log.info("shut down %d served instance(s)", len(instances))
    with _engine_lock:
        _INSTANCES.clear()


def running_venv_names(name: str) -> set[str]:
    """Venv directory names a live (or desired) instance needs to keep."""
    names: set[str] = set()
    with _engine_lock:
        instances = [inst for inst in _INSTANCES.values() if inst.name == name]
    for instance in instances:
        if instance.proc.poll() is None:
            source = versioning.version_path(name, instance.tag)
            try:
                names.add(core.venv_path(name, source, "prod").name)
            except core.CapError:
                pass
    record = _registry_record(name)
    if record is not None and record.get("desired") == "start":
        source = versioning.version_path(name, str(record.get("tag") or ""))
        try:
            names.add(core.venv_path(name, source, "prod").name)
        except core.CapError:
            pass
    return names


def logs(name: str, tail: int | None = None) -> dict[str, Any]:
    """Served-child log tail for the active tag, without exposing filesystem paths."""
    core.cap_path(name)
    state = versioning.read_state(name)
    tag = (state.get("active") or {}).get("tag")
    if not tag:
        raise CapError(f"{name!r} has no active version to read logs for")
    path = _log_path(name, str(tag))
    from . import staging

    data = staging.read_log(name, "pipeline") if not path.is_file() else None
    if data is None:
        limit = max(1, min(int(tail or core.log_tail_bytes()), core.log_tail_bytes()))
        try:
            size = path.stat().st_size
            with path.open("rb") as handle:
                if size > limit:
                    handle.seek(size - limit)
                raw = handle.read()
        except OSError:
            raw, size = b"", 0
        data = {
            "text": raw.decode("utf-8", errors="replace"),
            "bytes": size,
            "truncated": size > limit,
        }
    return {**data, "kind": "served", "tag": tag}


def served_summaries() -> list[dict[str, Any]]:
    """Compact served state for the JSON index fallback."""
    out = []
    for name in sorted(read_registry()):
        info = _status_of(name)
        out.append(
            {
                "name": name,
                "status": info["status"],
                "url": info["url"],
                "version": info["version"],
            }
        )
    return out


# --------------------------------------------------------------------------- health

_ready_cache: tuple[float, dict[str, Any]] | None = None
_ready_lock = threading.Lock()
READY_TTL = 5.0


async def healthz(request: Request) -> Response:
    """Liveness: the process is serving. No auth and no disk access."""
    return JSONResponse({"ok": True, "service": "capforge", "version": __version__})


def _readiness() -> dict[str, Any]:
    global _ready_cache
    now = time.monotonic()
    with _ready_lock:
        if _ready_cache is not None and now - _ready_cache[0] < READY_TTL:
            return _ready_cache[1]
    from . import sandbox

    state = core.state_dir()
    checks = {
        "state_dir": state.is_dir() and os.access(state, os.W_OK),
        "uv": shutil.which("uv") is not None,
        "git": shutil.which("git") is not None,
        "landlock": sandbox.abi_version() is not None,
    }
    ok = checks["state_dir"] and checks["uv"] and checks["git"]
    if core.sandbox_mode() == "require":
        ok = ok and checks["landlock"]
    payload = {"ok": ok, "service": "capforge", "version": __version__, "checks": checks}
    with _ready_lock:
        _ready_cache = (now, payload)
    return payload


async def readyz(request: Request) -> Response:
    """Readiness: state is writable and the runtime tools are usable."""
    payload = await run_in_threadpool(_readiness)
    return JSONResponse(payload, status_code=200 if payload["ok"] else 503)


# --------------------------------------------------------------------------- auth


def _rate_limited(client_ip: str) -> bool:
    now = time.time()
    with _session_lock:
        attempts = _failed_logins.get(client_ip)
        if attempts is None:
            return False
        while attempts and now - attempts[0] > LOGIN_BACKOFF:
            attempts.popleft()
        return len(attempts) >= MAX_FAILED_LOGINS


def _record_failed_login(client_ip: str) -> None:
    with _session_lock:
        _failed_logins.setdefault(client_ip, deque()).append(time.time())


def _session_valid(sid: str | None) -> bool:
    if not sid:
        return False
    with _session_lock:
        expiry = _sessions.get(sid)
        if expiry is None:
            return False
        if expiry < time.time():
            _sessions.pop(sid, None)
            return False
    return True


def _authenticated(request: Request) -> bool:
    if web_token() is None:
        return False
    return _session_valid(request.cookies.get(SESSION_COOKIE))


def _csrf_ok(request: Request) -> bool:
    return request.headers.get(CSRF_HEADER) == "1"


def _purge_expired_sessions() -> None:
    now = time.time()
    with _session_lock:
        for sid in [sid for sid, expiry in _sessions.items() if expiry < now]:
            _sessions.pop(sid, None)


# --------------------------------------------------------------------------- console API


async def api_login(request: Request) -> Response:
    token = web_token()
    if token is None:
        return JSONResponse(
            {"ok": False, "error": "console disabled: set CAPFORGE_WEB_TOKEN"}, status_code=403
        )
    client_ip = request.client.host if request.client else "?"
    if _rate_limited(client_ip):
        return JSONResponse(
            {"ok": False, "error": "too many failed logins; wait ten minutes"}, status_code=429
        )
    try:
        payload = await request.json()
    except ValueError:
        payload = {}
    supplied = str(payload.get("token") or "")
    if hmac.compare_digest(supplied.encode("utf-8"), token.encode("utf-8")):
        sid = secrets.token_urlsafe(32)
        with _session_lock:
            _sessions[sid] = time.time() + SESSION_TTL
        response = JSONResponse({"ok": True})
        response.set_cookie(
            SESSION_COOKIE,
            sid,
            max_age=SESSION_TTL,
            httponly=True,
            samesite="strict",
            path="/",
        )
        return response
    _record_failed_login(client_ip)
    return JSONResponse({"ok": False, "error": "invalid token"}, status_code=401)


async def api_session(request: Request) -> Response:
    _purge_expired_sessions()
    authed = _authenticated(request)
    return JSONResponse(
        {"ok": True, "authed": authed, "console_enabled": web_token() is not None}
    )


def _console_denied(request: Request, *, write: bool) -> Response | None:
    if web_token() is None:
        return JSONResponse(
            {"ok": False, "error": "console disabled: set CAPFORGE_WEB_TOKEN"}, status_code=403
        )
    if not _authenticated(request):
        return JSONResponse({"ok": False, "error": "not authenticated"}, status_code=401)
    if write and not _csrf_ok(request):
        return JSONResponse(
            {"ok": False, "error": f"missing {CSRF_HEADER} header"}, status_code=403
        )
    return None


def _env_caps_for(name: str) -> list[core.Capability]:
    """Draft plus active-tag declarations; values are never read here."""
    caps = [core.load_capability(name)]
    tag = (versioning.read_state(name).get("active") or {}).get("tag")
    if tag:
        source = versioning.version_path(name, tag)
        if source.is_dir():
            active = core.load_capability_at(name, source)
            if not active.error:
                caps.append(active)
    return caps


def _restart_if_running(name: str) -> dict[str, Any]:
    """Restart a running (or desired-running) web instance after a runtime change."""
    status = _status_of(name)
    if status.get("status") != "running" and status.get("desired") != "start":
        return {"restarted": False}
    try:
        served = restart(name)
    except CapError as exc:
        return {"restarted": False, "restart_error": str(exc)}
    return {"restarted": True, "served": served}


def _restart_after_env(name: str) -> dict[str, Any]:
    """Environment and Landlock are fixed at spawn, so restart a live web child."""
    caps = _env_caps_for(name)
    if not any(cap.serves_web for cap in caps):
        return {"restarted": False}
    return _restart_if_running(name)


def _restart_after_share(name: str) -> dict[str, Any]:
    """A grant change alters both CAPFORGE_SHARES and the Landlock policy."""
    return _restart_if_running(name)


def _share_requests() -> dict[str, dict[str, str]]:
    """folder -> capability -> requested access, from draft and active tags."""
    out: dict[str, dict[str, str]] = {}
    for cap in core.list_capabilities():
        caps = [cap]
        tag = (versioning.read_state(cap.name).get("active") or {}).get("tag")
        if tag:
            source = versioning.version_path(cap.name, tag)
            if source.is_dir():
                active = core.load_capability_at(cap.name, source)
                if not active.error:
                    caps.append(active)
        for item in caps:
            for spec in item.shares:
                out.setdefault(spec.name, {})[cap.name] = spec.access
    return out


def _share_census() -> dict[str, Any]:
    return core.share_census(_share_requests())


async def api_caps(request: Request) -> Response:
    if web_token() is None:
        return JSONResponse(
            {"ok": False, "error": "console disabled: set CAPFORGE_WEB_TOKEN"}, status_code=403
        )
    if not _authenticated(request):
        return JSONResponse({"ok": False, "error": "not authenticated"}, status_code=401)

    def build() -> list[dict[str, Any]]:
        out = []
        for cap in core.list_capabilities():
            state = versioning.read_state(cap.name)
            active = (state.get("active") or {}).get("tag")
            record = _registry_record(cap.name)
            caps_for = _env_caps_for(cap.name)
            served = None
            if record is not None:
                info = _status_of(cap.name)
                served = {
                    "status": info["status"],
                    "url": info["url"],
                    "version": info["version"],
                    "stale": info["stale"],
                    "desired": info["desired"],
                    "endpoints": info["endpoints"],
                }
            out.append(
                {
                    "name": cap.name,
                    "description": cap.description,
                    "kind": cap.kind,
                    "runs_cli": cap.runs_cli,
                    "serves_web": cap.serves_web,
                    "web_route": core.cap_root_path(cap.name) + "/" if cap.serves_web else None,
                    "cli_entrypoint": cap.cli_entrypoint,
                    "web_entrypoint": cap.web_entrypoint,
                    "app": cap.app_name,
                    "active": active,
                    "draft_ready": cap.draft_ready,
                    "draft_error": cap.error,
                    "draft_changed": bool(
                        active
                        and cap.draft_ready
                        and state.get("active", {}).get("content_hash")
                        and state["active"]["content_hash"] != core.tree_hash(cap.path)
                    ),
                    "served": served,
                    "env": core.env_status(cap.name, caps_for),
                    "shares": core.share_status(cap.name, caps_for),
                }
            )
        return out

    caps = await run_in_threadpool(build)
    return JSONResponse({"ok": True, "caps": caps})


async def api_control(request: Request) -> Response:
    if web_token() is None:
        return JSONResponse(
            {"ok": False, "error": "console disabled: set CAPFORGE_WEB_TOKEN"}, status_code=403
        )
    if not _authenticated(request):
        return JSONResponse({"ok": False, "error": "not authenticated"}, status_code=401)
    if not _csrf_ok(request):
        return JSONResponse(
            {"ok": False, "error": f"missing {CSRF_HEADER} header"}, status_code=403
        )
    name = request.path_params["name"]
    action = request.path_params["action"]
    if action not in ("start", "stop", "restart"):
        return JSONResponse({"ok": False, "error": f"unknown action {action!r}"}, status_code=404)
    try:

        def run() -> dict[str, Any]:
            if action == "start":
                return start(name)
            if action == "stop":
                return stop(name)
            return restart(name)

        result = await run_in_threadpool(run)
    except CapError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    return JSONResponse({**result, "ok": True, "action": action})


async def api_env(request: Request) -> Response:
    """Read env status or set one value. Values are write-only: never returned."""
    denied = _console_denied(request, write=request.method != "GET")
    if denied is not None:
        return denied
    name = request.path_params["name"]
    try:
        if request.method == "GET":
            caps = await run_in_threadpool(_env_caps_for, name)
            return JSONResponse({"ok": True, "name": name, "env": core.env_status(name, caps)})
        try:
            payload = await request.json()
        except ValueError:
            payload = {}
        key = str(payload.get("name") or "")
        value = payload.get("value")
        if not isinstance(value, str):
            return JSONResponse(
                {"ok": False, "error": "value must be a string"}, status_code=400
            )
        core.load_capability(name)
        result = await run_in_threadpool(core.set_cap_env, name, key, value)
        restart_info = await run_in_threadpool(_restart_after_env, name)
        caps = await run_in_threadpool(_env_caps_for, name)
        return JSONResponse(
            {**result, **restart_info, "ok": True, "env": core.env_status(name, caps)}
        )
    except CapError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)


async def api_env_var(request: Request) -> Response:
    """Delete one operator-managed value."""
    denied = _console_denied(request, write=True)
    if denied is not None:
        return denied
    name = request.path_params["name"]
    key = request.path_params["var"]
    try:
        core.load_capability(name)
        result = await run_in_threadpool(core.delete_cap_env, name, key)
        restart_info = await run_in_threadpool(_restart_after_env, name)
        caps = await run_in_threadpool(_env_caps_for, name)
        return JSONResponse(
            {**result, **restart_info, "ok": True, "env": core.env_status(name, caps)}
        )
    except CapError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)


async def api_shares(request: Request) -> Response:
    """Census of shared folders and grants, or create a new operator-owned folder."""
    denied = _console_denied(request, write=request.method != "GET")
    if denied is not None:
        return denied
    try:
        if request.method == "GET":
            payload = await run_in_threadpool(_share_census)
            return JSONResponse({"ok": True, **payload})
        try:
            body = await request.json()
        except ValueError:
            body = {}
        name = str(body.get("name") or "")
        note = str(body.get("note") or "")
        result = await run_in_threadpool(core.create_share, name, note)
        census = await run_in_threadpool(_share_census)
        return JSONResponse({**result, "ok": True, **census})
    except CapError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)


async def api_share(request: Request) -> Response:
    """Delete one empty shared folder; `confirm` must equal its name."""
    denied = _console_denied(request, write=True)
    if denied is not None:
        return denied
    folder = request.path_params["folder"]
    try:
        body = await request.json()
    except ValueError:
        body = {}
    confirm = str(body.get("confirm") or "")
    try:
        result = await run_in_threadpool(core.delete_share, folder, confirm)
        census = await run_in_threadpool(_share_census)
        return JSONResponse({**result, "ok": True, **census})
    except CapError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)


async def api_cap_shares(request: Request) -> Response:
    """Declared vs approved shares for one capability."""
    denied = _console_denied(request, write=False)
    if denied is not None:
        return denied
    name = request.path_params["name"]
    try:
        core.load_capability(name)
        caps = await run_in_threadpool(_env_caps_for, name)
        return JSONResponse(
            {"ok": True, "name": name, "shares": core.share_status(name, caps)}
        )
    except CapError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=404)


async def api_cap_share(request: Request) -> Response:
    """Approve, change or revoke one capability's grant for one folder."""
    denied = _console_denied(request, write=True)
    if denied is not None:
        return denied
    name = request.path_params["name"]
    folder = request.path_params["folder"]
    try:
        core.load_capability(name)
        caps = await run_in_threadpool(_env_caps_for, name)
        requested: dict[str, str] = {}
        for cap in caps:
            for spec in cap.shares:
                requested.setdefault(spec.name, spec.access)
        if request.method == "DELETE":
            result = await run_in_threadpool(core.revoke_share_grant, name, folder)
            restart_info = (
                await run_in_threadpool(_restart_after_share, name)
                if result.get("revoked")
                else {"restarted": False}
            )
        else:
            if folder not in requested:
                return JSONResponse(
                    {"ok": False, "error": f"{folder!r} is not declared in deploy.py"},
                    status_code=400,
                )
            try:
                body = await request.json()
            except ValueError:
                body = {}
            access = str(body.get("access") or "").strip().lower()
            if access not in core.SHARE_ACCESS:
                return JSONResponse(
                    {"ok": False, "error": "access must be ro or rw"}, status_code=400
                )
            if access != requested[folder]:
                return JSONResponse(
                    {
                        "ok": False,
                        "error": (
                            f"{folder!r} requests {requested[folder]}, so the grant must "
                            f"match it (got {access})"
                        ),
                    },
                    status_code=400,
                )
            result = await run_in_threadpool(
                core.approve_share_request, name, folder, access
            )
            restart_info = await run_in_threadpool(_restart_after_share, name)
        caps = await run_in_threadpool(_env_caps_for, name)
        return JSONResponse(
            {
                **result,
                **restart_info,
                "ok": True,
                "shares": core.share_status(name, caps),
            }
        )
    except CapError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)


def _bounded_tail(value: str | None) -> int:
    try:
        requested = int(value) if value is not None else core.log_tail_bytes()
    except (TypeError, ValueError):
        requested = core.log_tail_bytes()
    return max(1, min(requested, core.log_tail_bytes()))


async def api_cap_detail(request: Request) -> Response:
    """Full operator view of one capability: tags, pipeline, served state and env."""
    denied = _console_denied(request, write=False)
    if denied is not None:
        return denied
    name = request.path_params["name"]

    def build() -> dict[str, Any]:
        from . import staging

        core.cap_path(name)
        cap = core.load_capability(name)
        state = versioning.read_state(name)
        active = state.get("active") or {}
        record = _registry_record(name)
        caps_for = _env_caps_for(name)
        served = None
        if record is not None:
            info = _status_of(name)
            served = {
                "status": info["status"],
                "url": info["url"],
                "version": info["version"],
                "stale": info["stale"],
                "desired": info["desired"],
                "endpoints": info["endpoints"],
            }
        pipeline = staging.read_state(name)
        log = staging.read_log(name, "pipeline", core.log_tail_bytes()) if pipeline else None
        return {
            "ok": True,
            "name": name,
            "description": cap.description,
            "kind": cap.kind,
            "runs_cli": cap.runs_cli,
            "serves_web": cap.serves_web,
            "web_route": core.cap_root_path(cap.name) + "/" if cap.serves_web else None,
            "cli_entrypoint": cap.cli_entrypoint,
            "web_entrypoint": cap.web_entrypoint,
            "app": cap.app_name,
            "data": cap.data,
            "active": active.get("tag"),
            "active_content_hash": (active.get("content_hash") or "")[:12],
            "draft_ready": cap.draft_ready,
            "draft_error": cap.error,
            "draft_changed": bool(
                active.get("content_hash")
                and cap.draft_ready
                and active["content_hash"] != core.tree_hash(cap.path)
            ),
            "tags": state.get("tags") or [],
            "pruned": state.get("pruned") or [],
            "pipeline": {
                "state": pipeline.get("state"),
                "staging_id": pipeline.get("staging_id"),
                "sha": pipeline.get("sha"),
                "tag": pipeline.get("tag"),
                "updated": pipeline.get("updated"),
                "error": pipeline.get("error"),
            },
            "log": log,
            "served": served,
            "env": core.env_status(name, caps_for),
            "shares": core.share_status(name, caps_for),
        }

    try:
        payload = await run_in_threadpool(build)
    except CapError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=404)
    return JSONResponse(payload)


async def api_logs(request: Request) -> Response:
    """Bounded pipeline, scaffold or served log tail."""
    denied = _console_denied(request, write=False)
    if denied is not None:
        return denied
    name = request.path_params["name"]
    kind = str(request.query_params.get("kind") or "pipeline").strip().lower()
    if kind not in ("pipeline", "scaffold", "served"):
        return JSONResponse(
            {"ok": False, "error": "kind must be pipeline, scaffold or served"}, status_code=400
        )
    tail = _bounded_tail(request.query_params.get("tail"))
    try:
        if kind == "served":
            payload = await run_in_threadpool(logs, name, tail)
        else:
            from . import staging

            payload = await run_in_threadpool(staging.read_log, name, kind, tail)
    except CapError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    return JSONResponse({**payload, "ok": True, "name": name, "kind": kind})


async def api_rollback(request: Request) -> Response:
    """Move the active pointer to an existing tag; no draft or repo change."""
    denied = _console_denied(request, write=True)
    if denied is not None:
        return denied
    name = request.path_params["name"]
    try:
        payload = await request.json()
    except ValueError:
        payload = {}
    tag = str(payload.get("tag") or "")
    if not core.TAG_RE.match(tag):
        return JSONResponse({"ok": False, "error": "a valid tag is required"}, status_code=400)
    try:
        result = await run_in_threadpool(versioning.rollback, name, tag)
    except CapError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    return JSONResponse({**result, "ok": True})


async def api_delete(request: Request) -> Response:
    """Delete a capability and all of its server state. Confirm with the exact name."""
    denied = _console_denied(request, write=True)
    if denied is not None:
        return denied
    name = request.path_params["name"]
    try:
        payload = await request.json()
    except ValueError:
        payload = {}
    confirm = str(payload.get("confirm") or "")
    from . import server as server_module

    result = await run_in_threadpool(server_module.delete_capability, name, confirm)
    return JSONResponse(result, status_code=200 if result.get("ok") else 400)


# ----------------------------------------------------------------------------- git


def _git_auth(request: Request, name: str) -> dict[str, Any] | None:
    from . import staging

    header = request.headers.get("authorization", "")
    if not header.lower().startswith("basic "):
        return None
    try:
        raw = base64.b64decode(header.split(" ", 1)[1], validate=True).decode("utf-8")
        username, _, token = raw.partition(":")
    except (ValueError, UnicodeDecodeError):
        return None
    if not token:
        return None
    record = staging.verify(token, name)
    if record is None:
        return None
    actor = record.get("actor") or {}
    if actor.get("id") and os.environ.get("CAPFORGE_GIT_CHECK_USER", "1") == "1":
        if username != actor.get("id"):
            return None
    result = dict(record)
    result["auth_method"] = "basic"
    result["basic_username"] = username
    return result


def _unauthorized(name: str) -> Response:
    return Response(
        "authentication required\n",
        status_code=401,
        headers={"WWW-Authenticate": f'Basic realm="capforge/{name}"'},
        media_type="text/plain",
    )


def _max_upload_bytes() -> int:
    try:
        return max(1, int(os.environ.get("CAPFORGE_MAX_UPLOAD_BYTES", 64 * 1024 * 1024)))
    except (TypeError, ValueError):
        return 64 * 1024 * 1024


class PayloadTooLarge(Exception):
    """A streamed request body exceeded the configured cap."""


def _declared_too_large(request: Request, limit: int) -> bool:
    declared = request.headers.get("content-length")
    if not declared:
        return False
    try:
        return int(declared) > limit
    except ValueError:
        return False


async def _iter_limited(request: Request, limit: int) -> AsyncIterator[bytes]:
    """Yield request chunks while enforcing a cap, without buffering the body."""
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > limit:
            raise PayloadTooLarge
        if chunk:
            yield chunk


async def _spool_body(request: Request, limit: int) -> Path | None:
    """Spool an upload to disk instead of RAM; None when it exceeds the cap."""
    if _declared_too_large(request, limit):
        return None
    directory = core.state_dir() / "tmp"
    directory.mkdir(parents=True, exist_ok=True)
    handle, raw_path = tempfile.mkstemp(prefix="push-", dir=str(directory))
    path = Path(raw_path)
    total = 0
    try:
        with os.fdopen(handle, "wb") as out:
            async for chunk in request.stream():
                total += len(chunk)
                if total > limit:
                    raise PayloadTooLarge
                out.write(chunk)
    except PayloadTooLarge:
        path.unlink(missing_ok=True)
        return None
    except BaseException:
        # Includes client disconnects/cancellation: never leave a spool behind.
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    return path


def _run_cgi(
    name: str,
    service: str,
    request: Request,
    body_path: Path | None,
    actor: str,
) -> Response:
    """Run one git http-backend request. Called in a threadpool."""
    from . import gitremote

    repo = gitremote.repo_path(name)
    if not (repo / "HEAD").is_file():
        return Response("repository not found\n", status_code=404, media_type="text/plain")
    root = core.state_dir() / "git"
    env = core._base_env()
    env.update(
        {
            "GIT_PROJECT_ROOT": str(root),
            "GIT_HTTP_EXPORT_ALL": "1",
            "PATH_INFO": (
                f"/{name}.git/info/refs"
                if request.method == "GET"
                else f"/{name}.git/{service}"
            ),
            "QUERY_STRING": request.url.query,
            "REQUEST_METHOD": request.method,
            "CONTENT_TYPE": request.headers.get("content-type", ""),
            "CONTENT_LENGTH": str(body_path.stat().st_size) if body_path else "0",
            "REMOTE_USER": actor or "git",
            "REMOTE_ADDR": request.client.host if request.client else "?",
            "GIT_PROTOCOL": request.headers.get("git-protocol", ""),
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_TEMPLATE_DIR": str(root / "empty-template"),
            "CAPFORGE_PIPELINE_LOCK": str(core.state_dir() / "locks" / f"{name}-pipeline.lock"),
        }
    )
    scratch = core.state_dir() / "tmp" / f"git-{name}-{time.time_ns()}"
    scratch.mkdir(parents=True, exist_ok=True)
    env["TMPDIR"] = str(scratch)
    env["HOME"] = str(scratch)
    started = time.monotonic()
    stdin_handle: Any = None
    stdin: Any = subprocess.DEVNULL
    try:
        if body_path is not None:
            stdin_handle = body_path.open("rb")
            stdin = stdin_handle
        proc = subprocess.Popen(
            ["git", "http-backend"],
            stdin=stdin,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            cwd=str(root),
            start_new_session=True,
        )
        try:
            with core.track_child(proc.pid):
                out, err = proc.communicate(timeout=core.git_timeout())
        except subprocess.TimeoutExpired:
            core._kill_group(proc)
            out, err = proc.communicate()
            return Response("git backend timed out\n", status_code=502, media_type="text/plain")
    except OSError as exc:
        return Response(f"git backend failed: {exc}\n", status_code=502, media_type="text/plain")
    finally:
        if stdin_handle is not None:
            try:
                stdin_handle.close()
            except OSError:
                pass
        shutil.rmtree(scratch, ignore_errors=True)
    if proc.returncode != 0 and not out:
        return Response("git backend failed\n", status_code=502, media_type="text/plain")
    header, _, payload = out.partition(b"\r\n\r\n")
    if not header:
        header, _, payload = out.partition(b"\n\n")
    status = 200
    headers: dict[str, str] = {}
    for raw in header.replace(b"\r\n", b"\n").split(b"\n"):
        line = raw.decode("latin-1").strip()
        if not line or ":" not in line:
            continue
        key, value = line.split(":", 1)
        key, value = key.strip(), value.strip()
        if key.lower() == "status":
            try:
                status = int(value.split()[0])
            except (ValueError, IndexError):
                status = 200
        elif key.lower() in {"content-type", "cache-control", "expires", "pragma"}:
            headers[key] = value
    if "content-type" not in {k.lower() for k in headers}:
        headers["Content-Type"] = "application/x-git-receive-pack-result"
    headers["Cache-Control"] = "no-store"
    headers["X-Content-Type-Options"] = "nosniff"
    log.info(
        "git cap=%s service=%s status=%d duration_ms=%d bytes=%d",
        name,
        service,
        status,
        int((time.monotonic() - started) * 1000),
        len(payload),
    )
    return Response(payload, status_code=status, headers=headers)


async def git_info_refs(request: Request) -> Response:
    name = request.path_params["name"]
    if not core.NAME_RE.match(name or ""):
        return Response("not found\n", status_code=404, media_type="text/plain")
    service = request.query_params.get("service", "")
    if service not in {"git-upload-pack", "git-receive-pack"}:
        return Response("smart HTTP only\n", status_code=403, media_type="text/plain")
    record = _git_auth(request, name)
    if record is None:
        return _unauthorized(name)
    return await run_in_threadpool(
        _run_cgi,
        name,
        service,
        request,
        None,
        str((record.get("actor") or {}).get("id") or ""),
    )


async def git_service(request: Request) -> Response:
    from . import gitremote, staging

    name = request.path_params["name"]
    service = request.path_params["service"]
    if not core.NAME_RE.match(name or ""):
        return Response("not found\n", status_code=404, media_type="text/plain")
    if service not in {"git-upload-pack", "git-receive-pack"}:
        return Response("unknown service\n", status_code=404, media_type="text/plain")
    record = _git_auth(request, name)
    if record is None:
        return _unauthorized(name)
    body_path = await _spool_body(request, _max_upload_bytes())
    if body_path is None:
        return Response("request body too large\n", status_code=413, media_type="text/plain")
    actor = str((record.get("actor") or {}).get("id") or "")
    try:
        if service == "git-upload-pack":
            return await run_in_threadpool(_run_cgi, name, service, request, body_path, actor)

        def push() -> Response:
            # The state check and the pipeline start are one critical section with
            # the CGI call: otherwise a second push can pass the check while the
            # first is between _run_cgi and staging.start(), and one tree is dropped
            # or promoted under another push's sha.
            with staging.push_lock(name):
                current = staging.reconcile(name)
                if current.get("state") and current.get("state") not in staging.TERMINAL_STATES:
                    return Response("pipeline busy\n", status_code=409, media_type="text/plain")
                before = gitremote.rev_parse(name)
                response = _run_cgi(name, service, request, body_path, actor)
                after = gitremote.rev_parse(name)
                if response.status_code == 200 and after and after != before:
                    try:
                        staging.start(
                            name,
                            after,
                            record.get("actor") or {},
                            gitremote.commit_subject(name, after),
                            auth_method=str(record.get("auth_method") or "basic"),
                            basic_username=str(record.get("basic_username") or ""),
                        )
                    except core.CapError as exc:
                        log.warning("pipeline start failed cap=%s: %s", name, exc)
            return response

        return await run_in_threadpool(push)
    finally:
        try:
            body_path.unlink(missing_ok=True)
        except OSError:
            log.warning("could not remove spooled upload %s", body_path, exc_info=True)


# --------------------------------------------------------------------------- proxy


async def proxy_redirect(request: Request) -> Response:
    name = request.path_params["name"]
    target = f"/cap/{name}/"
    if request.url.query:
        target = f"{target}?{request.url.query}"
    return RedirectResponse(target, status_code=307)


async def proxy(request: Request) -> Response:
    name = request.path_params["name"]
    path = request.path_params.get("path", "")
    if not core.NAME_RE.match(name):
        return JSONResponse({"error": "unknown capability"}, status_code=404)
    with _engine_lock:
        instance = _INSTANCES.get(name)
        running = instance is not None and instance.proc.poll() is None
    if not running or instance is None:
        return JSONResponse(
            {
                "error": f"{name!r} is not being served",
                "hint": "use serve_capability(name, action='start') on the MCP surface",
            },
            status_code=404,
        )
    if _declared_too_large(request, MAX_PROXY_BODY):
        return JSONResponse({"error": "request body too large"}, status_code=413)
    headers = {
        key: value
        for key, value in request.headers.items()
        if key not in PROXY_DROP_REQUEST
        and key not in {"host", "content-length"}
        and not key.startswith("x-capforge-")
    }
    headers["host"] = f"127.0.0.1:{instance.port}"
    headers["x-capforge-capability"] = name
    headers["x-capforge-version"] = instance.tag
    client: httpx.AsyncClient = request.app.state.proxy_client
    upstream = httpx.URL(f"http://127.0.0.1:{instance.port}/{path}").copy_merge_params(
        request.query_params
    )
    empty_body = request.headers.get("content-length") == "0"
    content: Any = (
        None
        if request.method in ("GET", "HEAD") or empty_body
        else _iter_limited(request, MAX_PROXY_BODY)
    )
    try:
        upstream_request = client.build_request(
            request.method, upstream, headers=headers, content=content
        )
        upstream_response = await client.send(upstream_request, stream=True)
    except PayloadTooLarge:
        return JSONResponse({"error": "request body too large"}, status_code=413)
    except httpx.HTTPError:
        log.warning("proxy error cap=%s upstream=127.0.0.1:%d", name, instance.port)
        return JSONResponse(
            {
                "error": f"{name!r} did not answer",
                "hint": "the served process may have died; check serve_capability(name)",
            },
            status_code=502,
        )
    response_headers = {
        key: value
        for key, value in upstream_response.headers.items()
        if key not in PROXY_DROP_RESPONSE
    }
    response_headers["x-content-type-options"] = "nosniff"
    return StreamingResponse(
        upstream_response.aiter_raw(),
        status_code=upstream_response.status_code,
        headers=response_headers,
        background=BackgroundTask(upstream_response.aclose),
    )


async def json_index(request: Request) -> Response:
    return JSONResponse(
        {
            "ok": True,
            "service": "capforge",
            "ui": False,
            "web_port": web_port(),
            "routes": {"ui": "/ui/", "api": "/api/", "git": "/git/", "cap": "/cap/"},
            "capabilities": await run_in_threadpool(served_summaries),
        }
    )


async def ui_redirect(request: Request) -> Response:
    return RedirectResponse("/ui/", status_code=308)


def asgi_app() -> Starlette:
    """The :8080 application: /ui, /api, /git, /cap and health probes.

    There is no root /{name} catch-all. Route order is health, API, git, UI,
    cap, root. The console and capability proxy share this origin; see
    docs/NEXT.md for the reverse-proxy isolation option.
    """
    routes: list[Any] = [
        Route("/healthz", healthz, methods=["GET"]),
        Route("/readyz", readyz, methods=["GET"]),
        Route("/api/login", api_login, methods=["POST"]),
        Route("/api/session", api_session, methods=["GET"]),
        Route("/api/shares", api_shares, methods=["GET", "POST"]),
        Route("/api/shares/{folder}", api_share, methods=["DELETE"]),
        Route("/api/caps", api_caps, methods=["GET"]),
        Route("/api/caps/{name}/env", api_env, methods=["GET", "POST"]),
        Route("/api/caps/{name}/env/{var}", api_env_var, methods=["DELETE"]),
        Route("/api/caps/{name}/shares", api_cap_shares, methods=["GET"]),
        Route(
            "/api/caps/{name}/shares/{folder}",
            api_cap_share,
            methods=["PUT", "DELETE"],
        ),
        Route("/api/caps/{name}/logs", api_logs, methods=["GET"]),
        Route("/api/caps/{name}/rollback", api_rollback, methods=["POST"]),
        Route("/api/caps/{name}/delete", api_delete, methods=["POST"]),
        Route("/api/caps/{name}", api_cap_detail, methods=["GET"]),
        Route("/api/caps/{name}/{action}", api_control, methods=["POST"]),
        Route("/git/{name}.git/info/refs", git_info_refs, methods=["GET"]),
        Route("/git/{name}.git/{service}", git_service, methods=["POST"]),
    ]
    dist = ui_dir() if ui_enabled() else None
    if dist is not None:
        routes.append(Route("/ui", ui_redirect, methods=["GET"]))
        routes.append(Mount("/ui", StaticFiles(directory=dist, html=True), name="ui"))
    routes.append(Route("/cap/{name}", proxy_redirect, methods=ALL_METHODS))
    routes.append(Route("/cap/{name}/{path:path}", proxy, methods=ALL_METHODS))
    if dist is not None:
        routes.append(Route("/", ui_redirect, methods=["GET"]))
    else:
        routes.append(Route("/", json_index, methods=["GET"]))

    app = Starlette(routes=routes)
    app.state.proxy_client = httpx.AsyncClient(
        timeout=httpx.Timeout(connect=5.0, read=120.0, write=120.0, pool=120.0)
    )
    return app
