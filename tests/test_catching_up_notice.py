"""The "catching up" notice says something true about the reply ahead.

Three bugs this pins:

1. The token figure summed only `input_tokens`, which on a caching provider is just the UNCACHED remainder. A long warm-cache thread reported a couple hundred tokens, producing the nonsense the operator saw: "Looking at 84 prior messages (~94 tokens)".
2. Because that figure read ~0, the notice was effectively gated on message count alone, so it fired on any thread past 50 messages, including short ones where the reply was not slow at all.
3. The figure was then the PEAK prompt over the whole session, held against a fixed 500k. A peak never comes down, so a DM thread that had once reached 511k tokens said "Compacting the history before I reply" on every later turn (2026-10-26/27), while the engine started each reply within two seconds on a 258k to 369k prompt and never compacted. The word now comes from the runner's own outlook.
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


def test_latest_prompt_tokens_counts_the_cache_and_follows_the_latest_call():
    """The exact shape that produced "~94 tokens": a big prompt served almost entirely from cache, so input_tokens is a rounding error. And the DM thread of 2026-10-26: one turn reached 511k, the next ones ran at 258k; the notice must follow the prompt as it is now, not the peak."""
    cached = [
        _Msg(input_tokens=94, cache_read_tokens=118_000, cache_write_tokens=1_200),
        _Msg(input_tokens=120, cache_read_tokens=96_000),
    ]
    assert _latest_prompt_tokens(_entries(cached)) == 96_120
    uncached = [_Msg(input_tokens=42_000), _Msg(input_tokens=51_500)]
    assert _latest_prompt_tokens(_entries(uncached)) == 51_500
    peak_then_smaller = [
        _Msg(input_tokens=1_000, cache_read_tokens=510_094),
        _Msg(input_tokens=2_100, cache_read_tokens=256_000),
    ]
    assert _latest_prompt_tokens(_entries(peak_then_smaller)) == 258_100
    # A turn without usage yet is skipped; no usage at all reads as 0.
    assert _latest_prompt_tokens(_entries([_Msg(input_tokens=40_000), _Msg(text="no usage yet")])) == 40_000
    assert _latest_prompt_tokens(_entries([_Msg(text="x")])) == 0


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
async def test_a_cached_or_small_prompt_stays_quiet():
    """Message count is no longer a trigger: a 500-message thread that is cheap in tokens, or served from cache, stays quiet."""
    cached = ContextOutlook(
        used_tokens=458_000, effective_window=950_000, will_compact=False, prompt_cached=True,
    )
    assert await _notice(_session(_DM_TURNS, cached)) == []
    small = ContextOutlook(
        used_tokens=40_000, effective_window=950_000, will_compact=False, prompt_cached=False,
    )
    assert await _notice(_session(_DM_TURNS[:1], small)) == []


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
    # The measure stands for the transcript as it is, as after a compaction
    # between turns. A transcript cut since is measured afresh.
    runner.mark_context_compacted(session, 0)
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

    class _NoWindow(_Provider):
        context_window = 0

    # A provider without a window reports nothing rather than dividing by zero.
    out = AsyncAgentRunner(_NoWindow()).context_outlook(Session.create(system_prompt="t"))
    assert out.effective_window == 0 and not out.will_compact and out.fraction == 0.0
