"""Speed tiers listed as their own model ids: OpenAI's `-ultrafast` and
Anthropic's `-fast`. The API gets the base model plus the tier flag, and the
cost meter prices each call by the tier id. The Anthropic tier's wire shape
and its quota drop live in test_fast_mode_quota.py."""

import pytest

from engine.openai_provider import ULTRAFAST_MODELS, OpenAIConfig, OpenAIProvider
from engine.providers import MODEL_REGISTRY, compute_cost
from engine.types import Message, ThinkingConfig


def _openai(model: str) -> OpenAIProvider:
    return OpenAIProvider(
        OpenAIConfig(
            api_key="test-key",
            model=model,
            reasoning=ThinkingConfig(enabled=True, effort="high"),
        )
    )


@pytest.mark.parametrize("base", sorted(ULTRAFAST_MODELS))
def test_ultrafast_id_sends_base_model_on_the_ultrafast_tier(base: str) -> None:
    provider = _openai(f"{base}-ultrafast")

    params = provider.build_request_params([Message(role="user", content="hi")])

    assert params["model"] == base
    assert params["service_tier"] == "ultrafast"
    assert params["reasoning"]["effort"] == "high"
    assert provider.context_window == MODEL_REGISTRY[base]["context_window"]
    assert provider.model_id == f"{base}-ultrafast"


def test_standard_id_sends_no_service_tier() -> None:
    params = _openai("gpt-6.1-sol").build_request_params(
        [Message(role="user", content="hi")]
    )

    assert "service_tier" not in params


def test_ultrafast_on_a_model_without_the_tier_is_refused() -> None:
    # The API answers 400 "Invalid service_tier argument" for these.
    with pytest.raises(ValueError, match="Ultrafast requested for gpt-6-luna"):
        _openai("gpt-6-luna-ultrafast")


@pytest.mark.parametrize("base", sorted(ULTRAFAST_MODELS))
def test_ultrafast_costs_six_times_standard(base: str) -> None:
    usage = dict(
        input_tokens=1_000_000,
        output_tokens=1_000_000,
        cache_read_tokens=1_000_000,
        cache_write_tokens=1_000_000,
    )

    assert compute_cost(f"{base}-ultrafast", **usage) == pytest.approx(
        6 * compute_cost(base, **usage)
    )
