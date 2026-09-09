"""Tests for bridge/plugins/loader.py: Claude-Code-style plugin
install/uninstall/list, SkillStore exposure of plugin skills and
command-skills, and .mcp.json merge into the MCP catalog.

No real ~/.freyja paths are ever written — every test runs against
tmp_path via the overridable plugins_root/mcp_path roots, and HOME is
pointed at tmp_path so SkillStore's default scan roots are hermetic.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bridge.knowledge.skill_store import SkillStore
from bridge.mcp.config import load_catalog, save_catalog
from bridge.plugins.loader import PluginError, install, list_installed, uninstall

SLACK_PLUGIN = Path("/tmp/slack-skills-plugin")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _hermetic_home(tmp_path, monkeypatch):
    """Point HOME at tmp_path so SkillStore never reads or writes the
    real ~/.freyja / ~/.claude trees during these tests."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir(exist_ok=True)


@pytest.fixture
def fake_plugin(tmp_path) -> Path:
    """A minimal Claude Code plugin: manifest (with one unsupported
    manifest key), 2 skills, 1 command, an unsupported agents/ dir, and
    a .mcp.json with one stdio server using ${CLAUDE_PLUGIN_ROOT}."""
    src = tmp_path / "src" / "demo-plugin"
    (src / ".claude-plugin").mkdir(parents=True)
    (src / ".claude-plugin" / "plugin.json").write_text(
        json.dumps(
            {
                "name": "demo",
                "version": "0.1.0",
                "description": "Demo plugin for tests",
                "hooks": {"PreToolUse": []},
            }
        ),
        encoding="utf-8",
    )
    for skill, desc in (
        ("alpha", "Alpha skill for wrangling widgets"),
        ("beta", "Beta skill for polishing gizmos"),
    ):
        d = src / "skills" / skill
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text(
            f"---\nname: {skill}\ndescription: {desc}\n---\n\nDo the {skill} thing carefully.\n",
            encoding="utf-8",
        )
    commands = src / "commands"
    commands.mkdir()
    (commands / "greet.md").write_text(
        "---\ndescription: Greet someone by name\n---\n\nGreet $ARGUMENTS warmly.\n",
        encoding="utf-8",
    )
    (src / "agents").mkdir()
    (src / ".mcp.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "demo-server": {
                        "type": "stdio",
                        "command": "${CLAUDE_PLUGIN_ROOT}/server/run",
                        "args": ["--root", "${CLAUDE_PLUGIN_ROOT}"],
                        "env": {"DEMO_HOME": "${CLAUDE_PLUGIN_ROOT}/data"},
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    return src


@pytest.fixture
def roots(tmp_path):
    return tmp_path / "plugins", tmp_path / "mcp.json"


# ---------------------------------------------------------------------------
# Install / list / uninstall round trip
# ---------------------------------------------------------------------------

def test_install_copies_plugin_and_records_installed_json(fake_plugin, roots):
    plugins_root, mcp_path = roots
    summary = install(fake_plugin, plugins_root=plugins_root, mcp_path=mcp_path)

    assert summary["name"] == "demo"
    assert summary["version"] == "0.1.0"
    assert summary["path"] == str(plugins_root / "demo")
    assert (plugins_root / "demo" / "skills" / "alpha" / "SKILL.md").is_file()
    assert summary["skill_names"] == ["demo:alpha", "demo:beta"]
    assert summary["command_names"] == ["demo:greet"]
    # Both the manifest key and the conventional directory are reported.
    assert set(summary["unsupported"]) == {"hooks", "agents"}

    installed = json.loads((plugins_root / "installed.json").read_text())
    record = installed["demo"]
    assert record["name"] == "demo"
    assert record["version"] == "0.1.0"
    assert record["source"] == str(fake_plugin)
    assert record["installed_at"] > 0
    assert set(record["unsupported"]) == {"hooks", "agents"}
    assert record["skills"] == 2
    assert record["commands"] == 1
    assert record["servers"] == 1


def test_list_installed_returns_counts(fake_plugin, roots):
    plugins_root, mcp_path = roots
    assert list_installed(plugins_root=plugins_root) == []
    install(fake_plugin, plugins_root=plugins_root, mcp_path=mcp_path)
    records = list_installed(plugins_root=plugins_root)
    assert len(records) == 1
    assert records[0]["name"] == "demo"
    assert (records[0]["skills"], records[0]["commands"], records[0]["servers"]) == (2, 1, 1)


def test_uninstall_removes_dir_record_and_own_servers(fake_plugin, roots):
    plugins_root, mcp_path = roots
    # A pre-existing foreign server must survive the uninstall.
    foreign = {
        "version": 1,
        "servers": {
            "other": {"transport": "stdio", "command": "/usr/bin/true"},
        },
    }
    mcp_path.write_text(json.dumps(foreign), encoding="utf-8")

    install(fake_plugin, plugins_root=plugins_root, mcp_path=mcp_path)
    result = uninstall("demo", plugins_root=plugins_root, mcp_path=mcp_path)

    assert result["removed_dir"] is True
    assert result["removed_record"] is True
    assert result["removed_mcp_servers"] == ["demo-server"]
    assert not (plugins_root / "demo").exists()
    assert "demo" not in json.loads((plugins_root / "installed.json").read_text())

    catalog = load_catalog(mcp_path)
    assert "demo-server" not in catalog.specs
    assert "other" in catalog.specs  # only its own entries removed


def test_uninstall_unknown_plugin_raises(roots):
    plugins_root, mcp_path = roots
    with pytest.raises(PluginError):
        uninstall("nope", plugins_root=plugins_root, mcp_path=mcp_path)


def test_install_rejects_non_plugin_dir(tmp_path, roots):
    plugins_root, mcp_path = roots
    src = tmp_path / "not-a-plugin"
    src.mkdir()
    with pytest.raises(PluginError, match="claude-plugin"):
        install(src, plugins_root=plugins_root, mcp_path=mcp_path)


def test_install_missing_source_raises(roots):
    plugins_root, mcp_path = roots
    with pytest.raises(PluginError, match="not found"):
        install("/nowhere/at/all", plugins_root=plugins_root, mcp_path=mcp_path)


# ---------------------------------------------------------------------------
# MCP catalog merge
# ---------------------------------------------------------------------------

def test_mcp_merge_disabled_standard_and_plugin_root_expanded(fake_plugin, roots):
    plugins_root, mcp_path = roots
    summary = install(fake_plugin, plugins_root=plugins_root, mcp_path=mcp_path)
    assert summary["mcp"]["added"] == ["demo-server"]

    plugin_dir = str(plugins_root / "demo")
    catalog = load_catalog(mcp_path)
    spec = catalog.specs["demo-server"]
    assert spec.transport == "stdio"
    assert spec.enabled is False
    assert spec.trust == "standard"
    assert spec.source == {"plugin": f"demo@{fake_plugin}"}
    assert spec.command == f"{plugin_dir}/server/run"
    assert spec.args == ["--root", plugin_dir]
    assert spec.env == {"DEMO_HOME": f"{plugin_dir}/data"}
    # Stored expanded: no unexpanded var survives anywhere in the file.
    assert "${CLAUDE_PLUGIN_ROOT}" not in mcp_path.read_text(encoding="utf-8")


def test_mcp_merge_is_idempotent_and_preserves_operator_edits(fake_plugin, roots):
    plugins_root, mcp_path = roots
    install(fake_plugin, plugins_root=plugins_root, mcp_path=mcp_path)

    # Operator enables the server between installs.
    catalog = load_catalog(mcp_path)
    catalog.specs["demo-server"].enabled = True
    save_catalog(catalog, mcp_path)

    summary = install(fake_plugin, plugins_root=plugins_root, mcp_path=mcp_path)
    assert summary["mcp"]["added"] == []
    assert summary["mcp"]["already_present"] == ["demo-server"]

    catalog = load_catalog(mcp_path)
    assert list(catalog.specs) == ["demo-server"]  # no duplicate
    assert catalog.specs["demo-server"].enabled is True  # edit survived


def test_mcp_merge_conflict_with_foreign_server_is_reported_not_overwritten(
    fake_plugin, roots
):
    plugins_root, mcp_path = roots
    mcp_path.write_text(
        json.dumps(
            {
                "version": 1,
                "servers": {
                    "demo-server": {"transport": "stdio", "command": "/usr/bin/true"},
                },
            }
        ),
        encoding="utf-8",
    )
    summary = install(fake_plugin, plugins_root=plugins_root, mcp_path=mcp_path)
    assert summary["mcp"]["added"] == []
    assert [c["name"] for c in summary["mcp"]["conflicts"]] == ["demo-server"]

    spec = load_catalog(mcp_path).specs["demo-server"]
    assert spec.command == "/usr/bin/true"  # untouched
    assert spec.source is None


def test_plugin_without_mcp_json_leaves_catalog_alone(fake_plugin, roots):
    plugins_root, mcp_path = roots
    (fake_plugin / ".mcp.json").unlink()
    summary = install(fake_plugin, plugins_root=plugins_root, mcp_path=mcp_path)
    assert summary["mcp"] == {
        "added": [],
        "already_present": [],
        "conflicts": [],
        "invalid": [],
    }
    assert not mcp_path.exists()  # never created gratuitously


# ---------------------------------------------------------------------------
# SkillStore exposure
# ---------------------------------------------------------------------------

def _store(tmp_path, plugins_root) -> SkillStore:
    return SkillStore(tmp_path / "workspace", plugins_root=plugins_root)


def test_skillstore_discovers_prefixed_plugin_skills(fake_plugin, roots, tmp_path):
    plugins_root, mcp_path = roots
    install(fake_plugin, plugins_root=plugins_root, mcp_path=mcp_path)

    store = _store(tmp_path, plugins_root)
    names = {s.name for s in store.list_skills()}
    assert {"demo:alpha", "demo:beta", "demo:greet"} <= names

    alpha = store.get("demo:alpha")
    assert alpha is not None
    assert alpha.scope == "plugin"
    assert alpha.id == "plugin:build:demo:alpha"

    hits = store.search("wrangling widgets")
    assert hits and hits[0][0].name == "demo:alpha"

    skill, content = store.load("demo:alpha")
    assert skill is not None
    assert "Do the alpha thing carefully." in content


def test_skillstore_loads_command_skill_instructions(fake_plugin, roots, tmp_path):
    plugins_root, mcp_path = roots
    install(fake_plugin, plugins_root=plugins_root, mcp_path=mcp_path)

    store = _store(tmp_path, plugins_root)
    command = store.get("demo:greet")
    assert command is not None
    assert command.description == "Greet someone by name"
    assert "command" in command.tags
    assert "/demo:greet" in command.triggers

    skill, content = store.load("demo:greet")
    assert skill is not None
    assert "Greet $ARGUMENTS warmly." in content


def test_skillstore_refresh_picks_up_install_and_uninstall(fake_plugin, roots, tmp_path):
    plugins_root, mcp_path = roots
    store = _store(tmp_path, plugins_root)  # created before any install
    assert not any(s.name.startswith("demo:") for s in store.list_skills())

    install(fake_plugin, plugins_root=plugins_root, mcp_path=mcp_path)
    names = {s.name for s in store.list_skills()}  # fingerprint change → rescan
    assert {"demo:alpha", "demo:beta", "demo:greet"} <= names

    uninstall("demo", plugins_root=plugins_root, mcp_path=mcp_path)
    assert not any(s.name.startswith("demo:") for s in store.list_skills())


# ---------------------------------------------------------------------------
# Link mode
# ---------------------------------------------------------------------------

def test_link_mode_symlinks_files_and_keeps_source_pristine(
    fake_plugin, roots, tmp_path
):
    plugins_root, mcp_path = roots
    summary = install(fake_plugin, link=True, plugins_root=plugins_root, mcp_path=mcp_path)
    assert summary["link"] is True

    installed_skill = plugins_root / "demo" / "skills" / "alpha" / "SKILL.md"
    assert installed_skill.is_symlink()
    assert installed_skill.resolve() == (fake_plugin / "skills" / "alpha" / "SKILL.md").resolve()

    # Generated command shim is a real file in the install dir, and the
    # source checkout was not mutated.
    shim = plugins_root / "demo" / "skills" / "commands" / "greet" / "SKILL.md"
    assert shim.is_file() and not shim.is_symlink()
    assert not (fake_plugin / "skills" / "commands").exists()

    # Content edits in the source show up live through the symlink.
    store = _store(tmp_path, plugins_root)
    _, content = store.load("demo:alpha")
    assert "Do the alpha thing carefully." in content
    (fake_plugin / "skills" / "alpha" / "SKILL.md").write_text(
        "---\nname: alpha\ndescription: Alpha skill\n---\n\nUpdated alpha instructions.\n",
        encoding="utf-8",
    )
    _, content = store.load("demo:alpha")
    assert "Updated alpha instructions." in content


def test_link_mode_rejects_git_url(roots):
    plugins_root, mcp_path = roots
    with pytest.raises(PluginError, match="link mode"):
        install(
            "https://example.com/some/plugin.git",
            link=True,
            plugins_root=plugins_root,
            mcp_path=mcp_path,
        )


# ---------------------------------------------------------------------------
# Integration: real slack-skills-plugin clone (skip if missing)
# ---------------------------------------------------------------------------

@pytest.mark.skipif(
    not (SLACK_PLUGIN / ".claude-plugin" / "plugin.json").is_file(),
    reason="/tmp/slack-skills-plugin clone not present",
)
def test_slack_skills_plugin_integration(tmp_path, roots):
    plugins_root, mcp_path = roots
    summary = install(SLACK_PLUGIN, plugins_root=plugins_root, mcp_path=mcp_path)

    assert summary["name"] == "slack"
    assert len(summary["skill_names"]) == 8
    assert len(summary["command_names"]) == 5
    assert summary["mcp"]["added"] == ["slack"]

    expected_skills = {
        f"slack:{n}"
        for n in (
            "block-kit",
            "create-slack-app",
            "slack-api",
            "slack-cli",
            "slack-docs",
            "slack-messaging",
            "slack-search",
            "test-slack-app",
        )
    }
    expected_commands = {
        f"slack:{n}"
        for n in (
            "channel-digest",
            "draft-announcement",
            "find-discussions",
            "standup",
            "summarize-channel",
        )
    }
    store = _store(tmp_path, plugins_root)
    names = {s.name for s in store.list_skills()}
    assert expected_skills <= names
    assert expected_commands <= names

    skill, content = store.load("slack:block-kit")
    assert skill is not None and len(content) > 100
    skill, content = store.load("slack:channel-digest")
    assert skill is not None and "$ARGUMENTS" in content

    # Hosted slack server merged disabled, trust standard; the plugin's
    # camelCase oauth block is normalized and implies auth=oauth.
    spec = load_catalog(mcp_path).specs["slack"]
    assert spec.transport == "http"
    assert spec.url == "https://mcp.slack.com/mcp"
    assert spec.enabled is False
    assert spec.trust == "standard"
    assert spec.source == {"plugin": f"slack@{SLACK_PLUGIN}"}
    assert spec.auth == "oauth"
    assert spec.oauth == {
        "client_id": "1601185624273.8899143856786",
        "redirect_port": 3118,
        "redirect_host": "localhost",
    }
