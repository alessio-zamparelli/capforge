"""LLM provider registry and the loopback gateway used by capabilities.

Capability code never receives a provider key. It receives a base URL, a model
name and a short-lived capability token; this module proxies OpenAI-compatible
requests to the configured provider and injects the real credential. The
gateway binds to 127.0.0.1 only and is shared by CLI runs and served children.

The registry lives under the state directory:

    <state>/llm.json                       providers, models and the global default
    <state>/secrets/llm/<provider>.json    API key, mode 0600
    <state>/llm/caps/<capability>.json     per-capability selection
"""

from __future__ import annotations

import json
import logging
import os
import re
import secrets
import threading
import time
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx

from . import core

log = logging.getLogger("capforge")

CONFIG_VERSION = 1
MAX_PROVIDERS = 32
MAX_MODELS = 100
MAX_BODY_BYTES = 16 * 1024 * 1024
MAX_SECRET_BYTES = 16 * 1024
DEFAULT_TIMEOUT = 120
DEFAULT_CONNECT_TIMEOUT = 10
AUTH_MODES = ("bearer", "header", "none")
MODEL_ID_RE = re.compile(r"^[^\s\x00-\x1f\x7f]{1,200}$")
HEADER_NAME_RE = re.compile(r"^[A-Za-z0-9-]{1,64}$")
GENERATION_PATHS = ("/v1/chat/completions", "/v1/responses")

# Requests and responses that must not be forwarded by the gateway.
_DROP_REQUEST = {
    "host",
    "content-length",
    "authorization",
    "accept-encoding",
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
}
_DROP_RESPONSE = {
    "content-length",
    "transfer-encoding",
    "connection",
    "keep-alive",
    "set-cookie",
    "clear-site-data",
    "proxy-authenticate",
    "trailer",
    "upgrade",
}


# --------------------------------------------------------------------------- paths


def _config_path() -> Path:
    return core._contained(core.state_dir(), core.state_dir() / "llm.json")


def _lock_path() -> Path:
    return core._contained(core.state_dir(), core.state_dir() / "locks" / "llm.lock")


def _secret_path(provider: str) -> Path:
    core.valid_provider_id(provider)
    return core._contained(
        core.state_dir(), core.state_dir() / "secrets" / "llm" / f"{provider}.json"
    )


def _selection_path(capability: str) -> Path:
    if not core.NAME_RE.match(capability or ""):
        raise core.CapError(f"Invalid capability name {capability!r}.")
    return core._contained(
        core.state_dir(), core.state_dir() / "llm" / "caps" / f"{capability}.json"
    )


def _quarantine(path: Path) -> None:
    dest = path.with_name(f"{path.stem}.corrupt-{time.time_ns()}{path.suffix}")
    try:
        path.replace(dest)
        log.warning("quarantined corrupt LLM file %s -> %s", path, dest)
    except OSError:
        log.exception("could not quarantine corrupt LLM file %s", path)


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        _quarantine(path)
        return None


def _write_json(path: Path, payload: Any, *, secret: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if secret:
        try:
            path.parent.chmod(0o700)
        except OSError:
            pass
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{time.time_ns()}.tmp")
    mode = 0o600 if secret else 0o644
    descriptor = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
        tmp.replace(path)
    except Exception:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


# ----------------------------------------------------------------------- validation


def _base_url(value: Any) -> str:
    raw = str(value or "").strip().rstrip("/")
    if not raw:
        raise core.CapError("LLM provider base_url is required")
    parsed = urlsplit(raw)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise core.CapError("LLM provider base_url must be an absolute http(s) URL")
    if parsed.query or parsed.fragment:
        raise core.CapError("LLM provider base_url cannot contain a query or fragment")
    return raw


def _models(value: Any) -> list[dict[str, str]]:
    if not isinstance(value, (list, tuple)):
        raise core.CapError("LLM provider models must be a list")
    if len(value) > MAX_MODELS:
        raise core.CapError(f"LLM provider declares more than {MAX_MODELS} models")
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in value:
        if isinstance(item, str):
            raw: dict[str, Any] = {"id": item}
        elif isinstance(item, dict):
            raw = item
        else:
            raise core.CapError("each LLM model must be a string or {id, label}")
        model_id = str(raw.get("id") or "").strip()
        if not MODEL_ID_RE.match(model_id):
            raise core.CapError(f"invalid LLM model id {model_id!r}")
        if model_id in seen:
            raise core.CapError(f"duplicate LLM model id {model_id!r}")
        seen.add(model_id)
        label = str(raw.get("label") or model_id).strip() or model_id
        out.append({"id": model_id, "label": label[:200]})
    if not out:
        raise core.CapError("LLM provider must declare at least one model")
    return out


def _normalize_provider(provider: str, raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise core.CapError(f"LLM provider {provider!r} must be an object")
    label = str(raw.get("label") or provider).strip()[:100] or provider
    base_url = _base_url(raw.get("base_url"))
    auth = str(raw.get("auth") or "bearer").strip().lower()
    if auth not in AUTH_MODES:
        raise core.CapError(f"LLM provider auth must be one of {list(AUTH_MODES)}")
    header_name = str(raw.get("header_name") or "Authorization").strip()
    if auth == "header" and not HEADER_NAME_RE.match(header_name):
        raise core.CapError("LLM provider header_name is not a valid HTTP header name")
    models = _models(raw.get("models"))
    return {
        "label": label,
        "base_url": base_url,
        "auth": auth,
        "header_name": header_name if auth == "header" else "Authorization",
        "models": models,
    }


def _normalize_config(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise core.CapError("LLM configuration must be an object")
    providers_raw = raw.get("providers") or {}
    if not isinstance(providers_raw, dict):
        raise core.CapError("LLM providers must be an object")
    if len(providers_raw) > MAX_PROVIDERS:
        raise core.CapError(f"at most {MAX_PROVIDERS} LLM providers are supported")
    providers: dict[str, dict[str, Any]] = {}
    for provider, body in providers_raw.items():
        pid = core.valid_provider_id(str(provider))
        providers[pid] = _normalize_provider(pid, body)
    default = raw.get("default")
    if default is not None and not isinstance(default, dict):
        raise core.CapError("LLM default must be {provider, model}")
    default_ref: tuple[str, str] | None = None
    if isinstance(default, dict):
        provider = str(default.get("provider") or "")
        model = str(default.get("model") or "")
        if _valid_ref(providers, provider, model):
            default_ref = (provider, model)
    if default_ref is None:
        default_ref = _first_model(providers)
    return {
        "version": CONFIG_VERSION,
        "providers": providers,
        "default": (
            {"provider": default_ref[0], "model": default_ref[1]} if default_ref else None
        ),
    }


def _first_model(providers: dict[str, dict[str, Any]]) -> tuple[str, str] | None:
    for provider, body in providers.items():
        for model in body["models"]:
            return provider, model["id"]
    return None


def _valid_ref(providers: dict[str, dict[str, Any]], provider: str, model: str) -> bool:
    body = providers.get(provider)
    return bool(body and any(item["id"] == model for item in body["models"]))


# --------------------------------------------------------------------------- config


def read_config() -> dict[str, Any]:
    raw = _read_json(_config_path())
    if raw is None:
        return {"version": CONFIG_VERSION, "providers": {}, "default": None}
    try:
        return _normalize_config(raw)
    except core.CapError as exc:
        log.warning("invalid LLM configuration: %s", exc)
        _quarantine(_config_path())
        return {"version": CONFIG_VERSION, "providers": {}, "default": None}


def _write_config(config: dict[str, Any]) -> None:
    _write_json(_config_path(), config)


def read_secret(provider: str) -> str | None:
    raw = _read_json(_secret_path(provider))
    if not isinstance(raw, dict):
        return None
    value = raw.get("value")
    return value if isinstance(value, str) and value else None


def set_secret(provider: str, value: str) -> dict[str, Any]:
    core.valid_provider_id(provider)
    if provider not in read_config()["providers"]:
        raise core.CapError(f"Unknown LLM provider {provider!r}")
    if not isinstance(value, str) or not value.strip():
        raise core.CapError("LLM API key must be a non-empty string")
    if "\x00" in value:
        raise core.CapError("LLM API key cannot contain NUL bytes")
    if len(value.encode("utf-8")) > MAX_SECRET_BYTES:
        raise core.CapError(f"LLM API key is larger than {MAX_SECRET_BYTES} bytes")
    with core._file_lock(_lock_path()):
        _write_json(_secret_path(provider), {"value": value.strip()}, secret=True)
    log.info("llm secret set provider=%s", provider)
    return {"provider": provider, "configured": True}


def _delete_secret(provider: str) -> None:
    try:
        _secret_path(provider).unlink()
    except FileNotFoundError:
        pass


def upsert_provider(payload: dict[str, Any]) -> dict[str, Any]:
    provider = core.valid_provider_id(str(payload.get("id") or ""))
    with core._file_lock(_lock_path()):
        config = read_config()
        if provider not in config["providers"] and len(config["providers"]) >= MAX_PROVIDERS:
            raise core.CapError(f"at most {MAX_PROVIDERS} LLM providers are supported")
        config["providers"][provider] = _normalize_provider(provider, payload)
        if config["default"] is None or not _valid_ref(
            config["providers"], config["default"]["provider"], config["default"]["model"]
        ):
            first = _first_model(config["providers"])
            config["default"] = (
                {"provider": first[0], "model": first[1]} if first else None
            )
        _write_config(config)
    log.info("llm provider upserted provider=%s", provider)
    return provider_summary(provider, config["providers"][provider])


def delete_provider(provider: str) -> dict[str, Any]:
    provider = core.valid_provider_id(provider)
    with core._file_lock(_lock_path()):
        config = read_config()
        removed = config["providers"].pop(provider, None)
        if removed is None:
            raise core.CapError(f"Unknown LLM provider {provider!r}")
        if config["default"] and config["default"]["provider"] == provider:
            first = _first_model(config["providers"])
            config["default"] = (
                {"provider": first[0], "model": first[1]} if first else None
            )
        _write_config(config)
    _delete_secret(provider)
    log.info("llm provider deleted provider=%s", provider)
    return {"provider": provider, "removed": True, "default": config["default"]}


def set_default(provider: str, model: str) -> dict[str, Any]:
    provider = core.valid_provider_id(provider)
    model = str(model or "").strip()
    with core._file_lock(_lock_path()):
        config = read_config()
        if not _valid_ref(config["providers"], provider, model):
            raise core.CapError(f"Unknown LLM model {provider}/{model}")
        config["default"] = {"provider": provider, "model": model}
        _write_config(config)
    log.info("llm default provider=%s model=%s", provider, model)
    return {"default": config["default"]}


def provider_summary(provider: str, body: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": provider,
        "label": body["label"],
        "base_url": body["base_url"],
        "auth": body["auth"],
        "header_name": body["header_name"],
        "models": body["models"],
        "api_key_configured": read_secret(provider) is not None,
    }


def describe() -> dict[str, Any]:
    config = read_config()
    return {
        "default": config["default"],
        "providers": [
            provider_summary(provider, body)
            for provider, body in sorted(config["providers"].items())
        ],
    }


# ------------------------------------------------------------------------ selections


def _read_selection(capability: str) -> dict[str, Any] | None:
    raw = _read_json(_selection_path(capability))
    if not isinstance(raw, dict):
        return None
    provider = raw.get("provider")
    model = raw.get("model")
    if not isinstance(provider, str) or not isinstance(model, str):
        return None
    return {"provider": provider, "model": model}


def _write_selection(capability: str, provider: str, model: str) -> None:
    _write_json(
        _selection_path(capability),
        {
            "provider": provider,
            "model": model,
            "updated_at": int(time.time()),
        },
    )


def selection_status(capability: str) -> dict[str, Any] | None:
    """Resolve the effective model for the console; never returns a key."""
    config = read_config()
    if not config["providers"]:
        return None
    stored = _read_selection(capability)
    stale = False
    if stored is not None:
        if _valid_ref(config["providers"], stored["provider"], stored["model"]):
            ref, source = stored, "cap"
        else:
            stale, ref, source = True, config["default"], "default"
    else:
        ref, source = config["default"], "default"
    if ref is None:
        return None
    body = config["providers"][ref["provider"]]
    label = next(
        (item["label"] for item in body["models"] if item["id"] == ref["model"]),
        ref["model"],
    )
    return {
        "provider": ref["provider"],
        "provider_label": body["label"],
        "model": ref["model"],
        "model_label": label,
        "source": source,
        "stale": stale,
    }


def set_selection(capability: str, provider: str, model: str) -> dict[str, Any]:
    core.cap_path(capability)
    provider = core.valid_provider_id(provider)
    model = str(model or "").strip()
    config = read_config()
    if not _valid_ref(config["providers"], provider, model):
        raise core.CapError(f"Unknown LLM model {provider}/{model}")
    with core._file_lock(_lock_path()):
        _write_selection(capability, provider, model)
    log.info("llm selection cap=%s provider=%s model=%s", capability, provider, model)
    return selection_status(capability) or {}


def clear_selection(capability: str) -> dict[str, Any]:
    core.cap_path(capability)
    try:
        _selection_path(capability).unlink()
    except FileNotFoundError:
        pass
    log.info("llm selection cleared cap=%s", capability)
    return selection_status(capability) or {}


# -------------------------------------------------------------------------- runtime


@dataclass(frozen=True)
class Resolved:
    capability: str
    provider: str
    model: str
    base_url: str
    auth: str
    header_name: str
    api_key: str | None


def resolve(capability: str) -> Resolved | None:
    config = read_config()
    if not config["providers"]:
        return None
    stored = _read_selection(capability)
    ref = config["default"]
    if stored is not None and _valid_ref(config["providers"], stored["provider"], stored["model"]):
        ref = stored
    if ref is None:
        return None
    body = config["providers"][ref["provider"]]
    key = read_secret(ref["provider"])
    if body["auth"] != "none" and not key:
        raise core.CapError(
            f"LLM provider {ref['provider']!r} has no API key; set it in the console"
        )
    return Resolved(
        capability=capability,
        provider=ref["provider"],
        model=ref["model"],
        base_url=body["base_url"],
        auth=body["auth"],
        header_name=body["header_name"],
        api_key=key,
    )


def runtime_env(cap: core.Capability, *, ttl: int | None = None) -> dict[str, str]:
    """Environment for one active run or served instance.

    Raises CapError when the capability declares LLM use but no model resolves,
    so a broken dependency fails before the child starts.
    """
    if not cap.llm:
        return {}
    resolved = resolve(cap.name)
    if resolved is None:
        raise core.CapError(
            f"{cap.name!r} declares LLM = True but no LLM model is configured; "
            "add a provider and select a model in the console"
        )
    token = mint_token(cap.name, ttl=ttl)
    return {
        "CAPFORGE_LLM_BASE_URL": gateway_base_url(),
        "CAPFORGE_LLM_TOKEN": token,
        "CAPFORGE_LLM_MODEL": resolved.model,
        "CAPFORGE_LLM_PROVIDER": resolved.provider,
        "CAPFORGE_LLM_API": "openai",
    }


def test_env() -> dict[str, str]:
    """Placeholders for tests, drafts and the web boot probe; never a real token."""
    return {
        "CAPFORGE_LLM_BASE_URL": "http://127.0.0.1:9/v1",
        "CAPFORGE_LLM_TOKEN": core.ENV_PLACEHOLDER,
        "CAPFORGE_LLM_MODEL": core.ENV_PLACEHOLDER,
        "CAPFORGE_LLM_PROVIDER": "test",
        "CAPFORGE_LLM_API": "openai",
    }


# --------------------------------------------------------------------------- tokens

_TOKENS: dict[str, tuple[str, float | None]] = {}
_TOKEN_LOCK = threading.Lock()


def mint_token(capability: str, *, ttl: int | None = None) -> str:
    token = secrets.token_urlsafe(32)
    expires = None if ttl is None else time.time() + max(1, ttl)
    with _TOKEN_LOCK:
        _prune_tokens_locked()
        _TOKENS[token] = (capability, expires)
    return token


def revoke(token: str | None) -> None:
    if not token:
        return
    with _TOKEN_LOCK:
        _TOKENS.pop(token, None)


def _prune_tokens_locked() -> None:
    now = time.time()
    for token, (_cap, expires) in list(_TOKENS.items()):
        if expires is not None and expires <= now:
            _TOKENS.pop(token, None)


def _token_capability(token: str) -> str | None:
    if not token:
        return None
    with _TOKEN_LOCK:
        _prune_tokens_locked()
        entry = _TOKENS.get(token)
    if entry is None:
        return None
    capability, expires = entry
    if expires is not None and expires <= time.time():
        return None
    return capability


# -------------------------------------------------------------------------- gateway


class _GatewayError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


class _GatewayHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"
    server_version = "capforge-llm/1"

    def do_GET(self) -> None:  # noqa: N802 - http.server API
        self._handle()

    def do_POST(self) -> None:  # noqa: N802
        self._handle()

    def do_PUT(self) -> None:  # noqa: N802
        self._handle()

    def do_DELETE(self) -> None:  # noqa: N802
        self._handle()

    def log_message(self, fmt: str, *args: Any) -> None:
        log.debug("llm gateway: " + fmt, *args)

    def _handle(self) -> None:
        started = time.monotonic()
        status = 500
        try:
            status = self._dispatch()
        except _GatewayError as exc:
            status = exc.status
            self._send_json(exc.status, {"error": {"message": exc.message, "type": "capforge"}})
        except (BrokenPipeError, ConnectionResetError):
            status = 499
        except Exception:
            log.exception("llm gateway request failed")
            status = 502
            self._send_json(
                502, {"error": {"message": "LLM gateway failure", "type": "capforge"}}
            )
        finally:
            log.info(
                "llm gateway method=%s status=%s duration_ms=%d",
                self.command,
                status,
                int((time.monotonic() - started) * 1000),
            )

    def _dispatch(self) -> int:
        header = self.headers.get("authorization", "")
        token = header[7:].strip() if header.lower().startswith("bearer ") else ""
        capability = _token_capability(token)
        if capability is None:
            raise _GatewayError(401, "invalid or expired capability token")

        path = urlsplit(self.path).path
        if path == "/v1/models":
            return self._list_models(capability)
        if path not in GENERATION_PATHS:
            raise _GatewayError(404, "unknown LLM gateway endpoint")

        resolved = resolve(capability)
        if resolved is None:
            raise _GatewayError(503, "no LLM model is configured for this capability")

        body = self._body()
        if path in GENERATION_PATHS:
            body = self._enforce_model(body, resolved.model)

        headers = {
            key: value
            for key, value in self.headers.items()
            if key.lower() not in _DROP_REQUEST
        }
        headers["accept-encoding"] = "identity"
        if resolved.auth == "bearer" and resolved.api_key:
            headers["authorization"] = f"Bearer {resolved.api_key}"
        elif resolved.auth == "header" and resolved.api_key:
            headers[resolved.header_name] = resolved.api_key

        target = resolved.base_url + path.removeprefix("/v1")
        timeout = httpx.Timeout(DEFAULT_TIMEOUT, connect=DEFAULT_CONNECT_TIMEOUT)
        try:
            with httpx.Client(timeout=timeout) as client:
                with client.stream(
                    self.command, target, headers=headers, content=body
                ) as upstream:
                    self.send_response(upstream.status_code)
                    for key, value in upstream.headers.items():
                        if key.lower() in _DROP_RESPONSE:
                            continue
                        self.send_header(key, value)
                    self.send_header("connection", "close")
                    self.end_headers()
                    for chunk in upstream.iter_raw():
                        self.wfile.write(chunk)
                        self.wfile.flush()
                    return upstream.status_code
        except httpx.HTTPError as exc:
            log.warning(
                "llm upstream failed cap=%s provider=%s error=%s",
                capability,
                resolved.provider,
                type(exc).__name__,
            )
            raise _GatewayError(502, "LLM provider request failed") from exc

    def _list_models(self, capability: str) -> int:
        config = read_config()
        resolved = resolve(capability)
        if resolved is None:
            raise _GatewayError(503, "no LLM model is configured for this capability")
        body = config["providers"].get(resolved.provider)
        if body is None:
            raise _GatewayError(503, "the selected LLM provider is gone")
        payload = {
            "object": "list",
            "data": [
                {
                    "id": item["id"],
                    "object": "model",
                    "created": 0,
                    "owned_by": resolved.provider,
                }
                for item in body["models"]
            ],
        }
        self._send_json(200, payload)
        return 200

    def _body(self) -> bytes:
        try:
            length = int(self.headers.get("content-length") or 0)
        except ValueError:
            raise _GatewayError(400, "invalid content-length") from None
        if length < 0 or length > MAX_BODY_BYTES:
            raise _GatewayError(413, "request body too large")
        return self.rfile.read(length) if length else b""

    def _enforce_model(self, body: bytes, selected: str) -> bytes:
        if not body:
            raise _GatewayError(400, "a JSON body is required")
        try:
            payload = json.loads(body)
        except ValueError:
            raise _GatewayError(400, "the request body must be JSON") from None
        if not isinstance(payload, dict):
            raise _GatewayError(400, "the request body must be a JSON object")
        requested = payload.get("model")
        if requested is not None and requested != selected:
            raise _GatewayError(
                400, f"the operator selected model {selected!r} for this capability"
            )
        payload["model"] = selected
        return json.dumps(payload, ensure_ascii=False).encode("utf-8")

    def _send_json(self, status: int, payload: Any) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        try:
            self.send_response(status)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(data)))
            self.send_header("connection", "close")
            self.end_headers()
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass


class _GatewayServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


@dataclass
class _Gateway:
    server: _GatewayServer
    thread: threading.Thread
    port: int


_GATEWAY: _Gateway | None = None
_GATEWAY_LOCK = threading.Lock()


def gateway_base_url() -> str:
    return f"http://127.0.0.1:{_ensure_gateway().port}/v1"


def _ensure_gateway() -> _Gateway:
    global _GATEWAY
    with _GATEWAY_LOCK:
        if _GATEWAY is not None and _GATEWAY.thread.is_alive():
            return _GATEWAY
        server = _GatewayServer(("127.0.0.1", 0), _GatewayHandler)
        thread = threading.Thread(
            target=server.serve_forever,
            name="capforge-llm-gateway",
            daemon=True,
        )
        thread.start()
        port = int(server.server_address[1])
        _GATEWAY = _Gateway(server=server, thread=thread, port=port)
        log.info("llm gateway listening on 127.0.0.1:%d", port)
        return _GATEWAY


def shutdown() -> None:
    global _GATEWAY
    with _GATEWAY_LOCK:
        gateway, _GATEWAY = _GATEWAY, None
    if gateway is not None:
        gateway.server.shutdown()
        gateway.server.server_close()
        gateway.thread.join(timeout=5)
    with _TOKEN_LOCK:
        _TOKENS.clear()


# ------------------------------------------------------------------------- provider test


def test_provider(provider: str) -> dict[str, Any]:
    """Best-effort connectivity check used by the console; never returns the key."""
    provider = core.valid_provider_id(provider)
    config = read_config()
    body = config["providers"].get(provider)
    if body is None:
        raise core.CapError(f"Unknown LLM provider {provider!r}")
    key = read_secret(provider)
    if body["auth"] != "none" and not key:
        raise core.CapError(f"LLM provider {provider!r} has no API key")
    headers = {"accept": "application/json", "accept-encoding": "identity"}
    if body["auth"] == "bearer" and key:
        headers["authorization"] = f"Bearer {key}"
    elif body["auth"] == "header" and key:
        headers[body["header_name"]] = key
    try:
        with httpx.Client(timeout=httpx.Timeout(15.0, connect=5.0)) as client:
            response = client.get(f"{body['base_url']}/models", headers=headers)
    except httpx.HTTPError as exc:
        return {"ok": False, "error": f"connection failed: {type(exc).__name__}"}
    if response.status_code >= 400:
        return {"ok": False, "status": response.status_code, "error": "provider rejected the request"}
    return {"ok": True, "status": response.status_code}
