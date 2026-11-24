"""The Codex runtime keeps its thread, stops when told and says what it runs.

Three failures in one Codex session (2026-11-23/24):
- Every new `codex app-server` process opened a new thread. A turn the
  90 s silence watchdog retired took the session's memory with it, and the
  next message began a thread that knew nothing.
- Stop cancelled only Freyja's wait; Codex worked on.
- The app named the session's model (Claude Opus 5.5) while Codex ran its
  own (GPT-6 Astra), and a model picked in Freyja never reached Codex.

A stand-in for `codex app-server` speaks the JSON-RPC and writes each
request it gets to a log.
"""

from __future__ import annotations

import asyncio
import json
import sys
import textwrap
from pathlib import Path

from bridge.runtimes.codex_adapter import CodexAdapter

_SERVER = textwrap.dedent(
    r'''
    import json, sys
    log = open(sys.argv[1], "a")
    def send(msg):
        sys.stdout.write(json.dumps(msg) + "\n"); sys.stdout.flush()
    efforts = [{"reasoningEffort": e, "description": ""} for e in ("low", "high", "xhigh")]
    turn = None
    for line in sys.stdin:
        msg = json.loads(line)
        method, rid, params = msg.get("method"), msg.get("id"), msg.get("params") or {}
        if rid is None:
            continue
        log.write(json.dumps({"method": method, "params": params}) + "\n"); log.flush()
        if method == "initialize":
            send({"id": rid, "result": {}})
        elif method == "thread/resume":
            if params["threadId"] == "thread-known":
                send({"id": rid, "result": {"thread": {"id": "thread-known"}, "model": "gpt-6-astra", "reasoningEffort": "xhigh"}})
            else:
                send({"id": rid, "error": {"code": -32600, "message": "no rollout found"}})
        elif method == "thread/start":
            send({"id": rid, "result": {"thread": {"id": "thread-new"}, "model": "gpt-6-astra", "reasoningEffort": "low"}})
        elif method == "model/list":
            send({"id": rid, "result": {"data": [
                {"id": "gpt-6-astra", "model": "gpt-6-astra", "supportedReasoningEfforts": efforts},
                {"id": "gpt-6.2-sol", "model": "gpt-6.2-sol", "supportedReasoningEfforts": efforts},
            ]}})
        elif method == "turn/start":
            turn = "turn-%d" % rid
            send({"id": rid, "result": {"turn": {"id": turn}}})
            text = params["input"][0]["text"]
            if text != "work until stopped":
                send({"method": "item/completed", "params": {"item": {"type": "agentMessage", "text": "done"}}})
                send({"method": "turn/completed", "params": {"turn": {"id": turn, "status": "completed"}}})
        elif method == "turn/interrupt":
            send({"id": rid, "result": {}})
            send({"method": "turn/completed", "params": {"turn": {"id": params["turnId"], "status": "interrupted"}}})
    '''
)


def _adapter(tmp_path: Path, resume: str | None, events: list[dict]) -> tuple[CodexAdapter, Path]:
    server = tmp_path / "fake_codex.py"
    server.write_text(_SERVER)
    log = tmp_path / "requests.jsonl"
    adapter = CodexAdapter(
        runtime_id="codex_app_server",
        session_id="s1",
        workspace=str(tmp_path),
        emit=events.append,
        resume_harness_session_id=resume,
    )
    # The registry's spec spawns `codex app-server`; point it at the stand-in.
    adapter._spec = type(adapter._spec)(  # noqa: SLF001
        id="codex_app_server", label="Codex", description="", command=sys.executable, args=(str(server), str(log))
    )
    return adapter, log


def _requests(log: Path) -> list[dict]:
    return [json.loads(line) for line in log.read_text().splitlines()]


async def test_a_new_codex_process_resumes_the_sessions_thread(tmp_path):
    events: list[dict] = []
    adapter, log = _adapter(tmp_path, "thread-known", events)
    try:
        await adapter.run_turn("hello")
        assert adapter.harness_session_id == "thread-known"
        # The watchdog retires the process; the next one carries on the thread.
        await adapter.close()
        await adapter.run_turn("again")
        assert adapter.harness_session_id == "thread-known"
        methods = [r["method"] for r in _requests(log)]
        assert methods.count("thread/resume") == 2 and "thread/start" not in methods
        assert all(r["params"]["excludeTurns"] for r in _requests(log) if r["method"] == "thread/resume")
        # The app hears what Codex runs, once until it changes.
        said = [e for e in events if e.get("subtype") == "harness_model"]
        assert said[0]["details"]["model"] == "gpt-6-astra"
        assert said[0]["details"]["reasoningEffort"] == "xhigh"
        assert said[0]["details"]["resumed"] is True
        assert len(said) == 1
    finally:
        await adapter.close()

    # A thread Codex no longer has: a new one, which the bridge reports.
    events.clear()
    adapter, log = _adapter(tmp_path, "thread-gone", events)
    try:
        await adapter.run_turn("hello")
        assert adapter.harness_session_id == "thread-new"
    finally:
        await adapter.close()


async def test_codex_runs_the_sessions_model_only_when_it_offers_it(tmp_path):
    events: list[dict] = []
    adapter, log = _adapter(tmp_path, "thread-known", events)
    try:
        # A Claude model: Codex keeps its own, and nothing is sent.
        adapter.prefer_model("claude-opus-5-5", "xhigh")
        await adapter.run_turn("one")
        # An OpenAI model Codex lists, with an effort it takes.
        adapter.prefer_model("gpt-6.2-sol", "high")
        await adapter.run_turn("two")
        # Asked again: already running it, so nothing to send.
        await adapter.run_turn("three")
        turns = [r["params"] for r in _requests(log) if r["method"] == "turn/start"]
        assert "model" not in turns[0] and "effort" not in turns[0]
        assert (turns[1]["model"], turns[1]["effort"]) == ("gpt-6.2-sol", "high")
        assert "model" not in turns[2] and "effort" not in turns[2]
        assert [e["details"]["model"] for e in events if e.get("subtype") == "harness_model"] == [
            "gpt-6-astra",
            "gpt-6.2-sol",
        ]
    finally:
        await adapter.close()


async def test_stopping_a_codex_turn_interrupts_codex(tmp_path):
    events: list[dict] = []
    adapter, log = _adapter(tmp_path, "thread-known", events)
    try:
        turn = asyncio.create_task(adapter.run_turn("work until stopped"))
        for _ in range(200):
            if log.exists() and any(r["method"] == "turn/start" for r in _requests(log)):
                break
            await asyncio.sleep(0.01)
        # What Stop does: cancel the bridge's turn, then ask the adapter.
        turn.cancel()
        await adapter.cancel()
        try:
            await turn
        except asyncio.CancelledError:
            pass
        await asyncio.sleep(0.05)
        interrupts = [r["params"] for r in _requests(log) if r["method"] == "turn/interrupt"]
        assert interrupts and interrupts[0]["threadId"] == "thread-known"
        # The next message is a turn of its own on the same thread.
        result = await adapter.run_turn("next")
        assert result.text == "done"
    finally:
        await adapter.close()


async def test_picking_a_claude_model_on_a_codex_session_says_codex_keeps_its_own(monkeypatch, tmp_path):
    import bridge.freyja_bridge as fb

    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    events: list[dict] = []
    monkeypatch.setattr(fb, "emit", events.append)
    state = fb._BridgeState(workspace=str(tmp_path), default_model="kimi-k3-fast")
    sess = await state.ensure_session("s1", runtime="codex_app_server")
    sess.harness_adapter = type("Running", (), {"model": "gpt-6-astra", "close": lambda self: asyncio.sleep(0)})()

    await fb._handle_command(state, {"type": "set_model", "sessionId": "s1", "model": "claude-opus-5-5"})
    (said,) = [e for e in events if e.get("subtype") == "harness_model_unsupported"]
    assert "stays on gpt-6-astra" in said["message"]
    assert said["details"]["chatVisible"] is True

    # An OpenAI model is Codex's to run: nothing to warn about.
    events.clear()
    await fb._handle_command(state, {"type": "set_model", "sessionId": "s1", "model": "gpt-6.2-sol"})
    assert not [e for e in events if e.get("subtype") == "harness_model_unsupported"]
