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

from . import core, gitremote

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

TEST = '''\
"""Tests for {name}. A failed test blocks promotion."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def run(payload: dict) -> dict:
    proc = subprocess.run(
        [sys.executable, str(ROOT / "main.py")],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_happy_path() -> None:
    assert run({{"text": "hello"}})["length"] == 5


def test_rejects_bad_input() -> None:
    assert "error" in run({{"text": 42}})
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

AGENTS = '''\
# {name}

This capability is authored in a git repository. Capforge does not share its
capability directory with your terminal; you work in a clone.

## Loop

1. `staging("{name}", action="token")` returns a short-lived token and `git_url`.
2. Clone the repository with that token (credential helper or `GIT_CONFIG_*`).
3. Edit the uv project. Add dependencies with `uv add`; always run `uv lock`.
4. `git commit` and `git push origin HEAD:main`.
5. Poll `staging("{name}", action="latest")`; read `staging("{name}", action="logs")`
   if the pipeline failed.

Promotion is automatic after a green pipeline test. There is no manual promotion
tool. Runtime files belong under `CAPFORGE_DATA_DIR`; add `data: shared` to
`SKILL.md` only if state must survive a new tag.

## Environment

Declare every variable you read as `env: [NAME, ...]` in `SKILL.md`. The operator
sets the values in the console; you cannot read or write them and must never commit
them. Values are shared across tags and injected only into the active tag. Tests
and the web boot probe get `capforge-test-placeholder`, so never require a real
secret at import time.
'''

SKILL = """\
---
name: {name}
description: {description}
entrypoint: {entrypoint}
# env: [API_KEY]
{extra}---

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
    web = kind == "web"
    deps = '["fastapi", "uvicorn", "httpx"]' if web else "[]"
    (root / "pyproject.toml").write_text(
        PYPROJECT.format(name=name, dependencies=deps), encoding="utf-8"
    )
    (root / ".python-version").write_text("3.14\n", encoding="utf-8")
    (root / ".gitignore").write_text(GITIGNORE, encoding="utf-8")
    (root / "AGENTS.md").write_text(AGENTS.format(name=name), encoding="utf-8")
    if web:
        (root / "app.py").write_text(APP.format(name=name), encoding="utf-8")
        (root / "tests").mkdir()
        (root / "tests" / "test_scaffold.py").write_text(
            TEST_WEB.format(name=name), encoding="utf-8"
        )
        extra = "kind: web\n"
        entry = "app.py"
    else:
        (root / "main.py").write_text(MAIN.format(name=name), encoding="utf-8")
        (root / "tests").mkdir()
        (root / "tests" / "test_scaffold.py").write_text(
            TEST.format(name=name), encoding="utf-8"
        )
        extra = "data: version\n"
        entry = "main.py"
    (root / "SKILL.md").write_text(
        SKILL.format(name=name, description=description, entrypoint=entry, extra=extra),
        encoding="utf-8",
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
    if kind not in core.WEB_KINDS:
        raise core.CapError(f"kind must be one of {list(core.WEB_KINDS)}")
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
        return {
            "name": name,
            "path": str(draft),
            "git_url": _git_url(name),
            "sha": sha,
            "kind": kind,
            "python": core.PYTHON_VERSION,
            "files": core.list_files(draft),
        }
    except Exception:
        shutil.rmtree(work, ignore_errors=True)
        raise
    finally:
        shutil.rmtree(work, ignore_errors=True)
