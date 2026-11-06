"""With `slack.enable_tool_filter` on, the gateway context each Slack message
carries tells the model what the filter allows.

It said only the read-only tools were fine, and named none of `write_file`,
`edit_file`, `edit_json`, `memory` or `schedule`, which the filter allows
within limits: writes in the session's project output directory, memory
lists and shows only, and `schedule` changes and runs only jobs made under
the filter.
"""

from __future__ import annotations

import pytest

from bridge.gateway import slack_capabilities
from bridge.gateway.capabilities import tools_allowed_for_gateway
from bridge.gateway.config import config_path
from bridge.gateway.platforms.base import MessageSource, Platform
from bridge.gateway.session_router import gateway_source_block

SOURCE = MessageSource(
    platform=Platform.SLACK, workspace_id="T1", chat_type="channel", chat_id="C1",
    user_id="U0OPERATOR", thread_id="5.5", message_id="5.5",
)


@pytest.fixture(autouse=True)
def _no_capability_card(monkeypatch):
    # The Slack capability card reads tokens from the environment.
    monkeypatch.setattr(slack_capabilities, "slack_capability_block", lambda: "")


def _block(*, filter_on: bool) -> str:
    config_path().write_text(f"slack:\n  enable_tool_filter: {str(filter_on).lower()}\n")
    return gateway_source_block(SOURCE)


def test_it_names_every_tool_the_filter_allows():
    block = _block(filter_on=True)
    missing = {t for t in tools_allowed_for_gateway(Platform.SLACK) if f"`{t}`" not in block}
    assert missing == set()


def test_it_says_what_the_filter_limits():
    block = _block(filter_on=True)
    assert "Read-only tools" not in block
    assert "project output directory" in block
    assert "lists and shows" in block
    assert "jobs made under this filter" in block


def test_with_the_filter_off_it_offers_every_tool():
    block = _block(filter_on=False)
    assert "TOOL ACCESS: you have the FULL tool surface" in block
    assert "project output directory" not in block
