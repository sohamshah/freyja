"""The scheduler LaunchAgent must come up with the same keyring as the gateway.

Provider keys and platform tokens live in ``~/.freyja/.env``, not in the
LaunchAgent plist (which carries only FREYJA_HOME, FREYJA_HEADLESS, PATH).
``_load_env_into_os_environ`` merges that file into ``os.environ`` — but it was
called from exactly one place, ``run._async_main``, and that is not the only
way a GatewayDaemon gets built.

The scheduler agent runs ``freyja_bridge.py --headless --scheduler-only``,
which reaches ``_main_headless`` → ``GatewayDaemon()`` → ``start()`` and never
touches ``_async_main``. So it booted with an empty keyring:

    [slack] SLACK_BOT_TOKEN not set — run `freyja setup slack`
    ...
    ValueError: ANTHROPIC_API_KEY is not set     <- outcome_classifier.py:215

Harmless while the interactive gateway holds the scheduler lock, because that
process has its env. It stops being harmless the moment the standalone daemon
is the one running the jobs — which is the entire reason it exists.

The load now happens in ``start()``, the choke point every caller passes
through.
"""

from __future__ import annotations

import ast
import inspect
import pathlib

import pytest

from bridge.gateway.run import GatewayDaemon, _load_env_into_os_environ

BRIDGE = pathlib.Path(__file__).resolve().parent.parent / "bridge"


# ── the loader's contract ────────────────────────────────────────────

def test_missing_keys_are_filled_from_the_env_file(monkeypatch):
    monkeypatch.setattr(
        "bridge.gateway.run.read_env",
        lambda: {"ANTHROPIC_API_KEY": "from-file", "SLACK_BOT_TOKEN": "tok"},
    )
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)

    _load_env_into_os_environ()

    import os
    assert os.environ["ANTHROPIC_API_KEY"] == "from-file"
    assert os.environ["SLACK_BOT_TOKEN"] == "tok"


def test_an_existing_env_var_is_never_clobbered(monkeypatch):
    """Operators override per-launch; the file must not win over that.
    This is also what makes the call idempotent, which is why _async_main
    calling it earlier is a no-op rather than a conflict."""
    monkeypatch.setattr(
        "bridge.gateway.run.read_env", lambda: {"ANTHROPIC_API_KEY": "from-file"}
    )
    monkeypatch.setenv("ANTHROPIC_API_KEY", "explicit")

    _load_env_into_os_environ()
    _load_env_into_os_environ()  # twice — must still be the explicit value

    import os
    assert os.environ["ANTHROPIC_API_KEY"] == "explicit"


def test_loader_survives_a_missing_env_file(monkeypatch):
    monkeypatch.setattr("bridge.gateway.run.read_env", dict)
    _load_env_into_os_environ()  # must not raise


# ── it is wired at the choke point ───────────────────────────────────

def test_start_loads_env_before_building_state():
    """Ordering matters: _BridgeState construction and adapter connect
    both read provider keys, so the load has to precede them."""
    src = inspect.getsource(GatewayDaemon.start)
    assert "_load_env_into_os_environ()" in src

    load_at = src.index("_load_env_into_os_environ()")
    state_at = src.index("_BridgeState(")
    assert load_at < state_at, "env must load before _BridgeState is built"


def test_headless_entrypoint_reaches_start():
    """_main_headless must go through GatewayDaemon.start() — that is what
    makes the fix cover it. If it ever builds state directly, the daemon is
    back to an empty keyring and this test should fail loudly."""
    src = (BRIDGE / "freyja_bridge.py").read_text()
    tree = ast.parse(src)
    fn = next(
        n for n in ast.walk(tree)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
        and n.name == "_main_headless"
    )
    body = ast.unparse(fn)
    assert "GatewayDaemon()" in body
    # Send-only: the dedicated gateway owns inbound Slack. A listening
    # scheduler daemon made both processes answer every mention.
    assert "gateway.start(listen=False)" in body


def test_headless_does_not_log_a_workspace_it_does_not_use():
    """It used to log os.getcwd() — the app bundle, because the launcher
    cd's there for the bundled Python. start() actually uses Path.home(),
    so the line described a directory the daemon never ran in."""
    tree = ast.parse((BRIDGE / "freyja_bridge.py").read_text())
    fn = next(
        n for n in ast.walk(tree)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
        and n.name == "_main_headless"
    )
    body = ast.unparse(fn)
    assert "os.getcwd()" not in body
