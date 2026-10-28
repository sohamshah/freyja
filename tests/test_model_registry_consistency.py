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
    RETIRED_MODEL_REPLACEMENTS,
    compute_cost,
    resolve_retired_model,
)
from engine.types import ThinkingConfig

# Every id the registry has dropped so far. Each must be absent from every
# table and present in RETIRED_MODEL_REPLACEMENTS, so a session, job,
# binding or profile that still names it lands on something live. Verified
# dead or deprecated by live probe and the providers' deprecation pages on
# 2026-10-28 (the Fireworks nine on 2026-09-29).
REMOVED_MODEL_IDS = frozenset({
    "claude-sonnet-4-5",
    "gpt-5.3-codex",
    "gpt-5.4-nano",
    "zai-glm-4.7",
    "deepseek-v4-pro",
    "glm-5.1",
    "glm-5.2",
    "kimi-k2.5",
    "kimi-k2.6",
    "kimi-k2.7-code",
    "minimax-m2.7",
    "qwen3.6-plus",
    "qwen3.7-plus",
    "gemini-3.7-flash",
    "gemini-3.5-flash",
    "gemini-3.1-flash",
    "gemini-2.5-pro",
    "gemini-2.5-flash",
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


def test_a_model_never_falls_back_to_itself():
    selfref = {m: c for m, c in FALLBACK_CHAINS.items() if m in c}
    assert selfref == {}, f"self-referencing chains: {selfref}"


# ── retired ids ──────────────────────────────────────────────────────────

def test_removed_ids_are_gone_from_every_table():
    """The bug this file was written for: a dead id sitting in a table as
    if it were live. Once removed, it stays removed everywhere."""
    from bridge.freyja_bridge import AVAILABLE_MODELS, MODEL_REASONING_META
    from engine.constants import MODEL_CONTEXT_WINDOWS

    tables = {
        "MODEL_REGISTRY": set(MODEL_REGISTRY),
        "MODEL_PRICING_PER_M": set(MODEL_PRICING_PER_M),
        "FALLBACK_CHAINS keys": set(FALLBACK_CHAINS),
        "FALLBACK_CHAINS targets": {t for c in FALLBACK_CHAINS.values() for t in c},
        "MODEL_CONTEXT_WINDOWS": set(MODEL_CONTEXT_WINDOWS),
        "AVAILABLE_MODELS": {m["id"] for m in AVAILABLE_MODELS},
        "MODEL_REASONING_META": set(MODEL_REASONING_META),
    }
    offenders = {
        name: sorted(ids & REMOVED_MODEL_IDS) for name, ids in tables.items()
        if ids & REMOVED_MODEL_IDS
    }
    assert offenders == {}, f"removed ids still present: {offenders}"


def test_every_removed_id_has_a_live_replacement():
    """Removal without a replacement strands whatever still names the id."""
    missing = sorted(REMOVED_MODEL_IDS - set(RETIRED_MODEL_REPLACEMENTS))
    assert missing == [], f"removed ids with no replacement: {missing}"
    dead_ends = {
        old: new for old, new in RETIRED_MODEL_REPLACEMENTS.items()
        if resolve_retired_model(new) not in MODEL_REGISTRY
    }
    assert dead_ends == {}, f"replacements that are not live: {dead_ends}"
    stale = sorted(set(RETIRED_MODEL_REPLACEMENTS) & set(MODEL_REGISTRY))
    assert stale == [], f"ids both registered and retired: {stale}"


def test_resolve_retired_model_follows_chains_and_leaves_live_ids_alone():
    assert resolve_retired_model("gemini-3.7-flash") == "gemini-3.8-flash"
    assert resolve_retired_model("kimi-k2.6") == "kimi-k3"
    for live in ("claude-opus-5-5", "gemini-4-argon", "kimi-k3"):
        assert resolve_retired_model(live) == live
    # Unknown ids pass through untouched: rejecting them with a message is
    # the caller's job (gateway /model, Slack flags, the picker).
    assert resolve_retired_model("not-a-model") == "not-a-model"


def test_sub_agent_profiles_name_only_live_models():
    """A profile whose model list holds a removed id spawns on the
    replacement (resolve_model_choice maps it), but the built-in profiles
    should not need the net: fix the profile."""
    from bridge.tools.agent_types import AGENT_TYPES

    stale = {
        name: sorted(
            m for m in [*(_as_list(t.model)), *t.model_fallbacks] if m in REMOVED_MODEL_IDS
        )
        for name, t in AGENT_TYPES.items()
    }
    stale = {k: v for k, v in stale.items() if v}
    assert stale == {}, f"built-in profiles naming removed models: {stale}"


def _as_list(value):
    if isinstance(value, (list, tuple)):
        return list(value)
    return [value] if isinstance(value, str) else []


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
                  "claude-haiku-4-5", "claude-opus-4-5"):
        assert off.to_api_param(model) is None, model
