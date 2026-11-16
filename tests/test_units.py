"""Fast unit tests: no virtualenvs, no capability subprocesses.

A few process-hygiene tests spawn plain `python -c` children to prove the
stray sweep; those are plain OS processes, not capability runs.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from pathlib import Path

import pytest

from capforge import core
from capforge.cli import _is_skills_path


def test_parse_skill_md_inline_lists():
    text = (
        "---\n"
        "name: demo\n"
        "description: A demo.\n"
        "entrypoint: main.py\n"
        "packages: [pypdf>=5, httpx]\n"
        "---\n\n"
        "# demo\n"
    )
    meta, body = core.parse_skill_md(text)
    assert meta["name"] == "demo"
    assert meta["packages"] == ["pypdf>=5", "httpx"]
    assert body.startswith("# demo")


def test_parse_skill_md_block_lists_and_quotes():
    text = (
        "---\n"
        "name: demo\n"
        "description: 'A demo.'\n"
        "packages:\n"
        "  - 'six'\n"
        "  - httpx\n"
        "---\n"
    )
    meta, _ = core.parse_skill_md(text)
    assert meta["description"] == "A demo."
    assert meta["packages"] == ["six", "httpx"]


def test_parse_skill_md_without_frontmatter():
    meta, body = core.parse_skill_md("hello")
    assert meta == {}
    assert body == "hello"


def test_kind_parsing_and_validation(tmp_path):
    cap = tmp_path / "demo"
    cap.mkdir()
    (cap / "app.py").write_text("app = None\n", encoding="utf-8")
    (cap / "SKILL.md").write_text(
        "---\ndescription: d\nkind: web\nentrypoint: app.py\npackages: [uvicorn]\n---\n",
        encoding="utf-8",
    )
    loaded = core.load_capability_at("demo", cap)
    assert loaded.kind == "web"
    assert loaded.app_name == "app"
    assert loaded.error is None

    (cap / "SKILL.md").write_text(
        "---\ndescription: d\nkind: worker\nentrypoint: app.py\npackages: [uvicorn]\n---\n",
        encoding="utf-8",
    )
    assert "kind must be one of" in core.load_capability_at("demo", cap).error


def test_web_requires_uvicorn(tmp_path):
    cap = tmp_path / "demo"
    cap.mkdir()
    (cap / "app.py").write_text("app = None\n", encoding="utf-8")
    (cap / "SKILL.md").write_text(
        "---\ndescription: d\nkind: web\nentrypoint: app.py\npackages: []\n---\n",
        encoding="utf-8",
    )
    loaded = core.load_capability_at("demo", cap)
    assert loaded.error is not None
    assert "uvicorn" in loaded.error


def test_web_rejects_non_identifier_app(tmp_path):
    cap = tmp_path / "demo"
    cap.mkdir()
    (cap / "app.py").write_text("app = None\n", encoding="utf-8")
    (cap / "SKILL.md").write_text(
        "---\ndescription: d\nkind: web\nentrypoint: app.py\napp: 'bad name'\npackages: [uvicorn]\n---\n",
        encoding="utf-8",
    )
    loaded = core.load_capability_at("demo", cap)
    assert loaded.error is not None
    assert "identifier" in loaded.error


def test_execute_refuses_web_capabilities(tmp_path, monkeypatch):
    monkeypatch.setenv("CAPFORGE_STATE_DIR", str(tmp_path / "state"))
    cap = core.Capability(name="demo", path=tmp_path, kind="web")
    with pytest.raises(core.CapError) as exc_info:
        core.execute(cap, tmp_path, {}, tag=None)
    assert "serve_capability" in str(exc_info.value)


def test_registry_persistence(tmp_path, monkeypatch):
    import capforge.web as web

    monkeypatch.setenv("CAPFORGE_STATE_DIR", str(tmp_path / "state"))
    record = {"name": "demo", "tag": "v1", "desired": "start", "packages": ["uvicorn"]}
    web._persist_record("demo", record)
    assert web.read_registry()["demo"] == record
    web._persist_record("demo", {**record, "desired": "stop"})
    assert web.read_registry()["demo"]["desired"] == "stop"
    web._persist_record("demo", None)
    assert web.read_registry() == {}


def test_corrupt_registry_is_quarantined(tmp_path, monkeypatch):
    import capforge.web as web

    monkeypatch.setenv("CAPFORGE_STATE_DIR", str(tmp_path / "state"))
    served = tmp_path / "state" / "served"
    served.mkdir(parents=True)
    (served / "registry.json").write_text("{not json", encoding="utf-8")
    assert web.read_registry() == {}
    assert not (served / "registry.json").exists()
    assert list(served.glob("registry.corrupt-*.json"))


def test_registry_drops_invalid_entries(tmp_path, monkeypatch):
    import capforge.web as web

    monkeypatch.setenv("CAPFORGE_STATE_DIR", str(tmp_path / "state"))
    served = tmp_path / "state" / "served"
    served.mkdir(parents=True)
    (served / "registry.json").write_text(
        json.dumps(
            {
                "good": {"name": "good", "tag": "v1", "desired": "stop"},
                "BAD NAME": {"tag": "v1", "desired": "stop"},
                "bad-desired": {"tag": "v1", "desired": "maybe"},
            }
        ),
        encoding="utf-8",
    )
    assert list(web.read_registry()) == ["good"]


def test_parse_bound_port(tmp_path):
    import capforge.web as web

    log = tmp_path / "server.log"
    log.write_text(
        "INFO:     Started server process\n"
        "INFO:     Uvicorn running on http://127.0.0.1:43215 (Press CTRL+C to quit)\n",
        encoding="utf-8",
    )
    assert web._parse_bound_port(log) == 43215
    log.write_text("no port line here\n", encoding="utf-8")
    assert web._parse_bound_port(log) is None


# ------------------------------------------------------------------ console units


def _console_client(app):
    """httpx client against the console ASGI app; httpx2-free (no TestClient)."""
    import httpx

    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://console")


def test_console_requires_token(tmp_path, monkeypatch):
    import anyio

    import capforge.web as web

    monkeypatch.delenv("CAPFORGE_WEB_TOKEN", raising=False)
    monkeypatch.setenv("CAPFORGE_WEB_UI", "0")

    async def run() -> dict[str, int]:
        async with _console_client(web.asgi_app()) as client:
            login = (await client.post("/api/login", json={"token": "x"})).status_code
            caps = (await client.get("/api/caps")).status_code
            control = (await client.post("/api/caps/demo/start")).status_code
            return {"login": login, "caps": caps, "control": control}

    codes = anyio.run(run)
    assert codes == {"login": 403, "caps": 403, "control": 403}


def test_console_login_and_csrf(tmp_path, monkeypatch):
    import anyio

    import capforge.web as web

    monkeypatch.setenv("CAPFORGE_CAPS_DIR", str(tmp_path / "caps"))
    monkeypatch.setenv("CAPFORGE_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("CAPFORGE_WEB_TOKEN", "sekrit")
    monkeypatch.setenv("CAPFORGE_WEB_UI", "0")

    async def run() -> None:
        async with _console_client(web.asgi_app()) as client:
            session = (await client.get("/api/session")).json()
            assert session == {"ok": True, "authed": False, "console_enabled": True}
            assert (await client.get("/api/caps")).status_code == 401
            bad = await client.post("/api/login", json={"token": "wrong"})
            assert bad.status_code == 401
            good = await client.post("/api/login", json={"token": "sekrit"})
            assert good.status_code == 200
            assert "capforge_session" in good.cookies
            cookie = good.cookies["capforge_session"]
            headers = {"cookie": f"capforge_session={cookie}"}
            # Cookie-authenticated GET works...
            caps = await client.get("/api/caps", headers=headers)
            assert caps.status_code == 200
            assert caps.json()["ok"] is True
            # ...but writes need the CSRF header.
            assert (await client.post("/api/caps/nope/start", headers=headers)).status_code == 403
            with_csrf = await client.post(
                "/api/caps/nope/start", headers={**headers, "x-csrf": "1"}
            )
            assert with_csrf.status_code == 400  # authenticated; unknown cap -> CapError

    anyio.run(run)


def test_console_json_index_fallback(tmp_path, monkeypatch):
    import anyio

    import capforge.web as web

    monkeypatch.delenv("CAPFORGE_WEB_TOKEN", raising=False)
    monkeypatch.setenv("CAPFORGE_WEB_UI", "0")

    async def run() -> None:
        async with _console_client(web.asgi_app()) as client:
            index = await client.get("/")
            assert index.status_code == 200
            body = index.json()
            assert body["service"] == "capforge"
            assert body["ui"] is False
            assert isinstance(body["capabilities"], list)

    anyio.run(run)


def test_console_unknown_cap_is_404(tmp_path, monkeypatch):
    import anyio

    import capforge.web as web

    monkeypatch.delenv("CAPFORGE_WEB_TOKEN", raising=False)
    monkeypatch.setenv("CAPFORGE_WEB_UI", "0")

    async def run() -> None:
        async with _console_client(web.asgi_app()) as client:
            assert (await client.get("/no_such_cap/anything")).status_code == 404
            redirect = await client.get("/no_such_cap", follow_redirects=False)
            assert redirect.status_code == 307

    anyio.run(run)


# ------------------------------------------------------------------ nothing survives a call


def test_set_subreaper_is_idempotent():
    assert core.set_subreaper() in (True, False)
    # Second call must not re-issue prctl or change the answer.
    assert core.set_subreaper() == core.set_subreaper()


def test_sweep_leaves_owned_children_alone():
    """register_owned protects a real direct child from the stray sweep."""
    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        core.register_owned(proc.pid)
        with core._child_lock:
            core._owned_children.add(proc.pid)  # explicit, in case prctl is unavailable
        assert core.sweep_stray_children() == []
        assert proc.poll() is None
    finally:
        core.unregister_owned(proc.pid)
        with core._child_lock:
            core._owned_children.discard(proc.pid)
        proc.kill()
        proc.wait(timeout=10)


def test_sweep_kills_unowned_direct_child():
    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        # Not registered: the sweep must treat this direct child as stray.
        killed = core.sweep_stray_children()
        assert proc.pid in killed
        proc.wait(timeout=10)  # SIGKILL delivery is not synchronous with poll()
        assert proc.returncode is not None
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=10)


@pytest.mark.parametrize(
    "spec",
    ["six", "pypdf>=5", "httpx[http2]", "numpy==1.26.*", "pkg[extra]>=1"],
)
def test_valid_package_requirements(spec):
    assert core.validate_packages([spec]) == [spec]


@pytest.mark.parametrize(
    "spec",
    ["--target=/tmp/x", "six seven", "six,seven", "https://example/six.whl", "-r x.txt", ""],
)
def test_invalid_package_requirements(spec):
    if spec == "":
        assert core.validate_packages([spec]) == []
    else:
        with pytest.raises(core.CapError):
            core.validate_packages([spec])


def test_package_limit():
    too_many = [f"pkg{i}" for i in range(core.MAX_PACKAGES + 1)]
    with pytest.raises(core.CapError):
        core.validate_packages(too_many)


def test_tree_hash_ignores_caches(tmp_path):
    (tmp_path / "main.py").write_text("print(1)\n", encoding="utf-8")
    before = core.tree_hash(tmp_path)
    (tmp_path / "__pycache__").mkdir()
    (tmp_path / "__pycache__" / "main.pyc").write_bytes(b"cache")
    (tmp_path / ".pytest_cache").mkdir()
    (tmp_path / ".pytest_cache" / "nodeids").write_text("x", encoding="utf-8")
    assert core.tree_hash(tmp_path) == before


def test_requirements_hash_is_stable_and_order_independent():
    assert core.requirements_hash(["b", "a", "a"]) == core.requirements_hash(["a", "b"])
    assert len(core.requirements_hash([])) == 16


def test_entrypoint_cannot_escape_the_capability(tmp_path):
    cap = tmp_path / "demo"
    cap.mkdir()
    (cap / "main.py").write_text("print(1)\n", encoding="utf-8")
    (cap / "SKILL.md").write_text(
        "---\ndescription: d\nentrypoint: ../escape.py\n---\n", encoding="utf-8"
    )
    loaded = core.load_capability_at("demo", cap)
    assert loaded.error is not None
    assert "escapes" in loaded.error


def test_absolute_entrypoint_is_rejected(tmp_path):
    cap = tmp_path / "demo"
    cap.mkdir()
    (cap / "SKILL.md").write_text(
        "---\ndescription: d\nentrypoint: /etc/passwd\n---\n", encoding="utf-8"
    )
    loaded = core.load_capability_at("demo", cap)
    assert loaded.error is not None
    assert "escapes" in loaded.error


def test_corrupt_state_types_are_coerced(tmp_path, monkeypatch):
    monkeypatch.setenv("CAPFORGE_STATE_DIR", str(tmp_path / "state"))
    state_dir = tmp_path / "state" / "state"
    state_dir.mkdir(parents=True)
    (state_dir / "demo.json").write_text(
        json.dumps({"active": "v1", "tags": ["v1", {"tag": "bad tag"}]}),
        encoding="utf-8",
    )
    state = core.read_state("demo")
    assert state["active"] is None
    assert state["tags"] == []


def test_corrupt_state_is_renamed(tmp_path, monkeypatch):
    monkeypatch.setenv("CAPFORGE_STATE_DIR", str(tmp_path / "state"))
    state_dir = tmp_path / "state" / "state"
    state_dir.mkdir(parents=True)
    bad = state_dir / "demo.json"
    bad.write_text("{", encoding="utf-8")
    assert core.read_state("demo")["active"] is None
    assert not bad.exists()
    assert list(state_dir.glob("demo.corrupt-*.json"))


def test_data_dir_create_flag(tmp_path, monkeypatch):
    monkeypatch.setenv("CAPFORGE_STATE_DIR", str(tmp_path / "state"))
    path = core.data_dir("demo", "v1", create=False)
    assert not path.exists()
    created = core.data_dir("demo", "v1")
    assert created.is_dir()


@pytest.mark.parametrize(
    "path",
    [
        "/home/a773158d/.agents/skills",
        "/home/capforge/.agents/skills",
        "/home/other/.cptr/skills",
        "/srv/terminal/.claude/skills",
        "/home/x/.codex/skills",
    ],
)
def test_doctor_accepts_skills_dirs_in_any_home(path):
    assert _is_skills_path(Path(path))


@pytest.mark.parametrize(
    "path",
    ["/srv/caps", "/home/x/.config/skills", "/home/x/.agents/other", "/home/x/skills"],
)
def test_doctor_rejects_paths_that_are_not_terminal_skills(path):
    assert not _is_skills_path(Path(path))


def test_workspace_guide_is_installed_and_reserved(tmp_path, monkeypatch):
    monkeypatch.setenv("CAPFORGE_CAPS_DIR", str(tmp_path / "caps"))
    path = core.ensure_workspace_skill()
    assert path is not None
    text = path.read_text(encoding="utf-8")
    assert text.startswith("---")
    assert core.WORKSPACE_SKILL_MARKER in text
    assert "get_contract" in text
    assert core.list_capabilities() == []


def test_workspace_guide_preserves_user_file(tmp_path, monkeypatch):
    monkeypatch.setenv("CAPFORGE_CAPS_DIR", str(tmp_path / "caps"))
    path = tmp_path / "caps" / "_capforge" / "SKILL.md"
    path.parent.mkdir(parents=True)
    path.write_text("---\ndescription: mine\n---\n", encoding="utf-8")
    assert core.ensure_workspace_skill() == path
    assert "description: mine" in path.read_text(encoding="utf-8")


def test_workspace_guide_can_be_disabled(tmp_path, monkeypatch):
    monkeypatch.setenv("CAPFORGE_CAPS_DIR", str(tmp_path / "caps"))
    monkeypatch.setenv("CAPFORGE_GUIDE", "0")
    assert core.ensure_workspace_skill() is None
    assert not (tmp_path / "caps" / "_capforge").exists()


def test_tool_surface_is_fixed_at_eleven():
    from capforge.server import mcp

    names = [tool.name for tool in asyncio.run(mcp.list_tools())]
    assert names == [
        "get_contract",
        "list_capabilities",
        "get_capability",
        "scaffold_capability",
        "install_packages",
        "test_capability",
        "run_capability",
        "serve_capability",
        "promote_capability",
        "rollback_capability",
        "list_tags",
    ]
