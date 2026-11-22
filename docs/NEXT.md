# Next

Known limitations and deliberately deferred work for capforge 0.4.0. The design and current
guarantees are in [`ARCHITECTURE.md`](ARCHITECTURE.md).

## Open items

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
- **Repository growth.** Per-repo quota (`CAPFORGE_REPO_QUOTA_BYTES`) and a post-promotion
  `git gc` exist, but there is no operator dashboard or automatic orphan-repo cleanup.
- **Legacy tags.** The pip/`packages` runtime was removed in 0.4.0. Tags promoted by older
  releases are refused with a migration error; operators must re-promote them with
  `pyproject.toml`/`uv.lock` (or restore the code into a clone and push). There is no automatic
  converter.
- **`deploy.py` migration.** The 0.5.0 hard cut requires `deploy.py`; tags created by 0.4.x have
  no descriptor and are refused, so they cannot be rolled back to. Re-push the capability with a
  `deploy.py` and promote a new tag to migrate it.
- **Capability deletion.** `delete_capability(name, confirm)` and `capforge delete` remove a
  capability and its state; there is no undo/archive, and scaffold overwrite does not exist.
- **Secrets.** Operator-managed env values are plaintext JSON with mode `0600` under
  `<state>/secrets/`; there is no at-rest encryption, rotation history, reveal or deletion
  workflow beyond the console. They are per capability and shared across tags, so a promoted push
  can read them at runtime. See `docs/ARCHITECTURE.md` §10.
- **Shared data.** `data: shared` is unversioned and unsynchronized. Overlapping CLI calls and a
  served web child can race; the capability must use SQLite/locks. Backups are the operator's job.
- **Logs.** One rotated generation per pipeline/scaffold; served child logs rotate at 5 MB. There
  is no log shipping, history, or console log endpoint yet.
- **Console.** The React bundle is served under `/ui/` and uses relative `/api` calls. It is not
  covered by a browser test; route/asset behaviour is tested through ASGI.
- **Multi-replica.** State, locks and repos assume one host and a local filesystem. A shared
  state/lock backend is out of scope.
- **Docker.** The image targets a modern kernel for Landlock. Old kernels require
  `CAPFORGE_SANDBOX=warn`/`off`, which is not a security boundary.
