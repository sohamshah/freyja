"""Shell commands that step aside for the operator (bridge/tools/background_shell.py).

With a session context present, a running bash command yields to the
background when a follow-up is waiting (after SOFT_YIELD_AFTER_S) or at
once on a cut-in: the call returns with the output so far, the command
keeps running into a log, and the session hears about its exit. Stopping
a command (turn stop, timeout) takes its whole process group down with
SIGTERM, then SIGKILL.
"""

from __future__ import annotations

import asyncio
import subprocess
import time
import uuid

import pytest

from bridge.tools import background_shell as bs
from bridge.tools.bash_tool import BashTool


@pytest.fixture(autouse=True)
def _short_waits(monkeypatch):
    # A running command is checked for its time limit and for yield
    # requests every POLL_S; a queued follow-up waits SOFT_YIELD_AFTER_S.
    monkeypatch.setattr(bs, "POLL_S", 0.02)
    monkeypatch.setattr(bs, "SOFT_YIELD_AFTER_S", 0.05)


def _ctx(tmp_path, exits: list):
    started: list = []

    async def on_exit(bg):
        exits.append(bg)

    ctx = bs.ToolContext(
        session_id="s",
        tool_yield=bs.ToolYield(),
        output_dir=tmp_path / "background",
        on_background_start=started.append,
        on_background_exit=on_exit,
    )
    return ctx, started


async def _run(tool, command, ctx=None, timeout=30):
    token = bs.CURRENT_TOOL_CONTEXT.set(ctx) if ctx is not None else None
    try:
        return await tool.execute("c1", {"command": command, "summary": "test", "timeout": timeout})
    finally:
        if token is not None:
            bs.CURRENT_TOOL_CONTEXT.reset(token)


def _alive(marker: str) -> bool:
    return bool(subprocess.run(["pgrep", "-f", marker], capture_output=True, text=True).stdout.strip())


async def _until(predicate, timeout=3.0):
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, "condition did not become true"
        await asyncio.sleep(0.01)


def _marker(seconds: int) -> str:
    return f"sleep {seconds}.{uuid.uuid4().int % 1000:03d}"


async def test_a_waiting_followup_moves_a_long_command_to_the_background(tmp_path):
    exits: list = []
    ctx, started = _ctx(tmp_path, exits)
    ctx.tool_yield.request(hard=False)
    tool = BashTool(working_dir=str(tmp_path))

    result = await _run(tool, "echo START; sleep 0.2; echo DONE", ctx)
    assert "Moved to the background" in result.content
    assert "STILL RUNNING" in result.content
    assert "START" in result.content
    (bg,) = started
    assert bg.reason == "followup"

    await _until(lambda: exits)
    assert exits == [bg]
    assert bg.exit_code == 0 and bg.state == "exited"
    log = (tmp_path / "background" / f"{bg.id}.log").read_text()
    assert "START" in log and "DONE" in log and "exited with code 0" in log

    memo = bs.build_background_command_memo(bg)
    assert memo.kind == "memo" and memo.meta["state"] == "done"
    assert "DONE" in memo.content and "no human input" in memo.content


async def test_a_cut_in_moves_it_at_once(tmp_path):
    exits: list = []
    ctx, started = _ctx(tmp_path, exits)
    tool = BashTool(working_dir=str(tmp_path))

    async def cut_in():
        await asyncio.sleep(0.03)
        ctx.tool_yield.request(hard=True)

    asyncio.create_task(cut_in())
    marker = _marker(4)
    result = await _run(tool, marker, ctx)
    assert "the operator cut in" in result.content
    assert _alive(marker)  # it was not stopped
    bs.stop_process_group(started[0]._process.proc, grace_s=0.05)
    await _until(lambda: not _alive(marker))


async def test_without_a_session_context_it_just_runs(tmp_path):
    tool = BashTool(working_dir=str(tmp_path))
    result = await _run(tool, "echo plain")
    assert "STDOUT:\nplain" in result.content
    assert "Exit code: 0" in result.content


async def test_stopping_the_turn_stops_the_whole_process_group(tmp_path):
    tool = BashTool(working_dir=str(tmp_path))
    marker = _marker(30)
    task = asyncio.create_task(_run(tool, f"{marker} && echo never"))
    await _until(lambda: _alive(marker))
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await _until(lambda: not _alive(marker))


async def test_a_timeout_stops_it_and_keeps_the_output(tmp_path):
    tool = BashTool(working_dir=str(tmp_path))
    result = await _run(tool, "echo PARTIAL; sleep 5", timeout=0.1)
    assert result.is_error
    assert "timed out after 0.1 seconds" in result.content
    assert "PARTIAL" in result.content


async def test_a_backgrounded_command_still_has_its_time_limit(tmp_path):
    exits: list = []
    ctx, started = _ctx(tmp_path, exits)
    ctx.tool_yield.request(hard=False)
    tool = BashTool(working_dir=str(tmp_path))
    marker = _marker(20)
    result = await _run(tool, marker, ctx, timeout=0.15)
    assert "Moved to the background" in result.content
    assert "time limit still applies" in result.content
    await _until(lambda: exits)
    (bg,) = exits
    assert bg.state == "timed_out"
    assert "time limit" in bs.build_background_command_memo(bg).content
    await _until(lambda: not _alive(marker))
    assert bg._watch_task is not None


async def test_stop_kills_group_members_that_outlive_the_shell():
    marker = _marker(30)
    # The subshell ignores SIGTERM and execs sleep (the ignore survives exec);
    # the leader /bin/sh dies on the SIGTERM.
    proc = await asyncio.create_subprocess_shell(
        f"(trap '' TERM; exec {marker}) & sleep 60",
        start_new_session=True,
    )
    await _until(lambda: _alive(marker))
    bs.stop_process_group(proc, grace_s=0.05)
    await proc.wait()
    await _until(lambda: not _alive(marker))
