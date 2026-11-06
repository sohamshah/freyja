"""A flat `allowed_user_ids` list fails closed instead of crashing the gateway.

`slack.allowed_user_ids` maps each workspace id to a list of user ids.
Written as a flat list, `GatewayConfig.load` called `.items()` on it and
raised, so the gateway LaunchAgent died at start and launchd started it
again every few seconds. Now the file loads, the allowlist lets nobody
in, the rest of the file still applies, and the log says how to write
the setting.
"""

from __future__ import annotations

import logging

import pytest

from bridge.gateway import config as gateway_config
from bridge.gateway.config import GatewayConfig

FLAT = """\
defaults:
  model: claude-opus-5-5
  reasoning_level: high
slack:
  allowed_user_ids: [U0OPERATOR, "*"]
  mention_required_in_channels: false
"""


@pytest.mark.parametrize("value", ['[U0OPERATOR, "*"]', '"*"', "U0OPERATOR"])
def test_an_allowlist_that_is_not_a_map_lets_nobody_in(value, caplog):
    gateway_config.config_path().write_text(
        FLAT.replace('[U0OPERATOR, "*"]', value), encoding="utf-8",
    )
    with caplog.at_level(logging.ERROR, logger=gateway_config.__name__):
        cfg = GatewayConfig.load()
    assert cfg.slack.allowed_user_ids == {}
    assert not cfg.slack.user_allowed("T1", "U0OPERATOR")
    assert not cfg.slack.user_allowed("T1", "U0MEMBER")
    # The rest of the file still applies.
    assert cfg.default_reasoning_level == "high"
    assert cfg.slack.mention_required_in_channels is False
    # The log names the setting and shows the shape it takes.
    error = " ".join(r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR)
    assert "slack.allowed_user_ids" in error
    assert "T012345: [U001]" in error
