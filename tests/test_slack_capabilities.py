"""Slack capability card: presence-only, identity-cached, never leaks tokens.

Every test runs with FREYJA_HOME pointed at a tmp dir, all SLACK_* vars scrubbed from the real environment, and ``urllib.request.urlopen`` replaced with a function that raises, so nothing here can read the operator's ~/.freyja/.env or touch the network.
"""

from __future__ import annotations

import io
import json
import logging
import urllib.request
from types import SimpleNamespace

import pytest

from bridge.gateway import slack_capabilities as sc
from bridge.gateway.platforms.base import MessageSource, Platform
from bridge.gateway.session_router import gateway_source_block

BOT_VALUE = "xoxb-SECRETBOT-111-222-abcdef"
USER_VALUE = "xoxp-SECRETUSER-333-444-ghijkl"
APP_VALUE = "xapp-SECRETAPP-555-mnopqr"
# Distinctive substrings of the values; none may appear in any output.
LEAK_MARKERS = (BOT_VALUE, USER_VALUE, APP_VALUE, "SECRETBOT", "SECRETUSER", "SECRETAPP")

BOT_PAYLOAD = {
    "ok": True, "user": "freyja", "user_id": "U0BOT", "team": "Acme",
    "team_id": "T0ACME", "bot_id": "B0BOT",
}
USER_PAYLOAD = {
    "ok": True, "user": "soham", "user_id": "U0SOHAM", "team": "Acme",
    "team_id": "T0ACME",
}


def _no_network(*_a, **_k):  # pragma: no cover - only fires on a bug
    raise AssertionError("network access attempted in tests")


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    monkeypatch.setenv("FREYJA_HOME", str(tmp_path))
    for var in (sc.SLACK_BOT_TOKEN, sc.SLACK_USER_TOKEN, sc.SLACK_APP_TOKEN):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(urllib.request, "urlopen", _no_network)
    sc.reset_identity_cache()
    yield
    sc.reset_identity_cache()


def _stub_auth(monkeypatch, *, fail: set[str] | None = None):
    """Replace ``_auth_test`` with a stub that records call count and never returns anything derived from the token value."""
    fail = fail or set()
    calls: list[str] = []

    def fake(token: str) -> dict:
        calls.append(token)
        if token in fail:
            raise OSError("simulated transport failure")
        if token.startswith("xoxb-"):
            return dict(BOT_PAYLOAD)
        if token.startswith("xoxp-"):
            return dict(USER_PAYLOAD)
        return {"ok": False, "error": "invalid_auth"}

    monkeypatch.setattr(sc, "_auth_test", fake)
    return calls


def _mcp_json(tmp_path, *, enabled: bool):
    path = tmp_path / "mcp.json"
    path.write_text(json.dumps({
        "version": 1,
        "servers": {
            "slack": {"transport": "http", "url": "https://mcp.slack.com/mcp", "enabled": enabled},
        },
    }))
    return path


# ---------------------------------------------------------------------------
# env-presence permutations
# ---------------------------------------------------------------------------

def test_both_tokens_full_card(monkeypatch, tmp_path):
    _stub_auth(monkeypatch)
    env = {
        sc.SLACK_BOT_TOKEN: BOT_VALUE,
        sc.SLACK_USER_TOKEN: USER_VALUE,
        sc.SLACK_APP_TOKEN: APP_VALUE,
    }
    block = sc.slack_capability_block(env, mcp_path=tmp_path / "missing-mcp.json")

    assert (
        "SLACK_BOT_TOKEN — present; acts as bot @freyja (U0BOT, bot B0BOT) in workspace Acme"
    ) in block
    assert "sees only channels it has joined" in block
    assert "SLACK_USER_TOKEN — present; acts as @soham (U0SOHAM) in workspace Acme" in block
    assert "private channels, DMs, standalone canvases, workspace search" in block
    assert "SLACK_APP_TOKEN — present; Socket Mode plumbing only" in block
    # read/search guidance names the user-token endpoints
    for ep in ("search.messages", "search.files", "assistant.search.context", "files.list",
               "conversations.history", "conversations.replies", "files.info", "url_private"):
        assert ep in block
    assert "appears as the operator personally — confirm with the user" in block
    assert "attributed to Freyja" in block
    assert "set -a; source ~/.freyja/.env; set +a" in block
    assert 'Bearer $SLACK_USER_TOKEN' in block
    assert "do NOT grep or cat" in block
    # no MCP line when there's no mcp.json
    assert "MCP" not in block
    # concise: header + ~10-14 lines
    assert 8 <= len(block.splitlines()) <= 14


def test_bot_only_explains_missing_search(monkeypatch, tmp_path):
    _stub_auth(monkeypatch)
    env = {sc.SLACK_BOT_TOKEN: BOT_VALUE}
    block = sc.slack_capability_block(env, mcp_path=tmp_path / "missing-mcp.json")

    assert "SLACK_BOT_TOKEN — present; acts as bot @freyja" in block
    assert "SLACK_USER_TOKEN — not set" in block
    assert "UNAVAILABLE" in block
    assert "bot token cannot call search.*" in block
    assert "xoxp-" in block  # tells the operator how to enable it
    assert "SLACK_APP_TOKEN" not in block
    assert 'Bearer $SLACK_BOT_TOKEN' in block
    # user-token-only guidance must not be offered
    assert "READ / SEARCH with the user token" not in block
    assert "appears as the operator personally" not in block


def test_no_or_blank_tokens_make_a_short_card_without_resolving(monkeypatch, tmp_path):
    calls = _stub_auth(monkeypatch)
    for env in ({}, {sc.SLACK_BOT_TOKEN: "   ", sc.SLACK_USER_TOKEN: ""}):
        block = sc.slack_capability_block(env, mcp_path=tmp_path / "missing-mcp.json")
        assert "No SLACK_BOT_TOKEN or SLACK_USER_TOKEN is set" in block
        assert "Slack Web API calls are unavailable" in block
        assert len(block.splitlines()) <= 3
    assert calls == []  # nothing to resolve
    block = sc.slack_capability_block({sc.SLACK_APP_TOKEN: APP_VALUE}, mcp_path=tmp_path / "x.json")
    assert "SLACK_APP_TOKEN is set (Socket Mode plumbing only" in block


# ---------------------------------------------------------------------------
# no token value leak
# ---------------------------------------------------------------------------

def test_no_token_value_leaks_anywhere(monkeypatch, tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    # Fail the user token so the failure-path log line is exercised too.
    calls = _stub_auth(monkeypatch, fail={USER_VALUE})
    env = {
        sc.SLACK_BOT_TOKEN: BOT_VALUE,
        sc.SLACK_USER_TOKEN: USER_VALUE,
        sc.SLACK_APP_TOKEN: APP_VALUE,
    }
    block = sc.slack_capability_block(env, mcp_path=_mcp_json(tmp_path, enabled=False))

    # The resolver really did receive the values (so the check is meaningful)…
    assert set(calls) == {BOT_VALUE, USER_VALUE}
    # …but none of them, nor any distinctive fragment, appears in output, in the cache, or in any log record.
    for marker in LEAK_MARKERS:
        assert marker not in block
        assert marker not in repr(sc._identity_cache)
        assert marker not in repr(sc.resolve_slack_identities(env))
        for rec in caplog.records:
            assert marker not in rec.getMessage()
            assert marker not in repr(rec.args)


# ---------------------------------------------------------------------------
# identity resolution: stubbed urllib, failure path, caching
# ---------------------------------------------------------------------------

class _FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


def test_auth_test_posts_a_bearer_token_and_rejects_a_non_object_body(monkeypatch):
    seen: dict = {}

    def fake_urlopen(req, timeout=None):
        seen["url"] = req.full_url
        seen["method"] = req.get_method()
        seen["auth"] = req.get_header("Authorization")
        seen["timeout"] = timeout
        return _FakeResponse(json.dumps(BOT_PAYLOAD).encode())

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    payload = sc._auth_test(BOT_VALUE)
    assert payload["user"] == "freyja"
    assert seen == {
        "url": sc.AUTH_TEST_URL,
        "method": "POST",
        "auth": f"Bearer {BOT_VALUE}",
        "timeout": 5.0,
    }

    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: _FakeResponse(b"[1, 2]"))
    with pytest.raises(ValueError):
        sc._auth_test(BOT_VALUE)


def test_resolve_shapes_identities_and_uses_the_first_of_comma_split_tokens(monkeypatch):
    calls = _stub_auth(monkeypatch)
    ids = sc.resolve_slack_identities({
        sc.SLACK_BOT_TOKEN: f"{BOT_VALUE}, xoxb-second-workspace",
        sc.SLACK_USER_TOKEN: USER_VALUE,
    })
    assert set(calls) == {BOT_VALUE, USER_VALUE}
    assert set(ids) == {sc.SLACK_BOT_TOKEN, sc.SLACK_USER_TOKEN}
    bot, user = ids[sc.SLACK_BOT_TOKEN], ids[sc.SLACK_USER_TOKEN]
    assert bot.is_bot and bot.user == "freyja" and bot.user_id == "U0BOT" and bot.bot_id == "B0BOT"
    assert not user.is_bot and user.user == "soham" and user.user_id == "U0SOHAM"
    assert user.team == "Acme" and user.team_id == "T0ACME"


def test_failed_resolution_degrades_the_wording_is_tried_once_and_never_raises(monkeypatch, tmp_path):
    calls = _stub_auth(monkeypatch, fail={BOT_VALUE, USER_VALUE})
    env = {sc.SLACK_BOT_TOKEN: BOT_VALUE, sc.SLACK_USER_TOKEN: USER_VALUE}
    ids = sc.resolve_slack_identities(env)
    assert ids == {sc.SLACK_BOT_TOKEN: None, sc.SLACK_USER_TOKEN: None}

    block = sc.slack_capability_block(env, mcp_path=tmp_path / "x.json")
    # Still lists which tokens exist, with degraded identity wording.
    assert "SLACK_BOT_TOKEN — present; acts as the bot user" in block
    assert "SLACK_USER_TOKEN — present; acts as the operator" in block
    assert "@" not in block.split("SLACK_USER_TOKEN — present")[1].split("\n")[0]
    # Failure is cached: one attempt per token per process.
    assert sorted(calls) == sorted([BOT_VALUE, USER_VALUE])

    # An ok:false answer degrades the same way.
    sc.reset_identity_cache()
    monkeypatch.setattr(sc, "_auth_test", lambda token: {"ok": False, "error": "invalid_auth"})
    assert sc.resolve_slack_identities({sc.SLACK_USER_TOKEN: USER_VALUE}) == {sc.SLACK_USER_TOKEN: None}

    # An unexpected exception in the resolver never reaches the caller.
    sc.reset_identity_cache()

    def boom(token):
        raise RuntimeError("unexpected")

    monkeypatch.setattr(sc, "_auth_test", boom)
    block = sc.slack_capability_block({sc.SLACK_BOT_TOKEN: BOT_VALUE}, mcp_path=tmp_path / "x.json")
    assert "SLACK_BOT_TOKEN — present" in block


def test_identity_is_cached_per_var_across_block_builds(monkeypatch, tmp_path):
    calls = _stub_auth(monkeypatch)
    sc.resolve_slack_identities({sc.SLACK_BOT_TOKEN: BOT_VALUE})
    env = {sc.SLACK_BOT_TOKEN: BOT_VALUE, sc.SLACK_USER_TOKEN: USER_VALUE}
    first = sc.slack_capability_block(env, mcp_path=tmp_path / "x.json")
    second = sc.slack_capability_block(env, mcp_path=tmp_path / "x.json")
    assert first == second
    # Exactly one auth.test per present token, not per build; the cache is per var, so the user token still resolved once.
    assert calls == [BOT_VALUE, USER_VALUE]

    sc.reset_identity_cache()
    sc.slack_capability_block(env, mcp_path=tmp_path / "x.json")
    assert len(calls) == 4


# ---------------------------------------------------------------------------
# MCP note
# ---------------------------------------------------------------------------

def test_mcp_note_appears_only_for_a_disabled_slack_server(monkeypatch, tmp_path):
    _stub_auth(monkeypatch)
    env = {sc.SLACK_BOT_TOKEN: BOT_VALUE}
    disabled = sc.slack_capability_block(env, mcp_path=_mcp_json(tmp_path, enabled=False))
    assert disabled.splitlines()[-1] == (
        "The Slack MCP server in mcp.json is disabled (pending OAuth support); "
        "use the Web API path above."
    )
    assert "MCP" not in sc.slack_capability_block(env, mcp_path=_mcp_json(tmp_path, enabled=True))
    path = tmp_path / "mcp.json"
    path.write_text("{not json")
    broken = sc.slack_capability_block(env, mcp_path=path)
    assert "SLACK_BOT_TOKEN — present" in broken
    assert "MCP" not in broken


def test_active_mcp_slack_tools_are_listed_instead_of_the_note(monkeypatch, tmp_path):
    _stub_auth(monkeypatch)
    env = {sc.SLACK_BOT_TOKEN: BOT_VALUE}
    path = _mcp_json(tmp_path, enabled=False)
    block = sc.slack_capability_block(
        env, mcp_path=path,
        tool_snapshot=lambda: ["bash", "mcp__slack__search_messages", "mcp__github__x", "mcp__slack__read"],
    )
    assert "Active Slack MCP tools: `mcp__slack__read`, `mcp__slack__search_messages`" in block
    assert "mcp__github__x" not in block
    assert sc._DISABLED_MCP_LINE not in block
    # A snapshot without Slack tools falls back to the catalog note.
    block = sc.slack_capability_block(env, mcp_path=path, tool_snapshot=lambda: ["bash", "mcp__github__x"])
    assert sc._DISABLED_MCP_LINE in block


# ---------------------------------------------------------------------------
# platform gating in gateway_source_block
# ---------------------------------------------------------------------------

def _source(platform):
    return MessageSource(
        platform=platform, workspace_id="T0ACME", chat_type="dm",
        chat_id="D0DM", user_id="U0SOHAM", user_name="soham",
    )


def test_gateway_source_block_appends_card_for_slack(monkeypatch, tmp_path):
    """Also covers the defaults: the card reads os.environ and the mcp.json under FREYJA_HOME when nothing is passed."""
    _stub_auth(monkeypatch)
    monkeypatch.setenv(sc.SLACK_BOT_TOKEN, BOT_VALUE)
    monkeypatch.setenv(sc.SLACK_USER_TOKEN, USER_VALUE)
    _mcp_json(tmp_path, enabled=False)

    out = gateway_source_block(_source(Platform.SLACK))
    expected = sc.slack_capability_block()
    assert out.endswith("\n" + expected)
    assert "SLACK ACCESS" in out
    assert "acts as @soham (U0SOHAM)" in out
    assert sc._DISABLED_MCP_LINE in out
    for marker in LEAK_MARKERS:
        assert marker not in out
    # The pre-existing gateway context is still there ahead of the card.
    assert out.startswith("You are responding on the **Slack** gateway")
    assert "This is a private DM with soham." in out


def test_gateway_source_block_unchanged_for_non_slack(monkeypatch):
    _stub_auth(monkeypatch)
    monkeypatch.setenv(sc.SLACK_BOT_TOKEN, BOT_VALUE)
    monkeypatch.setenv(sc.SLACK_USER_TOKEN, USER_VALUE)

    called = []
    monkeypatch.setattr(sc, "slack_capability_block", lambda *a, **k: called.append(1) or "CARD")

    fake_platform = SimpleNamespace(value="telegram")
    out = gateway_source_block(_source(fake_platform))
    assert called == []
    assert "CARD" not in out
    assert "SLACK ACCESS" not in out
    assert "SLACK_BOT_TOKEN" not in out
    assert out.startswith("You are responding on the **Telegram** gateway")


def test_gateway_source_block_survives_card_failure(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("card exploded")

    monkeypatch.setattr(sc, "slack_capability_block", boom)
    out = gateway_source_block(_source(Platform.SLACK))
    assert out.startswith("You are responding on the **Slack** gateway")
    assert "SLACK ACCESS" not in out
