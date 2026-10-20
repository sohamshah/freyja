"""Agent-experience surface: thread titles, app context, app-authored posts.

Three behaviours added after the agent_view migration, each with a
failure mode that is invisible rather than loud:

  · Proactive DMs root their own thread now, and an untitled thread is
    indistinguishable from every other one in the Messages tab.
  · app_context_changed entities were being captured and never used.
  · Messages posted under a human identity by an app bypass the bot
    echo filter entirely.
"""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

import pytest

from bridge.gateway.platforms.slack import SlackAdapter


def _adapter(tmp_path=None) -> SlackAdapter:
    """A SlackAdapter with only the state these helpers touch."""
    from bridge.gateway.slack_bindings import ThreadBindings

    a = SlackAdapter.__new__(SlackAdapter)
    a._app_context = {}
    a._APP_CONTEXT_MAX = 500
    a._seen_app_authors = set()
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

def test_proactive_thread_gets_a_title():
    a, c = _adapter(), _Client()
    asyncio.run(a._title_thread(c, "D123", "1788.1", "Build finished: 12 passed"))
    assert len(c.calls) == 1
    assert c.calls[0]["channel_id"] == "D123"
    assert c.calls[0]["thread_ts"] == "1788.1"
    # Slack's rename rejects colons (`invalid_name`), so they go.
    assert c.calls[0]["title"] == "Build finished 12 passed"


def test_title_is_truncated_not_paragraph_length():
    a, c = _adapter(), _Client()
    asyncio.run(a._title_thread(c, "D1", "1.0", "word " * 200))
    title = c.calls[0]["title"]
    assert len(title) <= a._THREAD_TITLE_MAX
    assert title.endswith("…")


def test_title_strips_markdown_and_newlines():
    """Raw agent output often opens with bold/quote chrome; a title
    beginning with '*' or a newline reads as broken."""
    a, c = _adapter(), _Client()
    asyncio.run(a._title_thread(c, "D1", "1.0", "*:warning: Alert*\n\nsecond line"))
    title = c.calls[0]["title"]
    assert title.startswith("warning")
    assert "\n" not in title


def test_empty_content_sets_no_title():
    a, c = _adapter(), _Client()
    asyncio.run(a._title_thread(c, "D1", "1.0", "   \n  "))
    assert c.calls == []


def test_title_on_a_thread_without_a_session_creates_one():
    """rename answers not_authorized until a session exists; the title
    then rides on the setStatus call that creates it."""
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


def test_same_title_is_not_sent_twice():
    a, c = _adapter(), _Client()
    asyncio.run(a._title_thread(c, "D1", "1.0", "Same title"))
    asyncio.run(a._title_thread(c, "D1", "1.0", "Same title"))
    assert len(c.calls) == 1


# ── app context ──────────────────────────────────────────────────────

def test_app_context_renders_entities_in_relevance_order():
    a = _adapter()
    a._app_context[("T1", "U1")] = [
        {"type": "slack#/types/channel_id", "value": "C123"},
        {"type": "slack#/types/canvas_id", "value": "F999"},
    ]
    assert a.describe_app_context("T1", "U1") == "channel C123, canvas F999"


def test_app_context_absent_returns_empty_not_unknown():
    """The caller omits the line entirely when this is empty — telling
    the model 'unknown' would spend tokens asserting ignorance."""
    assert _adapter().describe_app_context("T1", "U1") == ""


def test_app_context_tolerates_malformed_entities():
    a = _adapter()
    a._app_context[("T1", "U1")] = [
        {"type": "slack#/types/channel_id"},          # no value
        "not-a-dict",                                  # wrong shape
        {"type": "slack#/types/channel_id", "value": "C1"},
    ]
    assert a.describe_app_context("T1", "U1") == "channel C1"


def test_app_context_is_scoped_per_user():
    a = _adapter()
    a._app_context[("T1", "U1")] = [{"type": "x/channel_id", "value": "C1"}]
    assert a.describe_app_context("T1", "U2") == ""
    assert a.describe_app_context("T2", "U1") == ""


@pytest.mark.parametrize("n_entities", [1, 3, 10])
def test_app_context_caps_at_three(n_entities):
    a = _adapter()
    a._app_context[("T", "U")] = [
        {"type": "x/channel_id", "value": f"C{i}"} for i in range(n_entities)
    ]
    assert a.describe_app_context("T", "U").count(",") <= 2


# ── app-authored message diagnostic ──────────────────────────────────

def test_app_authored_diagnostic_is_once_per_app():
    """It logs to capture an undocumented payload shape, not to narrate
    every message — a chatty integration would flood the log."""
    import inspect

    src = inspect.getsource(SlackAdapter._on_message_event) if hasattr(
        SlackAdapter, "_on_message_event"
    ) else inspect.getsource(SlackAdapter)
    assert "_seen_app_authors" in src
    # The guard must be membership-based, not unconditional logging.
    assert "not in self._seen_app_authors" in src


def test_app_authored_messages_are_not_dropped():
    """Deliberate: an app_id can belong to a legitimate integration the
    operator routes into a thread. Dropping on a guess would silently
    swallow real input, which is worse than the duplicate turn."""
    import inspect

    src = inspect.getsource(SlackAdapter)
    marker = src.index("_seen_app_authors.add")
    following = src[marker : marker + 600]
    assert "return" not in following.split("logger.info")[0]
