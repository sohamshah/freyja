"""Next-fire computation + NL → typed-schedule resolution.

Cron uses ``croniter`` when available (preferred), and falls back to a
small custom parser for the common patterns we encode ourselves
(weekday/weekend, every-N-minutes/hours, fixed times). NL parsing for
one-shot ("in 30 minutes", "tomorrow at 5pm") prefers ``dateparser``
when installed; otherwise a small parser handles the common cases.

Everything stores time as POSIX float seconds. Wall-clock-aware
schedules carry an IANA timezone so DST is automatic.
"""

from __future__ import annotations

import logging
import re
import time
from datetime import datetime, timedelta, timezone as dt_timezone
from typing import Any

from bridge.scheduler.models import (
    CronSchedule,
    IntervalSchedule,
    OnceSchedule,
    ScheduleSpec,
    SelfPacedSchedule,
)

logger = logging.getLogger("freyja.scheduler.scheduling")


# ─── Timezone helpers ──────────────────────────────────────────────────


def _resolve_tz(name: str) -> Any:
    """IANA name → tzinfo. Falls back to UTC on unknown name."""
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(name or "UTC")
    except Exception:  # noqa: BLE001
        return dt_timezone.utc


def now_in(tz_name: str) -> datetime:
    return datetime.now(_resolve_tz(tz_name))


# ─── Next-fire computation ─────────────────────────────────────────────


def compute_next_fire(
    schedule: ScheduleSpec,
    *,
    now: float | None = None,
    last_fire: float | None = None,
) -> float | None:
    """POSIX timestamp of the next fire time, or None if the schedule
    has no future fire (one-shot already-fired, etc.)."""
    if now is None:
        now = time.time()

    if isinstance(schedule, OnceSchedule):
        try:
            target = _parse_iso(schedule.at_iso, schedule.timezone)
        except Exception:  # noqa: BLE001
            return None
        return target if target > now else None

    if isinstance(schedule, IntervalSchedule):
        if schedule.seconds <= 0:
            return None
        floor = now
        if schedule.after_iso:
            try:
                start = _parse_iso(schedule.after_iso, schedule.timezone)
                if start > floor:
                    floor = start
            except Exception:  # noqa: BLE001
                pass
        if last_fire is None or last_fire <= 0:
            return floor + schedule.seconds
        # Catch up: if many intervals have passed (the bridge was
        # closed), jump straight to the next future interval rather
        # than firing every missed one.
        elapsed = floor - last_fire
        if elapsed < schedule.seconds:
            return last_fire + schedule.seconds
        n = int(elapsed // schedule.seconds) + 1
        return last_fire + n * schedule.seconds

    if isinstance(schedule, CronSchedule):
        return _cron_next(schedule.expression, schedule.timezone, now)

    if isinstance(schedule, SelfPacedSchedule):
        # Self-paced is driven by continue_loop / complete_loop calls
        # the agent makes during a fire. The runtime sets
        # ``next_fire_at`` directly on the JobRecord at the end of
        # each iteration. When that hasn't happened yet (first fire),
        # schedule it for ``min_delay_seconds`` from now.
        return now + max(1, schedule.min_delay_seconds)

    return None


def _parse_iso(iso: str, tz_name: str) -> float:
    """Parse an ISO/RFC3339 timestamp. If the string is naive (no
    offset), interpret it in ``tz_name``."""
    if not iso:
        raise ValueError("empty iso")
    s = iso.strip()
    # Python's fromisoformat handles offsets and 'Z' on 3.11+.
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        # Fall through to dateparser if installed.
        try:
            import dateparser  # type: ignore[import-not-found]

            dt = dateparser.parse(s)  # type: ignore[assignment]
            if dt is None:
                raise ValueError(f"unparseable iso: {iso}")
        except ImportError:
            raise
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_resolve_tz(tz_name))
    return dt.timestamp()


def _cron_next(expression: str, tz_name: str, now: float) -> float | None:
    """Next fire of a 5-field cron in ``tz_name``. Uses croniter when
    available."""
    if not expression.strip():
        return None
    tz = _resolve_tz(tz_name)
    base = datetime.fromtimestamp(now, tz=tz)
    try:
        from croniter import croniter  # type: ignore[import-not-found]

        itr = croniter(expression, base)
        return itr.get_next(datetime).timestamp()
    except ImportError:
        # Tiny built-in cron interpreter (5-field, no extensions).
        return _builtin_cron_next(expression, base)
    except Exception as exc:  # noqa: BLE001
        logger.warning("cron parse failed for %r: %s", expression, exc)
        return None


def _builtin_cron_next(expression: str, base: datetime) -> float | None:
    """Minimal cron next-fire — supports `*`, `*/N`, `a,b,c`, `a-b`.
    Fields: minute hour dom month dow. Sunday = 0 or 7."""
    fields = expression.split()
    if len(fields) != 5:
        return None
    try:
        minute_set = _expand_cron_field(fields[0], 0, 59)
        hour_set = _expand_cron_field(fields[1], 0, 23)
        dom_set = _expand_cron_field(fields[2], 1, 31)
        month_set = _expand_cron_field(fields[3], 1, 12)
        dow_set = _expand_cron_field(fields[4], 0, 6, sunday7=True)
    except ValueError:
        return None

    cur = base.replace(second=0, microsecond=0) + timedelta(minutes=1)
    # Cap search at ~4 years to avoid infinite loops on impossible specs.
    horizon = cur + timedelta(days=366 * 4)
    while cur <= horizon:
        if (cur.month in month_set
                and cur.day in dom_set
                and (cur.weekday() + 1) % 7 in dow_set
                and cur.hour in hour_set
                and cur.minute in minute_set):
            return cur.timestamp()
        cur += timedelta(minutes=1)
    return None


def _expand_cron_field(s: str, lo: int, hi: int, *, sunday7: bool = False) -> set[int]:
    out: set[int] = set()
    for part in s.split(","):
        step = 1
        if "/" in part:
            head, _, step_s = part.partition("/")
            step = int(step_s)
            part = head
        if part == "*" or part == "":
            start, end = lo, hi
        elif "-" in part:
            a, _, b = part.partition("-")
            start, end = int(a), int(b)
        else:
            start = end = int(part)
        for v in range(start, end + 1, step):
            if sunday7 and v == 7:
                v = 0
            if lo <= v <= hi:
                out.add(v)
    return out


# ─── NL → ScheduleSpec resolution ──────────────────────────────────────


_DURATION_UNIT = {
    "s": 1, "sec": 1, "secs": 1, "second": 1, "seconds": 1,
    "m": 60, "min": 60, "mins": 60, "minute": 60, "minutes": 60,
    "h": 3600, "hr": 3600, "hrs": 3600, "hour": 3600, "hours": 3600,
    "d": 86400, "day": 86400, "days": 86400,
}

_WEEKDAY_DOW = {
    "mon": 1, "monday": 1,
    "tue": 2, "tuesday": 2,
    "wed": 3, "wednesday": 3,
    "thu": 4, "thursday": 4,
    "fri": 5, "friday": 5,
    "sat": 6, "saturday": 6,
    "sun": 0, "sunday": 0,
}


def parse_when(when: str, *, timezone: str = "UTC") -> ScheduleSpec:
    """Resolve a natural-language ``when`` string into a typed schedule.

    Coverage (case-insensitive):

      One-shot:
        "tomorrow at 5pm"
        "in 30 minutes" / "in 2h"
        "2026-06-15T14:00:00-07:00"
        any ISO8601

      Interval:
        "every 5 minutes" / "every 2h"

      Cron-like:
        "every weekday at 9am"
        "every monday at noon"
        "every monday and friday at 4pm"
        "every saturday at 10:30am"
        "every day at 8am"

      Self-paced:
        "self-paced between 60s and 30m"

    Falls through to ``dateparser`` (when installed) for unrecognized
    one-shot phrases. Raises ``ValueError`` if nothing parses."""
    s = (when or "").strip()
    if not s:
        raise ValueError("empty 'when'")
    low = s.lower()

    # ─ Self-paced
    m = re.match(
        r"self.paced(?:\s+between\s+(\d+)\s*([a-z]+)\s+and\s+(\d+)\s*([a-z]+))?\s*$",
        low,
    )
    if m:
        if m.group(1):
            mn = int(m.group(1)) * _DURATION_UNIT.get(m.group(2), 1)
            mx = int(m.group(3)) * _DURATION_UNIT.get(m.group(4), 1)
        else:
            mn, mx = 60, 1800
        return SelfPacedSchedule(min_delay_seconds=mn, max_delay_seconds=mx)

    # ─ "every N <unit>"
    m = re.match(r"every\s+(\d+)\s*([a-z]+)\s*$", low)
    if m:
        n = int(m.group(1))
        unit = _DURATION_UNIT.get(m.group(2))
        if unit:
            return IntervalSchedule(seconds=n * unit, timezone=timezone)

    # ─ "every weekday|day|<dow>[s] [and <dow>...] at <time>"
    m = re.match(
        r"every\s+(weekday|weekdays|day|"
        r"(?:mon(?:day)?|tue(?:sday)?|wed(?:nesday)?|thu(?:rsday)?|fri(?:day)?|"
        r"sat(?:urday)?|sun(?:day)?)(?:s)?"
        r"(?:\s+(?:and|,)\s+(?:mon(?:day)?|tue(?:sday)?|wed(?:nesday)?|thu(?:rsday)?|"
        r"fri(?:day)?|sat(?:urday)?|sun(?:day)?)(?:s)?)*"
        r")\s+at\s+(.+)$",
        low,
    )
    if m:
        dow_phrase = m.group(1)
        time_phrase = m.group(2).strip()
        hh, mm = _parse_time_phrase(time_phrase)
        if dow_phrase in ("weekday", "weekdays"):
            dow = "1-5"
        elif dow_phrase == "day":
            dow = "*"
        else:
            tokens = re.findall(
                r"mon(?:day)?|tue(?:sday)?|wed(?:nesday)?|thu(?:rsday)?|"
                r"fri(?:day)?|sat(?:urday)?|sun(?:day)?",
                dow_phrase,
            )
            dows = sorted({_WEEKDAY_DOW[t] for t in tokens})
            dow = ",".join(str(d) for d in dows) if dows else "*"
        expr = f"{mm} {hh} * * {dow}"
        return CronSchedule(expression=expr, timezone=timezone)

    # ─ "at <time>" / "tomorrow at <time>" / "today at <time>"
    m = re.match(r"(?:(today|tomorrow)\s+)?at\s+(.+)$", low)
    if m:
        which = m.group(1)
        time_phrase = m.group(2).strip()
        hh, mm = _parse_time_phrase(time_phrase)
        tz = _resolve_tz(timezone)
        now = datetime.now(tz)
        target = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if which == "tomorrow":
            target = target + timedelta(days=1)
        elif which is None and target <= now:
            target = target + timedelta(days=1)
        return OnceSchedule(at_iso=target.isoformat(), timezone=timezone)

    # ─ "in <N> <unit>"
    m = re.match(r"in\s+(\d+)\s*([a-z]+)\s*$", low)
    if m:
        n = int(m.group(1))
        unit = _DURATION_UNIT.get(m.group(2))
        if unit:
            tz = _resolve_tz(timezone)
            target = datetime.now(tz) + timedelta(seconds=n * unit)
            return OnceSchedule(at_iso=target.isoformat(), timezone=timezone)

    # ─ Fall back to dateparser for unrecognized one-shots.
    try:
        import dateparser  # type: ignore[import-not-found]

        dt = dateparser.parse(s, settings={
            "TIMEZONE": timezone,
            "RETURN_AS_TIMEZONE_AWARE": True,
            "PREFER_DATES_FROM": "future",
        })
        if dt is not None:
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=_resolve_tz(timezone))
            return OnceSchedule(at_iso=dt.isoformat(), timezone=timezone)
    except ImportError:
        pass

    # ─ ISO8601 last-ditch
    try:
        ts = _parse_iso(s, timezone)
        return OnceSchedule(
            at_iso=datetime.fromtimestamp(ts, tz=_resolve_tz(timezone)).isoformat(),
            timezone=timezone,
        )
    except Exception:  # noqa: BLE001
        pass

    raise ValueError(f"could not parse 'when' phrase: {when!r}")


def _parse_time_phrase(s: str) -> tuple[int, int]:
    """'9am' → (9,0). '4:30pm' → (16,30). 'noon' → (12,0). '0900' → (9,0).
    Raises ValueError on unparseable."""
    low = s.strip().lower()
    if low in ("noon", "12pm"):
        return 12, 0
    if low in ("midnight", "12am"):
        return 0, 0
    m = re.match(r"(\d{1,2})(?::(\d{2}))?\s*(am|pm)?$", low)
    if m:
        hh = int(m.group(1))
        mm = int(m.group(2)) if m.group(2) else 0
        suf = m.group(3)
        if suf == "pm" and hh < 12:
            hh += 12
        elif suf == "am" and hh == 12:
            hh = 0
        if 0 <= hh <= 23 and 0 <= mm <= 59:
            return hh, mm
    m = re.match(r"(\d{2})(\d{2})$", low)
    if m:
        return int(m.group(1)), int(m.group(2))
    raise ValueError(f"unparseable time phrase: {s!r}")


# ─── Human-readable cadence label ──────────────────────────────────────


def cadence_label(s: ScheduleSpec) -> str:
    if isinstance(s, OnceSchedule):
        return f"once at {s.at_iso}"
    if isinstance(s, IntervalSchedule):
        return f"every {_humanize_seconds(s.seconds)}"
    if isinstance(s, CronSchedule):
        return f"cron({s.expression}) [{s.timezone}]"
    if isinstance(s, SelfPacedSchedule):
        return (
            f"self-paced ({_humanize_seconds(s.min_delay_seconds)}–"
            f"{_humanize_seconds(s.max_delay_seconds)})"
        )
    return "?"


def _humanize_seconds(n: int) -> str:
    if n % 86400 == 0:
        d = n // 86400
        return f"{d}d"
    if n % 3600 == 0:
        h = n // 3600
        return f"{h}h"
    if n % 60 == 0:
        m = n // 60
        return f"{m}m"
    return f"{n}s"
