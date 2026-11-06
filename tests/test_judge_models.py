"""Judge model selection for board mode and goal mode.

The load-bearing invariant: a judge must never be the model whose work
it is grading. Board mode gets that by drawing from a cross-provider
pool; goal mode used to violate it outright by judging with
`self.model_id`, so a session graded its own homework — invisible in
the UI, and it silently tracked whatever the default model became.
"""

from __future__ import annotations

from bridge.freyja_bridge import (
    _GOAL_JUDGE_MODEL,
    _GOAL_JUDGE_MODEL_ALT,
    _GOAL_JUDGE_QUICK_MODEL,
    _GOAL_JUDGE_QUICK_MODEL_ALT,
    _KANBAN_JUDGE_MODEL_POOL,
    _distinct_judge_model,
)
from engine.providers import MODEL_PRICING_PER_M, MODEL_REGISTRY, get_provider_name

_ALL_JUDGE_MODELS = (
    *_KANBAN_JUDGE_MODEL_POOL,
    _GOAL_JUDGE_MODEL,
    _GOAL_JUDGE_MODEL_ALT,
    _GOAL_JUDGE_QUICK_MODEL,
    _GOAL_JUDGE_QUICK_MODEL_ALT,
)


def test_every_judge_model_is_registered_and_priced():
    """An unregistered judge model fails at spawn time, mid-review —
    the pool comment calls this out as the main risk of editing it."""
    unregistered = sorted(m for m in _ALL_JUDGE_MODELS if m not in MODEL_REGISTRY)
    assert unregistered == [], f"judge models not in MODEL_REGISTRY: {unregistered}"
    unpriced = sorted(m for m in _ALL_JUDGE_MODELS if m not in MODEL_PRICING_PER_M)
    assert unpriced == [], f"judge models with no pricing row: {unpriced}"


def test_board_pool_spans_three_providers():
    """The pool exists to average out single-model bias; that only works
    if the entries actually come from different providers."""
    providers = {get_provider_name(m) for m in _KANBAN_JUDGE_MODEL_POOL}
    assert providers == {"anthropic", "openai", "google"}
    assert len(_KANBAN_JUDGE_MODEL_POOL) == len(set(_KANBAN_JUDGE_MODEL_POOL))


def test_judge_never_grades_its_own_model():
    """Regression guard for the actual bug: goal mode judged with
    `self.model_id`. A session already on the judge model gets the alt,
    which must differ from the preferred or the guard does nothing."""
    assert (
        _distinct_judge_model("glm-5.3-fireworks", _GOAL_JUDGE_MODEL, _GOAL_JUDGE_MODEL_ALT)
        == _GOAL_JUDGE_MODEL
    )
    assert (
        _distinct_judge_model(_GOAL_JUDGE_MODEL, _GOAL_JUDGE_MODEL, _GOAL_JUDGE_MODEL_ALT)
        == _GOAL_JUDGE_MODEL_ALT
    )
    assert (
        _distinct_judge_model(
            _GOAL_JUDGE_QUICK_MODEL, _GOAL_JUDGE_QUICK_MODEL, _GOAL_JUDGE_QUICK_MODEL_ALT
        )
        == _GOAL_JUDGE_QUICK_MODEL_ALT
    )
    # Whitespace/empty session ids must not accidentally collide.
    assert (
        _distinct_judge_model("", _GOAL_JUDGE_MODEL, _GOAL_JUDGE_MODEL_ALT)
        == _GOAL_JUDGE_MODEL
    )
    assert _GOAL_JUDGE_MODEL_ALT != _GOAL_JUDGE_MODEL
    assert _GOAL_JUDGE_QUICK_MODEL_ALT != _GOAL_JUDGE_QUICK_MODEL


def test_quick_judge_is_actually_cheap():
    """`quick` is the cost/latency tier — it must not cost more than the
    standard judge it's meant to undercut (kimi-k3-fast, for instance,
    is $4.50/$22.50 and would invert the tiers)."""
    quick_in, quick_out = MODEL_PRICING_PER_M[_GOAL_JUDGE_QUICK_MODEL][:2]
    std_in, std_out = MODEL_PRICING_PER_M[_GOAL_JUDGE_MODEL][:2]
    assert quick_in < std_in and quick_out < std_out
