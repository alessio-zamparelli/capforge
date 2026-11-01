"""Fast unit tests: no virtualenvs, no subprocesses."""

from __future__ import annotations

import asyncio
import json

import pytest

from capforge import core


def test_parse_skill_md_inline_lists():
    text = (
        "---\n"
        "name: demo\n"
        "description: A demo.\n"
        "entrypoint: main.py\n"
        "packages: [pypdf>=5, httpx]\n"
        "---\n\n"
        "# demo\n"
    )
    meta, body = core.parse_skill_md(text)
    assert meta["name"] == "demo"
    assert meta["packages"] == ["pypdf>=5", "httpx"]
    assert body.startswith("# demo")


def test_parse_skill_md_block_lists_and_quotes():
    text = (
        "---\n"
        "name: demo\n"
        "description: 'A demo.'\n"
        "packages:\n"
        "  - 'six'\n"
        "  - httpx\n"
        "---\n"
    )
    meta, _ = core.parse_skill_md(text)
    assert meta["description"] == "A demo."
    assert meta["packages"] == ["six", "httpx"]


def test_parse_skill_md_without_frontmatter():
    meta, body = core.parse_skill_md("hello")
    assert meta == {}
    assert body == "hello"


@pytest.mark.parametrize(
    "spec",
    ["six", "pypdf>=5", "httpx[http2]", "numpy==1.26.*", "pkg[extra]>=1"],
)
def test_valid_package_requirements(spec):
    assert core.validate_packages([spec]) == [spec]


@pytest.mark.parametrize(
    "spec",
    ["--target=/tmp/x", "six seven", "six,seven", "https://example/six.whl", "-r x.txt", ""],
)
def test_invalid_package_requirements(spec):
    if spec == "":
        assert core.validate_packages([spec]) == []
    else:
        with pytest.raises(core.CapError):
            core.validate_packages([spec])


def test_package_limit():
    too_many = [f"pkg{i}" for i in range(core.MAX_PACKAGES + 1)]
    with pytest.raises(core.CapError):
        core.validate_packages(too_many)


def test_tree_hash_ignores_caches(tmp_path):
    (tmp_path / "main.py").write_text("print(1)\n", encoding="utf-8")
    before = core.tree_hash(tmp_path)
    (tmp_path / "__pycache__").mkdir()
    (tmp_path / "__pycache__" / "main.pyc").write_bytes(b"cache")
    (tmp_path / ".pytest_cache").mkdir()
    (tmp_path / ".pytest_cache" / "nodeids").write_text("x", encoding="utf-8")
    assert core.tree_hash(tmp_path) == before


def test_requirements_hash_is_stable_and_order_independent():
    assert core.requirements_hash(["b", "a", "a"]) == core.requirements_hash(["a", "b"])
    assert len(core.requirements_hash([])) == 16


def test_entrypoint_cannot_escape_the_capability(tmp_path):
    cap = tmp_path / "demo"
    cap.mkdir()
    (cap / "main.py").write_text("print(1)\n", encoding="utf-8")
    (cap / "SKILL.md").write_text(
        "---\ndescription: d\nentrypoint: ../escape.py\n---\n", encoding="utf-8"
    )
    loaded = core.load_capability_at("demo", cap)
    assert loaded.error is not None
    assert "escapes" in loaded.error


def test_absolute_entrypoint_is_rejected(tmp_path):
    cap = tmp_path / "demo"
    cap.mkdir()
    (cap / "SKILL.md").write_text(
        "---\ndescription: d\nentrypoint: /etc/passwd\n---\n", encoding="utf-8"
    )
    loaded = core.load_capability_at("demo", cap)
    assert loaded.error is not None
    assert "escapes" in loaded.error


def test_corrupt_state_types_are_coerced(tmp_path, monkeypatch):
    monkeypatch.setenv("CAPFORGE_STATE_DIR", str(tmp_path / "state"))
    state_dir = tmp_path / "state" / "state"
    state_dir.mkdir(parents=True)
    (state_dir / "demo.json").write_text(
        json.dumps({"active": "v1", "tags": ["v1", {"tag": "bad tag"}]}),
        encoding="utf-8",
    )
    state = core.read_state("demo")
    assert state["active"] is None
    assert state["tags"] == []


def test_corrupt_state_is_renamed(tmp_path, monkeypatch):
    monkeypatch.setenv("CAPFORGE_STATE_DIR", str(tmp_path / "state"))
    state_dir = tmp_path / "state" / "state"
    state_dir.mkdir(parents=True)
    bad = state_dir / "demo.json"
    bad.write_text("{", encoding="utf-8")
    assert core.read_state("demo")["active"] is None
    assert not bad.exists()
    assert list(state_dir.glob("demo.corrupt-*.json"))


def test_data_dir_create_flag(tmp_path, monkeypatch):
    monkeypatch.setenv("CAPFORGE_STATE_DIR", str(tmp_path / "state"))
    path = core.data_dir("demo", "v1", create=False)
    assert not path.exists()
    created = core.data_dir("demo", "v1")
    assert created.is_dir()


def test_workspace_guide_is_installed_and_reserved(tmp_path, monkeypatch):
    monkeypatch.setenv("CAPFORGE_CAPS_DIR", str(tmp_path / "caps"))
    path = core.ensure_workspace_skill()
    assert path is not None
    text = path.read_text(encoding="utf-8")
    assert text.startswith("---")
    assert core.WORKSPACE_SKILL_MARKER in text
    assert "get_contract" in text
    assert core.list_capabilities() == []


def test_workspace_guide_preserves_user_file(tmp_path, monkeypatch):
    monkeypatch.setenv("CAPFORGE_CAPS_DIR", str(tmp_path / "caps"))
    path = tmp_path / "caps" / "_capforge" / "SKILL.md"
    path.parent.mkdir(parents=True)
    path.write_text("---\ndescription: mine\n---\n", encoding="utf-8")
    assert core.ensure_workspace_skill() == path
    assert "description: mine" in path.read_text(encoding="utf-8")


def test_workspace_guide_can_be_disabled(tmp_path, monkeypatch):
    monkeypatch.setenv("CAPFORGE_CAPS_DIR", str(tmp_path / "caps"))
    monkeypatch.setenv("CAPFORGE_GUIDE", "0")
    assert core.ensure_workspace_skill() is None
    assert not (tmp_path / "caps" / "_capforge").exists()


def test_tool_surface_is_fixed_at_ten():
    from capforge.server import mcp

    names = [tool.name for tool in asyncio.run(mcp.list_tools())]
    assert names == [
        "get_contract",
        "list_capabilities",
        "get_capability",
        "scaffold_capability",
        "install_packages",
        "test_capability",
        "run_capability",
        "promote_capability",
        "rollback_capability",
        "list_tags",
    ]
