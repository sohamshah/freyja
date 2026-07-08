"""Timer verbs (contract §4): timer.set / timer.list / timer.cancel.

Timers are plain asyncio tasks inside the bridge process — deliberately
process-lifetime only (no persistence in slice 1): a bridge restart
drops them, same as killing a kitchen timer by unplugging it. On fire
the manager (a) pushes a `voice_timer_fired` payload to the emitter
VoiceService installs at boot via `set_emitter`, and (b) posts a macOS
notification with sound, so the timer is audible even after the voice
session that set it has closed.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from dataclasses import dataclass
from typing import Any, Callable, Optional

from bridge.voice.adapters import mac
from bridge.voice.adapters.mac import as_quoted
from bridge.voice.verbs import Verb, VerbRegistry, VerbResult

logger = logging.getLogger(__name__)


def _fmt_duration(seconds: float) -> str:
    s = int(round(seconds))
    if s >= 60:
        m, r = divmod(s, 60)
        return f"{m}m {r}s" if r else f"{m}m"
    return f"{s}s"


@dataclass
class _Timer:
    label: str
    seconds: float
    fires_at: float  # event-loop clock (loop.time()), for remaining-time math
    task: asyncio.Task


class TimerManager:
    """All live timers, keyed by (unique) label. One instance per process."""

    def __init__(self) -> None:
        self._timers: dict[str, _Timer] = {}
        self.emitter: Optional[Callable[[dict[str, Any]], Any]] = None

    def start(self, seconds: float, label: str = "") -> _Timer:
        loop = asyncio.get_running_loop()
        base = label.strip() or f"{_fmt_duration(seconds)} timer"
        # Labels key the dict, so de-dupe: "tea", "tea 2", "tea 3", …
        final = base
        n = 2
        while final in self._timers:
            final = f"{base} {n}"
            n += 1
        task = loop.create_task(self._run(final, seconds))
        timer = _Timer(label=final, seconds=seconds, fires_at=loop.time() + seconds, task=task)
        self._timers[final] = timer
        return timer

    async def _run(self, label: str, seconds: float) -> None:
        await asyncio.sleep(seconds)
        self._timers.pop(label, None)
        if self.emitter is not None:
            try:
                payload = {"type": "voice_timer_fired", "label": label, "seconds": seconds}
                result = self.emitter(payload)
                if inspect.isawaitable(result):
                    await result
            except Exception:
                logger.exception("voice timer emitter failed for %r", label)
        # Notification is best-effort — the emitter event is the real signal.
        ok, out = await mac.run_osascript(
            f"display notification {as_quoted(label + ' — done')} "
            'with title "Freyja" sound name "Glass"'
        )
        if not ok:
            logger.warning("timer notification failed for %r: %s", label, out)

    def cancel(self, label: Optional[str] = None) -> Optional[_Timer]:
        if label:
            timer = self._timers.pop(label, None)
        elif self._timers:
            # No label: "cancel the timer" — most recently set wins, since
            # that's almost always the one the user is talking about.
            timer = self._timers.pop(next(reversed(self._timers)))
        else:
            timer = None
        if timer is not None:
            timer.task.cancel()
        return timer

    def cancel_all(self) -> None:
        for label in list(self._timers):
            self.cancel(label)

    def snapshot(self) -> list[dict[str, Any]]:
        loop = asyncio.get_running_loop()
        return [
            {
                "label": t.label,
                "remaining_sec": max(0.0, t.fires_at - loop.time()),
                "total_sec": t.seconds,
            }
            for t in self._timers.values()
        ]


_manager = TimerManager()


def set_emitter(cb: Optional[Callable[[dict[str, Any]], Any]]) -> None:
    """Install the bridge's emit callback; VoiceService calls this at boot."""
    _manager.emitter = cb


async def _timer_set(args: dict[str, Any]) -> VerbResult:
    try:
        seconds = float(args.get("seconds") or 0)
        minutes = float(args.get("minutes") or 0)
    except (TypeError, ValueError):
        return VerbResult(ok=False, summary="timer duration must be a number", error=str(args))
    total = seconds + minutes * 60
    if total <= 0:
        return VerbResult(ok=False, summary="timer needs a duration", error="missing duration")
    timer = _manager.start(total, str(args.get("label") or ""))

    async def undo() -> VerbResult:
        if _manager.cancel(timer.label) is None:
            return VerbResult(ok=False, summary=f"⏱ {timer.label} already fired")
        return VerbResult(ok=True, summary=f"cancelled ⏱ {timer.label}")

    return VerbResult(
        ok=True,
        summary=f"⏱ {timer.label} — {_fmt_duration(total)}",
        data={"label": timer.label, "seconds": total},
        undo=undo,
    )


async def _timer_list(args: dict[str, Any]) -> VerbResult:
    snap = _manager.snapshot()
    if not snap:
        return VerbResult(ok=True, summary="no timers running", data={"timers": []})
    summary = "; ".join(
        f"⏱ {t['label']} — {_fmt_duration(t['remaining_sec'])} left" for t in snap
    )
    return VerbResult(ok=True, summary=summary, data={"timers": snap})


async def _timer_cancel(args: dict[str, Any]) -> VerbResult:
    label = str(args.get("label") or "").strip() or None
    timer = _manager.cancel(label)
    if timer is None:
        summary = f"no timer named {label}" if label else "no timers running"
        return VerbResult(ok=False, summary=summary, error="not_found")
    return VerbResult(ok=True, summary=f"cancelled ⏱ {timer.label}")


def register(registry: VerbRegistry) -> None:
    registry.register(
        Verb(
            name="timer.set",
            description="Set a countdown timer (seconds and/or minutes; label optional)",
            params={
                "seconds": {"type": "number"},
                "minutes": {"type": "number"},
                "label": {"type": "string"},
            },
            required=[],
            tier="auto",
            run=_timer_set,
        )
    )
    registry.register(
        Verb(
            name="timer.list",
            description="List running timers with time remaining",
            params={},
            required=[],
            tier="auto",
            run=_timer_list,
        )
    )
    registry.register(
        Verb(
            name="timer.cancel",
            description="Cancel a timer by label, or the most recent one",
            params={"label": {"type": "string"}},
            required=[],
            tier="auto",
            run=_timer_cancel,
        )
    )
