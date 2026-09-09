"""stdio_watchdog + pid-file sweep tests (design 4.3).

The watchdog is exercised as a real subprocess against a scripted parent
stand-in (a plain sleeper process) — never the real bridge. All pid
files live under tmp_path; ~/.freyja/mcp-run is never touched.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

from bridge.mcp.pidfiles import (
    pid_file_path,
    process_cmdline,
    read_pid_file,
    sweep_stale_pidfiles,
    write_pid_file,
)
from bridge.mcp.stdio_watchdog import WATCHDOG_SCRIPT, wrap_with_watchdog

SLEEPER = [sys.executable, "-c", "import time; time.sleep(120)"]


def _wait_until(predicate, timeout_s: float, interval_s: float = 0.05) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval_s)
    return predicate()


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _spawn_watchdog(
    tmp_path: Path,
    *,
    parent_pid: int,
    child_cmd: list[str],
    poll: float = 0.1,
) -> tuple[subprocess.Popen, Path]:
    pid_file = tmp_path / "watch.pid"
    proc = subprocess.Popen(
        [
            sys.executable, str(WATCHDOG_SCRIPT),
            "--parent-pid", str(parent_pid),
            "--poll-interval", str(poll),
            "--pid-file", str(pid_file),
            "--", *child_cmd,
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return proc, pid_file


def _child_pid(pid_file: Path, timeout_s: float = 10.0) -> int:
    assert _wait_until(pid_file.exists, timeout_s), "pid file never written"
    record = read_pid_file(pid_file)
    assert record is not None
    return record["pid"]


# ---------------------------------------------------------------------------
# Watchdog behavior
# ---------------------------------------------------------------------------

def test_parent_death_kills_child_group(tmp_path):
    parent = subprocess.Popen(SLEEPER)
    watchdog = None
    try:
        watchdog, pid_file = _spawn_watchdog(
            tmp_path, parent_pid=parent.pid, child_cmd=SLEEPER
        )
        child_pid = _child_pid(pid_file)
        assert _alive(child_pid)

        parent.kill()
        parent.wait(timeout=10)

        assert _wait_until(lambda: not _alive(child_pid), 5), (
            "server survived parent death"
        )
        assert watchdog.wait(timeout=5) == 1  # parent-death exit code
    finally:
        parent.kill()
        if watchdog is not None and watchdog.poll() is None:
            watchdog.kill()


def test_child_exit_code_propagates(tmp_path):
    watchdog, _pid_file = _spawn_watchdog(
        tmp_path,
        parent_pid=os.getpid(),  # this test process stays alive
        child_cmd=[sys.executable, "-c", "import sys; sys.exit(7)"],
    )
    assert watchdog.wait(timeout=10) == 7


def test_sigterm_to_watchdog_kills_server_group(tmp_path):
    # This is the clean-shutdown path the MCP SDK drives (terminate on
    # context exit): SIGTERM to the watchdog must take the server down.
    watchdog, pid_file = _spawn_watchdog(
        tmp_path, parent_pid=os.getpid(), child_cmd=SLEEPER
    )
    child_pid = _child_pid(pid_file)
    assert _alive(child_pid)

    watchdog.terminate()
    assert watchdog.wait(timeout=10) is not None
    assert _wait_until(lambda: not _alive(child_pid), 5), (
        "server survived watchdog SIGTERM"
    )


def test_pid_file_contents_and_wrap_helper(tmp_path):
    command, args = wrap_with_watchdog(
        "/usr/bin/true", ["--flag"], parent_pid=1234,
        poll_interval_s=0.5, pid_file=tmp_path / "x.pid",
    )
    assert command == sys.executable
    assert args[0] == str(WATCHDOG_SCRIPT)
    assert "--parent-pid" in args and "1234" in args
    assert "--pid-file" in args
    sep = args.index("--")
    assert args[sep + 1:] == ["/usr/bin/true", "--flag"]

    watchdog, pid_file = _spawn_watchdog(
        tmp_path, parent_pid=os.getpid(), child_cmd=SLEEPER
    )
    try:
        child_pid = _child_pid(pid_file)
        record = json.loads(pid_file.read_text())
        assert record["pid"] == child_pid
        assert record["owner_pid"] == os.getpid()
        assert record["watchdog_pid"] == watchdog.pid
        assert "time.sleep(120)" in record["cmdline"]
    finally:
        watchdog.terminate()
        watchdog.wait(timeout=10)


# ---------------------------------------------------------------------------
# PID-file sweep
# ---------------------------------------------------------------------------

def _spawn_dummy() -> subprocess.Popen:
    # Own session so killpg in the sweep can only ever hit the dummy.
    return subprocess.Popen(SLEEPER, start_new_session=True)


def test_sweep_kills_stale_pid_with_matching_cmdline(tmp_path):
    run_dir = tmp_path / "mcp-run"
    dummy = _spawn_dummy()
    try:
        live_cmd = process_cmdline(dummy.pid)
        assert live_cmd is not None
        path = pid_file_path(run_dir, "stale")
        # No owner_pid recorded -> treated as orphaned (dead bridge).
        write_pid_file(path, dummy.pid, live_cmd)

        killed = sweep_stale_pidfiles(run_dir)

        assert dummy.pid in killed
        assert not path.exists()
        assert _wait_until(lambda: not _alive(dummy.pid) or dummy.poll() is not None, 5)
    finally:
        if dummy.poll() is None:
            dummy.kill()
        dummy.wait(timeout=5)


def test_sweep_spares_pid_with_mismatched_cmdline(tmp_path):
    run_dir = tmp_path / "mcp-run"
    dummy = _spawn_dummy()
    try:
        path = pid_file_path(run_dir, "reused")
        write_pid_file(path, dummy.pid, "totally-different-server --serve")

        killed = sweep_stale_pidfiles(run_dir)

        assert killed == []
        assert not path.exists()  # stale file still removed
        assert _alive(dummy.pid)  # process spared (pid reuse guard)
    finally:
        dummy.kill()
        dummy.wait(timeout=5)


def test_sweep_skips_servers_owned_by_a_live_bridge(tmp_path):
    run_dir = tmp_path / "mcp-run"
    owner = _spawn_dummy()   # stand-in for another live bridge process
    server = _spawn_dummy()
    try:
        live_cmd = process_cmdline(server.pid)
        path = pid_file_path(run_dir, "owned")
        write_pid_file(path, server.pid, live_cmd, owner_pid=owner.pid)

        killed = sweep_stale_pidfiles(run_dir)
        assert killed == []
        assert path.exists()  # left alone entirely
        assert _alive(server.pid)

        # Owner dies -> next sweep reclaims the server.
        owner.kill()
        owner.wait(timeout=5)
        killed = sweep_stale_pidfiles(run_dir)
        assert server.pid in killed
        assert not path.exists()
    finally:
        for proc in (owner, server):
            if proc.poll() is None:
                proc.kill()
            proc.wait(timeout=5)


async def test_manager_start_sweeps_run_dir(tmp_path):
    from bridge.mcp.config import McpCatalog
    from bridge.mcp.manager import McpManager

    run_dir = tmp_path / "mcp-run"
    dummy = _spawn_dummy()
    try:
        live_cmd = process_cmdline(dummy.pid)
        path = pid_file_path(run_dir, "leftover")
        write_pid_file(path, dummy.pid, live_cmd)

        manager = McpManager(McpCatalog(), run_dir=run_dir, watchdog=False)
        await manager.start()

        assert not path.exists()
        assert _wait_until(lambda: not _alive(dummy.pid) or dummy.poll() is not None, 5)
        await manager.stop()
    finally:
        if dummy.poll() is None:
            dummy.kill()
        dummy.wait(timeout=5)


def test_sweep_ignores_garbage_and_missing_dir(tmp_path):
    assert sweep_stale_pidfiles(tmp_path / "does-not-exist") == []
    run_dir = tmp_path / "mcp-run"
    run_dir.mkdir()
    (run_dir / "junk.pid").write_text("not json", encoding="utf-8")
    (run_dir / "wrongshape.pid").write_text('{"pid": "nope"}', encoding="utf-8")
    assert sweep_stale_pidfiles(run_dir) == []
    assert list(run_dir.glob("*.pid")) == []
