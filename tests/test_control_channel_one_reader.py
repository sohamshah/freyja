"""Only the gateway reads the control channel.

The scheduler daemon builds a send-only GatewayDaemon too. Booted while
no gateway ran, it started reading commands.jsonl and never stopped, so
once the gateway started both read the one file: each command (a stop,
a message typed for a Slack thread, a scheduled fire handed over) ran in
the daemon as well, where every Slack session is only a copy and the
command is dropped or lands in a file the gateway never wakes for.
"""

from __future__ import annotations

import asyncio
import os

from bridge.gateway.control_channel import ControlChannelReader, append_command

SLACK = "freyja:slack:T1:channel:C1:1.0"


async def test_the_scheduler_daemon_never_reads_the_gateways_commands(monkeypatch):
    import bridge.gateway.pid as gp
    from bridge.gateway.run import GatewayDaemon

    monkeypatch.setattr(ControlChannelReader, "POLL_INTERVAL_SEC", 0.01)
    handled: list[tuple[str, str]] = []

    async def _on_cancel_turn(self, cmd):
        handled.append((self.name, cmd["turnId"]))

    monkeypatch.setattr(GatewayDaemon, "_on_cancel_turn", _on_cancel_turn)
    owner: dict[str, int | None] = {"pid": None}
    monkeypatch.setattr(gp, "get_running_pid", lambda: owner["pid"])

    # The scheduler daemon boots first, while no gateway runs.
    daemon = GatewayDaemon()
    daemon.name = "scheduler daemon"
    await daemon._start_control_channel()
    # Then the gateway takes the lock and starts.
    owner["pid"] = os.getpid()
    gateway = GatewayDaemon()
    gateway.name = "gateway"
    await gateway._start_control_channel()
    try:
        turns = [f"turn-{n}" for n in range(5)]
        for turn in turns:
            append_command({"type": "cancel_turn", "sessionId": SLACK, "turnId": turn})
        for _ in range(100):
            if len(handled) >= len(turns):
                break
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.1)
    finally:
        for reader in (daemon.control_channel, gateway.control_channel):
            if reader is not None:
                await reader.stop()

    assert handled == [("gateway", turn) for turn in turns]
    assert daemon.control_channel is None


async def test_commands_sent_while_no_gateway_runs_wait_for_it(monkeypatch):
    import bridge.gateway.pid as gp
    from bridge.gateway.run import GatewayDaemon

    monkeypatch.setattr(ControlChannelReader, "POLL_INTERVAL_SEC", 0.01)
    handled: list[str] = []

    async def _on_cancel_turn(self, cmd):
        handled.append(cmd["turnId"])

    monkeypatch.setattr(GatewayDaemon, "_on_cancel_turn", _on_cancel_turn)
    owner: dict[str, int | None] = {"pid": None}
    monkeypatch.setattr(gp, "get_running_pid", lambda: owner["pid"])

    # A gateway ran once, so the channel has a saved offset.
    owner["pid"] = os.getpid()
    first = GatewayDaemon()
    await first._start_control_channel()
    await first.control_channel.stop()
    owner["pid"] = None

    daemon = GatewayDaemon()
    await daemon._start_control_channel()
    append_command({"type": "cancel_turn", "sessionId": SLACK, "turnId": "turn-1"})
    await asyncio.sleep(0.1)
    assert daemon.control_channel is None and handled == []

    owner["pid"] = os.getpid()
    gateway = GatewayDaemon()
    await gateway._start_control_channel()
    try:
        for _ in range(100):
            if handled:
                break
            await asyncio.sleep(0.01)
    finally:
        await gateway.control_channel.stop()
    assert handled == ["turn-1"]
