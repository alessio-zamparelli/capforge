"""End-to-end lifecycle: scaffold -> test -> promote -> run -> edit -> rollback."""

from __future__ import annotations

import os
import re
import shutil
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
    monkeypatch.setenv("CAPFORGE_KEEP_TAGS", "3")
    monkeypatch.setenv("CAPFORGE_RUN_TIMEOUT", "60")
    monkeypatch.setenv("CAPFORGE_TEST_TIMEOUT", "180")
    (root / "caps").mkdir(parents=True)
    import importlib

    from capforge import core, server, versioning

    importlib.reload(core)
    importlib.reload(versioning)
    importlib.reload(server)
    yield root, server
    shutil.rmtree(root, ignore_errors=True)


def test_scaffold_creates_runnable_capability(env):
    _, s = env
    res = s.scaffold_capability("hello", "Measures a string.", packages=[])
    assert res["ok"], res
    assert (Path(res["path"]) / "main.py").is_file()
    assert (Path(res["path"]) / "SKILL.md").is_file()

    listed = s.list_capabilities()
    assert listed["count"] == 1
    assert listed["capabilities"][0]["name"] == "hello"
    assert listed["capabilities"][0]["draft_ready"] is True
    assert listed["capabilities"][0]["active"] is None


def test_scaffold_refuses_existing_without_overwrite(env):
    _, s = env
    assert s.scaffold_capability("hello", "d")["ok"]
    again = s.scaffold_capability("hello", "d")
    assert again["ok"] is False
    assert "already exists" in again["error"]


def test_name_validation(env):
    _, s = env
    for bad in ("../etc", "UPPER", "1leading", "with space", ""):
        assert s.scaffold_capability(bad, "d")["ok"] is False


def test_test_capability_runs_pytest(env):
    _, s = env
    s.scaffold_capability("hello", "d")
    res = s.test_capability("hello")
    assert res["ok"], res
    assert res["tests"]["exit_code"] == 0
    assert "passed" in res["tests"]["stdout"] or res["tests"]["exit_code"] == 0


def test_test_capability_with_args_returns_output(env):
    _, s = env
    s.scaffold_capability("hello", "d")
    res = s.test_capability("hello", args={"text": "abc"})
    assert res["ok"], res
    assert res["run"]["result"] == {"text": "abc", "length": 3}


def test_promote_then_run_active(env):
    _, s = env
    s.scaffold_capability("hello", "d")
    promo = s.promote_capability("hello", tag="v1", note="first")
    assert promo["ok"], promo
    assert promo["tag"] == "v1"
    assert promo["previous"] is None

    run = s.run_capability("hello", args={"text": "hi"})
    assert run["ok"], run
    assert run["version"] == "v1"
    assert run["mode"] == "active"
    assert run["result"] == {"text": "hi", "length": 2}


def test_promote_refuses_when_tests_fail(env):
    _, s = env
    s.scaffold_capability("hello", "d")
    main = Path(s.get_capability("hello")["path"]) / "main.py"
    main.write_text("import sys\nprint('boom', file=sys.stderr)\nsys.exit(3)\n", encoding="utf-8")

    res = s.promote_capability("hello", tag="v1")
    assert res["ok"] is False
    assert "Tests failed" in res["error"]
    assert s.list_tags("hello")["tags"] == []


def test_promote_skips_identical_draft(env):
    _, s = env
    s.scaffold_capability("hello", "d")
    assert s.promote_capability("hello", tag="v1")["ok"]
    again = s.promote_capability("hello", tag="v2")
    assert again["ok"] is True
    assert again["promoted"] is False
    assert "identical" in again["reason"]


def test_run_uses_active_not_draft(env):
    _, s = env
    s.scaffold_capability("hello", "d")
    s.promote_capability("hello", tag="v1")

    path = Path(s.get_capability("hello")["path"]) / "main.py"
    text = path.read_text(encoding="utf-8")
    path.write_text(text.replace('"length": len(text)', '"length": len(text) * 100'), encoding="utf-8")

    run = s.run_capability("hello", args={"text": "ab"})
    assert run["version"] == "v1"
    assert run["result"]["length"] == 2, "an active tag must not see draft edits"


def _retune(s, cap: str, multiplier: int) -> None:
    """Change the capability's behaviour coherently: implementation AND its test.
    Idempotent, so calling it repeatedly does not compound the multiplier."""
    root = Path(s.get_capability(cap)["path"])
    main = root / "main.py"
    text = main.read_text(encoding="utf-8")
    main.write_text(
        re.sub(r'"length": len\(text\)[^,}\n]*', f'"length": len(text) * {multiplier}', text),
        encoding="utf-8",
    )
    test = root / "tests" / "test_main.py"
    ttext = test.read_text(encoding="utf-8")
    test.write_text(
        re.sub(r'run\(\{"text": "hello"\}\)\["length"\] == \d+',
               f'run({{"text": "hello"}})["length"] == {5 * multiplier}', ttext),
        encoding="utf-8",
    )


def test_rollback_restores_previous_tag(env):
    _, s = env
    s.scaffold_capability("hello", "d")
    s.promote_capability("hello", tag="v1")

    _retune(s, "hello", 100)
    assert s.test_capability("hello")["ok"]
    promo = s.promote_capability("hello", tag="v2")
    assert promo["ok"] and promo["previous"] == "v1"
    assert s.run_capability("hello", args={"text": "ab"})["result"]["length"] == 200

    back = s.rollback_capability("hello", "v1")
    assert back["ok"], back
    assert back["was"] == "v2"
    assert s.run_capability("hello", args={"text": "ab"})["result"]["length"] == 2


def test_rollback_unknown_tag(env):
    _, s = env
    s.scaffold_capability("hello", "d")
    res = s.rollback_capability("hello", "nope")
    assert res["ok"] is False
    assert "No tag" in res["error"]


def test_prune_keeps_n_tags_and_never_the_active_one(env):
    _, s = env
    s.scaffold_capability("hello", "d")

    for i in range(1, 6):
        _retune(s, "hello", i + 1)
        assert s.promote_capability("hello", tag=f"v{i}")["ok"]

    tags = s.list_tags("hello")
    assert len(tags["tags"]) == 3, tags
    assert [t["tag"] for t in tags["tags"]] == ["v3", "v4", "v5"]
    assert tags["active"] == "v5"
    assert s.run_capability("hello", args={"text": "a"})["result"]["length"] == 6

    # the active version survives even when it is the oldest one kept
    assert s.rollback_capability("hello", "v3")["ok"]
    _retune(s, "hello", 100)
    assert s.promote_capability("hello", tag="v6")["ok"]
    assert s.list_tags("hello")["active"] == "v6"
    assert s.run_capability("hello", args={"text": "a"})["result"]["length"] == 100


def test_duplicate_tag_refused(env):
    _, s = env
    s.scaffold_capability("hello", "d")
    s.promote_capability("hello", tag="v1")
    path = Path(s.get_capability("hello")["path"]) / "main.py"
    path.write_text(path.read_text(encoding="utf-8") + "\n# edit\n", encoding="utf-8")
    res = s.promote_capability("hello", tag="v1")
    assert res["ok"] is False
    assert "already exists" in res["error"]


def test_promote_with_packages_records_them(env):
    _, s = env
    s.scaffold_capability("hello", "d")
    res = s.promote_capability("hello", tag="v1", packages=["six"])
    assert res["ok"], res
    assert res["packages"] == ["six"]
    assert "packages: [six]" in Path(res["version_path"]).joinpath("SKILL.md").read_text(encoding="utf-8")


def test_run_without_promote_falls_back_to_draft_with_warning(env):
    _, s = env
    s.scaffold_capability("hello", "d")
    res = s.run_capability("hello", args={"text": "x"})
    assert res["ok"], res
    assert res["mode"] == "draft"
    assert "warning" in res


def test_bad_json_output_is_a_failure(env):
    _, s = env
    s.scaffold_capability("hello", "d")
    path = Path(s.get_capability("hello")["path"]) / "main.py"
    path.write_text("print('not json')\n", encoding="utf-8")
    res = s.run_capability("hello", args={})
    assert res["ok"] is False
    assert "not valid JSON" in res["stderr"]


def test_timeout_is_reported(env, monkeypatch):
    _, s = env
    s.scaffold_capability("hello", "d")
    path = Path(s.get_capability("hello")["path"]) / "main.py"
    path.write_text("import time\ntime.sleep(30)\n", encoding="utf-8")
    monkeypatch.setenv("CAPFORGE_RUN_TIMEOUT", "1")
    import importlib

    from capforge import core

    importlib.reload(core)
    res = s.run_capability("hello", args={})
    assert res["ok"] is False
    assert res.get("timed_out") is True


def test_timeout_kills_the_whole_process_group(env, monkeypatch):
    _, s = env
    s.scaffold_capability("hello", "d")
    path = Path(s.get_capability("hello")["path"]) / "main.py"
    path.write_text(
        "import os, subprocess, sys, time\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])\n"
        "with open(os.path.join(os.environ['CAPFORGE_DATA_DIR'], 'child.pid'), 'w') as fh:\n"
        "    fh.write(str(child.pid))\n"
        "time.sleep(30)\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("CAPFORGE_RUN_TIMEOUT", "1")
    import importlib

    from capforge import core

    importlib.reload(core)
    res = s.run_capability("hello", args={})
    assert res["ok"] is False and res.get("timed_out") is True

    pid = int((Path(res["data_dir"]) / "child.pid").read_text(encoding="utf-8"))
    for _ in range(40):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.05)
    else:
        raise AssertionError(f"child {pid} survived the timeout")


def test_data_dir_is_per_version(env):
    _, s = env
    s.scaffold_capability("hello", "d")
    path = Path(s.get_capability("hello")["path"]) / "main.py"
    path.write_text(
        "import json, os, sys\n"
        "args = json.loads(sys.stdin.read() or '{}')\n"
        "p = os.path.join(os.environ['CAPFORGE_DATA_DIR'], 'n.txt')\n"
        "n = int(open(p).read()) + 1 if os.path.exists(p) else 1\n"
        "open(p, 'w').write(str(n))\n"
        "json.dump({'n': n, 'version': os.environ['CAPFORGE_VERSION']}, sys.stdout)\n",
        encoding="utf-8",
    )
    s.promote_capability("hello", tag="v1", skip_tests=True)
    assert s.run_capability("hello")["result"] == {"n": 1, "version": "v1"}
    assert s.run_capability("hello")["result"] == {"n": 2, "version": "v1"}

    s.promote_capability("hello", tag="v2", skip_tests=True, force=True)
    assert s.run_capability("hello")["result"] == {"n": 1, "version": "v2"}, "data must be per version"


def test_environment_is_filtered(env, monkeypatch):
    _, s = env
    monkeypatch.setenv("OPENAI_API_KEY", "sk-secret")
    monkeypatch.setenv("WEBUI_SECRET_KEY", "nope")
    monkeypatch.setenv("PIP_INDEX_URL", "https://user:s3cr3t@example/simple")
    s.scaffold_capability("hello", "d")
    path = Path(s.get_capability("hello")["path"]) / "main.py"
    path.write_text(
        "import json, os, sys\n"
        "json.dump({'leaked': [k for k in os.environ if 'KEY' in k or 'SECRET' in k or 'TOKEN' in k],"
        " 'pip_index': 'PIP_INDEX_URL' in os.environ}, sys.stdout)\n",
        encoding="utf-8",
    )
    res = s.run_capability("hello", args={})
    assert res["ok"], res
    assert res["result"]["leaked"] == []
    assert res["result"]["pip_index"] is False


def test_install_packages_rejects_empty(env):
    _, s = env
    s.scaffold_capability("hello", "d")
    assert s.install_packages("hello", [])["ok"] is False


def test_install_packages_builds_venv_and_records(env):
    _, s = env
    s.scaffold_capability("hello", "d")
    res = s.install_packages("hello", ["six"])
    assert res["ok"], res
    assert res["packages"] == ["six"]
    assert Path(res["venv"]).is_dir()
    assert "packages: [six]" in (Path(s.get_capability("hello")["path"]) / "SKILL.md").read_text(encoding="utf-8")


def test_get_contract_returns_layout(env):
    _, s = env
    c = s.get_contract()
    assert c["ok"]
    assert "stdin" in c["contract"]
    assert c["caps_dir"].endswith("caps")
    assert c["keep_tags"] == 3


def test_diff_shows_draft_changes(env):
    _, s = env
    s.scaffold_capability("hello", "d")
    s.promote_capability("hello", tag="v1")
    path = Path(s.get_capability("hello")["path"]) / "main.py"
    path.write_text(path.read_text(encoding="utf-8") + "\n# a change\n", encoding="utf-8")
    diff = s.list_tags("hello", diff_against_active=True)["diff"]
    assert "a change" in diff


def test_broken_capability_does_not_break_listing(env):
    _, s = env
    s.scaffold_capability("good", "fine")
    bad = Path(s.get_capability("good")["path"]).parent / "broken"
    bad.mkdir()
    (bad / "main.py").write_text("print(1)", encoding="utf-8")
    listed = s.list_capabilities()
    names = {c["name"]: c for c in listed["capabilities"]}
    assert names["good"]["draft_ready"] is True
    assert names["broken"]["draft_ready"] is False
    assert "SKILL.md is missing" in names["broken"]["draft_error"]


def test_active_entrypoint_does_not_follow_draft_edits(env):
    _, s = env
    s.scaffold_capability("hello", "d")
    assert s.promote_capability("hello", tag="v1")["ok"]

    root = Path(s.get_capability("hello")["path"])
    (root / "other.py").write_text(
        "import json, sys\njson.dump({'from': 'draft'}, sys.stdout)\n", encoding="utf-8"
    )
    skill = root / "SKILL.md"
    skill.write_text(
        skill.read_text(encoding="utf-8").replace("entrypoint: main.py", "entrypoint: other.py"),
        encoding="utf-8",
    )

    run = s.run_capability("hello", args={"text": "hi"})
    assert run["ok"], run
    assert run["entrypoint"] == "main.py"
    assert run["result"] == {"text": "hi", "length": 2}


def test_active_runs_when_draft_metadata_is_broken(env):
    _, s = env
    s.scaffold_capability("hello", "d")
    assert s.promote_capability("hello", tag="v1")["ok"]
    (Path(s.get_capability("hello")["path"]) / "SKILL.md").unlink()

    run = s.run_capability("hello", args={"text": "x"})
    assert run["ok"], run
    assert run["version"] == "v1"


def test_active_empty_packages_do_not_inherit_draft_packages(env):
    _, s = env
    s.scaffold_capability("pkgiso", "d")
    assert s.promote_capability("pkgiso", tag="v1", packages=[], skip_tests=True)["ok"]

    skill = Path(s.get_capability("pkgiso")["path"]) / "SKILL.md"
    skill.write_text(
        skill.read_text(encoding="utf-8").replace("packages: []", "packages: [six]"),
        encoding="utf-8",
    )
    run = s.run_capability("pkgiso", args={})
    assert run["ok"], run
    assert run["packages"] == []


def test_diagnostics_before_json_are_allowed(env):
    _, s = env
    s.scaffold_capability("hello", "d")
    path = Path(s.get_capability("hello")["path"]) / "main.py"
    path.write_text(
        "import json, sys\n"
        "print('a diagnostic line')\n"
        "json.dump({'value': 1}, sys.stdout)\n",
        encoding="utf-8",
    )
    run = s.run_capability("hello", args={})
    assert run["ok"], run
    assert run["result"] == {"value": 1}


def test_smoke_test_requires_valid_json(env):
    _, s = env
    s.scaffold_capability("smoke", "d")
    root = Path(s.get_capability("smoke")["path"])
    shutil.rmtree(root / "tests")
    (root / "main.py").write_text("print('not json at all')\n", encoding="utf-8")

    tested = s.test_capability("smoke")
    assert tested["ok"] is False
    promoted = s.promote_capability("smoke", tag="v1")
    assert promoted["ok"] is False
    assert "Tests failed" in promoted["error"]


@pytest.mark.parametrize(
    "bad",
    ["--target=/tmp/x", "six seven", "six,seven", "https://example/six.whl", "-r requirements.txt"],
)
def test_invalid_package_requirements_rejected(env, bad):
    _, s = env
    s.scaffold_capability("hello", "d")
    assert s.install_packages("hello", [bad])["ok"] is False
    assert s.scaffold_capability("other", "d", packages=[bad])["ok"] is False


def test_scaffold_rejects_multiline_description(env):
    _, s = env
    for description in ("two\nlines", "has --- inside", ""):
        assert s.scaffold_capability("hello", description)["ok"] is False


def test_scaffold_overwrite_archives_the_old_draft(env):
    _, s = env
    s.scaffold_capability("hello", "d")
    main = Path(s.get_capability("hello")["path"]) / "main.py"
    main.write_text("# precious\n", encoding="utf-8")

    again = s.scaffold_capability("hello", "d", overwrite=True)
    assert again["ok"], again
    backup = Path(again["backup"])
    assert "precious" in (backup / "main.py").read_text(encoding="utf-8")


def test_corrupt_state_is_quarantined(env):
    root, s = env
    s.scaffold_capability("hello", "d")
    state_dir = root / "state" / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    state_file = state_dir / "hello.json"
    state_file.write_text("{not json", encoding="utf-8")

    from capforge import core

    assert core.read_state("hello")["active"] is None
    assert not state_file.exists()
    assert list(state_dir.glob("hello.corrupt-*.json"))


def test_prune_removes_per_version_data_and_keeps_active(env):
    root, s = env
    s.scaffold_capability("hello", "d")
    from capforge import core

    core.data_dir("hello", "v1").mkdir(parents=True, exist_ok=True)
    (core.data_dir("hello", "v1") / "state.txt").write_text("old", encoding="utf-8")

    for tag in ("v1", "v2", "v3", "v4"):
        assert s.promote_capability("hello", tag=tag, skip_tests=True, force=True)["ok"]

    assert not (root / "state" / "data" / "hello" / "v1").exists()
    assert root.joinpath("state", "versions", "hello", "v4").is_dir()
    tags = s.list_tags("hello")
    assert tags["active"] == "v4"
    assert "v1" in tags["pruned"]
