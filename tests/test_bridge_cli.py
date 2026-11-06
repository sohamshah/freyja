"""`freyja_bridge.py --help` prints usage and starts nothing.

main() looked for its two flags in ``sys.argv`` and ignored every other
argument, so ``--help`` (or a typo) fell through to a full interactive
bridge against ~/.freyja: it wrote event files, re-saved the scheduler
LaunchAgent plist and opened MCP sessions. These tests call main() with
both bridge loops replaced; they never run the script.
"""

from __future__ import annotations

import pytest

import bridge.freyja_bridge as fb


@pytest.fixture
def started(monkeypatch):
    calls: list[str] = []

    async def interactive() -> None:
        calls.append("interactive")

    async def headless() -> None:
        calls.append("headless")

    monkeypatch.setattr(fb, "_main", interactive)
    monkeypatch.setattr(fb, "_main_headless", headless)
    monkeypatch.delenv("FREYJA_HEADLESS", raising=False)
    return calls


def _run(monkeypatch, *args: str) -> None:
    monkeypatch.setattr(fb.sys, "argv", ["freyja_bridge.py", *args])
    fb.main()


def test_help_prints_usage_and_starts_nothing(monkeypatch, started, capsys):
    with pytest.raises(SystemExit) as exc:
        _run(monkeypatch, "--help")
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "usage:" in out
    assert "--headless" in out and "--scheduler-only" in out
    assert started == []


def test_an_unknown_flag_exits_with_usage_and_starts_nothing(
    monkeypatch, started, capsys
):
    with pytest.raises(SystemExit) as exc:
        _run(monkeypatch, "--hepl")
    assert exc.value.code == 2
    assert "usage:" in capsys.readouterr().err
    assert started == []


def test_the_known_flags_still_pick_the_loop(monkeypatch, started):
    _run(monkeypatch)
    _run(monkeypatch, "--headless", "--scheduler-only")
    _run(monkeypatch, "--scheduler-only")
    assert started == ["interactive", "headless", "headless"]
