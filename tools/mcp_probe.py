"""Talk to the running capforge server over real MCP HTTP, the way Open WebUI would."""

import json
import os
import sys

import httpx

URL = os.environ.get("CAPFORGE_URL", "http://127.0.0.1:8099/mcp")


def parse(resp: httpx.Response):
    for line in resp.text.splitlines():
        if line.startswith("data: "):
            return json.loads(line[6:])
    return json.loads(resp.text)


def main() -> int:
    client = httpx.Client(timeout=180, headers={"Accept": "application/json, text/event-stream"})
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
    assert len(names) == 10, names

    def call(name, args):
        resp = parse(
            client.post(
                URL,
                json={"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": name, "arguments": args}},
            )
        )
        if "error" in resp:
            raise SystemExit(f"{name} -> protocol error {resp['error']}")
        content = resp["result"]["content"]
        text = next(c["text"] for c in content if c["type"] == "text")
        return json.loads(text)

    contract = call("get_contract", {})
    print("caps_dir", contract["caps_dir"], "keep", contract["keep_tags"])

    made = call("scaffold_capability", {"name": "wordcount", "description": "Counts words in a text."})
    print("scaffold ->", made["ok"], made.get("files"))

    tested = call("test_capability", {"name": "wordcount", "args": {"text": "one two three"}})
    print("test ->", tested["ok"], tested.get("run", {}).get("result"))

    promo = call("promote_capability", {"name": "wordcount", "tag": "v1", "note": "initial"})
    print("promote ->", promo["ok"], promo.get("tag"), promo.get("git_commit"))

    ran = call("run_capability", {"name": "wordcount", "args": {"text": "a b"}})
    print("run ->", ran["ok"], ran["version"], ran["result"])

    tags = call("list_tags", {"name": "wordcount", "diff_against_active": True})
    print("tags ->", [t["tag"] for t in tags["tags"]], "active", tags["active"])

    listed = call("list_capabilities", {})
    print("list ->", [(c["name"], c["active"], c["draft_changed"]) for c in listed["capabilities"]])

    installed = call("install_packages", {"name": "wordcount", "packages": ["six"]})
    print("install ->", installed["ok"], installed.get("packages"), installed.get("error", "")[:80])

    missing = call("run_capability", {"name": "does_not_exist", "args": {}})
    print("bad name ->", missing["ok"], missing["error"][:70])
    return 0


if __name__ == "__main__":
    sys.exit(main())
