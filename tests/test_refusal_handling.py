"""Regression tests for Fable-class refusal handling (session-mr9tf56x).

Fable 5's safety classifiers can decline a request with HTTP 200 +
``stop_reason: "refusal"`` — including mid-stream, cutting the text off
mid-word. Before the fix the engine chained four defects:

1. ``StopCondition.should_stop`` didn't recognize ``refusal``, so the
   loop continued and re-called the provider with the transcript ending
   in the just-committed assistant partial — an assistant prefill,
   rejected by Fable 5 with 400 "does not support assistant message
   prefill".
2. The refused partial/empty assistant content was committed to the
   transcript unconditionally, so later turns carried empty assistant
   entries.
3. ``_ensure_alternating`` merged two consecutive empty assistant
   strings with "\\n\\n" — a whitespace-only text block the API rejects
   with 400 "text content blocks must contain non-whitespace text",
   permanently poisoning the session.
4. The SDK's stream accumulator drops ``stop_details`` (anthropic 0.94,
   lib/streaming/_messages.py), so the refusal category/explanation
   never reached the logs or UI.

These tests pin the fixes plus the server-side refusal-fallback opt-in
(``fallbacks`` + ``server-side-fallback-2026-06-01`` beta) for
Fable-class models.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from engine import anthropic_provider
from engine.anthropic_provider import (
    AnthropicConfig,
    AnthropicProvider,
    REFUSAL_FALLBACK_TARGETS,
    SERVER_FALLBACK_BETA,
    _model_wants_refusal_fallback,
    _refusal_fallback_target,
)
from engine.providers import APIUsage, ProviderError, ProviderResponse
from engine.runner import StopCondition, _describe_refusal
from engine.types import Message, ThinkingConfig


def _provider(monkeypatch, model: str) -> AnthropicProvider:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-dummy")
    return AnthropicProvider(AnthropicConfig(model=model))


def _refusal_response(
    content: str = "",
    stop_details: dict | None = None,
) -> ProviderResponse:
    return ProviderResponse(
        content=content,
        tool_calls=None,
        usage=APIUsage(input_tokens=10, output_tokens=2),
        stop_reason="refusal",
        stop_details=stop_details,
        model="claude-fable-5",
    )


# ============================================================================
# 1. Stop condition: refusal must stop the loop
# ============================================================================

def test_should_stop_on_refusal():
    stop = StopCondition()
    assert stop.should_stop(_refusal_response(), iteration=1, max_iter=100)


def test_should_stop_on_refusal_with_partial_content():
    """Mid-stream refusal: partial prose, still a hard stop — continuing
    would replay the transcript ending in an assistant message (prefill)."""
    stop = StopCondition()
    resp = _refusal_response(content="Path 1 is genuinely uncanny when the reply dra")
    assert stop.should_stop(resp, iteration=1, max_iter=100)


# ============================================================================
# 2. Refusal diagnostics carry the full real reason
# ============================================================================

def test_describe_refusal_includes_category_and_explanation():
    msg, info = _describe_refusal(_refusal_response(
        stop_details={
            "type": "refusal",
            "category": "cyber",
            "explanation": "Request touched restricted cybersecurity content.",
        },
    ))
    assert "category=cyber" in msg
    assert "Request touched restricted cybersecurity content." in msg
    assert info["category"] == "cyber"
    assert info["explanation"] == "Request touched restricted cybersecurity content."


def test_describe_refusal_explicit_when_api_sent_no_details():
    """A refusal with no stop_details must say so — not render as an
    empty string that reads like a silent early stop."""
    msg, info = _describe_refusal(_refusal_response(stop_details=None))
    assert "no stop_details" in msg
    assert info["stop_details"] is None


def test_describe_refusal_notes_discarded_partial():
    msg, info = _describe_refusal(_refusal_response(
        content="partial prose that got cut",
        stop_details={"category": "cyber"},
    ))
    assert "discarded" in msg
    assert info["partial_output_discarded"] is True
    assert info["partial_output_chars"] == len("partial prose that got cut")


# ============================================================================
# 3. Provider conversion: empty assistant turns can't poison a request
# ============================================================================

def test_convert_messages_drops_empty_assistant_messages(monkeypatch):
    """Replay of the poisoned session-mr9tf56x transcript: two persisted
    empty assistant stubs must vanish from the request instead of merging
    into a whitespace-only text block."""
    p = _provider(monkeypatch, "claude-fable-5")
    messages = [
        Message(role="user", content="how can we make a custom app"),
        Message(role="assistant", content="Short version: two paths… reply dra"),
        Message(role="user", content="how would we go about path 1"),
        Message(role="assistant", content=""),
        Message(role="assistant", content=""),
    ]
    converted = p._convert_messages(messages)

    for m in converted:
        content = m["content"]
        if isinstance(content, str):
            assert content.strip(), f"whitespace-only message survived: {m!r}"
        elif isinstance(content, list):
            for block in content:
                if block.get("type") == "text":
                    assert block["text"].strip()
    # The trailing empties are gone → conversation ends with the user turn
    # (no accidental assistant prefill).
    assert converted[-1]["role"] == "user"


def test_ensure_alternating_merge_never_yields_whitespace_only(monkeypatch):
    p = _provider(monkeypatch, "claude-fable-5")
    merged = p._ensure_alternating([
        {"role": "assistant", "content": ""},
        {"role": "assistant", "content": "real text"},
    ])
    assert merged == [{"role": "assistant", "content": "real text"}]

    merged = p._ensure_alternating([
        {"role": "assistant", "content": ""},
        {"role": "assistant", "content": ""},
    ])
    assert merged[0]["content"] == ""  # not "\n\n"


# ============================================================================
# 4. Server-side refusal fallbacks: opted in for Fable-class models only
# ============================================================================

def test_fallback_model_detection():
    assert _model_wants_refusal_fallback("claude-fable-5")
    assert _model_wants_refusal_fallback("claude-mythos-5")
    assert _model_wants_refusal_fallback("claude-opus-5-5")
    assert _model_wants_refusal_fallback("claude-sonnet-5-5")
    assert not _model_wants_refusal_fallback("claude-opus-4-8")
    assert not _model_wants_refusal_fallback("claude-sonnet-5")
    assert not _model_wants_refusal_fallback("claude-sonnet-4-6")
    assert not _model_wants_refusal_fallback("")


def test_each_model_falls_back_to_a_target_its_allow_list_has():
    """A target outside the model's allowed_fallback_models is a 400 on every
    request: Sonnet 5.5 shipped with claude-opus-4-8 and never answered.
    Allow-lists from GET /v1/models/{model}, 2026-10-09."""
    allowed = {
        "claude-fable-5": {"claude-opus-4-8", "claude-opus-5"},
        "claude-fable-5-1": {"claude-opus-4-8", "claude-opus-5"},
        "claude-opus-5-5": {"claude-opus-4-8", "claude-opus-5"},
        "claude-opus-5-5-fast": {"claude-opus-4-8", "claude-opus-5"},
        "claude-sonnet-5-5": {"claude-sonnet-5"},
    }
    for model, targets in allowed.items():
        assert _refusal_fallback_target(model) in targets, model


def test_build_request_adds_fallbacks_for_fable(monkeypatch):
    p = _provider(monkeypatch, "claude-fable-5")
    kwargs = p._build_request(
        messages=[Message(role="user", content="hello")],
    )
    assert kwargs["extra_body"]["fallbacks"] == [{"model": "claude-opus-4-8"}]
    assert SERVER_FALLBACK_BETA in kwargs["extra_headers"]["anthropic-beta"]


def test_build_request_sonnet_5_5_falls_back_to_sonnet_5(monkeypatch):
    p = _provider(monkeypatch, "claude-sonnet-5-5")
    kwargs = p._build_request(
        messages=[Message(role="user", content="hello")],
        thinking=ThinkingConfig(enabled=True, effort="high"),
    )
    assert kwargs["thinking"]["type"] == "adaptive"
    assert kwargs["extra_body"]["fallbacks"] == [{"model": "claude-sonnet-5"}]


def test_sonnet_5_5_thinking_off_goes_without_a_fallback(monkeypatch):
    """Thinking off on Sonnet 5.5 is `between_tools`, which claude-sonnet-5
    lacks; the API checks the request against the target too (400 "…
    between_tools is not supported for this model", live 2026-10-09)."""
    p = _provider(monkeypatch, "claude-sonnet-5-5")
    kwargs = p._build_request(
        messages=[Message(role="user", content="hello")],
        thinking=ThinkingConfig(enabled=False),
    )
    assert kwargs["thinking"] == {"type": "between_tools"}
    assert "fallbacks" not in (kwargs.get("extra_body") or {})


class _FallbackRefused(Exception):
    """The API's 400 for a target outside the model's allow-list."""

    status_code = 400

    def __str__(self):
        return (
            "Error code: 400 - {'type': 'error', 'error': {'type': "
            "'invalid_request_error', 'message': \"'claude-opus-4-8' is not a "
            "valid fallback target for 'claude-sonnet-5-5'. See this model's "
            "allowed_fallback_models on GET /v1/models/claude-sonnet-5-5.\"}}"
        )


def test_a_refused_fallback_is_dropped_and_the_request_resent(monkeypatch):
    monkeypatch.setattr(anthropic_provider, "_REFUSED_FALLBACKS", {})
    # The target Sonnet 5.5 shipped with, which its allow-list lacks.
    monkeypatch.setitem(REFUSAL_FALLBACK_TARGETS, "claude-sonnet-5-5", "claude-opus-4-8")
    hello = [Message(role="user", content="hello")]
    on = ThinkingConfig(enabled=True)
    p = _provider(monkeypatch, "claude-sonnet-5-5")
    sent = p._build_request(messages=hello, thinking=on)
    assert sent["extra_body"]["fallbacks"] == [{"model": "claude-opus-4-8"}]

    err = p._request_error(_FallbackRefused(), sent)
    assert err.code == "invalid_fallback" and err.retryable

    resend = p._build_request(messages=hello, thinking=on)
    assert "fallbacks" not in (resend.get("extra_body") or {})
    # One-shot callers make a provider per call; those go without it too.
    fresh = _provider(monkeypatch, "claude-sonnet-5-5")._build_request(messages=hello, thinking=on)
    assert "fallbacks" not in (fresh.get("extra_body") or {})
    # Nothing left to drop, so a second refusal is final: no loop.
    assert not p._request_error(_FallbackRefused(), resend).retryable
    # Other models keep theirs.
    fable = _provider(monkeypatch, "claude-fable-5")._build_request(messages=hello)
    assert fable["extra_body"]["fallbacks"] == [{"model": "claude-opus-4-8"}]


class _Unsupported(Exception):
    """A feature refused, by the model or by its fallback target."""

    status_code = 400

    def __init__(self, request_id: str):
        super().__init__()
        self.request_id = request_id

    def __str__(self):
        return (
            "Error code: 400 - {'type': 'error', 'error': {'type': "
            "'invalid_request_error', 'message': 'tool_choice: type \"tool\" "
            "and \"any\" are not supported for this model.'}, "
            f"'request_id': '{self.request_id}'}}"
        )


def test_a_feature_the_model_itself_lacks_keeps_the_fallback(monkeypatch):
    """"not supported for this model" may come from the target or the model.
    The resend without the fallback tells which: the same refusal again
    means the model, so the fallback is kept for later requests."""
    monkeypatch.setattr(anthropic_provider, "_REFUSED_FALLBACKS", {})
    hello = [Message(role="user", content="hello")]
    p = _provider(monkeypatch, "claude-sonnet-5-5")
    sent = p._build_request(messages=hello, thinking=ThinkingConfig(enabled=True))

    first = p._request_error(_Unsupported("req_011A"), sent)
    assert first.code == "invalid_fallback" and first.retryable
    resend = p._build_request(messages=hello, thinking=ThinkingConfig(enabled=True))
    assert "fallbacks" not in (resend.get("extra_body") or {})

    again = p._request_error(_Unsupported("req_011B"), resend)
    assert not again.retryable
    later = p._build_request(messages=hello, thinking=ThinkingConfig(enabled=True))
    assert later["extra_body"]["fallbacks"] == [{"model": "claude-sonnet-5"}]


@pytest.mark.asyncio
async def test_runner_resends_at_once_after_a_refused_fallback():
    """Same model, no backoff, no move down the model fallback chain: the
    provider already dropped the refused target."""
    import time as _time

    from engine.runner import AsyncAgentRunner
    from engine.session import Session

    class _Provider:
        name, model_id, context_window = "anthropic", "claude-sonnet-5-5", 1_000_000

        def __init__(self):
            self.calls = 0

        async def complete_async(self, **kwargs):
            self.calls += 1
            if self.calls == 1:
                raise ProviderError(
                    str(_FallbackRefused()), status=400, code="invalid_fallback", retryable=True
                )
            return ProviderResponse(
                content="done",
                tool_calls=None,
                stop_reason="end_turn",
                usage=APIUsage(input_tokens=1, output_tokens=1),
                model=self.model_id,
            )

    provider = _Provider()
    started = _time.perf_counter()
    result = await AsyncAgentRunner(provider).run(
        Session.create(system_prompt="t"), "go", stream=False
    )
    assert result.success and provider.calls == 2
    assert _time.perf_counter() - started < 0.5


def test_build_request_no_fallbacks_for_opus(monkeypatch):
    p = _provider(monkeypatch, "claude-opus-4-8")
    kwargs = p._build_request(
        messages=[Message(role="user", content="hello")],
    )
    assert "fallbacks" not in (kwargs.get("extra_body") or {})


# ============================================================================
# 5. Fallback content blocks: echo-safety and pass-through
# ============================================================================

def _sdk_response(blocks, stop_reason="end_turn", model="claude-fable-5"):
    return SimpleNamespace(
        content=blocks,
        stop_reason=stop_reason,
        stop_details=None,
        model=model,
        usage=SimpleNamespace(
            input_tokens=10,
            output_tokens=5,
            cache_read_input_tokens=0,
            cache_creation_input_tokens=0,
            speed=None,
        ),
    )


def test_parse_response_drops_pre_fallback_thinking(monkeypatch):
    """Blocks before the last `fallback` block are the refused model's
    partial — its thinking must not be echoed back on later turns."""
    p = _provider(monkeypatch, "claude-fable-5")
    blocks = [
        SimpleNamespace(type="thinking", thinking="refused-model thinking", signature="s1"),
        SimpleNamespace(type="text", text="partial before refusal "),
        SimpleNamespace(type="fallback"),
        SimpleNamespace(type="thinking", thinking="fallback-model thinking", signature="s2"),
        SimpleNamespace(type="text", text="continuation from opus"),
    ]
    parsed = p._parse_response(_sdk_response(blocks, model="claude-opus-4-8"))
    assert parsed.thinking_blocks is not None
    assert len(parsed.thinking_blocks) == 1
    assert parsed.thinking_blocks[0].thinking == "fallback-model thinking"
    # Text from both sides is kept (the API itself uses the partial text
    # as continuation context).
    assert "partial before refusal" in parsed.content
    assert "continuation from opus" in parsed.content


def test_parse_response_refusal_without_details_still_parses(monkeypatch):
    p = _provider(monkeypatch, "claude-fable-5")
    parsed = p._parse_response(_sdk_response(
        [SimpleNamespace(type="text", text="partial")],
        stop_reason="refusal",
    ))
    assert parsed.stop_reason == "refusal"
    assert parsed.stop_details is None


# ============================================================================
# 6. Bridge: the user sees the full real reason
# ============================================================================

def test_refusal_message_shows_even_when_already_streamed():
    """Unlike provider errors, the refusal notice must appear even after
    streamed prose — it's what explains the mid-word cut-off."""
    from bridge.freyja_bridge import _format_user_facing_runner_failure
    detail = (
        "category=cyber; explanation: Request touched restricted "
        "cybersecurity content; 1625 chars of partial output were cut off "
        "mid-stream and discarded from the model's context"
    )
    out = _format_user_facing_runner_failure(
        reason="refusal", message=detail, already_streamed=True,
    )
    assert out != ""
    assert "refusal" in out.lower()
    assert "category=cyber" in out
    assert "Request touched restricted cybersecurity content" in out


def test_refusal_message_without_streamed_prose():
    from bridge.freyja_bridge import _format_user_facing_runner_failure
    out = _format_user_facing_runner_failure(
        reason="refusal",
        message="the API returned no stop_details for this refusal",
        already_streamed=False,
    )
    assert "refusal" in out.lower()
    assert "no stop_details" in out
    # Actionable next step is present.
    assert "claude-opus-4-8" in out


# ============================================================================
# 7. Async runner integration: refusal ends the turn, commits nothing
# ============================================================================

class _RefusingProvider:
    """Fake provider: always returns a mid-stream refusal with partial text."""

    name = "anthropic"
    model_id = "claude-fable-5"
    context_window = 1_000_000

    def __init__(self):
        self.calls = 0

    async def complete_async(self, **kwargs):
        self.calls += 1
        # The bug's signature: a second call would have arrived with the
        # conversation ending in an assistant message (prefill).
        messages = kwargs.get("messages") or []
        assert messages[-1].role != "assistant", (
            "runner attempted an assistant-prefill continuation after a refusal"
        )
        return _refusal_response(
            content="the experience is genuinely uncanny when the reply dra",
            stop_details={"category": "cyber", "explanation": "restricted content"},
        )


@pytest.mark.asyncio
async def test_async_runner_refusal_ends_turn_without_committing():
    from engine.runner import AsyncAgentRunner
    from engine.session import Session

    provider = _RefusingProvider()
    runner = AsyncAgentRunner(provider)
    session = Session.create(system_prompt="test")

    result = await runner.run(session, "how would we go about path 1", stream=False)

    assert provider.calls == 1, "refusal must not be retried/continued"
    assert result.success is False
    assert result.error is not None
    assert result.error.reason == "refusal"
    assert result.error.retryable is False
    assert result.stop_reason == "refusal"
    # Full real reason present in the error message.
    assert "category=cyber" in result.error.message
    assert "restricted content" in result.error.message
    # The refused partial was discarded: no assistant message in the
    # transcript, so the next turn can't trip the prefill 400.
    roles = [m.role for m in session.get_messages()]
    assert "assistant" not in roles
