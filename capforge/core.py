"""Core: workspace layout, capability discovery, sandboxed execution, versioning."""

from __future__ import annotations

import ctypes
import fcntl
import hashlib
import json
import logging
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .contract import WORKSPACE_SKILL, WORKSPACE_SKILL_MARKER

log = logging.getLogger("capforge")

NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
TAG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
# A single pip requirement. Deliberately conservative: no whitespace, commas,
# URLs or leading '-' so a model-authored spec can never be read as a pip flag
# or break the `packages: [a, b]` frontmatter it is serialized into.
PKG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+*!<>=~;\[\]-]{0,199}$")
# The ASGI object name in `app:` frontmatter; it becomes half of a uvicorn
# "module:variable" argument, so it must be a plain Python identifier.
APP_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

WEB_KINDS = ("cli", "web")

DEFAULT_RUN_TIMEOUT = 120
DEFAULT_TEST_TIMEOUT = 300
DEFAULT_INSTALL_TIMEOUT = 240
DEFAULT_SERVE_BOOT_TIMEOUT = 60
DEFAULT_KEEP_TAGS = 5
MAX_PACKAGES = 50
MAX_CAPTURE_BYTES = 256 * 1024

# prctl(PR_SET_CHILD_SUBREAPER): detached grandchildren of capability calls
# re-parent to this process instead of PID 1, where the orphan sweep can find them.
PR_SET_CHILD_SUBREAPER = 36

# Environment variables a capability is allowed to inherit. Everything else is dropped,
# so a capability cannot read provider keys or the hub's own credentials. pip/install
# settings are deliberately absent: only the installer needs them.
ENV_ALLOWLIST = ("PATH", "LANG", "LC_ALL", "TZ", "TERM", "SSL_CERT_FILE", "SSL_CERT_DIR")
PIP_ENV_ALLOWLIST = ("PIP_INDEX_URL", "PIP_EXTRA_INDEX_URL")

# Keep in sync with versioning.SKIP.
SKIP_PARTS = frozenset(
    {"__pycache__", ".git", ".pytest_cache", ".venv", "venv", ".mypy_cache", ".ruff_cache"}
)


class CapError(Exception):
    """A failure that should be reported to the model verbatim, not as a traceback."""


# --------------------------------------------------------------------------- paths


def caps_dir() -> Path:
    """Folders the model edits. Defaults to the Open Terminal skills dir so Open WebUI
    lists every capability in its own per-turn skill manifest, at no context cost."""
    return Path(os.environ.get("CAPFORGE_CAPS_DIR") or (Path.home() / ".agents" / "skills")).resolve()


def state_dir() -> Path:
    return Path(os.environ.get("CAPFORGE_STATE_DIR") or (Path.home() / ".capforge")).resolve()


# Reserved: leading underscore is invalid as a capability name and skipped by discovery.
WORKSPACE_SKILL_DIR = "_capforge"


def guide_enabled() -> bool:
    raw = os.environ.get("CAPFORGE_GUIDE", "1").strip().lower()
    return raw not in {"0", "false", "no", "off"}


def ensure_workspace_skill() -> Path | None:
    """Install the generated guide skill into the caps root, unless disabled.

    The folder is reserved (leading underscore), so it never appears in
    `list_capabilities` and cannot collide with a real capability. A file without the
    managed marker is treated as user-authored and is never overwritten; deleting the
    generated file restores it on the next `serve`.
    """
    if not guide_enabled():
        return None
    path = caps_dir() / WORKSPACE_SKILL_DIR / "SKILL.md"
    try:
        if path.is_file():
            existing = path.read_text(encoding="utf-8", errors="replace")
            if WORKSPACE_SKILL_MARKER not in existing or existing == WORKSPACE_SKILL:
                return path
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(WORKSPACE_SKILL, encoding="utf-8")
        tmp.replace(path)
    except OSError:
        log.warning("could not install the workspace guide at %s", path, exc_info=True)
        return None
    log.info("installed workspace guide at %s", path)
    return path


def keep_tags() -> int:
    try:
        return max(1, int(os.environ.get("CAPFORGE_KEEP_TAGS", DEFAULT_KEEP_TAGS)))
    except ValueError:
        return DEFAULT_KEEP_TAGS


def _timeout(env_name: str, default: int) -> int:
    try:
        return max(1, int(os.environ.get(env_name, default)))
    except ValueError:
        return default


def run_timeout() -> int:
    return _timeout("CAPFORGE_RUN_TIMEOUT", DEFAULT_RUN_TIMEOUT)


def test_timeout() -> int:
    return _timeout("CAPFORGE_TEST_TIMEOUT", DEFAULT_TEST_TIMEOUT)


def install_timeout() -> int:
    return _timeout("CAPFORGE_INSTALL_TIMEOUT", DEFAULT_INSTALL_TIMEOUT)


def serve_boot_timeout() -> int:
    """Budget for a web capability to bind its port (probe and serve share it)."""
    return _timeout("CAPFORGE_SERVE_BOOT_TIMEOUT", DEFAULT_SERVE_BOOT_TIMEOUT)


def _contained(base: Path, child: Path) -> Path:
    base_r, child_r = base.resolve(), child.resolve()
    if child_r != base_r and base_r not in child_r.parents:
        raise CapError(f"Path escapes the workspace: {child}")
    return child_r


def cap_path(name: str) -> Path:
    if not NAME_RE.match(name or ""):
        raise CapError(
            f"Invalid capability name {name!r}. Use lowercase letters, digits and underscores, "
            "starting with a letter (max 64 chars)."
        )
    return _contained(caps_dir(), caps_dir() / name)


def version_path(name: str, tag: str) -> Path:
    if not NAME_RE.match(name or ""):
        raise CapError(f"Invalid capability name {name!r}.")
    if not TAG_RE.match(tag or ""):
        raise CapError(f"Invalid tag {tag!r}.")
    return _contained(state_dir() / "versions", state_dir() / "versions" / name / tag)


def validate_packages(packages: list[str] | None) -> list[str]:
    """Return clean requirement specs or raise a message the model can act on."""
    cleaned: list[str] = []
    for raw in packages or []:
        spec = str(raw).strip()
        if not spec:
            continue
        if not PKG_RE.match(spec):
            raise CapError(
                f"Invalid package requirement {raw!r}. Use a plain requirement such as "
                "'pypdf>=5' or 'httpx': no spaces, commas, URLs, quotes or leading '-'."
            )
        cleaned.append(spec)
    if len(cleaned) > MAX_PACKAGES:
        raise CapError(f"Too many packages ({len(cleaned)}); the limit is {MAX_PACKAGES}.")
    return cleaned


# ----------------------------------------------------------------------------- locks


@contextmanager
def _file_lock(path: Path) -> Iterator[None]:
    """Serialise read-modify-write cycles across processes on a local filesystem."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a+", encoding="utf-8") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        except OSError:  # pragma: no cover - filesystems without flock
            log.warning("flock unavailable on %s; continuing without a lock", path)
            yield
            return
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@contextmanager
def cap_lock(name: str) -> Iterator[None]:
    """One lock per capability, covering state and frontmatter mutations."""
    if not NAME_RE.match(name or ""):
        raise CapError(f"Invalid capability name {name!r}.")
    lock = _contained(state_dir(), state_dir() / "locks" / f"{name}.lock")
    with _file_lock(lock):
        yield


# ----------------------------------------------------------------------- frontmatter


def parse_skill_md(text: str) -> tuple[dict[str, Any], str]:
    """Split SKILL.md into (frontmatter dict, body). Tolerant: no YAML needed."""
    if not text.startswith("---"):
        return {}, text
    end = text.find("\n---", 3)
    if end == -1:
        return {}, text
    raw = text[3:end].strip("\n")
    body = text[end + 4 :].lstrip("\n")
    meta: dict[str, Any] = {}
    key: str | None = None
    for line in raw.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if line.startswith((" ", "\t", "- ")) and key:
            item = line.strip().lstrip("- ").strip().strip("'\"")
            if isinstance(meta.get(key), list):
                if item:
                    meta[key].append(item)
            else:
                meta[key] = [item] if item else []
            continue
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip()
        value = value.strip().strip("'\"")
        if value.startswith("[") and value.endswith("]"):
            meta[key] = [v.strip().strip("'\"") for v in value[1:-1].split(",") if v.strip()]
        else:
            meta[key] = value
    return meta, body


# ------------------------------------------------------------------------- manifests


@dataclass
class Capability:
    name: str
    path: Path
    description: str = ""
    entrypoint: str = "main.py"
    packages: list[str] = field(default_factory=list)
    kind: str = "cli"
    app_name: str = "app"
    body: str = ""
    error: str | None = None

    @property
    def draft_ready(self) -> bool:
        return self.error is None and (self.path / self.entrypoint).is_file()


def _load_from(name: str, path: Path) -> Capability:
    path = Path(path).resolve()
    skill = path / "SKILL.md"
    cap = Capability(name=name, path=path)
    if not skill.is_file():
        cap.error = "SKILL.md is missing"
        return cap
    meta, body = parse_skill_md(skill.read_text(encoding="utf-8", errors="replace"))
    cap.description = str(meta.get("description") or "").strip()
    raw_entry = str(meta.get("entrypoint") or "main.py").strip() or "main.py"
    cap.body = body
    if not cap.description:
        cap.error = "SKILL.md frontmatter has no description"
        return cap
    raw_pkgs = meta.get("packages") or []
    try:
        cap.packages = validate_packages(
            [str(p) for p in (raw_pkgs if isinstance(raw_pkgs, list) else [raw_pkgs]) if str(p).strip()]
        )
    except CapError as exc:
        cap.error = str(exc)
        return cap
    cap.kind = str(meta.get("kind") or "cli").strip().lower() or "cli"
    if cap.kind not in WEB_KINDS:
        cap.error = f"kind must be one of {list(WEB_KINDS)}, got {cap.kind!r}"
        return cap
    cap.app_name = str(meta.get("app") or "app").strip() or "app"
    if cap.kind == "web":
        if "uvicorn" not in cap.packages:
            cap.error = (
                "kind: web requires 'uvicorn' in packages, e.g. packages: [fastapi, uvicorn]"
            )
            return cap
        if not APP_NAME_RE.match(cap.app_name):
            cap.error = f"app must be a Python identifier, got {cap.app_name!r}"
            return cap
    try:
        entry = _contained(path, path / raw_entry)
        cap.entrypoint = entry.relative_to(path).as_posix()
    except (CapError, ValueError):
        cap.error = f"entrypoint {raw_entry!r} escapes the capability folder"
        return cap
    if cap.kind == "web":
        stem = Path(cap.entrypoint).stem
        if not APP_NAME_RE.match(stem):
            cap.error = (
                f"web entrypoint {cap.entrypoint!r} must have a valid Python module name"
            )
            return cap
    if not (path / cap.entrypoint).is_file():
        cap.error = f"entrypoint {cap.entrypoint!r} not found"
    return cap


def load_capability(name: str) -> Capability:
    path = cap_path(name)
    if not path.is_dir():
        raise CapError(
            f"No capability named {name!r} in {caps_dir()}. Use scaffold_capability to create one."
        )
    return _load_from(name, path)


def load_capability_at(name: str, path: Path) -> Capability:
    """Load metadata from a snapshot, so the active tag describes itself."""
    return _load_from(name, path)


def list_capabilities() -> list[Capability]:
    root = caps_dir()
    if not root.is_dir():
        return []
    caps: list[Capability] = []
    for entry in sorted(root.iterdir()):
        if not entry.is_dir() or entry.name.startswith((".", "_")):
            continue
        if not NAME_RE.match(entry.name):
            continue
        try:
            caps.append(load_capability(entry.name))
        except CapError:
            continue
    return caps


# ----------------------------------------------------------------------------- state


def _empty_state(name: str) -> dict[str, Any]:
    return {"name": name, "active": None, "tags": [], "pruned": []}


def _state_file(name: str) -> Path:
    if not NAME_RE.match(name or ""):
        raise CapError(f"Invalid capability name {name!r}.")
    return _contained(state_dir(), state_dir() / "state" / f"{name}.json")


def _quarantine_state(path: Path) -> None:
    dest = path.with_name(f"{path.stem}.corrupt-{time.time_ns()}{path.suffix}")
    try:
        path.replace(dest)
        log.warning("quarantined corrupt state file %s -> %s", path, dest)
    except OSError:
        log.exception("could not quarantine corrupt state file %s", path)


def read_state(name: str) -> dict[str, Any]:
    path = _state_file(name)
    if not path.is_file():
        return _empty_state(name)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        _quarantine_state(path)
        return _empty_state(name)
    if not isinstance(data, dict):
        _quarantine_state(path)
        return _empty_state(name)
    data["name"] = name
    active = data.get("active")
    if not isinstance(active, dict) or not TAG_RE.match(str(active.get("tag") or "")):
        data["active"] = None
    raw_tags = data.get("tags")
    data["tags"] = [
        tag
        for tag in (raw_tags if isinstance(raw_tags, list) else [])
        if isinstance(tag, dict) and TAG_RE.match(str(tag.get("tag") or ""))
    ]
    raw_pruned = data.get("pruned")
    data["pruned"] = [
        item
        for item in (raw_pruned if isinstance(raw_pruned, list) else [])
        if isinstance(item, dict) and isinstance(item.get("tag"), str)
    ]
    return data


def write_state(name: str, data: dict[str, Any]) -> None:
    path = _state_file(name)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


# ----------------------------------------------------------------------------- hashes


def tree_hash(root: Path) -> str:
    """Stable content hash of a capability snapshot, ignoring caches and VCS."""
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root)
        if any(part in SKIP_PARTS for part in rel.parts):
            continue
        if not path.is_file():
            continue
        digest.update(rel.as_posix().encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def requirements_hash(packages: list[str]) -> str:
    payload = "\n".join(sorted(set(packages)))
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


# ------------------------------------------------------------------------ processes


def _drain(stream: Any, sink: list[bytes]) -> None:
    """Read a pipe to EOF, keeping only the tail so a runaway child cannot exhaust RAM."""
    total = 0
    while True:
        chunk = stream.read(65536)
        if not chunk:
            break
        sink.append(chunk)
        total += len(chunk)
        while total > MAX_CAPTURE_BYTES and len(sink) > 1:
            total -= len(sink.pop(0))


def _write_stdin(stream: Any, data: bytes) -> None:
    """Write stdin from a thread so a large payload cannot deadlock against stdout."""
    try:
        stream.write(data)
        stream.close()
    except (BrokenPipeError, OSError):
        pass


def _kill_group(proc: subprocess.Popen) -> None:
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except OSError:
        try:
            proc.kill()
        except OSError:
            pass


# Direct children of this process that must survive a stray sweep: in-flight
# call children and long-lived web instances. Sweeps never touch them.
_live_children: set[int] = set()
_owned_children: set[int] = set()
_child_lock = threading.Lock()
_subreaper_set = False


def register_owned(pid: int) -> None:
    """Mark a long-lived child (a served web instance) as wanted, not stray."""
    with _child_lock:
        _owned_children.add(pid)


def unregister_owned(pid: int) -> None:
    with _child_lock:
        _owned_children.discard(pid)


def set_subreaper() -> bool:
    """Adopt detached grandchildren so the stray sweep can find them.

    Without this, a daemon that double-forks inside a capability call re-parents
    to PID 1 and becomes invisible to any parent-based accounting. Nearest
    subreaper wins, so this works in containers where PID 1 is someone else, too.
    """
    global _subreaper_set
    if _subreaper_set:
        return True
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        if libc.prctl(PR_SET_CHILD_SUBREAPER, 1, 0, 0, 0) != 0:
            log.warning("could not set the subreaper flag: %s", ctypes.get_errno())
            return False
    except OSError:
        log.warning("prctl unavailable; detached processes will not be swept", exc_info=True)
        return False
    _subreaper_set = True
    return True


def _read_ppid(stat: Path) -> tuple[int, str] | None:
    try:
        text = stat.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    close = text.rfind(")")
    if close == -1:
        return None
    fields = text[close + 2 :].split()
    if len(fields) < 2:
        return None
    try:
        return int(fields[1]), fields[0]
    except ValueError:
        return None


def _reap_zombie(pid: int) -> None:
    """Reap one known zombie child. Specific-pid waits avoid stealing the exit
    status of a live child another thread is blocked on."""
    try:
        os.waitpid(pid, os.WNOHANG)
    except (ChildProcessError, OSError):
        pass


def sweep_stray_children() -> list[int]:
    """Kill processes re-parented to this process that nothing owns.

    A capability call must leave nothing running behind it: any descendant that
    outlived its call is either a naive fork (already killed with its group) or a
    detached daemon, which shows up here as an adopted child. Served web
    instances are registered as owned and are exempt. Best effort and quiet.
    """
    killed: list[int] = []
    zombies: list[int] = []
    me = os.getpid()
    try:
        entries = list(Path("/proc").iterdir())
    except OSError:
        return killed
    for entry in entries:
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        if pid == me:
            continue
        parsed = _read_ppid(entry / "stat")
        if parsed is None or parsed[0] != me:
            continue
        if parsed[1] == "Z":
            zombies.append(pid)
            continue
        with _child_lock:
            owned = pid in _owned_children or pid in _live_children
        if owned:
            continue
        try:
            os.kill(pid, signal.SIGKILL)
            killed.append(pid)
            log.warning("killed stray process pid=%d re-parented by a capability call", pid)
        except OSError:
            pass
    for pid in zombies:
        _reap_zombie(pid)
    return killed


def _run_process(
    cmd: list[str],
    *,
    cwd: Path | None,
    env: dict[str, str],
    payload: str | None,
    timeout: int,
) -> tuple[int | None, str, str, bool]:
    """Run one child in its own process group and capture a bounded tail of its output."""
    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE if payload is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=str(cwd) if cwd is not None else None,
        env=env,
        start_new_session=True,
    )
    with _child_lock:
        _live_children.add(proc.pid)
    out_sink: list[bytes] = []
    err_sink: list[bytes] = []
    readers = [
        threading.Thread(target=_drain, args=(proc.stdout, out_sink), daemon=True),
        threading.Thread(target=_drain, args=(proc.stderr, err_sink), daemon=True),
    ]
    for reader in readers:
        reader.start()
    writer: threading.Thread | None = None
    if payload is not None and proc.stdin is not None:
        writer = threading.Thread(
            target=_write_stdin, args=(proc.stdin, payload.encode("utf-8")), daemon=True
        )
        writer.start()
    timed_out = False
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
    finally:
        # Nothing survives a call: the group dies on timeout, and also after a
        # normal exit, so a capability cannot leave in-group children running.
        # Detached (setsid'd) descendants escape the group; the stray sweep
        # catches those after re-parenting.
        _kill_group(proc)
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass
        with _child_lock:
            _live_children.discard(proc.pid)
    if writer is not None:
        writer.join(timeout=5)
    for reader in readers:
        reader.join(timeout=5)
    for stream in (proc.stdout, proc.stderr):
        if stream is not None:
            try:
                stream.close()
            except OSError:
                pass
    stdout = b"".join(out_sink).decode("utf-8", errors="replace")
    stderr = b"".join(err_sink).decode("utf-8", errors="replace")
    return proc.returncode, stdout, stderr, timed_out


# --------------------------------------------------------------------------- venvs


def _venv_for(name: str, packages: list[str]) -> Path:
    base = _contained(state_dir(), state_dir() / "venvs" / name)
    return base / requirements_hash(packages)


def _venv_lock(name: str, packages: list[str]) -> Path:
    digest = requirements_hash(packages)
    return _contained(state_dir(), state_dir() / "locks" / f"{name}-venv-{digest}.lock")


def _base_env() -> dict[str, str]:
    env = {k: os.environ[k] for k in ENV_ALLOWLIST if k in os.environ}
    env.setdefault("PATH", "/usr/local/bin:/usr/bin:/bin")
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["HOME"] = os.environ.get("HOME", "/tmp")
    env.pop("PYTHONPATH", None)
    env.pop("PYTHONHOME", None)
    return env


def _install_env() -> dict[str, str]:
    """The installer may see pip index settings; capability code never does."""
    env = _base_env()
    env.update({k: os.environ[k] for k in PIP_ENV_ALLOWLIST if k in os.environ})
    return env


def ensure_venv(name: str, packages: list[str], notes: list[str] | None = None) -> Path:
    """Return a venv holding exactly `packages`. Reused when the requirement set is unchanged."""
    clean = sorted(set(validate_packages(packages)))
    venv = _venv_for(name, clean)
    python = venv / "bin" / "python"
    if python.is_file():
        return venv
    with _file_lock(_venv_lock(name, clean)):
        if python.is_file():
            return venv
        venv.parent.mkdir(parents=True, exist_ok=True)
        tmp = venv.with_name(venv.name + ".building")
        shutil.rmtree(tmp, ignore_errors=True)
        # pip is only needed when there is something to install, so the stdlib-only path skips it.
        with_pip = bool(clean)
        code, _, stderr, timed_out = _run_process(
            [sys.executable, "-m", "venv", *([] if with_pip else ["--without-pip"]), str(tmp)],
            cwd=None,
            env=_install_env(),
            payload=None,
            timeout=install_timeout(),
        )
        if timed_out or code != 0:
            shutil.rmtree(tmp, ignore_errors=True)
            reason = "timed out" if timed_out else (stderr.strip()[-800:] or f"exit {code}")
            raise CapError(f"Could not create a virtualenv: {reason}")
        if with_pip:
            code, _, stderr, timed_out = _run_process(
                [
                    str(tmp / "bin" / "python"),
                    "-m",
                    "pip",
                    "install",
                    "--quiet",
                    "--disable-pip-version-check",
                    *clean,
                ],
                cwd=None,
                env=_install_env(),
                payload=None,
                timeout=install_timeout(),
            )
            if notes is not None:
                notes.append(f"pip install {' '.join(clean)} -> exit {code}")
            if timed_out:
                shutil.rmtree(tmp, ignore_errors=True)
                raise CapError(f"Installing {clean} timed out after {install_timeout()}s")
            if code != 0:
                shutil.rmtree(tmp, ignore_errors=True)
                raise CapError(f"Installing {clean} failed:\n{stderr.strip()[-2000:]}")
        if venv.exists():
            shutil.rmtree(venv, ignore_errors=True)
        tmp.replace(venv)
        log.info("built venv cap=%s requirements=%s path=%s", name, clean, venv)
    return venv


# ------------------------------------------------------------------------ execution


def data_dir(name: str, tag: str | None, *, create: bool = True) -> Path:
    safe_tag = re.sub(r"[^A-Za-z0-9._-]", "_", tag or "draft")
    path = _contained(state_dir(), state_dir() / "data" / name / safe_tag)
    if create:
        path.mkdir(parents=True, exist_ok=True)
    return path


@dataclass
class ExecResult:
    ok: bool
    exit_code: int | None
    stdout: str
    stderr: str
    duration_ms: int
    timed_out: bool = False
    result: Any = None

    def as_dict(self) -> dict[str, Any]:
        out = {
            "ok": self.ok,
            "exit_code": self.exit_code,
            "duration_ms": self.duration_ms,
            "stdout": self.stdout[-4000:],
            "stderr": self.stderr[-4000:],
        }
        if self.timed_out:
            out["timed_out"] = True
        if self.result is not None:
            out["result"] = self.result
        return out


def _parse_output(stdout: str) -> tuple[Any, str | None]:
    """Parse the whole output first, then the last non-empty line (the documented contract)."""
    text = stdout.strip()
    if not text:
        return None, None
    try:
        return json.loads(text), None
    except ValueError as exc:
        last = next((line.strip() for line in reversed(text.splitlines()) if line.strip()), "")
        if last and last != text:
            try:
                return json.loads(last), None
            except ValueError:
                pass
        return None, str(exc)


def execute(
    cap: Capability,
    source: Path,
    args: dict[str, Any] | None,
    *,
    tag: str | None,
    packages: list[str] | None = None,
    timeout: int | None = None,
    data_tag: str | None = None,
) -> ExecResult:
    """Run one capability entrypoint in a subprocess: JSON in on stdin, JSON out on stdout."""
    if cap.kind == "web":
        raise CapError(
            f"{cap.name!r} is a web capability (kind: web): it has no JSON entrypoint. "
            "Use serve_capability(name, action='start') to run it as a server."
        )
    entry = _contained(source, source / cap.entrypoint)
    if not entry.is_file():
        raise CapError(f"Entrypoint {cap.entrypoint!r} is missing from {source}")
    venv = ensure_venv(cap.name, packages if packages is not None else cap.packages)
    env = _base_env()
    env["CAPFORGE_CAP"] = cap.name
    env["CAPFORGE_VERSION"] = tag or "draft"
    env["CAPFORGE_DATA_DIR"] = str(data_dir(cap.name, data_tag if data_tag is not None else tag))
    env["VIRTUAL_ENV"] = str(venv)
    env["PATH"] = f"{venv / 'bin'}:{env['PATH']}"

    payload = json.dumps(args if isinstance(args, dict) else {}, ensure_ascii=False)
    started = time.monotonic()
    code, stdout, stderr, timed_out = _run_process(
        [str(venv / "bin" / "python"), str(entry)],
        cwd=source,
        env=env,
        payload=payload,
        timeout=timeout or run_timeout(),
    )
    sweep_stray_children()
    duration = int((time.monotonic() - started) * 1000)
    if timed_out:
        log.info(
            "run cap=%s version=%s ok=false duration_ms=%d timed_out=true",
            cap.name,
            tag or "draft",
            duration,
        )
        return ExecResult(
            ok=False,
            exit_code=None,
            stdout=stdout,
            stderr=f"Timed out after {timeout or run_timeout()}s",
            duration_ms=duration,
            timed_out=True,
        )
    parsed, parse_error = _parse_output(stdout)
    ok = code == 0 and parse_error is None
    if parse_error:
        stderr = f"{stderr}\nstdout is not valid JSON: {parse_error}".strip()
    log.info(
        "run cap=%s version=%s ok=%s exit=%s duration_ms=%d",
        cap.name,
        tag or "draft",
        ok,
        code,
        duration,
    )
    return ExecResult(
        ok=ok,
        exit_code=code,
        stdout=stdout,
        stderr=stderr,
        duration_ms=duration,
        result=parsed,
    )


def run_tests(
    cap: Capability,
    source: Path,
    packages: list[str] | None = None,
    timeout: int | None = None,
) -> ExecResult:
    """Run the capability's own test suite, or a real smoke run when it has none.

    Web capabilities are gated by a real boot + HTTP probe instead of the JSON
    smoke path, in addition to pytest when tests exist: a promotion must prove
    the ASGI app actually starts.
    """
    tests = source / "tests"
    has_tests = tests.is_dir() and any(tests.rglob("test_*.py"))
    if cap.kind == "web":
        if has_tests:
            result = _run_pytest(cap, source, packages, timeout)
            if not result.ok:
                return result
        return probe_web_boot(cap, source, packages=packages)
    if not has_tests:
        # Same execution path as a real call: invalid stdout must fail the gate.
        return execute(
            cap,
            source,
            {},
            tag=None,
            packages=packages,
            timeout=timeout or test_timeout(),
            data_tag="test",
        )
    return _run_pytest(cap, source, packages, timeout)


def _run_pytest(
    cap: Capability,
    source: Path,
    packages: list[str] | None,
    timeout: int | None,
) -> ExecResult:
    """Run the draft's pytest suite in a venv with pytest added."""
    tests = source / "tests"
    venv = ensure_venv(
        cap.name, list(packages if packages is not None else cap.packages) + ["pytest"]
    )
    env = _base_env()
    env["CAPFORGE_CAP"] = cap.name
    env["CAPFORGE_VERSION"] = "draft"
    env["CAPFORGE_DATA_DIR"] = str(data_dir(cap.name, "test"))
    env["VIRTUAL_ENV"] = str(venv)
    env["PATH"] = f"{venv / 'bin'}:{env['PATH']}"
    started = time.monotonic()
    code, stdout, stderr, timed_out = _run_process(
        [
            str(venv / "bin" / "python"),
            "-m",
            "pytest",
            "-q",
            "--no-header",
            "-p",
            "no:cacheprovider",
            str(tests),
        ],
        cwd=source,
        env=env,
        payload=None,
        timeout=timeout or test_timeout(),
    )
    duration = int((time.monotonic() - started) * 1000)
    if timed_out:
        return ExecResult(
            ok=False,
            exit_code=None,
            stdout=stdout,
            stderr=f"Tests timed out after {timeout or test_timeout()}s",
            duration_ms=duration,
            timed_out=True,
        )
    return ExecResult(
        ok=code == 0,
        exit_code=code,
        stdout=stdout,
        stderr=stderr,
        duration_ms=duration,
    )


# ------------------------------------------------------------------ web boot probe


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _http_probe(port: int, deadline: float) -> tuple[bool, str]:
    """Raw-socket GET /, retried until the deadline. ANY response proves the
    ASGI app booted; `/health` is a scaffold convention, not contractual."""
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False, "no HTTP response before the boot budget expired"
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=min(2.0, remaining)) as conn:
                conn.settimeout(min(2.0, remaining))
                conn.sendall(b"GET / HTTP/1.0\r\nHost: localhost\r\n\r\n")
                data = conn.recv(4096)
                if data:
                    head = data.decode("latin-1", errors="replace").splitlines()[0][:120]
                    return True, head
        except OSError:
            pass
        time.sleep(0.1)


def web_child_argv(cap: Capability, source: Path, port: int) -> list[str]:
    """uvicorn arguments for a web capability's ASGI app. `--app-dir` keeps
    nested entrypoints importable; children always bind 127.0.0.1."""
    module = Path(cap.entrypoint).stem
    app_dir = (source / cap.entrypoint).parent
    return [
        "-m",
        "uvicorn",
        f"{module}:{cap.app_name}",
        "--host",
        "127.0.0.1",
        "--port",
        str(port),
        "--app-dir",
        str(app_dir),
        "--log-level",
        "info",
    ]


def web_child_env(
    cap: Capability,
    *,
    tag: str | None,
    venv: Path,
    port: int,
    data_tag: str | None = None,
    public_url: str | None = None,
) -> dict[str, str]:
    """Allowlisted environment for a web child: same rules as execute(), plus
    the serve-specific variables the app may read."""
    env = _base_env()
    env["CAPFORGE_CAP"] = cap.name
    env["CAPFORGE_VERSION"] = tag or "draft"
    env["CAPFORGE_DATA_DIR"] = str(data_dir(cap.name, data_tag if data_tag is not None else tag))
    env["CAPFORGE_SERVE_PORT"] = str(port)
    env["VIRTUAL_ENV"] = str(venv)
    env["PATH"] = f"{venv / 'bin'}:{env['PATH']}"
    if public_url:
        env["CAPFORGE_PUBLIC_URL"] = public_url
    return env


def probe_web_boot(
    cap: Capability,
    source: Path,
    *,
    packages: list[str] | None = None,
    timeout: int | None = None,
) -> ExecResult:
    """Boot the ASGI app once on a throwaway port and probe it over HTTP.

    Used as the promotion gate for kind: web. The child is killed before
    returning; anything that detached itself is left to the stray sweep."""
    entry = _contained(source, source / cap.entrypoint)
    if not entry.is_file():
        raise CapError(f"Entrypoint {cap.entrypoint!r} is missing from {source}")
    venv = ensure_venv(cap.name, packages if packages is not None else cap.packages)
    port = _free_port()
    env = web_child_env(cap, tag=None, venv=venv, port=port, data_tag="test")
    cmd = [str(venv / "bin" / "python"), *web_child_argv(cap, source, port)]
    started = time.monotonic()
    budget = timeout or serve_boot_timeout()
    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        cwd=str(source),
        env=env,
        start_new_session=True,
    )
    with _child_lock:
        _live_children.add(proc.pid)
    sink: list[bytes] = []
    reader = threading.Thread(target=_drain, args=(proc.stdout, sink), daemon=True)
    reader.start()
    try:
        up, detail = _http_probe(port, time.monotonic() + budget)
    finally:
        _kill_group(proc)
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass
        reader.join(timeout=5)
        with _child_lock:
            _live_children.discard(proc.pid)
        if proc.stdout is not None:
            try:
                proc.stdout.close()
            except OSError:
                pass
        sweep_stray_children()
    duration = int((time.monotonic() - started) * 1000)
    child_log = b"".join(sink).decode("utf-8", errors="replace")
    if up:
        log.info("boot probe ok cap=%s port=%d duration_ms=%d", cap.name, port, duration)
        return ExecResult(ok=True, exit_code=0, stdout=detail, stderr=child_log, duration_ms=duration)
    log.info("boot probe failed cap=%s port=%d duration_ms=%d", cap.name, port, duration)
    return ExecResult(
        ok=False,
        exit_code=None,
        stdout="",
        stderr=f"web boot failed: {detail}\n{child_log[-2000:]}",
        duration_ms=duration,
    )
