"""The scheduler daemon keeps daemon.log bounded, and keeps every line.

The launcher shim appends the daemon's stdout and stderr to daemon.log:
every event of the scheduler sessions it runs, and its log lines. Nothing
rotated it. The gateway copies its own log's last 8 MB to gateway.log.1 and
empties the log, dropping the rest and the .1 before: the user's rule is to
compress old logs, never delete them.
"""

from __future__ import annotations

import asyncio
import gzip
import logging

import bridge.scheduler.daemon as daemon
from bridge.scheduler.daemon import daemon_log_path, rotate_daemon_log_if_large


def _write(lines: list[str]) -> None:
    with daemon_log_path().open("a") as f:
        f.writelines(f"{line}\n" for line in lines)


def _kept() -> str:
    """Every line the log, its last segment and the gzipped ones hold, oldest first."""
    logs = daemon_log_path().parent
    old = sorted(logs.glob("daemon.log.*.gz"), key=lambda p: p.stat().st_mtime_ns)
    return "".join(
        [gzip.decompress(p.read_bytes()).decode() for p in old]
        + [(logs / "daemon.log.1").read_text(), daemon_log_path().read_text()]
    )


def test_a_small_log_is_left_alone():
    _write(["one", "two"])

    assert not rotate_daemon_log_if_large(max_bytes=1024)

    assert daemon_log_path().read_text() == "one\ntwo\n"
    assert not (daemon_log_path().parent / "daemon.log.1").exists()


def test_a_large_log_is_copied_whole_to_its_segment_and_emptied_in_place():
    lines = [f"line {n}" for n in range(200)]
    _write(lines)
    inode = daemon_log_path().stat().st_ino

    assert rotate_daemon_log_if_large(max_bytes=1024)

    # Each byte at the offset it had in the log, so a tail reads on there.
    assert (daemon_log_path().parent / "daemon.log.1").read_text() == "".join(f"{l}\n" for l in lines)
    # The shim holds the log open for appending: emptied, never moved.
    assert daemon_log_path().read_text() == ""
    assert daemon_log_path().stat().st_ino == inode


def test_older_segments_are_gzipped_and_no_line_is_dropped():
    written: list[str] = []
    for round_ in range(3):
        lines = [f"round {round_} line {n}" for n in range(200)]
        written += lines
        _write(lines)
        assert rotate_daemon_log_if_large(max_bytes=1024)
    _write(["after"])

    assert len(list(daemon_log_path().parent.glob("daemon.log.*.gz"))) == 2
    assert _kept() == "".join(f"{l}\n" for l in written + ["after"])


def test_the_writers_locks_are_held_from_the_copy_through_the_emptying():
    _write([f"line {n}" for n in range(200)])
    seen: list[tuple[str, bool, int]] = []
    segment = daemon_log_path().parent / "daemon.log.1"

    class Lock:
        def __enter__(self):
            seen.append(("held", segment.exists(), daemon_log_path().stat().st_size))

        def __exit__(self, *_a):
            seen.append(("released", segment.exists(), daemon_log_path().stat().st_size))

    assert rotate_daemon_log_if_large(max_bytes=1024, locks=[Lock()])

    assert seen[0][:2] == ("held", False) and seen[0][2] > 1024
    assert seen[1] == ("released", True, 0)


async def test_the_headless_daemon_rotates_its_log(monkeypatch, capfd):
    import bridge.freyja_bridge as fb
    import bridge.gateway.run as gateway_run

    class _Gateway:
        state = None

        async def start(self, *, listen=None):
            pass

    started: list[tuple] = []

    def rotation(*locks):
        started.append(locks)
        return asyncio.sleep(0)

    monkeypatch.setattr(fb, "emit", lambda _event: None)
    monkeypatch.setattr(gateway_run, "GatewayDaemon", _Gateway)
    monkeypatch.setattr(daemon, "rotate_daemon_log_periodically", rotation)
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    try:
        await fb._main_headless()
        added = [h for h in root.handlers if h not in handlers]
    finally:
        for handler in root.handlers[:]:
            if handler not in handlers:
                root.removeHandler(handler)
        root.setLevel(level)

    [locks] = started
    # The log handler that writes to it, then emit()'s.
    assert locks[-1] is fb._EMIT_LOCK
    assert added and all(h.lock in locks for h in added)
