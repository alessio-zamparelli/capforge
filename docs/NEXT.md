# Next

Known limitations and deliberately deferred work for capforge 0.8.0. The design and current
guarantees are in [`ARCHITECTURE.md`](ARCHITECTURE.md).

## Open items

- **LLM providers.** Only OpenAI-compatible endpoints are supported; native Anthropic/Gemini
  translation is deliberately out of scope. There is no token/usage accounting, per-capability
  quota, retry policy, or streaming backpressure beyond the raw passthrough. Provider keys are
  plaintext at mode `0600` (same as `Env` secrets) with no rotation history. A capability could
  still call any reachable model endpoint directly because network egress is not confined.
- **Model selection.** The operator's selection is enforced at the gateway, but a capability can
  read `CAPFORGE_LLM_MODEL` and tailor prompts to it. There is no per-tag selection history and no
  automatic re-resolution when a provider is renamed; a stale selection falls back to the default
  and is flagged in the console.
- **OIDC.** ID-token signatures are not verified (userinfo is the trust anchor). There is no group
  sync, role model, refresh token, back-channel logout, or provider-initiated logout. Sessions are
  bearer cookies with no server-side revocation list; rotating `<state>/auth.key` (or restarting)
  invalidates them. Discovery is cached for an hour and a provider outage fails login closed.
  `CAPFORGE_PUBLIC_URL` must be set explicitly; a reverse-proxy path prefix is not supported for
  the callback.
- **Capability cookies.** The proxy drops upstream `Set-Cookie`, so a served capability cannot set
  its own session cookies; it must rely on the forwarded `X-Capforge-User*` identity.
- **Landlock coverage.** Capability, pytest and served web children use Landlock. `git
  http-backend`, `git archive` and `uv sync` are tracked but not yet under a Landlock policy.
  Closing that gap should not require new dependencies.
- **Upload streaming.** `git-upload-pack` responses are currently buffered by the CGI bridge.
  Large repositories should stream to the client with a background task that kills the child on
  disconnect.
- **Network, CPU and memory.** Landlock is filesystem-only. There is no network namespace,
  seccomp, cgroup or memory limit; a capability can still make outbound calls and consume CPU.
  For untrusted code, run it in a separate container/user with egress policy and cgroup limits, or
  add Bubblewrap/nsjail (see the comparison in `plans/`).
- **Git tokens.** One token grants clone and push for 600 s. There is no read-only scope and no
  revocation UI; minting a new token and waiting out the TTL is the current lever.
- **Pipeline queue and cancellation.** A push is refused with `409` while a pipeline runs, and a
  long `uv sync` cannot be cancelled from the console or MCP. Coalescing to the latest push and a
  cooperative cancel that kills the process group are deferred.
- **Scaffold/template versioning.** Capabilities are frozen at scaffold time. A future contract
  change needs an upgrade path for `AGENTS.md`, `deploy.py` and `pyproject.toml`.
- **Backup/export.** State plus `versions/` is the only copy of every tag. There is no operator
  export/import or backup command.
- **Repository growth.** There is no per-repository quota or automatic orphan-repo cleanup. A
  post-promotion `git gc` exists.
- **Legacy tags.** The pip/`packages` runtime was removed in 0.4.0. Tags promoted by older
  releases are refused with a migration error; operators must re-promote them with
  `pyproject.toml`/`uv.lock` (or restore the code into a clone and push). There is no automatic
  converter.
- **`deploy.py` migration.** The 0.5.0 hard cut requires `deploy.py`; tags created by 0.4.x have
  no descriptor and are refused, so they cannot be rolled back to. Re-push the capability with a
  `deploy.py` and promote a new tag to migrate it.
- **Capability deletion.** `delete_capability(name, confirm)` only marks a capability pending; an
  operator purges it via the console or `capforge delete` and can cancel via `capforge
  cancel-delete`. There is no undo/archive after purge, and scaffold overwrite does not exist.
- **Secrets.** Operator-managed env values are plaintext JSON with mode `0600` under
  `<state>/secrets/`; there is no at-rest encryption, rotation history, reveal or deletion
  workflow beyond the console. They are per capability and shared across tags, so a promoted push
  can read them at runtime. See `docs/ARCHITECTURE.md` §10.
- **Shared data.** `DATA = "shared"` and operator-approved `SHARES` folders are unversioned and
  unsynchronized. Overlapping CLI calls and a served web child can race; the capability must use
  SQLite/locks. Backups are the operator's job. There are no quotas, no automatic orphan-folder
  cleanup, no restore workflow, and no per-tag grants.
- **Shared-folder approvals.** Grants are per capability and survive promotions. There is no
  approval history beyond the stored timestamp, no expiry, and no read-only token scope. Revoking
  a grant restarts a desired-running web child but does not kill an in-flight CLI run.
- **Coverage of child processes.** The CLI scaffold covers `main.py` by importing `handle()` and
  executing the real entrypoint in-process with `runpy`; arbitrary subprocesses or threads spawned
  by capability tests still need coverage process-startup instrumentation to be measured.
- **Logs.** One rotated generation per pipeline/scaffold; served child logs rotate at 5 MB. The
  console exposes bounded tails, but there is no log shipping or history.
- **Console and capability origin.** `/ui`, `/api`, `/git` and `/cap` share the web listener. A
  served web capability's JavaScript can call the console API with the operator's session; the
  proxy strips cookies/headers, but same-origin isolation requires serving `/cap` on a different
  origin at the reverse proxy. Splitting the built-in listeners is deliberately deferred.
- **Console.** The React + shadcn bundle is served under `/ui/` and uses relative `/api` calls. It is
  not covered by an automated browser test; route/asset behaviour and the share API are tested
  through ASGI, with manual `agent-browser` smoke checks.
- **Multi-replica.** State, locks and repos assume one host and a local filesystem. A shared
  state/lock backend is out of scope.
- **Docker.** The image targets a modern kernel for Landlock. Old kernels require
  `CAPFORGE_SANDBOX=warn`/`off`, which is not a security boundary.
