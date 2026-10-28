"""emit() keeps each event whole when several threads emit at once.

Compaction runs in a worker thread (engine/runner.py run_off_loop), and what
it calls emits from there: its log lines through the logging bridge and the
summarizer's llm_call_metric. The event loop keeps emitting meanwhile.
Electron main reads the bridge's stdout as one JSON event per line, so two
events that interleave break the line, and main drops both.
"""

from __future__ import annotations

import io
import json
import os
import sys
import threading

import pytest

import bridge.freyja_bridge as fb

THREADS = 4
PER_THREAD = 100
# Tool results and logged errors are often this big; an event this size takes
# several writes to the pipe.
TEXT = "x" * 64_000


@pytest.fixture
def stdout_pipe(monkeypatch, tmp_path):
    """A pipe opened as Electron main spawns the bridge's stdout
    (PYTHONUNBUFFERED=1, so each write goes straight to the pipe), with the
    debug log and the session mirror under tmp_path. Yields the stream and a
    function that closes it and returns what came through."""
    monkeypatch.setattr(fb, "_DEBUG_LOG_PATH", tmp_path / "bridge-events.jsonl")
    monkeypatch.setattr(fb, "_DEBUG_LOG_PREV_PATH", tmp_path / "bridge-events.prev.jsonl")
    monkeypatch.setattr(fb, "_DEBUG_LOG_ENABLED", True)
    monkeypatch.setattr(fb, "_SESSION_EVENT_DIR", tmp_path / "sessions")
    monkeypatch.setattr(fb, "_SESSION_EVENT_FILES", fb._OrderedDict())

    read_fd, write_fd = os.pipe()
    out = io.TextIOWrapper(io.FileIO(write_fd, "w"), encoding="utf-8", write_through=True)
    chunks: list[bytes] = []

    def drain() -> None:
        with io.FileIO(read_fd, "r") as f:
            while chunk := f.read(1 << 16):
                chunks.append(chunk)

    reader = threading.Thread(target=drain)
    reader.start()

    def close() -> str:
        if not out.closed:
            out.close()
        reader.join()
        return b"".join(chunks).decode("utf-8")

    yield out, close
    close()
    for sid in list(fb._SESSION_EVENT_FILES):
        fb._close_session_event_file(sid)


def _emit_at_once(stdout, session_for) -> None:
    """THREADS threads emit PER_THREAD big events each, all at once, to
    ``stdout``. Swapped in here, not in the fixture: pytest puts its own
    capture back on sys.stdout before the test runs."""
    start = threading.Barrier(THREADS)

    def run(n: int) -> None:
        start.wait()
        for i in range(PER_THREAD):
            fb.emit({
                "type": "system_event",
                "sessionId": session_for(n),
                "subtype": "probe",
                "message": f"{n}:{i} {TEXT}",
            })

    threads = [threading.Thread(target=run, args=(n,)) for n in range(THREADS)]
    # Switch threads as often as the interpreter can, so writers overlap.
    interval = sys.getswitchinterval()
    real_stdout, sys.stdout = sys.stdout, stdout
    sys.setswitchinterval(1e-6)
    try:
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    finally:
        sys.setswitchinterval(interval)
        sys.stdout = real_stdout


def _broken(lines: list[str]) -> list[str]:
    bad = []
    for line in lines:
        try:
            json.loads(line)
        except ValueError:
            bad.append(line[:60] + "..." + line[-20:])
    return bad


def test_every_stdout_line_is_one_whole_event(stdout_pipe):
    stdout, close = stdout_pipe
    _emit_at_once(stdout, lambda n: f"session-{n}")

    lines = [line for line in close().split("\n") if line]

    bad = _broken(lines)
    assert not bad, f"{len(bad)} of {len(lines)} stdout lines are not one JSON event: {bad[:2]}"
    assert len(lines) == THREADS * PER_THREAD


def test_the_session_mirror_and_debug_log_keep_every_row_whole(stdout_pipe, tmp_path):
    stdout, close = stdout_pipe
    _emit_at_once(stdout, lambda n: "session-shared")
    close()
    fb._close_session_event_file("session-shared")

    for path in (tmp_path / "sessions" / "session-shared.events.jsonl", tmp_path / "bridge-events.jsonl"):
        lines = [line for line in path.read_text(encoding="utf-8").split("\n") if line]
        bad = _broken(lines)
        assert not bad, f"{path.name}: {len(bad)} of {len(lines)} rows are not one JSON event: {bad[:2]}"
        assert len(lines) == THREADS * PER_THREAD, path.name
