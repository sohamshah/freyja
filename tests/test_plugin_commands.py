"""plugin_command handler tests: install/list/remove roundtrip against
tmp roots, SkillStore refresh + McpManager reload hooks, error shapes,
the freyja_bridge plugin_command IPC branch, and the /plugin helpers.

The handler is called directly (as freyja_bridge._handle_command and the
gateway /plugin command do); plugin roots and MCP catalogs live in
tmp_path only — no real ~/.freyja paths are ever read or written.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bridge.mcp.manager import McpManager
from bridge.plugins.commands import (
    format_plugin_table,
    handle_plugin_command,
    parse_plugin_args,
)


# ---------------------------------------------------------------------------
# Fixtures / fakes
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _hermetic_home(tmp_path, monkeypatch):
    """Point HOME at tmp_path as defence in depth. Every test passes
    explicit plugins_root/mcp_path anyway (the loader's DEFAULT_* are
    resolved at import time, so HOME alone would not be enough)."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir(exist_ok=True)


def _make_plugin(tmp_path: Path, name: str, *, with_mcp: bool) -> Path:
    """A minimal Claude Code plugin: manifest (with one unsupported
    manifest key), one skill, one command, an unsupported agents/ dir,
    and (optionally) a .mcp.json with one stdio server."""
    src = tmp_path / "src" / name
    (src / ".claude-plugin").mkdir(parents=True)
    (src / ".claude-plugin" / "plugin.json").write_text(
        json.dumps({
            "name": name,
            "version": "0.1.0",
            "description": "Plugin for plugin_command tests",
            "hooks": {"PreToolUse": []},
        }),
        encoding="utf-8",
    )
    skill = src / "skills" / "hello"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\nname: hello\ndescription: Say hello\n---\n\nSay hello.\n",
        encoding="utf-8",
    )
    commands = src / "commands"
    commands.mkdir()
    (commands / "deploy.md").write_text(
        "---\ndescription: Deploy the thing\n---\n\nDeploy $ARGUMENTS.\n",
        encoding="utf-8",
    )
    (src / "agents").mkdir()
    if with_mcp:
        (src / ".mcp.json").write_text(
            json.dumps({
                "mcpServers": {
                    f"{name}-server": {
                        "type": "stdio",
                        "command": "${CLAUDE_PLUGIN_ROOT}/server/run",
                        "args": ["--root", "${CLAUDE_PLUGIN_ROOT}"],
                    }
                }
            }),
            encoding="utf-8",
        )
    return src


def _roots(tmp_path: Path) -> tuple[Path, Path]:
    """(plugins_root, mcp_path) under tmp_path; mcp.json starts empty."""
    plugins_root = tmp_path / "plugins"
    mcp_path = tmp_path / "mcp.json"
    mcp_path.write_text(
        json.dumps({"version": 1, "servers": {}}), encoding="utf-8"
    )
    return plugins_root, mcp_path


class FakeSkillStore:
    def __init__(self, fail: bool = False):
        self.refresh_calls = 0
        self.fail = fail

    def refresh(self) -> None:
        self.refresh_calls += 1
        if self.fail:
            raise RuntimeError("boom")


class FakeMcpManager:
    def __init__(self, catalog_path: Path):
        self._catalog_path = catalog_path
        self.reload_calls = 0

    async def reload(self):
        self.reload_calls += 1
        return {"added": [], "removed": [], "changed": []}


# ---------------------------------------------------------------------------
# install -> list -> remove roundtrip (real loader, tmp roots)
# ---------------------------------------------------------------------------

async def test_list_empty(tmp_path):
    plugins_root, _mcp = _roots(tmp_path)
    result = await handle_plugin_command(
        {"action": "list"}, plugins_root=plugins_root
    )
    assert result["ok"] is True
    assert result["action"] == "list"
    assert result["plugins"] == []


async def test_install_list_remove_roundtrip(tmp_path):
    plugins_root, mcp_path = _roots(tmp_path)
    src = _make_plugin(tmp_path, "demo", with_mcp=True)

    result = await handle_plugin_command(
        {"action": "install", "source": str(src)},
        plugins_root=plugins_root,
        mcp_path=mcp_path,
    )
    assert result["ok"] is True, result
    assert result["action"] == "install"
    assert result["plugin"] == "demo"
    summary = result["summary"]
    assert summary["skill_names"] == ["demo:hello"]
    assert summary["command_names"] == ["demo:deploy"]
    assert summary["mcp"]["added"] == ["demo-server"]
    assert set(summary["unsupported"]) == {"hooks", "agents"}
    # Summary message carries counts + unsupported sections + merge note.
    assert "1 skill(s)" in result["message"]
    assert "1 command(s)" in result["message"]
    assert "1 MCP server(s)" in result["message"]
    assert "hooks" in result["message"] and "agents" in result["message"]
    assert "disabled" in result["message"]
    # Plugin landed under the tmp root; server merged disabled.
    assert (plugins_root / "demo" / "skills" / "hello" / "SKILL.md").is_file()
    doc = json.loads(mcp_path.read_text(encoding="utf-8"))
    assert doc["servers"]["demo-server"]["enabled"] is False
    assert doc["servers"]["demo-server"]["source"]["plugin"].startswith("demo@")

    # list shows the installed record with its counts.
    result = await handle_plugin_command(
        {"action": "list"}, plugins_root=plugins_root
    )
    assert [p["name"] for p in result["plugins"]] == ["demo"]
    rec = result["plugins"][0]
    assert rec["version"] == "0.1.0"
    assert rec["skills"] == 1 and rec["commands"] == 1 and rec["servers"] == 1

    # remove cleans the directory, the record, and the catalog entry.
    result = await handle_plugin_command(
        {"action": "remove", "name": "demo"},
        plugins_root=plugins_root,
        mcp_path=mcp_path,
    )
    assert result["ok"] is True, result
    assert "demo-server" in result["message"]
    assert result["removed"]["removed_mcp_servers"] == ["demo-server"]
    assert not (plugins_root / "demo").exists()
    doc = json.loads(mcp_path.read_text(encoding="utf-8"))
    assert "demo-server" not in doc["servers"]
    result = await handle_plugin_command(
        {"action": "list"}, plugins_root=plugins_root
    )
    assert result["plugins"] == []


# ---------------------------------------------------------------------------
# SkillStore refresh + McpManager reload hooks
# ---------------------------------------------------------------------------

async def test_install_and_remove_refresh_skill_stores(tmp_path):
    plugins_root, mcp_path = _roots(tmp_path)
    src = _make_plugin(tmp_path, "demo", with_mcp=False)
    stores = [FakeSkillStore(), FakeSkillStore()]

    result = await handle_plugin_command(
        {"action": "install", "source": str(src)},
        plugins_root=plugins_root,
        mcp_path=mcp_path,
        skill_stores=stores,
    )
    assert result["ok"] is True
    assert all(s.refresh_calls == 1 for s in stores)

    result = await handle_plugin_command(
        {"action": "remove", "name": "demo"},
        plugins_root=plugins_root,
        mcp_path=mcp_path,
        skill_stores=stores,
    )
    assert result["ok"] is True
    assert all(s.refresh_calls == 2 for s in stores)


async def test_skill_refresh_failure_is_a_note_not_an_error(tmp_path):
    plugins_root, mcp_path = _roots(tmp_path)
    src = _make_plugin(tmp_path, "demo", with_mcp=False)
    result = await handle_plugin_command(
        {"action": "install", "source": str(src)},
        plugins_root=plugins_root,
        mcp_path=mcp_path,
        skill_stores=[FakeSkillStore(fail=True)],
    )
    assert result["ok"] is True  # install itself succeeded
    assert "skill refresh failed" in result["message"]


async def test_mcp_reload_called_only_when_servers_present(tmp_path):
    plugins_root, mcp_path = _roots(tmp_path)
    manager = FakeMcpManager(mcp_path)

    # Plugin without MCP servers: no reload.
    src_plain = _make_plugin(tmp_path, "plain", with_mcp=False)
    result = await handle_plugin_command(
        {"action": "install", "source": str(src_plain)},
        plugins_root=plugins_root,
        mcp_manager=manager,
    )
    assert result["ok"] is True
    assert manager.reload_calls == 0

    # Plugin with MCP servers: reload on install and on remove.
    src_mcp = _make_plugin(tmp_path, "demo", with_mcp=True)
    result = await handle_plugin_command(
        {"action": "install", "source": str(src_mcp)},
        plugins_root=plugins_root,
        mcp_manager=manager,
    )
    assert result["ok"] is True
    assert manager.reload_calls == 1
    # The catalog write went to the manager's own catalog path
    # (mcp_path derived from the manager when not given explicitly).
    doc = json.loads(mcp_path.read_text(encoding="utf-8"))
    assert "demo-server" in doc["servers"]

    result = await handle_plugin_command(
        {"action": "remove", "name": "demo"},
        plugins_root=plugins_root,
        mcp_manager=manager,
    )
    assert result["ok"] is True
    assert manager.reload_calls == 2

    # Removing the server-less plugin: still no extra reload.
    result = await handle_plugin_command(
        {"action": "remove", "name": "plain"},
        plugins_root=plugins_root,
        mcp_manager=manager,
    )
    assert result["ok"] is True
    assert manager.reload_calls == 2


async def test_real_manager_sees_merged_server_after_install(tmp_path):
    """End-to-end with a real McpManager: install merges the plugin's
    server disabled, the handler's reload makes it show up in
    status_snapshot (/mcp status), and remove clears it again."""
    plugins_root, mcp_path = _roots(tmp_path)
    manager = McpManager.load(mcp_path, watchdog=False)
    assert manager.status_snapshot() == []

    src = _make_plugin(tmp_path, "demo", with_mcp=True)
    result = await handle_plugin_command(
        {"action": "install", "source": str(src)},
        plugins_root=plugins_root,
        mcp_manager=manager,
    )
    assert result["ok"] is True, result
    rows = {row["server"]: row for row in manager.status_snapshot()}
    assert "demo-server" in rows
    assert rows["demo-server"]["state"] == "disabled"
    assert rows["demo-server"]["enabled"] is False

    result = await handle_plugin_command(
        {"action": "remove", "name": "demo"},
        plugins_root=plugins_root,
        mcp_manager=manager,
    )
    assert result["ok"] is True
    assert manager.status_snapshot() == []


# ---------------------------------------------------------------------------
# Error shapes
# ---------------------------------------------------------------------------

async def test_usage_and_unknown_errors(tmp_path):
    plugins_root, mcp_path = _roots(tmp_path)
    # Missing args -> usage errors.
    result = await handle_plugin_command(
        {"action": "install"}, plugins_root=plugins_root, mcp_path=mcp_path
    )
    assert result["ok"] is False and "usage" in result["message"]
    result = await handle_plugin_command(
        {"action": "remove"}, plugins_root=plugins_root, mcp_path=mcp_path
    )
    assert result["ok"] is False and "usage" in result["message"]
    # Unknown action is a clean error, not an exception.
    result = await handle_plugin_command(
        {"action": "explode"}, plugins_root=plugins_root
    )
    assert result["ok"] is False
    assert "unknown plugin_command action" in result["message"]
    # PluginError surfaces as ok=False with the loader's message.
    result = await handle_plugin_command(
        {"action": "install", "source": str(tmp_path / "nope")},
        plugins_root=plugins_root,
        mcp_path=mcp_path,
    )
    assert result["ok"] is False and "not found" in result["message"]
    result = await handle_plugin_command(
        {"action": "remove", "name": "ghost"},
        plugins_root=plugins_root,
        mcp_path=mcp_path,
    )
    assert result["ok"] is False and "not installed" in result["message"]
    # Missing action defaults to list.
    result = await handle_plugin_command({}, plugins_root=plugins_root)
    assert result["ok"] is True and result["action"] == "list"


# ---------------------------------------------------------------------------
# Bridge stdin-IPC branch (freyja_bridge._handle_command)
# ---------------------------------------------------------------------------

async def test_bridge_plugin_command_ipc_branch(tmp_path, monkeypatch):
    """The plugin_command branch in _handle_command replies with a single
    plugin_command_result event (requestId passthrough) via emit(), pulls
    plugins_root/mcp_manager off state, and refreshes every live
    session's SkillStore."""
    import types

    import bridge.freyja_bridge as fb

    events: list[dict] = []
    monkeypatch.setattr(fb, "emit", lambda e: events.append(e))

    plugins_root, mcp_path = _roots(tmp_path)
    manager = McpManager.load(mcp_path, watchdog=False)
    store = FakeSkillStore()
    state = types.SimpleNamespace(
        sessions={
            "s1": types.SimpleNamespace(skill_store=store),
            "s2": types.SimpleNamespace(skill_store=None),
        },
        mcp_manager=manager,
        plugins_root=plugins_root,
        active_session_id=None,
    )

    src = _make_plugin(tmp_path, "demo", with_mcp=True)
    await fb._handle_command(
        state,
        {
            "type": "plugin_command",
            "action": "install",
            "source": str(src),
            "requestId": "r1",
        },
    )
    assert len(events) == 1
    event = events[0]
    assert event["type"] == "plugin_command_result"
    assert event["ok"] is True
    assert event["requestId"] == "r1"
    assert event["plugin"] == "demo"
    assert store.refresh_calls == 1
    # The manager reloaded and sees the merged (disabled) server.
    assert {row["server"] for row in manager.status_snapshot()} == {"demo-server"}
    # Everything stayed inside tmp roots.
    assert (plugins_root / "demo").is_dir()
    assert "demo-server" in json.loads(mcp_path.read_text(encoding="utf-8"))["servers"]

    # list through the same branch.
    events.clear()
    await fb._handle_command(
        state, {"type": "plugin_command", "action": "list", "requestId": "r2"}
    )
    assert events[0]["requestId"] == "r2"
    assert [p["name"] for p in events[0]["plugins"]] == ["demo"]

    # Unknown action comes back as a clean ok=False result, not a crash.
    events.clear()
    await fb._handle_command(state, {"type": "plugin_command", "action": "detonate"})
    assert events[0]["ok"] is False
    assert "unknown plugin_command action" in events[0]["message"]


# ---------------------------------------------------------------------------
# /plugin helpers
# ---------------------------------------------------------------------------

def test_parse_plugin_args():
    assert parse_plugin_args("") == ("list", "")
    assert parse_plugin_args("  ") == ("list", "")
    assert parse_plugin_args("list") == ("list", "")
    assert parse_plugin_args("Install https://github.com/x/y.git") == (
        "install", "https://github.com/x/y.git",
    )
    # The arg keeps internal whitespace (paths with spaces).
    assert parse_plugin_args("install /tmp/My Plugins/demo") == (
        "install", "/tmp/My Plugins/demo",
    )
    assert parse_plugin_args("remove demo") == ("remove", "demo")


def test_format_plugin_table():
    rows = [
        {"name": "demo", "version": "0.1.0", "skills": 2, "commands": 1,
         "servers": 1, "source": "/tmp/src/demo"},
        {"name": "huge", "version": "", "skills": 0, "commands": 0,
         "servers": 0, "source": "https://example.com/" + "x" * 60},
    ]
    table = format_plugin_table(rows)
    lines = table.splitlines()
    assert lines[0].split() == [
        "name", "version", "skills", "commands", "servers", "source",
    ]
    assert lines[1].startswith("-")
    assert "demo" in lines[2] and "0.1.0" in lines[2]
    assert "…" in lines[3]  # long source truncated
    # Empty list message.
    assert format_plugin_table([]) == "no plugins installed"
