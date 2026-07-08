"""Slack reach verbs (bridge/voice/adapters/slack.py).

Nothing here touches real Slack — ``AsyncWebClient`` is monkeypatched on
the adapter module with a canned fake, and the module-level caches are
reset around every test.
"""

from __future__ import annotations

import time

import pytest

from bridge.voice.adapters import slack
from bridge.voice.verbs import VerbRegistry


@pytest.fixture
def reg():
    registry = VerbRegistry()
    slack.register(registry)
    return registry


@pytest.fixture(autouse=True)
def _reset_caches():
    slack._reset_caches()
    yield
    slack._reset_caches()


@pytest.fixture
def token_env(monkeypatch):
    # Comma-separated multi-workspace convention: only the FIRST is used.
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-first, xoxb-second")


class FakeSlackClient:
    """Duck-types the slice of AsyncWebClient the adapter uses. Class
    attributes hold the shared canned world + call log so the adapter's
    own instantiations (`AsyncWebClient(token=...)`) all hit it."""

    calls: list[tuple[str, dict]] = []
    tokens: list[str] = []
    channels: list[dict] = []
    history: list[dict] = []
    members: list[dict] = []
    users: dict[str, dict] = {}
    fail_with: Exception | None = None

    @classmethod
    def reset(cls, channels=(), history=(), members=(), users=None):
        cls.calls = []
        cls.tokens = []
        cls.channels = list(channels)
        cls.history = list(history)
        cls.members = list(members)
        cls.users = dict(users or {})
        cls.fail_with = None

    def __init__(self, token=None):
        type(self).tokens.append(token)

    def _log(self, method, kwargs):
        type(self).calls.append((method, kwargs))
        if type(self).fail_with is not None:
            raise type(self).fail_with

    async def conversations_list(self, **kwargs):
        self._log("conversations_list", kwargs)
        return {"channels": type(self).channels, "response_metadata": {"next_cursor": ""}}

    async def conversations_history(self, **kwargs):
        self._log("conversations_history", kwargs)
        return {"messages": type(self).history}

    async def users_info(self, **kwargs):
        self._log("users_info", kwargs)
        return {"user": type(self).users.get(kwargs.get("user"), {})}

    async def users_list(self, **kwargs):
        self._log("users_list", kwargs)
        return {"members": type(self).members, "response_metadata": {"next_cursor": ""}}

    async def conversations_open(self, **kwargs):
        self._log("conversations_open", kwargs)
        return {"channel": {"id": "D-OPENED"}}

    async def chat_postMessage(self, **kwargs):
        self._log("chat_postMessage", kwargs)
        return {"ok": True, "ts": "1.0"}


@pytest.fixture
def fake_client(monkeypatch):
    FakeSlackClient.reset(
        channels=[
            {"id": "C-GENERAL", "name": "general"},
            {"id": "C-SHIP", "name": "ship-it"},
        ],
        history=[
            # Slack returns newest-first; the adapter flips to chronological.
            {"user": "U2", "text": "second message", "ts": "1783600200.000100"},
            {"user": "U1", "text": "first message", "ts": "1783600100.000100"},
        ],
        members=[
            {
                "id": "U1",
                "name": "ada",
                "profile": {"display_name": "Ada", "real_name": "Ada Lovelace"},
            },
            {
                "id": "U2",
                "name": "grace",
                "profile": {"display_name": "", "real_name": "Grace Hopper"},
            },
            {"id": "U9", "name": "ghost", "deleted": True, "profile": {"real_name": "Ghost"}},
        ],
        users={
            "U1": {"profile": {"display_name": "Ada", "real_name": "Ada Lovelace"}},
            "U2": {"profile": {"display_name": "", "real_name": "Grace Hopper"}},
        },
    )
    monkeypatch.setattr(slack, "AsyncWebClient", FakeSlackClient)
    return FakeSlackClient


def calls_named(name):
    return [kwargs for method, kwargs in FakeSlackClient.calls if method == name]


# ── token handling ────────────────────────────────────────────────────────


async def test_read_without_token(reg, fake_client, monkeypatch):
    monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
    res = await reg.get("slack.read").run({"channel": "general"})
    assert not res.ok
    assert res.summary == "Slack isn't wired — SLACK_BOT_TOKEN missing"
    assert res.data == {"setup": "slack"}
    assert FakeSlackClient.calls == []


async def test_send_without_token(reg, fake_client, monkeypatch):
    monkeypatch.setenv("SLACK_BOT_TOKEN", "   ")
    res = await reg.get("slack.send").run({"channel": "general", "text": "hi"})
    assert not res.ok
    assert res.data == {"setup": "slack"}


async def test_first_token_of_comma_list_is_used(reg, fake_client, token_env):
    await reg.get("slack.read").run({"channel": "general"})
    assert FakeSlackClient.tokens == ["xoxb-first"]


# ── channel resolution + cache ────────────────────────────────────────────


async def test_read_happy_path(reg, fake_client, token_env):
    res = await reg.get("slack.read").run({"channel": "#general"})
    assert res.ok
    assert res.summary == "read 2 from #general"
    # No canned say= — the model digests data.messages itself.
    assert res.say is None
    msgs = res.data["messages"]
    # Chronological (adapter flips Slack's newest-first), names resolved.
    assert [m["who"] for m in msgs] == ["Ada", "Grace Hopper"]
    assert [m["text"] for m in msgs] == ["first message", "second message"]
    for m in msgs:
        assert m["when"] == time.strftime(
            "%H:%M", time.localtime(1783600100.0001 if m["who"] == "Ada" else 1783600200.0001)
        )
    (history_call,) = calls_named("conversations_history")
    assert history_call == {"channel": "C-GENERAL", "limit": 8}
    (list_call,) = calls_named("conversations_list")
    assert list_call["types"] == "public_channel,private_channel"


async def test_read_count_clamped_to_20(reg, fake_client, token_env):
    await reg.get("slack.read").run({"channel": "general", "count": 500})
    (history_call,) = calls_named("conversations_history")
    assert history_call["limit"] == 20


async def test_read_truncates_long_text(reg, fake_client, token_env):
    FakeSlackClient.history = [{"user": "U1", "text": "x" * 900, "ts": "1783600100.0"}]
    res = await reg.get("slack.read").run({"channel": "general"})
    assert len(res.data["messages"][0]["text"]) == 300


async def test_channel_map_cached_for_five_minutes(reg, fake_client, token_env):
    await reg.get("slack.read").run({"channel": "general"})
    await reg.get("slack.read").run({"channel": "ship-it"})
    assert len(calls_named("conversations_list")) == 1  # cache hit
    # Expire the cache → refetched.
    slack._channel_cache["expires"] = 0.0
    await reg.get("slack.read").run({"channel": "general"})
    assert len(calls_named("conversations_list")) == 2


async def test_user_names_cached_across_reads(reg, fake_client, token_env):
    await reg.get("slack.read").run({"channel": "general"})
    await reg.get("slack.read").run({"channel": "general"})
    # Two authors, two users_info calls total — not two per read.
    assert len(calls_named("users_info")) == 2


async def test_read_unknown_channel(reg, fake_client, token_env):
    res = await reg.get("slack.read").run({"channel": "nope"})
    assert not res.ok
    assert res.summary == "no channel named #nope"
    assert calls_named("conversations_history") == []


async def test_read_api_error_is_terse(reg, fake_client, token_env):
    FakeSlackClient.fail_with = RuntimeError("boom\nlong traceback body")
    res = await reg.get("slack.read").run({"channel": "general"})
    assert not res.ok
    assert res.summary == "Slack read failed: boom"
    assert "\n" not in res.summary


# ── slack.send ────────────────────────────────────────────────────────────


def test_send_registered_confirm_tier(reg):
    verb = reg.get("slack.send")
    assert verb.tier == "confirm"
    assert verb.required == ["text"]
    assert reg.get("slack.read").tier == "auto"


async def test_send_to_channel(reg, fake_client, token_env):
    text = "shipping the reach verbs " + "x" * 60
    res = await reg.get("slack.send").run({"channel": "#general", "text": text})
    assert res.ok
    assert res.summary == f"→ #general: {text[:60]}"
    assert res.undo is None  # a sent message is sent
    (post,) = calls_named("chat_postMessage")
    assert post == {"channel": "C-GENERAL", "text": text}


async def test_send_dm_resolves_user_by_name(reg, fake_client, token_env):
    res = await reg.get("slack.send").run({"user": "grace hopper", "text": "lunch?"})
    assert res.ok
    assert res.summary == "→ @Grace Hopper: lunch?"
    (opened,) = calls_named("conversations_open")
    assert opened == {"users": ["U2"]}
    (post,) = calls_named("chat_postMessage")
    assert post == {"channel": "D-OPENED", "text": "lunch?"}


async def test_send_dm_prefers_exact_display_name(reg, fake_client, token_env):
    res = await reg.get("slack.send").run({"user": "Ada", "text": "hi"})
    assert res.ok
    (opened,) = calls_named("conversations_open")
    assert opened == {"users": ["U1"]}


async def test_send_dm_unknown_user(reg, fake_client, token_env):
    res = await reg.get("slack.send").run({"user": "Zaphod", "text": "hi"})
    assert not res.ok
    assert res.summary == "no Slack member matching Zaphod"
    assert calls_named("chat_postMessage") == []


async def test_send_dm_ambiguous_user_enumerates_instead_of_guessing(
    reg, fake_client, monkeypatch, token_env
):
    """Resolution happens AFTER the spoken confirmation, so a fuzzy guess
    here is how the wrong Alex gets an unrecallable DM. Two members
    matching 'al' must refuse with candidates, never pick one."""
    FakeSlackClient.reset(
        channels=[],
        history=[],
        members=[
            {"id": "U3", "name": "alex.c", "profile": {"display_name": "Alex Chen"}},
            {"id": "U4", "name": "alex.w", "profile": {"display_name": "Alex Wong"}},
        ],
        users={},
    )
    res = await reg.get("slack.send").run({"user": "alex", "text": "hi"})
    assert not res.ok
    assert res.data["candidates"] == ["Alex Chen", "Alex Wong"]
    assert "Alex Chen" in res.error and "Alex Wong" in res.error
    assert calls_named("conversations_open") == []
    assert calls_named("chat_postMessage") == []


async def test_send_dm_unique_fuzzy_match_still_resolves(reg, fake_client, token_env):
    # 'grac' prefixes exactly one member — unique fuzzy is safe to take.
    res = await reg.get("slack.send").run({"user": "grac", "text": "hi"})
    assert res.ok
    (opened,) = calls_named("conversations_open")
    assert opened == {"users": ["U2"]}


async def test_unknown_channel_hints_at_other_workspaces(reg, fake_client, token_env):
    # token_env sets TWO comma-separated tokens — the not-found error must
    # say voice only searches the first workspace.
    res = await reg.get("slack.read").run({"channel": "nope"})
    assert not res.ok
    assert "first Slack workspace" in res.error


async def test_send_requires_target_and_text(reg, fake_client, token_env):
    res = await reg.get("slack.send").run({"text": "hi"})
    assert not res.ok and res.error == "missing_target"
    res = await reg.get("slack.send").run({"channel": "general"})
    assert not res.ok and res.error == "missing_text"
    assert FakeSlackClient.calls == []


async def test_send_api_error_is_terse(reg, fake_client, token_env):
    class FakeSlackApiError(Exception):
        def __init__(self):
            super().__init__("The request to the Slack API failed.\n<Response 400>")
            self.response = {"error": "not_in_channel"}

    FakeSlackClient.fail_with = FakeSlackApiError()
    res = await reg.get("slack.send").run({"channel": "general", "text": "hi"})
    assert not res.ok
    assert res.summary == "Slack send failed: not_in_channel"
