"""MCP surface. Ten tools, stable names, no schema churn as capabilities come and go."""

from __future__ import annotations

import logging
import shutil
import time
from pathlib import Path
from typing import Any

from fastmcp import FastMCP

from . import __version__, versioning
from .contract import CONTRACT, GITIGNORE, SCAFFOLD_MAIN, SCAFFOLD_SKILL, SCAFFOLD_TEST
from .core import (
    CapError,
    cap_lock,
    cap_path,
    caps_dir,
    data_dir,
    ensure_venv,
    execute,
    install_timeout,
    keep_tags,
    load_capability,
    load_capability_at,
    requirements_hash,
    run_tests,
    run_timeout,
    state_dir,
    test_timeout,
    tree_hash,
    validate_packages,
)
from .core import (
    list_capabilities as discover,
)

log = logging.getLogger("capforge")

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


def _archive_draft(name: str, path: Path) -> Path:
    """Keep the previous draft instead of deleting it on scaffold(overwrite=true)."""
    stamp = time.strftime("%Y%m%d-%H%M%S")
    dest = state_dir() / "backups" / name / f"{stamp}-{time.time_ns() % 1_000_000:06d}"
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(path, dest, ignore=versioning.SKIP)
    return dest


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
                    active.get("content_hash")
                    and cap.draft_ready
                    and active["content_hash"] != tree_hash(cap.path)
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
    Start here instead of writing files by hand: the skeleton already satisfies the contract.
    overwrite=true archives the previous draft under the state directory first."""
    try:
        path = cap_path(name)
    except CapError as exc:
        return _fail(str(exc))
    description = description.strip()
    if not description:
        return _fail("description is required: it is the only thing the model sees in the manifest.")
    if "\n" in description or "\r" in description or "---" in description:
        return _fail("description must be one line and must not contain '---'.")
    if len(description) > 500:
        return _fail("description must be at most 500 characters.")
    try:
        pkgs = validate_packages(packages or [])
    except CapError as exc:
        return _fail(str(exc))

    backup: Path | None = None
    try:
        with cap_lock(name):
            if path.exists() and not overwrite:
                return _fail(
                    f"{name!r} already exists at {path}. Pass overwrite=true to replace it "
                    "(the old draft is archived first), or edit it in place."
                )
            if path.exists():
                backup = _archive_draft(name, path)
                shutil.rmtree(path)
            (path / "tests").mkdir(parents=True)
            (path / "SKILL.md").write_text(
                SCAFFOLD_SKILL.format(
                    name=name, description=description, packages=", ".join(pkgs)
                ),
                encoding="utf-8",
            )
            (path / "main.py").write_text(SCAFFOLD_MAIN.format(name=name), encoding="utf-8")
            (path / "tests" / "test_main.py").write_text(
                SCAFFOLD_TEST.format(name=name), encoding="utf-8"
            )
            (path / ".gitignore").write_text(GITIGNORE, encoding="utf-8")
    except OSError as exc:
        return _fail(f"Could not write {path}: {exc}")
    return {
        "ok": True,
        "name": name,
        "path": str(path),
        "files": ["SKILL.md", "main.py", "tests/test_main.py"],
        "backup": str(backup) if backup else None,
        "next": "Edit main.py and SKILL.md, then call test_capability(name) and promote_capability(name).",
    }


@mcp.tool
def install_packages(name: str, packages: list[str]) -> dict:
    """Install Python packages into this capability's own virtualenv and record them in
    SKILL.md. Isolated from the hub and from every other capability. To remove packages,
    call promote_capability with the exact new `packages` list."""
    cap, err = _cap_or_error(name)
    if err:
        return err
    try:
        wanted = validate_packages(packages)
    except CapError as exc:
        return _fail(str(exc))
    if not wanted:
        return _fail("packages is empty")
    merged = sorted({*cap.packages, *wanted})
    try:
        with cap_lock(cap.name):
            venv = ensure_venv(cap.name, merged)
            versioning.write_packages(cap.name, merged)
    except CapError as exc:
        return _fail(str(exc))
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
    """Run the draft's test suite (pytest if tests/test_*.py exists, otherwise a real smoke
    run through the same JSON contract). Optionally also run the entrypoint once with `args`
    and report `run_ok` separately. The test suite is the pre-promotion gate: promote refuses
    while tests fail."""
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
        "tests_ok": tests.ok,
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
            payload["run_ok"] = False
            payload["ok"] = False
        else:
            payload["run"] = run.as_dict()
            payload["run_ok"] = run.ok
            payload["ok"] = bool(payload["ok"] and run.ok)
    return payload


# ------------------------------------------------------------------------- runtime


@mcp.tool
def run_capability(name: str, args: dict | None = None) -> dict:
    """Execute the ACTIVE tagged version of a capability. Falls back to the draft only when
    nothing has been promoted yet, and says so in the result. The tag's own SKILL.md defines
    its entrypoint and packages; later draft edits cannot change what the tag runs."""
    try:
        path = cap_path(name)
    except CapError as exc:
        return _fail(str(exc))
    if not path.is_dir():
        return _fail(
            f"No capability named {name!r} in {caps_dir()}. Use scaffold_capability to create one."
        )

    state = versioning.read_state(name)
    active = state.get("active") or {}
    tag = active.get("tag")

    if tag:
        source = versioning.version_path(name, tag)
        if not source.is_dir():
            return _fail(
                f"Active tag {tag!r} is recorded but its files are gone. Roll back or promote again."
            )
        source_cap = load_capability_at(name, source)
        if source_cap.error:
            return _fail(f"Active tag {tag!r} is not runnable: {source_cap.error}")
        packages = active["packages"] if "packages" in active else source_cap.packages
        mode = "active"
    else:
        source_cap = load_capability(name)
        if not source_cap.draft_ready:
            return _fail(
                f"{name!r} has never been promoted and the draft is not runnable "
                f"({source_cap.error}). Run test_capability and promote_capability first."
            )
        source, packages, tag, mode = source_cap.path, source_cap.packages, None, "draft"

    try:
        result = execute(source_cap, source, args or {}, tag=tag, packages=packages)
    except CapError as exc:
        return _fail(str(exc))

    out = result.as_dict()
    out.update(
        {
            "name": name,
            "version": tag or "draft",
            "mode": mode,
            "entrypoint": source_cap.entrypoint,
            "packages": packages,
            "content_hash": str(active.get("content_hash") or tree_hash(source))[:12],
            "data_dir": str(data_dir(name, tag)),
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
    Runs the tests first and refuses on failure. Old tags beyond the keep limit are pruned,
    along with their per-version data and unused venvs. Pass force=true to tag an unchanged
    draft (a re-release of the same content)."""
    cap, err = _cap_or_error(name)
    if err:
        return err
    if not cap.draft_ready:
        return _fail(f"Draft is not runnable: {cap.error}")
    if packages is not None:
        try:
            packages = validate_packages(packages)
        except CapError as exc:
            return _fail(str(exc))

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
        validated = cap_path(name).name
        result = versioning.rollback(validated, tag)
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
    from .core import ensure_workspace_skill

    ensure_workspace_skill()
    mcp.run()
