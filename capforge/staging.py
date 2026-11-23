"""Git tokens, staging state, bounded logs and the asynchronous push pipeline."""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import shutil
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from . import core, gitremote, versioning

log = logging.getLogger("capforge")

DEFAULT_TOKEN_TTL = 600
MAX_TOKENS_PER_NAME = 20
MAX_TOKENS = 500
MAX_REQUESTS = 10_000
_TERMINAL = {"promoted", "unchanged", "failed", "interrupted"}
# Public alias for callers (web push route) that gate on pipeline activity.
TERMINAL_STATES = frozenset(_TERMINAL)

# staging_id -> name for pipelines whose worker thread is alive in this process.
_ACTIVE: dict[str, str] = {}
_active_lock = threading.Lock()


# --------------------------------------------------------------------------- paths


def staging_dir() -> Path:
    path = core.state_dir() / "staging"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _tokens_file() -> Path:
    return core._contained(staging_dir(), staging_dir() / "tokens.json")


def _state_file(name: str) -> Path:
    if not core.NAME_RE.match(name or ""):
        raise core.CapError(f"Invalid capability name {name!r}.")
    return core._contained(staging_dir(), staging_dir() / f"{name}.json")


def log_path(name: str, kind: str) -> Path:
    if not core.NAME_RE.match(name or ""):
        raise core.CapError(f"Invalid capability name {name!r}.")
    if kind not in {"pipeline", "scaffold"}:
        raise core.CapError("kind must be pipeline or scaffold")
    path = core._contained(core.state_dir() / "logs", core.state_dir() / "logs" / name / f"{kind}.log")
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


# --------------------------------------------------------------------------- logs


_REDACT = (
    (re.compile(r"(?i)(authorization:\s*)\S+"), r"\1[redacted]"),
    (re.compile(r"\bcfg_[A-Za-z0-9_.-]+"), "[redacted-token]"),
    (re.compile(r"(?i)((?:api[_-]?key|token|secret|password)\s*[=:]\s*)\S+"), r"\1[redacted]"),
)


def _redact(text: str) -> str:
    for pattern, replacement in _REDACT:
        text = pattern.sub(replacement, text)
    return text


def append_log(name: str, kind: str, text: str) -> None:
    path = log_path(name, kind)
    payload = _redact(text.rstrip() + "\n")
    try:
        with path.open("a", encoding="utf-8") as handle:
            handle.write(payload)
        if path.stat().st_size > core.log_max_bytes():
            data = path.read_text(encoding="utf-8", errors="replace")
            path.write_text(data[-core.log_max_bytes() :], encoding="utf-8")
    except OSError:
        log.warning("could not write %s log for %s", kind, name, exc_info=True)


def read_log(name: str, kind: str, tail: int | None = None) -> dict[str, Any]:
    path = log_path(name, kind)
    limit = max(1, min(int(tail or core.log_tail_bytes()), core.log_tail_bytes()))
    try:
        size = path.stat().st_size
        with path.open("rb") as handle:
            if size > limit:
                handle.seek(size - limit)
            data = handle.read()
    except OSError:
        return {"text": "", "bytes": 0, "truncated": False}
    return {
        "text": data.decode("utf-8", errors="replace"),
        "bytes": size,
        "truncated": size > limit,
    }


# ------------------------------------------------------------------------- tokens


def _read_tokens() -> dict[str, dict[str, Any]]:
    path = _tokens_file()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_tokens(data: dict[str, dict[str, Any]]) -> None:
    path = _tokens_file()
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


def _ttl() -> int:
    try:
        return max(60, int(os.environ.get("CAPFORGE_GIT_TOKEN_TTL", DEFAULT_TOKEN_TTL)))
    except ValueError:
        return DEFAULT_TOKEN_TTL


def mint(name: str, actor: dict[str, Any] | None = None) -> dict[str, Any]:
    core.cap_path(name)
    if core.pending_delete(name):
        raise core.CapError(
            f"{name!r} is pending deletion; an operator must purge or cancel it first"
        )
    actor = actor or {}
    if os.environ.get("CAPFORGE_REQUIRE_USER", "0").strip() == "1" and not actor.get("id"):
        raise core.CapError("An authenticated actor is required to mint a git token")
    now = int(time.time())
    ttl = _ttl()
    token_id = secrets.token_hex(8)
    secret = secrets.token_urlsafe(32)
    record = {
        "name": name,
        "hash": hashlib.sha256(secret.encode("utf-8")).hexdigest(),
        "actor": actor,
        "created_at": now,
        "expires_at": now + ttl,
        "requests": 0,
        "bytes": 0,
        "last_used": None,
    }
    with core._file_lock(core.state_dir() / "locks" / "staging-tokens.lock"):
        tokens = _read_tokens()
        tokens = {
            key: value
            for key, value in tokens.items()
            if isinstance(value, dict) and int(value.get("expires_at") or 0) > now
        }
        live = [value for value in tokens.values() if value.get("name") == name]
        if len(live) >= MAX_TOKENS_PER_NAME:
            raise core.CapError(f"Too many live git tokens for {name!r}")
        if len(tokens) >= MAX_TOKENS:
            raise core.CapError("Too many live git tokens")
        tokens[token_id] = record
        _write_tokens(tokens)
    log.info("minted git token cap=%s actor=%s ttl=%ds", name, actor.get("id"), ttl)
    return {
        "token": f"cfg_{token_id}.{secret}",
        "ttl_s": ttl,
        "expires_at": record["expires_at"],
        "actor": actor,
        "name": name,
        "auth": "basic",
        "basic_username": actor.get("id") or "git",
    }


def verify(token: str, name: str) -> dict[str, Any] | None:
    try:
        prefix, secret = token.split(".", 1)
        token_id = prefix.removeprefix("cfg_")
    except ValueError:
        return None
    if not token_id or not secret:
        return None
    now = int(time.time())
    with core._file_lock(core.state_dir() / "locks" / "staging-tokens.lock"):
        tokens = _read_tokens()
        record = tokens.get(token_id)
        if not isinstance(record, dict):
            return None
        if record.get("name") != name or int(record.get("expires_at") or 0) <= now:
            return None
        if int(record.get("requests") or 0) >= MAX_REQUESTS:
            return None
        expected = str(record.get("hash") or "")
        if not hmac.compare_digest(hashlib.sha256(secret.encode("utf-8")).hexdigest(), expected):
            return None
        record["requests"] = int(record.get("requests") or 0) + 1
        record["last_used"] = now
        _write_tokens(tokens)
        return record


def revoke_tokens(name: str) -> int:
    """Drop every outstanding git token for one capability."""
    removed = 0
    with core._file_lock(core.state_dir() / "locks" / "staging-tokens.lock"):
        tokens = _read_tokens()
        kept = {
            key: value
            for key, value in tokens.items()
            if value.get("name") != name
        }
        removed = len(tokens) - len(kept)
        if removed:
            _write_tokens(kept)
    if removed:
        log.info("revoked %d git token(s) cap=%s", removed, name)
    return removed


# -------------------------------------------------------------------------- state


def read_state(name: str) -> dict[str, Any]:
    path = _state_file(name)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def write_state(name: str, data: dict[str, Any]) -> None:
    path = _state_file(name)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


def latest(name: str) -> dict[str, Any]:
    data = reconcile(name)
    tail = read_log(name, "pipeline") if data else {"text": "", "bytes": 0, "truncated": False}
    return {**data, "log_tail": tail["text"], "log_bytes": tail["bytes"], "log_truncated": tail["truncated"]}


def active(name: str) -> bool:
    data = read_state(name)
    return bool(data) and data.get("state") not in _TERMINAL


def _pipeline_lock_path(name: str) -> Path:
    return core._contained(
        core.state_dir(), core.state_dir() / "locks" / f"{name}-pipeline.lock"
    )


def reconcile(name: str) -> dict[str, Any]:
    """Mark a non-terminal pipeline as interrupted when no worker can own it.

    A crash or restart leaves `received`/`testing` behind. The pipeline flock is
    released with the process, so a free lock plus no in-process worker means
    the state is stale. Recover it so pushes and deletes are not wedged forever.
    """
    data = read_state(name)
    state = data.get("state")
    if not state or state in _TERMINAL:
        return data
    staging_id = str(data.get("staging_id") or "")
    with _active_lock:
        if _ACTIVE.get(name) == staging_id:
            return data
    with core.try_file_lock(_pipeline_lock_path(name)) as free:
        # Re-check under the lock: a worker may have started and registered
        # between the read above and here.
        with _active_lock:
            if _ACTIVE.get(name) == staging_id:
                return data
        current = read_state(name)
        if current.get("state") != state:
            return current
        if not free:
            return current
        _set(
            name,
            staging_id,
            "interrupted",
            error="pipeline worker disappeared; marked interrupted",
        )
        log.warning("reconciled stale pipeline cap=%s staging=%s was=%s", name, staging_id, state)
        return read_state(name)


def reconcile_all() -> list[str]:
    """Recover every stale staging record at startup; returns the names touched."""
    root = staging_dir()
    recovered: list[str] = []
    for path in sorted(root.glob("*.json")):
        name = path.stem
        if name == "tokens" or not core.NAME_RE.match(name):
            continue
        before = read_state(name).get("state")
        if reconcile(name).get("state") == "interrupted" and before != "interrupted":
            recovered.append(name)
    if recovered:
        log.info("reconciled %d interrupted pipeline(s)", len(recovered))
    return recovered


# --------------------------------------------------------------------------- locks


@contextmanager
def push_lock(name: str) -> Iterator[None]:
    lock = core._contained(core.state_dir(), core.state_dir() / "locks" / f"{name}-git-push.lock")
    with core._file_lock(lock):
        yield


@contextmanager
def pipeline_lock(name: str) -> Iterator[None]:
    with core._file_lock(_pipeline_lock_path(name)):
        yield


def forget(name: str) -> None:
    """Remove token records, staging state and logs for a deleted capability."""
    _state_file(name).unlink(missing_ok=True)
    with core._file_lock(core.state_dir() / "locks" / "staging-tokens.lock"):
        tokens = _read_tokens()
        _write_tokens({k: v for k, v in tokens.items() if v.get("name") != name})
    shutil.rmtree(core.state_dir() / "logs" / name, ignore_errors=True)


def _backups_dir(name: str) -> Path:
    return core.cap_path(name).parent / ".staging" / "backups"


def cleanup_backups(name: str) -> None:
    """Remove every draft backup left for one capability (delete/restore safety)."""
    directory = _backups_dir(name)
    if not directory.is_dir():
        return
    for entry in directory.glob(f"{name}-*"):
        shutil.rmtree(entry, ignore_errors=True)


def sweep_backups(max_age_seconds: int = 24 * 3600) -> list[str]:
    """Drop stale draft backups that survived a crash; best effort at startup."""
    caps = core.caps_dir()
    directory = caps / ".staging" / "backups"
    if not directory.is_dir():
        return []
    now = time.time()
    removed: list[str] = []
    for entry in directory.iterdir():
        try:
            if now - entry.stat().st_mtime < max_age_seconds:
                continue
        except OSError:
            continue
        shutil.rmtree(entry, ignore_errors=True)
        removed.append(entry.name)
    if removed:
        log.info("swept %d stale staging backup(s)", len(removed))
    return removed


def _remove_stale(path: Path, now: float, max_age_seconds: int) -> bool:
    try:
        if now - path.stat().st_mtime < max_age_seconds:
            return False
    except OSError:
        return False
    try:
        if path.is_dir():
            shutil.rmtree(path, ignore_errors=True)
        else:
            path.unlink(missing_ok=True)
    except OSError:
        return False
    return True


def sweep_stale(max_age_seconds: int = 24 * 3600) -> list[str]:
    """Drop staging trees and upload/scratch files left by a crashed pipeline."""
    now = time.time()
    removed: list[str] = []
    directory = core.caps_dir() / ".staging"
    if directory.is_dir():
        for entry in directory.iterdir():
            if entry.name == "backups" or not entry.is_dir():
                continue
            if _remove_stale(entry, now, max_age_seconds):
                removed.append(entry.name)
    tmp = core.state_dir() / "tmp"
    if tmp.is_dir():
        for entry in tmp.iterdir():
            if not (entry.name.startswith("push-") or entry.name.startswith("git-")):
                continue
            if _remove_stale(entry, now, max_age_seconds):
                removed.append(entry.name)
    if removed:
        log.info("swept %d stale staging artifact(s)", len(removed))
    return removed


# ------------------------------------------------------------------------ pipeline


def _set(name: str, staging_id: str, state: str, **fields: Any) -> None:
    data = read_state(name)
    data.update({"name": name, "staging_id": staging_id, "state": state, "updated": int(time.time())})
    data.update(fields)
    write_state(name, data)
    append_log(
        name,
        "pipeline",
        f"[{staging_id}] {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} {state}"
        + (f" {fields}" if fields else ""),
    )


def start(
    name: str,
    sha: str,
    actor: dict[str, Any],
    subject: str,
    *,
    auth_method: str = "basic",
    basic_username: str = "",
) -> dict[str, Any]:
    core.cap_path(name)
    if core.pending_delete(name):
        raise core.CapError(
            f"{name!r} is pending deletion; an operator must purge or cancel it first"
        )
    if not gitremote.repo_path(name).is_dir():
        raise core.CapError(f"No repository for {name!r}; scaffold it first")
    current = reconcile(name)
    if current.get("state") not in _TERMINAL and current.get("state"):
        raise core.CapError(f"A pipeline is already running for {name!r}")
    staging_id = f"{int(time.time())}-{secrets.token_hex(3)}"
    # Register before writing the state so a concurrent `latest`/`start` can
    # never mistake a just-started worker for a crashed one.
    with _active_lock:
        _ACTIVE[name] = staging_id
    try:
        _set(
            name,
            staging_id,
            "received",
            sha=sha,
            actor=actor,
            git_source={"ref": "refs/heads/main", "sha": sha, "subject": subject},
            tag=None,
            error=None,
            auth_method=auth_method,
            basic_username=basic_username,
        )
        thread = threading.Thread(
            target=_run_pipeline,
            args=(name, staging_id, sha, actor, subject, auth_method, basic_username),
            daemon=True,
        )
        thread.start()
    except Exception:
        with _active_lock:
            _ACTIVE.pop(name, None)
        raise
    return {"ok": True, "name": name, "staging_id": staging_id, "state": "received", "sha": sha}


def _run_pipeline(
    name: str,
    staging_id: str,
    sha: str,
    actor: dict[str, Any],
    subject: str,
    auth_method: str,
    basic_username: str,
) -> None:
    parent = core.cap_path(name).parent / ".staging" / f"{name}-{staging_id}"
    tree = parent / "tree"
    backup: Path | None = None
    try:
        with pipeline_lock(name):
            _set(name, staging_id, "extracting", sha=sha)
            gitremote.materialise(name, tree)
            _set(name, staging_id, "validating", sha=sha)
            cap = core.load_capability_at(name, tree)
            if cap.error:
                raise core.CapError(f"pushed tree is not runnable: {cap.error}")
            _set(name, staging_id, "testing", sha=sha)
            tested = core.run_tests(cap, tree)
            append_log(
                name,
                "pipeline",
                "tests stdout:\n" + tested.stdout[-8000:] + "\ntests stderr:\n" + tested.stderr[-8000:],
            )
            if not tested.ok:
                raise core.CapError(
                    "Tests failed; promotion refused.\n"
                    + (tested.stderr.strip() or tested.stdout.strip())[-2000:]
                )
            draft = core.cap_path(name)
            stamp = time.strftime("%Y%m%d-%H%M%S")
            with core.cap_lock(name):
                if draft.exists():
                    backup = parent.parent / "backups" / f"{name}-{stamp}"
                    backup.parent.mkdir(parents=True, exist_ok=True)
                    os.replace(draft, backup)
                shutil.rmtree(draft, ignore_errors=True)
                os.replace(tree, draft)
                try:
                    result = versioning.promote(
                        name,
                        tag=None,
                        note=(
                            f"git {sha[:12]} by {actor.get('id') or 'unknown'} "
                            f"({auth_method}:{basic_username or '-'}): {subject}"
                        ),
                        git_source={
                            "ref": "refs/heads/main",
                            "sha": sha,
                            "subject": subject,
                            "auth_method": auth_method,
                            "basic_username": basic_username,
                        },
                        pushed_by=actor.get("id"),
                        locked=True,
                    )
                except Exception:
                    # Restore the previous draft if the state update failed.
                    shutil.rmtree(draft, ignore_errors=True)
                    if backup is not None and backup.exists():
                        os.replace(backup, draft)
                    raise
                else:
                    # The swap and promotion succeeded; the backup is no longer
                    # needed and would otherwise grow the caps directory forever.
                    if backup is not None:
                        shutil.rmtree(backup, ignore_errors=True)
            if result.get("promoted"):
                _set(name, staging_id, "promoted", sha=sha, tag=result.get("tag"))
            else:
                _set(name, staging_id, "unchanged", sha=sha, tag=None)
            gitremote.update_processed(name, sha)
            try:
                gitremote.gc(name, timeout=30)
            except Exception:
                log.warning("git gc failed for %s", name, exc_info=True)
    except Exception as exc:
        append_log(name, "pipeline", f"[{staging_id}] failure: {exc}\n")
        _set(name, staging_id, "failed", sha=sha, error=str(exc)[-4000:])
        log.warning("pipeline failed cap=%s staging=%s: %s", name, staging_id, exc)
    finally:
        with _active_lock:
            if _ACTIVE.get(name) == staging_id:
                _ACTIVE.pop(name, None)
        shutil.rmtree(parent, ignore_errors=True)
