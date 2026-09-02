"""Z.ai provider (glm-5.3) request-shaping tests.

GLM-5.3's reasoning is mandatory: thinking.type "disabled" 400s (Z.ai error
code 1210), and the effort ladder is low/high/max only. The provider must
therefore always send thinking enabled plus a reasoning_effort on the
supported ladder — including when Freyja's ThinkingConfig says disabled
(→ low, the documented migration path) or "medium" (→ high).
"""

from __future__ import annotations

import pytest

from engine.providers import MODEL_REGISTRY, get_provider_name
from engine.types import Message, TextBlock, ThinkingConfig
from engine.zai_provider import (
    ZaiConfig,
    ZaiProvider,
    _map_effort_to_zai,
)


def _provider(**cfg_kwargs) -> ZaiProvider:
    # Dummy key — construction makes no network call; we only exercise the
    # pure request builders below.
    return ZaiProvider(ZaiConfig(model="glm-5.3", api_key="dummy-key", **cfg_kwargs))


def test_registry_routes_glm53_to_zai():
    assert get_provider_name("glm-5.3") == "zai"
    assert get_provider_name("glm-5.3-flash") == "zai"
    # The `-fireworks` twins are the same weights on a different host —
    # they must NOT resolve to the Z.ai provider.
    assert get_provider_name("glm-5.3-fireworks") == "fireworks"
    assert get_provider_name("glm-5.3-flash-fireworks") == "fireworks"
    for model in (
        "glm-5.3",
        "glm-5.3-flash",
        "glm-5.3-fireworks",
        "glm-5.3-flash-fireworks",
    ):
        spec = MODEL_REGISTRY[model]
        assert spec["reasoning_mode"] == "required", model
        assert spec["reasoning_levels"] == ("low", "high", "max"), model


def test_env_var_routing_splits_zai_from_fireworks():
    from bridge.tools.agent_types import _env_var_for_model

    assert _env_var_for_model("glm-5.3") == "ZAI_API_KEY"
    assert _env_var_for_model("glm-5.3-flash") == "ZAI_API_KEY"
    assert _env_var_for_model("glm-5.3-fireworks") == "FIREWORKS_API_KEY"
    assert _env_var_for_model("glm-5.3-flash-fireworks") == "FIREWORKS_API_KEY"
    # Unchanged neighbours
    assert _env_var_for_model("glm-5.2") == "FIREWORKS_API_KEY"
    assert _env_var_for_model("zai-glm-4.7") == "CEREBRAS_API_KEY"
    assert _env_var_for_model("claude-opus-4-8") == "ANTHROPIC_API_KEY"
    # Gemini had no rule at all before the registry lookup went in.
    assert _env_var_for_model("gemini-3.7-flash") == "GEMINI_API_KEY"


def test_fireworks_required_mode_respects_model_ladder():
    """GLM 5.3 on Fireworks has no "medium" rung; MiniMax has no "max"."""
    from engine.fireworks_provider import FireworksConfig, FireworksProvider

    def resolve(model: str, effort: str) -> str | None:
        p = FireworksProvider(FireworksConfig(model=model, api_key="dummy-key"))
        return p._resolve_reasoning_effort(ThinkingConfig(enabled=True, effort=effort))

    # max must survive instead of being silently downgraded to medium
    assert resolve("glm-5.3-fireworks", "max") == "max"
    assert resolve("glm-5.3-fireworks", "high") == "high"
    assert resolve("glm-5.3-fireworks", "low") == "low"
    assert resolve("glm-5.3-fireworks", "medium") == "high"  # no medium rung
    # MiniMax behaviour is unchanged by that fix
    assert resolve("minimax-m2.7", "max") == "medium"
    assert resolve("minimax-m2.7", "low") == "low"
    assert resolve("minimax-m2.7", "medium") == "medium"


def test_fireworks_required_mode_omits_effort_when_disabled():
    """Thinking can't be turned off on GLM 5.3 — send no effort at all
    rather than "none", which the model would reject."""
    from engine.fireworks_provider import FireworksConfig, FireworksProvider

    p = FireworksProvider(
        FireworksConfig(model="glm-5.3-fireworks", api_key="dummy-key")
    )
    kwargs = p._build_request(
        [Message(role="user", content=[TextBlock(text="hi")])],
        thinking=ThinkingConfig(enabled=False),
    )
    assert "reasoning_effort" not in kwargs


def test_flash_sends_images_as_image_url_parts():
    """glm-5.3-flash is natively multimodal; plain glm-5.3 is text-only."""
    from engine.types import ImageBlock

    img = ImageBlock(source_type="base64", media_type="image/png", data="QUJD")
    msg = Message(role="user", content=[TextBlock(text="what is this?"), img])

    flash = ZaiProvider(ZaiConfig(model="glm-5.3-flash", api_key="dummy-key"))
    parts = flash._convert_messages([msg])[0]["content"]
    assert isinstance(parts, list)
    assert {"type": "image_url", "image_url": {"url": "data:image/png;base64,QUJD"}} in parts

    text_only = ZaiProvider(ZaiConfig(model="glm-5.3", api_key="dummy-key"))
    assert isinstance(text_only._convert_messages([msg])[0]["content"], str)


def test_effort_ladder_mapping():
    levels = ("low", "high", "max")
    assert _map_effort_to_zai("low", levels) == "low"
    assert _map_effort_to_zai("high", levels) == "high"
    assert _map_effort_to_zai("max", levels) == "max"
    # Rungs Z.ai doesn't have
    assert _map_effort_to_zai("none", levels) == "low"
    assert _map_effort_to_zai("minimal", levels) == "low"
    assert _map_effort_to_zai("medium", levels) == "high"
    assert _map_effort_to_zai("xhigh", levels) == "high"


def test_disabled_thinking_still_sends_enabled_with_low():
    p = _provider()
    kwargs = p._build_request(
        [Message(role="user", content=[TextBlock(text="hi")])],
        thinking=ThinkingConfig(enabled=False),
    )
    assert kwargs["reasoning_effort"] == "low"
    assert kwargs["extra_body"] == {"thinking": {"type": "enabled"}}


def test_enabled_thinking_maps_effort():
    p = _provider()
    for effort, expected in (("low", "low"), ("medium", "high"), ("high", "high"), ("max", "max")):
        kwargs = p._build_request(
            [Message(role="user", content=[TextBlock(text="hi")])],
            thinking=ThinkingConfig(enabled=True, effort=effort),
        )
        assert kwargs["reasoning_effort"] == expected, effort
        assert kwargs["extra_body"] == {"thinking": {"type": "enabled"}}


def test_request_always_carries_thinking_even_without_config():
    # No thinking kwarg at all → falls back to config default (disabled) → low.
    p = _provider()
    kwargs = p._build_request([Message(role="user", content=[TextBlock(text="hi")])])
    assert kwargs["reasoning_effort"] in {"low", "high", "max"}
    assert kwargs["extra_body"] == {"thinking": {"type": "enabled"}}


@pytest.mark.asyncio
async def test_structured_output_request_shape(monkeypatch):
    """json_object mode, lowest effort, schema injected into system prompt."""
    p = _provider()
    captured: dict = {}

    async def fake_create(**kwargs):
        captured.update(kwargs)

        class _Usage:
            prompt_tokens = 1
            completion_tokens = 1
            completion_tokens_details = None

        class _Msg:
            content = '{"ok": true}'
            tool_calls = None
            reasoning_content = None

        class _Choice:
            message = _Msg()
            finish_reason = "stop"

        class _Resp:
            choices = [_Choice()]
            usage = _Usage()

        return _Resp()

    monkeypatch.setattr(p._async_client.chat.completions, "create", fake_create)

    result = await p.complete_structured(
        [Message(role="user", content=[TextBlock(text="classify this")])],
        schema={"type": "object", "properties": {"ok": {"type": "boolean"}}},
        system_prompt="You are a judge.",
    )

    assert result.data == {"ok": True}
    assert captured["response_format"] == {"type": "json_object"}
    assert captured["reasoning_effort"] == "low"
    assert captured["extra_body"] == {"thinking": {"type": "enabled"}}
    system_msg = captured["messages"][0]
    assert system_msg["role"] == "system"
    assert "You are a judge." in system_msg["content"]
    assert "JSON Schema" in system_msg["content"]


def test_insufficient_balance_is_billing_not_rate_limit():
    """Z.ai answers 429 for "insufficient balance" (code 1113) as well as
    for real rate limits. Only the latter is worth retrying."""
    from openai import APIStatusError

    from engine.providers import BillingError, RateLimitError

    p = _provider()

    def _err(body: str) -> APIStatusError:
        exc = APIStatusError.__new__(APIStatusError)
        Exception.__init__(exc, body)
        exc.status_code = 429
        return exc

    billing = _err(
        "Error code: 429 - {'error': {'code': '1113', 'message': "
        "'Insufficient balance or no resource package. Please recharge.'}}"
    )
    assert isinstance(p._convert_api_error(billing), BillingError)

    throttled = _err("Error code: 429 - {'error': {'message': 'Too many requests'}}")
    converted = p._convert_api_error(throttled)
    assert isinstance(converted, RateLimitError) and converted.retryable


def test_missing_key_raises(monkeypatch):
    from engine.providers import AuthenticationError

    monkeypatch.delenv("ZAI_API_KEY", raising=False)
    with pytest.raises(AuthenticationError):
        ZaiProvider(ZaiConfig(model="glm-5.3"))


def test_base_url_env_override(monkeypatch):
    monkeypatch.setenv("ZAI_BASE_URL", "https://api.z.ai/api/coding/paas/v4")
    p = _provider()
    assert "coding" in str(p._async_client.base_url)
