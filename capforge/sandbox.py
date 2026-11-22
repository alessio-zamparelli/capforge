"""Landlock filesystem confinement for capability and tool children.

Landlock is an unprivileged Linux LSM: a process restricts itself, the rules are
inherited across fork/exec and cannot be relaxed. This module builds a ruleset from
an explicit allowlist, and provides a tiny exec wrapper so a threaded server never
needs `preexec_fn` (which is unsafe once other threads exist).

Policy is deliberately path-based and conservative: read-only system/venv/code,
read-write for the exact capability data directories and a private temp directory,
everything else (state, tokens, other capabilities, home, /proc, /sys) denied.
"""

from __future__ import annotations

import ctypes
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# x86_64 and arm64 use the same generic numbers for these three syscalls.
NR_LANDLOCK_CREATE_RULESET = 444
NR_LANDLOCK_ADD_RULE = 445
NR_LANDLOCK_RESTRICT_SELF = 446

LANDLOCK_CREATE_RULESET_VERSION = 1
LANDLOCK_RULE_PATH_BENEATH = 1

ACCESS_EXECUTE = 1 << 0
ACCESS_WRITE_FILE = 1 << 1
ACCESS_READ_FILE = 1 << 2
ACCESS_READ_DIR = 1 << 3
ACCESS_REMOVE_DIR = 1 << 4
ACCESS_REMOVE_FILE = 1 << 5
ACCESS_MAKE_CHAR = 1 << 6
ACCESS_MAKE_DIR = 1 << 7
ACCESS_MAKE_REG = 1 << 8
ACCESS_MAKE_SOCK = 1 << 9
ACCESS_MAKE_FIFO = 1 << 10
ACCESS_MAKE_BLOCK = 1 << 11
ACCESS_MAKE_SYM = 1 << 12
ACCESS_REFER = 1 << 13
ACCESS_TRUNCATE = 1 << 14
ACCESS_IOCTL_DEV = 1 << 15

READ_ONLY = ACCESS_EXECUTE | ACCESS_READ_FILE | ACCESS_READ_DIR
READ_WRITE = (
    ACCESS_EXECUTE
    | ACCESS_WRITE_FILE
    | ACCESS_READ_FILE
    | ACCESS_READ_DIR
    | ACCESS_REMOVE_DIR
    | ACCESS_REMOVE_FILE
    | ACCESS_MAKE_CHAR
    | ACCESS_MAKE_DIR
    | ACCESS_MAKE_REG
    | ACCESS_MAKE_SOCK
    | ACCESS_MAKE_FIFO
    | ACCESS_MAKE_BLOCK
    | ACCESS_MAKE_SYM
    | ACCESS_REFER
    | ACCESS_TRUNCATE
)

PR_SET_NO_NEW_PRIVS = 38

_libc = ctypes.CDLL(None, use_errno=True)
_abi: int | None | bool = False


class SandboxError(RuntimeError):
    """Landlock is unavailable or a rule could not be applied."""


@dataclass(frozen=True)
class Policy:
    """An explicit path allowlist. All paths are resolved before use."""

    read: tuple[Path, ...] = ()
    write: tuple[Path, ...] = ()
    read_files: tuple[Path, ...] = ()
    write_files: tuple[Path, ...] = ()

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Policy:
        return cls(
            read=tuple(Path(p) for p in data.get("read", [])),
            write=tuple(Path(p) for p in data.get("write", [])),
            read_files=tuple(Path(p) for p in data.get("read_files", [])),
            write_files=tuple(Path(p) for p in data.get("write_files", [])),
        )

    def as_dict(self) -> dict[str, list[str]]:
        return {
            "read": [str(p) for p in self.read],
            "write": [str(p) for p in self.write],
            "read_files": [str(p) for p in self.read_files],
            "write_files": [str(p) for p in self.write_files],
        }


class _RulesetAttr(ctypes.Structure):
    _fields_ = [("handled_access_fs", ctypes.c_uint64)]


class _PathBeneathAttr(ctypes.Structure):
    _pack_ = 1
    _layout_ = "ms"
    _fields_ = [("allowed_access", ctypes.c_uint64), ("parent_fd", ctypes.c_int32)]


def abi_version() -> int | None:
    """Return the Landlock ABI version, or None when the kernel lacks it."""
    global _abi
    if _abi is not False:
        return _abi
    result = _libc.syscall(
        NR_LANDLOCK_CREATE_RULESET, None, ctypes.c_size_t(0), ctypes.c_uint32(
            LANDLOCK_CREATE_RULESET_VERSION
        )
    )
    if result < 0:
        _abi = None
        return None
    _abi = int(result)
    return _abi


def _handled(abi: int) -> int:
    rights = READ_WRITE
    if abi >= 2:
        rights |= ACCESS_REFER
    else:
        rights &= ~ACCESS_REFER
    if abi >= 3:
        rights |= ACCESS_TRUNCATE
    else:
        rights &= ~ACCESS_TRUNCATE
    if abi >= 5:
        rights |= ACCESS_IOCTL_DEV
    else:
        rights &= ~ACCESS_IOCTL_DEV
    return rights


def _add_rule(ruleset_fd: int, path: Path, allowed: int) -> None:
    try:
        fd = os.open(path, os.O_PATH | os.O_CLOEXEC)
    except OSError:
        # A path that is absent cannot be accessed anyway; skipping is safe and makes
        # policies portable across images. The data/temp paths are created first.
        return
    try:
        if not path.is_dir():
            # Directory-only rights (READ_DIR, MAKE_*, REMOVE_*, REFER) are invalid on a
            # regular file; keep the file rights.
            allowed &= ACCESS_EXECUTE | ACCESS_READ_FILE | ACCESS_WRITE_FILE | ACCESS_TRUNCATE
        attr = _PathBeneathAttr(allowed_access=allowed, parent_fd=fd)
        result = _libc.syscall(
            NR_LANDLOCK_ADD_RULE,
            ctypes.c_int(ruleset_fd),
            ctypes.c_int(LANDLOCK_RULE_PATH_BENEATH),
            ctypes.byref(attr),
            ctypes.c_uint32(0),
        )
        if result != 0:
            raise SandboxError(
                f"landlock_add_rule({path}) failed: {os.strerror(ctypes.get_errno())}"
            )
    finally:
        os.close(fd)


def apply(policy: Policy) -> None:
    """Restrict the current process. Must run before exec of the target."""
    abi = abi_version()
    if abi is None:
        raise SandboxError("Landlock is not available on this kernel")
    handled = _handled(abi)
    attr = _RulesetAttr(handled_access_fs=handled)
    ruleset = _libc.syscall(
        NR_LANDLOCK_CREATE_RULESET,
        ctypes.byref(attr),
        ctypes.c_size_t(ctypes.sizeof(attr)),
        ctypes.c_uint32(0),
    )
    if ruleset < 0:
        raise SandboxError(f"landlock_create_ruleset failed: {os.strerror(ctypes.get_errno())}")
    try:
        for path in policy.read:
            _add_rule(ruleset, Path(path), READ_ONLY & handled)
        for path in policy.write:
            Path(path).mkdir(parents=True, exist_ok=True)
            _add_rule(ruleset, Path(path), handled)
        for path in policy.read_files:
            _add_rule(ruleset, Path(path), ACCESS_READ_FILE)
        for path in policy.write_files:
            _add_rule(ruleset, Path(path), ACCESS_READ_FILE | ACCESS_WRITE_FILE)
        if _libc.prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0:
            raise SandboxError(f"prctl(PR_SET_NO_NEW_PRIVS) failed: {os.strerror(ctypes.get_errno())}")
        if _libc.syscall(NR_LANDLOCK_RESTRICT_SELF, ctypes.c_int(ruleset), ctypes.c_uint32(0)) != 0:
            raise SandboxError(f"landlock_restrict_self failed: {os.strerror(ctypes.get_errno())}")
    finally:
        os.close(ruleset)


def wrap(cmd: list[str], policy: Policy) -> list[str]:
    """Return a command that applies `policy` and execs `cmd`.

    The helper is a fresh Python process; this avoids `preexec_fn` in the threaded
    server and keeps the restriction inherited by every descendant of the target.
    The policy travels in argv so callers cannot accidentally pass an environment
    without it; paths are not secrets.
    """
    payload = json.dumps(policy.as_dict(), separators=(",", ":"))
    return [sys.executable, "-m", "capforge.sandbox", "--policy", payload, "--", *cmd]


def _main(argv: list[str]) -> int:
    if len(argv) < 4 or argv[0] != "--policy" or argv[2] != "--":
        print("usage: python -m capforge.sandbox --policy <json> -- <command...>", file=sys.stderr)
        return 2
    try:
        policy = Policy.from_dict(json.loads(argv[1]))
        apply(policy)
    except (SandboxError, OSError, ValueError) as exc:
        print(f"capforge sandbox: {exc}", file=sys.stderr)
        return 126
    os.execvpe(argv[3], argv[3:], os.environ)
    return 127


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
