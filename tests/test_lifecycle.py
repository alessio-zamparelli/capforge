"""End-to-end git-authored lifecycle: scaffold, uv env, push pipeline, promote, data."""

from __future__ import annotations

import importlib
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.slow


@pytest.fixture()
def env(monkeypatch):
    root = Path(tempfile.mkdtemp(prefix="capforge-e2e-"))
    monkeypatch.setenv("CAPFORGE_CAPS_DIR", str(root / "caps"))
    monkeypatch.setenv("CAPFORGE_STATE_DIR", str(root / "state"))
    monkeypatch.setenv("CAPFORGE_PUBLIC_URL", "http://capforge.test")
    monkeypatch.setenv("CAPFORGE_KEEP_TAGS", "2")
    monkeypatch.setenv("CAPFORGE_RUN_TIMEOUT", "60")
    monkeypatch.setenv("CAPFORGE_TEST_TIMEOUT", "180")
    monkeypatch.setenv("CAPFORGE_INSTALL_TIMEOUT", "180")
    monkeypatch.setenv("CAPFORGE_SANDBOX", "require")
    monkeypatch.setenv("CAPFORGE_WEB_PORT", "0")
    (root / "caps").mkdir(parents=True)
    from capforge import core, gitremote, scaffold, staging, versioning

    importlib.reload(core)
    importlib.reload(versioning)
    importlib.reload(gitremote)
    importlib.reload(staging)
    importlib.reload(scaffold)
    from capforge import server

    importlib.reload(server)
    yield root, server
    shutil.rmtree(root, ignore_errors=True)


def _commit_to_main(name: str, mutate) -> str:
    from capforge import gitremote

    work = Path(tempfile.mkdtemp(prefix=f"{name}-clone-"))
    try:
        proc = subprocess.run(
            ["git", "clone", "-q", str(gitremote.repo_path(name)), str(work)],
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert proc.returncode == 0, proc.stderr
        mutate(work)
        for args in (
            ["git", "-C", str(work), "add", "-A"],
            [
                "git",
                "-C",
                str(work),
                "-c",
                "user.email=test@example.test",
                "-c",
                "user.name=test",
                "commit",
                "-q",
                "-m",
                "test update",
            ],
        ):
            proc = subprocess.run(args, capture_output=True, text=True, timeout=60)
            assert proc.returncode == 0, proc.stderr
        return gitremote.publish(name, work)
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _wait(name: str, timeout: float = 120) -> dict:
    from capforge import staging

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        state = staging.latest(name)
        if state.get("state") in {"promoted", "unchanged", "failed", "interrupted"}:
            return state
        time.sleep(0.5)
    return staging.latest(name)


def test_scaffold_creates_uv_project_and_repo(env):
    _, s = env
    res = s.scaffold_capability("hello", "Measures a string.")
    assert res["ok"], res
    root = Path(res["path"])
    assert (root / "SKILL.md").is_file()
    assert (root / "deploy.py").is_file()
    assert (root / "main.py").is_file()
    assert (root / "pyproject.toml").is_file()
    assert (root / "uv.lock").is_file()
    assert (root / ".python-version").read_text().strip() == "3.14"
    assert (Path(res["git_url"]).name if False else res["git_url"]).endswith("/git/hello.git")
    listed = s.list_capabilities()
    assert listed["count"] == 1
    assert listed["capabilities"][0]["draft_ready"] is True


def test_scaffold_lists_generated_files(env):
    _, s = env
    made = s.scaffold_capability("hello", "d")
    assert made["ok"], made
    assert "tests/test_scaffold.py" in made["files"]
    assert "deploy.py" in made["files"]
    detail = s.get_capability("hello")
    assert "tests/test_scaffold.py" in detail["files"]
    assert "deploy.py" in detail["files"]
    deploy = (Path(made["path"]) / "deploy.py").read_text(encoding="utf-8")
    assert 'KIND = "cli"' in deploy
    assert 'ENTRYPOINT = "main.py"' in deploy
    assert "pydantic" in (Path(made["path"]) / "pyproject.toml").read_text(encoding="utf-8")
    assert "entrypoint:" not in (Path(made["path"]) / "SKILL.md").read_text(encoding="utf-8")
    test_source = (Path(made["path"]) / "tests" / "test_scaffold.py").read_text(encoding="utf-8")
    assert "anyio" not in test_source

    web = s.scaffold_capability("webscaffold", "web", kind="web")
    assert web["ok"]
    web_test = (Path(web["path"]) / "tests" / "test_scaffold.py").read_text(encoding="utf-8")
    assert "anyio" not in web_test
    assert "asyncio" in web_test


def test_web_pipeline_log_contains_pytest_and_boot(env):
    _, s = env
    assert s.scaffold_capability("websvc", "web", kind="web")["ok"]

    def mutate(work: Path) -> None:
        (work / "AGENTS.md").write_text(
            (work / "AGENTS.md").read_text(encoding="utf-8") + "\n", encoding="utf-8"
        )

    sha = _commit_to_main("websvc", mutate)
    from capforge import staging

    staging.start("websvc", sha, {"id": "tester"}, "test web")
    state = _wait("websvc")
    assert state["state"] == "promoted", state
    logs = s.staging("websvc", action="logs")["text"]
    assert "pytest:" in logs and "passed" in logs
    assert "boot probe:" in logs


def test_run_draft_builds_uv_env(env):
    _, s = env
    assert s.scaffold_capability("hello", "d")["ok"]
    res = s.run_capability("hello", {"text": "abc"})
    assert res["ok"], res
    assert res["mode"] == "draft"
    assert res["result"] == {"text": "abc", "length": 3}
    assert "state/venvs/hello" in res.get("data_dir", "").replace("data", "venvs")


def test_versioning_promote_and_rollback(env):
    root, s = env
    assert s.scaffold_capability("hello", "d")["ok"]
    from capforge import versioning

    first = versioning.promote("hello", tag="v1", note="first")
    assert first["promoted"] is True
    assert first["env"] == "uv"
    assert first["lock_hash"]
    assert s.run_capability("hello", {"text": "ab"})["version"] == "v1"

    main = root / "caps" / "hello" / "main.py"
    main.write_text(main.read_text() + "\n# draft edit\n", encoding="utf-8")
    second = versioning.promote("hello", tag="v2")
    assert second["promoted"] is True and second["previous"] == "v1"
    back = s.rollback_capability("hello", "v1")
    assert back["ok"] and back["was"] == "v2"
    assert s.run_capability("hello", {"text": "ab"})["version"] == "v1"


def test_pipeline_push_promotes_on_green(env):
    root, s = env
    assert s.scaffold_capability("hello", "d")["ok"]

    def mutate(work: Path) -> None:
        main = work / "main.py"
        main.write_text(main.read_text() + "\n# pushed\n", encoding="utf-8")

    sha = _commit_to_main("hello", mutate)
    from capforge import staging

    started = staging.start("hello", sha, {"id": "tester"}, "test update")
    assert started["ok"]
    state = _wait("hello")
    assert state["state"] == "promoted", state
    assert state["tag"]
    assert state["sha"] == sha
    assert state["git_source"]["sha"] == sha
    assert s.run_capability("hello", {"text": "abc"})["result"] == {"text": "abc", "length": 3}
    logs = s.staging("hello", action="logs")
    assert logs["ok"] and logs["text"]
    backups = root / "caps" / ".staging" / "backups"
    assert not list(backups.glob("hello-*")), "a successful promotion must not leak a draft backup"


def test_pipeline_red_tests_leave_active_untouched(env):
    root, s = env
    assert s.scaffold_capability("hello", "d")["ok"]
    from capforge import staging, versioning

    versioning.promote("hello", tag="v1")
    active_before = s.run_capability("hello", {"text": "a"})["result"]

    def mutate(work: Path) -> None:
        (work / "main.py").write_text("print('not json')\n", encoding="utf-8")

    sha = _commit_to_main("hello", mutate)
    staging.start("hello", sha, {"id": "tester"}, "break it")
    state = _wait("hello")
    assert state["state"] == "failed", state
    assert state["tag"] is None
    assert s.run_capability("hello", {"text": "a"})["result"] == active_before
    assert s.run_capability("hello", {"text": "a"})["version"] == "v1"


def test_staging_token_and_auth(env):
    _, s = env
    assert s.scaffold_capability("hello", "d")["ok"]
    minted = s.staging("hello", action="token")
    assert minted["ok"] and minted["token"].startswith("cfg_")
    assert minted["git_url"].endswith("/git/hello.git")
    from capforge import staging

    assert staging.verify(minted["token"], "hello") is not None
    assert staging.verify(minted["token"], "other") is None
    assert staging.verify("cfg_bad.bad", "hello") is None


def test_shared_data_survives_new_tag(env):
    root, s = env
    assert s.scaffold_capability("hello", "d")["ok"]
    deploy = root / "caps" / "hello" / "deploy.py"
    deploy.write_text(
        deploy.read_text().replace('DATA = "version"', 'DATA = "shared"'), encoding="utf-8"
    )
    main = root / "caps" / "hello" / "main.py"
    main.write_text(
        "import json, os, sys\n"
        "from pathlib import Path\n"
        "args=json.loads(sys.stdin.read() or '{}')\n"
        "p=Path(os.environ.get('CAPFORGE_SHARED_DATA_DIR', os.environ['CAPFORGE_DATA_DIR']))/'n'\n"
        "n=int(p.read_text())+1 if p.exists() else 1\n"
        "p.write_text(str(n))\n"
        "json.dump({'n':n}, sys.stdout)\n",
        encoding="utf-8",
    )
    (root / "caps" / "hello" / "tests" / "test_scaffold.py").write_text(
        "def test_ok():\n    assert True\n", encoding="utf-8"
    )
    from capforge import versioning

    versioning.promote("hello", tag="v1")
    assert s.run_capability("hello")["result"] == {"n": 1}
    versioning.promote("hello", tag="v2", force=True)
    assert s.run_capability("hello")["result"] == {"n": 2}


def test_operator_env_reaches_active_run_but_not_tests(env):
    root, s = env
    from capforge import core

    assert s.scaffold_capability("envcap", "Reads an operator env value.")["ok"]
    cap = root / "caps" / "envcap"
    (cap / "main.py").write_text(
        "import json, os, sys\n"
        "json.loads(sys.stdin.read() or '{}')\n"
        "json.dump({'key': os.environ.get('DEMO_KEY')}, sys.stdout)\n",
        encoding="utf-8",
    )
    (cap / "tests" / "test_scaffold.py").write_text(
        "import os\n\n"
        "def test_placeholder():\n"
        "    assert os.environ.get('DEMO_KEY') == 'capforge-test-placeholder'\n",
        encoding="utf-8",
    )
    deploy = cap / "deploy.py"
    deploy.write_text(
        deploy.read_text(encoding="utf-8").replace("# API_KEY: str", "DEMO_KEY: str"),
        encoding="utf-8",
    )
    core.set_cap_env("envcap", "DEMO_KEY", "real-secret")

    tested = s.test_capability("envcap", {})
    assert tested["ok"], tested
    assert "real-secret" not in str(tested)
    assert tested["run"]["result"] == {"key": core.ENV_PLACEHOLDER}

    assert s.promote_capability("envcap", tag="v1")["ok"]
    run = s.run_capability("envcap")
    assert run["ok"], run
    assert run["result"] == {"key": "real-secret"}

    core.delete_cap_env("envcap", "DEMO_KEY")
    missing = s.run_capability("envcap")
    assert missing["ok"] is False and "DEMO_KEY" in missing["error"]


def test_sandbox_denies_state_and_other_caps(env):
    root, s = env
    assert s.scaffold_capability("hello", "d")["ok"]
    target = root / "state" / "state" / "hello.json"
    main = root / "caps" / "hello" / "main.py"
    main.write_text(
        "import json, sys\n"
        f"try:\n    open({str(target)!r}).read()\n"
        "except Exception as e:\n    json.dump({'denied': type(e).__name__}, sys.stdout)\n"
        "else:\n    json.dump({'denied': False}, sys.stdout)\n",
        encoding="utf-8",
    )
    res = s.run_capability("hello")
    assert res["ok"], res
    assert res["result"]["denied"] == "PermissionError"


def test_prune_removes_per_version_data(env):
    root, s = env
    assert s.scaffold_capability("hello", "d")["ok"]
    from capforge import core, versioning

    for tag in ("v1", "v2", "v3"):
        core.data_dir("hello", tag).mkdir(parents=True, exist_ok=True)
        (core.data_dir("hello", tag) / "x").write_text(tag, encoding="utf-8")
        versioning.promote("hello", tag=tag, force=True)
    assert not (root / "state" / "data" / "hello" / "v1").exists()
    assert s.list_tags("hello")["active"] == "v3"


def test_cross_capability_shared_folder_requires_approval_and_honours_access(env):
    root, s = env
    from capforge import core, versioning

    writer_main = (
        "import json, os, sys\n"
        "from pathlib import Path\n"
        "json.loads(sys.stdin.read() or '{}')\n"
        "shares = json.loads(os.environ.get('CAPFORGE_SHARES', '{}'))\n"
        "entry = shares['shared-data']\n"
        "path = Path(entry['path']) / 'value.txt'\n"
        "path.write_text('from-writer')\n"
        "json.dump({'access': entry['access'], 'read': path.read_text()}, sys.stdout)\n"
    )
    reader_main = (
        "import json, os, sys\n"
        "from pathlib import Path\n"
        "json.loads(sys.stdin.read() or '{}')\n"
        "entry = json.loads(os.environ['CAPFORGE_SHARES'])['shared-data']\n"
        "path = Path(entry['path']) / 'value.txt'\n"
        "json.dump({'access': entry['access'], 'value': path.read_text()}, sys.stdout)\n"
    )
    blocked_main = (
        "import json, os, sys\n"
        "from pathlib import Path\n"
        "json.loads(sys.stdin.read() or '{}')\n"
        "entry = json.loads(os.environ['CAPFORGE_SHARES'])['shared-data']\n"
        "path = Path(entry['path']) / 'blocked.txt'\n"
        "path.write_text('should-not-happen')\n"
        "json.dump({'wrote': True}, sys.stdout)\n"
    )

    for name, main, access in (
        ("writer", writer_main, "rw"),
        ("reader", reader_main, "ro"),
        ("blocked", blocked_main, "ro"),
    ):
        assert s.scaffold_capability(name, f"{name} share test")["ok"]
        cap = root / "caps" / name
        (cap / "main.py").write_text(main, encoding="utf-8")
        (cap / "tests" / "test_scaffold.py").write_text(
            "def test_ok():\n    assert True\n", encoding="utf-8"
        )
        with (cap / "deploy.py").open("a", encoding="utf-8") as handle:
            handle.write(f'\nSHARES = {{"shared-data": "{access}"}}\n')

    # Declaration is only a request: nothing is mounted before approval.
    pending = s.run_capability("writer", {})
    assert pending["ok"] is False
    assert "not approved" in pending["error"]

    core.create_share("shared-data", "cross-cap data")
    core.set_share_grant("writer", "shared-data", "rw")
    core.set_share_grant("reader", "shared-data", "ro")
    core.set_share_grant("blocked", "shared-data", "ro")
    for name in ("writer", "reader", "blocked"):
        versioning.promote(name, tag="v1")

    wrote = s.run_capability("writer", {})
    assert wrote["ok"] is True, wrote
    assert wrote["result"] == {"access": "rw", "read": "from-writer"}

    read = s.run_capability("reader", {})
    assert read["ok"] is True, read
    assert read["result"] == {"access": "ro", "value": "from-writer"}

    denied = s.run_capability("blocked", {})
    assert denied["ok"] is False, denied
    assert not (core.share_path("shared-data") / "blocked.txt").exists()

    # Revocation is immediate for the next run; the folder and data remain.
    core.revoke_share_grant("writer", "shared-data")
    revoked = s.run_capability("writer", {})
    assert revoked["ok"] is False
    assert "not approved" in revoked["error"]
    assert (core.share_path("shared-data") / "value.txt").read_text() == "from-writer"
