"""The Slack sink posts a scheduled run's output through the gateway's
Slack adapter.

The sink looked for the adapter in ``state.platform_adapters``, which
nothing fills, and then in ``state.gateway_runner._adapters``. The
gateway keeps its adapters in ``adapters``, and only the headless
scheduler daemon set ``gateway_runner``; the gateway process, which
usually owns the tick loop, never did. Every Slack-delivered run failed
with "no Slack adapter available".
"""

from __future__ import annotations

import os
import sys
from types import ModuleType, SimpleNamespace

import pytest

import bridge.gateway.run as run_mod
from bridge.gateway.run import GatewayDaemon
from bridge.scheduler.models import SlackSinkSpec
from bridge.scheduler.sinks import RunOutput, SinkContext
from bridge.scheduler.sinks.slack import deliver_slack


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    import bridge.transcript_persistence as tp

    monkeypatch.setenv("FREYJA_HOME", str(tmp_path))
    monkeypatch.setattr(tp, "SESSIONS_DIR", tmp_path / "sessions")
    telemetry = ModuleType("bridge.compaction_telemetry")
    telemetry.append_telemetry = lambda *_a, **_k: None
    monkeypatch.setitem(sys.modules, "bridge.compaction_telemetry", telemetry)


class _Scheduler:
    async def start(self) -> None:
        return None


class _State:
    def __init__(self, **_: object) -> None:
        self.sessions: dict = {}
        self.scheduler = _Scheduler()
        self.talk_wake_hook_factory = None


class _SlackAdapter:
    name = "slack"

    def __init__(self) -> None:
        self.listen: bool | None = None
        self.sent: list[tuple[str, str, str | None]] = []

    async def connect(self, _on_inbound, *, listen: bool = True) -> bool:
        self.listen = listen
        return True

    async def send(self, chat_id: str, content: str, *, thread_id=None, **_kw):
        self.sent.append((chat_id, content, thread_id))
        return SimpleNamespace(ok=True, message_id="1793000000.000100")


async def _start_gateway(monkeypatch, **start_kwargs) -> GatewayDaemon:
    import bridge.freyja_bridge as fb
    import bridge.gateway.approval as approval
    import bridge.gateway.config as gcfg
    import bridge.gateway.mcp_slack as mcp_slack
    import bridge.gateway.pid as pid_mod

    monkeypatch.setattr(fb, "_BridgeState", _State)
    monkeypatch.setattr(
        gcfg.GatewayConfig, "load",
        classmethod(lambda cls: SimpleNamespace(
            default_model="m", default_reasoning_level="low")),
    )
    monkeypatch.setattr(run_mod, "SlackAdapter", _SlackAdapter)
    monkeypatch.setattr(
        mcp_slack, "ensure_manager",
        lambda _d: SimpleNamespace(server_count=0),
    )
    monkeypatch.setattr(pid_mod, "get_running_pid", lambda: os.getpid())
    monkeypatch.setattr(approval, "_ADAPTERS", {})
    daemon = GatewayDaemon()

    async def _noop(*_a, **_k):
        return None

    monkeypatch.setattr(daemon, "_start_control_channel", _noop)
    monkeypatch.setattr(daemon, "_sweep_orphaned_permission_requests", lambda: None)
    await daemon.start(**start_kwargs)
    return daemon


async def _deliver(state) -> str | None:
    spec = SlackSinkSpec(
        workspace_id="T1", chat_id="C1", thread_ts="1793000000.000001",
        include_banner=False, include_metadata=False,
    )
    job = SimpleNamespace(id="sched_1", name="digest")
    run = SimpleNamespace(run_id="run_1", iteration=0)
    ctx = SinkContext(state=state, outputs_dir=None, log=lambda *_a: None)
    return await deliver_slack(spec, job, run, RunOutput(text="the digest"), ctx)


@pytest.mark.parametrize(
    "start_kwargs",
    [
        pytest.param({}, id="gateway process"),
        pytest.param({"listen": False}, id="send-only scheduler daemon"),
    ],
)
async def test_a_scheduled_run_posts_through_the_gateway_s_slack_adapter(
    monkeypatch, start_kwargs
):
    daemon = await _start_gateway(monkeypatch, **start_kwargs)
    adapter = daemon.adapters[0]

    ts = await _deliver(daemon.state)

    assert ts == "1793000000.000100"
    assert adapter.sent == [("C1", "the digest", "1793000000.000001")]
    assert adapter.listen is start_kwargs.get("listen", True)
