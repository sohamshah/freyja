"""The Freyja MCP server that harness runtimes (Claude Code, Codex) spawn
(bridge/runtimes/freyja_mcp_server.py) and the bridge dispatcher behind it
(_BridgeSession._dispatch_harness_tool_call). A tool the catalog's
descriptions tell the model to call must be in the catalog, and every catalog
tool must reach a bridge tool. A fake freyja_native lists the displays;
nothing is captured and no input is sent."""

from __future__ import annotations

import asyncio
import json
import re
from types import SimpleNamespace

from bridge.freyja_bridge import _BridgeSession
from bridge.runtimes import freyja_mcp_server as mcp
from bridge.tools import computer_tools as ct

CATALOG_NAMES = {tool["name"] for tool in mcp.TOOL_CATALOG}


def _session(cache=None):
    session = SimpleNamespace(
        id="t",
        runtime="claude-code",
        computer_cancel=asyncio.Event(),
        _harness_tools_cache=dict(cache or {}),
    )
    session._build_harness_computer_tools = lambda: _BridgeSession._build_harness_computer_tools(session)
    return session


def _dispatch(session, name, arguments=None):
    return _BridgeSession._dispatch_harness_tool_call(session, name, arguments or {})


def test_every_tool_a_description_names_is_in_the_catalog():
    named = set(re.findall(r"\bfreyja_[a-z_]+", json.dumps(mcp.TOOL_CATALOG)))
    assert named - CATALOG_NAMES == set()


class _Recorder:
    """Stands in for a bridge tool, so dispatch runs nothing real."""

    def __init__(self, name):
        self.name = name

    async def execute(self, call_id, arguments):
        return ct.ToolResult(call_id=call_id, content=f"ran {self.name}", is_error=False)


async def test_every_catalog_tool_reaches_a_bridge_tool():
    spec = ct.ComputerToolSpec(session_id="t", emit_event=lambda e: None, cancel_event=asyncio.Event())
    cache = {tool.definition.name: _Recorder(tool.definition.name) for tool in ct.build_computer_tools(spec)}
    session = _session(cache)
    for name in sorted(CATALOG_NAMES):
        res = await _dispatch(session, name)
        assert res["isError"] is False, f"{name}: {res['content']}"


class _Desk:
    """A fake freyja_native with displays 1 (primary) and 2."""

    @staticmethod
    def list_displays():
        return [
            SimpleNamespace(id=1, width=3024, height=1964, scale=2.0, is_primary=True),
            SimpleNamespace(id=2, width=2560, height=1440, scale=1.0, is_primary=False),
        ]


async def test_list_displays_returns_the_ids_screenshot_takes(monkeypatch):
    monkeypatch.setattr(ct, "_import_native", lambda: _Desk)
    monkeypatch.setattr(ct, "display_geometry", lambda: {})
    res = await _dispatch(_session(), "freyja_list_displays")
    assert res["isError"] is False
    text = res["content"][0]["text"]
    assert text.startswith("Found 2 display(s):")
    assert "id=1  3024x1964  scale=2.0x (primary)" in text
    assert "id=2  2560x1440  scale=1.0x" in text
