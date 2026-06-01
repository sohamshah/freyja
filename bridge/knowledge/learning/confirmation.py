"""Operator confirmation: turn a candidate into a real skill (or discard it).

This is the data-layer handler the desktop UI / Slack click handler calls
when the operator clicks Promote / Edit / Discard on a candidate emitted
by the drafter. No UI here — just the file moves, telemetry, and
SkillStore invalidation.

Flow
────

  · ``promote(candidate_id, edits=...)`` — read the candidate from
    ``.candidates/<id>.yaml``, apply optional field overrides, render
    a SKILL.md (YAML frontmatter + body matching the schema
    ``bridge.knowledge.skill_store._parse_skill_md`` expects), write it
    to ``~/.freyja/skills/<name>/SKILL.md`` atomically, log
    EVENT_PROMOTED, and delete the pending candidate file. If a
    directory with that name already exists we refuse with
    ``reason='name_collision'`` — overwriting an operator-curated skill
    is the most destructive thing this module could do, so it requires
    explicit intent via ``edits={"name": "<new-name>"}``.

  · ``discard(candidate_id, reason=...)`` — move the candidate from
    ``.candidates/`` to ``.rejected/`` (carrying the rejection metadata
    so the drafter's negative library can consult it next time), log
    EVENT_DISCARDED, and delete the pending file.

Atomic writes
─────────────
SKILL.md goes through ``tempfile.mkstemp`` + ``os.replace`` in the
target directory so a concurrent reader (the per-session SkillStore
walking ``~/.freyja/skills/``) never sees a half-written file. Same
discipline as ``candidates._atomic_write_yaml`` and
``value_score._persist_rollup``.

SkillStore invalidation
───────────────────────
``bridge.knowledge.skill_store.SkillStore`` is constructed per-session
(see ``bridge.freyja_bridge``), not as a global singleton — its
``refresh()`` method uses an mtime+size fingerprint of every
``SKILL.md`` it can see, so writing a new file with ``os.replace``
naturally invalidates the next ``refresh()`` call. There's nothing for
us to poke. If a singleton later appears, this is the place to thread
the invalidate call.
"""

from __future__ import annotations

import logging
import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from bridge.knowledge.learning import candidates, events
from bridge.knowledge.learning.paths import (
    ensure_loop_dirs,
    skills_root,
)

logger = logging.getLogger(__name__)


# ── Public result type ──


@dataclass
class PromotionResult:
    """Return value for ``promote`` / ``discard``.

    ``ok`` is the only field callers need to branch on. ``skill_path``
    is set on a successful promote (so the UI can offer "open in
    finder" or similar); ``reason`` is set on failure (so the UI can
    render a useful toast).
    """

    ok: bool
    candidate_id: str
    skill_path: Path | None
    reason: str = ""


# ── Promote ──


def promote(
    candidate_id: str,
    *,
    actor: str = "operator",
    edits: dict[str, Any] | None = None,
) -> PromotionResult:
    """Promote a pending candidate to a real skill on disk.

    Reads ``.candidates/<id>.yaml``, applies any ``edits`` overrides
    (the desktop UI's Edit affordance sends partial dicts), writes a
    SKILL.md under ``~/.freyja/skills/<name>/``, records
    EVENT_PROMOTED, and deletes the candidate file.

    Returns a ``PromotionResult``. The only failure modes are:

      · candidate not found (UI race — operator double-clicked, or the
        candidate was already promoted in another window)
      · name collision with an existing skill directory (refuse rather
        than overwrite; the operator must rename via ``edits``)
      · disk write failure (raised as ``OSError`` from the underlying
        atomic-write — we let it propagate so the caller sees the real
        error)
    """
    cand = candidates.get_pending(candidate_id)
    if cand is None:
        logger.info("confirmation.promote miss id=%s", candidate_id)
        return PromotionResult(
            ok=False,
            candidate_id=candidate_id,
            skill_path=None,
            reason="not_found",
        )

    # Apply caller-supplied overrides on top of the candidate. Only
    # fields the operator UI reasonably exposes are honored — we don't
    # let an edits dict scribble over candidate_id/source_session_id
    # etc., since those would corrupt the audit trail.
    edited = _apply_edits(cand, edits or {})

    name = (edited.name or "").strip()
    if not name:
        return PromotionResult(
            ok=False,
            candidate_id=candidate_id,
            skill_path=None,
            reason="empty_name",
        )

    target_dir = skills_root() / name
    skill_path = target_dir / "SKILL.md"

    # Collision check. Bare existence of the directory is enough —
    # there might be a SKILL.md, references/, scripts/, anything we
    # don't want to clobber. The operator forces by renaming via edits.
    if target_dir.exists():
        logger.info(
            "confirmation.promote name_collision name=%s id=%s",
            name, candidate_id,
        )
        return PromotionResult(
            ok=False,
            candidate_id=candidate_id,
            skill_path=None,
            reason="name_collision",
        )

    body_text = _render_skill_md(edited)

    try:
        _atomic_write_skill_md(skill_path, body_text)
    except OSError as exc:
        # Roll back the just-created skill dir if writing failed mid-way
        # so we don't leave an empty directory behind that future
        # promote calls would treat as a collision.
        logger.warning(
            "confirmation.promote write_failed id=%s err=%s",
            candidate_id, exc,
        )
        try:
            if target_dir.exists() and not any(target_dir.iterdir()):
                target_dir.rmdir()
        except OSError:
            pass
        raise

    # Telemetry: appended after the write succeeded so a failed write
    # doesn't lie in the event log. Best-effort — events.append swallows
    # its own errors.
    events.append({
        "event": events.EVENT_PROMOTED,
        "skill": name,
        "candidate_id": candidate_id,
        "actor": actor or "operator",
        "source_session_id": edited.source_session_id,
        "source_turn_id": edited.source_turn_id,
        "skill_type": edited.skill_type,
        "skill_path": str(skill_path),
    })

    # Remove the pending file last — if we crashed between the write
    # and the delete, a re-run would hit name_collision and the
    # operator can manually clean up the orphaned candidate, which is
    # safer than losing the freshly-promoted skill on disk.
    candidates.delete_pending(candidate_id)

    _refresh_skill_store_singleton()

    logger.info(
        "confirmation.promote ok name=%s id=%s actor=%s path=%s",
        name, candidate_id, actor, skill_path,
    )
    return PromotionResult(
        ok=True,
        candidate_id=candidate_id,
        skill_path=skill_path,
        reason="",
    )


# ── Discard ──


def discard(
    candidate_id: str,
    *,
    actor: str = "operator",
    reason: str = "operator-rejected",
) -> PromotionResult:
    """Discard a pending candidate. Moves it to ``.rejected/`` so the
    drafter's negative library can learn from it, records
    EVENT_DISCARDED, and deletes the pending file.

    Returns a ``PromotionResult`` with ``ok=True`` even on a
    "not found" candidate — discard is idempotent. The desktop UI may
    fire a discard twice (double-click, network retry) and the user
    intent is identical either way.
    """
    cand = candidates.get_pending(candidate_id)
    if cand is None:
        logger.info("confirmation.discard miss id=%s", candidate_id)
        # Still log EVENT_DISCARDED so the operator's intent is
        # visible in the event log even if the candidate already
        # walked. Skip the .rejected/ write since we have nothing
        # to write.
        events.append({
            "event": events.EVENT_DISCARDED,
            "skill": "",
            "candidate_id": candidate_id,
            "actor": actor or "operator",
            "reason": reason or "",
            "note": "not_found",
        })
        return PromotionResult(
            ok=True,
            candidate_id=candidate_id,
            skill_path=None,
            reason="not_found",
        )

    try:
        candidates.write_rejected(cand, reason=reason, actor=actor)
    except OSError as exc:
        # A rejected-dir write failure shouldn't block discarding —
        # the operator's intent is "make this candidate go away".
        # Log and continue to the delete + event-log steps.
        logger.warning(
            "confirmation.discard write_rejected_failed id=%s err=%s",
            candidate_id, exc,
        )

    events.append({
        "event": events.EVENT_DISCARDED,
        "skill": cand.name,
        "candidate_id": candidate_id,
        "actor": actor or "operator",
        "reason": reason or "",
    })

    candidates.delete_pending(candidate_id)

    logger.info(
        "confirmation.discard ok name=%s id=%s actor=%s reason=%s",
        cand.name, candidate_id, actor, reason,
    )
    return PromotionResult(
        ok=True,
        candidate_id=candidate_id,
        skill_path=None,
        reason="",
    )


# ── Internals ──


# Fields the operator UI is allowed to override via the `edits` dict.
# Everything else (candidate_id, drafted_at, source_*, guard_*) is
# audit metadata that must round-trip unchanged.
_EDITABLE_FIELDS = frozenset({
    "name",
    "description",
    "skill_type",
    "triggers",
    "tags",
    "body",
})


def _apply_edits(c: candidates.Candidate, edits: dict[str, Any]) -> candidates.Candidate:
    """Return a copy of ``c`` with ``edits`` applied.

    Unknown keys are silently ignored — the desktop UI may evolve
    independently of this module and we'd rather drop a stray field
    than fail the promote.
    """
    if not edits:
        return c

    def _str(value: Any, fallback: str) -> str:
        if value is None:
            return fallback
        s = str(value).strip()
        return s if s else fallback

    def _list(value: Any, fallback: list[str]) -> list[str]:
        if value is None:
            return list(fallback)
        if isinstance(value, list):
            return [str(v) for v in value if str(v).strip()]
        if isinstance(value, str) and value.strip():
            return [value.strip()]
        return list(fallback)

    # Build only from whitelisted fields so a stray edits['source_session_id']
    # can't corrupt the audit trail.
    name = _str(edits.get("name"), c.name) if "name" in edits else c.name
    description = (
        _str(edits.get("description"), c.description)
        if "description" in edits else c.description
    )
    skill_type = (
        _str(edits.get("skill_type"), c.skill_type)
        if "skill_type" in edits else c.skill_type
    )
    triggers = (
        _list(edits.get("triggers"), c.triggers)
        if "triggers" in edits else c.triggers
    )
    tags = _list(edits.get("tags"), c.tags) if "tags" in edits else c.tags
    body = edits["body"] if ("body" in edits and isinstance(edits["body"], str)) else c.body

    # Warn (don't fail) if the caller passed unknown keys — helps catch
    # UI/bridge schema drift.
    unknown = set(edits.keys()) - _EDITABLE_FIELDS
    if unknown:
        logger.info(
            "confirmation.promote edits.unknown_keys id=%s keys=%s",
            c.candidate_id, sorted(unknown),
        )

    return candidates.Candidate(
        candidate_id=c.candidate_id,
        drafted_at=c.drafted_at,
        source_session_id=c.source_session_id,
        source_turn_id=c.source_turn_id,
        drafter_model=c.drafter_model,
        decision=c.decision,
        rationale=c.rationale,
        guard_verdict=c.guard_verdict,
        guard_findings=list(c.guard_findings or []),
        name=name,
        description=description,
        triggers=list(triggers),
        tags=list(tags),
        body=body,
        skill_type=skill_type,
    )


def _yaml_list(values: list[str]) -> str:
    """Render a list as a block-style YAML sequence. We hand-roll the
    serializer (rather than calling yaml.safe_dump) because
    ``skill_store._parse_frontmatter`` is a tiny line-oriented parser
    that only understands ``key: value`` and ``  - item`` shapes —
    PyYAML's full flow-style output would not round-trip cleanly back
    through that parser.

    Empty list → ``[]`` so the key still appears (consistent shape for
    downstream readers)."""
    cleaned = [str(v).strip() for v in values or [] if str(v).strip()]
    if not cleaned:
        return "[]"
    lines = []
    for item in cleaned:
        # Quote any item that contains characters the parser would
        # split on, or that begins with a YAML-significant char. The
        # parser strips surrounding quotes, so this is safe.
        if any(ch in item for ch in (":", "#", "[", "]", "{", "}", ",")) or item.startswith(("-", "?", "*", "&", "!", "|", ">", "%", "@", "`")):
            escaped = item.replace("\\", "\\\\").replace("\"", "\\\"")
            lines.append(f"  - \"{escaped}\"")
        else:
            lines.append(f"  - {item}")
    return "\n" + "\n".join(lines)


def _yaml_scalar(value: str) -> str:
    """Render a string scalar safe for the line-oriented frontmatter
    parser. Quotes when the value contains the parser's split char
    (``:``) or leading whitespace, otherwise leaves bare."""
    s = (value or "").replace("\r", " ").replace("\n", " ").strip()
    if not s:
        return ""
    if ":" in s or s.startswith(("-", "?", "*", "&", "!", "|", ">", "%", "@", "`", " ", "\t")):
        escaped = s.replace("\\", "\\\\").replace("\"", "\\\"")
        return f"\"{escaped}\""
    return s


def _render_skill_md(c: candidates.Candidate) -> str:
    """Render a candidate as a SKILL.md document.

    Frontmatter keys mirror what ``skill_store._parse_skill_md`` reads:
    ``name``, ``description``, ``type``, ``triggers``, ``tags``,
    ``source``. We always emit ``type`` (not ``skill_type``) since the
    parser checks ``type`` first; this matches the convention in
    operator-authored skills already on disk.

    The body is appended verbatim, separated from the frontmatter by
    the ``---`` delimiter the parser uses. A trailing newline keeps the
    file POSIX-clean.
    """
    lines = ["---"]
    lines.append(f"name: {_yaml_scalar(c.name)}")

    desc = _yaml_scalar(c.description)
    if desc:
        lines.append(f"description: {desc}")

    lines.append(f"type: {_yaml_scalar(c.skill_type or 'build')}")

    # Triggers / tags always emitted (even when empty) so the parser
    # produces a consistent metadata shape and downstream filters can
    # rely on the keys being present.
    lines.append(f"triggers:{_yaml_list(list(c.triggers or []))}")
    lines.append(f"tags:{_yaml_list(list(c.tags or []))}")

    # Provenance: the source field is operator-readable telemetry —
    # tells future readers "this came from the skill-learning loop on
    # session X" rather than from a hand-authored library.
    source = f"freyja-drafter:{c.source_session_id}" if c.source_session_id else "freyja-drafter"
    lines.append(f"source: {_yaml_scalar(source)}")

    lines.append("---")

    body = (c.body or "").rstrip()
    if body:
        lines.append("")
        lines.append(body)
    lines.append("")
    return "\n".join(lines)


def _atomic_write_skill_md(path: Path, text: str) -> None:
    """Atomic write into ``<skills_root>/<name>/SKILL.md``.

    Creates the parent directory if needed, then writes via mkstemp
    in the same directory so ``os.replace`` is a same-filesystem
    atomic rename. fsync ensures the data hits disk before the rename
    so a crash mid-promote can't leave a zero-byte file visible.
    """
    ensure_loop_dirs()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(
        dir=str(path.parent),
        prefix=f".{path.stem}_",
        suffix=".tmp",
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fp:
            fp.write(text)
            fp.flush()
            os.fsync(fp.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _refresh_skill_store_singleton() -> None:
    """Invalidate any in-process SkillStore cache.

    ``bridge.knowledge.skill_store.SkillStore`` is constructed per
    session (see ``bridge.freyja_bridge.SessionState``) — there is
    currently no module-level singleton to refresh. Its ``refresh()``
    method uses an mtime+size fingerprint that picks up our new
    SKILL.md the next time any session calls into the store, so a
    no-op here is correct today.

    If a global singleton is ever introduced (e.g. an mcp-skills server
    that holds one store across requests), thread the
    ``store.refresh()`` call through here. The function is structured
    so that adding a callable to a class-level registry would be a
    one-line change without touching ``promote``'s call site.
    """
    try:
        # Soft import: the skill_store module may not be loadable in
        # very-stripped test environments. We're best-effort.
        from bridge.knowledge import skill_store as _skill_store_mod
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("confirmation.refresh skill_store import_failed err=%s", exc)
        return

    # Look for a conventional singleton attribute. None exists today;
    # this is the hook point for when one is added.
    candidate_names = ("_INSTANCE", "_SINGLETON", "INSTANCE", "_default_store")
    for attr in candidate_names:
        inst = getattr(_skill_store_mod, attr, None)
        if inst is None:
            continue
        refresh = getattr(inst, "refresh", None)
        if callable(refresh):
            try:
                refresh()
                logger.debug("confirmation.refresh skill_store via=%s", attr)
            except Exception as exc:  # pragma: no cover - defensive
                logger.warning(
                    "confirmation.refresh skill_store failed attr=%s err=%s",
                    attr, exc,
                )
            return
    # No singleton found — that's the expected branch today.
