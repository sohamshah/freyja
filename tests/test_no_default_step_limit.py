"""No default per-turn step ceiling.

The runner used to cap every turn at min(160, max(100, 24 + 8·profiles))
iterations and most subagent profiles had their own 60–160 caps. Long tasks
hit them mid-plan and the operator saw "I hit my per-turn step limit (100
steps)". Turns now run until the model ends them; explicit
``StopCondition(max_iterations=N)`` still works, and the kanban review loop
keeps its back-and-forth cap.
"""

from __future__ import annotations

import sys

from engine.runner import AgentRunner
from engine.session import Session, TranscriptManager
from engine.types import AgentConfig
from engine.providers import APIUsage, ProviderResponse

from tests.test_max_iterations_visibility import _LoopingProvider, _NoopTool


def test_default_config_is_uncapped():
    assert AgentConfig().compute_max_iterations(1) == sys.maxsize
    assert AgentConfig().compute_max_iterations(50) == sys.maxsize
    assert AgentConfig(max_iterations=7).compute_max_iterations(1) == 7


def test_default_run_goes_past_the_old_160_ceiling():
    class _StopsAt200(_LoopingProvider):
        calls = 0

        def complete(self, messages, **kw):
            type(self).calls += 1
            if type(self).calls < 200:
                return super().complete(messages, **kw)
            return ProviderResponse(
                content="done", tool_calls=None,
                usage=APIUsage(input_tokens=1, output_tokens=1),
                stop_reason="end_turn", model="fake-model",
            )

    runner = AgentRunner(_StopsAt200())
    runner.tool_registry.register(_NoopTool())
    res = runner.run(Session(id="s", transcript=TranscriptManager(), system_prompt="sp"), "go")
    assert res.success is True, res.error
    assert res.iterations == 200


def test_work_profiles_uncapped_review_and_background_profiles_capped(monkeypatch):
    monkeypatch.delenv("FREYJA_SUBAGENT_MAX_ITERATIONS", raising=False)
    from bridge.tools.agent_types import load_agent_types

    types = load_agent_types(None)
    for name in ("general", "explore", "explore-fast", "code", "verify", "plan",
                 "review", "test", "browser-qa", "performance", "docs",
                 "memory-curator", "specifier"):
        if name in types and types[name].source == "builtin":
            assert types[name].max_iterations is None, name
    for name, cap in (("judge-calibrator", 1), ("judge-deep", 3),
                      ("skill-drafter", 15), ("skill-drafter-fork", 6)):
        assert types[name].max_iterations == cap, name


def test_kanban_review_back_and_forth_cap_kept():
    from bridge.freyja_bridge import _BridgeSession

    assert _BridgeSession.KANBAN_MAX_REVIEW_ITERATIONS == 3
