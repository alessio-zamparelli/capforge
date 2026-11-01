# Next

Known limitations and likely next steps for `capforge`. This is not a dated roadmap; it records
gaps that are deliberately deferred. The design and current guarantees are in
[`ARCHITECTURE.md`](ARCHITECTURE.md).

## Open items

- Enforce network and filesystem isolation if capabilities become less trusted (container/nsjail,
  read-only mounts, egress policy, cgroup limits). The current posture is same-user, no OS sandbox.
- Add authentication if the server is exposed beyond an internal network.
- Add metrics/traces if the INFO log is not enough; there are no counters today.
- Consider a shared state/lock backend if multi-replica deployment is ever required; the JSON files
  and `flock` assume a single host and a local filesystem.
- Deleting a capability is intentionally left to the shared terminal (`rm -rf`), because the caps
  directory is the model's workspace; `scaffold_capability(overwrite=true)` archives the old draft
  instead of destroying it.
