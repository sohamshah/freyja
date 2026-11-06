"""Cross-codepoint invariants for the model registry.

docs/ADDING-A-MODEL.md lists 22 places model metadata lives. A missed entry
never fails loudly — it silently falls back to a wrong default. These tests
turn the parts that ARE mechanically checkable into failures, as one sweep
per invariant over every model.

Written 2026-09-29, after a probe found 9 of the 14 Fireworks ids in this
repo returning 404 while still sitting in FALLBACK_CHAINS as the rescue path
for nearly every Anthropic model.
"""

from __future__ import annotations

import pytest

from engine.anthropic_provider import (
    ADAPTIVE_THINKING_MODELS,
    ALWAYS_THINKING_MODELS,
    BETWEEN_TOOLS_THINKING_MODELS,
    LEGACY_THINKING_MODELS,
    THINKING_MODELS,
)
from engine.constants import MODEL_CONTEXT_WINDOWS
from engine.providers import (
    FALLBACK_CHAINS,
    MODEL_PRICING_PER_M,
    MODEL_REGISTRY,
    RETIRED_MODEL_REPLACEMENTS,
    compute_cost,
    resolve_retired_model,
)
from engine.types import (
    _ADAPTIVE_THINKING_MODEL_IDS,
    _BETWEEN_TOOLS_THINKING_MODEL_IDS,
    ThinkingConfig,
    _is_adaptive_thinking_model,
)

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


def _anthropic_models() -> list[str]:
    return sorted(m for m, spec in MODEL_REGISTRY.items() if spec["provider"] == "anthropic")


def _base(model: str) -> str:
    """The `-fast` tier id looks up like its base model."""
    return model[:-5] if model.endswith("-fast") else model


# ── registry completeness ────────────────────────────────────────────────

def test_every_registry_model_is_priced():
    """Unpriced model → cost meter reads $0.000 forever."""
    missing = sorted(m for m in MODEL_REGISTRY if m not in MODEL_PRICING_PER_M)
    assert missing == [], f"models with no pricing row: {missing}"
    unpriced = sorted(m for m in MODEL_REGISTRY if compute_cost(m, input_tokens=1) is None)
    assert unpriced == [], f"compute_cost() returned None for: {unpriced}"


def test_every_registry_model_has_one_context_window():
    """The window lives in three places: the registry row, the
    MODEL_CONTEXT_WINDOWS lookup (missing → 200k, which compacted Opus 4.8
    sessions 5x early) and the picker's contextWindow. They must agree."""
    missing = sorted(set(MODEL_REGISTRY) - set(MODEL_CONTEXT_WINDOWS))
    assert missing == [], f"models with no MODEL_CONTEXT_WINDOWS entry: {missing}"
    stale = sorted(set(MODEL_CONTEXT_WINDOWS) - set(MODEL_REGISTRY))
    assert stale == [], f"MODEL_CONTEXT_WINDOWS entries for unregistered models: {stale}"
    drift = {
        m: (MODEL_CONTEXT_WINDOWS[m], MODEL_REGISTRY[m]["context_window"])
        for m in MODEL_REGISTRY
        if MODEL_CONTEXT_WINDOWS[m] != MODEL_REGISTRY[m]["context_window"]
    }
    assert drift == {}, f"constants vs registry window drift: {drift}"
    catalog_drift = {
        m["id"]: (m.get("contextWindow"), MODEL_REGISTRY[m["id"]]["context_window"])
        for m in _catalog()
        if m["id"] in MODEL_REGISTRY
        and m.get("contextWindow") != MODEL_REGISTRY[m["id"]]["context_window"]
    }
    assert catalog_drift == {}, f"picker vs registry window drift: {catalog_drift}"


def test_fallback_chains_point_at_live_models():
    """No chain → a 503 on the primary is just a failed turn. An
    unregistered target raises in get_provider_name() mid-failover."""
    missing = sorted(m for m in MODEL_REGISTRY if not FALLBACK_CHAINS.get(m))
    assert missing == [], f"models with no fallback chain: {missing}"
    bad = sorted(
        {t for chain in FALLBACK_CHAINS.values() for t in chain if t not in MODEL_REGISTRY}
    )
    assert bad == [], f"fallback targets missing from MODEL_REGISTRY: {bad}"
    selfref = {m: c for m, c in FALLBACK_CHAINS.items() if m in c}
    assert selfref == {}, f"self-referencing chains: {selfref}"


# ── retired ids ──────────────────────────────────────────────────────────

def test_removed_ids_are_gone_from_every_table():
    """The bug this file was written for: a dead id sitting in a table as
    if it were live. Once removed, it stays removed everywhere."""
    tables = {
        "MODEL_REGISTRY": set(MODEL_REGISTRY),
        "MODEL_PRICING_PER_M": set(MODEL_PRICING_PER_M),
        "FALLBACK_CHAINS keys": set(FALLBACK_CHAINS),
        "FALLBACK_CHAINS targets": {t for c in FALLBACK_CHAINS.values() for t in c},
        "MODEL_CONTEXT_WINDOWS": set(MODEL_CONTEXT_WINDOWS),
        "AVAILABLE_MODELS": {m["id"] for m in _catalog()},
        "MODEL_REASONING_META": set(_reasoning_meta()),
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


# ── bridge reasoning ladders ─────────────────────────────────────────────

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


def test_always_thinking_models_do_not_offer_a_none_rung():
    """Offering 'none' where thinking can't be disabled is a silent lie:
    the request omits `thinking` and the model reasons anyway, on the
    operator's money."""
    meta = _reasoning_meta()
    offenders = {
        m: meta[m]["reasoningLevels"]
        for m in ALWAYS_THINKING_MODELS
        if m in meta and "none" in meta[m].get("reasoningLevels", [])
    }
    assert offenders == {}, f"always-thinking models offering 'none': {offenders}"


# ── thinking shape per Anthropic model ───────────────────────────────────

def test_every_anthropic_thinking_model_is_in_the_exact_match_sets():
    """The thinking sets are `x in SET` checks, so a model registered with
    thinking=True but missing from them silently runs without thinking.
    Fable 5.1 was wired this way once; this sweep covers every model."""
    without_thinking = sorted(
        m for m in _anthropic_models()
        if MODEL_REGISTRY[m]["thinking"] and _base(m) not in THINKING_MODELS
    )
    assert without_thinking == [], f"thinking models outside THINKING_MODELS: {without_thinking}"
    no_shape = sorted(
        m for m in _anthropic_models()
        if MODEL_REGISTRY[m]["thinking"]
        and not _is_adaptive_thinking_model(m)
        and _base(m) not in LEGACY_THINKING_MODELS
    )
    assert no_shape == [], f"thinking models with no request shape: {no_shape}"
    unregistered = sorted(THINKING_MODELS - set(MODEL_REGISTRY))
    assert unregistered == [], f"THINKING_MODELS entries not registered: {unregistered}"


def test_thinking_shape_sets_are_in_sync_across_the_two_copies():
    """engine/types.py holds the copies that shape requests; the provider
    holds the ones that report the capability. Drift means the UI and the
    wire disagree."""
    assert BETWEEN_TOOLS_THINKING_MODELS == _BETWEEN_TOOLS_THINKING_MODEL_IDS
    assert ADAPTIVE_THINKING_MODELS == _ADAPTIVE_THINKING_MODEL_IDS


@pytest.mark.parametrize("model", sorted(BETWEEN_TOOLS_THINKING_MODELS))
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


def test_every_other_anthropic_model_omits_thinking_when_off():
    """Regression guard: the _build_request gate was loosened for
    between_tools; everything else must keep sending no thinking field."""
    off = ThinkingConfig(enabled=False)
    sent = {
        m: off.to_api_param(m)
        for m in _anthropic_models()
        if _base(m) not in BETWEEN_TOOLS_THINKING_MODELS and off.to_api_param(m) is not None
    }
    assert sent == {}, f"thinking sent with thinking off: {sent}"
