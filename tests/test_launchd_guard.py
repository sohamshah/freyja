"""Only the real user's Freyja may change launchd jobs.

launchd keeps one job registry per macOS user, whatever HOME a process
has. An e2e run with a temporary HOME once installed the scheduler job
pointing into its temporary folder; once the folder was deleted launchd
couldn't start the job, and `npm run rebuild` hung on `launchctl
kickstart`. See bridge/launchd_guard.py.

None of these tests call launchctl: the launchctl wrappers are replaced
with recorders.
"""

from __future__ import annotations

import os
import pwd
from pathlib import Path

import pytest

import bridge.launchd_guard as guard
from bridge.launchd_guard import launchd_changes_blocked

ACCOUNT_HOME = Path(pwd.getpwuid(os.getuid()).pw_dir)


def _as_real_user(monkeypatch):
    monkeypatch.delenv("FREYJA_NO_LAUNCHD", raising=False)
    monkeypatch.setenv("HOME", str(ACCOUNT_HOME))
    monkeypatch.setenv("FREYJA_HOME", str(ACCOUNT_HOME / ".freyja"))


def test_real_user_with_real_homes_may_change_launchd(monkeypatch):
    _as_real_user(monkeypatch)
    assert launchd_changes_blocked() is None


def test_temporary_home_is_blocked(monkeypatch, tmp_path):
    _as_real_user(monkeypatch)
    monkeypatch.setenv("HOME", str(tmp_path))
    assert "is not this account's home" in launchd_changes_blocked()


def test_temporary_freyja_home_is_blocked(monkeypatch, tmp_path):
    _as_real_user(monkeypatch)
    monkeypatch.setenv("FREYJA_HOME", str(tmp_path / ".freyja"))
    assert "is a temporary directory" in launchd_changes_blocked()


def test_explicit_opt_out_is_blocked(monkeypatch):
    _as_real_user(monkeypatch)
    monkeypatch.setenv("FREYJA_NO_LAUNCHD", "1")
    assert launchd_changes_blocked() == "FREYJA_NO_LAUNCHD is set"


def test_tests_never_change_launchd():
    # conftest sets FREYJA_NO_LAUNCHD for every test.
    assert launchd_changes_blocked() is not None


# ── scheduler daemon ───────────────────────────────────────────────────────

@pytest.fixture
def daemon(monkeypatch, tmp_path):
    """The daemon module with HOME in tmp_path and launchctl replaced by
    recorders. `launchd` is what `launchctl print` reports."""
    import subprocess

    import bridge.scheduler.daemon as d

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("FREYJA_HOME", str(tmp_path / ".freyja"))
    monkeypatch.setattr(d, "is_supported_platform", lambda: True)
    monkeypatch.setattr(
        d, "resolve_bridge_invocation",
        lambda: ("/opt/python/bin/python3", ["/opt/bridge/freyja_bridge.py", "--headless"]),
    )
    calls: list[str] = []
    launchd: dict = {}
    monkeypatch.setattr(d, "_launchctl_print", lambda _label: dict(launchd) or None)
    monkeypatch.setattr(d, "_launchctl_unload", lambda: calls.append("unload") or True)
    monkeypatch.setattr(d, "_launchctl_load", lambda: calls.append("load") or True)

    def _no_launchctl(*args, **kwargs):
        raise AssertionError(f"unexpected subprocess call: {args}")

    monkeypatch.setattr(subprocess, "run", _no_launchctl)
    return d, calls, launchd


def test_blocked_install_writes_nothing_and_never_calls_launchctl(daemon, tmp_path):
    d, calls, _ = daemon
    result = d.ensure_daemon_installed(reason="test")
    assert result["installed"] is False and "launchd_blocked" in result["reason"]
    assert calls == []
    assert not (tmp_path / "Library" / "LaunchAgents").exists()


def test_blocked_uninstall_leaves_the_job_alone(daemon):
    d, calls, _ = daemon
    assert d.uninstall_daemon()["uninstalled"] is False
    assert calls == []


def test_drifted_registration_is_replaced(daemon, monkeypatch):
    d, calls, launchd = daemon
    monkeypatch.setattr(guard, "launchd_changes_blocked", lambda: None)

    d.ensure_daemon_installed(reason="first")          # writes plist, loads it
    assert calls == ["load"]
    shim = str(d.shim_path())

    calls.clear()
    launchd.update(state="running", program=shim)
    d.ensure_daemon_installed(reason="boot")            # matches: nothing to do
    assert calls == []

    # Another process registered the label with a program that is gone.
    launchd.update(state="spawn scheduled", program="/tmp/freyja-e2e-x/scheduler-launcher")
    d.ensure_daemon_installed(reason="boot")
    assert calls == ["unload", "load"]


def test_launchctl_print_reads_the_jobs_own_state_and_program(monkeypatch):
    import subprocess

    import bridge.scheduler.daemon as d

    out = (
        "gui/501/com.freyja.scheduler = {\n"
        "\tstate = running\n"
        "\tprogram = /Users/x/Library/Application Support/Freyja/scheduler-launcher\n"
        "\tpid = 123\n"
        "\tendpoints = {\n"
        "\t\tstate = active\n"
        "\t}\n"
        "}\n"
    ).encode()
    monkeypatch.setattr(
        subprocess, "run",
        lambda *a, **k: type("R", (), {"returncode": 0, "stdout": out})(),
    )
    info = d._launchctl_print("com.freyja.scheduler")  # noqa: SLF001
    assert info == {
        "state": "running",
        "program": "/Users/x/Library/Application Support/Freyja/scheduler-launcher",
        "pid": 123,
    }


# ── gateway daemon ─────────────────────────────────────────────────────────

def test_gateway_install_refuses_when_blocked(monkeypatch, tmp_path):
    from bridge.gateway.setup import launchd

    monkeypatch.setenv("HOME", str(tmp_path))
    with pytest.raises(RuntimeError, match="refusing to change the gateway's launchd job"):
        launchd.install()
    with pytest.raises(RuntimeError):
        launchd.uninstall()
    assert not (tmp_path / "Library" / "LaunchAgents").exists()
