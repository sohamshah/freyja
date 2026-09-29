"""Cross-codepoint invariants for the model registry.

docs/ADDING-A-MODEL.md lists 22 places model metadata lives. A missed entry
never fails loudly — it silently falls back to a wrong default. These tests
turn the parts that ARE mechanically checkable into failures.

Written 2026-09-29, after a probe found 9 of the 14 Fireworks ids in this
repo returning 404 while still sitting in FALLBACK_CHAINS as the rescue path
for nearly every Anthropic model.
"""

from __future__ import annotations

import pytest

from engine.providers import (
    FALLBACK_CHAINS,
    MODEL_PRICING_PER_M,
    MODEL_REGISTRY,
    compute_cost,
)
from engine.types import ThinkingConfig

# Ids kept in the registry so a pinned session still resolves, but which the
# provider no longer serves. They may be fallback KEYS, never TARGETS.
# Verified dead by live probe against the Fireworks API on 2026-09-29.
RETIRED_MODEL_IDS = frozenset({
    "deepseek-v4-pro",
    "glm-5.1",
    "glm-5.2",
    "kimi-k2.5",
    "kimi-k2.6",
    "kimi-k2.7-code",
    "minimax-m2.7",
    "qwen3.6-plus",
    "qwen3.7-plus",
})


def _catalog():
    from bridge.freyja_bridge import AVAILABLE_MODELS

    return AVAILABLE_MODELS


def _reasoning_meta():
    from bridge.freyja_bridge import MODEL_REASONING_META

    return MODEL_REASONING_META


# ── registry completeness ────────────────────────────────────────────────

def test_every_registry_model_is_priced():
    """Unpriced model → cost meter reads $0.000 forever."""
    missing = sorted(m for m in MODEL_REGISTRY if m not in MODEL_PRICING_PER_M)
    assert missing == [], f"models with no pricing row: {missing}"


def test_every_registry_model_has_a_fallback_chain():
    """No chain → a 503 on the primary is just a failed turn."""
    missing = sorted(m for m in MODEL_REGISTRY if not FALLBACK_CHAINS.get(m))
    assert missing == [], f"models with no fallback chain: {missing}"


def test_compute_cost_returns_a_number_for_every_registry_model():
    unpriced = sorted(
        m for m in MODEL_REGISTRY if compute_cost(m, input_tokens=1) is None
    )
    assert unpriced == [], f"compute_cost() returned None for: {unpriced}"


# ── fallback chain sanity ────────────────────────────────────────────────

def test_fallback_targets_are_all_registered_models():
    """An unregistered target raises in get_provider_name() mid-failover."""
    bad = sorted(
        {t for chain in FALLBACK_CHAINS.values() for t in chain if t not in MODEL_REGISTRY}
    )
    assert bad == [], f"fallback targets missing from MODEL_REGISTRY: {bad}"


def test_no_chain_falls_back_onto_a_retired_model():
    """The bug this file was written for: a rescue path that 404s."""
    offenders = {
        model: [t for t in chain if t in RETIRED_MODEL_IDS]
        for model, chain in FALLBACK_CHAINS.items()
        if any(t in RETIRED_MODEL_IDS for t in chain)
    }
    assert offenders == {}, f"chains pointing at retired models: {offenders}"


def test_a_model_never_falls_back_to_itself():
    selfref = {m: c for m, c in FALLBACK_CHAINS.items() if m in c}
    assert selfref == {}, f"self-referencing chains: {selfref}"


# ── bridge catalog ↔ engine registry ────────────────────────────────────

def test_catalog_models_are_all_in_the_engine_registry():
    """A picker entry with no registry row raises 'Unknown model family'."""
    extra = sorted({m["id"] for m in _catalog()} - set(MODEL_REGISTRY))
    assert extra == [], f"catalog models absent from MODEL_REGISTRY: {extra}"


def test_every_thinking_model_has_reasoning_meta():
    """Missing meta → empty reasoning selector or a wrong default."""
    missing = sorted(
        m["id"] for m in _catalog()
        if m.get("thinking") and m["id"] not in _reasoning_meta()
    )
    assert missing == [], f"thinking models with no reasoning meta: {missing}"


def test_reasoning_default_is_always_an_offered_level():
    bad = {
        mid: meta for mid, meta in _reasoning_meta().items()
        if meta.get("reasoningLevels")
        and meta.get("reasoningDefault") not in meta["reasoningLevels"]
    }
    assert bad == {}, f"reasoningDefault not in reasoningLevels: {bad}"


# ── thinking-off correctness ─────────────────────────────────────────────

def test_always_thinking_models_do_not_offer_a_none_rung():
    """Offering 'none' where thinking can't be disabled is a silent lie:
    the request omits `thinking` and the model reasons anyway, on the
    operator's money."""
    from engine.anthropic_provider import ALWAYS_THINKING_MODELS

    meta = _reasoning_meta()
    offenders = {
        m: meta[m]["reasoningLevels"]
        for m in ALWAYS_THINKING_MODELS
        if m in meta and "none" in meta[m].get("reasoningLevels", [])
    }
    assert offenders == {}, f"always-thinking models offering 'none': {offenders}"


@pytest.mark.parametrize("model", sorted({"claude-sonnet-5-5"}))
def test_between_tools_model_emits_the_exact_accepted_shape(model):
    """The API 400s on between_tools paired with display, budget_tokens,
    block_binding, or an xhigh/max effort. Assert we can't produce those."""
    off = ThinkingConfig(enabled=False, effort="none")
    assert off.to_api_param(model) == {"type": "between_tools"}
    assert off.get_output_config(model) is None

    # Even if an effort leaks in alongside "off", no output_config is sent.
    for effort in ("xhigh", "max"):
        leaked = ThinkingConfig(enabled=False, effort=effort)
        assert leaked.to_api_param(model) == {"type": "between_tools"}
        assert leaked.get_output_config(model) is None


def test_between_tools_models_are_in_sync_across_the_two_copies():
    """engine/types.py holds the copy that shapes requests; the provider
    holds the one that reports the capability. Drift means the UI and the
    wire disagree."""
    from engine.anthropic_provider import BETWEEN_TOOLS_THINKING_MODELS
    from engine.types import _BETWEEN_TOOLS_THINKING_MODEL_IDS

    assert BETWEEN_TOOLS_THINKING_MODELS == _BETWEEN_TOOLS_THINKING_MODEL_IDS


def test_adaptive_thinking_sets_are_in_sync_across_the_two_copies():
    from engine.anthropic_provider import ADAPTIVE_THINKING_MODELS
    from engine.types import _ADAPTIVE_THINKING_MODEL_IDS

    assert ADAPTIVE_THINKING_MODELS == _ADAPTIVE_THINKING_MODEL_IDS


def test_non_between_tools_models_still_omit_thinking_when_off():
    """Regression guard: the _build_request gate was loosened for
    between_tools; everything else must keep sending no thinking field."""
    off = ThinkingConfig(enabled=False)
    for model in ("claude-opus-5-5", "claude-sonnet-5", "claude-sonnet-4-6",
                  "claude-haiku-4-5", "claude-sonnet-4-5"):
        assert off.to_api_param(model) is None, model
