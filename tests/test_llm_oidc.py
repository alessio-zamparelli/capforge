"""Fast unit tests for the LLM gateway, OIDC flow and their console APIs.

No capability subprocesses and no real network: the provider and identity
endpoints are local `http.server` children on loopback.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import anyio
import httpx
import pytest

from capforge import core, llm, oidc


@pytest.fixture(autouse=True)
def _state(tmp_path, monkeypatch):
    monkeypatch.setenv("CAPFORGE_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("CAPFORGE_CAPS_DIR", str(tmp_path / "caps"))
    monkeypatch.setenv("CAPFORGE_WEB_UI", "0")
    yield
    llm.shutdown()
    oidc._KEY = None
    oidc._DISCOVERY.clear()


def _deploy_text(*, kind: str = '"web"', extra: str = "") -> str:
    lines = ["from pydantic import BaseModel", "", "", "class Env(BaseModel):", "    pass", ""]
    lines.append(f"KIND = {kind}")
    if "web" in kind:
        lines.append('WEB_ENTRYPOINT = "app.py"')
        lines.append('APP = "app"')
    else:
        lines.append('CLI_ENTRYPOINT = "main.py"')
    lines.append('DATA = "version"')
    if extra:
        lines.extend(extra.strip().splitlines())
    return "\n".join(lines) + "\n"


# ------------------------------------------------------------------------ deploy.py


def test_deploy_parses_llm_and_auth():
    spec = core.parse_deploy_py(
        _deploy_text(
            extra='LLM = True\nAUTH = {"provider": "corp", "allow_domains": ["Example.com"], '
            '"public_paths": ["/health"]}'
        )
    )
    assert spec.llm is True
    assert spec.auth is not None
    assert spec.auth.provider == "corp"
    assert spec.auth.allow_domains == ("example.com",)
    assert spec.auth.public_paths == ("/health",)


def test_deploy_auth_shorthand_and_defaults():
    spec = core.parse_deploy_py(_deploy_text(extra='AUTH = "corp"'))
    assert spec.auth == core.AuthSpec(provider="corp")


def test_deploy_rejects_invalid_llm_and_auth():
    with pytest.raises(core.CapError, match="LLM must be True or False"):
        core.parse_deploy_py(_deploy_text(extra="LLM = 1"))
    with pytest.raises(core.CapError, match="AUTH is only valid"):
        core.parse_deploy_py(_deploy_text(kind='"cli"', extra='AUTH = "corp"'))
    with pytest.raises(core.CapError, match="unknown keys"):
        core.parse_deploy_py(_deploy_text(extra='AUTH = {"provider": "corp", "nope": 1}'))
    with pytest.raises(core.CapError, match="Invalid provider id"):
        core.parse_deploy_py(_deploy_text(extra='AUTH = {"provider": "Bad Id"}'))
    with pytest.raises(core.CapError, match="not a safe path"):
        core.parse_deploy_py(
            _deploy_text(extra='AUTH = {"provider": "corp", "public_paths": ["../x"]}')
        )
    with pytest.raises(core.CapError, match="AUTH must be"):
        core.parse_deploy_py(_deploy_text(extra="AUTH = 3"))


# ------------------------------------------------------------------------- LLM store


def test_llm_provider_crud_and_default():
    assert llm.describe() == {"default": None, "providers": []}
    llm.upsert_provider(
        {
            "id": "fake",
            "label": "Fake",
            "base_url": "https://llm.example.com/v1/",
            "models": [{"id": "m1", "label": "Model One"}, "m2"],
        }
    )
    described = llm.describe()
    assert described["default"] == {"provider": "fake", "model": "m1"}
    assert described["providers"][0]["base_url"] == "https://llm.example.com/v1"
    assert described["providers"][0]["models"] == [
        {"id": "m1", "label": "Model One"},
        {"id": "m2", "label": "m2"},
    ]
    assert described["providers"][0]["api_key_configured"] is False

    llm.set_secret("fake", "sk-test")
    assert llm.describe()["providers"][0]["api_key_configured"] is True
    assert llm.read_secret("fake") == "sk-test"

    llm.set_default("fake", "m2")
    assert llm.describe()["default"] == {"provider": "fake", "model": "m2"}

    llm.delete_provider("fake")
    assert llm.describe() == {"default": None, "providers": []}
    assert llm.read_secret("fake") is None


def test_llm_rejects_bad_providers():
    bad = [
        {"id": "Bad", "base_url": "https://x/v1", "models": ["m"]},
        {"id": "p", "base_url": "http://example.com/v1", "models": ["m"]},
        {"id": "p", "base_url": "https://x/v1", "models": []},
        {"id": "p", "base_url": "https://x/v1?q=1", "models": ["m"]},
        {"id": "p", "base_url": "https://x/v1", "models": ["m"], "auth": "weird"},
    ]
    for payload in bad:
        with pytest.raises(core.CapError):
            llm.upsert_provider(payload)
    with pytest.raises(core.CapError, match="no API key"):
        llm.upsert_provider({"id": "p", "base_url": "https://x/v1", "models": ["m"]})
        llm.resolve("demo")
    with pytest.raises(core.CapError, match="Unknown LLM model"):
        llm.set_default("p", "nope")


def test_llm_selection_falls_back_to_default_and_marks_stale():
    llm.upsert_provider({"id": "p", "base_url": "https://x/v1", "models": ["m1", "m2"]})
    llm.set_secret("p", "k")
    assert llm.selection_status("demo") == {
        "provider": "p",
        "provider_label": "p",
        "model": "m1",
        "model_label": "m1",
        "source": "default",
        "stale": False,
    }
    llm.set_selection("demo", "p", "m2")
    assert llm.selection_status("demo")["source"] == "cap"
    assert llm.selection_status("demo")["model"] == "m2"
    # Removing the model makes the stored selection stale, not fatal.
    llm.upsert_provider({"id": "p", "base_url": "https://x/v1", "models": ["m1"]})
    status = llm.selection_status("demo")
    assert status is not None
    assert status["stale"] is True
    assert status["model"] == "m1"
    cleared = llm.clear_selection("demo")
    assert cleared["source"] == "default"


def test_llm_runtime_env_is_gated_and_placeholder_for_tests():
    cap = core.Capability(name="demo", path=core.caps_dir() / "demo", llm=True)
    with pytest.raises(core.CapError, match="no LLM model is configured"):
        llm.runtime_env(cap)

    llm.upsert_provider({"id": "p", "base_url": "https://x/v1", "models": ["m1"]})
    with pytest.raises(core.CapError, match="no API key"):
        llm.runtime_env(cap)

    llm.set_secret("p", "k")
    env = llm.runtime_env(cap)
    try:
        assert env["CAPFORGE_LLM_API"] == "openai"
        assert env["CAPFORGE_LLM_MODEL"] == "m1"
        assert env["CAPFORGE_LLM_PROVIDER"] == "p"
        assert env["CAPFORGE_LLM_BASE_URL"].startswith("http://127.0.0.1:")
        assert env["CAPFORGE_LLM_TOKEN"] != core.ENV_PLACEHOLDER
        resolved = llm.resolve("demo")
        assert resolved is not None
        assert resolved.api_key == "k"
    finally:
        llm.revoke(env["CAPFORGE_LLM_TOKEN"])

    placeholder = llm.test_env()
    assert placeholder["CAPFORGE_LLM_TOKEN"] == core.ENV_PLACEHOLDER
    assert placeholder["CAPFORGE_LLM_MODEL"] == core.ENV_PLACEHOLDER


def test_llm_gateway_enforces_model_token_and_provider_key():
    seen: dict[str, object] = {}

    class Upstream(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            length = int(self.headers.get("content-length") or 0)
            body = json.loads(self.rfile.read(length) or b"{}")
            seen["path"] = self.path
            seen["auth"] = self.headers.get("authorization")
            seen["model"] = body.get("model")
            payload = json.dumps({"ok": True}).encode("utf-8")
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args):  # noqa: D401 - silence
            pass

    upstream = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
    thread = threading.Thread(target=upstream.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{upstream.server_address[1]}"
        llm.upsert_provider({"id": "fake", "base_url": base, "models": ["m1", "m2"]})
        llm.set_secret("fake", "provider-secret")
        llm.set_selection("demo", "fake", "m1")
        token = llm.mint_token("demo")

        url = f"{llm.gateway_base_url()}/chat/completions"
        auth = {"authorization": f"Bearer {token}"}
        ok = httpx.post(url, json={"model": "m1", "messages": []}, headers=auth)
        assert ok.status_code == 200
        assert seen == {"path": "/chat/completions", "auth": "Bearer provider-secret", "model": "m1"}

        wrong = httpx.post(url, json={"model": "m2"}, headers=auth)
        assert wrong.status_code == 400
        assert "operator selected" in wrong.json()["error"]["message"]

        implicit = httpx.post(url, json={"messages": []}, headers=auth)
        assert implicit.status_code == 200
        assert seen["model"] == "m1"

        listed = httpx.get(f"{llm.gateway_base_url()}/models", headers=auth)
        assert listed.status_code == 200
        assert [item["id"] for item in listed.json()["data"]] == ["m1", "m2"]

        assert httpx.post(url, json={}, headers={"authorization": "Bearer nope"}).status_code == 401
        assert httpx.post(f"{llm.gateway_base_url()}/other", json={}, headers=auth).status_code == 404

        llm.revoke(token)
        assert httpx.post(url, json={"messages": []}, headers=auth).status_code == 401
    finally:
        upstream.shutdown()
        upstream.server_close()


# ------------------------------------------------------------------------ OIDC store


def test_oidc_provider_crud_redacts_the_secret():
    payload = {
        "id": "corp",
        "label": "Corp",
        "issuer": "https://login.example.com/",
        "client_id": "client",
        "scopes": ["openid", "email"],
        "session_ttl": 600,
    }
    assert oidc.upsert_provider(payload) == {"provider": "corp", "updated": True}
    described = oidc.describe("https://hub.example.com")
    assert described["redirect_uri"] == "https://hub.example.com/__auth/callback"
    provider = described["providers"][0]
    assert provider["issuer"] == "https://login.example.com"
    assert provider["client_secret_configured"] is False
    assert provider["redirect_uri"] == "https://hub.example.com/__auth/callback"
    assert "secret" not in json.dumps(described).lower().replace("client_secret_configured", "")

    oidc.set_secret("corp", "shh")
    assert oidc.describe("https://hub.example.com")["providers"][0][
        "client_secret_configured"
    ] is True

    oidc.delete_provider("corp")
    assert oidc.describe("https://hub.example.com")["providers"] == []
    assert oidc.read_secret("corp") is None


def test_oidc_rejects_bad_providers():
    bad = [
        {"id": "corp", "issuer": "http://example.com", "client_id": "c"},
        {"id": "corp", "issuer": "https://x", "client_id": ""},
        {"id": "corp", "issuer": "https://x", "client_id": "c", "session_ttl": 10},
        {"id": "corp", "issuer": "https://x", "client_id": "c", "token_auth": "weird"},
        {"id": "corp", "issuer": "https://x", "client_id": "c", "scopes": []},
        {"id": "Bad", "issuer": "https://x", "client_id": "c"},
    ]
    for payload in bad:
        with pytest.raises(core.CapError):
            oidc.upsert_provider(payload)


def test_oidc_signed_sessions_and_state():
    identity = {
        "sub": "u1",
        "email": "User@Example.com",
        "name": "User",
        "groups": ["eng"],
        "provider": "corp",
    }
    cookie = oidc.make_session("demo", "corp", identity, 600)
    session = oidc.read_session(cookie, "demo")
    assert session is not None
    assert session["email"] == "User@Example.com"
    assert oidc.read_session(cookie, "other") is None
    assert oidc.read_session(cookie + "x", "demo") is None
    assert oidc.read_session("garbage", "demo") is None
    assert oidc.read_session(oidc.make_session("demo", "corp", identity, -5), "demo") is None

    state = oidc.make_state({"state": "s", "cap": "demo", "next": "/cap/demo/"})
    assert oidc.read_state(state)["cap"] == "demo"
    assert oidc.read_state(state + "0") is None

    header = json.loads(oidc._b64decode(oidc.identity_header(identity)))
    assert header == {
        "sub": "u1",
        "email": "User@Example.com",
        "name": "User",
        "groups": ["eng"],
        "provider": "corp",
    }


def test_oidc_is_allowed():
    spec = core.AuthSpec(
        provider="corp",
        allow_emails=("a@example.com",),
        allow_domains=("other.com",),
        allow_groups=("eng",),
    )
    assert oidc.is_allowed({"email": "a@example.com", "groups": []}, spec)
    assert oidc.is_allowed({"email": "b@OTHER.com", "groups": []}, spec)
    assert oidc.is_allowed({"email": "b@x.com", "groups": ["eng"]}, spec)
    assert not oidc.is_allowed({"email": "b@x.com", "groups": ["ops"]}, spec)
    assert oidc.is_allowed({"email": "b@x.com"}, core.AuthSpec(provider="corp"))


# --------------------------------------------------------------------- OIDC network


class _OidcServer:
    def __init__(self) -> None:
        this = self
        self.seen: dict[str, object] = {}

        def _json(handler: BaseHTTPRequestHandler, payload: dict) -> None:
            data = json.dumps(payload).encode("utf-8")
            handler.send_response(200)
            handler.send_header("content-type", "application/json")
            handler.send_header("content-length", str(len(data)))
            handler.end_headers()
            handler.wfile.write(data)

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                base = this.base
                if self.path == "/.well-known/openid-configuration":
                    _json(
                        self,
                        {
                            "issuer": base,
                            "authorization_endpoint": base + "/authorize",
                            "token_endpoint": base + "/token",
                            "userinfo_endpoint": base + "/userinfo",
                        }
                    )
                elif self.path == "/userinfo":
                    this.seen["userinfo_auth"] = self.headers.get("authorization")
                    _json(
                        self,
                        {
                            "sub": "u1",
                            "email": "User@Example.com",
                            "name": "User",
                            "groups": ["eng"],
                        }
                    )
                else:
                    self.send_response(404)
                    self.end_headers()

            def do_POST(self):  # noqa: N802
                length = int(self.headers.get("content-length") or 0)
                this.seen["token_body"] = self.rfile.read(length).decode("utf-8")
                this.seen["token_auth"] = self.headers.get("authorization")
                _json(self, {"access_token": "at", "token_type": "Bearer"})

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


def test_oidc_authorization_url_exchange_and_userinfo():
    server = _OidcServer()
    try:
        oidc.upsert_provider(
            {
                "id": "corp",
                "issuer": server.base,
                "client_id": "client",
                "scopes": ["openid", "email"],
                "token_auth": "post",
            }
        )
        oidc.set_secret("corp", "shh")
        provider = oidc.get_provider("corp")
        assert provider is not None

        async def run() -> None:
            url, cookie = await oidc.authorization_url(
                provider, "demo", "/cap/demo/", "https://hub.example.com"
            )
            assert url.startswith(server.base + "/authorize?")
            assert "client_id=client" in url
            assert "code_challenge_method=S256" in url
            state = oidc.read_state(cookie)
            assert state is not None
            assert state["cap"] == "demo"
            assert state["provider"] == "corp"
            assert state["next"] == "/cap/demo/"

            tokens = await oidc.exchange_code(
                provider, "code", state["verifier"], "https://hub.example.com"
            )
            assert tokens["access_token"] == "at"
            assert "client_secret=shh" in server.seen["token_body"]
            assert "code_verifier=" in server.seen["token_body"]

            identity = await oidc.fetch_identity(provider, tokens)
            assert identity == {
                "sub": "u1",
                "email": "user@example.com",
                "name": "User",
                "groups": ["eng"],
                "provider": "corp",
            }
            assert server.seen["userinfo_auth"] == "Bearer at"

            tested = await oidc.test_provider("corp")
            assert tested["ok"] is True
            assert tested["issuer"] == server.base

        anyio.run(run)
    finally:
        server.close()


# ------------------------------------------------------------------------ web routes


def _client(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://console")


def test_console_llm_and_oidc_apis(monkeypatch):
    import capforge.web as web

    monkeypatch.setenv("CAPFORGE_WEB_TOKEN", "sekrit")
    monkeypatch.setenv("CAPFORGE_PUBLIC_URL", "https://hub.example.com")

    async def run() -> None:
        async with _client(web.asgi_app()) as client:
            assert (await client.get("/api/llm")).status_code == 401
            login = await client.post("/api/login", json={"token": "sekrit"})
            assert login.status_code == 200
            cookie = {"cookie": f"capforge_session={login.cookies['capforge_session']}"}
            csrf = {**cookie, "x-csrf": "1"}

            configured = await client.put(
                "/api/llm/providers/fake",
                headers=csrf,
                json={"base_url": "https://llm.example.com/v1", "models": ["m1"]},
            )
            assert configured.status_code == 200, configured.text
            assert configured.json()["default"] == {"provider": "fake", "model": "m1"}

            key = await client.put(
                "/api/llm/providers/fake/secret", headers=csrf, json={"value": "sk-secret"}
            )
            assert key.status_code == 200
            assert "sk-secret" not in key.text
            listed = await client.get("/api/llm", headers=cookie)
            assert listed.json()["providers"][0]["api_key_configured"] is True

            default = await client.put(
                "/api/llm/default", headers=csrf, json={"provider": "fake", "model": "m1"}
            )
            assert default.status_code == 200

            missing = await client.get("/api/caps/nope/llm", headers=cookie)
            assert missing.status_code == 200
            assert missing.json()["llm"]["provider"] == "fake"

            selected = await client.put(
                "/api/caps/nope/llm",
                headers=csrf,
                json={"provider": "fake", "model": "m1"},
            )
            assert selected.status_code == 200, selected.text
            assert selected.json()["llm"]["source"] == "cap"
            cleared = await client.delete("/api/caps/nope/llm", headers=csrf)
            assert cleared.status_code == 200
            assert cleared.json()["llm"]["source"] == "default"

            oidc_list = await client.get("/api/oidc", headers=cookie)
            assert oidc_list.status_code == 200
            assert oidc_list.json()["redirect_uri"] == (
                "https://hub.example.com/__auth/callback"
            )
            provider = await client.put(
                "/api/oidc/providers/corp",
                headers=csrf,
                json={
                    "issuer": "https://login.example.com",
                    "client_id": "client",
                    "scopes": ["openid"],
                },
            )
            assert provider.status_code == 200, provider.text
            secret = await client.put(
                "/api/oidc/providers/corp/secret", headers=csrf, json={"value": "shh"}
            )
            assert secret.status_code == 200
            removed = await client.delete("/api/oidc/providers/corp", headers=csrf)
            assert removed.status_code == 200
            assert removed.json()["providers"] == []

    anyio.run(run)


def test_cap_auth_paths_are_public_and_safe():
    from starlette.requests import Request

    from capforge import web

    spec = core.AuthSpec(provider="corp", public_paths=("/health", "/assets/css"))
    assert web._public_path(spec, "/health")
    assert web._public_path(spec, "/assets/css/site.css")
    assert not web._public_path(spec, "/assets/cssx")
    assert not web._public_path(spec, "/private")
    assert web._public_path(core.AuthSpec(provider="corp", public_paths=("/",)), "/x")

    assert web._safe_next("demo", "/cap/demo/page?x=1") == "/cap/demo/page?x=1"
    assert web._safe_next("demo", "/cap/other/") == "/cap/demo/"
    assert web._safe_next("demo", "https://evil.example/cap/demo/") == "/cap/demo/"
    assert web._safe_next("demo", "/cap/demo/../secret") == "/cap/demo/"

    scope = {
        "type": "http",
        "method": "GET",
        "path": "/cap/nope/__auth/login",
        "headers": [],
        "query_string": b"",
        "server": ("test", 80),
        "client": ("test", 1),
        "scheme": "http",
    }
    request = Request(scope)
    assert request.method == "GET"


def test_cap_auth_routes_login_callback_and_logout(monkeypatch):
    import capforge.web as web

    monkeypatch.setenv("CAPFORGE_WEB_TOKEN", "sekrit")
    monkeypatch.setenv("CAPFORGE_PUBLIC_URL", "https://hub.example.com")
    oidc.upsert_provider(
        {
            "id": "corp",
            "issuer": "https://login.example.com",
            "client_id": "client",
            "scopes": ["openid", "email"],
        }
    )
    cap = core.Capability(
        name="demo",
        path=core.caps_dir() / "demo",
        modes=("web",),
        auth=core.AuthSpec(provider="corp"),
    )
    monkeypatch.setattr(web, "_active_or_raise", lambda name: ("v1", {}, cap.path))
    monkeypatch.setattr(web.core, "load_capability_at", lambda name, source: cap)

    async def fake_authorization_url(provider, capability, next_path, public_base):
        assert provider["id"] == "corp"
        assert capability == "demo"
        assert next_path == "/cap/demo/page"
        return "https://login.example.com/authorize?state=s", "state-cookie"

    async def fake_exchange(provider, code, verifier, public_base):
        assert code == "abc"
        assert verifier == "verifier"
        return {"access_token": "at"}

    async def fake_identity(provider, tokens):
        return {
            "sub": "u1",
            "email": "user@example.com",
            "name": "User",
            "groups": ["eng"],
            "provider": "corp",
        }

    monkeypatch.setattr(web.oidc, "authorization_url", fake_authorization_url)
    monkeypatch.setattr(web.oidc, "exchange_code", fake_exchange)
    monkeypatch.setattr(web.oidc, "fetch_identity", fake_identity)

    async def run() -> None:
        async with _client(web.asgi_app()) as client:
            login = await client.get(
                "/cap/demo/__auth/login?next=/cap/demo/page", follow_redirects=False
            )
            assert login.status_code == 303
            assert login.headers["location"] == "https://login.example.com/authorize?state=s"
            assert oidc.STATE_COOKIE in login.cookies

            state = oidc.make_state(
                {
                    "state": "s",
                    "cap": "demo",
                    "provider": "corp",
                    "verifier": "verifier",
                    "nonce": "n",
                    "next": "/cap/demo/page",
                }
            )
            client.cookies.set(oidc.STATE_COOKIE, state)
            callback = await client.get(
                "/__auth/callback?state=s&code=abc", follow_redirects=False
            )
            assert callback.status_code == 303, callback.text
            assert callback.headers["location"] == "/cap/demo/page"
            session_cookie = callback.cookies.get(oidc.session_cookie_name("demo"))
            assert session_cookie is not None
            session = oidc.read_session(session_cookie, "demo")
            assert session is not None and session["email"] == "user@example.com"

            bad_state = await client.get(
                "/__auth/callback?state=wrong&code=abc", follow_redirects=False
            )
            assert bad_state.status_code == 400

            logout = await client.get("/cap/demo/__auth/logout", follow_redirects=False)
            assert logout.status_code == 303
            assert logout.headers["location"] == "/__auth/logged-out"

    anyio.run(run)


def test_cap_auth_login_requires_public_url_and_provider(monkeypatch):
    import capforge.web as web

    monkeypatch.setenv("CAPFORGE_WEB_TOKEN", "sekrit")
    monkeypatch.delenv("CAPFORGE_PUBLIC_URL", raising=False)
    cap = core.Capability(
        name="demo",
        path=core.caps_dir() / "demo",
        modes=("web",),
        auth=core.AuthSpec(provider="corp"),
    )
    monkeypatch.setattr(web, "_active_or_raise", lambda name: ("v1", {}, cap.path))
    monkeypatch.setattr(web.core, "load_capability_at", lambda name, source: cap)

    async def run() -> None:
        async with _client(web.asgi_app()) as client:
            no_public = await client.get("/cap/demo/__auth/login")
            assert no_public.status_code == 503
            assert "CAPFORGE_PUBLIC_URL" in no_public.text

            monkeypatch.setenv("CAPFORGE_PUBLIC_URL", "https://hub.example.com")
            no_provider = await client.get("/cap/demo/__auth/login")
            assert no_provider.status_code == 503
            assert "not configured" in no_provider.text

    anyio.run(run)
