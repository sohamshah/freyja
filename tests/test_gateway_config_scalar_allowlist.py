"""A workspace whose allowlist is one value, not a list, lets nobody in and
the log says how to write it.

Each workspace under `slack.allowed_user_ids` takes a list of user ids.
Written as one value, `T1: U001` was read character by character, as
`['U', '0', '0', '1']`: it let nobody in, but nothing said why. A number
or `true` there raised at load, so the gateway died at start and launchd
started it again every few seconds. The value is not read as a one-item
list: `"*"` would then open the workspace to every member, with full
tools, from a shape the wizard never writes.
"""

from __future__ import annotations

import logging

import pytest

from bridge.gateway import config as gateway_config
from bridge.gateway.config import GatewayConfig

YAML = """\
defaults:
  reasoning_level: high
slack:
  allowed_user_ids:
    T1: VALUE
    T2: [U0OPERATOR]
  mention_required_in_channels: false
"""


@pytest.mark.parametrize("value", ["U0OPERATOR", '"*"', "U0OPERATOR, U0MEMBER", "12345", "true"])
def test_one_value_for_a_workspace_lets_nobody_in_and_says_so(value, caplog):
    gateway_config.config_path().write_text(YAML.replace("VALUE", value), encoding="utf-8")
    with caplog.at_level(logging.ERROR, logger=gateway_config.__name__):
        cfg = GatewayConfig.load()
    assert cfg.slack.allowed_user_ids["T1"] == []
    assert not cfg.slack.user_allowed("T1", "U0OPERATOR")
    assert not cfg.slack.user_allowed("T1", "U")
    # The other workspace and the rest of the file still apply.
    assert cfg.slack.user_allowed("T2", "U0OPERATOR")
    assert cfg.default_reasoning_level == "high"
    assert cfg.slack.mention_required_in_channels is False
    error = " ".join(r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR)
    assert "slack.allowed_user_ids.T1" in error
    assert "T1: [U001]" in error
