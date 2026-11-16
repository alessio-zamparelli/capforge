# Next

Known limitations and likely next steps for `capforge`. This is not a dated roadmap; it records
gaps that are deliberately deferred. The design and current guarantees are in
[`ARCHITECTURE.md`](ARCHITECTURE.md).

## Open items

- Enforce network and filesystem isolation if capabilities become less trusted (container/nsjail,
  read-only mounts, egress policy, cgroup limits). The current posture is same-user, no OS sandbox.
  Served web instances have no run timeout (they are resident by design); CPU/memory limits would
  need a cgroup per child, which is deliberately out of scope for now.
- Add authentication if the server is exposed beyond an internal network. The console requires
  `CAPFORGE_WEB_TOKEN`, but the reverse proxy at `:8080/<name>/` is open by design; if capability
  apps themselves must not be public, terminate auth in front of capforge.
- Web proxy gaps, deferred deliberately: WebSocket and HTTP/2 upgrades are not proxied (a
  capability needing them should not rely on the proxy); request/response streaming is buffered
  through starlette with a 32 MB body cap; the proxy has no per-request timeout configuration
  surface beyond httpx defaults.
- Serve registry durability: the registry is a single JSON file, like the rest of the state;
  multi-replica serving is out of scope, as for tags and locks.
- The `:8080` log of served children is bounded (5 MB rotation, one generation), but nothing
  ships those logs anywhere; a future metrics/trace story should account for them.
- Add metrics/traces if the INFO log is not enough; there are no counters today.
- Consider a shared state/lock backend if multi-replica deployment is ever required; the JSON files
  and `flock` assume a single host and a local filesystem.
- Deleting a capability is intentionally left to the shared terminal (`rm -rf`), because the caps
  directory is the model's workspace; `scaffold_capability(overwrite=true)` archives the old draft
  instead of destroying it. Deleting a *served* capability also requires
  `serve_capability(action="stop")` first; the registry entry of a deleted capability is left
  stale until then (restore marks missing versions `stop`).
- The console bundle is optional at runtime (`CAPFORGE_WEB_UI=0` serves a JSON index). Bundling
  new UI features means maintaining React + Tailwind through bun; keep the bundle small.
