"""Talk to a running capforge server over real MCP HTTP, the way Open WebUI would.

Exercises the git-authored loop: scaffold -> token -> clone -> commit -> push ->
pipeline -> promoted -> run, plus one web capability start/proxy/restart/stop.
"""

import base64
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx

URL = os.environ.get("CAPFORGE_URL", "http://127.0.0.1:8099/mcp")
WEB_URL = os.environ.get("CAPFORGE_WEB_PROBE_URL", "http://127.0.0.1:8080")
CAP_NAME = f"probe_{os.getpid()}"
WEB_NAME = f"webprobe_{os.getpid()}"
TOOLS = [
    "get_contract",
    "list_capabilities",
    "get_capability",
    "scaffold_capability",
    "run_capability",
    "serve_capability",
    "staging",
    "delete_capability",
    "rollback_capability",
    "list_tags",
]


def parse(resp: httpx.Response):
    for line in resp.text.splitlines():
        if line.startswith("data: "):
            return json.loads(line[6:])
    return json.loads(resp.text)


def cleanup(contract: dict, name: str) -> None:
    caps = Path(contract["caps_dir"])
    state = Path(contract["state_dir"])
    shutil.rmtree(caps / name, ignore_errors=True)
    for sub in ("versions", "venvs", "data", "backups", "staging", "logs"):
        shutil.rmtree(state / sub / name, ignore_errors=True)
    shutil.rmtree(state / "git" / f"{name}.git", ignore_errors=True)
    shutil.rmtree(state / "served" / name, ignore_errors=True)
    (state / "state" / f"{name}.json").unlink(missing_ok=True)
    (state / "staging" / f"{name}.json").unlink(missing_ok=True)
    for leftover in (caps / ".staging").glob(f"{name}-*"):
        shutil.rmtree(leftover, ignore_errors=True)
    for leftover in (caps / ".staging" / "backups").glob(f"{name}-*"):
        shutil.rmtree(leftover, ignore_errors=True)
    shutil.rmtree(state / "git" / f"{name}.git", ignore_errors=True)
    for lock in (state / "locks").glob(f"{name}*.lock"):
        lock.unlink(missing_ok=True)


def push(name: str, token: str, git_url: str, basic_username: str, edit) -> None:
    work = Path(tempfile.mkdtemp(prefix=f"{name}-probe-"))
    try:
        encoded = base64.b64encode(f"{basic_username}:{token}".encode()).decode()
        env = dict(os.environ)
        env.update(
            {
                "GIT_CONFIG_COUNT": "1",
                "GIT_CONFIG_KEY_0": "http.extraHeader",
                "GIT_CONFIG_VALUE_0": f"Authorization: Basic {encoded}",
            }
        )
        proc = subprocess.run(
            ["git", "clone", "-q", git_url, str(work)],
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert proc.returncode == 0, proc.stderr
        edit(work)
        for args in (
            ["git", "-C", str(work), "add", "-A"],
            [
                "git",
                "-C",
                str(work),
                "-c",
                "user.email=probe@example.test",
                "-c",
                "user.name=probe",
                "commit",
                "-q",
                "-m",
                "probe update",
            ],
            ["git", "-C", str(work), "push", "-q", "origin", "HEAD:main"],
        ):
            proc = subprocess.run(args, env=env, capture_output=True, text=True, timeout=60)
            assert proc.returncode == 0, proc.stderr
    finally:
        shutil.rmtree(work, ignore_errors=True)


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
                "clientInfo": {"name": "probe", "version": "2"},
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
    assert names == TOOLS, names

    def call(name: str, args: dict) -> dict:
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
        text = next(c["text"] for c in resp["result"]["content"] if c["type"] == "text")
        return json.loads(text)

    contract = call("get_contract", {})
    print("caps_dir", contract["caps_dir"], "keep", contract["keep_tags"])
    try:
        made = call(
            "scaffold_capability",
            {"name": CAP_NAME, "description": "Counts words in a text."},
        )
        print("scaffold ->", made["ok"], made["git_url"])

        minted = call("staging", {"name": CAP_NAME, "action": "token"})
        print("token ->", minted["ok"], minted["ttl_s"], "s")
        push(
            CAP_NAME,
            minted["token"],
            minted["git_url"],
            minted["basic_username"],
            lambda work: (work / "main.py").write_text(
                (work / "main.py").read_text() + "\n# probe\n", encoding="utf-8"
            ),
        )

        state = {}
        for _ in range(180):
            state = call("staging", {"name": CAP_NAME, "action": "latest"})
            print("pipeline ->", state.get("state"), state.get("tag"))
            if state.get("state") in {"promoted", "unchanged", "failed", "interrupted"}:
                break
            time.sleep(1)
        assert state.get("state") == "promoted", state
        logs = call("staging", {"name": CAP_NAME, "action": "logs"})
        print("logs ->", len(logs["text"]), "bytes")

        ran = call("run_capability", {"name": CAP_NAME, "args": {"text": "a b"}})
        print("run ->", ran["ok"], ran["version"], ran["result"])
        assert ran["result"] == {"text": "a b", "length": 3}

        tags = call("list_tags", {"name": CAP_NAME})
        print("tags ->", [t["tag"] for t in tags["tags"]], "active", tags["active"])
        listed = call("list_capabilities", {})
        print("list ->", [(c["name"], c["active"]) for c in listed["capabilities"]])
        missing = call("run_capability", {"name": "does_not_exist", "args": {}})
        print("bad name ->", missing["ok"], missing["error"][:70])

        web_made = call(
            "scaffold_capability",
            {"name": WEB_NAME, "description": "Probe web service.", "kind": "web"},
        )
        print("web scaffold ->", web_made["ok"], web_made["git_url"])
        web_minted = call("staging", {"name": WEB_NAME, "action": "token"})
        push(
            WEB_NAME,
            web_minted["token"],
            web_minted["git_url"],
            web_minted["basic_username"],
            lambda work: (work / "AGENTS.md").write_text(
                (work / "AGENTS.md").read_text() + "\n", encoding="utf-8"
            ),
        )
        web_state = {}
        for _ in range(180):
            web_state = call("staging", {"name": WEB_NAME, "action": "latest"})
            print("web pipeline ->", web_state.get("state"), web_state.get("tag"))
            if web_state.get("state") in {"promoted", "unchanged", "failed", "interrupted"}:
                break
            time.sleep(1)
        assert web_state.get("state") == "promoted", web_state

        started = call("serve_capability", {"name": WEB_NAME, "action": "start"})
        print("serve start ->", started["ok"], started.get("status"), started.get("url"))
        assert started.get("status") == "running", started
        prefix = started["url"].rstrip("/")
        with httpx.Client(timeout=30) as web_client:
            health = web_client.get(f"{prefix}/health")
            print("proxy /health ->", health.status_code, health.text[:60])
            assert health.status_code == 200
            bare = web_client.get(f"{WEB_URL}/cap/{WEB_NAME}", follow_redirects=False)
            print("bare /cap/<name> ->", bare.status_code, bare.headers.get("location"))
            assert bare.status_code == 307
            unserved = web_client.get(f"{WEB_URL}/cap/no_such_probe_cap/health")
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
