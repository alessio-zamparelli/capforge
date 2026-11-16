"""Talk to the running capforge server over real MCP HTTP, the way Open WebUI would.

The probe uses unique capability names and removes them, their tags, venvs, data, locks
and serve-registry entries afterwards when the caps/state directories are visible from
this process. It also drives one `kind: web` capability through the full serving round
trip (start -> proxy -> restart -> stop) against the web listener on :8080.
"""

import json
import os
import shutil
import sys
from pathlib import Path

import httpx

URL = os.environ.get("CAPFORGE_URL", "http://127.0.0.1:8099/mcp")
WEB_URL = os.environ.get("CAPFORGE_WEB_PROBE_URL", "http://127.0.0.1:8080")
CAP_NAME = f"probe_{os.getpid()}"
WEB_NAME = f"webprobe_{os.getpid()}"


def parse(resp: httpx.Response):
    for line in resp.text.splitlines():
        if line.startswith("data: "):
            return json.loads(line[6:])
    return json.loads(resp.text)


def cleanup(contract: dict, name: str) -> None:
    caps = Path(contract["caps_dir"])
    state = Path(contract["state_dir"])
    shutil.rmtree(caps / name, ignore_errors=True)
    for sub in ("versions", "venvs", "data", "backups"):
        shutil.rmtree(state / sub / name, ignore_errors=True)
    shutil.rmtree(state / "served" / name, ignore_errors=True)
    (state / "state" / f"{name}.json").unlink(missing_ok=True)
    (state / "locks" / f"{name}.lock").unlink(missing_ok=True)
    for lock in (state / "locks").glob(f"{name}-*.lock"):
        lock.unlink(missing_ok=True)
    # A served instance must not survive the probe: stop it through the MCP
    # surface first (the caller does that), then drop its registry entry.
    registry = state / "served" / "registry.json"
    try:
        data = json.loads(registry.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    if isinstance(data, dict) and name in data:
        data.pop(name, None)
        tmp = registry.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
        tmp.replace(registry)


def main() -> int:
    client = httpx.Client(
        timeout=180, headers={"Accept": "application/json, text/event-stream"}
    )
    r = client.post(
        URL,
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "probe", "version": "1"},
            },
        },
    )
    sid = r.headers["mcp-session-id"]
    print("session", sid[:8], "->", parse(r)["result"]["serverInfo"])
    client.headers["mcp-session-id"] = sid

    client.post(URL, json={"jsonrpc": "2.0", "method": "notifications/initialized"})

    listed = parse(client.post(URL, json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"}))
    names = [t["name"] for t in listed["result"]["tools"]]
    print("tools/list ->", names)
    assert len(names) == 11, names
    assert "serve_capability" in names, names

    def call(name, args):
        resp = parse(
            client.post(
                URL,
                json={
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "tools/call",
                    "params": {"name": name, "arguments": args},
                },
            )
        )
        if "error" in resp:
            raise SystemExit(f"{name} -> protocol error {resp['error']}")
        content = resp["result"]["content"]
        text = next(c["text"] for c in content if c["type"] == "text")
        return json.loads(text)

    contract = call("get_contract", {})
    print("caps_dir", contract["caps_dir"], "keep", contract["keep_tags"])
    try:
        made = call(
            "scaffold_capability",
            {"name": CAP_NAME, "description": "Counts words in a text."},
        )
        print("scaffold ->", made["ok"], made.get("files"))

        tested = call("test_capability", {"name": CAP_NAME, "args": {"text": "one two three"}})
        print("test ->", tested["ok"], tested.get("run", {}).get("result"))

        promo = call("promote_capability", {"name": CAP_NAME, "tag": "v1", "note": "initial"})
        print("promote ->", promo["ok"], promo.get("tag"), promo.get("git_commit"))

        ran = call("run_capability", {"name": CAP_NAME, "args": {"text": "a b"}})
        print("run ->", ran["ok"], ran["version"], ran["result"])

        tags = call("list_tags", {"name": CAP_NAME, "diff_against_active": True})
        print("tags ->", [t["tag"] for t in tags["tags"]], "active", tags["active"])

        listed = call("list_capabilities", {})
        print(
            "list ->",
            [(c["name"], c["active"], c["draft_changed"]) for c in listed["capabilities"]],
        )

        installed = call("install_packages", {"name": CAP_NAME, "packages": ["six"]})
        print("install ->", installed["ok"], installed.get("packages"), installed.get("error", "")[:80])

        missing = call("run_capability", {"name": "does_not_exist", "args": {}})
        print("bad name ->", missing["ok"], missing["error"][:70])

        # ---- kind: web round trip: scaffold -> test (boot probe) -> promote -> serve
        web_made = call(
            "scaffold_capability",
            {"name": WEB_NAME, "description": "Probe web service.", "kind": "web"},
        )
        print("web scaffold ->", web_made["ok"], web_made.get("kind"), web_made.get("files"))

        web_tested = call("test_capability", {"name": WEB_NAME})
        print("web test (boot probe) ->", web_tested["ok"])

        web_promo = call("promote_capability", {"name": WEB_NAME, "tag": "v1", "note": "web"})
        print("web promote ->", web_promo["ok"], web_promo.get("tag"))

        refused = call("run_capability", {"name": WEB_NAME, "args": {}})
        print("run on web cap refused ->", refused["ok"] is False, refused.get("error", "")[:60])

        started = call("serve_capability", {"name": WEB_NAME, "action": "start"})
        print("serve start ->", started["ok"], started.get("status"), started.get("url"))
        assert started.get("status") == "running", started
        prefix = started["url"].rstrip("/")
        with httpx.Client(timeout=30) as web_client:
            health = web_client.get(f"{prefix}/health")
            print("proxy /health ->", health.status_code, health.text[:60])
            assert health.status_code == 200
            bare = web_client.get(f"{WEB_URL}/{WEB_NAME}", follow_redirects=False)
            print("bare /<name> ->", bare.status_code, bare.headers.get("location"))
            assert bare.status_code == 307
            unserved = web_client.get(f"{WEB_URL}/no_such_probe_cap/health")
            print("unserved ->", unserved.status_code)
            assert unserved.status_code == 404

        restarted = call("serve_capability", {"name": WEB_NAME, "action": "restart"})
        print("serve restart ->", restarted["ok"], restarted.get("status"))

        stopped = call("serve_capability", {"name": WEB_NAME, "action": "stop"})
        print("serve stop ->", stopped["ok"], stopped.get("status"))
        assert stopped.get("status") == "stopped", stopped
    finally:
        cleanup(contract, CAP_NAME)
        cleanup(contract, WEB_NAME)
        print("cleaned up", CAP_NAME, WEB_NAME)
    return 0


if __name__ == "__main__":
    sys.exit(main())
