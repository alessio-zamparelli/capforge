"""Scaffold a capability as a uv project and publish its base commit.

The scaffold no longer writes into a terminal-visible shared folder. It builds a
self-contained project, locks it with uv, commits it, publishes `main` in the
capability's bare repo, and materialises the same tree as the server-side draft.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from . import contract, core, gitremote

MAIN = '''\
"""Entrypoint for {name}. One JSON object on stdin, one JSON value on stdout."""

from __future__ import annotations

import json
import sys


def handle(args: dict) -> dict:
    text = args.get("text")
    if not isinstance(text, str):
        return {{"error": "text must be a string"}}
    return {{"text": text, "length": len(text)}}


def main() -> int:
    try:
        args = json.loads(sys.stdin.read() or "{{}}")
    except ValueError:
        print("stdin must be a JSON object", file=sys.stderr)
        return 2
    if not isinstance(args, dict):
        print("stdin must be a JSON object", file=sys.stderr)
        return 2
    try:
        result = handle(args)
    except Exception as exc:  # noqa: BLE001 - a bug must not look like a crash
        print(f"unhandled error: {{type(exc).__name__}}: {{exc}}", file=sys.stderr)
        return 1
    json.dump(result, sys.stdout, ensure_ascii=False, sort_keys=True)
    sys.stdout.write("\\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
'''

DEPLOY_CLI = '''\
"""Deployment descriptor for {name}. Capforge reads this file; keep it declarative."""

from __future__ import annotations

from pydantic import BaseModel


class Env(BaseModel):
    """Operator-set values, injected only into the active tag."""

    # API_KEY: str


KIND = ["cli"]
CLI_ENTRYPOINT = "main.py"
DATA = "version"
# SHARES = {{"datasets": "ro"}}  # operator-approved cross-capability folders
# LLM = True  # call the hub model gateway; the operator picks the model
'''

DEPLOY_WEB = '''\
"""Deployment descriptor for {name}. Capforge reads this file; keep it declarative."""

from __future__ import annotations

from pydantic import BaseModel


class Env(BaseModel):
    """Operator-set values, injected only into the active tag."""

    # API_KEY: str


KIND = ["web"]
WEB_ENTRYPOINT = "app.py"
APP = "app"
DATA = "version"
# SHARES = {{"datasets": "ro"}}  # operator-approved cross-capability folders
# LLM = True  # call the hub model gateway; the operator picks the model
# AUTH = {{"provider": "corp"}}  # require OIDC sign-in; configure the provider in the console
'''

DEPLOY_BOTH = '''\
"""Deployment descriptor for {name}. Capforge reads this file; keep it declarative."""

from __future__ import annotations

from pydantic import BaseModel


class Env(BaseModel):
    """Operator-set values, injected only into the active tag."""

    # API_KEY: str


KIND = ["cli", "web"]
CLI_ENTRYPOINT = "main.py"
WEB_ENTRYPOINT = "app.py"
APP = "app"
DATA = "version"
# SHARES = {{"datasets": "ro"}}  # operator-approved cross-capability folders
# LLM = True  # call the hub model gateway; the operator picks the model
# AUTH = {{"provider": "corp"}}  # require OIDC sign-in; configure the provider in the console
'''

TEST = '''\
"""Tests for {name}. A failed test blocks promotion."""

from __future__ import annotations

import io
import json
import runpy
import subprocess
import sys
from pathlib import Path

import pytest

import main as entrypoint
from main import handle

ROOT = Path(__file__).resolve().parents[1]


def test_happy_path() -> None:
    assert handle({{"text": "hello"}})["length"] == 5


def test_rejects_bad_input() -> None:
    assert "error" in handle({{"text": 42}})


def test_main_rejects_invalid_json(monkeypatch, capsys) -> None:
    monkeypatch.setattr(sys, "stdin", io.StringIO("{{"))
    assert entrypoint.main() == 2
    assert "stdin must be a JSON object" in capsys.readouterr().err


def test_main_rejects_non_object(monkeypatch, capsys) -> None:
    monkeypatch.setattr(sys, "stdin", io.StringIO("[]"))
    assert entrypoint.main() == 2
    assert "stdin must be a JSON object" in capsys.readouterr().err


def test_main_reports_handler_crash(monkeypatch, capsys) -> None:
    monkeypatch.setattr(entrypoint, "handle", lambda args: 1 / 0)
    monkeypatch.setattr(sys, "stdin", io.StringIO('{{"text": "hello"}}'))
    assert entrypoint.main() == 1
    assert "unhandled error: ZeroDivisionError" in capsys.readouterr().err


def test_real_entrypoint_under_coverage(monkeypatch, capsys) -> None:
    # runpy executes the real file, including the __main__ guard, in this process,
    # so pytest-cov can measure main.py instead of a fresh, untraced interpreter.
    monkeypatch.setattr(sys, "stdin", io.StringIO('{{"text": "hello"}}'))
    with pytest.raises(SystemExit) as exc:
        runpy.run_path(str(ROOT / "main.py"), run_name="__main__")
    assert exc.value.code == 0
    assert json.loads(capsys.readouterr().out)["length"] == 5


def test_process_boundary() -> None:
    # Separate process: proves stdin/stdout/exit-code behaviour, but is not traced.
    proc = subprocess.run(
        [sys.executable, str(ROOT / "main.py")],
        input=json.dumps({{"text": "hello"}}),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout.strip().splitlines()[-1])["length"] == 5
'''

APP = '''\
"""ASGI app for web capability {name}.

After promotion it runs as a long-lived service behind /cap/{name}/.
Validate every input: anyone who can reach the proxy is a caller.
"""

from __future__ import annotations

import os
from pathlib import Path

from fastapi import FastAPI

# The server boots uvicorn with --root-path /cap/{name} and exports
# CAPFORGE_ROOT_PATH. Do not pass root_path= to FastAPI; it is deprecated.
app = FastAPI(title="{name}")


@app.get("/")
def index() -> dict:
    return {{"service": "{name}", "status": "ok"}}


@app.get("/health")
def health() -> dict:
    return {{"healthy": True}}


@app.get("/data")
def data() -> dict:
    root = Path(os.environ.get("CAPFORGE_SHARED_DATA_DIR") or os.environ["CAPFORGE_DATA_DIR"])
    return {{"dir": str(root), "exists": root.is_dir()}}
'''

TEST_WEB = '''\
"""In-process tests for {name}; the promotion gate also boots the real app."""

from __future__ import annotations

import asyncio
import httpx

from app import app


def test_health() -> None:
    async def run() -> None:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/health")
        assert response.status_code == 200
        assert response.json()["healthy"] is True

    asyncio.run(run())
'''

SKILL = """\
---
name: {name}
description: {description}
---

# {name}

Describe what this capability does and what it returns. The model reads this in
`get_capability` and in the repository.
"""

GITIGNORE = "__pycache__/\n*.pyc\n.pytest_cache/\n.mypy_cache/\n.ruff_cache/\n.venv/\n"

PYPROJECT = """\
[project]
name = "{name}"
version = "0.1.0"
requires-python = ">=3.14,<3.15"
dependencies = {dependencies}

[dependency-groups]
dev = ["pytest", "pytest-cov"]

[tool.uv]
package = false

[tool.coverage.run]
source = ["."]
omit = ["tests/*"]

[tool.pytest.ini_options]
addopts = "--cov=. --cov-report=term-missing"
"""


def _valid_description(description: str) -> str:
    text = (description or "").strip()
    if not text or "\n" in text or "\r" in text or "---" in text:
        raise core.CapError("description must be one non-empty line without '---'")
    return text


def _write_project(root: Path, name: str, description: str, kind: str) -> None:
    modes = ("cli", "web") if kind == "both" else (kind,)
    runs_cli = "cli" in modes
    serves_web = "web" in modes
    deps = (
        '["fastapi", "uvicorn", "httpx", "pydantic"]' if serves_web else '["pydantic"]'
    )
    (root / "pyproject.toml").write_text(
        PYPROJECT.format(name=name, dependencies=deps), encoding="utf-8"
    )
    (root / ".python-version").write_text("3.14\n", encoding="utf-8")
    (root / ".gitignore").write_text(GITIGNORE, encoding="utf-8")
    (root / "AGENTS.md").write_text(
        contract.AGENTS_TEMPLATE.replace("__CAPABILITY__", name), encoding="utf-8"
    )
    template = DEPLOY_BOTH if len(modes) > 1 else (DEPLOY_WEB if serves_web else DEPLOY_CLI)
    (root / "deploy.py").write_text(template.format(name=name), encoding="utf-8")
    tests = root / "tests"
    tests.mkdir()
    if runs_cli:
        (root / "main.py").write_text(MAIN.format(name=name), encoding="utf-8")
        (tests / "test_scaffold.py").write_text(TEST.format(name=name), encoding="utf-8")
    if serves_web:
        (root / "app.py").write_text(APP.format(name=name), encoding="utf-8")
        web_test = tests / ("test_web.py" if runs_cli else "test_scaffold.py")
        web_test.write_text(TEST_WEB.format(name=name), encoding="utf-8")
    (root / "SKILL.md").write_text(
        SKILL.format(name=name, description=description), encoding="utf-8"
    )


def _uv_lock(root: Path) -> None:
    uv = shutil.which("uv") or "/bin/uv"
    if not Path(uv).exists():
        raise core.CapError("uv is not installed; the image must provide uv")
    code, _, stderr, timed_out = core._run_process(
        [uv, "lock"],
        cwd=root,
        env=core._install_env(),
        payload=None,
        timeout=core.install_timeout(),
    )
    if timed_out:
        raise core.CapError("uv lock timed out")
    if code != 0:
        raise core.CapError(f"uv lock failed:\n{stderr.strip()[-2000:]}")


def _commit(root: Path, name: str) -> None:
    env = core._base_env()
    env.update(
        {
            "GIT_AUTHOR_NAME": "capforge",
            "GIT_AUTHOR_EMAIL": "capforge@localhost",
            "GIT_COMMITTER_NAME": "capforge",
            "GIT_COMMITTER_EMAIL": "capforge@localhost",
        }
    )
    for args in (
        ["git", "init", "-q"],
        ["git", "add", "-A"],
        ["git", "commit", "-q", "-m", f"scaffold {name}"],
    ):
        proc = subprocess.run(
            args, cwd=str(root), env=env, capture_output=True, text=True, timeout=60
        )
        if proc.returncode != 0:
            raise core.CapError(
                f"{' '.join(args)} failed: {(proc.stdout or proc.stderr).strip()[-1200:]}"
            )


def _git_url(name: str) -> str:
    from . import web

    return f"{web.public_base()}/git/{name}.git"


def create(name: str, description: str, kind: str = "cli") -> dict[str, Any]:
    """Create the base project, publish main and materialise the draft."""
    if not core.NAME_RE.match(name or ""):
        raise core.CapError(
            f"Invalid capability name {name!r}. Use lowercase letters, digits and underscores, "
            "starting with a letter (max 64 chars)."
        )
    if kind not in core.SCAFFOLD_KINDS:
        raise core.CapError(f"kind must be one of {list(core.SCAFFOLD_KINDS)}")
    description = _valid_description(description)
    draft = core.cap_path(name)
    if draft.exists() or gitremote.repo_path(name).exists():
        raise core.CapError(f"Capability {name!r} already exists.")

    base = core.state_dir() / "scaffold"
    base.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix=f"{name}-", dir=str(base)))
    try:
        _write_project(work, name, description, kind)
        _uv_lock(work)
        _commit(work, name)
        gitremote.init_repo(name)
        sha = gitremote.publish(name, work)
        shutil.rmtree(draft, ignore_errors=True)
        draft.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(work, draft, ignore=shutil.ignore_patterns(".git"))
        with core.cap_lock(name):
            state = core.read_state(name)
            state.update({"name": name, "active": None, "tags": [], "pruned": []})
            core.write_state(name, state)
        log_path = core.state_dir() / "logs" / name / "scaffold.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text(
            f"scaffold {name}\nsha: {sha}\nkind: {kind}\nproject: {work}\n", encoding="utf-8"
        )
        files = core.list_files(draft, limit=core.MAX_LISTED_FILES + 1)
        return {
            "name": name,
            "path": str(draft),
            "git_url": _git_url(name),
            "sha": sha,
            "kind": kind,
            "modes": ["cli", "web"] if kind == "both" else [kind],
            "python": core.PYTHON_VERSION,
            "files": files[: core.MAX_LISTED_FILES],
            "files_truncated": len(files) > core.MAX_LISTED_FILES,
        }
    except Exception:
        shutil.rmtree(work, ignore_errors=True)
        raise
    finally:
        shutil.rmtree(work, ignore_errors=True)
