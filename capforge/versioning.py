"""Versioning: snapshot a draft into an immutable tag, promote it, roll back, prune.

The working copy in CAPFORGE_CAPS_DIR is always the draft. Tags live outside it, so a
rollback never depends on what the model did to the working copy afterwards.
"""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

from .core import (
    CapError,
    cap_lock,
    cap_path,
    data_dir,
    keep_tags,
    read_state,
    requirements_hash,
    state_dir,
    tree_hash,
    validate_packages,
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


def _ensure_git(root: Path) -> None:
    """Make the caps tree a git repo, if it is not one already. Best effort, never fatal."""
    if not root.is_dir():
        return
    if (root / ".git").exists():
        return
    code, _ = _git("init", "-q", cwd=root)
    if code != 0:
        log.warning("could not initialise git in %s; audit trail disabled", root)
        return
    _git("config", "user.email", "capforge@localhost", cwd=root)
    _git("config", "user.name", "capforge", cwd=root)
    ignore = root / ".gitignore"
    if not ignore.exists():
        ignore.write_text(
            "__pycache__/\n*.pyc\n.pytest_cache/\n.mypy_cache/\n.ruff_cache/\n.venv/\n",
            encoding="utf-8",
        )


def git_commit(name: str, tag: str) -> str | None:
    """Commit the capability folder as the audit record for `tag`. Returns the commit sha."""
    root = cap_path(name).parent
    _ensure_git(root)
    if not (root / ".git").exists():
        return None
    _git("add", "-A", name, cwd=root)
    code, out = _git("commit", "-q", "-m", f"{name}@{tag}", cwd=root)
    if code != 0 and "nothing to commit" not in out:
        log.warning("git commit for %s@%s failed: %s", name, tag, out[-400:])
        return None
    code, sha = _git("rev-parse", "--short", "HEAD", cwd=root)
    return sha if code == 0 else None


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
    packages: list[str] | None = None,
    force: bool = False,
    dry_run: bool = False,
    note: str = "",
) -> dict[str, Any]:
    """Tag the draft and make it the active version. Refuses on an unchanged draft."""
    from .core import load_capability

    cap = load_capability(name)
    if cap.error:
        raise CapError(f"Cannot promote {name!r}: {cap.error}")
    if not cap.draft_ready:
        raise CapError(f"Cannot promote {name!r}: entrypoint {cap.entrypoint!r} is missing")
    if packages is not None:
        packages = validate_packages(packages)

    with cap_lock(name):
        draft_hash = tree_hash(cap.path)
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
            result["packages"] = packages if packages is not None else cap.packages
            result["would_prune"] = _would_prune(
                tags, keep_tags(), active_tag=(active or {}).get("tag")
            )
            return result

        # Record the packages before snapshotting, so the tag carries the frontmatter its venv is built from.
        if packages is not None and list(packages) != list(cap.packages):
            write_packages(name, packages)
            cap.packages = list(packages)
            draft_hash = tree_hash(cap.path)

        snap = snapshot(name, final_tag)
        snap["packages"] = list(cap.packages)
        snap["entrypoint"] = cap.entrypoint
        snap["requirements_hash"] = requirements_hash(snap["packages"])
        snap["note"] = note
        snap["promoted_at"] = int(time.time())
        snap["previous"] = active.get("tag") if active else None

        tags = [t for t in tags if t.get("tag") != final_tag]
        tags.append(snap)
        state.update({"active": snap, "tags": tags})
        pruned = _prune(state, keep_tags())
        write_state(name, state)

        sha = git_commit(name, final_tag)
    result.update(
        {
            "content_hash": snap["content_hash"],
            "packages": snap["packages"],
            "entrypoint": snap["entrypoint"],
            "previous": snap["previous"],
            "pruned": pruned,
            "git_commit": sha,
            "version_path": snap["path"],
        }
    )
    log.info(
        "promote cap=%s tag=%s previous=%s pruned=%s commit=%s",
        name,
        final_tag,
        snap["previous"],
        pruned,
        sha,
    )
    return result


def write_packages(name: str, packages: list[str]) -> None:
    """Persist the package list into SKILL.md so the draft records what it needs.

    Callers must hold cap_lock(name); the write is a read-modify-write of the draft.
    """
    clean = validate_packages(packages)
    path = cap_path(name) / "SKILL.md"
    if not path.is_file():
        raise CapError(f"SKILL.md is missing for {name!r}")
    text = path.read_text(encoding="utf-8")
    block = "packages: [" + ", ".join(clean) + "]"
    if "\npackages:" in text:
        out, replaced = [], False
        for line in text.splitlines():
            if line.startswith("packages:") and not replaced:
                out.append(block)
                replaced = True
            else:
                out.append(line)
        text = "\n".join(out) + ("\n" if text.endswith("\n") else "")
    else:
        head, sep, rest = text.partition("\n---")
        if not sep:
            raise CapError(f"SKILL.md for {name!r} has no frontmatter block")
        text = f"{head}\n{block}{sep}{rest}"
    path.write_text(text, encoding="utf-8")


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


def _gc_venvs(name: str, tags: list[dict[str, Any]]) -> None:
    """Drop venvs no surviving tag (or its pytest twin) can use. Best effort."""
    base = state_dir() / "venvs" / name
    if not base.is_dir():
        return
    live: set[str] = set()
    for tag in tags:
        pkgs = tag.get("packages") or []
        if isinstance(pkgs, list):
            specs = [str(p) for p in pkgs]
            live.add(requirements_hash(specs))
            live.add(requirements_hash([*specs, "pytest"]))
    for entry in sorted(base.iterdir()):
        if not entry.is_dir() or entry.name in live:
            continue
        if re.fullmatch(r"[0-9a-f]{16}", entry.name) or entry.name.endswith(".building"):
            shutil.rmtree(entry, ignore_errors=True)
            for lock in (state_dir() / "locks").glob(f"{name}-venv-{entry.name}.lock"):
                lock.unlink(missing_ok=True)
            log.info("pruned venv cap=%s path=%s", name, entry)


def _prune(state: dict[str, Any], keep: int) -> list[str]:
    """Drop the oldest tags beyond the keep limit. The active one is never dropped."""
    tags = list(state.get("tags") or [])
    active_tag = (state.get("active") or {}).get("tag")
    dropped: list[str] = []
    while len(tags) > keep:
        ordered = sorted(tags, key=lambda t: t.get("created_at") or 0)
        victim = next((t for t in ordered if t["tag"] != active_tag), None)
        if victim is None:
            break
        tags.remove(victim)
        shutil.rmtree(version_path(state["name"], victim["tag"]), ignore_errors=True)
        shutil.rmtree(data_dir(state["name"], victim["tag"], create=False), ignore_errors=True)
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
                "entrypoint": t.get("entrypoint") or "",
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
