"""When `gateway.yaml` cannot be read, the Slack tool filter is on.

`gateway_filter_enabled` returned False when loading the config raised,
and `GatewayConfig.load` itself returned its defaults (filter off) on a
YAML error. So a typo in the file, or junk written over it, gave every new
Slack session the full toolset, and the log did not say the filter was off.
The operator still gets replies: the filter only takes tools away.
"""

from __future__ import annotations

import logging

from bridge.gateway import capabilities
from bridge.gateway.capabilities import gateway_filter_enabled, tools_allowed_for_gateway
from bridge.gateway.config import config_path
from bridge.gateway.platforms.base import MessageSource, Platform


def test_a_yaml_error_turns_the_filter_on_and_says_so(caplog):
    config_path().write_text("slack:\n  enable_tool_filter: false\n  allowed_user_ids: {T1: [U1\n")
    with caplog.at_level(logging.ERROR, logger=capabilities.__name__):
        assert gateway_filter_enabled(Platform.SLACK) is True
    error = " ".join(r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR)
    assert str(config_path()) in error and "filter is on" in error


def test_a_file_that_is_not_a_mapping_turns_the_filter_on():
    config_path().write_text("- slack\n- enable_tool_filter\n")
    assert gateway_filter_enabled(Platform.SLACK) is True


def test_a_missing_or_readable_file_keeps_its_setting():
    assert not config_path().exists()
    assert gateway_filter_enabled(Platform.SLACK) is False
    config_path().write_text("slack:\n  enable_tool_filter: false\n")
    assert gateway_filter_enabled(Platform.SLACK) is False
    config_path().write_text("slack:\n  enable_tool_filter: true\n")
    assert gateway_filter_enabled(Platform.SLACK) is True


async def test_a_slack_session_still_builds_with_its_reply_tools(monkeypatch, tmp_path):
    import bridge.freyja_bridge as fb

    monkeypatch.setattr(fb, "emit", lambda _e: None)
    config_path().write_text("slack: [\n")
    state = fb._BridgeState(workspace=str(tmp_path), default_model="claude-sonnet-5-5")
    sess = fb._BridgeSession(
        "freyja:slack:T1:channel:C1:5.5", workspace=str(tmp_path), model_id="claude-sonnet-5-5",
        reasoning_level="auto", coordination_strategy="bus", state=state,
    )
    sess.gateway_source = MessageSource(
        platform=Platform.SLACK, workspace_id="T1", chat_type="channel", chat_id="C1",
        user_id="U0OPERATOR", thread_id="5.5", message_id="5.5",
    )
    await sess.initialize()
    names = set(sess.tool_registry.list_names())
    assert names <= tools_allowed_for_gateway(Platform.SLACK)
    assert {"read_file", "send_attachment", "slack_widget"} <= names
    assert "bash" not in names
    assert sess.runner is not None
