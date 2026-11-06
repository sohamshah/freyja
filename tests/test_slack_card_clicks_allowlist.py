"""Approve, Deny, Promote and Discard on a Slack card count only from
someone on the allowlist.

The cards post in the session's chat, so everyone in the channel sees
them, and the click handlers checked no one: any member could approve a
command the agent asked to run (bash, an MCP tool) or publish a skill
the agent then loads in every session. Messages and slash commands pass
the allowlist first; a click is as privileged as either.
"""

from __future__ import annotations

import pytest

from bridge.gateway import approval
from bridge.gateway.config import SlackConfig
from bridge.gateway.platforms.slack import SlackAdapter

OPERATOR, MEMBER = "U0OPERATOR", "U0MEMBER"


def _adapter() -> SlackAdapter:
    return SlackAdapter(slack_config=SlackConfig(allowed_user_ids={"T1": [OPERATOR]}))


def _click(user: str, value: str, team: str = "T1") -> dict:
    return {
        "actions": [{"value": value}],
        "user": {"id": user, "team_id": team},
        "team": {"id": team},
        "channel": {"id": "C1"},
        "message": {"ts": "1.0"},
    }


class _Respond:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def __call__(self, **kw) -> None:
        self.calls.append(kw)


@pytest.fixture
def pending():
    """A permission prompt the agent is waiting on."""
    answers: list[bool] = []
    approval.register_external_resolver("req-1", lambda ok: answers.append(ok) or True)
    yield answers
    approval.unregister_external_resolver("req-1")


@pytest.mark.parametrize("approved", [True, False])
async def test_a_click_from_a_member_not_on_the_allowlist_answers_nothing(pending, approved):
    respond = _Respond()
    await _adapter()._handle_approval_click(_click(MEMBER, "req-1"), respond, approved=approved)
    assert pending == []
    # The card keeps its buttons; the member alone is told why.
    assert all(not call.get("replace_original") for call in respond.calls)
    assert respond.calls and respond.calls[0]["response_type"] == "ephemeral"


async def test_the_operators_click_still_answers(pending):
    respond = _Respond()
    await _adapter()._handle_approval_click(_click(OPERATOR, "req-1"), respond, approved=True)
    assert pending == [True]
    assert respond.calls[0]["replace_original"] is True


async def test_a_click_from_another_workspace_answers_nothing(pending):
    click = _click(OPERATOR, "req-1", team="T9")
    await _adapter()._handle_approval_click(click, _Respond(), approved=True)
    assert pending == []


@pytest.mark.parametrize("action", ["promote", "discard"])
async def test_a_skill_card_click_from_a_member_not_on_the_allowlist_does_nothing(
    monkeypatch, action,
):
    from bridge.knowledge.learning import confirmation

    calls: list[str] = []
    monkeypatch.setattr(confirmation, "promote", lambda cid, **_kw: calls.append(cid))
    monkeypatch.setattr(confirmation, "discard", lambda cid, **_kw: calls.append(cid))
    respond = _Respond()
    await _adapter()._handle_skill_candidate_click(_click(MEMBER, "cand-1"), respond, action=action)
    assert calls == []
    assert respond.calls and respond.calls[0]["response_type"] == "ephemeral"
