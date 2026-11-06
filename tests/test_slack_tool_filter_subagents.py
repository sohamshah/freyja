"""A sub-agent of a Slack session under the tool filter gets only the
filter's tools.

A child inherits its parent's filtered tools, but each child was also
given `talk`, `list_agent_sessions` and `show_widget`, and the bus, kanban
and task tools of its mode. None is on the filter's list, and `talk` can
wake any session, a desktop session with every tool included, with a
message that session then acts on.
"""

from __future__ import annotations

import pytest

from bridge.gateway.capabilities import tools_allowed_for_gateway
from bridge.gateway.config import config_path
from bridge.gateway.platforms.base import MessageSource, Platform

SLACK = "freyja:slack:T1:channel:C1:5.5"
CHILD_EXTRAS = {"talk", "list_agent_sessions", "show_widget"}


class _Stop(Exception):
    pass


async def _child_tools(monkeypatch, tmp_path, *, filter_on: bool, strategy: str) -> set[str]:
    """Build a Slack session, spawn one child from its sub_agent tool, and
    return the tools the child's runner is given."""
    import bridge.freyja_bridge as fb
    import engine.runner as runner_module

    monkeypatch.setattr(fb, "emit", lambda _e: None)
    config_path().write_text(f"slack:\n  enable_tool_filter: {str(filter_on).lower()}\n")
    state = fb._BridgeState(workspace=str(tmp_path), default_model="claude-sonnet-5-5")
    sess = fb._BridgeSession(
        SLACK, workspace=str(tmp_path), model_id="claude-sonnet-5-5",
        reasoning_level="auto", coordination_strategy=strategy, state=state,
    )
    sess.gateway_source = MessageSource(
        platform=Platform.SLACK, workspace_id="T1", chat_type="channel", chat_id="C1",
        user_id="U0OPERATOR", thread_id="5.5", message_id="5.5",
    )
    await sess.initialize()
    seen: dict[str, set[str]] = {}

    class _Runner:
        def __init__(self, *, tool_registry, **_kw):
            seen["tools"] = set(tool_registry.list_names())
            raise _Stop

    monkeypatch.setattr(runner_module, "AsyncAgentRunner", _Runner)
    tool = sess.tool_registry.get("sub_agent")
    record = tool._spec.registry.register(  # noqa: SLF001
        id="sub_filter_1", label="helper", task="look around", mode="background",
    )
    record.task_id = "t1"
    try:
        with pytest.raises(_Stop):
            await tool._run_child(record)  # noqa: SLF001
    finally:
        sess._stop_kanban_dispatcher()
    return seen["tools"]


@pytest.mark.parametrize("strategy", ["bus", "kanban"])
async def test_a_child_gets_only_the_filters_tools(monkeypatch, tmp_path, strategy):
    tools = await _child_tools(monkeypatch, tmp_path, filter_on=True, strategy=strategy)
    assert tools <= tools_allowed_for_gateway(Platform.SLACK)
    assert "talk" not in tools
    assert {"read_file", "send_attachment"} <= tools


async def test_with_the_filter_off_a_child_keeps_talk_and_its_mode_tools(monkeypatch, tmp_path):
    tools = await _child_tools(monkeypatch, tmp_path, filter_on=False, strategy="bus")
    assert CHILD_EXTRAS | {"publish_finding", "read_findings", "tasks", "bash"} <= tools
