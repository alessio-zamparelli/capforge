# Configuring OIDC for capforge

This guide is for the **operator**. It covers how to put a served web capability behind an OIDC
identity provider, how to register the callback, and what the capability receives after a
successful sign-in.

OIDC is enforced by the capforge hub. Capability code never sees access tokens, ID tokens or the
client secret. It only receives the verified identity as request headers.

> OIDC applies to **web capabilities only** (`KIND` includes `"web"`). It does not protect the MCP
> endpoint or the console API; those use `CAPFORGE_MCP_TOKEN` and `CAPFORGE_WEB_TOKEN`.

Requirements:

- capforge 0.8.0 or newer.
- A web capability that is promoted and served at `/cap/<name>/`.
- An OIDC provider that supports Authorization Code + PKCE and a userinfo endpoint.
- HTTPS at the public origin. The provider callback must reach the same capforge listener.

---

## 1. How the flow works

```text
browser ──► /cap/<name>/                  hub proxy: no session?
        ◄── 303 /cap/<name>/__auth/login
browser ──► /cap/<name>/__auth/login      hub builds authorize URL + signed state cookie
        ◄── 303 provider /authorize
provider ─► user signs in
browser ──► /__auth/callback?code=...     hub verifies state, exchanges code, reads userinfo
        ◄── 303 back to /cap/<name>/...   sets signed session cookie
browser ──► /cap/<name>/...               hub checks session + allow list, forwards identity
```

Key properties:

- The hub terminates the OIDC flow for every capability. There is one global redirect URI:
  `<CAPFORGE_PUBLIC_URL>/__auth/callback`.
- The authorization request uses PKCE (`S256`), a signed `state` cookie and a `nonce`. The
  nonce is sent for providers that require it; because ID-token signatures are not verified, it is
  not used as a token-binding check.
- The code is exchanged over TLS directly with the provider's token endpoint; the identity is read
  from the provider's userinfo endpoint.
- ID-token signatures are **not** verified. Userinfo, fetched with the access token capforge just
  obtained over TLS, is the trust anchor. This keeps the dependency set at `httpx`.
- The capability receives no cookie or token. The proxy strips incoming `x-capforge-*` headers and
  adds trusted identity headers itself.

---

## 2. Set the public URL

The redirect URI is derived from `CAPFORGE_PUBLIC_URL`, so it must be explicit when OIDC is used.

```sh
export CAPFORGE_PUBLIC_URL="https://capforge.example.com"
export CAPFORGE_WEB_TOKEN="$(openssl rand -hex 32)"
uv run capforge serve
```

`CAPFORGE_PUBLIC_URL` must match the browser-facing origin exactly:

- include the scheme (`https://`);
- include a non-default port if you use one;
- do **not** add a trailing slash;
- note that post-login redirects are root-relative (`/cap/<name>/...`). A path prefix is therefore
  not recommended for OIDC deployments; prefer a dedicated hostname. If you do use a prefix, the
  reverse proxy must strip it so capforge sees `/cap/...` and `/__auth/callback`.

Check the effective value:

```sh
uv run capforge doctor
```

`doctor` prints the number of configured OIDC providers and warns when providers exist but
`CAPFORGE_PUBLIC_URL` is unset. At login time, a missing public URL returns a `503` page rather
than guessing a redirect URI.

> Keep TLS termination in front of capforge. Do not expose the hub over plain HTTP when OIDC is
> in use: the session cookie is only marked `Secure` when `CAPFORGE_PUBLIC_URL` starts with
> `https://`.

---

## 3. Register the redirect URI with your provider

In the provider's application/client settings, register this exact URI:

```text
<CAPFORGE_PUBLIC_URL>/__auth/callback
```

For example:

```text
https://capforge.example.com/__auth/callback
```

It is the same for every capability and every provider. Capabilities are distinguished by the
signed `state` cookie, not by the callback URL.

The console shows the exact value on the **Authentication** tab, and `/api/oidc` returns it as
`redirect_uri`.

Provider notes (confirm details in your provider's documentation, as console labels differ):

| Provider | Typical issuer | Notes |
|---|---|---|
| Keycloak | `https://keycloak.example.com/realms/<realm>` | Add a `groups` client-scope mapper if you want `allow_groups`. |
| Authentik | `https://authentik.example.com/application/o/<slug>/` | The issuer usually ends with a trailing slash; capforge normalises it. |
| Microsoft Entra ID | `https://login.microsoftonline.com/<tenant>/v2.0` | The `groups` claim may need token configuration. |
| Google | `https://accounts.google.com` | Userinfo has `email`/`name`; it has no `groups`, so `allow_groups` will never match. |
| Okta / Auth0 | `https://<tenant>.okta.com` / `https://<tenant>.auth0.com` | Add a custom claim if groups are needed. |
| GitLab | `https://gitlab.example.com` | Use the application's client id and secret; confirm the `openid` scope. |

The provider must expose a discovery document at:

```text
<issuer>/.well-known/openid-configuration
```

whose `issuer` value matches the configured issuer (a trailing slash is tolerated). Discovery is
cached in memory for one hour.

---

## 4. Create the provider in the console

1. Open `/ui/` and sign in with `CAPFORGE_WEB_TOKEN`.
2. Select the **Authentication** tab.
3. Click **New provider** and fill in:

| Field | Meaning | Default / limits |
|---|---|---|
| **Provider id** | Name referenced from `deploy.py`. Lowercase letters, digits, `-`, `_`; starts with a letter or digit; max 40 chars. | required, e.g. `corp` |
| **Label** | Display name in the console. | provider id |
| **Issuer** | OIDC issuer base URL. HTTPS unless loopback (`127.0.0.1`, `localhost`, `::1`). No query or fragment. | required |
| **Client id** | Client/application id at the provider. | required |
| **Scopes** | Space- or comma-separated list. `openid` must be present. | `openid email profile` |
| **Client authentication** | `HTTP Basic` (`client_secret_basic`) or `Form post` (`client_secret_post`) when a secret is set. | `HTTP Basic` |
| **Session lifetime** | Signed session cookie lifetime, in seconds. | `28800` (8 h); min `300`, max `2592000` (30 days) |
| **Email claim** | Userinfo claim used for `allow_emails` / `allow_domains` and the email header. | `email` |
| **Name claim** | Userinfo claim used for `name` in the identity JSON. | `name` |
| **Groups claim** | Userinfo claim used for `allow_groups`. May be a list or a single string. | `groups` |

4. Click **Save provider**.
5. Open the provider's **Secret** dialog and paste the client secret. This is a write-only field;
   the console only reports whether a secret is configured. Leave it unset only for a **public
   client** that relies on PKCE alone.
6. Click **Test** to run discovery. A successful test reports the issuer and the authorization,
   token and userinfo endpoints.
7. Copy the **Redirect URI** shown at the top of the tab and compare it with the provider
   registration from step 3.

If you use `client_secret_post`, choose **Form post**; otherwise the provider will reject the token
exchange with `invalid_client`.

### Limits

- At most 32 OIDC providers.
- At most 32 scopes.
- At most 100 entries per allow list.
- At most 50 `public_paths` entries.

### Where settings live

```text
<CAPFORGE_STATE_DIR>/oidc.json                     providers and non-secret settings
<CAPFORGE_STATE_DIR>/secrets/oidc/<provider>.json  client secret, mode 0600
<CAPFORGE_STATE_DIR>/auth.key                      session signing key, mode 0600, auto-created
```

Back up `<state>/oidc.json` and `<state>/secrets/oidc/` with the rest of the state. Deleting
`auth.key` invalidates every existing session after the next restart; users simply sign in again.

---

## 5. Declare `AUTH` in the capability

The capability opts in from its repository's `deploy.py`. Assignments are parsed statically, so
keep them literal.

```python
KIND = ["web"]
WEB_ENTRYPOINT = "app.py"
APP = "app"

AUTH = {
    "provider": "corp",                  # required; provider id from the console
    "allow_emails": ["alice@example.com"],
    "allow_domains": ["example.com"],
    "allow_groups": ["engineering"],
    "public_paths": ["/health", "/assets"],
}
```

Shorthand for a provider with no restrictions:

```python
AUTH = "corp"
```

This is equivalent to `AUTH = {"provider": "corp"}`.

### Semantics

- `AUTH` is refused when `KIND` does not include `"web"`.
- With **no** `allow_emails`, `allow_domains` or `allow_groups`, any identity the provider
  verifies is accepted.
- If allow lists are present, **any** match grants access:
  - emails are compared case-insensitively with the verified email;
  - domains are the part after `@`, compared case-insensitively;
  - groups are matched exactly against the strings in the groups claim.
- `public_paths` are relative to the capability root and are matched as path prefixes:
  - `/health` matches `/health` and `/health/live`, but not `/healthz`;
  - `/assets` matches `/assets/app.css`;
  - `/` makes the entire capability public (use with care).
- The provider id must exist in the console when the capability is started. A missing provider
  makes `serve_capability(action="start")` fail with a clear error, and the proxy returns `503`
  if the provider is deleted while the capability is running.
- `AUTH` is part of the tag. To change it, push a new tag and restart the served instance.
  Provider settings in the console are read per request, so editing a provider's issuer, scopes,
  secret or allow-related claims applies without a new tag.

### Optional public paths

Only mark endpoints that really must work without a browser session, such as liveness probes or
static assets needed by the signed-out page. Everything else should require authentication.

---

## 6. Promote, serve and verify

```text
scaffold_capability(name, description)        # or edit an existing clone
staging(name, action="token")
git clone ... && edit deploy.py && git push origin HEAD:main
staging(name, action="latest")                # wait for promoted
serve_capability(name, action="start")
```

Then:

1. Open `http://<hub>/cap/<name>/` in a private browser window.
2. The hub should redirect to the provider's sign-in page.
3. After sign-in, you should land back on the capability.
4. Check that the capability sees `X-Capforge-User` (see below).
5. Call `/cap/<name>/__auth/logout` and confirm the session cookie is cleared and the next request
   redirects to sign-in again.

If the capability is already running, use `serve_capability(name, action="restart")` after
promoting a tag with a new `AUTH` block.

---

## 7. What the capability receives

After a successful sign-in, the proxy adds these request headers:

```text
X-Capforge-User: <base64url-encoded JSON>
X-Capforge-User-Email: <verified email>
```

The JSON object has:

```json
{
  "sub": "subject-id",
  "email": "alice@example.com",
  "name": "Alice",
  "groups": ["engineering"],
  "provider": "corp"
}
```

Decode it in Python:

```python
import base64
import json
from urllib.parse import unquote


def identity_from_headers(headers: dict[str, str]) -> dict | None:
    raw = headers.get("x-capforge-user")
    if not raw:
        return None
    padded = raw + "=" * (-len(raw) % 4)
    return json.loads(base64.urlsafe_b64decode(padded))
```

Rules for capability code:

- Trust `X-Capforge-User*` **only** behind the capforge proxy. The proxy strips client-supplied
  `x-capforge-*` headers before forwarding, but a capability reached directly would not have that
  protection.
- Do not implement a second login page or read the hub's cookies. The proxy drops upstream
  `Set-Cookie`, so capability-set cookies do not survive.
- Treat the headers as informational. They are not a substitute for authorization inside the
  capability if it exposes sensitive operations; the `AUTH` allow list is the coarse gate.

Requests that are not authenticated behave differently by shape:

- A `GET`/`HEAD` that accepts HTML gets a `303` redirect to the login route, with `next` pointing
  back into the capability.
- Other requests get `401` with `{"error": "authentication required"}`.
- An authenticated user who fails the allow list gets a `403` HTML page.

---

## 8. Sessions and auth key

- Cookie name: `capforge_cap_<name>`.
- Scope: `Path=/cap/<name>/`; one capability cannot use another's session.
- Flags: `HttpOnly`; `SameSite=Lax`; `Secure` when `CAPFORGE_PUBLIC_URL` is HTTPS.
- Lifetime: the provider's **Session lifetime**, enforced by a signed `exp` claim and `Max-Age`.
- Signing: HMAC-SHA256 with `<state>/auth.key`.
- Logout: `GET /cap/<name>/__auth/logout` deletes the cookie and shows the signed-out page.
- There is no server-side session store or revocation list. Rotating `auth.key` (or deleting it)
  invalidates all sessions; otherwise a copied cookie remains valid until `exp`.

The `state` cookie used during login is separate: `capforge_oidc_state`, `Path=/__auth`,
`HttpOnly`, `SameSite=Lax`, 10-minute lifetime.

---

## 9. Security notes

- **HTTPS everywhere.** The token exchange and userinfo calls are authenticated with the client
  secret and access token. Terminate TLS in front of capforge and never send the browser to a
  plain-HTTP public URL.
- **Callback registration is exact.** A mismatch produces `redirect_uri_mismatch` at the provider.
  The redirect URI must be on the same public origin that users open. Post-login redirects are
  root-relative, so prefer a dedicated hostname; if a path prefix is used, the reverse proxy must
  strip it before forwarding.
- **The console is a separate trust boundary.** `/cap` and `/api` share an origin by default. A
  served capability's JavaScript can reach the console API with the operator's browser session.
  If capabilities are not fully trusted, expose `/cap` on a different origin at the reverse proxy.
  OIDC protects the capability from anonymous users; it does not isolate it from the console.
- **Allow lists are the operator's gate.** A provider that verifies any user in the tenant will
  let any of those users in unless you configure `allow_emails`, `allow_domains` or
  `allow_groups`. Use a test identity to confirm the claims are present.
- **Groups require a claim.** If `allow_groups` never matches, check the groups claim name in the
  console and add the provider-side mapper/claim. Google userinfo, for example, has no groups.
- **Public paths are public.** Do not put secrets or data routes under a `public_paths` prefix.
- **Provider compromise.** Capforge trusts the token endpoint and userinfo endpoint returned by
  discovery. Keep the issuer HTTPS and the provider configuration under operator control.

---

## 10. Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `503 OIDC unavailable`, message says `CAPFORGE_PUBLIC_URL` | Public URL unset | Set `CAPFORGE_PUBLIC_URL` to the browser-facing HTTPS origin and restart the hub. |
| `503 OIDC unavailable`, message says provider is not configured | Provider id missing or deleted | Create it on the **Authentication** tab, or fix the `AUTH` provider id in `deploy.py`. |
| `403 Access denied` after sign-in | Identity does not match any allow list | Check the verified email/domain/groups; adjust the allow lists or the provider claim mapping. |
| Redirect loop back to the provider | Session cookie not stored | Confirm HTTPS, the callback origin, `SameSite=Lax` compatibility, and that the browser accepts cookies for `/cap/<name>/`. |
| `400 Sign-in expired` | State cookie missing or older than 10 minutes, or the browser blocked the cookie | Start the flow again from `/cap/<name>/`; check proxy/cookie settings and clock skew. |
| `400 Sign-in failed`, state did not match | Two tabs or a stale state cookie | Close stale tabs and retry. |
| `redirect_uri_mismatch` at the provider | Registered callback differs from `<CAPFORGE_PUBLIC_URL>/__auth/callback` | Register the exact URI; check scheme, host, port, prefix and trailing slash. |
| `502 OIDC unavailable` on login | Discovery or token exchange failed | Run **Test** in the console; check issuer reachability, DNS, TLS and firewall. |
| `invalid_client` from the token endpoint | Wrong client auth mode or secret | Match **HTTP Basic** vs **Form post** to the provider; re-save the secret. |
| Login succeeds but the capability sees no `X-Capforge-User` | Request bypassed the proxy, or the path is public | Use the `/cap/<name>/` URL; check `public_paths`. |
| Groups allow list never matches | Missing/misnamed groups claim | Set **Groups claim** correctly and add the provider mapper; confirm with **Test** and userinfo. |
| Session ends sooner than expected | Session lifetime, provider session policy or cookie loss | Check **Session lifetime** (min 300 s) and the provider's own session policy. |
| Provider changes have no effect | Capability still runs a tag with an old `AUTH`, or the provider was deleted | The provider store is read per request; for `AUTH` changes push a new tag and restart. |
| Everything works but `capforge doctor` warns about OIDC | Providers exist without `CAPFORGE_PUBLIC_URL` | Set the public URL; it is required to build the callback. |

Useful logs are on the capforge server at `INFO` level with stable fields:

```text
oidc login cap=<name> provider=<id>
oidc denied cap=<name> provider=<id>
oidc callback failed cap=<name> provider=<id> error=...
oidc login failed cap=<name> provider=<id> error=...
```

No tokens, client secrets or identity claims are logged.

---

## 11. Console API (automation)

The same operations are available over the token-authenticated console API. Writes need
`X-CSRF: 1` and the console session cookie.

```sh
# Create the console session; the cookie is stored in cookies.txt.
curl -s -c cookies.txt -H 'content-type: application/json' \
  -d '{"token": "'"$CAPFORGE_WEB_TOKEN"'"}' \
  https://capforge.example.com/api/login

# List providers and the redirect URI.
curl -s -b cookies.txt https://capforge.example.com/api/oidc

# Create or update a provider.
curl -s -b cookies.txt -H 'X-CSRF: 1' \
  -H 'content-type: application/json' \
  -X PUT https://capforge.example.com/api/oidc/providers/corp \
  -d '{
        "label": "Corporate SSO",
        "issuer": "https://login.example.com",
        "client_id": "capforge",
        "scopes": ["openid", "email", "profile"],
        "token_auth": "basic",
        "session_ttl": 28800,
        "email_claim": "email",
        "name_claim": "name",
        "groups_claim": "groups"
      }'

# Store the client secret (write-only).
curl -s -b cookies.txt -H 'X-CSRF: 1' \
  -H 'content-type: application/json' \
  -X PUT https://capforge.example.com/api/oidc/providers/corp/secret \
  -d '{"value": "'"$OIDC_CLIENT_SECRET"'"}'

# Run discovery.
curl -s -b cookies.txt -H 'X-CSRF: 1' \
  -X POST https://capforge.example.com/api/oidc/providers/corp/test
```

Provider responses never include the secret; they only report
`client_secret_configured: true|false`.

---

## 12. Minimal end-to-end example

`deploy.py` in the capability repository:

```python
KIND = ["web"]
WEB_ENTRYPOINT = "app.py"
APP = "app"
DATA = "version"

AUTH = {
    "provider": "corp",
    "allow_domains": ["example.com"],
    "allow_groups": ["capforge-users"],
    "public_paths": ["/health"],
}
```

Operator environment:

```sh
export CAPFORGE_PUBLIC_URL="https://capforge.example.com"
export CAPFORGE_WEB_TOKEN="$(openssl rand -hex 32)"
uv run capforge serve
```

Console:

1. **Authentication → New provider**
   - id `corp`
   - issuer `https://login.example.com`
   - client id and secret from the provider
   - scopes `openid email profile`
   - groups claim `groups`
2. Register `https://capforge.example.com/__auth/callback` at the provider.
3. **Test** discovery, then save.
4. Push the capability and wait for `promoted`.
5. `serve_capability(name, action="start")`, then open
   `https://capforge.example.com/cap/<name>/`.

The capability now requires a signed-in user from `example.com` in the `capforge-users` group,
except for `/health` and `/health/...`.

---

## See also

- [`ARCHITECTURE.md`](ARCHITECTURE.md) — listeners, proxy, execution and security model.
- [`NEXT.md`](NEXT.md) — deliberately deferred OIDC work.
- `README.md` — deployment, environment variables and the capability contract.
