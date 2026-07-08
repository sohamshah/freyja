"""Verb adapters — the hands of the voice agent.

Each module exposes `register(registry)` for its verb family;
`register_all` is what `verbs.build_default_registry()` calls.
`mission.spawn` is NOT here — service.py registers it directly because
it needs bridge session access.

All Mac side effects funnel through `adapters.mac.run_osascript` /
`run_exec`, which is also the monkeypatch seam the adapter tests use.
Network reach (slack via slack_sdk, screen's vision call via httpx) is
monkeypatched at each module's client attribute instead — no test ever
touches real Slack, OpenAI, or the screen.
"""

from bridge.voice.adapters import mac, screen, slack, spotify, system, timers
from bridge.voice.adapters.timers import TimerManager, set_emitter
from bridge.voice.verbs import VerbRegistry


def register_all(registry: VerbRegistry) -> None:
    spotify.register(registry)
    system.register(registry)
    timers.register(registry)
    slack.register(registry)
    screen.register(registry)


__all__ = [
    "TimerManager",
    "mac",
    "register_all",
    "screen",
    "set_emitter",
    "slack",
    "spotify",
    "system",
    "timers",
]
