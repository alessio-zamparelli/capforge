"""MCP surface. Ten tools, stable names, no schema churn as capabilities come and go."""

from __future__ import annotations

import json
import shutil
from typing import Any

from fastmcp import FastMCP

from . import __version__
from .contract import CONTRACT, GITIGNORE, SCAFFOLD_MAIN, SCAFFOLD_SKILL, SCAFFOLD_TEST
from .core import (
    CapError,
    cap_path,
    caps_dir,
    data_dir,
    execute,
    install_timeout,
    keep_tags,
    list_capabilities as discover,
    load_capability,
    requirements_hash,
    run_tests,
    run_timeout,
    state_dir,
    test_timeout,
    tree_hash,
    ensure_venv,
)
from . import versioning

mcp = FastMCP(
    "capforge",
    instructions=(
        "Deterministic, versioned capabilities. Write a capability folder, test the draft, "
        "promote it to an immutable tag, then call it. Call get_contract first."
    ),
)


def _fail(message: str) -> dict[str, Any]:
    return {"ok": False, "error": message}


def _cap_or_error(name: str):
    try:
        return load_capability(name), None
    except CapError as exc:
        return None, _fail(str(exc))


# ------------------------------------------------------------------------ discovery


@mcp.tool
def get_contract() -> dict:
    """Read this first: the folder layout, the stdin/stdout contract, and the rules a
    capability must follow. Returns the caps root (shared with the terminal), the state
    directory, and the current limits."""
    return {
        "ok": True,
        "version": __version__,
        "caps_dir": str(caps_dir()),
        "state_dir": str(state_dir()),
        "keep_tags": keep_tags(),
        "limits": {
            "run_timeout_s": run_timeout(),
            "test_timeout_s": test_timeout(),
            "install_timeout_s": install_timeout(),
        },
        "contract": CONTRACT,
    }


@mcp.tool
def list_capabilities() -> dict:
    """List every capability in the caps root with its active tag and draft status.
    Cheap: call it instead of guessing names."""
    caps = discover()
    out = []
    for cap in caps:
        state = versioning.read_state(cap.name)
        active = state.get("active") or {}
        out.append(
            {
                "name": cap.name,
                "description": cap.description,
                "active": active.get("tag"),
                "draft_ready": cap.draft_ready,
                "draft_error": cap.error,
                "draft_changed": bool(
                    active.get("content_hash") and cap.draft_ready and active["content_hash"] != tree_hash(cap.path)
                ),
                "tags": len(state.get("tags") or []),
                "packages": cap.packages,
            }
        )
    return {"ok": True, "caps_dir": str(caps_dir()), "count": len(out), "capabilities": out}


@mcp.tool
def get_capability(name: str) -> dict:
    """Full detail for one capability: SKILL.md as written, frontmatter, entrypoint,
    packages, hashes, tags and the active version."""
    cap, err = _cap_or_error(name)
    if err:
        return err
    state = versioning.read_state(cap.name)
    return {
        "ok": True,
        "name": cap.name,
        "path": str(cap.path),
        "description": cap.description,
        "entrypoint": cap.entrypoint,
        "packages": cap.packages,
        "draft_ready": cap.draft_ready,
        "draft_error": cap.error,
        "draft_hash": tree_hash(cap.path) if cap.path.is_dir() else None,
        "skill_md": (cap.path / "SKILL.md").read_text(encoding="utf-8", errors="replace")
        if (cap.path / "SKILL.md").is_file()
        else None,
        "active": state.get("active"),
        "tags": state.get("tags") or [],
    }


# ------------------------------------------------------------------------ authoring


@mcp.tool
def scaffold_capability(
    name: str,
    description: str,
    packages: list[str] | None = None,
    overwrite: bool = False,
) -> dict:
    """Create a capability folder with SKILL.md, a working main.py and one passing test.
    Start here instead of writing files by hand: the skeleton already satisfies the contract."""
    try:
        path = cap_path(name)
    except CapError as exc:
        return _fail(str(exc))
    if path.exists() and not overwrite:
        return _fail(f"{name!r} already exists at {path}. Pass overwrite=true to replace it, or edit it in place.")
    if not description.strip():
        return _fail("description is required: it is the only thing the model sees in the manifest.")
    pkgs = [p for p in (packages or []) if str(p).strip()]
    try:
        if path.exists():
            shutil.rmtree(path)
        (path / "tests").mkdir(parents=True)
        (path / "SKILL.md").write_text(
            SCAFFOLD_SKILL.format(name=name, description=description.strip(), packages=", ".join(pkgs)),
            encoding="utf-8",
        )
        (path / "main.py").write_text(SCAFFOLD_MAIN.format(name=name), encoding="utf-8")
        (path / "tests" / "test_main.py").write_text(SCAFFOLD_TEST.format(name=name), encoding="utf-8")
        (path / ".gitignore").write_text(GITIGNORE, encoding="utf-8")
    except OSError as exc:
        return _fail(f"Could not write {path}: {exc}")
    return {
        "ok": True,
        "name": name,
        "path": str(path),
        "files": ["SKILL.md", "main.py", "tests/test_main.py"],
        "next": "Edit main.py and SKILL.md, then call test_capability(name) and promote_capability(name).",
    }


@mcp.tool
def install_packages(name: str, packages: list[str]) -> dict:
    """Install Python packages into this capability's own virtualenv and record them in
    SKILL.md. Isolated from the hub and from every other capability."""
    cap, err = _cap_or_error(name)
    if err:
        return err
    wanted = [str(p).strip() for p in (packages or []) if str(p).strip()]
    if not wanted:
        return _fail("packages is empty")
    merged = sorted({*cap.packages, *wanted})
    try:
        venv = ensure_venv(cap.name, merged)
    except CapError as exc:
        return _fail(str(exc))
    versioning._write_packages(cap.name, merged)
    reloaded = load_capability(cap.name)
    return {
        "ok": True,
        "name": cap.name,
        "installed": wanted,
        "packages": merged,
        "venv": str(venv),
        "requirements_hash": requirements_hash(merged),
        "note": "Recorded in SKILL.md. A new requirement set builds a fresh venv on next run; "
        "the old one is left in place for the versions that pinned it.",
    }


# ----------------------------------------------------------------------- validation


@mcp.tool
def test_capability(name: str, args: dict | None = None) -> dict:
    """Run the draft's test suite (pytest if tests/test_*.py exists, otherwise a smoke run).
    Optionally also run the entrypoint once with `args` and return what it produced.
    This is the pre-promotion gate: promote refuses while this fails."""
    cap, err = _cap_or_error(name)
    if err:
        return err
    if not cap.draft_ready:
        return _fail(f"Draft is not runnable: {cap.error}")

    try:
        tests = run_tests(cap, cap.path)
    except CapError as exc:
        return _fail(str(exc))

    payload: dict[str, Any] = {
        "ok": tests.ok,
        "name": cap.name,
        "draft_hash": tree_hash(cap.path)[:12],
        "tests": {
            "passed": tests.ok,
            "exit_code": tests.exit_code,
            "duration_ms": tests.duration_ms,
            "stdout": tests.stdout[-3000:],
            "stderr": tests.stderr[-3000:],
        },
    }
    if args is not None:
        try:
            run = execute(cap, cap.path, args, tag=None)
        except CapError as exc:
            payload["run"] = {"ok": False, "error": str(exc)}
        else:
            payload["run"] = run.as_dict()
            payload["ok"] = bool(payload["ok"] and run.ok)
    return payload


# ------------------------------------------------------------------------- runtime


@mcp.tool
def run_capability(name: str, args: dict | None = None) -> dict:
    """Execute the ACTIVE tagged version of a capability. Falls back to the draft only when
    nothing has been promoted yet, and says so in the result."""
    cap, err = _cap_or_error(name)
    if err:
        return err
    state = versioning.read_state(cap.name)
    active = state.get("active") or {}
    tag = active.get("tag")

    if tag:
        source = versioning.version_path(cap.name, tag)
        if not source.is_dir():
            return _fail(f"Active tag {tag!r} is recorded but its files are gone. Roll back or promote again.")
        packages = active.get("packages") or cap.packages
        mode = "active"
    else:
        if not cap.draft_ready:
            return _fail(
                f"{name!r} has never been promoted and the draft is not runnable ({cap.error}). "
                "Run test_capability and promote_capability first."
            )
        source, packages, tag, mode = cap.path, cap.packages, None, "draft"

    try:
        result = execute(cap, source, args or {}, tag=tag, packages=packages)
    except CapError as exc:
        return _fail(str(exc))

    out = result.as_dict()
    out.update(
        {
            "name": cap.name,
            "version": tag or "draft",
            "mode": mode,
            "content_hash": (active.get("content_hash") or tree_hash(cap.path))[:12],
            "data_dir": str(data_dir(cap.name, tag)),
        }
    )
    if mode == "draft":
        out["warning"] = "Running the unpromoted draft. Promote it once the tests pass."
    return out


# ------------------------------------------------------------------------ promotion


@mcp.tool
def promote_capability(
    name: str,
    tag: str | None = None,
    note: str = "",
    packages: list[str] | None = None,
    skip_tests: bool = False,
    force: bool = False,
) -> dict:
    """Snapshot the draft into an immutable tag and make it the active version.
    Runs the tests first and refuses on failure. Old tags beyond the keep limit are pruned.
    Pass force=true to tag an unchanged draft (a re-release of the same content)."""
    cap, err = _cap_or_error(name)
    if err:
        return err
    if not cap.draft_ready:
        return _fail(f"Draft is not runnable: {cap.error}")

    if not skip_tests:
        try:
            tests = run_tests(cap, cap.path, packages=packages)
        except CapError as exc:
            return _fail(f"Tests could not run: {exc}")
        if not tests.ok:
            return {
                "ok": False,
                "error": "Tests failed; promotion refused.",
                "exit_code": tests.exit_code,
                "stdout": tests.stdout[-3000:],
                "stderr": tests.stderr[-3000:],
                "hint": "Fix the code, or pass skip_tests=true with a note explaining why.",
            }

    try:
        result = versioning.promote(cap.name, tag, packages=packages, note=note, force=force)
    except CapError as exc:
        return _fail(str(exc))

    result["ok"] = True
    result["tests_skipped"] = skip_tests
    return result


@mcp.tool
def rollback_capability(name: str, tag: str) -> dict:
    """Make an older tag active again. Does not touch the working copy."""
    try:
        result = versioning.rollback(cap_path(name).name, tag)
    except CapError as exc:
        return _fail(str(exc))
    result["ok"] = True
    return result


@mcp.tool
def list_tags(name: str, diff_against_active: bool = False) -> dict:
    """Tags for a capability, newest last, with the active one marked. Set
    diff_against_active to see how the working copy differs from the active version."""
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
