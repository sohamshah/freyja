"""The "catching up" notice says something true about the reply ahead.

Three bugs this pins:

1. The token figure summed only `input_tokens`, which on a caching
   provider is just the UNCACHED remainder. A long warm-cache thread
   reported a couple hundred tokens, producing the nonsense the operator
   saw: "Looking at 84 prior messages (~94 tokens)".
2. Because that figure read ~0, the notice was effectively gated on
   message count alone, so it fired on any thread past 50 messages,
   including short ones where the reply was not slow at all.
3. The figure was then the PEAK prompt over the whole session, held
   against a fixed 500k. A peak never comes down, so a DM thread that had
   once reached 511k tokens said "Compacting the history before I reply"
   on every later turn (2026-10-26/27), while the engine started each
   reply within two seconds on a 258k to 369k prompt and never compacted.
   The word now comes from the runner's own outlook.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

import pytest

from bridge.gateway.run import GatewayDaemon, _latest_prompt_tokens
from engine.runner import AsyncAgentRunner, ContextOutlook
from engine.session import Session


@dataclass
class _Msg:
    """Minimal stand-in for engine.types.Message's usage surface."""

    input_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    text: str = ""

    def get_text(self) -> str:
        return self.text


def _entries(msgs: list[_Msg]) -> list[Any]:
    return [SimpleNamespace(message=m) for m in msgs]


# ── the fallback estimate (no runner) ──


def test_cached_conversation_reports_real_prompt_size():
    """The exact shape that produced "~94 tokens": a big prompt served
    almost entirely from cache, so input_tokens is a rounding error."""
    turns = [
        _Msg(input_tokens=94, cache_read_tokens=118_000, cache_write_tokens=1_200),
        _Msg(input_tokens=120, cache_read_tokens=96_000),
    ]
    assert max(m.input_tokens for m in turns) == 120
    assert _latest_prompt_tokens(_entries(turns)) == 96_120


def test_uncached_conversation_is_unaffected():
    turns = [_Msg(input_tokens=42_000), _Msg(input_tokens=51_500)]
    assert _latest_prompt_tokens(_entries(turns)) == 51_500


def test_the_latest_call_counts_not_the_peak():
    """The DM thread of 2026-10-26: one turn reached 511k, the next ones
    ran at 258k. The notice must follow the prompt as it is now."""
    turns = [
        _Msg(input_tokens=1_000, cache_read_tokens=510_094),
        _Msg(input_tokens=2_100, cache_read_tokens=256_000),
    ]
    assert _latest_prompt_tokens(_entries(turns)) == 258_100


def test_a_turn_without_usage_is_skipped():
    turns = [_Msg(input_tokens=40_000), _Msg(text="no usage yet")]
    assert _latest_prompt_tokens(_entries(turns)) == 40_000
    assert _latest_prompt_tokens(_entries([_Msg(text="x")])) == 0


@pytest.mark.parametrize(
    "tokens, should_fire",
    [
        (0, False),
        (12_000, False),
        (99_999, False),
        (100_000, True),
        (450_000, True),
    ],
)
def test_gate_is_token_only(tokens, should_fire):
    """Message count is no longer a trigger: a 500-message thread that is
    cheap in tokens stays quiet."""
    fires = tokens >= GatewayDaemon._CATCHING_UP_TOKEN_THRESHOLD
    assert fires is should_fire


def test_message_count_alone_never_fires():
    assert not hasattr(GatewayDaemon, "_CATCHING_UP_MSG_THRESHOLD"), (
        "message count was removed as a trigger; a lingering constant "
        "suggests the gate was partially reverted"
    )


def test_no_fixed_compaction_threshold_remains():
    """"Compacting" is the runner's call, not a number of ours."""
    assert not hasattr(GatewayDaemon, "_CATCHING_UP_BIG_TOKEN_THRESHOLD")


def test_char_fallback_scales_with_content():
    """When no turn has reported usage (first reply in a thread hydrated
    from Slack history), a chars/4 estimate stands in."""
    short = [_Msg(text="hi there")] * 5
    long = [_Msg(text="x" * 40_000)] * 30

    def fallback(msgs: list[_Msg]) -> int:
        return sum(len(m.get_text()) for m in msgs) // 4

    assert fallback(short) < GatewayDaemon._CATCHING_UP_TOKEN_THRESHOLD
    assert fallback(long) >= GatewayDaemon._CATCHING_UP_TOKEN_THRESHOLD


# ── the notice itself ──


class _Adapter:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send(self, chat_id, text, *, thread_id=None, raw_hint=None, **_kw):
        self.sent.append(text)
        return SimpleNamespace(ok=True)


def _message() -> Any:
    return SimpleNamespace(
        source=SimpleNamespace(chat_id="D1", thread_id="1.1", user_id="U1"), raw=None,
    )


def _session(msgs: list[_Msg], outlook: ContextOutlook | None = None) -> Any:
    inner = SimpleNamespace(transcript=SimpleNamespace(entries=_entries(msgs)))
    runner = None
    if outlook is not None:
        runner = SimpleNamespace(context_outlook=lambda _inner: outlook)
    return SimpleNamespace(session=inner, runner=runner)


async def _notice(session: Any) -> list[str]:
    daemon = GatewayDaemon.__new__(GatewayDaemon)
    adapter = _Adapter()
    await daemon._maybe_send_catching_up_notice(session, adapter, _message())
    return adapter.sent


_DM_TURNS = [_Msg(input_tokens=1_000, cache_read_tokens=510_094)] * 3 + [
    _Msg(input_tokens=2_000, cache_read_tokens=456_000),
]


@pytest.mark.asyncio
async def test_a_big_cold_prompt_is_catching_up_not_compacting():
    outlook = ContextOutlook(
        used_tokens=458_000, effective_window=950_000, will_compact=False, prompt_cached=False,
    )
    sent = await _notice(_session(_DM_TURNS, outlook))
    assert len(sent) == 1
    assert "4 prior messages" in sent[0] and "~458k tokens" in sent[0]
    assert "Catching up" in sent[0]
    assert "Compacting" not in sent[0]


@pytest.mark.asyncio
async def test_compacting_is_said_only_when_the_runner_will_compact():
    outlook = ContextOutlook(
        used_tokens=700_000, effective_window=950_000, will_compact=True, prompt_cached=False,
    )
    sent = await _notice(_session(_DM_TURNS, outlook))
    assert len(sent) == 1
    assert "Compacting the history" in sent[0] and "~700k tokens" in sent[0]


@pytest.mark.asyncio
async def test_a_cached_prompt_stays_quiet_whatever_its_size():
    outlook = ContextOutlook(
        used_tokens=458_000, effective_window=950_000, will_compact=False, prompt_cached=True,
    )
    assert await _notice(_session(_DM_TURNS, outlook)) == []


@pytest.mark.asyncio
async def test_a_small_prompt_stays_quiet():
    outlook = ContextOutlook(
        used_tokens=40_000, effective_window=950_000, will_compact=False, prompt_cached=False,
    )
    assert await _notice(_session(_DM_TURNS[:1], outlook)) == []


@pytest.mark.asyncio
async def test_without_a_runner_the_latest_call_stands_in_and_never_claims_compaction():
    sent = await _notice(_session(_DM_TURNS))
    assert len(sent) == 1
    assert "~458k tokens" in sent[0]
    assert "Compacting" not in sent[0]


@pytest.mark.asyncio
async def test_a_broken_outlook_falls_back():
    inner = SimpleNamespace(transcript=SimpleNamespace(entries=_entries(_DM_TURNS)))

    def boom(_inner):
        raise RuntimeError("no provider")

    session = SimpleNamespace(session=inner, runner=SimpleNamespace(context_outlook=boom))
    sent = await _notice(session)
    assert len(sent) == 1 and "~458k tokens" in sent[0]


# ── the runner's outlook ──


class _Provider:
    name = "anthropic"
    model_id = "claude-opus-5-5"
    context_window = 1_000_000
    prompt_cache_ttl_s = 300.0


def test_context_outlook_follows_the_runner_measure():
    runner = AsyncAgentRunner(_Provider())
    session = Session.create(system_prompt="t")
    effective = 1_000_000 - runner.config.max_tokens_per_turn
    # The DM thread of 2026-10-27: a 458k prompt in a 1M window.
    runner.usage.last_input = 2_000
    runner.usage.last_cache_read = 456_000
    out = runner.context_outlook(session)
    assert out.effective_window == effective
    assert out.used_tokens >= 458_000
    assert out.fraction == pytest.approx(out.used_tokens / effective)
    assert not out.will_compact
    assert not out.prompt_cached  # nothing sent yet, so nothing cached
    # Past 70% of the window left for input the runner compacts first.
    runner.usage.last_cache_read = int(effective * 0.71)
    assert runner.context_outlook(session).will_compact


def test_context_outlook_without_a_window():
    class _NoWindow(_Provider):
        context_window = 0

    runner = AsyncAgentRunner(_NoWindow())
    out = runner.context_outlook(Session.create(system_prompt="t"))
    assert out.effective_window == 0 and not out.will_compact and out.fraction == 0.0
