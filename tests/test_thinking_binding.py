"""Fable 5.2 binds each thinking block to the exact history before it, on
every account. Replaying a block after an edit (compaction, halving, a new
system prompt) is a 400 that resending cannot clear: probed 2026-11-17, an
edited system prompt gave "The block is bound to a different conversation".
Freyja edits history on purpose, so Fable 5.2 requests ask the API to drop
stale blocks instead. Older models must not get the field: on this account
it would opt them into the same check."""
from __future__ import annotations

import pytest

from engine.anthropic_provider import (
    SERVER_FALLBACK_BETA,
    THINKING_BINDING_BETA,
    AnthropicConfig,
    AnthropicProvider,
)
from engine.types import Message, ThinkingConfig


def _request(model: str, thinking: ThinkingConfig) -> dict:
    provider = AnthropicProvider(AnthropicConfig(api_key="test-key", model=model))
    return provider._build_request(
        messages=[Message(role="user", content="hi")], thinking=thinking,
    )


@pytest.mark.parametrize(
    "thinking",
    [ThinkingConfig(enabled=True, effort="high"), ThinkingConfig(enabled=False, effort="none")],
    ids=["thinking-on", "thinking-off"],
)
def test_fable_5_2_asks_to_drop_stale_thinking_blocks(thinking):
    sent = _request("claude-fable-5-2", thinking)
    assert sent["thinking"]["type"] == "adaptive"
    assert sent["thinking"]["block_binding"] == {"prefix_mismatch_behavior": "drop_block"}
    betas = sent["extra_headers"]["anthropic-beta"].split(",")
    assert THINKING_BINDING_BETA in betas
    # The refusal fallback still rides along (200 with both, probed): its
    # body field and its beta must both survive the header merge.
    assert sent["extra_body"]["fallbacks"] == [{"model": "claude-opus-4-8"}]
    assert SERVER_FALLBACK_BETA in betas


@pytest.mark.parametrize("model", ["claude-opus-5-5", "claude-fable-5-1", "claude-haiku-5-5"])
def test_models_checked_only_on_new_accounts_do_not_opt_in(model):
    sent = _request(model, ThinkingConfig(enabled=True, effort="high"))
    assert "block_binding" not in (sent.get("thinking") or {})
    headers = sent.get("extra_headers") or {}
    assert THINKING_BINDING_BETA not in headers.get("anthropic-beta", "")
