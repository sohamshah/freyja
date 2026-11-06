"""The control channel reader gets past a line it cannot read.

It reads at most MAX_LINE_BYTES at a time and dispatches whole lines only.
A longer line never fit, so the reader waited for its end forever, and
every command after it (a permission response the operator clicked, a
message typed for a Slack thread) waited too, across restarts, because
the saved offset stayed at the long line.
"""

from __future__ import annotations

import asyncio

import bridge.freyja_bridge as fb
from bridge.gateway.control_channel import MAX_LINE_BYTES, ControlChannelReader, append_command

SLACK = "freyja:slack:T1:channel:C1:1.0"


def _permission(request_id: str) -> dict:
    return {"type": "permission_response", "sessionId": SLACK, "requestId": request_id, "approved": True}


def _too_long() -> dict:
    return {"type": "talk_deliver", "to": SLACK, "message": {"content": "x" * (2 * MAX_LINE_BYTES)}}


async def _run(cmd_file, off_file, until, *, rounds: int = 200) -> list[str]:
    """Run a reader over the file until ``until(seen)`` holds; returns the
    request ids it dispatched."""
    seen: list[str] = []
    reader = ControlChannelReader(commands_file=cmd_file, offset_file=off_file)
    reader.POLL_INTERVAL_SEC = 0.01
    reader.register("permission_response", lambda cmd: seen.append(cmd["requestId"]))
    await reader.start()
    try:
        for _ in range(rounds):
            if until(seen):
                break
            await asyncio.sleep(0.01)
    finally:
        await reader.stop()
    return seen


async def test_a_line_over_the_limit_is_skipped_and_the_commands_after_it_still_run(tmp_path, monkeypatch):
    events: list[dict] = []
    monkeypatch.setattr(fb, "emit", events.append)
    cmd_file, off_file = tmp_path / "commands.jsonl", tmp_path / "commands.offset"
    off_file.write_text("0")
    for cmd in (_permission("r1"), _too_long(), _permission("r2"), _permission("r3")):
        append_command(cmd, path=cmd_file)

    seen = await _run(cmd_file, off_file, lambda s: len(s) >= 3)

    assert seen == ["r1", "r2", "r3"]
    # Saved past every line, so a restart reads none of them again.
    assert int(off_file.read_text()) == cmd_file.stat().st_size
    # The session the line was for is told it was skipped.
    (note,) = [e for e in events if e.get("subtype") == "control_command_skipped"]
    assert note["sessionId"] == SLACK and "talk_deliver" in note["message"]


async def test_a_restart_reads_on_from_the_line_after_the_skipped_one(tmp_path, monkeypatch):
    monkeypatch.setattr(fb, "emit", lambda _ev: None)
    cmd_file, off_file = tmp_path / "commands.jsonl", tmp_path / "commands.offset"
    off_file.write_text("0")
    append_command(_too_long(), path=cmd_file)
    append_command(_permission("r1"), path=cmd_file)
    assert await _run(cmd_file, off_file, lambda s: len(s) >= 1) == ["r1"]

    append_command(_permission("r2"), path=cmd_file)
    assert await _run(cmd_file, off_file, lambda s: len(s) >= 1) == ["r2"]


async def test_a_long_line_still_being_written_is_waited_for_then_skipped(tmp_path, monkeypatch):
    # Until its newline lands the reader cannot tell where it ends; the
    # saved offset stays at its start, never inside it.
    monkeypatch.setattr(fb, "emit", lambda _ev: None)
    cmd_file, off_file = tmp_path / "commands.jsonl", tmp_path / "commands.offset"
    off_file.write_text("0")
    with cmd_file.open("w") as fp:
        fp.write('{"type": "talk_deliver", "to": "%s", "message": "%s' % (SLACK, "x" * (2 * MAX_LINE_BYTES)))
    assert await _run(cmd_file, off_file, lambda s: False, rounds=20) == []
    assert off_file.read_text() == "0"

    with cmd_file.open("a") as fp:
        fp.write('"}\n')
    append_command(_permission("r1"), path=cmd_file)
    assert await _run(cmd_file, off_file, lambda s: len(s) >= 1) == ["r1"]
    assert int(off_file.read_text()) == cmd_file.stat().st_size
