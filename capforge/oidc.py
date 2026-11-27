"""OIDC provider registry and the authorization-code flow for served capabilities.

Providers are operator-managed in the console; a capability only names one in
`deploy.py` (`AUTH = {"provider": "corp"}`). The hub enforces authentication at
the `/cap/<name>/` proxy, then forwards the verified identity as trusted
`X-Capforge-User*` headers. Capability code never sees tokens or the client
secret.

The flow uses Authorization Code + PKCE, a signed state cookie and the provider's
userinfo endpoint. ID-token signatures are not verified: the access token is
obtained over TLS directly from the token endpoint, so userinfo is the trust
anchor and no crypto dependency is needed.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import threading
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urlsplit

import httpx

from . import core

log = logging.getLogger("capforge")

CONFIG_VERSION = 1
MAX_PROVIDERS = 32
MAX_SCOPES = 32
MAX_ALLOW = 100
DEFAULT_SESSION_TTL = 8 * 3600
MIN_SESSION_TTL = 300
MAX_SESSION_TTL = 30 * 24 * 3600
DEFAULT_SCOPES = ("openid", "email", "profile")
TOKEN_AUTH = ("basic", "post")
LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}
SESSION_COOKIE_PREFIX = "capforge_cap_"
STATE_COOKIE = "capforge_oidc_state"
STATE_TTL = 600
CLAIM_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]{0,63}$")


# --------------------------------------------------------------------------- paths


def _config_path() -> Path:
    return core._contained(core.state_dir(), core.state_dir() / "oidc.json")


def _lock_path() -> Path:
    return core._contained(core.state_dir(), core.state_dir() / "locks" / "oidc.lock")


def _secret_path(provider: str) -> Path:
    core.valid_provider_id(provider)
    return core._contained(
        core.state_dir(), core.state_dir() / "secrets" / "oidc" / f"{provider}.json"
    )


def _key_path() -> Path:
    return core._contained(core.state_dir(), core.state_dir() / "auth.key")


def _quarantine(path: Path) -> None:
    dest = path.with_name(f"{path.stem}.corrupt-{time.time_ns()}{path.suffix}")
    try:
        path.replace(dest)
        log.warning("quarantined corrupt OIDC file %s -> %s", path, dest)
    except OSError:
        log.exception("could not quarantine corrupt OIDC file %s", path)


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
    descriptor = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600 if secret else 0o644)
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


def _issuer(value: Any) -> str:
    raw = str(value or "").strip().rstrip("/")
    if not raw:
        raise core.CapError("OIDC issuer is required")
    parsed = urlsplit(raw)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise core.CapError("OIDC issuer must be an absolute http(s) URL")
    if parsed.scheme == "http" and (parsed.hostname or "") not in LOOPBACK_HOSTS:
        raise core.CapError("OIDC issuer must use https unless it is loopback")
    if parsed.query or parsed.fragment:
        raise core.CapError("OIDC issuer cannot contain a query or fragment")
    return raw


def _scopes(value: Any) -> list[str]:
    if value is None:
        return list(DEFAULT_SCOPES)
    if not isinstance(value, (list, tuple)):
        raise core.CapError("OIDC scopes must be a list of strings")
    scopes: list[str] = []
    for item in value:
        scope = str(item).strip()
        if not scope or any(ch.isspace() for ch in scope):
            raise core.CapError(f"invalid OIDC scope {scope!r}")
        if scope not in scopes:
            scopes.append(scope)
    if not scopes:
        raise core.CapError("OIDC scopes cannot be empty")
    if len(scopes) > MAX_SCOPES:
        raise core.CapError(f"at most {MAX_SCOPES} OIDC scopes are supported")
    return scopes


def _claim(value: Any, default: str) -> str:
    claim = str(value or default).strip()
    if not CLAIM_RE.match(claim):
        raise core.CapError(f"invalid OIDC claim name {claim!r}")
    return claim


def _normalize_provider(provider: str, raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise core.CapError(f"OIDC provider {provider!r} must be an object")
    label = str(raw.get("label") or provider).strip()[:100] or provider
    issuer = _issuer(raw.get("issuer"))
    client_id = str(raw.get("client_id") or "").strip()
    if not client_id or "\n" in client_id or "\r" in client_id:
        raise core.CapError("OIDC client_id is required")
    token_auth = str(raw.get("token_auth") or "basic").strip().lower()
    if token_auth not in TOKEN_AUTH:
        raise core.CapError(f"OIDC token_auth must be one of {list(TOKEN_AUTH)}")
    try:
        session_ttl = int(raw.get("session_ttl") or DEFAULT_SESSION_TTL)
    except (TypeError, ValueError):
        raise core.CapError("OIDC session_ttl must be an integer number of seconds") from None
    if not MIN_SESSION_TTL <= session_ttl <= MAX_SESSION_TTL:
        raise core.CapError(
            f"OIDC session_ttl must be between {MIN_SESSION_TTL} and {MAX_SESSION_TTL} seconds"
        )
    return {
        "label": label,
        "issuer": issuer,
        "client_id": client_id[:500],
        "scopes": _scopes(raw.get("scopes")),
        "token_auth": token_auth,
        "session_ttl": session_ttl,
        "email_claim": _claim(raw.get("email_claim"), "email"),
        "name_claim": _claim(raw.get("name_claim"), "name"),
        "groups_claim": _claim(raw.get("groups_claim"), "groups"),
    }


def _normalize_config(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise core.CapError("OIDC configuration must be an object")
    providers_raw = raw.get("providers") or {}
    if not isinstance(providers_raw, dict):
        raise core.CapError("OIDC providers must be an object")
    if len(providers_raw) > MAX_PROVIDERS:
        raise core.CapError(f"at most {MAX_PROVIDERS} OIDC providers are supported")
    providers: dict[str, dict[str, Any]] = {}
    for provider, body in providers_raw.items():
        pid = core.valid_provider_id(str(provider))
        providers[pid] = _normalize_provider(pid, body)
    return {"version": CONFIG_VERSION, "providers": providers}


# --------------------------------------------------------------------------- config


def read_config() -> dict[str, Any]:
    raw = _read_json(_config_path())
    if raw is None:
        return {"version": CONFIG_VERSION, "providers": {}}
    try:
        return _normalize_config(raw)
    except core.CapError as exc:
        log.warning("invalid OIDC configuration: %s", exc)
        _quarantine(_config_path())
        return {"version": CONFIG_VERSION, "providers": {}}


def get_provider(provider: str) -> dict[str, Any] | None:
    if not core.PROVIDER_ID_RE.match(provider or ""):
        return None
    body = read_config()["providers"].get(provider)
    return {"id": provider, **body} if body is not None else None


def read_secret(provider: str) -> str | None:
    raw = _read_json(_secret_path(provider))
    if not isinstance(raw, dict):
        return None
    value = raw.get("value")
    return value if isinstance(value, str) and value else None


def set_secret(provider: str, value: str) -> dict[str, Any]:
    core.valid_provider_id(provider)
    if provider not in read_config()["providers"]:
        raise core.CapError(f"Unknown OIDC provider {provider!r}")
    if not isinstance(value, str) or not value.strip():
        raise core.CapError("OIDC client secret must be a non-empty string")
    if "\x00" in value:
        raise core.CapError("OIDC client secret cannot contain NUL bytes")
    if len(value.encode("utf-8")) > core.MAX_ENV_VALUE_BYTES:
        raise core.CapError("OIDC client secret is too large")
    with core._file_lock(_lock_path()):
        _write_json(_secret_path(provider), {"value": value.strip()}, secret=True)
    log.info("oidc secret set provider=%s", provider)
    return {"provider": provider, "configured": True}


def upsert_provider(payload: dict[str, Any]) -> dict[str, Any]:
    provider = core.valid_provider_id(str(payload.get("id") or ""))
    with core._file_lock(_lock_path()):
        config = read_config()
        if provider not in config["providers"] and len(config["providers"]) >= MAX_PROVIDERS:
            raise core.CapError(f"at most {MAX_PROVIDERS} OIDC providers are supported")
        config["providers"][provider] = _normalize_provider(provider, payload)
        _write_json(_config_path(), config)
    log.info("oidc provider upserted provider=%s", provider)
    return {"provider": provider, "updated": True}


def delete_provider(provider: str) -> dict[str, Any]:
    provider = core.valid_provider_id(provider)
    with core._file_lock(_lock_path()):
        config = read_config()
        if config["providers"].pop(provider, None) is None:
            raise core.CapError(f"Unknown OIDC provider {provider!r}")
        _write_json(_config_path(), config)
    try:
        _secret_path(provider).unlink()
    except FileNotFoundError:
        pass
    log.info("oidc provider deleted provider=%s", provider)
    return {"provider": provider, "removed": True}


def provider_summary(
    provider: str, body: dict[str, Any], *, public_base: str
) -> dict[str, Any]:
    return {
        "id": provider,
        "label": body["label"],
        "issuer": body["issuer"],
        "client_id": body["client_id"],
        "scopes": body["scopes"],
        "token_auth": body["token_auth"],
        "session_ttl": body["session_ttl"],
        "email_claim": body["email_claim"],
        "name_claim": body["name_claim"],
        "groups_claim": body["groups_claim"],
        "client_secret_configured": read_secret(provider) is not None,
        "redirect_uri": redirect_uri(public_base),
    }


def describe(public_base: str) -> dict[str, Any]:
    config = read_config()
    return {
        "redirect_uri": redirect_uri(public_base),
        "providers": [
            provider_summary(provider, body, public_base=public_base)
            for provider, body in sorted(config["providers"].items())
        ],
    }


def redirect_uri(public_base: str) -> str:
    return public_base.rstrip("/") + "/__auth/callback"


# ------------------------------------------------------------------------- cookies


_KEY: bytes | None = None
_KEY_LOCK = threading.Lock()


def _key() -> bytes:
    global _KEY
    with _KEY_LOCK:
        if _KEY is not None:
            return _KEY
        path = _key_path()
        try:
            text = path.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            text = ""
        except OSError as exc:
            raise core.CapError(f"cannot read the OIDC session key: {exc}") from exc
        if len(text) < 32:
            path.parent.mkdir(parents=True, exist_ok=True)
            text = secrets.token_urlsafe(48)
            tmp = path.with_name(f".{path.name}.{os.getpid()}.{time.time_ns()}.tmp")
            descriptor = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                    handle.write(text + "\n")
                tmp.replace(path)
            except Exception:
                try:
                    tmp.unlink()
                except OSError:
                    pass
                raise
        _KEY = text.encode("utf-8")
        return _KEY


def _b64encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64decode(text: str) -> bytes:
    padding = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + padding)


def _sign(raw: bytes) -> str:
    return hmac.new(_key(), raw, hashlib.sha256).hexdigest()


def encode_signed(payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return f"{_b64encode(raw)}.{_sign(raw)}"


def decode_signed(value: str | None) -> dict[str, Any] | None:
    if not value or "." not in value:
        return None
    body, signature = value.split(".", 1)
    try:
        raw = _b64decode(body)
    except (ValueError, TypeError):
        return None
    try:
        valid = hmac.compare_digest(signature, _sign(raw))
    except TypeError:
        return None
    if not valid:
        return None
    try:
        payload = json.loads(raw)
    except ValueError:
        return None
    return payload if isinstance(payload, dict) else None


def session_cookie_name(capability: str) -> str:
    return f"{SESSION_COOKIE_PREFIX}{capability}"


def make_session(
    capability: str,
    provider: str,
    identity: dict[str, Any],
    ttl: int,
) -> str:
    now = int(time.time())
    return encode_signed(
        {
            "cap": capability,
            "provider": provider,
            "sub": identity.get("sub", ""),
            "email": identity.get("email", ""),
            "name": identity.get("name", ""),
            "groups": identity.get("groups", []),
            "iat": now,
            "exp": now + ttl,
            "jti": secrets.token_urlsafe(12),
        }
    )


def read_session(value: str | None, capability: str) -> dict[str, Any] | None:
    payload = decode_signed(value)
    if payload is None or payload.get("cap") != capability:
        return None
    try:
        expires = int(payload.get("exp") or 0)
    except (TypeError, ValueError):
        return None
    if expires <= int(time.time()):
        return None
    return payload


def make_state(payload: dict[str, Any]) -> str:
    return encode_signed({**payload, "exp": int(time.time()) + STATE_TTL})


def read_state(value: str | None) -> dict[str, Any] | None:
    payload = decode_signed(value)
    if payload is None:
        return None
    try:
        expires = int(payload.get("exp") or 0)
    except (TypeError, ValueError):
        return None
    return payload if expires > int(time.time()) else None


def identity_header(identity: dict[str, Any]) -> str:
    return _b64encode(
        json.dumps(
            {
                "sub": identity.get("sub", ""),
                "email": identity.get("email", ""),
                "name": identity.get("name", ""),
                "groups": identity.get("groups", []),
                "provider": identity.get("provider", ""),
            },
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    )


# ------------------------------------------------------------------------ discovery


_DISCOVERY: dict[str, tuple[float, dict[str, Any]]] = {}
_DISCOVERY_LOCK = threading.Lock()
DISCOVERY_TTL = 3600


async def discovery(provider: dict[str, Any], *, force: bool = False) -> dict[str, Any]:
    issuer = provider["issuer"]
    now = time.time()
    if not force:
        with _DISCOVERY_LOCK:
            cached = _DISCOVERY.get(issuer)
        if cached is not None and cached[0] > now:
            return cached[1]
    url = issuer + "/.well-known/openid-configuration"
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(15.0, connect=5.0), follow_redirects=True
        ) as client:
            response = await client.get(url, headers={"accept": "application/json"})
            response.raise_for_status()
            document = response.json()
    except httpx.HTTPError as exc:
        raise core.CapError(f"OIDC discovery failed for {issuer}: {type(exc).__name__}") from exc
    except ValueError as exc:
        raise core.CapError(f"OIDC discovery returned invalid JSON for {issuer}") from exc
    if not isinstance(document, dict):
        raise core.CapError(f"OIDC discovery returned an invalid document for {issuer}")
    if str(document.get("issuer") or "").rstrip("/") != issuer:
        raise core.CapError(f"OIDC discovery issuer mismatch for {issuer}")
    for key in ("authorization_endpoint", "token_endpoint", "userinfo_endpoint"):
        if not isinstance(document.get(key), str) or not document[key]:
            raise core.CapError(f"OIDC discovery is missing {key} for {issuer}")
    with _DISCOVERY_LOCK:
        _DISCOVERY[issuer] = (now + DISCOVERY_TTL, document)
    return document


async def authorization_url(
    provider: dict[str, Any],
    capability: str,
    next_path: str,
    public_base: str,
) -> tuple[str, str]:
    document = await discovery(provider)
    state = secrets.token_urlsafe(24)
    nonce = secrets.token_urlsafe(24)
    verifier = secrets.token_urlsafe(48)
    challenge = _b64encode(hashlib.sha256(verifier.encode("ascii")).digest())
    params = {
        "response_type": "code",
        "client_id": provider["client_id"],
        "redirect_uri": redirect_uri(public_base),
        "scope": " ".join(provider["scopes"]),
        "state": state,
        "nonce": nonce,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    endpoint = document["authorization_endpoint"]
    separator = "&" if "?" in endpoint else "?"
    cookie = make_state(
        {
            "state": state,
            "cap": capability,
            "provider": provider["id"],
            "verifier": verifier,
            "nonce": nonce,
            "next": next_path,
        }
    )
    return endpoint + separator + urlencode(params), cookie


async def exchange_code(
    provider: dict[str, Any],
    code: str,
    verifier: str,
    public_base: str,
) -> dict[str, Any]:
    document = await discovery(provider)
    data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri(public_base),
        "client_id": provider["client_id"],
        "code_verifier": verifier,
    }
    secret = read_secret(provider["id"])
    auth: tuple[str, str] | None = None
    if secret:
        if provider["token_auth"] == "basic":
            auth = (provider["client_id"], secret)
        else:
            data["client_secret"] = secret
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(15.0, connect=5.0)) as client:
            request_kwargs: dict[str, Any] = {}
            if auth is not None:
                request_kwargs["auth"] = auth
            response = await client.post(
                document["token_endpoint"],
                data=data,
                headers={"accept": "application/json"},
                **request_kwargs,
            )
            response.raise_for_status()
            payload = response.json()
    except httpx.HTTPError as exc:
        raise core.CapError(f"OIDC token exchange failed: {type(exc).__name__}") from exc
    except ValueError as exc:
        raise core.CapError("OIDC token endpoint returned invalid JSON") from exc
    if not isinstance(payload, dict) or not payload.get("access_token"):
        raise core.CapError("OIDC token endpoint returned no access token")
    return payload


async def fetch_identity(
    provider: dict[str, Any], tokens: dict[str, Any]
) -> dict[str, Any]:
    document = await discovery(provider)
    access_token = str(tokens.get("access_token") or "")
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(15.0, connect=5.0)) as client:
            response = await client.get(
                document["userinfo_endpoint"],
                headers={
                    "authorization": f"Bearer {access_token}",
                    "accept": "application/json",
                },
            )
            response.raise_for_status()
            claims = response.json()
    except httpx.HTTPError as exc:
        raise core.CapError(f"OIDC userinfo failed: {type(exc).__name__}") from exc
    except ValueError as exc:
        raise core.CapError("OIDC userinfo returned invalid JSON") from exc
    if not isinstance(claims, dict):
        raise core.CapError("OIDC userinfo returned an invalid document")
    sub = str(claims.get("sub") or "").strip()
    if not sub:
        raise core.CapError("OIDC userinfo has no sub claim")
    email = _claim_text(claims.get(provider["email_claim"])).lower()
    name = _claim_text(claims.get(provider["name_claim"]))
    groups = _claim_groups(claims.get(provider["groups_claim"]))
    return {
        "sub": sub,
        "email": email,
        "name": name,
        "groups": groups,
        "provider": provider["id"],
    }


def _claim_text(value: Any) -> str:
    return str(value).strip() if isinstance(value, (str, int, float)) else ""


def _claim_groups(value: Any) -> list[str]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple)):
        return []
    return [str(item).strip() for item in value if str(item).strip()][:MAX_ALLOW]


def is_allowed(identity: dict[str, Any], spec: core.AuthSpec) -> bool:
    emails = {item.lower() for item in spec.allow_emails}
    domains = {item.lower() for item in spec.allow_domains}
    groups = set(spec.allow_groups)
    if not emails and not domains and not groups:
        return True
    email = str(identity.get("email") or "").lower()
    if email and email in emails:
        return True
    if email and "@" in email and email.rsplit("@", 1)[1] in domains:
        return True
    return bool(groups and set(identity.get("groups") or []) & groups)


# ----------------------------------------------------------------------- provider test


async def test_provider(provider: str) -> dict[str, Any]:
    provider = core.valid_provider_id(provider)
    body = get_provider(provider)
    if body is None:
        raise core.CapError(f"Unknown OIDC provider {provider!r}")
    document = await discovery(body, force=True)
    return {
        "ok": True,
        "issuer": document["issuer"],
        "authorization_endpoint": document["authorization_endpoint"],
        "token_endpoint": document["token_endpoint"],
        "userinfo_endpoint": document["userinfo_endpoint"],
    }
