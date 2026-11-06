"""Fast mode an org has no quota for.

Without fast-mode quota for a model the API answers every `speed: "fast"`
request with 429 "rate limit of 0 fast mode input tokens per minute"
(Opus 4.8 and Opus 5 on this org, probed 2026-11-05; Opus 5.5 is served).
That reads like a rate limit but never clears. The provider drops the
`-fast` tier on the first one, the runner resends at once at standard speed
and posts a chat notice, and the picker gates a `-fast` id the org has no
quota for instead of letting the click wait through three backoffs for a
"provider overloaded" verdict."""

from __future__ import annotations

import asyncio

import httpx
import pytest

from engine.anthropic_provider import FAST_MODE_BETA, AnthropicConfig, AnthropicProvider
from engine.providers import (
    APIUsage,
    FastModeUnavailableError,
    ProviderResponse,
    RateLimitError,
)
from engine.types import Message

_URL = "https://api.anthropic.com/v1/messages"
_ORG = "00000000-0000-4000-8000-000000000000"


def _no_quota_body(model: str) -> dict:
    """The exact message shape the API sent on 2026-11-05."""
    return {
        "type": "error",
        "error": {
            "type": "rate_limit_error",
            "message": (
                "This request would exceed your organization's rate limit of 0 "
                f"fast mode input tokens per minute (org: {_ORG}, model: {model})"
            ),
        },
        "request_id": "req_test_fast_quota",
    }


def _burst_body() -> dict:
    """An ordinary per-minute limit on the same org: wait and retry."""
    return {
        "type": "error",
        "error": {
            "type": "rate_limit_error",
            "message": (
                "This request would exceed the rate limit for your organization "
                f"({_ORG}) of 2,000,000 input tokens per minute."
            ),
        },
        "request_id": "req_test_burst",
    }


def _http_error(provider: AnthropicProvider, status: int, body: dict):
    response = httpx.Response(status, request=httpx.Request("POST", _URL))
    return provider._client._make_status_error(f"{body}", body=body, response=response)


def _midstream_error(provider: AnthropicProvider, body: dict):
    """As the SDK raises an SSE ``error`` event: the connected stream's 200."""
    response = httpx.Response(200, request=httpx.Request("POST", _URL))
    return provider._client._make_status_error(f"{body}", body=body, response=response)


def _fast(model: str = "claude-opus-4-8-fast") -> AnthropicProvider:
    return AnthropicProvider(AnthropicConfig(api_key="test-key", model=model))


def _hello() -> list[Message]:
    return [Message(role="user", content="hello")]


def _asks_for_fast(request_kwargs: dict) -> bool:
    body = request_kwargs.get("extra_body") or {}
    headers = request_kwargs.get("extra_headers") or {}
    return body.get("speed") == "fast" and FAST_MODE_BETA in headers.get("anthropic-beta", "")


# ─── provider ─────────────────────────────────────────────────────────


def test_zero_quota_429_drops_the_fast_tier_for_the_rest_of_the_provider():
    p = _fast()
    sent = p._build_request(messages=_hello())
    assert _asks_for_fast(sent)
    assert p.model_id == "claude-opus-4-8-fast"

    err = p._request_error(_http_error(p, 429, _no_quota_body("claude-opus-4-8")), sent)

    assert isinstance(err, FastModeUnavailableError)
    assert err.code == "fast_mode_unavailable" and err.retryable
    assert err.model == "claude-opus-4-8" and err.tier_model_id == "claude-opus-4-8-fast"
    # The resend goes at standard speed, and the meter prices it so.
    resend = p._build_request(messages=_hello())
    assert not _asks_for_fast(resend)
    assert "speed" not in (resend.get("extra_body") or {})
    assert p.model_id == "claude-opus-4-8"
    # Nothing left to drop: a second one is an ordinary rate limit, no loop.
    again = p._request_error(_http_error(p, 429, _no_quota_body("claude-opus-4-8")), resend)
    assert isinstance(again, RateLimitError) and not isinstance(again, FastModeUnavailableError)


def test_a_burst_429_on_a_fast_provider_keeps_the_tier():
    p = _fast("claude-opus-5-5-fast")
    sent = p._build_request(messages=_hello())

    err = p._request_error(_http_error(p, 429, _burst_body()), sent)

    assert isinstance(err, RateLimitError) and not isinstance(err, FastModeUnavailableError)
    assert p.model_id == "claude-opus-5-5-fast"
    assert _asks_for_fast(p._build_request(messages=_hello()))


def test_zero_quota_wording_on_a_standard_provider_is_a_plain_rate_limit():
    p = AnthropicProvider(AnthropicConfig(api_key="test-key", model="claude-opus-4-8"))
    sent = p._build_request(messages=_hello())

    err = p._request_error(_http_error(p, 429, _no_quota_body("claude-opus-4-8")), sent)

    assert isinstance(err, RateLimitError) and not isinstance(err, FastModeUnavailableError)
    assert p.model_id == "claude-opus-4-8"


def test_a_midstream_zero_quota_event_drops_the_tier_too():
    p = _fast()
    sent = p._build_request(messages=_hello())

    err = p._request_error(_midstream_error(p, _no_quota_body("claude-opus-4-8")), sent)

    assert isinstance(err, FastModeUnavailableError) and err.retryable
    assert p.model_id == "claude-opus-4-8"
    assert not _asks_for_fast(p._build_request(messages=_hello()))


# ─── runner ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_runner_resends_at_once_at_standard_speed_and_says_so(monkeypatch):
    """No backoff, no third attempt, no fallback chain: the provider already
    dropped the tier. The operator gets one chat-visible line."""
    from engine.runner import AsyncAgentRunner
    from engine.session import Session

    slept: list[float] = []
    real_sleep = asyncio.sleep

    async def _sleep(delay, *args, **kwargs):
        slept.append(delay)
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", _sleep)

    class _Provider:
        name, context_window = "anthropic", 1_000_000

        def __init__(self):
            self.calls = 0
            self.model_id = "claude-opus-4-8-fast"

        async def complete_async(self, **kwargs):
            self.calls += 1
            if self.calls == 1:
                self.model_id = "claude-opus-4-8"
                raise FastModeUnavailableError(
                    str(_no_quota_body("claude-opus-4-8")),
                    model="claude-opus-4-8",
                    tier_model_id="claude-opus-4-8-fast",
                )
            return ProviderResponse(
                content="done",
                tool_calls=None,
                stop_reason="end_turn",
                usage=APIUsage(input_tokens=1, output_tokens=1),
                model=self.model_id,
            )

    events = []
    provider = _Provider()
    runner = AsyncAgentRunner(provider, on_system_event=events.append)
    result = await runner.run(Session.create(system_prompt="t"), "go", stream=False)

    assert result.success and provider.calls == 2
    assert not [delay for delay in slept if delay > 0]
    dropped = [e for e in events if e.type == "fast_mode_dropped"]
    assert len(dropped) == 1
    assert dropped[0].details["chatVisible"] is True
    assert dropped[0].details["model"] == "claude-opus-4-8"
    assert dropped[0].details["tierModel"] == "claude-opus-4-8-fast"
    assert "claude-opus-4-8" in dropped[0].message
    assert "standard speed" in dropped[0].message


# ─── bridge ───────────────────────────────────────────────────────────


def test_picker_gates_the_fast_id_the_org_has_no_quota_for(monkeypatch):
    from bridge.freyja_bridge import AVAILABLE_MODELS, _annotate_models

    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    by_id = {m["id"]: m for m in _annotate_models(AVAILABLE_MODELS)}

    gated = by_id["claude-opus-4-8-fast"]
    assert gated["available"] is False
    assert "fast mode" in gated["unavailableReason"].lower()
    assert "rate limit of 0 fast mode" in gated["unavailableReason"]
    served = by_id["claude-opus-5-5-fast"]
    assert served["available"] is True
    assert "unavailableReason" not in served


def test_failure_text_names_the_missing_quota_instead_of_overload():
    from bridge.freyja_bridge import _format_user_facing_runner_failure

    text = _format_user_facing_runner_failure(
        reason="rate_limit",
        message=_no_quota_body("claude-opus-4-8")["error"]["message"],
        already_streamed=False,
    )

    assert "Fast mode is not enabled" in text
    assert "overloaded" not in text.lower()
    assert "fast-mode quota" in text
