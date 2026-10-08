"""Per-app and per-site notes for the operator, kept as ordinary skills named
`jev-app-<slug>` and `jev-site-<host slug>`.

A skill like `jev-app-calculator` holds what earlier runs learned about one
app (where a control hides, which key works); `jev-site-console-cloud-google-com`
does the same for one website, whichever browser shows it. The loop shows them
to Jev and the LLM doors as hints that may be stale. A lookup never raises.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

NOTES_MAX_CHARS = 2000
NOTES_LABEL = "Notes from earlier runs on this app (may be stale)"


def app_slug(bundle_id_or_name: str) -> str:
    """Lowercased app name, or the last component of a bundle id, with every
    run of non-alphanumerics turned into one '-'."""
    raw = (bundle_id_or_name or "").strip()
    if "." in raw and " " not in raw:
        raw = raw.rsplit(".", 1)[-1]
    return re.sub(r"[^a-z0-9]+", "-", raw.lower()).strip("-")


def site_slug(host: str) -> str:
    """`console.cloud.google.com` -> `console-cloud-google-com` (no www)."""
    h = (host or "").strip().lower()
    h = h[4:] if h.startswith("www.") else h
    return re.sub(r"[^a-z0-9]+", "-", h).strip("-")


def site_notes(host: str, *, workspace: Path | str | None = None, store: Any = None) -> str:
    """Body of the `jev-site-<host slug>` skill, or ""."""
    slug = site_slug(host)
    return _skill_body(f"jev-site-{slug}", workspace, store) if slug else ""


def app_notes(
    bundle_id_or_name: str, *, workspace: Path | str | None = None, store: Any = None
) -> str:
    """Body of the `jev-app-<slug>` skill capped at 2000 chars, or "" when there
    is none or anything goes wrong."""
    slug = app_slug(bundle_id_or_name)
    return _skill_body(f"jev-app-{slug}", workspace, store) if slug else ""


def _skill_body(name: str, workspace: Path | str | None, store: Any) -> str:
    try:
        if store is None:
            from bridge.knowledge.skill_store import SkillStore  # noqa: PLC0415

            store = SkillStore(workspace if workspace is not None else Path.cwd())
        skill, content = store.load(name)
        if skill is None:
            return ""
        if content.startswith("[Skill:"):
            content = content.split("\n\n", 1)[1] if "\n\n" in content else ""
        return content.strip()[:NOTES_MAX_CHARS]
    except Exception:  # noqa: BLE001
        return ""


def notes_section(notes: str) -> str:
    return f"{NOTES_LABEL}:\n{notes}" if notes else ""
