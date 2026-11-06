"""Another process never reads a half-written gateway PID file.

Every process asks the PID file whether a gateway runs. A gateway starting
over an old PID file (a takeover, or a start after a crash) truncated the
file and then wrote its PID, so a read in between found it empty and saw no
gateway: the app refused messages typed for Slack sessions, and sent a
talk for a Slack thread to the thread's inbox file just as the gateway came
up to load it.
"""

from __future__ import annotations

import os
import threading

import pytest

from bridge.gateway import pid as gp


@pytest.fixture(autouse=True)
def _home(monkeypatch, tmp_path):
    monkeypatch.setenv("FREYJA_HOME", str(tmp_path))


def test_a_reader_never_finds_the_pid_file_empty_while_it_is_written():
    gp.write_pid(os.getpid())
    seen: list[int | None] = []
    stop = threading.Event()

    def _read() -> None:
        while not stop.is_set():
            seen.append(gp.get_running_pid())

    reader = threading.Thread(target=_read)
    reader.start()
    try:
        for _ in range(2000):
            gp.write_pid(os.getpid())
    finally:
        stop.set()
        reader.join()

    assert seen and None not in seen


def test_the_pid_file_holds_only_the_pid():
    gp.write_pid(4242)
    assert gp.pid_path().read_text(encoding="utf-8") == "4242"
    assert [p.name for p in gp.freyja_home().iterdir() if p.name.startswith(".gateway.pid")] == [".gateway.pid"]
