"""Claude Fable 5.1 wiring.

Fable 5.1 shares Fable 5's quirks (always-on adaptive thinking, forced
tool_choice rejected, refusal-fallback opt-in) but Freyja matches those
two ways: some sets are exact membership and some are substring. This
pins both halves, because the failure modes are silent — an exact-match
miss means the model runs without thinking, and a substring miss means a
400 on every structured-output call.
"""

from __future__ import annotations

from engine.anthropic_provider import (
    ADAPTIVE_THINKING_MODELS,
    THINKING_MODELS,
    _model_rejects_forced_tool_choice,
    _model_wants_refusal_fallback,
)
from engine.constants import MODEL_CONTEXT_WINDOWS
from engine.providers import (
    FALLBACK_CHAINS,
    MODEL_PRICING_PER_M,
    MODEL_REGISTRY,
    compute_cost,
    get_provider_name,
)
from engine.types import _is_adaptive_thinking_model

MODEL = "claude-fable-5-1"


def test_registered_across_engine_tables():
    assert get_provider_name(MODEL) == "anthropic"
    assert MODEL_CONTEXT_WINDOWS[MODEL] == 1_000_000
    assert MODEL_REGISTRY[MODEL]["context_window"] == 1_000_000
    assert MODEL_REGISTRY[MODEL]["thinking"] is True
    assert MODEL in FALLBACK_CHAINS
    assert all(t in MODEL_REGISTRY for t in FALLBACK_CHAINS[MODEL])


def test_exact_match_thinking_sets_include_it():
    """These are `x in SET` checks — a near-miss silently disables thinking."""
    assert MODEL in ADAPTIVE_THINKING_MODELS
    assert MODEL in THINKING_MODELS
    assert _is_adaptive_thinking_model(MODEL)


def test_substring_matched_quirks_are_inherited():
    """These use `base in model`, so "claude-fable-5" already covers 5.1.
    Asserted so a future switch to exact matching fails loudly here."""
    assert _model_rejects_forced_tool_choice(MODEL)
    assert _model_wants_refusal_fallback(MODEL)


def test_cache_read_is_the_cheaper_0_025x_rate():
    """Fable 5.1 is the only tier billing cache reads at 0.025x input
    ($0.25/MTok) rather than the usual 0.1x — Fable 5 is still $1.00."""
    in_rate, out_rate, cache_read, cache_write = MODEL_PRICING_PER_M[MODEL]
    assert (in_rate, out_rate, cache_write) == (10.0, 50.0, 12.5)
    assert cache_read == 0.25
    assert cache_read == in_rate * 0.025
    assert MODEL_PRICING_PER_M["claude-fable-5"][2] == 1.0

    # A cache-heavy turn should cost strictly less than the same turn on
    # Fable 5 — the whole point of the new rate.
    args = dict(input_tokens=10_000, output_tokens=2_000, cache_read_tokens=500_000)
    assert compute_cost(MODEL, **args) < compute_cost("claude-fable-5", **args)


def test_bridge_catalog_and_reasoning_meta():
    from bridge.freyja_bridge import AVAILABLE_MODELS, MODEL_REASONING_META

    entry = next(m for m in AVAILABLE_MODELS if m["id"] == MODEL)
    assert entry["family"] == "anthropic"
    assert entry["envVar"] == "ANTHROPIC_API_KEY"
    assert entry["contextWindow"] == 1_000_000
    meta = MODEL_REASONING_META[MODEL]
    assert meta["reasoningDefault"] == "high"
    assert meta["reasoningLevels"] == MODEL_REASONING_META["claude-fable-5"]["reasoningLevels"]
