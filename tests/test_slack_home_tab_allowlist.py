"""The App Home tab shows Freyja's state only to someone on the allowlist.

Any member of the workspace can open the app's Home tab. It listed the
operator's Slack sessions, scheduled jobs and skill candidates to
whoever opened it. Messages, slash commands and card clicks pass the
allowlist first; the Home tab now does too, and anyone else sees a
short note that this is a personal assistant.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

from bridge.gateway.config import SlackConfig
from bridge.gateway.run import GatewayDaemon

OPERATOR, MEMBER = "U0OPERATOR", "U0MEMBER"
SECRET_TITLE = "Plan the offsite budget"


class _Bindings:
    def title_for(self, _chat: str, _thread: str | None) -> str:
        return SECRET_TITLE


class _Adapter:
    def __init__(self) -> None:
        self.slack_config = SlackConfig(allowed_user_ids={"T1": [OPERATOR]})
        self.bindings = _Bindings()
        self.published: list[tuple[str, dict[str, Any]]] = []

    async def publish_home(self, user_id: str, view: dict[str, Any], *, team_id: str | None = None):
        self.published.append((user_id, view))
        return SimpleNamespace(ok=True)


def _daemon(adapter: _Adapter) -> GatewayDaemon:
    daemon = GatewayDaemon.__new__(GatewayDaemon)
    session = SimpleNamespace(pending_task=None, model_id="claude-opus-5-5")
    daemon.state = SimpleNamespace(
        sessions={"freyja:slack:T1:dm:D1": session}, default_model="claude-opus-5-5",
    )
    daemon._slack_adapter = lambda: adapter  # type: ignore[method-assign]
    return daemon


async def _open_home(user_id: str, team_id: str = "T1") -> dict[str, Any]:
    adapter = _Adapter()
    await _daemon(adapter)._on_home_opened(
        {"tab": "home", "team_id": team_id, "user_id": user_id, "chat_id": "D9"}
    )
    assert [u for u, _ in adapter.published] == [user_id]
    return adapter.published[0][1]


async def test_a_member_not_on_the_allowlist_sees_no_sessions_jobs_or_skills():
    view = await _open_home(MEMBER)
    text = json.dumps(view)
    assert SECRET_TITLE not in text
    assert "Sessions" not in text
    assert "Scheduled jobs" not in text
    assert "Skills waiting" not in text
    assert "claude-opus-5-5" not in text
    assert "personal assistant" in text


async def test_someone_from_another_workspace_sees_the_note_too():
    text = json.dumps(await _open_home(OPERATOR, team_id="T9"))
    assert SECRET_TITLE not in text
    assert "personal assistant" in text


async def test_the_operator_still_sees_the_status_view():
    text = json.dumps(await _open_home(OPERATOR))
    assert SECRET_TITLE in text
    assert "Scheduled jobs" in text
