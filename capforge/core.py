"""Core: workspace layout, capability discovery, sandboxed execution, versioning."""

from __future__ import annotations

import ast
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
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import sandbox

log = logging.getLogger("capforge")

NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
TAG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
# Operator-managed environment names declared by the Env model in deploy.py.
ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")
# Legacy pip requirement syntax. Kept only for parsing old frontmatter and rejecting it.
# The ASGI object name in `app:` frontmatter; it becomes half of a uvicorn
# "module:variable" argument, so it must be a plain Python identifier.
APP_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

WEB_KINDS = ("cli", "web")
DATA_KINDS = ("version", "shared")

PYTHON_VERSION = "3.14"
PYTHON_REQUIRES = ">=3.14,<3.15"

DEFAULT_RUN_TIMEOUT = 120
DEFAULT_TEST_TIMEOUT = 300
DEFAULT_INSTALL_TIMEOUT = 240
DEFAULT_SERVE_BOOT_TIMEOUT = 60
DEFAULT_KEEP_TAGS = 5
MAX_CAPTURE_BYTES = 256 * 1024
DEFAULT_SANDBOX = "require"
DEFAULT_LOG_MAX_BYTES = 1024 * 1024
DEFAULT_LOG_TAIL_BYTES = 64 * 1024

# prctl(PR_SET_CHILD_SUBREAPER): detached grandchildren of capability calls
# re-parent to this process instead of PID 1, where the orphan sweep can find them.
PR_SET_CHILD_SUBREAPER = 36

# Environment variables a capability is allowed to inherit. Everything else is dropped,
# so a capability cannot read provider keys or the hub's own credentials. pip/install
# settings are deliberately absent: only the installer needs them.
ENV_ALLOWLIST = ("PATH", "LANG", "LC_ALL", "TZ", "TERM", "SSL_CERT_FILE", "SSL_CERT_DIR")
PIP_ENV_ALLOWLIST = ("PIP_INDEX_URL", "PIP_EXTRA_INDEX_URL")

# Names that capability code may not override. They are either inherited/global, set by
# capforge for the child, or interpreted by Python/uv itself.
ENV_RESERVED = frozenset(
    {
        "HOME",
        "PATH",
        "TMPDIR",
        "VIRTUAL_ENV",
        "PYTHONUNBUFFERED",
        "PYTHONDONTWRITEBYTECODE",
        "CAPFORGE_CAP",
        "CAPFORGE_VERSION",
        "CAPFORGE_DATA_DIR",
        "CAPFORGE_SHARED_DATA_DIR",
        "CAPFORGE_SERVE_PORT",
        "CAPFORGE_PUBLIC_URL",
        "CAPFORGE_SERVE",
        *ENV_ALLOWLIST,
    }
)
ENV_RESERVED_PREFIXES = ("CAPFORGE_", "PYTHON", "LD_", "UV_")
MAX_ENV_VARS = 32
MAX_ENV_VALUE_BYTES = 16 * 1024
# Tests and the web boot probe never receive real values; declared names get this
# obvious placeholder so imports/settings objects still work without leaking secrets
# into model-visible pipeline logs.
ENV_PLACEHOLDER = "capforge-test-placeholder"
# Type-aware placeholders keep a typed pydantic Env model usable in tests/drafts.
ENV_PLACEHOLDERS = {"int": "0", "float": "0.0", "bool": "false"}

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


def sandbox_mode() -> str:
    raw = os.environ.get("CAPFORGE_SANDBOX", DEFAULT_SANDBOX).strip().lower()
    return raw if raw in {"require", "warn", "off"} else DEFAULT_SANDBOX


def log_max_bytes() -> int:
    return _timeout("CAPFORGE_LOG_MAX_BYTES", DEFAULT_LOG_MAX_BYTES)


def log_tail_bytes() -> int:
    return _timeout("CAPFORGE_LOG_TAIL_BYTES", DEFAULT_LOG_TAIL_BYTES)


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
def try_file_lock(path: Path) -> Iterator[bool]:
    """Non-blocking exclusive flock; yields False if another process holds it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a+", encoding="utf-8") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            yield False
            return
        try:
            yield True
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


# ------------------------------------------------------------- deploy.py descriptor

_DEPLOY_SCALARS = frozenset({"KIND", "ENTRYPOINT", "APP", "DATA"})
_ENV_ANNOTATIONS = {
    "str": "str",
    "SecretStr": "str",
    "HttpUrl": "str",
    "AnyUrl": "str",
    "EmailStr": "str",
    "FilePath": "str",
    "DirectoryPath": "str",
    "int": "int",
    "float": "float",
    "bool": "bool",
}
_ENV_TYPES = frozenset({"str", "int", "float", "bool"})
_MISSING = object()


@dataclass(frozen=True)
class EnvSpec:
    """One field of the pydantic Env model in deploy.py, parsed statically."""

    name: str
    type: str = "str"
    required: bool = True
    default: str | None = None
    description: str = ""
    min_length: int | None = None
    max_length: int | None = None
    ge: float | None = None
    le: float | None = None


@dataclass
class DeploySpec:
    """The deployment descriptor read from deploy.py."""

    kind: str = "cli"
    entrypoint: str = "main.py"
    app_name: str = "app"
    data: str = "version"
    env: list[EnvSpec] = field(default_factory=list)


def _node_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return ""


def _annotation_type(node: ast.expr) -> str | None:
    """Map an annotation to str/int/float/bool; None means "skip this field"."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        try:
            node = ast.parse(node.value, mode="eval").body
        except SyntaxError:
            return "str"
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
        if isinstance(node.right, ast.Constant) and node.right.value is None:
            return _annotation_type(node.left)
        if isinstance(node.left, ast.Constant) and node.left.value is None:
            return _annotation_type(node.right)
        return "str"
    if isinstance(node, ast.Subscript):
        base = _node_name(node.value)
        args = node.slice.elts if isinstance(node.slice, ast.Tuple) else [node.slice]
        if base == "Annotated":
            return _annotation_type(args[0]) if args else "str"
        if base == "Optional":
            return _annotation_type(args[0]) if args else "str"
        if base == "Union":
            values = [arg for arg in args if not (isinstance(arg, ast.Constant) and arg.value is None)]
            return _annotation_type(values[0]) if len(values) == 1 else "str"
        if base == "ClassVar":
            return None
    return _ENV_ANNOTATIONS.get(_node_name(node), "str")


def _literal_value(node: ast.AST, label: str) -> Any:
    try:
        return ast.literal_eval(node)
    except (ValueError, SyntaxError):
        raise CapError(f"deploy.py: {label} must be a literal value") from None


def _field_value(value: ast.expr | None) -> tuple[bool, Any, dict[str, Any]]:
    """Return (has_default, default, Field kwargs) for one Env annotation."""
    if value is None:
        return False, None, {}
    if isinstance(value, ast.Constant):
        return True, value.value, {}
    if isinstance(value, ast.Call) and _node_name(value.func) == "Field":
        kwargs: dict[str, Any] = {}
        for keyword in value.keywords:
            if keyword.arg is None:
                continue
            try:
                kwargs[keyword.arg] = ast.literal_eval(keyword.value)
            except (ValueError, SyntaxError):
                continue
        default: Any = _MISSING
        if value.args:
            try:
                default = ast.literal_eval(value.args[0])
            except (ValueError, SyntaxError):
                default = _MISSING
        if "default" in kwargs:
            default = kwargs["default"]
        return default is not _MISSING, (None if default is _MISSING else default), kwargs
    raise CapError("deploy.py: Env defaults must be literals or Field(...)")


def _default_text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (str, int, float)):
        return str(value)
    raise CapError("deploy.py: Env defaults must be scalar values")


def _int_or_none(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _float_or_none(value: Any) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def parse_deploy_py(text: str) -> DeploySpec:
    """Parse deploy.py with `ast`; never import capability code into the hub."""
    try:
        tree = ast.parse(text, filename="deploy.py")
    except SyntaxError as exc:
        raise CapError(f"deploy.py line {exc.lineno}: {exc.msg}") from None
    values: dict[str, Any] = {}
    env_node: ast.ClassDef | None = None
    for node in tree.body:
        if isinstance(node, ast.Assign):
            if len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
                target = node.targets[0].id
                if target in _DEPLOY_SCALARS:
                    values[target] = _literal_value(node.value, target)
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            if isinstance(node.target, ast.Name) and node.target.id in _DEPLOY_SCALARS:
                values[node.target.id] = _literal_value(node.value, node.target.id)
        elif isinstance(node, ast.ClassDef) and node.name == "Env":
            env_node = node
    if "KIND" not in values:
        raise CapError('deploy.py must define KIND = "cli" or KIND = "web"')
    kind = str(values["KIND"]).strip().lower()
    if kind not in WEB_KINDS:
        raise CapError(f"kind must be one of {list(WEB_KINDS)}, got {kind!r}")
    if "ENTRYPOINT" not in values:
        raise CapError('deploy.py must define ENTRYPOINT (for example "main.py")')
    entrypoint = str(values["ENTRYPOINT"]).strip()
    if not entrypoint:
        raise CapError("deploy.py: ENTRYPOINT must not be empty")
    app_name = str(values.get("APP") or "app").strip() or "app"
    data = str(values.get("DATA") or "version").strip().lower() or "version"
    if data not in DATA_KINDS:
        raise CapError(f"data must be one of {list(DATA_KINDS)}, got {data!r}")
    specs: list[EnvSpec] = []
    if env_node is not None:
        seen: set[str] = set()
        for stmt in env_node.body:
            if not isinstance(stmt, ast.AnnAssign) or not isinstance(stmt.target, ast.Name):
                continue
            field = stmt.target.id
            if field.startswith("_") or field == "model_config":
                continue
            type_name = _annotation_type(stmt.annotation)
            if type_name is None:
                continue
            name = valid_env_name(field)
            if name in seen:
                raise CapError(f"deploy.py: duplicate Env field {name!r}")
            seen.add(name)
            has_default, default_value, field_args = _field_value(stmt.value)
            description = field_args.get("description")
            specs.append(
                EnvSpec(
                    name=name,
                    type=type_name if type_name in _ENV_TYPES else "str",
                    required=not has_default,
                    default=_default_text(default_value) if has_default else None,
                    description=description if isinstance(description, str) else "",
                    min_length=_int_or_none(field_args.get("min_length")),
                    max_length=_int_or_none(field_args.get("max_length")),
                    ge=_float_or_none(field_args.get("ge")),
                    le=_float_or_none(field_args.get("le")),
                )
            )
    if len(specs) > MAX_ENV_VARS:
        raise CapError(f"env declares {len(specs)} variables; the limit is {MAX_ENV_VARS}.")
    return DeploySpec(kind=kind, entrypoint=entrypoint, app_name=app_name, data=data, env=specs)


# ------------------------------------------------------------------------- manifests


@dataclass
class Capability:
    name: str
    path: Path
    description: str = ""
    entrypoint: str = "main.py"
    packages: list[str] = field(default_factory=list)
    kind: str = "cli"
    data: str = "version"
    app_name: str = "app"
    env: list[str] = field(default_factory=list)
    env_specs: list[EnvSpec] = field(default_factory=list)
    body: str = ""
    error: str | None = None

    @property
    def draft_ready(self) -> bool:
        return self.error is None and (self.path / self.entrypoint).is_file()


def _dependency_names(path: Path) -> list[str]:
    """Read [project].dependencies from pyproject.toml; [] if absent/unreadable."""
    import tomllib

    project = path / "pyproject.toml"
    try:
        data = tomllib.loads(project.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    deps = data.get("project", {}).get("dependencies", [])
    if not isinstance(deps, list):
        return []
    names: list[str] = []
    for raw in deps:
        match = re.match(r"[A-Za-z0-9][A-Za-z0-9._-]*", str(raw).strip())
        if match:
            names.append(match.group(0).lower().replace("_", "-"))
    return names


def valid_env_name(name: str) -> str:
    """Validate one declared/stored environment name; refuses runtime-owned keys."""
    text = str(name).strip()
    if not ENV_NAME_RE.match(text):
        raise CapError(
            f"Invalid environment variable name {name!r}. Use letters, digits and underscores, "
            "starting with a letter or underscore (max 128 chars)."
        )
    if text in ENV_RESERVED or text.startswith(ENV_RESERVED_PREFIXES):
        raise CapError(f"Environment variable {text!r} is reserved by capforge.")
    return text


_LEGACY_FRONTMATTER = ("kind", "entrypoint", "app", "data", "env")


def _load_from(name: str, path: Path) -> Capability:
    path = Path(path).resolve()
    skill = path / "SKILL.md"
    cap = Capability(name=name, path=path)
    if not skill.is_file():
        cap.error = "SKILL.md is missing"
        return cap
    meta, body = parse_skill_md(skill.read_text(encoding="utf-8", errors="replace"))
    cap.description = str(meta.get("description") or "").strip()
    cap.body = body
    if not cap.description:
        cap.error = "SKILL.md frontmatter has no description"
        return cap
    if meta.get("packages"):
        cap.error = "packages is no longer supported; declare dependencies in pyproject.toml"
        return cap
    legacy = [key for key in _LEGACY_FRONTMATTER if meta.get(key)]
    if legacy:
        cap.error = (
            f"move {', '.join(legacy)} from SKILL.md to deploy.py; deploy.py now declares "
            "the app kind, entrypoint, data mode and environment"
        )
        return cap
    deploy = path / "deploy.py"
    if not deploy.is_file():
        cap.error = "deploy.py is missing; it declares KIND, ENTRYPOINT, DATA and Env"
        return cap
    try:
        spec = parse_deploy_py(deploy.read_text(encoding="utf-8", errors="replace"))
    except CapError as exc:
        cap.error = str(exc)
        return cap
    cap.kind = spec.kind
    cap.data = spec.data
    cap.app_name = spec.app_name
    cap.env_specs = spec.env
    cap.env = [item.name for item in spec.env]
    if cap.kind == "web":
        deps = _dependency_names(path)
        if "uvicorn" not in deps:
            cap.error = "kind: web requires 'uvicorn' in [project].dependencies"
            return cap
    if cap.kind == "web" and not APP_NAME_RE.match(cap.app_name):
        cap.error = f"app must be a Python identifier, got {cap.app_name!r}"
        return cap
    try:
        entry = _contained(path, path / spec.entrypoint)
        cap.entrypoint = entry.relative_to(path).as_posix()
    except (CapError, ValueError):
        cap.error = f"entrypoint {spec.entrypoint!r} escapes the capability folder"
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


# ---------------------------------------------------------------- capability environment


def _secrets_file(name: str) -> Path:
    if not NAME_RE.match(name or ""):
        raise CapError(f"Invalid capability name {name!r}.")
    return _contained(state_dir(), state_dir() / "secrets" / f"{name}.json")


def _quarantine_secrets(path: Path) -> None:
    dest = path.with_name(f"{path.stem}.corrupt-{time.time_ns()}{path.suffix}")
    try:
        path.replace(dest)
        log.warning("quarantined corrupt secrets file %s -> %s", path, dest)
    except OSError:
        log.exception("could not quarantine corrupt secrets file %s", path)


def read_cap_env(name: str) -> dict[str, dict[str, Any]]:
    """Operator-managed values for one capability. Never returned to the model.

    The file is per capability, not per tag, so values survive promotions. Reads
    need no lock: writers replace the file atomically.
    """
    path = _secrets_file(name)
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        _quarantine_secrets(path)
        return {}
    raw = data.get("env") if isinstance(data, dict) else None
    if not isinstance(raw, dict):
        _quarantine_secrets(path)
        return {}
    clean: dict[str, dict[str, Any]] = {}
    for key, record in raw.items():
        try:
            key = valid_env_name(key)
        except CapError:
            continue
        if not isinstance(record, dict):
            continue
        value = record.get("value")
        if not isinstance(value, str) or len(value.encode("utf-8")) > MAX_ENV_VALUE_BYTES:
            continue
        clean[key] = {"value": value, "updated_at": int(record.get("updated_at") or 0)}
    return clean


def _write_cap_env(name: str, env: dict[str, dict[str, Any]]) -> None:
    path = _secrets_file(name)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.parent.chmod(0o700)
    except OSError:
        pass
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{time.time_ns()}.tmp")
    descriptor = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump({"name": name, "env": env}, handle, indent=2, sort_keys=True)
            handle.write("\n")
        tmp.replace(path)
    except Exception:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def _env_spec_for(name: str, key: str) -> EnvSpec | None:
    """Look up the declared Env field; undeclared names are accepted as strings."""
    try:
        cap = load_capability(name)
    except CapError:
        return None
    for spec in cap.env_specs:
        if spec.name == key:
            return spec
    return None


def _validate_env_value(spec: EnvSpec | None, value: str) -> str:
    """Validate and normalise one typed value before it reaches the secrets store."""
    if spec is None:
        return value
    raw = value
    if spec.type == "int":
        try:
            parsed_int = int(raw.strip())
        except ValueError:
            raise CapError(f"{spec.name} must be an integer") from None
        if spec.ge is not None and parsed_int < spec.ge:
            raise CapError(f"{spec.name} must be at least {spec.ge:g}")
        if spec.le is not None and parsed_int > spec.le:
            raise CapError(f"{spec.name} must be at most {spec.le:g}")
        return str(parsed_int)
    if spec.type == "float":
        try:
            parsed_float = float(raw.strip())
        except ValueError:
            raise CapError(f"{spec.name} must be a number") from None
        if spec.ge is not None and parsed_float < spec.ge:
            raise CapError(f"{spec.name} must be at least {spec.ge:g}")
        if spec.le is not None and parsed_float > spec.le:
            raise CapError(f"{spec.name} must be at most {spec.le:g}")
        return str(parsed_float)
    if spec.type == "bool":
        lowered = raw.strip().lower()
        if lowered in {"true", "1", "yes", "on"}:
            return "true"
        if lowered in {"false", "0", "no", "off"}:
            return "false"
        raise CapError(f"{spec.name} must be true or false")
    if spec.min_length is not None and len(raw) < spec.min_length:
        raise CapError(f"{spec.name} must be at least {spec.min_length} characters")
    if spec.max_length is not None and len(raw) > spec.max_length:
        raise CapError(f"{spec.name} must be at most {spec.max_length} characters")
    return raw


def set_cap_env(name: str, key: str, value: str) -> dict[str, Any]:
    """Set one operator-managed value. The model has no path to this function."""
    key = valid_env_name(key)
    if not isinstance(value, str):
        raise CapError("Environment value must be a string.")
    if "\x00" in value:
        raise CapError("Environment values cannot contain NUL bytes.")
    if len(value.encode("utf-8")) > MAX_ENV_VALUE_BYTES:
        raise CapError(f"Environment value is larger than {MAX_ENV_VALUE_BYTES} bytes.")
    value = _validate_env_value(_env_spec_for(name, key), value)
    with cap_lock(name):
        env = read_cap_env(name)
        env[key] = {"value": value, "updated_at": int(time.time())}
        _write_cap_env(name, env)
    log.info("env set cap=%s name=%s", name, key)
    return {"name": key, "configured": True, "updated_at": env[key]["updated_at"]}


def delete_cap_env(name: str, key: str) -> dict[str, Any]:
    """Remove one value. Absent values are not an error."""
    key = valid_env_name(key)
    with cap_lock(name):
        env = read_cap_env(name)
        removed = env.pop(key, None) is not None
        if removed:
            _write_cap_env(name, env)
    log.info("env unset cap=%s name=%s removed=%s", name, key, removed)
    return {"name": key, "configured": False, "removed": removed}


def cap_env_values(name: str, cap: Capability) -> dict[str, str]:
    """Configured values plus declared defaults for exactly this version's fields."""
    stored = read_cap_env(name)
    values: dict[str, str] = {}
    for spec in cap.env_specs:
        if spec.name in stored:
            values[spec.name] = stored[spec.name]["value"]
        elif spec.default is not None:
            values[spec.name] = spec.default
    return values


def cap_env_missing(name: str, cap: Capability) -> list[str]:
    """Only required fields without a declared default block a run."""
    stored = read_cap_env(name)
    return [spec.name for spec in cap.env_specs if spec.required and spec.name not in stored]


def cap_env_placeholders(cap: Capability) -> dict[str, str]:
    """Non-secret values for tests/drafts: declared defaults or type-shaped placeholders."""
    values = {spec.name: spec.default for spec in cap.env_specs if spec.default is not None}
    for spec in cap.env_specs:
        if spec.name not in values:
            values[spec.name] = ENV_PLACEHOLDERS.get(spec.type, ENV_PLACEHOLDER)
    return values


def env_status(name: str, caps: Iterable[Capability]) -> list[dict[str, Any]]:
    """Names, types and configured state (never values) for the console and MCP.

    Declarations from both the draft and the active tag are included, plus stored
    names the current code no longer declares so the operator can clean them up.
    """
    stored = read_cap_env(name)
    specs: dict[str, EnvSpec] = {}
    for cap in caps:
        for spec in cap.env_specs:
            specs.setdefault(spec.name, spec)
    names = list(specs) + [key for key in sorted(stored) if key not in specs]
    return [
        {
            "name": key,
            "declared": key in specs,
            "configured": key in stored,
            "updated_at": stored.get(key, {}).get("updated_at") or 0,
            "type": specs[key].type if key in specs else "str",
            "required": specs[key].required if key in specs else True,
            "default": specs[key].default if key in specs else None,
            "description": specs[key].description if key in specs else "",
        }
        for key in names
    ]


def _apply_cap_env(env: dict[str, str], name: str, cap: Capability, *, tag: str | None) -> None:
    """Inject declared values only for an active tag; tests/drafts get placeholders."""
    if tag is None:
        env.update(cap_env_placeholders(cap))
        return
    missing = cap_env_missing(name, cap)
    if missing:
        raise CapError(
            f"{name!r} needs environment variables that only the operator can set in the "
            f"console: {', '.join(missing)}"
        )
    env.update(cap_env_values(name, cap))


# ----------------------------------------------------------------------------- hashes


def list_files(root: Path) -> list[str]:
    """Relative file list for MCP responses; stable, bounded by the caller's use."""
    root = Path(root)
    if not root.is_dir():
        return []
    out: list[str] = []
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root)
        if any(part in SKIP_PARTS for part in rel.parts):
            continue
        if path.is_file():
            out.append(rel.as_posix())
    return out


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


@contextmanager
def track_child(pid: int) -> Iterator[None]:
    """Mark a direct child as in-flight so a concurrent stray sweep leaves it alone."""
    with _child_lock:
        _live_children.add(pid)
    try:
        yield
    finally:
        with _child_lock:
            _live_children.discard(pid)


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
    """The installer may see index settings; capability code never does."""
    env = _base_env()
    env.update({k: os.environ[k] for k in PIP_ENV_ALLOWLIST if k in os.environ})
    # uv reads its own index/cache settings; expose them only to the installer.
    for key in (
        "UV_DEFAULT_INDEX",
        "UV_INDEX_URL",
        "UV_INDEX",
        "UV_CACHE_DIR",
        "UV_LINK_MODE",
        "UV_PYTHON",
        "UV_PYTHON_DOWNLOADS",
        "UV_NO_PROGRESS",
    ):
        if key in os.environ:
            env[key] = os.environ[key]
    return env


def lock_hash(source: Path) -> str:
    lock = Path(source) / "uv.lock"
    if not lock.is_file():
        raise CapError(
            f"{lock} is missing. The capability must be a uv project with a committed uv.lock."
        )
    return hashlib.sha256(lock.read_bytes()).hexdigest()[:16]


def venv_path(name: str, source: Path, group: str = "prod") -> Path:
    suffix = "-py314" if group == "prod" else "-py314-test"
    base = _contained(state_dir(), state_dir() / "venvs" / name)
    return base / f"{lock_hash(source)}{suffix}"


def _venv_lock(name: str, source: Path, group: str) -> Path:
    digest = lock_hash(source)
    return _contained(state_dir(), state_dir() / "locks" / f"{name}-venv-{digest}-{group}.lock")


def ensure_venv(
    name: str, source: Path, group: str = "prod", notes: list[str] | None = None
) -> Path:
    """Build the uv environment for one project/group, keyed by its uv.lock.

    The venv lives under the state dir and is never inside the draft or a version.
    `uv sync --frozen` is the only install path; a lock that does not match
    pyproject.toml fails the build instead of silently resolving something else.
    """
    if group not in {"prod", "test"}:
        raise CapError(f"Unknown environment group {group!r}")
    source = Path(source).resolve()
    venv = venv_path(name, source, group)
    python = venv / "bin" / "python"
    if python.is_file():
        return venv
    uv = shutil.which("uv") or "/bin/uv"
    if not Path(uv).exists():
        raise CapError("uv is not installed; the image must provide uv")
    with _file_lock(_venv_lock(name, source, group)):
        if python.is_file():
            return venv
        venv.parent.mkdir(parents=True, exist_ok=True)
        env = _install_env()
        env["UV_PROJECT_ENVIRONMENT"] = str(venv)
        env["UV_PYTHON"] = PYTHON_VERSION
        env["UV_PYTHON_DOWNLOADS"] = "never"
        env.setdefault("UV_LINK_MODE", "copy")
        env.setdefault("UV_CACHE_DIR", str(state_dir() / "cache" / "uv"))
        env.setdefault("UV_NO_PROGRESS", "1")
        args = [uv, "sync", "--frozen", "--no-install-project", "--python", PYTHON_VERSION]
        if group == "prod":
            args.append("--no-dev")
        else:
            args.extend(["--group", "dev"])
        code, _, stderr, timed_out = _run_process(
            args,
            cwd=source,
            env=env,
            payload=None,
            timeout=install_timeout(),
        )
        if notes is not None:
            notes.append(f"uv sync {group} -> exit {code}")
        if timed_out:
            shutil.rmtree(venv, ignore_errors=True)
            raise CapError(f"uv sync timed out after {install_timeout()}s")
        if code != 0:
            shutil.rmtree(venv, ignore_errors=True)
            raise CapError(f"uv sync failed:\n{stderr.strip()[-2000:]}")
        if not (venv / "bin" / "python").is_file():
            shutil.rmtree(venv, ignore_errors=True)
            raise CapError("uv sync did not create a virtualenv")
        log.info("built venv cap=%s group=%s lock=%s path=%s", name, group, lock_hash(source), venv)
    return venv


# ------------------------------------------------------------------------ execution


def data_dir(name: str, tag: str | None, *, create: bool = True) -> Path:
    safe_tag = re.sub(r"[^A-Za-z0-9._-]", "_", tag or "draft")
    path = _contained(state_dir(), state_dir() / "data" / name / safe_tag)
    if create:
        path.mkdir(parents=True, exist_ok=True)
    return path


def shared_data_dir(name: str, *, create: bool = True) -> Path:
    path = _contained(state_dir(), state_dir() / "data" / name / "shared")
    if create:
        path.mkdir(parents=True, exist_ok=True)
    return path


def _sandbox_policy(
    source: Path, venv: Path, data: Path, *, shared: Path | None = None
) -> sandbox.Policy:
    system = [Path(p) for p in ("/usr", "/usr/local", "/lib", "/lib64", "/bin", "/sbin")]
    base = Path(sys.base_prefix)
    executable = Path(sys.executable).resolve().parent
    read = [*system, base, executable, Path(venv), Path(source)]
    read_files = [
        Path("/etc/ld.so.cache"),
        Path("/etc/passwd"),
        Path("/etc/group"),
        Path("/etc/localtime"),
        Path("/dev/urandom"),
    ]
    cert = os.environ.get("SSL_CERT_FILE")
    if cert:
        read_files.append(Path(cert))
    write_files = [Path("/dev/null")]
    write = [Path(data)]
    if shared is not None:
        write.append(Path(shared))
    return sandbox.Policy(
        read=tuple(read),
        write=tuple(write),
        read_files=tuple(read_files),
        write_files=tuple(write_files),
    )


def _sandbox_command(cmd: list[str], policy: sandbox.Policy, name: str) -> list[str]:
    mode = sandbox_mode()
    if mode == "off":
        return cmd
    if sandbox.abi_version() is None:
        if mode == "require":
            raise CapError(
                "CAPFORGE_SANDBOX=require but Landlock is unavailable on this kernel"
            )
        log.warning("landlock unavailable; running cap=%s without filesystem confinement", name)
        return cmd
    return sandbox.wrap(cmd, policy)


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
    venv = ensure_venv(cap.name, source, "prod")
    data = data_dir(cap.name, data_tag if data_tag is not None else tag)
    scratch = data / ".tmp"
    scratch.mkdir(parents=True, exist_ok=True)
    shared = shared_data_dir(cap.name) if cap.data == "shared" else None
    env = _base_env()
    env["CAPFORGE_CAP"] = cap.name
    env["CAPFORGE_VERSION"] = tag or "draft"
    env["CAPFORGE_DATA_DIR"] = str(data)
    if shared is not None:
        env["CAPFORGE_SHARED_DATA_DIR"] = str(shared)
    env["TMPDIR"] = str(scratch)
    env["HOME"] = str(scratch)
    env["VIRTUAL_ENV"] = str(venv)
    env["PATH"] = f"{venv / 'bin'}:{env['PATH']}"
    _apply_cap_env(env, cap.name, cap, tag=tag)

    payload = json.dumps(args if isinstance(args, dict) else {}, ensure_ascii=False)
    started = time.monotonic()
    cmd = _sandbox_command(
        [str(venv / "bin" / "python"), str(entry)],
        _sandbox_policy(source, venv, data, shared=shared),
        cap.name,
    )
    code, stdout, stderr, timed_out = _run_process(
        cmd,
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
        pytest_result: ExecResult | None = None
        if has_tests:
            pytest_result = _run_pytest(cap, source, timeout)
            if not pytest_result.ok:
                return pytest_result
        boot = probe_web_boot(cap, source)
        if pytest_result is None:
            return boot
        combined = ExecResult(
            ok=boot.ok,
            exit_code=boot.exit_code,
            stdout=(
                "pytest:\n"
                + pytest_result.stdout
                + "\nboot probe:\n"
                + boot.stdout
            ),
            stderr=(
                ("pytest:\n" + pytest_result.stderr if pytest_result.stderr else "")
                + ("\nboot probe:\n" + boot.stderr if boot.stderr else "")
            ).strip(),
            duration_ms=pytest_result.duration_ms + boot.duration_ms,
            timed_out=boot.timed_out,
            result=boot.result,
        )
        return combined
    if not has_tests:
        # Same execution path as a real call: invalid stdout must fail the gate.
        return execute(
            cap,
            source,
            {},
            tag=None,
            timeout=timeout or test_timeout(),
            data_tag="test",
        )
    return _run_pytest(cap, source, timeout)


def _run_pytest(
    cap: Capability,
    source: Path,
    timeout: int | None,
) -> ExecResult:
    """Run the capability's pytest suite from its uv dev environment."""
    tests = source / "tests"
    venv = ensure_venv(cap.name, source, "test")
    data = data_dir(cap.name, "test")
    scratch = data / ".tmp"
    scratch.mkdir(parents=True, exist_ok=True)
    shared = shared_data_dir(cap.name) if cap.data == "shared" else None
    env = _base_env()
    env["CAPFORGE_CAP"] = cap.name
    env["CAPFORGE_VERSION"] = "test"
    env["CAPFORGE_DATA_DIR"] = str(data)
    if shared is not None:
        env["CAPFORGE_SHARED_DATA_DIR"] = str(shared)
    env["TMPDIR"] = str(scratch)
    env["HOME"] = str(scratch)
    env["COVERAGE_FILE"] = str(data / ".coverage")
    env["VIRTUAL_ENV"] = str(venv)
    env["PATH"] = f"{venv / 'bin'}:{env['PATH']}"
    # Model-authored tests and their output are visible to the model: never inject
    # real values here, only harmless placeholders for declared names.
    env.update(cap_env_placeholders(cap))
    started = time.monotonic()
    cmd = _sandbox_command(
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
        _sandbox_policy(source, venv, data, shared=shared),
        cap.name,
    )
    code, stdout, stderr, timed_out = _run_process(
        cmd,
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


def cap_root_path(name: str) -> str:
    """Public path prefix of a served capability; the proxy strips it before forwarding."""
    return f"/cap/{name}"


def web_child_argv(cap: Capability, source: Path, port: int) -> list[str]:
    """uvicorn arguments for a web capability's ASGI app. `--app-dir` keeps
    nested entrypoints importable; `--root-path` lets FastAPI build prefixed URLs
    without the deprecated `FastAPI(root_path=...)` parameter. Children always
    bind 127.0.0.1."""
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
        "--root-path",
        cap_root_path(cap.name),
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
    data = data_dir(cap.name, data_tag if data_tag is not None else tag)
    scratch = data / ".tmp"
    scratch.mkdir(parents=True, exist_ok=True)
    env["CAPFORGE_DATA_DIR"] = str(data)
    if cap.data == "shared":
        env["CAPFORGE_SHARED_DATA_DIR"] = str(shared_data_dir(cap.name))
    env["TMPDIR"] = str(scratch)
    env["HOME"] = str(scratch)
    env["CAPFORGE_SERVE_PORT"] = str(port)
    env["CAPFORGE_ROOT_PATH"] = cap_root_path(cap.name)
    env["VIRTUAL_ENV"] = str(venv)
    env["PATH"] = f"{venv / 'bin'}:{env['PATH']}"
    if public_url:
        env["CAPFORGE_PUBLIC_URL"] = public_url
    _apply_cap_env(env, cap.name, cap, tag=tag)
    return env


def probe_web_boot(
    cap: Capability,
    source: Path,
    *,
    timeout: int | None = None,
) -> ExecResult:
    """Boot the ASGI app once on a throwaway port and probe it over HTTP.

    Used as the promotion gate for kind: web. The child is killed before
    returning; anything that detached itself is left to the stray sweep."""
    entry = _contained(source, source / cap.entrypoint)
    if not entry.is_file():
        raise CapError(f"Entrypoint {cap.entrypoint!r} is missing from {source}")
    venv = ensure_venv(cap.name, source, "prod")
    port = _free_port()
    env = web_child_env(cap, tag=None, venv=venv, port=port, data_tag="test")
    data = data_dir(cap.name, "test")
    shared = shared_data_dir(cap.name) if cap.data == "shared" else None
    cmd = _sandbox_command(
        [str(venv / "bin" / "python"), *web_child_argv(cap, source, port)],
        _sandbox_policy(source, venv, data, shared=shared),
        cap.name,
    )
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
