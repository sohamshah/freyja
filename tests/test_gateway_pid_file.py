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


def test_a_reader_that_read_a_dead_pid_leaves_the_new_gateways_pid_file(monkeypatch):
    """A takeover: a reader reads the old gateway's PID, and the new gateway
    writes its own before the reader finds the old one dead."""
    gp.write_pid(4242)
    alive = gp._process_alive

    def _dead_after_takeover(pid: int) -> bool:
        if pid == 4242:
            gp.write_pid(os.getpid())
            return False
        return alive(pid)

    monkeypatch.setattr(gp, "_process_alive", _dead_after_takeover)
    assert gp.get_running_pid() is None
    assert gp.pid_path().read_text(encoding="utf-8") == str(os.getpid())


def test_the_gateway_knows_it_holds_the_lock_whatever_the_pid_file_says():
    import bridge.freyja_bridge as fb
    from bridge.inbox import InboxMessage
    from bridge.tools.talk_tool import TalkRouter

    slack = "freyja:slack:T1:channel:C1:1.0"
    router = TalkRouter(
        bridge_state=None, get_running_sessions=dict,
        resolve_archived_sub=lambda _sid: None, wake_archived_sub=lambda _sid, _msg: None,
    )
    msg = InboxMessage(id="m1", from_session="s", from_label="s", from_role="agent", content="hi")
    assert gp.acquire_lock()
    try:
        gp.pid_path().unlink()
        assert fb._process_owns_gateway()
        assert not fb._gateway_holds(slack)
        assert router.forward_cross_process(slack, msg, live_local=True, archived_state=None) is None
        gp.write_pid(4242)
        assert fb._process_owns_gateway()
    finally:
        gp.release_lock()
    assert not fb._process_owns_gateway()


def test_other_processes_see_the_apps_bridge_while_it_holds_its_lock(monkeypatch):
    import subprocess
    import sys

    monkeypatch.setattr(gp, "_desktop_lock", None)
    probe = [sys.executable, "-c", "from bridge.gateway import pid; print(pid.desktop_bridge_runs())"]

    def _runs_elsewhere() -> str:
        return subprocess.run(probe, capture_output=True, text=True, check=True).stdout.strip()

    assert _runs_elsewhere() == "False" and not gp.holds_desktop_lock()
    assert gp.hold_desktop_lock()
    try:
        assert gp.holds_desktop_lock() and gp.desktop_bridge_runs()
        assert _runs_elsewhere() == "True"
    finally:
        gp._desktop_lock.close()


def test_a_gateway_that_starts_beside_another_does_not_take_the_lock_too(monkeypatch):
    """Two gateways starting at the same moment both read no PID file, and
    each wrote its own: both ran, both read Slack and the control channel,
    and both saved every thread."""
    import subprocess
    import sys

    other = subprocess.Popen(
        [sys.executable, "-c", "import sys; from bridge.gateway import pid; print(pid.acquire_lock(), flush=True); sys.stdin.read()"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
    )
    try:
        assert other.stdout.readline().strip() == "True"
        # This one read the PID file before the other wrote it.
        monkeypatch.setattr(gp, "get_running_pid", lambda: None)
        assert not gp.acquire_lock()
        assert not gp.holds_lock()
    finally:
        other.stdin.close()
        other.wait(timeout=10)
    assert gp.acquire_lock()
    gp.release_lock()


def test_a_gateway_started_with_replace_takes_the_lock_once_the_old_one_is_gone():
    import subprocess
    import sys

    other = subprocess.Popen(
        [sys.executable, "-c", "import sys; from bridge.gateway import pid; print(pid.acquire_lock(), flush=True); sys.stdin.read()"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
    )
    reaper = threading.Thread(target=other.wait)
    try:
        assert other.stdout.readline().strip() == "True"
        reaper.start()
        assert gp.acquire_lock(replace=True)
        assert gp.holds_lock() and gp.get_running_pid() == os.getpid()
    finally:
        gp.release_lock()
        if other.returncode is None:
            other.kill()
        reaper.join(timeout=10)
