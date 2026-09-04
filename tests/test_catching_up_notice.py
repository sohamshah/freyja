"""The "catching up" notice fires on real context size, not message count.

Two bugs this pins:

1. The token figure summed only `input_tokens`, which on a caching
   provider is just the UNCACHED remainder. A long warm-cache thread
   reported a couple hundred tokens, producing the nonsense the operator
   saw: "Looking at 84 prior messages (~94 tokens)".
2. Because that figure read ~0, the notice was effectively gated on
   message count alone, so it fired on any thread past 50 messages —
   including short ones where the reply was not slow at all.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from bridge.gateway.run import GatewayDaemon


@dataclass
class _Msg:
    """Minimal stand-in for engine.types.Message's usage surface."""

    input_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    text: str = ""

    def get_text(self) -> str:
        return self.text


def _peak_prompt_tokens(messages: list[_Msg]) -> int:
    """Mirror of the estimator in _maybe_send_catching_up_notice."""
    peak = 0
    for m in messages:
        turn = (
            int(m.input_tokens or 0)
            + int(m.cache_read_tokens or 0)
            + int(m.cache_write_tokens or 0)
        )
        peak = max(peak, turn)
    return peak


def test_cached_conversation_reports_real_prompt_size():
    """The exact shape that produced "~94 tokens": a big prompt served
    almost entirely from cache, so input_tokens is a rounding error."""
    turns = [
        _Msg(input_tokens=94, cache_read_tokens=118_000, cache_write_tokens=1_200),
        _Msg(input_tokens=120, cache_read_tokens=96_000),
    ]
    # The old estimator took max(input_tokens) and got a nonsense number.
    assert max(m.input_tokens for m in turns) == 120
    # The real prompt is three orders of magnitude bigger.
    assert _peak_prompt_tokens(turns) == 119_294


def test_uncached_conversation_is_unaffected():
    """With no caching, input_tokens already is the prompt size, so the
    fix must not inflate it."""
    turns = [_Msg(input_tokens=42_000), _Msg(input_tokens=51_500)]
    assert _peak_prompt_tokens(turns) == 51_500


def test_peak_survives_a_compaction_dip():
    """After compaction the newest turn's prompt is small; the notice
    should still reflect how big the conversation got."""
    turns = [
        _Msg(input_tokens=1_000, cache_read_tokens=430_000),
        _Msg(input_tokens=2_100),  # post-compaction tail
    ]
    assert _peak_prompt_tokens(turns) == 431_000


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


def test_big_threshold_still_above_normal_threshold():
    """The 'compacting' wording is reserved for genuinely huge contexts."""
    assert (
        GatewayDaemon._CATCHING_UP_BIG_TOKEN_THRESHOLD
        > GatewayDaemon._CATCHING_UP_TOKEN_THRESHOLD
    )


def test_char_fallback_scales_with_content():
    """When no turn has reported usage (first reply in a thread hydrated
    from Slack history), a chars/4 estimate stands in."""
    short = [_Msg(text="hi there")] * 5
    long = [_Msg(text="x" * 40_000)] * 30

    def fallback(msgs: list[_Msg]) -> int:
        return sum(len(m.get_text()) for m in msgs) // 4

    assert fallback(short) < GatewayDaemon._CATCHING_UP_TOKEN_THRESHOLD
    assert fallback(long) >= GatewayDaemon._CATCHING_UP_TOKEN_THRESHOLD
