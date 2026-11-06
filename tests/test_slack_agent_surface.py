"""Agent-experience surface: thread titles and app context.

Two behaviours added after the agent_view migration, each with a failure mode that is invisible rather than loud: proactive DMs root their own thread now, and an untitled thread is indistinguishable from every other one in the Messages tab; app_context_changed entities were being captured and never used.
"""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

from bridge.gateway.platforms.slack import SlackAdapter


def _adapter(tmp_path=None) -> SlackAdapter:
    """A SlackAdapter with only the state these helpers touch."""
    from bridge.gateway.slack_bindings import ThreadBindings

    a = SlackAdapter.__new__(SlackAdapter)
    a._app_context = {}
    a._APP_CONTEXT_MAX = 500
    a._app = object()
    a._team_clients = {}
    a._channel_team = {}
    a.bindings = ThreadBindings(
        (tmp_path or Path(tempfile.mkdtemp())) / "bindings.json", flush_delay=0
    )
    return a


class _Client:
    """Fakes agents.sessions.rename; ``fail`` mimics a thread with no session."""

    def __init__(self, fail: bool = False):
        self.calls: list[dict] = []
        self.status_calls: list[dict] = []
        self._fail = fail

    async def agents_sessions_rename(self, **kwargs):
        if self._fail:
            raise RuntimeError("not_authorized")
        self.calls.append(kwargs)
        return {"ok": True}

    async def agents_sessions_setStatus(self, **kwargs):
        self.status_calls.append(kwargs)
        return {"ok": True}


# ── thread titling ───────────────────────────────────────────────────

def test_proactive_thread_gets_a_cleaned_title_once():
    """Slack's rename rejects colons (`invalid_name`), so they go; the same title is not sent twice; empty content sets no title at all."""
    a, c = _adapter(), _Client()
    asyncio.run(a._title_thread(c, "D123", "1788.1", "Build finished: 12 passed"))
    asyncio.run(a._title_thread(c, "D123", "1788.1", "Build finished: 12 passed"))
    assert len(c.calls) == 1
    assert c.calls[0]["channel_id"] == "D123"
    assert c.calls[0]["thread_ts"] == "1788.1"
    assert c.calls[0]["title"] == "Build finished 12 passed"
    asyncio.run(a._title_thread(c, "D1", "1.0", "   \n  "))
    assert len(c.calls) == 1


def test_title_on_a_thread_without_a_session_creates_one():
    """rename answers not_authorized until a session exists; the title then rides on the setStatus call that creates it."""
    a, c = _adapter(), _Client(fail=True)
    asyncio.run(a._title_thread(c, "D1", "1.0", "hello there"))  # must not raise
    assert c.calls == []
    assert c.status_calls and c.status_calls[0]["title"] == "hello there"
    assert c.status_calls[0]["status"] == "active"


def test_operator_title_is_not_overwritten_by_an_automatic_one():
    a, c = _adapter(), _Client()
    a.bindings.set_title("D1", "1.0", "My own name", by_user=True)
    asyncio.run(a._title_thread(c, "D1", "1.0", "auto title"))
    assert c.calls == []
    asyncio.run(a.rename_session("D1", "1.0", "Renamed by me", by_user=True, client=c))
    assert c.calls[-1]["title"] == "Renamed by me"


# ── app context ──────────────────────────────────────────────────────

def test_app_context_renders_up_to_three_entities_for_that_user_only():
    """Entities in relevance order, malformed ones skipped, at most three, scoped to the (team, user) pair. Absent context renders as "" because the caller omits the line entirely; telling the model 'unknown' would spend tokens asserting ignorance."""
    a = _adapter()
    assert a.describe_app_context("T1", "U1") == ""
    a._app_context[("T1", "U1")] = [
        {"type": "slack#/types/channel_id", "value": "C123"},
        {"type": "slack#/types/canvas_id", "value": "F999"},
    ]
    assert a.describe_app_context("T1", "U1") == "channel C123, canvas F999"
    assert a.describe_app_context("T1", "U2") == ""
    assert a.describe_app_context("T2", "U1") == ""
    a._app_context[("T1", "U1")] = [
        {"type": "slack#/types/channel_id"},          # no value
        "not-a-dict",                                  # wrong shape
        {"type": "slack#/types/channel_id", "value": "C1"},
    ]
    assert a.describe_app_context("T1", "U1") == "channel C1"
    a._app_context[("T", "U")] = [
        {"type": "x/channel_id", "value": f"C{i}"} for i in range(10)
    ]
    rendered = a.describe_app_context("T", "U")
    assert rendered and rendered.count(",") <= 2
