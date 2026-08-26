"""Voice routines — operator-taught macros for the Galdr voice agent
(contract §13, pinned).

A routine is a named, ordered list of auto-tier verb steps ("do it once,
then say *remember that as 'morning'*"). One YAML file per routine under
``~/.freyja/voice/routines/<slug>.yaml``:

    name: morning                  # display form, as spoken
    description: open cmux and start a session
    created_ts: 1780000000000      # epoch ms
    updated_ts: 1780000000000
    steps:
      - verb: app.focus
        args: {name: cmux}
      - verb: computer.press
        args: {key: cmd+n}
        wait_ms: 500               # optional post-step settle override
    stats: {runs: 0, ok: 0, fail: 0, last_run_ts: null}

Execution semantics (validation, tiers, receipts, undo) live in
``bridge/voice/service.py``; this module is storage only.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import yaml

logger = logging.getLogger(__name__)

# Query/read verbs that never become routine steps when steps are derived
# from an exchange's receipts (contract §13.2, pinned): looking something
# up is not an action worth replaying. routine.* is excluded wholesale —
# no recursion, and saving/listing/forgetting is bookkeeping, not work.
# Drift guard: tests assert every name here exists in the full registry
# (build_default_registry + the service-registered verbs).
INFO_VERBS: frozenset[str] = frozenset(
    {
        "computer.see",
        "screen.look",
        "app.frontmost",
        "spotify.now_playing",
        "clipboard.read",
        "calendar.today",
        "calendar.next",
        "mail.unread",
        "reminders.list",
        "files.list",
        "timer.list",
        "shortcuts.list",
        "contacts.find",
        "briefing.read",
        "freyja.sessions",
        "freyja.project_status",
        "mission.status",
        "routine.save",
        "routine.run",
        "routine.list",
        "routine.forget",
    }
)


def slugify(name: str) -> str:
    """Routine file slug: lowercased, whitespace→``-``, ``[a-z0-9-]``
    only, dash runs collapsed. "" when nothing survives (invalid name)."""
    slug = re.sub(r"\s+", "-", (name or "").strip().lower())
    slug = re.sub(r"[^a-z0-9-]", "", slug)
    slug = re.sub(r"-{2,}", "-", slug).strip("-")
    return slug


@dataclass
class RoutineStep:
    verb: str
    args: dict[str, Any] = field(default_factory=dict)
    wait_ms: Optional[int] = None  # post-step settle override (ms)

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"verb": self.verb, "args": dict(self.args)}
        if self.wait_ms is not None:
            d["wait_ms"] = int(self.wait_ms)
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "RoutineStep":
        if not isinstance(d, dict):
            raise ValueError("step must be a mapping")
        verb = str(d.get("verb") or "").strip()
        if not verb:
            raise ValueError("step has no verb")
        args = d.get("args") or {}
        if not isinstance(args, dict):
            raise ValueError(f"step {verb}: args must be a mapping")
        wait_ms = d.get("wait_ms")
        if wait_ms is not None:
            if isinstance(wait_ms, bool) or not isinstance(wait_ms, int) or wait_ms < 0:
                raise ValueError(f"step {verb}: wait_ms must be a non-negative integer")
        return cls(verb=verb, args=dict(args), wait_ms=wait_ms)


def _default_stats() -> dict[str, Any]:
    return {"runs": 0, "ok": 0, "fail": 0, "last_run_ts": None}


@dataclass
class Routine:
    name: str  # display form, as spoken ("morning")
    description: str = ""
    created_ts: int = 0  # epoch ms
    updated_ts: int = 0
    steps: list[RoutineStep] = field(default_factory=list)
    stats: dict[str, Any] = field(default_factory=_default_stats)

    @property
    def slug(self) -> str:
        return slugify(self.name)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "created_ts": int(self.created_ts),
            "updated_ts": int(self.updated_ts),
            "steps": [s.to_dict() for s in self.steps],
            "stats": dict(self.stats),
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Routine":
        if not isinstance(d, dict):
            raise ValueError("routine must be a mapping")
        name = str(d.get("name") or "").strip()
        if not slugify(name):
            raise ValueError("routine has no usable name")
        raw_steps = d.get("steps")
        if not isinstance(raw_steps, list):
            raise ValueError(f"routine {name!r}: steps must be a list")
        steps = [RoutineStep.from_dict(s) for s in raw_steps]
        stats = _default_stats()
        raw_stats = d.get("stats")
        if isinstance(raw_stats, dict):
            for key in ("runs", "ok", "fail"):
                val = raw_stats.get(key)
                if isinstance(val, int) and not isinstance(val, bool) and val >= 0:
                    stats[key] = val
            last = raw_stats.get("last_run_ts")
            if isinstance(last, int) and not isinstance(last, bool):
                stats["last_run_ts"] = last
        return cls(
            name=name,
            description=str(d.get("description") or ""),
            created_ts=int(d.get("created_ts") or 0),
            updated_ts=int(d.get("updated_ts") or 0),
            steps=steps,
            stats=stats,
        )


def _default_routines_dir() -> Path:
    return Path.home() / ".freyja" / "voice" / "routines"


class RoutineStore:
    """One-YAML-file-per-routine persistence. Atomic tmp+rename writes;
    a corrupt file is skipped with a log, never fatal (same tolerance as
    the receipt store — a torn write can't brick the feature)."""

    def __init__(self, path: Optional[Path] = None) -> None:
        self._dir = Path(path) if path is not None else _default_routines_dir()

    @property
    def path(self) -> Path:
        return self._dir

    def _file_for(self, slug: str) -> Path:
        return self._dir / f"{slug}.yaml"

    def _parse_file(self, file: Path) -> Optional[Routine]:
        try:
            raw = yaml.safe_load(file.read_text(encoding="utf-8"))
            return Routine.from_dict(raw)
        except FileNotFoundError:
            return None
        except Exception as exc:  # noqa: BLE001 — corrupt file: skip, log, live on
            logger.warning("skipping corrupt routine file %s: %s", file, exc)
            return None

    def load_all(self) -> list[Routine]:
        try:
            files = sorted(self._dir.glob("*.yaml"))
        except OSError:
            return []
        routines = []
        for file in files:
            routine = self._parse_file(file)
            if routine is not None:
                routines.append(routine)
        return routines

    def get(self, name: str) -> Optional[Routine]:
        """Lookup by normalized name — "Morning Routine" finds the file
        saved as ``morning-routine.yaml``."""
        slug = slugify(name)
        if not slug:
            return None
        return self._parse_file(self._file_for(slug))

    def save(self, routine: Routine) -> Path:
        """Atomic write (tmp + rename). Timestamps and stats are the
        caller's to manage — save persists exactly what it is given."""
        slug = routine.slug
        if not slug:
            raise ValueError(f"routine name {routine.name!r} yields an empty slug")
        self._dir.mkdir(parents=True, exist_ok=True)
        target = self._file_for(slug)
        tmp = target.with_name(target.name + ".tmp")
        tmp.write_text(
            yaml.safe_dump(routine.to_dict(), sort_keys=False, allow_unicode=True),
            encoding="utf-8",
        )
        os.replace(tmp, target)
        return target

    def delete(self, name: str) -> Optional[Routine]:
        """Remove a routine, returning the parsed routine (for undo
        restore) — None when there was nothing (or nothing readable) to
        return. The file is removed either way."""
        slug = slugify(name)
        if not slug:
            return None
        file = self._file_for(slug)
        routine = self._parse_file(file)
        try:
            file.unlink(missing_ok=True)
        except OSError as exc:
            logger.warning("could not delete routine file %s: %s", file, exc)
            return None
        return routine

    def names_md(self) -> str:
        """Compact listing for the system prompt: one `- name (N steps) —
        description` line per routine; "" when none are saved."""
        lines = []
        for routine in self.load_all():
            n = len(routine.steps)
            line = f"- {routine.name} ({n} step{'s' if n != 1 else ''})"
            if routine.description:
                line += f" — {routine.description}"
            lines.append(line)
        return "\n".join(lines)
