"""Slow web lifecycle tests: real venvs, real uvicorn children, real proxies.

Covers the contract that matters for kind: web: promote gates on a boot probe,
serve runs the active tag (never the draft), restart moves to the new tag,
prune stops a pinned instance without deadlocking, a call leaves no processes
behind it (double-fork daemon included), and the console API gates on the token.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

import pytest

pytestmark = pytest.mark.slow


@pytest.fixture()
def env(monkeypatch):
    root = Path(tempfile.mkdtemp(prefix="capforge-web-"))
    monkeypatch.setenv("CAPFORGE_CAPS_DIR", str(root / "caps"))
    monkeypatch.setenv("CAPFORGE_STATE_DIR", str(root / "state"))
    monkeypatch.setenv("CAPFORGE_KEEP_TAGS", "3")
    monkeypatch.setenv("CAPFORGE_RUN_TIMEOUT", "60")
    monkeypatch.setenv("CAPFORGE_TEST_TIMEOUT", "180")
    monkeypatch.setenv("CAPFORGE_SERVE_BOOT_TIMEOUT", "60")
    # A real port value keeps serving enabled, but nothing binds it: HTTP
    # assertions drive web.asgi_app() in-process through httpx.ASGITransport.
    monkeypatch.setenv("CAPFORGE_WEB_PORT", "18999")
    monkeypatch.delenv("CAPFORGE_WEB_TOKEN", raising=False)
    monkeypatch.setenv("CAPFORGE_WEB_UI", "0")
    (root / "caps").mkdir(parents=True)
    import importlib

    from capforge import core, server, versioning, web

    core.set_subreaper()
    importlib.reload(core)
    importlib.reload(versioning)
    importlib.reload(web)
    importlib.reload(server)
    core.set_subreaper()
    yield root, server, web, core
    web.shutdown()
    shutil.rmtree(root, ignore_errors=True)


def _web_cap_source(name: str, extra: str = "") -> str:
    return (
        "from fastapi import FastAPI\n"
        "app = FastAPI()\n\n\n"
        "@app.get(\"/\")\n"
        "def index() -> dict:\n"
        f"    return {{\"service\": \"{name}\"}}\n\n\n"
        '@app.get("/health")\n'
        "def health() -> dict:\n"
        '    return {"healthy": True}\n'
        + extra
    )


def _make_web_cap(env, name: str, *, tag: str = "v1", extra: str = "") -> None:
    _, server, _, _ = env
    made = server.scaffold_capability(name, f"{name} web service.", kind="web")
    assert made["ok"], made
    app_py = Path(made["path"]) / "app.py"
    app_py.write_text(_web_cap_source(name, extra), encoding="utf-8")
    tested = server.test_capability(name)
    assert tested["ok"], tested
    promo = server.promote_capability(name, tag=tag)
    assert promo["ok"], promo


def _asgi_get(web, path: str) -> tuple[int, str]:
    """GET through the console ASGI app in-process (no listener is bound)."""
    import anyio
    import httpx

    async def run() -> tuple[int, str]:
        transport = httpx.ASGITransport(app=web.asgi_app(), client=("127.0.0.1", 41000))
        async with httpx.AsyncClient(transport=transport, base_url="http://probe") as client:
            resp = await client.get(path)
            return resp.status_code, resp.text

    return anyio.run(run)


def test_web_lifecycle_proxy_and_restart(env):
    root, server, web, _core = env
    _make_web_cap(env, "websvc")
    started = server.serve_capability("websvc", action="start")
    assert started["ok"], started
    assert started["status"] == "running"
    assert started["url"].endswith("/cap/websvc/")
    assert {e["path"] for e in started["endpoints"]} == {"/", "/health"}

    code, body = _asgi_get(web, "/cap/websvc/health")
    assert code == 200 and '"healthy":true' in body.replace(" ", ""), body
    assert web._registry_record("websvc")["desired"] == "start"

    # A draft edit must not affect the running instance (pinned to the tag).
    app_py = Path(server.get_capability("websvc")["path"]) / "app.py"
    app_py.write_text("raise SystemExit(1)\n", encoding="utf-8")
    status = server.serve_capability("websvc", action="status")
    assert status["status"] == "running"

    # Promote v2, then restart moves the instance to the new tag.
    app_py.write_text(
        _web_cap_source(
            "websvc",
            '@app.get("/v2")\ndef v2() -> dict:\n    return {"v": 2}\n',
        ),
        encoding="utf-8",
    )
    assert server.test_capability("websvc")["ok"]
    assert server.promote_capability("websvc", tag="v2")["ok"]
    status = server.serve_capability("websvc", action="status")
    assert status["status"] == "running" and status["version"] == "v1" and status["stale"]
    restarted = server.serve_capability("websvc", action="restart")
    assert restarted["ok"], restarted
    assert restarted["version"] == "v2"

    stopped = server.serve_capability("websvc", action="stop")
    assert stopped["status"] == "stopped"
    assert web._registry_record("websvc")["desired"] == "stop"
    assert web._INSTANCES == {}


def test_delete_capability_stops_served_child(env):
    root, server, web, _core = env
    _make_web_cap(env, "deleted")
    started = server.serve_capability("deleted", action="start")
    assert started["status"] == "running"
    result = server.delete_capability("deleted", "deleted")
    assert result["ok"], result
    assert result["deleted"] is True
    assert server.get_capability("deleted")["ok"] is False
    assert "deleted" not in web._INSTANCES
    assert web._registry_record("deleted") is None
    assert not (root / "caps" / "deleted").exists()


def test_serve_refuses_draft_and_cli(env):
    _, server, _, _ = env
    # A CLI capability is refused even after promotion: kind is a tag property.
    assert server.scaffold_capability("plaincli", "A cli cap.")["ok"]
    assert server.test_capability("plaincli")["ok"]
    assert server.promote_capability("plaincli", tag="v1")["ok"]
    refused = server.serve_capability("plaincli", action="start")
    assert refused["ok"] is False and "not a web capability" in refused["error"], refused

    made = server.scaffold_capability("webdraft", "Draft web.", kind="web")
    assert made["ok"]
    refused = server.serve_capability("webdraft", action="start")
    assert refused["ok"] is False and "Promote" in refused["error"], refused


def test_boot_probe_blocks_promotion_of_dead_app(env):
    _, server, _, _ = env
    made = server.scaffold_capability("deadweb", "Fails at boot.", kind="web")
    assert made["ok"]
    app_py = Path(made["path"]) / "app.py"
    app_py.write_text("import nonexistent_module_xyz\napp = None\n", encoding="utf-8")
    (Path(made["path"]) / "tests" / "test_scaffold.py").unlink()
    tested = server.test_capability("deadweb")
    assert tested["ok"] is False
    assert "boot" in str(tested.get("tests", {}).get("stderr", "")).lower()
    promo = server.promote_capability("deadweb", tag="v1")
    assert promo["ok"] is False


def test_stop_kills_whole_process_group(env):
    root, server, web, core = env
    # The child spawns an extra process inside its own group; stopping the
    # instance must kill both.
    extra = (
        "import subprocess, sys, time\n"
        "_child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(600)'])\n"
    )
    _make_web_cap(env, "groupkill", extra=extra)
    started = server.serve_capability("groupkill", action="start")
    assert started["status"] == "running"
    pid = started["pid"]
    children = _descendants(pid)
    assert children, "expected the web child to have spawned a grandchild"
    stopped = server.serve_capability("groupkill", action="stop")
    assert stopped["status"] == "stopped"
    assert _descendants(pid) == set()
    for child in children:
        assert not _alive(child), f"grandchild {child} survived the stop"


def test_call_leaves_no_daemon_behind(env):
    """Double-forked daemon escape is adopted by the subreaper and swept."""
    root, server, _, core = env
    assert server.scaffold_capability("daemon", "Spawns a daemon.")["ok"]
    main = Path(server.get_capability("daemon")["path"]) / "main.py"
    main.write_text(
        "import json, os, sys\n"
        "if os.fork() > 0:\n"
        "    sys.exit(0)\n"  # parent exits; the middle process double-forks
        "if os.fork() > 0:\n"
        "    os._exit(0)\n"
        "os.setsid()\n"
        "null = os.open(os.devnull, os.O_RDWR)\n"
        "os.dup2(null, 0); os.dup2(null, 1); os.dup2(null, 2)\n"
        "if null > 2: os.close(null)\n"
        "import time\n"
        "time.sleep(600)\n",
        encoding="utf-8",
    )
    before = set(_direct_children(os.getpid()))
    result = server.run_capability("daemon", {})
    assert result["ok"], result
    # execute() sweeps after the call itself; another sweep must find nothing,
    # and the adopted daemon must be gone.
    assert core.sweep_stray_children() == []
    after = set(_direct_children(os.getpid())) - before
    assert not after, f"stray processes survived: {after}"


def test_owned_web_children_survive_sweeps(env):
    _, server, web, core = env
    _make_web_cap(env, "ownedsvc")
    started = server.serve_capability("ownedsvc", action="start")
    assert started["status"] == "running"
    pid = started["pid"]
    assert pid in set(_direct_children(os.getpid()))
    for _ in range(3):
        assert core.sweep_stray_children() == [], "the sweep killed an owned instance"
    assert server.serve_capability("ownedsvc", action="status")["status"] == "running"


def test_prune_stops_pinned_instance_without_deadlock(env):
    _, server, web, _core = env
    _make_web_cap(env, "pruned")
    started = server.serve_capability("pruned", action="start")
    assert started["status"] == "running"
    # Push the pinned tag (v1) out of the retention window. The instance stays
    # pinned to v1 because there is no restart, so pruning v1 must stop it.
    # prune runs while holding cap_lock(name); stop_pinned must not reacquire it.
    for tag in ("v2", "v3", "v4", "v5"):
        app_py = Path(server.get_capability("pruned")["path"]) / "app.py"
        app_py.write_text(
            _web_cap_source(
                "pruned",
                f'@app.get("/{tag}")\ndef {tag}() -> dict:\n    return {{"tag": "{tag}"}}\n',
            ),
            encoding="utf-8",
        )
        assert server.test_capability("pruned")["ok"], tag
        promo = server.promote_capability("pruned", tag=tag)
        assert promo["ok"] and promo.get("promoted") is not False, promo
    status = server.serve_capability("pruned", action="status")
    assert status["status"] == "stopped", status
    assert web._INSTANCES == {}
    assert web._registry_record("pruned")["desired"] == "stop"


def test_restore_respawns_desired_start(env, monkeypatch):
    _, server, web, _core = env
    _make_web_cap(env, "restore")
    started = server.serve_capability("restore", action="start")
    assert started["status"] == "running"
    web.shutdown()  # simulates a server restart: children die, registry keeps desired=start
    assert web._INSTANCES == {}
    assert web._registry_record("restore")["desired"] == "start"
    restored = web.restore()
    assert restored == 1
    status = server.serve_capability("restore", action="status")
    assert status["status"] == "running"
    server.serve_capability("restore", action="stop")


def test_restore_marks_missing_version_stop(env):
    _, server, web, _core = env
    web._persist_record(
        "ghost", {"name": "ghost", "tag": "v1", "desired": "start", "packages": []}
    )
    assert web.restore() == 0
    assert web._registry_record("ghost")["desired"] == "stop"


def test_console_control_round_trip(env, monkeypatch):
    root, server, web, _core = env
    monkeypatch.setenv("CAPFORGE_WEB_TOKEN", "round-trip-token")
    _make_web_cap(env, "consolecap")
    import anyio
    import httpx

    async def run() -> None:
        app = web.asgi_app()
        transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 41000))
        async with httpx.AsyncClient(transport=transport, base_url="http://x") as client:
            assert (await client.get("/api/caps")).status_code == 401
            login = await client.post("/api/login", json={"token": "round-trip-token"})
            assert login.status_code == 200
            caps = (await client.get("/api/caps")).json()["caps"]
            entry = next(c for c in caps if c["name"] == "consolecap")
            assert entry["kind"] == "web" and entry["served"] is None
            start = await client.post(
                "/api/caps/consolecap/start", headers={"x-csrf": "1"}
            )
            assert start.status_code == 200 and start.json()["status"] == "running", start.text
            caps = (await client.get("/api/caps")).json()["caps"]
            entry = next(c for c in caps if c["name"] == "consolecap")
            assert entry["served"]["status"] == "running"
            assert entry["served"]["endpoints"], "openapi endpoints should be listed"
            stop = await client.post("/api/caps/consolecap/stop", headers={"x-csrf": "1"})
            assert stop.status_code == 200 and stop.json()["status"] == "stopped"
            restart = await client.post("/api/caps/consolecap/restart", headers={"x-csrf": "1"})
            assert restart.status_code == 200 and restart.json()["status"] == "running"
            unknown = await client.post("/api/caps/consolecap/pause", headers={"x-csrf": "1"})
            assert unknown.status_code == 404

    anyio.run(run)
    server.serve_capability("consolecap", action="stop")


def test_console_json_index_and_disabled_console(env, monkeypatch):
    _, server, web, _core = env
    _make_web_cap(env, "indexcap")
    server.serve_capability("indexcap", action="start")
    import anyio
    import httpx

    async def run() -> None:
        app = web.asgi_app()  # no token: console disabled, proxy unaffected
        transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 41000))
        async with httpx.AsyncClient(transport=transport, base_url="http://x") as client:
            index = await client.get("/")
            assert index.status_code == 200
            body = index.json()
            assert body["service"] == "capforge" and body["ui"] is False
            assert body["capabilities"] == [
                {
                    "name": "indexcap",
                    "status": "running",
                    "url": body["capabilities"][0]["url"],
                    "version": "v1",
                }
            ]
            assert (await client.get("/cap/indexcap/health")).status_code == 200
            assert (await client.post("/api/login", json={"token": "x"})).status_code == 403

    anyio.run(run)
    server.serve_capability("indexcap", action="stop")


def test_proxy_passes_through_methods_and_headers(env):
    root, server, web, _core = env
    extra = (
        "from fastapi import Request\n\n\n"
        '@app.post("/echo")\n'
        "async def echo(request: Request) -> dict:\n"
        "    body = await request.json()\n"
        '    return {"echo": body, "marker": request.headers.get("x-marker")}\n'
    )
    _make_web_cap(env, "echocap", extra=extra)
    started = server.serve_capability("echocap", action="start")
    assert started["status"] == "running"
    import anyio
    import httpx

    async def run() -> None:
        app = web.asgi_app()
        transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 41000))
        async with httpx.AsyncClient(transport=transport, base_url="http://x") as client:
            big = "x" * (33 * 1024 * 1024)
            too_big = await client.post("/cap/echocap/echo", content=big.encode())
            assert too_big.status_code == 413
            ok = await client.post(
                "/cap/echocap/echo", json={"a": 1}, headers={"x-marker": "yes"}
            )
            assert ok.status_code == 200, ok.text
            assert ok.json() == {"echo": {"a": 1}, "marker": "yes"}
            not_found = await client.get("/cap/echocap/nope")
            assert not_found.status_code == 404

    anyio.run(run)
    server.serve_capability("echocap", action="stop")


def test_env_change_restarts_running_web_cap(env, monkeypatch):
    root, server, web, core = env
    made = server.scaffold_capability("envweb", "Reads an operator env value.", kind="web")
    assert made["ok"], made
    cap = root / "caps" / "envweb"
    (cap / "app.py").write_text(
        _web_cap_source(
            "envweb",
            "import os\n\n\n"
            '@app.get("/key")\n'
            "def key() -> dict:\n"
            '    return {"key": os.environ.get("DEMO_KEY")}\n',
        ),
        encoding="utf-8",
    )
    skill = cap / "SKILL.md"
    skill.write_text(
        skill.read_text(encoding="utf-8").replace("# env: [API_KEY]", "env: [DEMO_KEY]"),
        encoding="utf-8",
    )
    core.set_cap_env("envweb", "DEMO_KEY", "one")

    assert server.test_capability("envweb")["ok"]
    assert server.promote_capability("envweb", tag="v1")["ok"]
    started = server.serve_capability("envweb", action="start")
    assert started["status"] == "running"
    code, body = _asgi_get(web, "/cap/envweb/key")
    assert code == 200 and '"key":"one"' in body.replace(" ", ""), body

    monkeypatch.setenv("CAPFORGE_WEB_TOKEN", "round-trip-token")
    import anyio
    import httpx

    async def run() -> None:
        transport = httpx.ASGITransport(app=web.asgi_app(), client=("127.0.0.1", 41000))
        async with httpx.AsyncClient(transport=transport, base_url="http://x") as client:
            login = await client.post("/api/login", json={"token": "round-trip-token"})
            assert login.status_code == 200
            headers = {"X-CSRF": "1", "cookie": f"capforge_session={login.cookies['capforge_session']}"}
            saved = await client.post(
                "/api/caps/envweb/env",
                headers=headers,
                json={"name": "DEMO_KEY", "value": "two"},
            )
            assert saved.status_code == 200, saved.text
            assert saved.json()["restarted"] is True, saved.text
            assert "two" not in saved.text

    anyio.run(run)
    code, body = _asgi_get(web, "/cap/envweb/key")
    assert code == 200 and '"key":"two"' in body.replace(" ", ""), body
    server.serve_capability("envweb", action="stop")


def test_served_venv_survives_venv_gc(env):
    _, server, web, _core = env
    _make_web_cap(env, "vengc")
    started = server.serve_capability("vengc", action="start")
    assert started["status"] == "running"
    # Change the draft's packages so a promote creates a NEW requirement set,
    # then promote twice to trigger pruning + venv GC of the old set.
    skill = Path(server.get_capability("vengc")["path"]) / "SKILL.md"
    skill.write_text(
        skill.read_text(encoding="utf-8").replace(
            "packages: [fastapi, uvicorn, httpx]",
            "packages: [fastapi, uvicorn, httpx, six]",
        ),
        encoding="utf-8",
    )
    assert server.test_capability("vengc")["ok"]
    assert server.promote_capability("vengc", tag="v2")["ok"]
    status = server.serve_capability("vengc", action="status")
    assert status["status"] == "running"  # old venv must still exist for the child
    server.serve_capability("vengc", action="stop")


def test_corrupt_registry_is_quarantined_and_serving_survives(env):
    _, server, web, _core = env
    _make_web_cap(env, "corrupt")
    started = server.serve_capability("corrupt", action="start")
    assert started["status"] == "running"
    registry = web._registry_file()
    registry.write_text("{broken", encoding="utf-8")
    assert web.read_registry() == {}
    status = server.serve_capability("corrupt", action="status")
    assert status["status"] == "running"  # in-memory instance unaffected
    server.serve_capability("corrupt", action="stop")


def test_log_rotation(env):
    _, server, web, _core = env
    _make_web_cap(env, "chatty")
    started = server.serve_capability("chatty", action="start")
    assert started["status"] == "running"
    log_file = web._log_path("chatty", "v1")
    log_file.write_bytes(b"x" * (web.MAX_LOG_BYTES + 1))
    web._truncate_log_if_huge(log_file)
    assert log_file.stat().st_size <= web.MAX_LOG_TAIL
    web._rotate_log(log_file)
    assert not log_file.exists() or log_file.stat().st_size <= web.MAX_LOG_TAIL
    server.serve_capability("chatty", action="stop")


# ------------------------------------------------------------------ helpers


def _direct_children(pid: int) -> set[int]:
    out: set[int] = set()
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            stat = (entry / "stat").read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        close = stat.rfind(")")
        fields = stat[close + 2 :].split()
        if len(fields) >= 2 and fields[1] == str(pid):
            out.add(int(entry.name))
    return out


def _descendants(pid: int) -> set[int]:
    seen: set[int] = set()
    frontier = {pid}
    while frontier:
        children = {c for p in frontier for c in _direct_children(p)} - seen
        seen |= children
        frontier = children
    seen.discard(pid)
    return seen


def _alive(pid: int) -> bool:
    """True only for a live (non-zombie) process; kill(0) also matches zombies."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    close = stat.rfind(")")
    if close == -1:
        return False
    fields = stat[close + 2 :].split()
    return bool(fields) and fields[0] != "Z"
