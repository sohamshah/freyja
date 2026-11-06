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

import asyncio
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
    built: list["_SlackAdapter"] = []

    def __init__(self, *, config: dict | None = None) -> None:
        self.config = config or {}
        self.listen: bool | None = None
        self.sent: list[tuple[str, str, str | None]] = []
        _SlackAdapter.built.append(self)

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


class _WebClient:
    def __init__(self, team: str, posted: list[tuple[str, str]]) -> None:
        self.team = team
        self.posted = posted

    async def chat_postMessage(self, **kwargs):
        self.posted.append((self.team, kwargs["channel"]))
        return {"ok": True, "ts": "1793000000.000200"}


async def test_a_scheduled_run_posts_with_its_own_workspace_s_token():
    # A send-only adapter has never seen an inbound event, so it knows no
    # channel's workspace and fell back to the first one: a job for a
    # second workspace posted with the wrong token.
    from bridge.gateway.platforms.slack import SlackAdapter

    posted: list[tuple[str, str]] = []
    adapter = SlackAdapter()
    adapter._app = object()  # connected, send-only
    adapter._team_clients = {
        "T1": _WebClient("T1", posted),
        "T2": _WebClient("T2", posted),
    }
    state = SimpleNamespace(gateway_runner=SimpleNamespace(adapters=[adapter]))
    spec = SlackSinkSpec(workspace_id="T2", chat_id="C2", include_metadata=False)
    job = SimpleNamespace(id="sched_2", name="digest")
    run = SimpleNamespace(run_id="run_2", iteration=0)
    ctx = SinkContext(state=state, outputs_dir=None, log=lambda *_a: None)

    await deliver_slack(spec, job, run, RunOutput(text="the digest"), ctx)

    # The banner, then the output.
    assert posted == [("T2", "C2"), ("T2", "C2")]


async def test_the_desktop_bridge_posts_through_a_send_only_adapter(monkeypatch, tmp_path):
    # The desktop bridge runs no gateway, so "Run now" from the app on a
    # job with a Slack sink, or any fire while the desktop bridge held
    # the owner lock, failed with "no Slack adapter available". It now
    # builds one send-only adapter from the gateway's bot token. It must
    # never listen: a second Socket Mode connection splits Slack's events
    # and both processes answer (f032cfb).
    import bridge.gateway.platforms.slack as slack_platform

    monkeypatch.setattr(slack_platform, "SlackAdapter", _SlackAdapter)
    monkeypatch.setattr(_SlackAdapter, "built", [])
    monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
    (tmp_path / ".env").write_text("SLACK_BOT_TOKEN=xoxb-from-gateway-env\n")
    desktop_state = SimpleNamespace(gateway_runner=None)

    assert await _deliver(desktop_state) == "1793000000.000100"
    assert await _deliver(desktop_state) == "1793000000.000100"

    [adapter] = _SlackAdapter.built
    assert adapter.listen is False
    assert adapter.config == {"bot_token": "xoxb-from-gateway-env"}
    assert len(adapter.sent) == 2
    assert not os.environ.get("SLACK_BOT_TOKEN")


class _AuthClient(_WebClient):
    def __init__(self, token: str) -> None:
        super().__init__("T1", [])

    async def auth_test(self):
        return {"ok": True, "team_id": "T1", "user_id": "UBOT", "team": "t", "user": "freyja"}


async def test_a_send_only_adapter_leaves_the_listener_s_thread_bindings_alone(monkeypatch):
    # slack-bindings.json belongs to the listening gateway, which reads it
    # to know the threads it answers in. A send-only adapter (scheduler
    # daemon, desktop bridge) loaded it at start and, after each post,
    # wrote its own stale copy back, so threads the gateway joined since
    # were gone when the gateway next restarted.
    import bridge.gateway.platforms.slack as slack_platform
    from bridge.gateway.slack_bindings import ThreadBindings

    monkeypatch.setattr(slack_platform, "AsyncApp", lambda **_kw: object())
    monkeypatch.setattr(slack_platform, "AsyncWebClient", _AuthClient)
    sender = slack_platform.SlackAdapter(config={"bot_token": "xoxb-1"})

    async def _no_inbound(_message) -> None:
        return None

    assert await sender.connect(_no_inbound, listen=False)
    listener = ThreadBindings(flush_delay=0)
    listener.add_thread("1793000000.000300")
    listener.save()

    sender.bindings.flush_delay = 0
    result = await sender.send("C1", "the digest")
    assert result.ok
    await asyncio.sleep(0.05)

    assert ThreadBindings().has_thread("1793000000.000300")
