"""Versioning: snapshot a draft into an immutable tag, promote it, roll back, prune.

The working copy in CAPFORGE_CAPS_DIR is always the draft. Tags live outside it, so a
rollback never depends on what the model did to the working copy afterwards.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import time
from contextlib import nullcontext
from pathlib import Path
from typing import Any

from .core import (
    PYTHON_VERSION,
    CapError,
    cap_lock,
    cap_path,
    data_dir,
    keep_tags,
    lock_hash,
    read_state,
    state_dir,
    tag_lock_path,
    tree_hash,
    try_file_lock,
    version_path,
    write_state,
)

log = logging.getLogger("capforge")

SKIP = shutil.ignore_patterns(
    "__pycache__",
    "*.pyc",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".venv",
    "venv",
    ".git",
)
MAX_PRUNED = 100


def _git(*args: str, cwd: Path) -> tuple[int, str]:
    try:
        proc = subprocess.run(
            ["git", *args], cwd=str(cwd), capture_output=True, text=True, timeout=30
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 1, str(exc)
    return proc.returncode, (proc.stdout or proc.stderr).strip()


def snapshot(name: str, tag: str) -> dict[str, Any]:
    """Copy the current working copy into an immutable version folder."""
    src = cap_path(name)
    if not src.is_dir():
        raise CapError(f"No capability named {name!r}")
    dest = version_path(name, tag)
    if dest.exists():
        raise CapError(f"Tag {tag!r} already exists for {name!r}. Tags are immutable; pick another.")
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(src, dest, ignore=SKIP)
    return {
        "tag": tag,
        "path": str(dest),
        "content_hash": tree_hash(dest),
        "created_at": int(time.time()),
    }


def promote(
    name: str,
    tag: str | None,
    *,
    force: bool = False,
    dry_run: bool = False,
    note: str = "",
    git_source: dict[str, Any] | None = None,
    pushed_by: str | None = None,
    locked: bool = False,
) -> dict[str, Any]:
    """Tag the draft and make it the active version. Refuses on an unchanged draft.

    `locked=True` is for the git worker, which already holds `cap_lock` across the
    draft swap; reacquiring it would deadlock. The public/CLI path leaves it false.
    """
    from .core import load_capability

    cap = load_capability(name)
    if cap.error:
        raise CapError(f"Cannot promote {name!r}: {cap.error}")
    if not cap.draft_ready:
        raise CapError(
            f"Cannot promote {name!r}: {cap.error or 'a declared entrypoint is missing'}"
        )

    with (nullcontext() if locked else cap_lock(name)):
        draft_hash = tree_hash(cap.path)
        lock = lock_hash(cap.path)
        state = read_state(name)
        active = state.get("active")
        tags = list(state.get("tags") or [])

        if active and active.get("content_hash") == draft_hash and not force:
            return {
                "promoted": False,
                "reason": "The draft is identical to the active version. Nothing was tagged.",
                "active": active,
            }

        final_tag = tag or f"v{len(tags) + 1}-{time.strftime('%Y%m%d-%H%M%S')}"
        result: dict[str, Any] = {
            "promoted": True,
            "dry_run": dry_run,
            "capability": name,
            "tag": final_tag,
        }

        if dry_run:
            result["content_hash"] = draft_hash
            result["env"] = "uv"
            result["lock_hash"] = lock
            result["would_prune"] = _would_prune(
                tags, keep_tags(), active_tag=(active or {}).get("tag")
            )
            return result

        snap = snapshot(name, final_tag)
        snap["env"] = "uv"
        snap["lock_hash"] = lock
        snap["python"] = PYTHON_VERSION
        snap["kind"] = cap.kind
        snap["modes"] = list(cap.modes)
        snap["cli_entrypoint"] = cap.cli_entrypoint
        snap["web_entrypoint"] = cap.web_entrypoint
        snap["note"] = note
        snap["git_source"] = git_source
        snap["pushed_by"] = pushed_by
        snap["promoted_at"] = int(time.time())
        snap["previous"] = active.get("tag") if active else None

        tags = [t for t in tags if t.get("tag") != final_tag]
        tags.append(snap)
        state.update({"active": snap, "tags": tags})
        pruned = _prune(state, keep_tags())
        write_state(name, state)

    result.update(
        {
            "content_hash": snap["content_hash"],
            "env": snap["env"],
            "lock_hash": snap["lock_hash"],
            "kind": snap["kind"],
            "modes": snap["modes"],
            "cli_entrypoint": snap["cli_entrypoint"],
            "web_entrypoint": snap["web_entrypoint"],
            "previous": snap["previous"],
            "pruned": pruned,
            "version_path": snap["path"],
        }
    )
    log.info(
        "promote cap=%s tag=%s previous=%s pruned=%s",
        name,
        final_tag,
        snap["previous"],
        pruned,
    )
    return result


def _would_prune(
    tags: list[dict[str, Any]], keep: int, active_tag: str | None = None
) -> list[str]:
    """Tags that would be dropped if one more tag were added. Oldest first, active excluded."""
    ordered = [
        tag
        for tag in sorted(tags, key=lambda t: t.get("created_at") or 0)
        if tag.get("tag") != active_tag
    ]
    surplus = len(tags) + 1 - keep
    return [tag["tag"] for tag in ordered[: max(0, surplus)]]


def _venv_lock_path(name: str, digest: str, group: str) -> Path:
    return state_dir() / "locks" / f"{name}-venv-{digest}-{group}.lock"


def _venv_parts(entry_name: str) -> tuple[str, str, str]:
    """Split a venv dir name into (digest, group, lock_suffix)."""
    if entry_name.endswith("-py314-test"):
        return entry_name[: -len("-py314-test")], "test", "-py314-test"
    if entry_name.endswith("-py314"):
        return entry_name[: -len("-py314")], "prod", "-py314"
    return "", "", ""


def _gc_venvs(name: str, tags: list[dict[str, Any]]) -> None:
    """Drop uv venvs no surviving tag (or its test twin) can use. Best effort."""
    base = state_dir() / "venvs" / name
    if not base.is_dir():
        return
    live: set[str] = set()
    for tag in tags:
        lock = str(tag.get("lock_hash") or "")
        if lock:
            live.add(f"{lock}-py314")
            live.add(f"{lock}-py314-test")
    # The draft (and a test run against it) is not represented in `tags`; keep
    # its lock hash alive so a promote/prune cannot delete a venv mid-build.
    try:
        draft_lock = lock_hash(cap_path(name))
        live.add(f"{draft_lock}-py314")
        live.add(f"{draft_lock}-py314-test")
    except (CapError, OSError):
        pass
    try:
        from . import web

        live.update(web.running_venv_names(name))
    except Exception:  # best effort; web.py may be absent or broken
        log.debug("venv GC could not consult the serve registry for %s", name, exc_info=True)
    for entry in sorted(base.iterdir()):
        if not entry.is_dir() or entry.name in live:
            continue
        digest, group, suffix = _venv_parts(entry.name)
        if not suffix and not entry.name.endswith(".building"):
            continue
        if not suffix:
            shutil.rmtree(entry, ignore_errors=True)
            log.info("pruned incomplete venv cap=%s path=%s", name, entry)
            continue
        # Never delete a venv while `ensure_venv` is building or using it.
        lock_path = _venv_lock_path(name, digest, group)
        with try_file_lock(lock_path) as free:
            if not free:
                continue
            shutil.rmtree(entry, ignore_errors=True)
            lock_path.unlink(missing_ok=True)
        log.info("pruned venv cap=%s path=%s", name, entry)


def _prune(state: dict[str, Any], keep: int) -> list[str]:
    """Drop the oldest tags beyond the keep limit. The active one is never dropped."""
    tags = list(state.get("tags") or [])
    active_tag = (state.get("active") or {}).get("tag")
    dropped: list[str] = []
    blocked: set[str] = set()
    while len(tags) > keep:
        ordered = sorted(tags, key=lambda t: t.get("created_at") or 0)
        victim = next(
            (
                t
                for t in ordered
                if t["tag"] != active_tag and t["tag"] not in blocked
            ),
            None,
        )
        if victim is None:
            break
        # A CLI run holds a shared lock for its whole lifetime. Take the
        # exclusive lock non-blocking so we never remove a version, data dir or
        # venv under a running process; retry this tag on the next promotion.
        with try_file_lock(tag_lock_path(state["name"], victim["tag"])) as free:
            if not free:
                blocked.add(victim["tag"])
                log.info(
                    "prune deferred cap=%s tag=%s (run in progress)",
                    state["name"],
                    victim["tag"],
                )
                continue
            tags.remove(victim)
            # A served instance pinned to this tag is about to lose its version
            # dir, data and venv. Stop it first. Caller (promote) holds
            # cap_lock(name): stop_pinned must not reacquire it, a nested flock
            # would deadlock.
            try:
                from . import web

                web.stop_pinned(state["name"], victim["tag"])
            except Exception:  # best effort; web.py may be absent or broken
                log.warning(
                    "could not stop a served instance for pruned tag %s@%s",
                    state["name"],
                    victim["tag"],
                    exc_info=True,
                )
            shutil.rmtree(version_path(state["name"], victim["tag"]), ignore_errors=True)
            shutil.rmtree(data_dir(state["name"], victim["tag"], create=False), ignore_errors=True)
            shutil.rmtree(
                state_dir() / "served" / state["name"] / victim["tag"],
                ignore_errors=True,
            )
            state.setdefault("pruned", []).append({"tag": victim["tag"], "at": int(time.time())})
            dropped.append(victim["tag"])
    state["tags"] = tags
    if dropped:
        _gc_venvs(state["name"], tags)
        pruned = state.get("pruned")
        if isinstance(pruned, list) and len(pruned) > MAX_PRUNED:
            state["pruned"] = pruned[-MAX_PRUNED:]
    return dropped


def rollback(name: str, tag: str) -> dict[str, Any]:
    """Make an existing tag active again, without touching the draft."""
    with cap_lock(name):
        state = read_state(name)
        entry = next((t for t in state.get("tags") or [] if t.get("tag") == tag), None)
        if entry is None:
            known = [t.get("tag") for t in state.get("tags") or []]
            raise CapError(f"No tag {tag!r} for {name!r}. Known tags: {known}")
        if not version_path(name, tag).is_dir():
            raise CapError(f"Tag {tag!r} is recorded but its files are gone from {version_path(name, tag)}")
        previous = (state.get("active") or {}).get("tag")
        state["active"] = entry
        write_state(name, state)
    log.info("rollback cap=%s tag=%s was=%s", name, tag, previous)
    return {
        "rolled_back": True,
        "capability": name,
        "active": tag,
        "was": previous,
        "content_hash": entry.get("content_hash"),
    }


def list_tags(name: str) -> dict[str, Any]:
    state = read_state(name)
    active = (state.get("active") or {}).get("tag")
    return {
        "capability": name,
        "active": active,
        "keep": keep_tags(),
        "tags": [
            {
                "tag": t.get("tag"),
                "created_at": t.get("created_at"),
                "content_hash": (t.get("content_hash") or "")[:12],
                "packages": t.get("packages") or [],
                "kind": t.get("kind") or "cli",
                "modes": t.get("modes") or [],
                "cli_entrypoint": t.get("cli_entrypoint") or "",
                "web_entrypoint": t.get("web_entrypoint") or "",
                "note": t.get("note") or "",
                "active": t.get("tag") == active,
            }
            for t in state.get("tags") or []
        ],
        "pruned": [p.get("tag") for p in state.get("pruned") or []],
    }


def diff_against_active(name: str, tag: str | None = None) -> str:
    """Unified diff between a tag and the working copy (or another tag)."""
    state = read_state(name)
    active = tag or (state.get("active") or {}).get("tag")
    if not active:
        raise CapError(f"{name!r} has no active version to diff against")
    base = version_path(name, active)
    if not base.is_dir():
        raise CapError(f"Version {active!r} is missing from {base}")
    code, out = _git(
        "diff", "--no-index", "--no-color", "-U2", str(base), str(cap_path(name)), cwd=state_dir()
    )
    if code not in (0, 1):
        raise CapError(f"git diff failed: {out}")
    return out or "(no differences)"
