"""Filesystem conventions for the skill-learning loop.

All on-disk artifacts the loop produces or consumes live under
``~/.freyja/skills/`` next to the existing user-authored skill
directories. Centralizing the path resolution here means a single
spot to override the root in tests (``FREYJA_HOME`` env var, also
respected by ``bridge.freyja_bridge``).

Layout:

    ~/.freyja/skills/
    ├── <skill-name>/                ← canonical operator-visible skill
    │   ├── SKILL.md
    │   └── .history/                ← versioned old SKILL.md (Phase 4)
    ├── .candidates/<uuid>.yaml      ← drafter output, awaiting confirmation
    ├── .rejected/<uuid>.yaml        ← discarded candidates (negative library v1)
    ├── .archived/<skill-name>/      ← never deleted, just hidden from listing
    ├── .events.jsonl                ← append-only telemetry, all skills
    └── .value/<skill-name>.json     ← per-skill V rollup, recomputed on demand

Sanitization for the value rollup filenames mirrors
``persistence.ts:sanitizeId`` + ``bridge.freyja_bridge:_session_event_path``
so the rule is identical across processes.
"""

from __future__ import annotations

import os
import re
from pathlib import Path


def freyja_home() -> Path:
    """Resolve ~/.freyja with env-var override.

    Mirrors ``bridge.freyja_bridge._session_event_path`` so tests that
    point ``FREYJA_HOME`` at a tempdir get a fully isolated skill loop
    without needing to monkey-patch every module separately.
    """
    base = os.environ.get("FREYJA_HOME") or os.path.expanduser("~/.freyja")
    return Path(base)


def skills_root() -> Path:
    """The root operator-visible skill directory."""
    return freyja_home() / "skills"


def candidates_dir() -> Path:
    """Drafter output awaiting confirmation."""
    return skills_root() / ".candidates"


def rejected_dir() -> Path:
    """Operator-rejected candidates. Drafter consults this as a v1 negative
    library — future Phase 5 may surface it more aggressively."""
    return skills_root() / ".rejected"


def archived_dir() -> Path:
    """Hidden-from-listing but still-on-disk skills.

    We never delete. Archive is the maximum destructive action; recovery is
    by moving the directory back to ``skills_root()``. Mirrors Hermes'
    ``.archive/`` convention.
    """
    return skills_root() / ".archived"


def events_path() -> Path:
    """Append-only telemetry file shared by every skill.

    One JSONL file (not per-skill sidecars) so we never deal with
    file-locking or atomic-replace dances at write time. Per-skill stats
    are derived on read via ``value_score.compute_rollup``.
    """
    return skills_root() / ".events.jsonl"


def value_dir() -> Path:
    """Per-skill V rollup directory.

    Each skill gets one ``<safe-name>.json`` file. Rollups are recomputed
    when the events file mtime is newer than the rollup mtime — cheap to
    check, expensive only when there's actual new data.
    """
    return skills_root() / ".value"


_UNSAFE = re.compile(r"[^a-zA-Z0-9._-]")


def safe_skill_filename(name: str) -> str:
    """Sanitize a skill name for use as a filename. Lowercase + safe chars
    only. Matches the rule used elsewhere in Freyja for cross-process
    filename agreement (see ``bridge.freyja_bridge._session_event_path``
    and ``src/main/persistence.ts:sanitizeId``)."""
    safe = _UNSAFE.sub("_", name)[:160]
    return safe.lower()


def value_path_for(skill_name: str) -> Path:
    """Resolve the per-skill rollup file."""
    return value_dir() / f"{safe_skill_filename(skill_name)}.json"


def ensure_loop_dirs() -> None:
    """Pre-create every directory the loop writes to.

    Idempotent. Called once at loop init so the first append doesn't race
    on a missing parent dir. Best-effort: silently swallows mkdir failures
    so a read-only filesystem doesn't bring down the whole bridge.
    """
    for d in (
        skills_root(),
        candidates_dir(),
        rejected_dir(),
        archived_dir(),
        value_dir(),
    ):
        try:
            d.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
