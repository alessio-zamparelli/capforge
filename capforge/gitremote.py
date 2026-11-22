"""Bare receive/upload repositories and safe materialisation.

One bare repo per capability under the state dir. `git http-backend` serves it;
this module owns creation, configuration, the static pre-receive hook, ref
resolution, tar extraction and the processed-sha marker.
"""

from __future__ import annotations

import os
import subprocess
import tarfile
import tempfile
from pathlib import Path

from . import core

MAX_MEMBERS = 20_000
MAX_EXTRACT_BYTES = 512 * 1024 * 1024
MAX_PATH_LENGTH = 512
SKIP_PARTS = {".git", ".venv", "venv", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache"}
DEFAULT_MAX_INPUT = 64 * 1024 * 1024

HOOK = """#!/bin/sh
[ -n "$CAPFORGE_PIPELINE_LOCK" ] || exit 1
while read -r old new ref; do
  [ "$ref" = "refs/heads/main" ] || { echo "only refs/heads/main is accepted"; exit 1; }
done
flock -n "$CAPFORGE_PIPELINE_LOCK" true || { echo "a pipeline is already running"; exit 1; }
exit 0
"""


def repo_path(name: str) -> Path:
    if not core.NAME_RE.match(name or ""):
        raise core.CapError(f"Invalid capability name {name!r}.")
    return core._contained(core.state_dir() / "git", core.state_dir() / "git" / f"{name}.git")


def _git_env() -> dict[str, str]:
    env = core._base_env()
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    env["GIT_CONFIG_GLOBAL"] = "/dev/null"
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_PAGER"] = "cat"
    env["PAGER"] = "cat"
    env["GIT_TEMPLATE_DIR"] = str(core.state_dir() / "git" / "empty-template")
    return env


def _run(args: list[str], *, cwd: Path | None = None, timeout: int = 60) -> tuple[int, str]:
    try:
        proc = subprocess.Popen(
            args,
            cwd=str(cwd) if cwd else None,
            env=_git_env(),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
    except OSError as exc:
        return 1, str(exc)
    try:
        with core.track_child(proc.pid):
            out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        core._kill_group(proc)
        try:
            out, err = proc.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            out, err = "", "timed out"
        return 1, (out or err or "timed out")
    return proc.returncode or 0, ((out or "") + (err or "")).strip()


def write_hook(repo: Path) -> None:
    hook = repo / "hooks" / "pre-receive"
    hook.parent.mkdir(parents=True, exist_ok=True)
    tmp = hook.with_suffix(".tmp")
    tmp.write_text(HOOK, encoding="utf-8")
    tmp.chmod(0o755)
    tmp.replace(hook)


def init_repo(name: str) -> Path:
    """Create and configure the bare repo idempotently."""
    repo = repo_path(name)
    repo.parent.mkdir(parents=True, exist_ok=True)
    ensure_empty_template()
    (core.state_dir() / "tmp").mkdir(parents=True, exist_ok=True)
    if not (repo / "HEAD").exists():
        code, out = _run(["git", "init", "--bare", "-q", str(repo)])
        if code != 0:
            raise core.CapError(f"git init --bare failed for {name!r}: {out[-800:]}")
    max_input = str(DEFAULT_MAX_INPUT)
    try:
        max_input = str(int(os.environ.get("CAPFORGE_MAX_UPLOAD_BYTES", DEFAULT_MAX_INPUT)))
    except ValueError:
        pass
    config = {
        "receive.denyDeletes": "true",
        "receive.denyNonFastForwards": "true",
        "receive.fsckObjects": "true",
        "receive.maxInputSize": max_input,
        "core.logAllRefUpdates": "true",
        "gc.auto": "0",
        "http.receivepack": "true",
        "http.uploadpack": "true",
    }
    for key, value in config.items():
        code, out = _run(["git", "-C", str(repo), "config", key, value])
        if code != 0:
            raise core.CapError(f"git config {key} failed for {name!r}: {out[-800:]}")
    _run(["git", "-C", str(repo), "symbolic-ref", "HEAD", "refs/heads/main"])
    write_hook(repo)
    return repo


def rev_parse(name: str, ref: str = "refs/heads/main") -> str | None:
    repo = repo_path(name)
    if not repo.is_dir():
        return None
    code, out = _run(["git", "-C", str(repo), "rev-parse", "--verify", "-q", ref])
    return out if code == 0 and out else None


def publish(name: str, worktree: Path) -> str:
    """Publish a worktree's HEAD as main in the bare repo; return the sha.

    A local fetch is used instead of `git push`: it does not run receive-pack
    hooks, so scaffolding cannot be blocked by the pipeline hook it installs.
    """
    repo = init_repo(name)
    code, out = _run(
        ["git", "-C", str(repo), "fetch", "-q", str(worktree), "+HEAD:refs/heads/main"]
    )
    if code != 0:
        raise core.CapError(f"publishing {name!r} failed: {out[-1200:]}")
    _run(["git", "-C", str(repo), "symbolic-ref", "HEAD", "refs/heads/main"])
    sha = rev_parse(name)
    if sha is None:
        raise core.CapError(f"published {name!r} but main has no sha")
    return sha


def update_processed(name: str, sha: str | None) -> None:
    repo = repo_path(name)
    if not repo.is_dir():
        return
    if sha:
        _run(["git", "-C", str(repo), "update-ref", "refs/capforge/processed", sha])
    else:
        _run(["git", "-C", str(repo), "update-ref", "-d", "refs/capforge/processed"])


def commit_subject(name: str, sha: str) -> str:
    repo = repo_path(name)
    code, out = _run(["git", "-C", str(repo), "log", "-1", "--format=%s", sha])
    return out[:200] if code == 0 else ""


def _safe_member(member: tarfile.TarInfo) -> None:
    name = member.name
    if not name or name.startswith("/") or ".." in Path(name).parts:
        raise core.CapError(f"archive member escapes the tree: {name!r}")
    if len(name) > MAX_PATH_LENGTH:
        raise core.CapError(f"archive member path is too long: {name!r}")
    if any(part in SKIP_PARTS for part in Path(name).parts):
        raise core.CapError(f"archive member uses a reserved path: {name!r}")
    if not (member.isdir() or member.isfile()):
        raise core.CapError(f"archive member is not a regular file or directory: {name!r}")


def materialise(name: str, dest: Path) -> Path:
    """Extract refs/heads/main into `dest` with the staging guards applied."""
    repo = repo_path(name)
    sha = rev_parse(name)
    if sha is None:
        raise core.CapError(f"{name!r} has no refs/heads/main to materialise")
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        prefix=f"{name}-", suffix=".tar", dir=str(core.state_dir() / "tmp"), delete=False
    ) as handle:
        tar_path = Path(handle.name)
    try:
        code, out = _run(
            ["git", "-C", str(repo), "archive", "--format=tar", f"--output={tar_path}", "refs/heads/main"],
            timeout=core.run_timeout(),
        )
        if code != 0:
            raise core.CapError(f"git archive failed for {name!r}: {out[-1200:]}")
        total = 0
        members = 0
        with tarfile.open(tar_path, "r:") as archive:
            for member in archive:
                members += 1
                if members > MAX_MEMBERS:
                    raise core.CapError("archive has too many members")
                _safe_member(member)
                target = dest / member.name
                if member.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                payload = archive.extractfile(member)
                if payload is None:
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                written = 0
                with target.open("wb") as out_file:
                    while True:
                        chunk = payload.read(65536)
                        if not chunk:
                            break
                        written += len(chunk)
                        total += len(chunk)
                        if total > MAX_EXTRACT_BYTES:
                            raise core.CapError("archive expands past the extraction cap")
                        out_file.write(chunk)
                target.chmod(0o755 if member.mode & 0o111 else 0o644)
        return dest
    finally:
        tar_path.unlink(missing_ok=True)


def repo_size(name: str) -> int:
    repo = repo_path(name)
    if not repo.is_dir():
        return 0
    total = 0
    for path in repo.rglob("*"):
        try:
            if path.is_file() and not path.is_symlink():
                total += path.stat().st_size
        except OSError:
            pass
    return total


def gc(name: str, timeout: int = 60) -> None:
    repo = repo_path(name)
    if repo.is_dir():
        _run(["git", "-C", str(repo), "gc", "--quiet"], timeout=timeout)


def ensure_empty_template() -> Path:
    path = core.state_dir() / "git" / "empty-template"
    path.mkdir(parents=True, exist_ok=True)
    return path
