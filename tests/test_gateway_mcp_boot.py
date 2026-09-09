"""GatewayDaemon.start() must bring up the process-level McpManager before
the Slack adapter connects, so the first inbound message's session registry
already carries mcp__* tools (previously the manager only appeared on the
first /mcp command)."""
from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import bridge.gateway.run as run_mod  # noqa: E402
from bridge.gateway.run import GatewayDaemon  # noqa: E402


class _Scheduler:
    async def start(self) -> None:
        return None


class _FakeState:
    def __init__(self, **_: object) -> None:
        self.sessions: dict = {}
        self.scheduler = _Scheduler()
        self.talk_wake_hook_factory = None


@pytest.mark.asyncio
async def test_start_boots_mcp_manager_before_adapter_connect(monkeypatch):
    order: list[str] = []

    class _Adapter:
        name = "slack"

        async def connect(self, _on_inbound) -> bool:
            order.append("adapter.connect")
            return False  # no adapters → start() proceeds to control channel

    manager = types.SimpleNamespace(server_count=2)

    def fake_ensure_manager(daemon):
        order.append("ensure_manager")
        daemon.state.mcp_manager = manager
        return manager

    import bridge.freyja_bridge as fb
    import bridge.gateway.config as gcfg
    import bridge.gateway.mcp_slack as mcp_slack

    monkeypatch.setattr(fb, "_BridgeState", _FakeState)
    monkeypatch.setattr(
        gcfg.GatewayConfig, "load",
        classmethod(lambda cls: types.SimpleNamespace(
            default_model="m", default_reasoning_level="low")),
    )
    monkeypatch.setattr(run_mod, "SlackAdapter", _Adapter)
    monkeypatch.setattr(mcp_slack, "ensure_manager", fake_ensure_manager)

    daemon = GatewayDaemon()

    async def _noop(*_a, **_k):
        return None

    monkeypatch.setattr(daemon, "_start_control_channel", _noop)
    monkeypatch.setattr(daemon, "_sweep_orphaned_permission_requests", lambda: None)
    for name in dir(daemon):
        if name.startswith("_drain") or name.startswith("_report"):
            monkeypatch.setattr(daemon, name, _noop, raising=False)

    await daemon.start()

    assert order[:2] == ["ensure_manager", "adapter.connect"]
    assert daemon.state.mcp_manager is manager


@pytest.mark.asyncio
async def test_start_survives_mcp_manager_boot_failure(monkeypatch, caplog):
    class _Adapter:
        name = "slack"

        async def connect(self, _on_inbound) -> bool:
            return False

    import bridge.freyja_bridge as fb
    import bridge.gateway.config as gcfg
    import bridge.gateway.mcp_slack as mcp_slack

    monkeypatch.setattr(fb, "_BridgeState", _FakeState)
    monkeypatch.setattr(
        gcfg.GatewayConfig, "load",
        classmethod(lambda cls: types.SimpleNamespace(
            default_model="m", default_reasoning_level="low")),
    )
    monkeypatch.setattr(run_mod, "SlackAdapter", _Adapter)

    def boom(_daemon):
        raise RuntimeError("mcp.json unreadable")

    monkeypatch.setattr(mcp_slack, "ensure_manager", boom)
    daemon = GatewayDaemon()

    async def _noop(*_a, **_k):
        return None

    monkeypatch.setattr(daemon, "_start_control_channel", _noop)
    monkeypatch.setattr(daemon, "_sweep_orphaned_permission_requests", lambda: None)

    await daemon.start()  # must not raise
    assert "mcp manager boot failed" in caplog.text
