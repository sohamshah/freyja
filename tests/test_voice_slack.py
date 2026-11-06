"""Slack reach verbs (bridge/voice/adapters/slack.py).

Nothing here touches real Slack — ``AsyncWebClient`` is monkeypatched on the adapter module with a canned fake, and the module-level caches are reset around every test.
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
    """Duck-types the slice of AsyncWebClient the adapter uses. Class attributes hold the shared canned world + call log so the adapter's own instantiations (`AsyncWebClient(token=...)`) all hit it."""

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


async def test_read_or_send_without_token_is_a_setup_refusal(reg, fake_client, monkeypatch, tmp_path):
    monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
    # Point the ~/.freyja/.env fallback at an empty home too — the real
    # machine's wizard-written token must not leak into the suite.
    monkeypatch.setenv("FREYJA_HOME", str(tmp_path / "fh"))
    res = await reg.get("slack.read").run({"channel": "general"})
    assert not res.ok
    assert res.summary == (
        "Slack isn't wired — run `freyja setup slack` or set SLACK_BOT_TOKEN"
    )
    assert res.data == {"setup": "slack"}

    monkeypatch.setenv("SLACK_BOT_TOKEN", "   ")
    res = await reg.get("slack.send").run({"channel": "general", "text": "hi"})
    assert not res.ok
    assert res.data == {"setup": "slack"}
    assert FakeSlackClient.calls == []


async def test_token_falls_back_to_freyja_home_env(reg, fake_client, monkeypatch, tmp_path):
    """The setup wizard writes SLACK_BOT_TOKEN to ~/.freyja/.env, which the gateway daemon loads but the desktop bridge does not — the adapter must read it there itself (live gap: 'Slack isn't wired' with a connected gateway)."""
    monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
    fh = tmp_path / "fh"
    fh.mkdir()
    (fh / ".env").write_text("SLACK_BOT_TOKEN=xoxb-from-wizard, xoxb-second\n")
    monkeypatch.setenv("FREYJA_HOME", str(fh))
    res = await reg.get("slack.read").run({"channel": "general"})
    assert res.ok, res.summary
    # And the comma rule still applies to the fallback source.
    assert FakeSlackClient.tokens[0] == "xoxb-from-wizard"


# ── slack.read ────────────────────────────────────────────────────────────


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
    # Only the first of the comma-separated tokens is used.
    assert FakeSlackClient.tokens == ["xoxb-first"]


async def test_read_clamps_count_and_truncates_text(reg, fake_client, token_env):
    FakeSlackClient.history = [{"user": "U1", "text": "x" * 900, "ts": "1783600100.0"}]
    res = await reg.get("slack.read").run({"channel": "general", "count": 500})
    (history_call,) = calls_named("conversations_history")
    assert history_call["limit"] == 20
    assert len(res.data["messages"][0]["text"]) == 300


async def test_read_caches_channels_users_and_history(reg, fake_client, token_env):
    """Slack's non-Marketplace history quota is 1 req/min, so a second read of the same channel inside the TTL must not spend another conversations.history call; the channel map and user names are not refetched per read either."""
    res1 = await reg.get("slack.read").run({"channel": "general"})
    res2 = await reg.get("slack.read").run({"channel": "general"})
    assert res1.ok and res2.ok
    assert res2.data["messages"] == res1.data["messages"]
    assert len(calls_named("conversations_history")) == 1
    await reg.get("slack.read").run({"channel": "ship-it"})
    assert len(calls_named("conversations_list")) == 1  # channel map cached
    assert len(calls_named("users_info")) == 2  # two authors, resolved once each
    # Expire the channel cache → refetched.
    slack._channel_cache["expires"] = 0.0
    await reg.get("slack.read").run({"channel": "general"})
    assert len(calls_named("conversations_list")) == 2


async def test_read_unknown_channel_hints_at_other_workspaces(reg, fake_client, token_env):
    # token_env sets TWO comma-separated tokens — the not-found error must
    # say voice only searches the first workspace.
    res = await reg.get("slack.read").run({"channel": "nope"})
    assert not res.ok
    assert res.summary == "no channel named #nope"
    assert "first Slack workspace" in res.error
    assert calls_named("conversations_history") == []


async def test_api_errors_are_terse(reg, fake_client, token_env):
    FakeSlackClient.fail_with = RuntimeError("boom\nlong traceback body")
    res = await reg.get("slack.read").run({"channel": "general"})
    assert not res.ok
    assert res.summary == "Slack read failed: boom"
    assert "\n" not in res.summary

    class FakeSlackApiError(Exception):
        def __init__(self):
            super().__init__("The request to the Slack API failed.\n<Response 400>")
            self.response = {"error": "not_in_channel"}

    FakeSlackClient.fail_with = FakeSlackApiError()
    res = await reg.get("slack.send").run({"channel": "general", "text": "hi"})
    assert not res.ok
    assert res.summary == "Slack send failed: not_in_channel"


# ── slack.send ────────────────────────────────────────────────────────────


async def test_send_to_channel_is_confirm_tier_and_not_undoable(reg, fake_client, token_env):
    verb = reg.get("slack.send")
    assert verb.tier == "confirm" and verb.required == ["text"]
    assert reg.get("slack.read").tier == "auto"
    text = "shipping the reach verbs " + "x" * 60
    res = await verb.run({"channel": "#general", "text": text})
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

    # An exact display name wins; 'grac' prefixes exactly one member, so a
    # unique fuzzy match is safe to take.
    await reg.get("slack.send").run({"user": "Ada", "text": "hi"})
    await reg.get("slack.send").run({"user": "grac", "text": "hi"})
    opened = [c["users"] for c in calls_named("conversations_open")]
    assert opened == [["U2"], ["U1"], ["U2"]]


async def test_send_dm_unknown_user_sends_nothing(reg, fake_client, token_env):
    res = await reg.get("slack.send").run({"user": "Zaphod", "text": "hi"})
    assert not res.ok
    assert res.summary == "no Slack member matching Zaphod"
    assert calls_named("chat_postMessage") == []


async def test_send_dm_ambiguous_user_enumerates_instead_of_guessing(reg, fake_client, token_env):
    """Resolution happens AFTER the spoken confirmation, so a fuzzy guess here is how the wrong Alex gets an unrecallable DM. Two members matching 'al' must refuse with candidates, never pick one."""
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


# ── rate limiting (Slack's non-Marketplace 1-req/min history quota) ───────


class _FakeRateLimitError(Exception):
    """Duck-types SlackApiError: .response has .get('error') + .headers."""

    def __init__(self, retry_after):
        super().__init__("ratelimited")

        class _Resp(dict):
            pass

        self.response = _Resp(error="ratelimited")
        self.response.headers = {"Retry-After": str(retry_after)}


async def test_read_ratelimited_long_wait_is_graceful(reg, fake_client, token_env):
    """A 60s Retry-After (the 1/min history quota) must come back as a spoken-truth refusal with the wait, never a bare 'ratelimited'."""
    FakeSlackClient.fail_with = _FakeRateLimitError(60)
    res = await reg.get("slack.read").run({"channel": "general"})
    assert not res.ok
    assert res.summary == "Slack is rate-limiting channel reads — again in ~60s"
    assert res.data["retryAfterSec"] == 60
    assert "try again" in res.error


async def test_read_ratelimited_short_wait_retries_inline(reg, fake_client, token_env, monkeypatch):
    """Retry-After ≤3s is a burst limit — one inline retry should recover without surfacing anything to the operator."""
    naps = []

    async def fake_sleep(s):
        naps.append(s)

    monkeypatch.setattr(slack.asyncio, "sleep", fake_sleep)
    state = {"failed": False}

    def flaky_log(self, method, kwargs):
        type(self).calls.append((method, kwargs))
        if method == "conversations_history" and not state["failed"]:
            state["failed"] = True
            raise _FakeRateLimitError(2)

    monkeypatch.setattr(FakeSlackClient, "_log", flaky_log)
    res = await reg.get("slack.read").run({"channel": "general"})
    assert res.ok, res.summary
    assert naps == [2]
    assert len(calls_named("conversations_history")) == 2


async def test_channel_resolution_stops_at_first_matching_page(reg, fake_client, token_env, monkeypatch):
    """Pagination must early-exit when the wanted channel is on the page — a five-page cold burst is what 429s big workspaces."""
    pages = [
        {"channels": [{"id": "C-GENERAL", "name": "general"}],
         "response_metadata": {"next_cursor": "page2"}},
        {"channels": [{"id": "C-OTHER", "name": "other"}],
         "response_metadata": {"next_cursor": "page3"}},
    ]
    state = {"i": 0}

    async def paged_list(self, **kwargs):
        self._log("conversations_list", kwargs)
        page = pages[min(state["i"], len(pages) - 1)]
        state["i"] += 1
        return page

    monkeypatch.setattr(FakeSlackClient, "conversations_list", paged_list)
    res = await reg.get("slack.read").run({"channel": "general"})
    assert res.ok, res.summary
    assert len(calls_named("conversations_list")) == 1  # stopped at page 1
