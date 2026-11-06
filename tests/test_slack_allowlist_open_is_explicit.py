"""Opening the Slack gateway to a whole workspace is an explicit choice.

Everyone the allowlist lets in gets the full toolset (bash, file
writes, computer use). A blank answer to the setup wizard's allowlist
question wrote an empty list, and an empty list meant "every member of
this workspace"; `enforce_workspace_allowlist: false` let in every user
of every workspace. Now only "*" under a workspace opens it, the wizard
asks again on a blank answer and warns when "*" is chosen, and an empty
list lets nobody in.
"""

from __future__ import annotations

import pytest

from bridge.gateway import config as gateway_config
from bridge.gateway.config import GatewayConfig, SlackConfig
from bridge.gateway.setup import wizard

OPERATOR, MEMBER = "U0OPERATOR", "U0MEMBER"


# ── what the config lets in ──


def test_an_empty_list_lets_nobody_in():
    cfg = SlackConfig(allowed_user_ids={"T1": []})
    assert not cfg.user_allowed("T1", MEMBER)


def test_a_star_lets_every_member_of_that_workspace_in():
    cfg = SlackConfig(allowed_user_ids={"T1": ["*"]})
    assert cfg.user_allowed("T1", MEMBER)
    assert not cfg.user_allowed("T9", MEMBER)
    assert not cfg.user_allowed("T1", "")


def test_enforce_false_does_not_open_the_gateway():
    cfg = SlackConfig(allowed_user_ids={"T1": [OPERATOR]}, enforce_workspace_allowlist=False)
    assert cfg.user_allowed("T1", OPERATOR)
    assert not cfg.user_allowed("T1", MEMBER)
    assert not cfg.user_allowed("T9", MEMBER)


# The shape of a real one-person gateway.yaml, as the wizard writes it.
ONE_PERSON_YAML = """\
defaults:
  model: claude-opus-5-5
  reasoning_level: high
  coordination_strategy: bus
slack:
  allowed_user_ids:
    T0WORKSPACE:
    - U0OPERATOR
  enforce_workspace_allowlist: true
  mention_required_in_channels: true
  reply_in_thread: true
  enable_tool_filter: false
"""


def test_a_one_person_gateway_yaml_loads_the_same():
    gateway_config.config_path().write_text(ONE_PERSON_YAML, encoding="utf-8")
    cfg = GatewayConfig.load()
    assert cfg.slack.allowed_user_ids == {"T0WORKSPACE": [OPERATOR]}
    assert cfg.slack.user_allowed("T0WORKSPACE", OPERATOR)
    assert not cfg.slack.user_allowed("T0WORKSPACE", MEMBER)
    assert not cfg.slack.user_allowed("T9", OPERATOR)
    # Saving it again writes the same file.
    assert cfg.to_yaml() == ONE_PERSON_YAML


# ── the setup wizard ──


def _run_wizard(monkeypatch, answers: list[str]) -> tuple[SlackConfig, list[str], str]:
    """Run `freyja setup slack` with faked Slack and terminal. ``answers``
    are typed, in order, at the allowlist question; returns the saved
    config, the allowlist prompts shown, and everything printed."""
    import builtins
    import getpass

    asked: list[str] = []
    printed: list[str] = []
    pending = list(answers)

    def fake_input(prompt: str = "") -> str:
        if "Allowed user" in prompt:
            asked.append(prompt)
            if not pending:
                raise AssertionError("the wizard asked for the allowlist again")
            return pending.pop(0)
        return "n" if "Install" in prompt else ""

    def fake_getpass(prompt: str = "") -> str:
        return "xapp-test" if "App Token" in prompt else "xoxb-test"

    async def fake_verify(_bot: str, _app: str):
        return True, {"bot_user_id": "U0BOT", "bot_name": "freyja", "team_id": "T1", "team_name": "Acme"}

    monkeypatch.setattr(builtins, "input", fake_input)
    monkeypatch.setattr(getpass, "getpass", fake_getpass)
    monkeypatch.setattr(builtins, "print", lambda *a, **_k: printed.append(" ".join(map(str, a))))
    monkeypatch.setattr(wizard, "_copy_to_clipboard", lambda _text: False)
    monkeypatch.setattr(wizard, "_verify_tokens", fake_verify)
    monkeypatch.setattr(wizard, "_LAUNCHD_AVAILABLE", False)
    assert wizard.run_slack_setup() == 0
    assert pending == [], "the wizard stopped asking before using every answer"
    return GatewayConfig.load().slack, asked, "\n".join(printed)


def test_a_blank_answer_asks_again(monkeypatch):
    slack, asked, _ = _run_wizard(monkeypatch, ["", OPERATOR])
    assert len(asked) == 2
    assert slack.allowed_user_ids == {"T1": [OPERATOR]}
    assert not slack.user_allowed("T1", MEMBER)


@pytest.mark.parametrize("answer", ["any", "mallory"])
def test_an_answer_that_is_not_a_user_id_asks_again(monkeypatch, answer):
    slack, asked, _ = _run_wizard(monkeypatch, [answer, OPERATOR])
    assert len(asked) == 2
    assert slack.allowed_user_ids == {"T1": [OPERATOR]}
    assert slack.enforce_workspace_allowlist


def test_a_star_opens_this_workspace_with_a_warning(monkeypatch):
    slack, _, out = _run_wizard(monkeypatch, ["*"])
    assert slack.allowed_user_ids == {"T1": ["*"]}
    assert slack.user_allowed("T1", MEMBER)
    assert not slack.user_allowed("T9", MEMBER)
    assert "full tools" in out
