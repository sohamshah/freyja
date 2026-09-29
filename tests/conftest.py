import os

import pytest


@pytest.fixture(autouse=True)
def _isolated_freyja_home(tmp_path_factory, monkeypatch):
    # Every Freyja code path that persists operator state (MCP OAuth tokens,
    # catalog overrides, scheduler data) resolves through FREYJA_HOME. Without
    # this guard a test that forgets to inject a root writes into the real
    # ~/.freyja — which happened once with an OAuth client registration.
    if "FREYJA_HOME" not in os.environ:
        monkeypatch.setenv("FREYJA_HOME", str(tmp_path_factory.mktemp("freyja-home")))
    # launchd has one job registry per macOS user, so a test that installs
    # a LaunchAgent replaces the real Freyja job (bridge/launchd_guard.py).
    monkeypatch.setenv("FREYJA_NO_LAUNCHD", "1")
    yield
