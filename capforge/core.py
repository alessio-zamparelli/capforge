"""Core: workspace layout, capability discovery, sandboxed execution, versioning."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
TAG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
PKG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+\[\],<>=!~;: -]{0,200}$")

DEFAULT_RUN_TIMEOUT = 120
DEFAULT_TEST_TIMEOUT = 300
DEFAULT_INSTALL_TIMEOUT = 240
DEFAULT_KEEP_TAGS = 5

# Environment variables a capability is allowed to inherit. Everything else is dropped,
# so a capability cannot read provider keys or the hub's own credentials.
ENV_ALLOWLIST = ("PATH", "LANG", "LC_ALL", "TZ", "TERM", "SSL_CERT_FILE", "SSL_CERT_DIR", "PIP_INDEX_URL", "PIP_EXTRA_INDEX_URL")


class CapError(Exception):
    """A failure that should be reported to the model verbatim, not as a traceback."""


# --------------------------------------------------------------------------- paths


def caps_dir() -> Path:
    """Folders the model edits. Defaults to the Open Terminal skills dir so Open WebUI
    lists every capability in its own per-turn skill manifest, at no context cost."""
    return Path(os.environ.get("CAPFORGE_CAPS_DIR") or (Path.home() / ".agents" / "skills")).resolve()


def state_dir() -> Path:
    return Path(os.environ.get("CAPFORGE_STATE_DIR") or (Path.home() / ".capforge")).resolve()


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
    if not TAG_RE.match(tag or ""):
        raise CapError(f"Invalid tag {tag!r}.")
    return _contained(state_dir() / "versions", state_dir() / "versions" / name / tag)


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
    python: str = "python3"
    packages: list[str] = field(default_factory=list)
    body: str = ""
    error: str | None = None

    @property
    def draft_ready(self) -> bool:
        return self.error is None and (self.path / self.entrypoint).is_file()


def load_capability(name: str) -> Capability:
    path = cap_path(name)
    skill = path / "SKILL.md"
    if not path.is_dir():
        raise CapError(f"No capability named {name!r} in {caps_dir()}. Use scaffold_capability to create one.")
    cap = Capability(name=name, path=path)
    if not skill.is_file():
        cap.error = "SKILL.md is missing"
        return cap
    meta, body = parse_skill_md(skill.read_text(encoding="utf-8", errors="replace"))
    cap.description = str(meta.get("description") or "").strip()
    cap.entrypoint = str(meta.get("entrypoint") or "main.py").strip()
    cap.python = str(meta.get("python") or "python3").strip()
    pkgs = meta.get("packages") or []
    cap.packages = [str(p) for p in (pkgs if isinstance(pkgs, list) else [pkgs]) if str(p).strip()]
    cap.body = body
    if not cap.description:
        cap.error = "SKILL.md frontmatter has no description"
    elif not (path / cap.entrypoint).is_file():
        cap.error = f"entrypoint {cap.entrypoint!r} not found"
    return cap


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


def _state_file(name: str) -> Path:
    return _contained(state_dir(), state_dir() / "state" / f"{name}.json")


def read_state(name: str) -> dict[str, Any]:
    path = _state_file(name)
    if not path.is_file():
        return {"name": name, "active": None, "tags": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"name": name, "active": None, "tags": []}
    data.setdefault("name", name)
    data.setdefault("active", None)
    data.setdefault("tags", [])
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
    skip = {"__pycache__", ".git", ".pytest_cache", ".venv", "venv", ".mypy_cache", ".ruff_cache"}
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root)
        if any(part in skip for part in rel.parts):
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


# --------------------------------------------------------------------------- venvs


def _venv_for(name: str, packages: list[str]) -> Path:
    base = _contained(state_dir(), state_dir() / "venvs" / name)
    return base / requirements_hash(packages)


def ensure_venv(name: str, packages: list[str], log: list[str] | None = None) -> Path:
    """Return a venv holding exactly `packages`. Reused when the requirement set is unchanged."""
    venv = _venv_for(name, packages)
    python = venv / "bin" / "python"
    if python.is_file():
        return venv
    venv.parent.mkdir(parents=True, exist_ok=True)
    tmp = venv.with_name(venv.name + ".building")
    shutil.rmtree(tmp, ignore_errors=True)
    # pip is only needed when there is something to install, so the stdlib-only path skips it.
    with_pip = bool(packages)
    proc = subprocess.run(
        [sys.executable, "-m", "venv", *( [] if with_pip else ["--without-pip"] ), str(tmp)],
        capture_output=True, text=True, timeout=install_timeout(), env=_base_env(),
    )
    if proc.returncode != 0:
        shutil.rmtree(tmp, ignore_errors=True)
        raise CapError(f"Could not create a virtualenv: {proc.stderr.strip()[-800:]}")
    if with_pip:
        pip = [
            str(tmp / "bin" / "python"), "-m", "pip", "install",
            "--quiet", "--disable-pip-version-check", *packages,
        ]
        try:
            proc = subprocess.run(pip, capture_output=True, text=True, timeout=install_timeout(), env=_base_env())
        except subprocess.TimeoutExpired:
            shutil.rmtree(tmp, ignore_errors=True)
            raise CapError(f"Installing {packages} timed out after {install_timeout()}s") from None
        if log is not None:
            log.append(f"pip install {' '.join(packages)} -> exit {proc.returncode}")
        if proc.returncode != 0:
            shutil.rmtree(tmp, ignore_errors=True)
            raise CapError(f"Installing {packages} failed:\n{proc.stderr.strip()[-2000:]}")
    tmp.replace(venv)
    return venv


# ------------------------------------------------------------------------ execution


def _base_env() -> dict[str, str]:
    env = {k: os.environ[k] for k in ENV_ALLOWLIST if k in os.environ}
    env.setdefault("PATH", "/usr/local/bin:/usr/bin:/bin")
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["HOME"] = os.environ.get("HOME", "/tmp")
    env.pop("PYTHONPATH", None)
    env.pop("PYTHONHOME", None)
    return env


def data_dir(name: str, tag: str | None) -> Path:
    safe_tag = re.sub(r"[^A-Za-z0-9._-]", "_", tag or "draft")
    path = _contained(state_dir(), state_dir() / "data" / name / safe_tag)
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


def execute(
    cap: Capability,
    source: Path,
    args: dict[str, Any] | None,
    *,
    tag: str | None,
    packages: list[str] | None = None,
    timeout: int | None = None,
) -> ExecResult:
    """Run one capability entrypoint in a subprocess: JSON in on stdin, JSON out on stdout."""
    entry = source / cap.entrypoint
    if not entry.is_file():
        raise CapError(f"Entrypoint {cap.entrypoint!r} is missing from {source}")
    venv = ensure_venv(cap.name, packages if packages is not None else cap.packages)
    env = _base_env()
    env["CAPFORGE_CAP"] = cap.name
    env["CAPFORGE_VERSION"] = tag or "draft"
    env["CAPFORGE_DATA_DIR"] = str(data_dir(cap.name, tag))
    env["VIRTUAL_ENV"] = str(venv)
    env["PATH"] = f"{venv / 'bin'}:{env['PATH']}"

    payload = json.dumps(args if isinstance(args, dict) else {}, ensure_ascii=False)
    started = time.monotonic()
    try:
        proc = subprocess.run(
            [str(venv / "bin" / "python"), str(entry)],
            input=payload,
            capture_output=True,
            text=True,
            timeout=timeout or run_timeout(),
            cwd=str(source),
            env=env,
        )
    except subprocess.TimeoutExpired:
        return ExecResult(
            ok=False, exit_code=None, stdout="", stderr=f"Timed out after {timeout or run_timeout()}s",
            duration_ms=int((time.monotonic() - started) * 1000), timed_out=True,
        )
    duration = int((time.monotonic() - started) * 1000)
    parsed: Any = None
    parse_error = None
    if proc.returncode == 0:
        text = proc.stdout.strip()
        if text:
            try:
                parsed = json.loads(text)
            except ValueError as exc:
                parse_error = str(exc)
    ok = proc.returncode == 0 and parse_error is None
    stderr = proc.stderr
    if parse_error:
        stderr = f"{stderr}\nstdout is not valid JSON: {parse_error}".strip()
    return ExecResult(
        ok=ok, exit_code=proc.returncode, stdout=proc.stdout, stderr=stderr,
        duration_ms=duration, result=parsed,
    )


def run_tests(cap: Capability, source: Path, packages: list[str] | None = None, timeout: int | None = None) -> ExecResult:
    """Run the capability's own test suite, or a smoke run when it has none."""
    tests = source / "tests"
    venv = ensure_venv(cap.name, list(packages if packages is not None else cap.packages) + ["pytest"])
    env = _base_env()
    env["CAPFORGE_CAP"] = cap.name
    env["CAPFORGE_DATA_DIR"] = str(data_dir(cap.name, "test"))
    env["VIRTUAL_ENV"] = str(venv)
    env["PATH"] = f"{venv / 'bin'}:{env['PATH']}"
    started = time.monotonic()
    if tests.is_dir() and any(tests.rglob("test_*.py")):
        cmd = [str(venv / "bin" / "python"), "-m", "pytest", "-q", "--no-header", "-p", "no:cacheprovider", str(tests)]
    else:
        cmd = [str(venv / "bin" / "python"), str(source / cap.entrypoint)]
    try:
        proc = subprocess.run(
            cmd, input="{}", capture_output=True, text=True,
            timeout=timeout or test_timeout(), cwd=str(source), env=env,
        )
    except subprocess.TimeoutExpired:
        return ExecResult(
            ok=False, exit_code=None, stdout="", stderr=f"Tests timed out after {timeout or test_timeout()}s",
            duration_ms=int((time.monotonic() - started) * 1000), timed_out=True,
        )
    return ExecResult(
        ok=proc.returncode == 0, exit_code=proc.returncode,
        stdout=proc.stdout, stderr=proc.stderr,
        duration_ms=int((time.monotonic() - started) * 1000),
    )
