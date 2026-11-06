"""A sub-agent's token counts keep every call, whichever thread reports it.

A sub-agent's runner reports each model call to _on_sub_llm_call, which adds
the call's tokens to the record (``subagents status`` reads them) and counts
the call. Compaction reports its summary calls from worker threads; a summary
call that outlives its deadline reports from its abandoned thread while the
child's loop reports its next calls. ``record.input_tokens += n`` reads, adds
and writes back, so one report could overwrite the other. 795a5ad fixed the
same race for the parent session's cost.
"""

from __future__ import annotations

import asyncio
import sys
import threading
from types import ModuleType

import pytest


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    import bridge.transcript_persistence as tp

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("FREYJA_HOME", str(tmp_path))
    monkeypatch.setattr(tp, "SESSIONS_DIR", tmp_path / "sessions")
    telemetry = ModuleType("bridge.compaction_telemetry")
    telemetry.append_telemetry = lambda *_a, **_k: None
    monkeypatch.setitem(sys.modules, "bridge.compaction_telemetry", telemetry)


async def _child_llm_hook(monkeypatch, tmp_path):
    """Run a sub-agent whose runner stops at once; return its record and the
    on_llm_call hook its runner was given."""
    import engine.runner as runner_module
    import bridge.tools.sub_agent_tool as sat
    from bridge.tools.base import ToolRegistry
    from bridge.tools.sub_agent_registry import SubAgentRegistry

    hooks: list = []

    class _Runner(runner_module.AsyncAgentRunner):
        def __init__(self, *a, **kw):
            hooks.append(kw["on_llm_call"])
            super().__init__(*a, **kw)

        async def run(self, *_a, **_k):
            raise RuntimeError("stop here")

    monkeypatch.setattr(runner_module, "AsyncAgentRunner", _Runner)

    async def _emit(_event):
        return None

    class _Provider:
        name = "anthropic"
        model_id = "claude-sonnet-5-5"
        context_window = 200_000

    registry = SubAgentRegistry()
    spec = sat.SubAgentSpec(
        parent_workspace=str(tmp_path),
        parent_model="claude-sonnet-5-5",
        build_provider=lambda *a, **k: _Provider(),
        parent_registry=ToolRegistry(),
        registry=registry,
        emit_event=_emit,
        parent_session_id="parent-1",
    )
    record = registry.register(id="sub_t_1", label="reader", task="read it", mode="background")
    with pytest.raises(RuntimeError, match="stop here"):
        await sat.SubAgentTool(spec)._run_child(record)
    return record, hooks[0]


async def test_a_summary_call_from_a_worker_and_a_call_on_the_loop_both_count(monkeypatch, tmp_path):
    record, on_llm_call = await _child_llm_hook(monkeypatch, tmp_path)
    loop_thread = threading.current_thread()
    worker_holding = threading.Event()
    loop_added = threading.Event()

    class _Tokens(int):
        """The record's running total. Adding to it holds a worker thread
        until the loop has added its own call: the worker has read the
        total and not yet written it back."""

        def __add__(self, other):
            if threading.current_thread() is not loop_thread:
                worker_holding.set()
                loop_added.wait(5)
            return int(self) + other

    record.input_tokens = _Tokens(0)
    summary = {"model": "claude-sonnet-5-5", "input_tokens": 900, "call_kind": "summarizer"}
    main = {"model": "claude-sonnet-5-5", "input_tokens": 100}

    worker = asyncio.ensure_future(asyncio.to_thread(on_llm_call, summary))
    while not (worker_holding.is_set() or worker.done()):
        await asyncio.sleep(0.005)
    on_llm_call(main)
    loop_added.set()
    await worker
    for _ in range(3):
        await asyncio.sleep(0)

    assert record.input_tokens == 1_000
