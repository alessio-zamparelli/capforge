"""Fast unit tests: no virtualenvs, no capability subprocesses.

A few process-hygiene tests spawn plain `python -c` children to prove the
stray sweep; those are plain OS processes, not capability runs.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys

import pytest

from capforge import core


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
    (cap / "pyproject.toml").write_text(
        '[project]\nname="demo"\nversion="0.1.0"\nrequires-python=">=3.14,<3.15"\ndependencies=["uvicorn"]\n',
        encoding="utf-8",
    )
    (cap / "SKILL.md").write_text(
        "---\ndescription: d\nkind: web\nentrypoint: app.py\n---\n",
        encoding="utf-8",
    )
    loaded = core.load_capability_at("demo", cap)
    assert loaded.kind == "web"
    assert loaded.app_name == "app"
    assert loaded.error is None

    (cap / "SKILL.md").write_text(
        "---\ndescription: d\nkind: worker\nentrypoint: app.py\n---\n",
        encoding="utf-8",
    )
    assert "kind must be one of" in core.load_capability_at("demo", cap).error


def test_web_requires_uvicorn(tmp_path):
    cap = tmp_path / "demo"
    cap.mkdir()
    (cap / "app.py").write_text("app = None\n", encoding="utf-8")
    (cap / "SKILL.md").write_text(
        "---\ndescription: d\nkind: web\nentrypoint: app.py\n---\n",
        encoding="utf-8",
    )
    loaded = core.load_capability_at("demo", cap)
    assert loaded.error is not None
    assert "uvicorn" in loaded.error


def test_web_rejects_non_identifier_app(tmp_path):
    cap = tmp_path / "demo"
    cap.mkdir()
    (cap / "app.py").write_text("app = None\n", encoding="utf-8")
    (cap / "pyproject.toml").write_text(
        '[project]\nname="demo"\nversion="0.1.0"\nrequires-python=">=3.14,<3.15"\ndependencies=["uvicorn"]\n',
        encoding="utf-8",
    )
    (cap / "SKILL.md").write_text(
        "---\ndescription: d\nkind: web\nentrypoint: app.py\napp: 'bad name'\n---\n",
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


def test_console_env_api_is_write_only(tmp_path, monkeypatch):
    import anyio

    import capforge.web as web

    monkeypatch.setenv("CAPFORGE_CAPS_DIR", str(tmp_path / "caps"))
    monkeypatch.setenv("CAPFORGE_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("CAPFORGE_WEB_TOKEN", "sekrit")
    monkeypatch.setenv("CAPFORGE_WEB_UI", "0")
    _env_cap(tmp_path / "caps")

    async def run() -> None:
        async with _console_client(web.asgi_app()) as client:
            assert (await client.get("/api/caps/demo/env")).status_code == 401
            login = await client.post("/api/login", json={"token": "sekrit"})
            assert login.status_code == 200
            headers = {"cookie": f"capforge_session={login.cookies['capforge_session']}"}

            listed = await client.get("/api/caps/demo/env", headers=headers)
            assert listed.status_code == 200
            assert listed.json()["env"][0] == {
                "name": "API_KEY",
                "declared": True,
                "configured": False,
                "updated_at": 0,
            }

            # Writes need the CSRF header.
            missing_csrf = await client.post(
                "/api/caps/demo/env", headers=headers, json={"name": "API_KEY", "value": "v"}
            )
            assert missing_csrf.status_code == 403

            saved = await client.post(
                "/api/caps/demo/env",
                headers={**headers, "x-csrf": "1"},
                json={"name": "API_KEY", "value": "sekrit-value"},
            )
            assert saved.status_code == 200, saved.text
            assert "sekrit-value" not in saved.text
            assert saved.json()["restarted"] is False

            listed = await client.get("/api/caps/demo/env", headers=headers)
            assert "sekrit-value" not in listed.text
            assert listed.json()["env"][0]["configured"] is True

            cleared = await client.delete(
                "/api/caps/demo/env/API_KEY", headers={**headers, "x-csrf": "1"}
            )
            assert cleared.status_code == 200, cleared.text
            assert cleared.json()["removed"] is True

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
            assert (await client.get("/cap/no_such_cap/anything")).status_code == 404
            redirect = await client.get("/cap/no_such_cap", follow_redirects=False)
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


def test_tree_hash_ignores_caches(tmp_path):
    (tmp_path / "main.py").write_text("print(1)\n", encoding="utf-8")
    before = core.tree_hash(tmp_path)
    (tmp_path / "__pycache__").mkdir()
    (tmp_path / "__pycache__" / "main.pyc").write_bytes(b"cache")
    (tmp_path / ".pytest_cache").mkdir()
    (tmp_path / ".pytest_cache" / "nodeids").write_text("x", encoding="utf-8")
    assert core.tree_hash(tmp_path) == before


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


# ---------------------------------------------------------------- capability env


def _env_cap(root, name: str = "demo", env: str = "[API_KEY, SMTP_PASSWORD]"):
    cap = root / name
    cap.mkdir(parents=True)
    (cap / "main.py").write_text("print(1)\n", encoding="utf-8")
    (cap / "SKILL.md").write_text(
        f"---\ndescription: d\nentrypoint: main.py\nenv: {env}\n---\n", encoding="utf-8"
    )
    return core.load_capability_at(name, cap)


def test_env_declarations_are_parsed_and_validated(tmp_path):
    cap = _env_cap(tmp_path)
    assert cap.error is None
    assert cap.env == ["API_KEY", "SMTP_PASSWORD"]

    single = _env_cap(tmp_path, "single", "API_KEY")
    assert single.env == ["API_KEY"]

    reserved = _env_cap(tmp_path, "reserved", "[PATH]")
    assert reserved.error is not None and "reserved" in reserved.error

    invalid = _env_cap(tmp_path, "invalid", "[NOT VALID]")
    assert invalid.error is not None and "Invalid environment" in invalid.error


def test_env_store_is_private_and_shared_across_tags(tmp_path, monkeypatch):
    monkeypatch.setenv("CAPFORGE_STATE_DIR", str(tmp_path / "state"))
    cap = _env_cap(tmp_path / "caps")

    status = core.env_status("demo", [cap])
    assert status == [
        {"name": "API_KEY", "declared": True, "configured": False, "updated_at": 0},
        {"name": "SMTP_PASSWORD", "declared": True, "configured": False, "updated_at": 0},
    ]

    saved = core.set_cap_env("demo", "API_KEY", "sekrit-value")
    assert saved["configured"] is True
    path = core.state_dir() / "secrets" / "demo.json"
    assert path.is_file()
    assert (path.parent.stat().st_mode & 0o777) == 0o700
    assert (path.stat().st_mode & 0o777) == 0o600

    # The store is per capability, not per tag: another snapshot sees the same value.
    tagged = core.load_capability_at("demo", cap.path)
    assert core.cap_env_values("demo", tagged) == {"API_KEY": "sekrit-value"}
    assert core.cap_env_missing("demo", cap) == ["SMTP_PASSWORD"]
    assert "sekrit-value" not in json.dumps(core.env_status("demo", [cap]))

    assert core.delete_cap_env("demo", "API_KEY")["removed"] is True
    assert core.read_cap_env("demo") == {}


def test_env_values_are_injected_only_for_active_tags(tmp_path, monkeypatch):
    monkeypatch.setenv("CAPFORGE_STATE_DIR", str(tmp_path / "state"))
    cap = _env_cap(tmp_path / "caps")

    placeholder_env: dict[str, str] = {}
    core._apply_cap_env(placeholder_env, "demo", cap, tag=None)
    assert placeholder_env == {name: core.ENV_PLACEHOLDER for name in cap.env}

    with pytest.raises(core.CapError) as exc_info:
        core._apply_cap_env({}, "demo", cap, tag="v1")
    assert "API_KEY" in str(exc_info.value)

    core.set_cap_env("demo", "API_KEY", "sekrit-value")
    core.set_cap_env("demo", "SMTP_PASSWORD", "smtp-secret")
    active_env: dict[str, str] = {}
    core._apply_cap_env(active_env, "demo", cap, tag="v1")
    assert active_env == {"API_KEY": "sekrit-value", "SMTP_PASSWORD": "smtp-secret"}


def test_env_rejects_reserved_names_and_oversize_values(tmp_path, monkeypatch):
    monkeypatch.setenv("CAPFORGE_STATE_DIR", str(tmp_path / "state"))
    _env_cap(tmp_path / "caps")
    for name in ("PATH", "HOME", "CAPFORGE_DATA_DIR", "PYTHONPATH", "LD_PRELOAD", "UV_INDEX"):
        with pytest.raises(core.CapError):
            core.set_cap_env("demo", name, "x")
    with pytest.raises(core.CapError):
        core.set_cap_env("demo", "API_KEY", "x" * (core.MAX_ENV_VALUE_BYTES + 1))


def test_mcp_detail_reports_env_status_without_values(tmp_path, monkeypatch):
    from capforge import server

    monkeypatch.setenv("CAPFORGE_CAPS_DIR", str(tmp_path / "caps"))
    monkeypatch.setenv("CAPFORGE_STATE_DIR", str(tmp_path / "state"))
    _env_cap(tmp_path / "caps")
    core.set_cap_env("demo", "API_KEY", "sekrit-value")

    detail = server.get_capability("demo")
    assert detail["ok"] is True
    assert detail["env"][0]["configured"] is True
    assert "sekrit-value" not in json.dumps(detail)

    listed = server.list_capabilities()
    assert "sekrit-value" not in json.dumps(listed)
    assert listed["capabilities"][0]["env"][0]["configured"] is True


def test_corrupt_env_store_is_quarantined(tmp_path, monkeypatch):
    monkeypatch.setenv("CAPFORGE_STATE_DIR", str(tmp_path / "state"))
    directory = tmp_path / "state" / "secrets"
    directory.mkdir(parents=True)
    bad = directory / "demo.json"
    bad.write_text("{", encoding="utf-8")
    assert core.read_cap_env("demo") == {}
    assert not bad.exists()
    assert list(directory.glob("demo.corrupt-*.json"))


def test_ui_bundle_is_served_under_ui(tmp_path, monkeypatch):
    import re
    from pathlib import Path

    import anyio

    import capforge.web as web

    dist = Path(__file__).resolve().parents[1] / "ui" / "dist"
    if not (dist / "index.html").is_file():
        pytest.skip("the console bundle has not been built")
    monkeypatch.setenv("CAPFORGE_WEB_UI", "1")
    monkeypatch.setenv("CAPFORGE_UI_DIR", str(dist))

    async def run() -> None:
        async with _console_client(web.asgi_app()) as client:
            root = await client.get("/", follow_redirects=False)
            assert root.status_code == 308
            assert root.headers["location"] == "/ui/"
            page = await client.get("/ui/")
            assert page.status_code == 200
            assert "/ui/assets/" in page.text
            match = re.search(r'(?:src|href)="(/ui/assets/[^"]+)"', page.text)
            assert match is not None
            asset = await client.get(match.group(1))
            assert asset.status_code == 200

    anyio.run(run)


def test_tool_surface_is_fixed_at_ten():
    from capforge.server import mcp

    names = [tool.name for tool in asyncio.run(mcp.list_tools())]
    assert names == [
        "get_contract",
        "list_capabilities",
        "get_capability",
        "scaffold_capability",
        "run_capability",
        "serve_capability",
        "staging",
        "delete_capability",
        "rollback_capability",
        "list_tags",
    ]


def test_git_auth_is_basic_only_and_actor_bound(tmp_path, monkeypatch):
    import base64

    from starlette.requests import Request

    from capforge import staging, web

    monkeypatch.setenv("CAPFORGE_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("CAPFORGE_CAPS_DIR", str(tmp_path / "caps"))
    monkeypatch.setenv("CAPFORGE_GIT_CHECK_USER", "1")
    (tmp_path / "caps").mkdir()
    minted = staging.mint("demo", {"id": "actor-1"})
    assert minted["auth"] == "basic"
    assert minted["basic_username"] == "actor-1"

    def request(header: str) -> Request:
        return Request({"type": "http", "headers": [(b"authorization", header.encode())]})

    def basic(user: str, token: str) -> str:
        raw = base64.b64encode(f"{user}:{token}".encode()).decode()
        return "Basic " + raw

    assert web._git_auth(request(basic("actor-1", minted["token"])), "demo") is not None
    assert web._git_auth(request(basic("git", minted["token"])), "demo") is None
    assert web._git_auth(request(basic("", minted["token"])), "demo") is None
    assert web._git_auth(request("Bearer " + minted["token"]), "demo") is None

    monkeypatch.setenv("CAPFORGE_GIT_CHECK_USER", "0")
    record = web._git_auth(request(basic("anyone", minted["token"])), "demo")
    assert record is not None
    assert record["basic_username"] == "anyone"


def test_git_route_accepts_basic_and_rejects_bearer(tmp_path, monkeypatch):
    import anyio
    import httpx

    import capforge.web as web

    monkeypatch.setenv("CAPFORGE_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("CAPFORGE_CAPS_DIR", str(tmp_path / "caps"))
    monkeypatch.setenv("CAPFORGE_WEB_UI", "0")
    monkeypatch.setenv("CAPFORGE_GIT_CHECK_USER", "1")
    (tmp_path / "caps").mkdir()
    from capforge import staging

    token = staging.mint("demo", {"id": "actor-1"})

    async def run() -> None:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=web.asgi_app()), base_url="http://test"
        ) as client:
            bad = await client.get(
                "/git/demo.git/info/refs?service=git-upload-pack",
                auth=httpx.BasicAuth("git", token["token"]),
            )
            assert bad.status_code == 401
            ok = await client.get(
                "/git/demo.git/info/refs?service=git-upload-pack",
                auth=httpx.BasicAuth("actor-1", token["token"]),
            )
            assert ok.status_code == 404  # authenticated; the repo does not exist
            bearer = await client.get(
                "/git/demo.git/info/refs?service=git-upload-pack",
                headers={"authorization": "Bearer " + token["token"]},
            )
            assert bearer.status_code == 401

    anyio.run(run)


def test_delete_capability_removes_state_and_tokens(tmp_path, monkeypatch):

    from capforge import server, staging

    monkeypatch.setenv("CAPFORGE_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("CAPFORGE_CAPS_DIR", str(tmp_path / "caps"))
    monkeypatch.setenv("CAPFORGE_WEB_UI", "0")
    caps = tmp_path / "caps"
    state = tmp_path / "state"
    name = "gone"
    (caps / name / "tests").mkdir(parents=True)
    (caps / name / "main.py").write_text("print(1)\n", encoding="utf-8")
    (caps / name / "tests" / "test_scaffold.py").write_text("def test_ok(): assert True\n")
    for path in (
        state / "versions" / name,
        state / "venvs" / name,
        state / "data" / name,
        state / "logs" / name,
        state / "git" / f"{name}.git",
    ):
        path.mkdir(parents=True)
    (state / "state").mkdir(parents=True)
    (state / "state" / f"{name}.json").write_text("{}", encoding="utf-8")
    (state / "staging").mkdir(parents=True)
    (state / "staging" / f"{name}.json").write_text("{}", encoding="utf-8")
    staging.mint(name, {"id": "actor-1"})

    assert server.delete_capability(name, "wrong")["ok"] is False
    result = server.delete_capability(name, name)
    assert result["ok"] and result["deleted"] is True
    assert not (caps / name).exists()
    assert not (state / "versions" / name).exists()
    assert not (state / "git" / f"{name}.git").exists()
    assert not (state / "state" / f"{name}.json").exists()
    assert not (state / "staging" / f"{name}.json").exists()
    assert all(
        value.get("name") != name
        for value in staging._read_tokens().values()
    )
    again = server.delete_capability(name, name)
    assert again["ok"] and again["deleted"] is False
