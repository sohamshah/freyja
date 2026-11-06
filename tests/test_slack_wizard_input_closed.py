"""`freyja setup slack` stops when its input closes, says so, exits
non-zero and writes nothing.

A closed stdin made every prompt return its default: a blank token or
allowlist answer, then "Try again?" defaulting to yes, so the token and
allowlist questions asked forever. The last question, "Install + start
now?", defaulted to yes and installed the background service.
"""

from __future__ import annotations

import builtins
import getpass
from types import SimpleNamespace

import pytest

from bridge.gateway import config as gateway_config
from bridge.gateway.setup import env_writer, wizard

OPERATOR = "U0OPERATOR"


def _run(monkeypatch, closes_at: str) -> tuple[int, str, list[str]]:
    """Run the wizard with faked Slack and terminal; the input closes at
    the first prompt that contains ``closes_at``. Returns the exit code,
    everything printed and the services installed."""
    printed: list[str] = []
    installed: list[str] = []
    state = {"closed": False, "asked": 0}

    def ask(prompt: str = "") -> str:
        state["asked"] += 1
        if state["asked"] > 50:
            raise AssertionError("the wizard kept asking after its input closed")
        if closes_at in prompt:
            state["closed"] = True
        if state["closed"]:
            raise EOFError
        if "App Token" in prompt:
            return "xapp-test"
        if "Bot Token" in prompt:
            return "xoxb-test"
        if "Allowed user" in prompt:
            return OPERATOR
        if "Install" in prompt:
            return "y"
        return ""

    async def fake_verify(_bot: str, _app: str):
        return True, {"bot_user_id": "U0BOT", "bot_name": "freyja", "team_id": "T1", "team_name": "Acme"}

    monkeypatch.setattr(builtins, "input", ask)
    monkeypatch.setattr(getpass, "getpass", ask)
    monkeypatch.setattr(builtins, "print", lambda *a, **_k: printed.append(" ".join(map(str, a))))
    monkeypatch.setattr(wizard, "_copy_to_clipboard", lambda _text: False)
    monkeypatch.setattr(wizard, "_verify_tokens", fake_verify)
    monkeypatch.setattr(wizard, "_LAUNCHD_AVAILABLE", True)
    monkeypatch.setattr(wizard, "launchd_mod", SimpleNamespace(install=lambda: installed.append("gateway") or "x.plist"))
    monkeypatch.setattr(wizard, "get_running_pid", lambda: 1)
    code = wizard.run_slack_setup()
    return code, "\n".join(printed), installed


@pytest.mark.parametrize("closes_at", ["Press Enter", "App Token", "Bot Token", "Allowed user"])
def test_input_closing_before_the_allowlist_is_answered_writes_nothing(monkeypatch, closes_at):
    code, out, installed = _run(monkeypatch, closes_at)
    assert code != 0
    assert "Input ended" in out
    assert not env_writer.env_path().exists()
    assert not gateway_config.config_path().exists()
    assert installed == []


def test_input_closing_at_reconfigure_keeps_the_old_tokens(monkeypatch):
    env_writer.save_env_value("SLACK_APP_TOKEN", "xapp-old")
    env_writer.save_env_value("SLACK_BOT_TOKEN", "xoxb-old")
    before = env_writer.env_path().read_text()
    code, out, _ = _run(monkeypatch, "Reconfigure")
    assert code != 0
    assert "Input ended" in out
    assert env_writer.env_path().read_text() == before


def test_input_closing_at_install_does_not_install_the_service(monkeypatch):
    code, out, installed = _run(monkeypatch, "Install")
    assert code != 0
    assert installed == []
    assert "Input ended" in out
    # Every question before it was answered, so those answers are kept.
    assert env_writer.get_env_value("SLACK_BOT_TOKEN") == "xoxb-test"
    assert gateway_config.GatewayConfig.load().slack.allowed_user_ids == {"T1": [OPERATOR]}


def test_a_full_run_still_saves_and_installs(monkeypatch):
    code, _, installed = _run(monkeypatch, "never matches any prompt")
    assert code == 0
    assert installed == ["gateway"]
    assert env_writer.get_env_value("SLACK_APP_TOKEN") == "xapp-test"
    assert env_writer.get_env_value("SLACK_BOT_TOKEN") == "xoxb-test"
    assert gateway_config.GatewayConfig.load().slack.allowed_user_ids == {"T1": [OPERATOR]}
