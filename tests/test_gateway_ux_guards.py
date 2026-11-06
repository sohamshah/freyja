"""Gateway guards added after the 2026-10-05 silent-thread incident."""

from __future__ import annotations

import asyncio
import socket
from types import SimpleNamespace

import pytest

from bridge.gateway import link_check
from bridge.gateway.run import GatewayDaemon

# ── link_check ────────────────────────────────────────────────────────

def test_extract_hosts_handles_slack_and_bare_urls():
    text = (
        "see <https://demo.signoz.ema.co/alerts/edit?ruleId=1|rule> and "
        "https://github.com/Ema-Unlimited/agent-harness/pull/1178, then "
        "https://github.com/x again."
    )
    assert link_check.extract_hosts(text) == [
        "demo.signoz.ema.co",
        "github.com",
    ]


def _patch_resolver(monkeypatch, fake) -> None:
    link_check._cache.clear()
    monkeypatch.setattr(asyncio.get_running_loop().__class__, "getaddrinfo", fake, raising=False)


@pytest.mark.asyncio
async def test_unresolved_host_is_flagged_and_real_one_is_not(monkeypatch):
    async def fake_getaddrinfo(self, host, *a, **k):
        if host == "demo.signoz.ema.co":
            raise socket.gaierror(8, "nodename nor servname provided")
        return [("ok",)]

    _patch_resolver(monkeypatch, fake_getaddrinfo)
    out = await link_check.annotate_unresolved(
        "https://demo.signoz.ema.co/x and https://github.com/y"
    )
    assert "demo.signoz.ema.co" in out.split("Unverified link")[1]
    assert "github.com" not in out.split("Unverified link")[1]


@pytest.mark.asyncio
async def test_resolver_timeout_is_not_treated_as_fake(monkeypatch):
    """A slow resolver (the Mac off the VPN) is not evidence the host is fake. wait_for cancels the lookup at the deadline, so the long sleep never holds the test."""
    monkeypatch.setattr(link_check, "_LOOKUP_TIMEOUT_SEC", 0.05)

    async def slow(self, host, *a, **k):
        await asyncio.sleep(1)

    _patch_resolver(monkeypatch, slow)
    text = "https://slow.example/x"
    assert await link_check.annotate_unresolved(text) == text


# ── redelivery ────────────────────────────────────────────────────────

def _msg(mid="1.1", files=False, slash=False):
    return SimpleNamespace(
        source=SimpleNamespace(
            platform=SimpleNamespace(value="slack"),
            chat_id="C1",
            thread_id="T1",
            message_id=mid,
        ),
        attachments=[{"name": "a"}] if files else [],
        is_slash_command=slash,
    )


def test_redelivery_drops_only_a_repeated_message_id():
    d = GatewayDaemon()
    assert d._is_redelivery(_msg("1.1")) is False
    assert d._is_redelivery(_msg("1.1")) is True
    assert d._is_redelivery(_msg("1.2")) is False
    # app_mention (no files) and its richer message.channels twin share a ts.
    assert d._is_redelivery(_msg("1.3", files=False)) is False
    assert d._is_redelivery(_msg("1.3", files=True)) is False
    # Messages without an id and slash commands are never dropped.
    assert d._is_redelivery(_msg(mid=None)) is False
    assert d._is_redelivery(_msg(mid=None)) is False
    assert d._is_redelivery(_msg("2.1", slash=True)) is False
    assert d._is_redelivery(_msg("2.1", slash=True)) is False


# ── denied-user notice ────────────────────────────────────────────────

class _Client:
    def __init__(self):
        self.calls = []

    async def chat_postEphemeral(self, **kw):
        self.calls.append(kw)


def _adapter(client):
    from bridge.gateway.platforms.slack import SlackAdapter

    a = SlackAdapter.__new__(SlackAdapter)
    a._team_bot_user_ids = {"T": "UBOT"}
    a._bot_user_id = "UBOT"
    a._denied_notice_at = {}
    a._get_client = lambda chat_id, team_id=None: client
    return a


@pytest.mark.asyncio
async def test_denied_user_hears_back_once_on_a_mention_or_dm_and_never_on_chatter():
    c = _Client()
    a = _adapter(c)
    await a._tell_denied({"channel_type": "channel"}, "C1", "T", "U9", "lunch?")
    assert c.calls == []
    await a._tell_denied({"channel_type": "channel"}, "C1", "T", "U9", "hey <@UBOT> help")
    await a._tell_denied({"channel_type": "channel"}, "C1", "T", "U9", "<@UBOT> again")
    assert len(c.calls) == 1
    assert c.calls[0]["user"] == "U9"

    c = _Client()
    a = _adapter(c)
    await a._tell_denied({"channel_type": "im"}, "D1", "T", "U9", "hi")
    assert len(c.calls) == 1
