"""Fast unit tests: no virtualenvs, no capability subprocesses.

A few process-hygiene tests spawn plain `python -c` children to prove the
stray sweep; those are plain OS processes, not capability runs.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import threading
import time

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


def _skill(path, description: str = "d") -> None:
    (path / "SKILL.md").write_text(
        f"---\ndescription: {description}\n---\n", encoding="utf-8"
    )


def _deploy(
    path,
    *,
    kind: str | list[str] = "cli",
    cli_entrypoint: str | None = None,
    web_entrypoint: str | None = None,
    app: str | None = None,
    data: str = "version",
    env: str = "",
    shares: str | None = None,
) -> None:
    modes = [kind] if isinstance(kind, str) else list(kind)
    if "cli" in modes and cli_entrypoint is None:
        cli_entrypoint = "main.py"
    if "web" in modes and web_entrypoint is None:
        web_entrypoint = "app.py"
    kind_literal = f'"{kind}"' if isinstance(kind, str) else str(modes).replace("'", '"')
    lines = ["from pydantic import BaseModel", "", "", "class Env(BaseModel):"]
    if env:
        lines.extend(f"    {line}" for line in env.strip().splitlines())
    else:
        lines.append("    pass")
    lines.extend(["", "", f"KIND = {kind_literal}"])
    if cli_entrypoint is not None:
        lines.append(f'CLI_ENTRYPOINT = "{cli_entrypoint}"')
    if web_entrypoint is not None:
        lines.append(f'WEB_ENTRYPOINT = "{web_entrypoint}"')
    if app is not None:
        lines.append(f'APP = "{app}"')
    lines.append(f'DATA = "{data}"')
    if shares is not None:
        lines.append(f"SHARES = {shares}")
    (path / "deploy.py").write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_kind_parsing_and_validation(tmp_path):
    cap = tmp_path / "demo"
    cap.mkdir()
    (cap / "app.py").write_text("app = None\n", encoding="utf-8")
    (cap / "main.py").write_text("print(1)\n", encoding="utf-8")
    (cap / "pyproject.toml").write_text(
        '[project]\nname="demo"\nversion="0.1.0"\nrequires-python=">=3.14,<3.15"\ndependencies=["uvicorn"]\n',
        encoding="utf-8",
    )
    _skill(cap)
    _deploy(cap, kind="web", web_entrypoint="app.py")
    loaded = core.load_capability_at("demo", cap)
    assert loaded.kind == "web"
    assert loaded.modes == ("web",)
    assert loaded.runs_cli is False
    assert loaded.serves_web is True
    assert loaded.web_entrypoint == "app.py"
    assert loaded.cli_entrypoint is None
    assert loaded.app_name == "app"
    assert loaded.error is None

    _deploy(cap, kind=["cli", "web"])
    loaded = core.load_capability_at("demo", cap)
    assert loaded.kind == "both"
    assert loaded.modes == ("cli", "web")
    assert loaded.runs_cli is True and loaded.serves_web is True
    assert loaded.cli_entrypoint == "main.py"
    assert loaded.web_entrypoint == "app.py"
    assert loaded.error is None

    _deploy(cap, kind="worker", web_entrypoint="app.py")
    assert "KIND entries must be one of" in core.load_capability_at("demo", cap).error


def test_web_requires_uvicorn(tmp_path):
    cap = tmp_path / "demo"
    cap.mkdir()
    (cap / "app.py").write_text("app = None\n", encoding="utf-8")
    _skill(cap)
    _deploy(cap, kind="web", web_entrypoint="app.py")
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
    _skill(cap)
    _deploy(cap, kind="web", web_entrypoint="app.py", app="bad name")
    loaded = core.load_capability_at("demo", cap)
    assert loaded.error is not None
    assert "identifier" in loaded.error


def test_web_child_carries_the_public_root_path(tmp_path, monkeypatch):
    monkeypatch.setenv("CAPFORGE_STATE_DIR", str(tmp_path / "state"))
    cap = core.Capability(
        name="demo", path=tmp_path, kind="web", web_entrypoint="app.py", app_name="app"
    )
    argv = core.web_child_argv(cap, tmp_path, 1234)
    assert argv[argv.index("--root-path") + 1] == "/cap/demo"
    env = core.web_child_env(cap, tag="v1", venv=tmp_path, port=1234)
    assert env["CAPFORGE_ROOT_PATH"] == "/cap/demo"


def test_deploy_py_is_required(tmp_path):
    cap = tmp_path / "demo"
    cap.mkdir()
    (cap / "main.py").write_text("print(1)\n", encoding="utf-8")
    _skill(cap)
    loaded = core.load_capability_at("demo", cap)
    assert loaded.error is not None
    assert "deploy.py is missing" in loaded.error


def test_legacy_frontmatter_is_refused(tmp_path):
    cap = tmp_path / "demo"
    cap.mkdir()
    (cap / "main.py").write_text("print(1)\n", encoding="utf-8")
    (cap / "SKILL.md").write_text(
        "---\ndescription: d\nentrypoint: main.py\nenv: [API_KEY]\n---\n",
        encoding="utf-8",
    )
    loaded = core.load_capability_at("demo", cap)
    assert loaded.error is not None
    assert "move entrypoint, env from SKILL.md to deploy.py" in loaded.error


def test_deploy_py_parses_typed_env_fields():
    spec = core.parse_deploy_py(
        "from pydantic import BaseModel, Field\n\n"
        "class Env(BaseModel):\n"
        "    API_KEY: str = Field(min_length=3, max_length=40, description='key')\n"
        "    TIMEOUT: int = 30\n"
        "    DEBUG: bool = False\n"
        "    RATIO: float = 0.5\n"
        "    OPTIONAL: str | None = None\n"
        "    RAW: bytes\n\n"
        'KIND = ["cli"]\n'
        'CLI_ENTRYPOINT = "main.py"\n'
    )
    by_name = {item.name: item for item in spec.env}
    assert by_name["API_KEY"].type == "str"
    assert by_name["API_KEY"].required is True
    assert by_name["API_KEY"].min_length == 3
    assert by_name["API_KEY"].max_length == 40
    assert by_name["API_KEY"].description == "key"
    assert by_name["TIMEOUT"].type == "int"
    assert by_name["TIMEOUT"].default == "30"
    assert by_name["TIMEOUT"].required is False
    assert by_name["DEBUG"].default == "false"
    assert by_name["RATIO"].default == "0.5"
    assert by_name["OPTIONAL"].required is False
    assert by_name["RAW"].type == "str"


def test_deploy_py_rejects_bad_input():
    with pytest.raises(core.CapError):
        core.parse_deploy_py("KIND = os.environ['KIND']\nCLI_ENTRYPOINT = 'main.py'\n")
    with pytest.raises(core.CapError):
        core.parse_deploy_py("def broken(:\n")
    with pytest.raises(core.CapError):
        core.parse_deploy_py('CLI_ENTRYPOINT = "main.py"\n')
    with pytest.raises(core.CapError):
        core.parse_deploy_py('KIND = "cli"\n')
    with pytest.raises(core.CapError):
        core.parse_deploy_py(
            'KIND = "cli"\nCLI_ENTRYPOINT = "main.py"\nDATA = "forever"\n'
        )
    with pytest.raises(core.CapError):
        core.parse_deploy_py(
            'KIND = "cli"\nCLI_ENTRYPOINT = "main.py"\nclass Env:\n    PATH: str\n'
        )
    with pytest.raises(core.CapError):
        core.parse_deploy_py(
            'KIND = "cli"\nCLI_ENTRYPOINT = "main.py"\n'
            "class Env:\n    KEY: str\n    KEY: int\n"
        )


def test_deploy_py_requires_mode_entrypoints():
    with pytest.raises(core.CapError, match="ENTRYPOINT was renamed"):
        core.parse_deploy_py('KIND = "cli"\nENTRYPOINT = "main.py"\n')
    with pytest.raises(core.CapError, match="CLI_ENTRYPOINT is missing"):
        core.parse_deploy_py('KIND = ["cli"]\n')
    with pytest.raises(core.CapError, match="WEB_ENTRYPOINT is missing"):
        core.parse_deploy_py('KIND = ["web"]\n')
    with pytest.raises(core.CapError, match="CLI_ENTRYPOINT is set"):
        core.parse_deploy_py(
            'KIND = ["web"]\nWEB_ENTRYPOINT = "app.py"\nCLI_ENTRYPOINT = "main.py"\n'
        )
    with pytest.raises(core.CapError, match="WEB_ENTRYPOINT is set"):
        core.parse_deploy_py('KIND = ["cli"]\nCLI_ENTRYPOINT = "main.py"\n'
                             'WEB_ENTRYPOINT = "app.py"\n')
    with pytest.raises(core.CapError, match="APP is only valid"):
        core.parse_deploy_py('KIND = ["cli"]\nCLI_ENTRYPOINT = "main.py"\nAPP = "app"\n')
    with pytest.raises(core.CapError, match="more than once"):
        core.parse_deploy_py('KIND = ["cli", "cli"]\nCLI_ENTRYPOINT = "main.py"\n')
    with pytest.raises(core.CapError, match="at least one"):
        core.parse_deploy_py("KIND = []\n")
    with pytest.raises(core.CapError, match="KIND entries"):
        core.parse_deploy_py('KIND = ["worker"]\n')


def test_deploy_py_parses_both_modes():
    spec = core.parse_deploy_py(
        'from pydantic import BaseModel\n'
        "class Env(BaseModel):\n    pass\n"
        'KIND = ["cli", "web"]\n'
        'CLI_ENTRYPOINT = "main.py"\n'
        'WEB_ENTRYPOINT = "app.py"\n'
        'APP = "server"\n'
    )
    assert spec.kind == "both"
    assert spec.modes == ("cli", "web")
    assert spec.runs_cli and spec.serves_web
    assert spec.cli_entrypoint == "main.py"
    assert spec.web_entrypoint == "app.py"
    assert spec.app_name == "server"


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
            item = listed.json()["env"][0]
            assert item["name"] == "API_KEY"
            assert item["declared"] is True
            assert item["configured"] is False
            assert item["updated_at"] == 0
            assert item["type"] == "str"
            assert item["required"] is True
            assert item["default"] is None

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


def test_health_routes_are_public_and_console_is_gated(tmp_path, monkeypatch):
    import anyio

    import capforge.web as web

    monkeypatch.delenv("CAPFORGE_WEB_TOKEN", raising=False)
    monkeypatch.setenv("CAPFORGE_WEB_UI", "0")

    async def run() -> None:
        async with _console_client(web.asgi_app()) as client:
            assert (await client.get("/healthz")).status_code == 200
            assert (await client.get("/api/caps")).status_code == 403
            assert (await client.get("/ui/")).status_code == 404
            assert (await client.get("/cap/demo/")).status_code == 404
            assert (await client.get("/git/demo.git/info/refs")).status_code == 403

    anyio.run(run)


def test_health_and_readiness(tmp_path, monkeypatch):
    import anyio

    import capforge.web as web

    monkeypatch.setenv("CAPFORGE_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("CAPFORGE_WEB_UI", "0")
    monkeypatch.setenv("CAPFORGE_SANDBOX", "off")
    (tmp_path / "state").mkdir()
    web._ready_cache = None

    async def healthy() -> tuple[int, int, dict]:
        async with _console_client(web.asgi_app()) as client:
            health = await client.get("/healthz")
            ready = await client.get("/readyz")
            return health.status_code, ready.status_code, ready.json()

    statuses = anyio.run(healthy)
    assert statuses[0] == 200 and statuses[1] == 200
    assert statuses[2]["checks"]["state_dir"] is True

    # A missing runtime tool makes readiness fail; liveness stays green.
    import capforge.web as web_module

    monkeypatch.setattr(
        web_module.shutil,
        "which",
        lambda name: None if name == "uv" else f"/usr/bin/{name}",
    )
    web_module._ready_cache = None

    async def unhealthy() -> tuple[int, int]:
        async with _console_client(web_module.asgi_app()) as client:
            return (
                (await client.get("/healthz")).status_code,
                (await client.get("/readyz")).status_code,
            )

    assert anyio.run(unhealthy) == (200, 503)


def test_mcp_token_verifier():
    import anyio

    from capforge.server import _McpTokenVerifier

    verifier = _McpTokenVerifier("correct-token")

    async def check() -> tuple[bool, bool, bool]:
        good = await verifier.verify_token("correct-token")
        bad = await verifier.verify_token("wrong-token")
        empty = await verifier.verify_token("")
        return good is not None, bad is None, empty is None

    assert anyio.run(check) == (True, True, True)


def test_git_timeout_is_configurable(monkeypatch):
    monkeypatch.setenv("CAPFORGE_GIT_TIMEOUT", "42")
    assert core.git_timeout() == 42
    monkeypatch.setenv("CAPFORGE_GIT_TIMEOUT", "not-a-number")
    assert core.git_timeout() == core.DEFAULT_GIT_TIMEOUT


def test_backup_cleanup_and_sweep(tmp_path, monkeypatch):
    import capforge.staging as staging

    monkeypatch.setenv("CAPFORGE_CAPS_DIR", str(tmp_path / "caps"))
    monkeypatch.setenv("CAPFORGE_STATE_DIR", str(tmp_path / "state"))
    (tmp_path / "caps").mkdir()
    backups = tmp_path / "caps" / ".staging" / "backups"
    backups.mkdir(parents=True)
    live = backups / "demo-20260101-000000"
    stale = backups / "demo-20250101-000000"
    other = backups / "other-20250101-000000"
    for entry in (live, stale, other):
        entry.mkdir()

    staging.cleanup_backups("demo")
    assert not live.exists() and not stale.exists() and other.exists()

    old = time.time() - 2 * 24 * 3600
    os.utime(other, (old, old))
    assert staging.sweep_backups() == ["other-20250101-000000"]
    assert not other.exists()


def test_push_state_check_and_start_are_under_the_push_lock(tmp_path, monkeypatch):
    """A second push must see the first pipeline's state before running its CGI."""
    import anyio
    import httpx

    import capforge.web as web
    from capforge import gitremote, staging

    monkeypatch.setenv("CAPFORGE_CAPS_DIR", str(tmp_path / "caps"))
    monkeypatch.setenv("CAPFORGE_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("CAPFORGE_WEB_UI", "0")
    (tmp_path / "caps").mkdir()
    (tmp_path / "state").mkdir()

    calls = {"cgi": 0}
    in_cgi = threading.Event()
    release = threading.Event()

    monkeypatch.setattr(
        web,
        "_git_auth",
        lambda request, name: {
            "actor": {"id": "tester"},
            "auth_method": "basic",
            "basic_username": "tester",
        },
    )

    async def fake_body(request, limit):
        return b"push"

    monkeypatch.setattr(web, "_read_limited_body", fake_body)

    def fake_cgi(name, service, request, body, actor):
        calls["cgi"] += 1
        in_cgi.set()
        release.wait(timeout=5)
        return web.Response("ok", status_code=200)

    monkeypatch.setattr(web, "_run_cgi", fake_cgi)
    shas = iter(["before", "after"])
    monkeypatch.setattr(gitremote, "rev_parse", lambda name: next(shas, "after"))

    def fake_start(name, sha, *args, **kwargs):
        staging.write_state(name, {"name": name, "state": "received", "sha": sha})
        return {"ok": True}

    monkeypatch.setattr(staging, "start", fake_start)

    async def run() -> list[int]:
        transport = httpx.ASGITransport(app=web.asgi_app())
        async with httpx.AsyncClient(transport=transport, base_url="http://probe") as client:

            async def push():
                return await client.post("/git/demo.git/git-receive-pack", content=b"push")

            first = asyncio.create_task(push())
            await asyncio.to_thread(in_cgi.wait, 5)
            second = asyncio.create_task(push())
            await asyncio.sleep(0.1)
            release.set()
            responses = await asyncio.gather(first, second)
            return [response.status_code for response in responses]

    assert anyio.run(run) == [200, 409]
    assert calls["cgi"] == 1


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
    _skill(cap)
    _deploy(cap, cli_entrypoint="../escape.py")
    loaded = core.load_capability_at("demo", cap)
    assert loaded.error is not None
    assert "escapes" in loaded.error


def test_absolute_entrypoint_is_rejected(tmp_path):
    cap = tmp_path / "demo"
    cap.mkdir()
    _skill(cap)
    _deploy(cap, cli_entrypoint="/etc/passwd")
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


def _env_cap(
    root,
    name: str = "demo",
    fields: str = "API_KEY: str\nSMTP_PASSWORD: str",
):
    cap = root / name
    cap.mkdir(parents=True)
    (cap / "main.py").write_text("print(1)\n", encoding="utf-8")
    _skill(cap)
    _deploy(cap, env=fields)
    return core.load_capability_at(name, cap)


def _share_cap(root, name: str = "demo", shares: str = '{"datasets": "ro"}'):
    cap = root / name
    cap.mkdir(parents=True)
    (cap / "main.py").write_text("print(1)\n", encoding="utf-8")
    _skill(cap)
    _deploy(cap, shares=shares)
    return core.load_capability_at(name, cap)


def test_env_declarations_are_parsed_and_validated(tmp_path):
    cap = _env_cap(tmp_path)
    assert cap.error is None
    assert cap.env == ["API_KEY", "SMTP_PASSWORD"]

    single = _env_cap(tmp_path, "single", "API_KEY: str")
    assert single.env == ["API_KEY"]

    reserved = _env_cap(tmp_path, "reserved", "PATH: str")
    assert reserved.error is not None and "reserved" in reserved.error


def test_env_store_is_private_and_shared_across_tags(tmp_path, monkeypatch):
    monkeypatch.setenv("CAPFORGE_STATE_DIR", str(tmp_path / "state"))
    cap = _env_cap(tmp_path / "caps")

    status = core.env_status("demo", [cap])
    assert [item["name"] for item in status] == ["API_KEY", "SMTP_PASSWORD"]
    assert all(item["declared"] and not item["configured"] for item in status)
    assert status[0]["type"] == "str"
    assert status[0]["required"] is True
    assert status[0]["default"] is None
    assert status[0]["description"] == ""

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
    monkeypatch.setenv("CAPFORGE_CAPS_DIR", str(tmp_path / "caps"))
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


def test_typed_env_values_are_validated_and_defaults_injected(tmp_path, monkeypatch):
    monkeypatch.setenv("CAPFORGE_CAPS_DIR", str(tmp_path / "caps"))
    monkeypatch.setenv("CAPFORGE_STATE_DIR", str(tmp_path / "state"))
    cap = _env_cap(
        tmp_path / "caps",
        "typed",
        "API_KEY: str = Field(min_length=2, max_length=4)\n"
        "TIMEOUT: int = 30\n"
        "DEBUG: bool\n"
        "RATIO: float\n"
        "LEVEL: int = Field(default=2, ge=1, le=3)",
    )
    assert cap.error is None
    assert core.cap_env_missing("typed", cap) == ["API_KEY", "DEBUG", "RATIO"]
    assert core.cap_env_placeholders(cap) == {
        "API_KEY": core.ENV_PLACEHOLDER,
        "TIMEOUT": "30",
        "DEBUG": "false",
        "RATIO": "0.0",
        "LEVEL": "2",
    }

    core.set_cap_env("typed", "API_KEY", "abcd")
    core.set_cap_env("typed", "DEBUG", "yes")
    core.set_cap_env("typed", "RATIO", "1.5")
    assert core.cap_env_values("typed", cap) == {
        "API_KEY": "abcd",
        "TIMEOUT": "30",
        "DEBUG": "true",
        "RATIO": "1.5",
        "LEVEL": "2",
    }
    for key, value in (
        ("TIMEOUT", "soon"),
        ("DEBUG", "maybe"),
        ("API_KEY", "a"),
        ("API_KEY", "abcde"),
        ("LEVEL", "0"),
        ("LEVEL", "4"),
    ):
        with pytest.raises(core.CapError):
            core.set_cap_env("typed", key, value)


def test_agents_example_matches_the_scaffold_template():
    from pathlib import Path

    from capforge import contract

    root = Path(__file__).resolve().parents[1]
    assert (root / "AGENTS.md.example").read_text(encoding="utf-8") == contract.AGENTS_TEMPLATE


def test_env_rejects_reserved_names_and_oversize_values(tmp_path, monkeypatch):
    monkeypatch.setenv("CAPFORGE_CAPS_DIR", str(tmp_path / "caps"))
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
    backup = caps / ".staging" / "backups" / f"{name}-20260101-000000"
    backup.mkdir(parents=True)
    staging.mint(name, {"id": "actor-1"})

    assert server.delete_capability(name, "wrong")["ok"] is False
    result = server.delete_capability(name, name)
    assert result["ok"] and result["deleted"] is True
    assert not (caps / name).exists()
    assert not (state / "versions" / name).exists()
    assert not (state / "git" / f"{name}.git").exists()
    assert not (state / "state" / f"{name}.json").exists()
    assert not (state / "staging" / f"{name}.json").exists()
    assert not backup.exists()
    assert all(
        value.get("name") != name
        for value in staging._read_tokens().values()
    )
    again = server.delete_capability(name, name)
    assert again["ok"] and again["deleted"] is False


# ------------------------------------------------------------------ shared volumes


def test_deploy_py_parses_and_validates_shares():
    spec = core.parse_deploy_py(
        'KIND = ["cli"]\nCLI_ENTRYPOINT = "main.py"\n'
        'SHARES = {"datasets": "ro", "index-cache": "rw"}\n'
    )
    assert [(item.name, item.access) for item in spec.shares] == [
        ("datasets", "ro"),
        ("index-cache", "rw"),
    ]
    empty = core.parse_deploy_py('KIND = ["cli"]\nCLI_ENTRYPOINT = "main.py"\n')
    assert empty.shares == ()
    for text in (
        'KIND = ["cli"]\nCLI_ENTRYPOINT = "main.py"\nSHARES = ["datasets"]\n',
        'KIND = ["cli"]\nCLI_ENTRYPOINT = "main.py"\nSHARES = {"Bad": "ro"}\n',
        'KIND = ["cli"]\nCLI_ENTRYPOINT = "main.py"\nSHARES = {"datasets": "write"}\n',
        'KIND = ["cli"]\nCLI_ENTRYPOINT = "main.py"\nSHARES = {"x": "ro", " x ": "rw"}\n',
        'KIND = ["cli"]\nCLI_ENTRYPOINT = "main.py"\nSHARES = get_shares()\n',
    ):
        with pytest.raises(core.CapError):
            core.parse_deploy_py(text)


def test_share_registry_grants_census_and_delete(tmp_path, monkeypatch):
    monkeypatch.setenv("CAPFORGE_CAPS_DIR", str(tmp_path / "caps"))
    monkeypatch.setenv("CAPFORGE_STATE_DIR", str(tmp_path / "state"))
    cap = _share_cap(tmp_path / "caps", "demo", '{"datasets": "ro"}')

    created = core.create_share("datasets", "reference data")
    assert created["created"] is True
    assert (tmp_path / "state" / "shares" / "datasets").is_dir()
    with pytest.raises(core.CapError):
        core.create_share("datasets")

    census = core.share_census({"datasets": {"demo": "ro"}})
    folder = census["folders"][0]
    assert folder["name"] == "datasets"
    assert folder["note"] == "reference data"
    assert folder["requests"][0]["state"] == "pending"
    assert folder["requests"][0]["requested"] == "ro"

    approved = core.approve_share_request("demo", "datasets", "ro")
    assert approved["access"] == "ro"
    status = core.share_status("demo", [cap])
    assert status[0]["state"] == "approved"
    assert status[0]["access"] == "ro"
    assert status[0]["requested"] == "ro"
    assert core.approved_shares("demo") == {
        "datasets": {"access": "ro", "approved_at": approved["approved_at"]}
    }

    # Deleting a granted folder is refused; revoking makes it deletable.
    with pytest.raises(core.CapError):
        core.delete_share("datasets", "datasets")
    with pytest.raises(core.CapError):
        core.delete_share("datasets", "wrong")
    assert core.revoke_share_grant("demo", "datasets")["revoked"] is True
    deleted = core.delete_share("datasets", "datasets")
    assert deleted["deleted"] is True
    assert not (tmp_path / "state" / "shares" / "datasets").exists()
    assert core.share_census({})["folders"] == []


def test_share_resolution_fails_closed_and_builds_policy(tmp_path, monkeypatch):
    monkeypatch.setenv("CAPFORGE_CAPS_DIR", str(tmp_path / "caps"))
    monkeypatch.setenv("CAPFORGE_STATE_DIR", str(tmp_path / "state"))
    cap = _share_cap(
        tmp_path / "caps", "demo", '{"datasets": "ro", "cache": "rw"}'
    )
    core.create_share("datasets")
    core.create_share("cache")

    with pytest.raises(core.CapError) as missing:
        core.resolve_shares("demo", cap)
    assert "not approved" in str(missing.value)

    core.set_share_grant("demo", "datasets", "ro")
    core.set_share_grant("demo", "cache", "ro")
    with pytest.raises(core.CapError) as weaker:
        core.resolve_shares("demo", cap)
    assert "read-only" in str(weaker.value)

    core.set_share_grant("demo", "cache", "rw")
    mounts = core.resolve_shares("demo", cap)
    by_name = {mount.name: mount for mount in mounts}
    assert by_name["datasets"].access == "ro"
    assert by_name["cache"].access == "rw"
    assert core.share_path("datasets").is_dir()

    policy = core._sandbox_policy(
        tmp_path,
        tmp_path / "venv",
        tmp_path / "data",
        shares=mounts,
    )
    assert by_name["datasets"].path in policy.read
    assert by_name["cache"].path in policy.write

    placeholders = core.test_share_mounts(cap, tmp_path / "data" / "test")
    assert {mount.access for mount in placeholders} == {"ro", "rw"}
    assert all(mount.path.is_dir() for mount in placeholders)
    assert all(
        str(tmp_path / "state" / "shares") not in str(mount.path)
        for mount in placeholders
    )


def test_share_names_and_corrupt_registry_are_contained(tmp_path, monkeypatch):
    monkeypatch.setenv("CAPFORGE_STATE_DIR", str(tmp_path / "state"))
    for bad in ("../escape", "Upper", "with space", "..", ""):
        with pytest.raises(core.CapError):
            core.share_path(bad)
    registry = tmp_path / "state" / "state" / "shares.json"
    registry.parent.mkdir(parents=True)
    registry.write_text("{not json", encoding="utf-8")
    assert core.read_share_registry() == {"version": 1, "folders": {}}
    assert not registry.exists()
    assert list(registry.parent.glob("shares.corrupt-*.json"))


def test_console_share_api(tmp_path, monkeypatch):
    import importlib

    import anyio

    import capforge.core as core_module
    import capforge.web as web

    # Later slow tests reload core; keep the exception class in web aligned with it.
    importlib.reload(core_module)
    importlib.reload(web)

    monkeypatch.setenv("CAPFORGE_CAPS_DIR", str(tmp_path / "caps"))
    monkeypatch.setenv("CAPFORGE_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("CAPFORGE_WEB_TOKEN", "sekrit")
    monkeypatch.setenv("CAPFORGE_WEB_UI", "0")
    _share_cap(tmp_path / "caps", "demo", '{"datasets": "ro"}')

    async def run() -> None:
        async with _console_client(web.asgi_app()) as client:
            assert (await client.get("/api/shares")).status_code == 401
            login = await client.post("/api/login", json={"token": "sekrit"})
            headers = {"cookie": f"capforge_session={login.cookies['capforge_session']}"}

            census = await client.get("/api/shares", headers=headers)
            assert census.status_code == 200
            folder = census.json()["folders"][0]
            assert folder["name"] == "datasets"
            assert folder["exists"] is False
            assert folder["requests"][0]["state"] == "pending"

            # Approval needs CSRF.
            assert (
                await client.put(
                    "/api/caps/demo/shares/datasets",
                    headers=headers,
                    json={"access": "ro"},
                )
            ).status_code == 403

            bad_access = await client.put(
                "/api/caps/demo/shares/datasets",
                headers={**headers, "x-csrf": "1"},
                json={"access": "write"},
            )
            assert bad_access.status_code == 400
            upgraded = await client.put(
                "/api/caps/demo/shares/datasets",
                headers={**headers, "x-csrf": "1"},
                json={"access": "rw"},
            )
            assert upgraded.status_code == 400

            approved = await client.put(
                "/api/caps/demo/shares/datasets",
                headers={**headers, "x-csrf": "1"},
                json={"access": "ro"},
            )
            assert approved.status_code == 200, approved.text
            assert approved.json()["shares"][0]["state"] == "approved"
            assert (tmp_path / "state" / "shares" / "datasets").is_dir()

            undeclared = await client.put(
                "/api/caps/demo/shares/other",
                headers={**headers, "x-csrf": "1"},
                json={"access": "ro"},
            )
            assert undeclared.status_code == 400

            detail = await client.get("/api/caps/demo", headers=headers)
            assert detail.json()["shares"][0]["state"] == "approved"
            listed = await client.get("/api/caps/demo/shares", headers=headers)
            assert listed.status_code == 200
            assert listed.json()["shares"][0]["state"] == "approved"

            revoked = await client.delete(
                "/api/caps/demo/shares/datasets",
                headers={**headers, "x-csrf": "1"},
            )
            assert revoked.status_code == 200, revoked.text
            assert revoked.json()["revoked"] is True
            assert revoked.json()["shares"][0]["state"] == "pending"

            deleted = await client.request(
                "DELETE",
                "/api/shares/datasets",
                headers={**headers, "x-csrf": "1"},
                json={"confirm": "wrong"},
            )
            assert deleted.status_code == 400
            deleted = await client.request(
                "DELETE",
                "/api/shares/datasets",
                headers={**headers, "x-csrf": "1"},
                json={"confirm": "datasets"},
            )
            assert deleted.status_code == 200, deleted.text
            after = {item["name"]: item for item in deleted.json()["folders"]}
            assert after["datasets"]["exists"] is False
            assert after["datasets"]["requests"][0]["state"] == "pending"

            created = await client.post(
                "/api/shares",
                headers={**headers, "x-csrf": "1"},
                json={"name": "cache", "note": "scratch"},
            )
            assert created.status_code == 200, created.text
            named = {item["name"]: item for item in created.json()["folders"]}
            assert named["cache"]["note"] == "scratch"

    anyio.run(run)


def test_delete_capability_revokes_grants_but_keeps_folder(tmp_path, monkeypatch):
    from capforge import server

    monkeypatch.setenv("CAPFORGE_CAPS_DIR", str(tmp_path / "caps"))
    monkeypatch.setenv("CAPFORGE_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("CAPFORGE_WEB_UI", "0")
    _share_cap(tmp_path / "caps", "demo", '{"datasets": "ro"}')
    core.create_share("datasets")
    core.set_share_grant("demo", "datasets", "ro")

    result = server.delete_capability("demo", "demo")
    assert result["ok"] is True, result
    assert result["shares_revoked"] == 1
    assert (tmp_path / "state" / "shares" / "datasets").is_dir()
    assert core.approved_shares("demo") == {}
