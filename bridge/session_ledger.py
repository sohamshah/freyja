"""Session action ledger — runtime-authored ground truth of what an agent did.

This is the "Effect Ledger" from ``docs/GROUNDED-MEMORY-DESIGN.md``. It exists
because the agent's *live self-model of its own actions* must not depend on the
lossy, write-action-blind, opt-in context channels (tool-result pruning, the
cheap ``summarize_context`` scopes, an LLM summary). Those channels caused the
``session-mpxlhh4o`` incident where the agent built ``widget_tools.py`` (680
lines) and then told the user "everything in our conversation so far has been
read-only exploration."

The ledger is the *trust anchor*: every consequential action is captured
deterministically at the tool-result hook, written append-only to disk
(survives compaction by construction), and surfaced back into context as a
first-person ``<system-reminder>`` every turn. The agent literally cannot claim
"read-only" while ``created widget_tools.py (680 lines)`` is standing in its
current request.

Two row classes, mirroring the design's "effect vs observation" split:

  • **effect** — a consequential / irreversible action (file write/edit,
    generated image, a mutating shell command, a spawned sub-agent). Always
    kept, always injected. Small and high-signal.
  • **observation** — a look-around (read/grep/glob/web_search/web_fetch).
    Recorded compactly (target + size + the originating ``toolCallId`` so the
    full result is recall-able from ``raw_messages.jsonl``) but NOT injected,
    so 150 reads can't drown 48 writes the way they did in the incident.

This module is *pure-ish*: the class does disk I/O, but every classification /
rendering / detection helper is a free function so it can be unit-tested without
a session. The ledger lives in ``bridge/`` (never imported by ``engine/`` — the
engine stays ignorant of bridge stores and only receives injected strings).
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


# Tools whose successful result is a consequential action worth remembering as
# something the agent *did*. File mutations mirror artifact_store's
# MUTATING_FILE_TOOL_NAMES; the rest extend coverage to image generation and
# (via classify_bash_command) mutating shell commands.
EFFECT_FILE_TOOLS = frozenset({"write_file", "edit_file", "edit_json"})
EFFECT_OTHER_TOOLS = frozenset({"generate_image"})

# Tools that look around but don't change the world.
OBSERVATION_TOOLS = frozenset({
    "read_file", "grep", "glob", "list_directory",
    "web_search", "web_fetch", "fetch_url", "view_image",
})
# Of the observations, only *research* (external lookups) is worth a durable
# ledger row — the user explicitly wanted "research / web search done" kept.
# Local reads/greps are high-volume noise already recoverable via `recall`
# over raw_messages.jsonl, so we don't persist a row for them (keeps the
# ledger high-signal and small).
RESEARCH_TOOLS = frozenset({"web_search", "web_fetch", "fetch_url"})

# Mutating shell verbs. A bash command matching one of these is an effect; any
# other bash command (ls, cat, git status, grep, …) is treated as an
# observation and left out of the injected reminder. High-recall heuristic — we
# would rather over-capture a shell effect than silently miss one (the incident
# proved silence is the dangerous failure). A future upgrade can replace this
# with a `git status --porcelain` before/after delta for exact file attribution.
_BASH_EFFECT_PATTERNS = (
    r"\bgit\s+(commit|push|merge|rebase|reset|checkout\s+-b|cherry-pick|tag|apply|am|revert|stash\s+(push|pop|apply)|clean)\b",
    r"\bgh\s+(pr|release|issue)\s+(create|edit|merge|close)\b",
    r"(^|[^>])>>?(?!\s*&)\s*[^\s|&]+",  # output redirection into a file (not >&2)
    r"\btee\b",
    r"\b(mkdir|mv|cp|rm|rmdir|ln|touch|chmod|chown|truncate|dd|sed\s+-i|patch)\b",
    r"\b(npm|pnpm|yarn|bun)\s+(i|install|add|remove|ci|run\s+build)\b",
    r"\b(uv|pip|pip3|poetry)\s+(install|add|sync|remove)\b",
    r"\b(make|cargo|go)\s+(build|install|run|test|generate)\b",
    r"\bdocker\s+(build|run|compose)\b",
    r"\balembic\s+(upgrade|downgrade|revision)\b",
)
_BASH_EFFECT_RE = re.compile("|".join(_BASH_EFFECT_PATTERNS), re.IGNORECASE)

# Strong negative self-claims that, when the ledger holds real effects, almost
# certainly mean the agent forgot work it did. Deliberately conservative — we
# match unambiguous "I did nothing / only looked" assertions, not hedged or
# future-tense statements, to keep false positives near zero.
_NEGATIVE_CLAIM_RE = re.compile(
    r"(no recollection of (making|any) (changes|edits)"
    r"|(haven'?t|did\s*not|didn'?t|have not)\s+(made|make)\s+any\s+(changes|edits|modifications)"
    r"|no\s+(changes|edits|modifications)\s+(were\s+)?(made|done)"
    r"|(only|just)\s+(did\s+)?read[\- ]only\s+(exploration|work)"
    r"|everything\b[^.\n]{0,60}\bread[\-\s]only"
    r"|nothing\s+(was\s+)?(written|created|changed|modified)(?!\s+about)"
    r"|i\s+have\s+not\s+(written|created|edited|modified|changed)\s+any)",
    re.IGNORECASE,
)


def classify_tool(tool_name: str) -> str:
    """Return ``"effect"``, ``"observation"``, or ``"neither"`` for a tool.

    ``bash`` is special-cased to ``"neither"`` here because it needs the command
    string to decide — callers route bash through ``classify_bash_command``.
    """
    if tool_name in EFFECT_FILE_TOOLS or tool_name in EFFECT_OTHER_TOOLS:
        return "effect"
    if tool_name in OBSERVATION_TOOLS:
        return "observation"
    return "neither"


def classify_bash_command(command: str) -> str:
    """Classify a bash command as ``"effect"`` or ``"observation"``.

    Effect = matches a known mutating verb/redirect. Everything else is an
    observation (the agent looked at something via the shell).
    """
    if not command or not command.strip():
        return "observation"
    return "effect" if _BASH_EFFECT_RE.search(command) else "observation"


_PORCELAIN_VERB = {
    "M": "modified", " M": "modified", "MM": "modified", "AM": "modified",
    "A": "added", "A ": "added", "??": "created",
    "D": "deleted", " D": "deleted", "R": "renamed", "C": "copied",
}


def _parse_porcelain(text: str) -> dict[str, str]:
    """Map path -> 2-char status code from `git status --porcelain` output."""
    out: dict[str, str] = {}
    for line in (text or "").splitlines():
        if len(line) < 3:
            continue
        code = line[:2]
        rest = line[3:]
        # Renames render as "old -> new"; attribute to the new path.
        if " -> " in rest:
            rest = rest.split(" -> ", 1)[1]
        path = rest.strip().strip('"')
        if path:
            out[path] = code
    return out


def git_status_delta(before: str, after: str) -> list[dict[str, str]]:
    """Diff two `git status --porcelain` snapshots into per-file effects.

    Pure + testable. A file that newly appears or changes status is reported
    with a verb from its code (modified/added/created/deleted/renamed). A file
    that LEAVES the dirty set (present before, gone after) is reported as
    "committed" — that's how a `git commit` shows up (staged files become clean).
    Branch creation / no-op commands produce an empty delta (the caller then
    falls back to recording the command itself).
    """
    b = _parse_porcelain(before)
    a = _parse_porcelain(after)
    out: list[dict[str, str]] = []
    for path, code in a.items():
        if path not in b or b[path] != code:
            verb = _PORCELAIN_VERB.get(code) or _PORCELAIN_VERB.get(code.strip()) or "changed"
            out.append({"op": verb, "path": path})
    for path in b:
        if path not in a:
            out.append({"op": "committed", "path": path})
    out.sort(key=lambda r: r["path"])
    return out


def detect_negative_self_claim(text: str) -> bool:
    """True if ``text`` makes a strong "I did nothing / only explored" claim.

    Used by the self-model monitor: if this fires while the ledger holds
    effects, the agent has very likely forgotten its own work and we inject a
    correction + emit a ``forgetting_detected`` telemetry row.
    """
    if not text:
        return False
    return bool(_NEGATIVE_CLAIM_RE.search(text))


def _short_path(path: str, *, segments: int = 2) -> str:
    """Last ``segments`` path components — enough to identify a file without a
    full absolute path bloating the reminder."""
    if not path:
        return ""
    parts = Path(path).parts
    if len(parts) <= segments:
        return path
    return "/".join(parts[-segments:])


def _parent_dir(path: str, *, segments: int = 2) -> str:
    parts = Path(path).parts[:-1]
    if not parts:
        return ""
    return "/".join(parts[-segments:])


def render_ledger_reminder(
    effects: list[dict[str, Any]],
    pinned_facts: list[str],
    *,
    just_compacted: bool = False,
    shell_note: bool = False,
    memory_present: bool = False,
    cap: int = 12,
) -> str | None:
    """Render the standing write-ledger ``<system-reminder>`` block.

    Pure function so it can be unit-tested. Returns ``None`` when there's
    nothing worth surfacing (no effects, facts, or memory). Phrasing is
    deliberately plain — no shouting capitals — and frames the content as the
    agent's own first-person actions so the model treats it as memory, not a
    third-party briefing.
    """
    if not effects and not pinned_facts and not memory_present:
        return None

    lines: list[str] = ["<system-reminder>"]
    if just_compacted:
        lines.append(
            "Context was just compacted, so older tool-result detail may be "
            "condensed — but nothing you did was lost. Here is the durable "
            "record of your own work this session (it survives every "
            "compaction; trust it over your in-context memory if they disagree):"
        )
    else:
        lines.append(
            "Durable record of what you've done this session (runtime ledger, "
            "survives compaction; trust it over your in-context memory if they "
            "disagree):"
        )

    if effects:
        lines.append("")
        lines.append("Files and actions you've created or changed:")
        for row in effects[:cap]:
            summary = str(row.get("summary") or "").strip()
            loc = row.get("repo") or row.get("dir") or ""
            suffix = f" — {loc}" if loc else ""
            lines.append(f"  • {summary}{suffix}")
        if len(effects) > cap:
            lines.append(
                f"  • …and {len(effects) - cap} more "
                "(call `recall` or `artifacts` to list all)."
            )

    if pinned_facts:
        lines.append("")
        lines.append("Notes you pinned as load-bearing:")
        for fact in pinned_facts[:6]:
            lines.append(f"  • {fact.strip()}")

    if shell_note:
        lines.append("")
        lines.append(
            "(Some shell commands this session may have changed files not "
            "listed above — run `git status` in the relevant repo if you need "
            "the exact set.)"
        )

    if memory_present:
        lines.append("")
        lines.append(
            "You also have working notes in `session_memory` — call "
            "session_memory(action='read') to recover anything the summary "
            "condensed."
        )

    lines.append("")
    lines.append(
        "This is runtime context, not the user — don't reply to it. Before you "
        "tell the user you made no changes or only explored, check this list "
        "and reconcile against `git status` / `artifacts`."
    )
    lines.append("</system-reminder>")
    return "\n".join(lines)


class SessionLedger:
    """Append-only, per-session action ledger backed by a JSONL file.

    Lives at ``<project_dir>/action_ledger.jsonl`` (sibling to ``manifest.jsonl``
    and ``memory.md``). Thread-safe appends. Distinct from the artifact manifest
    (which records *verified files* for handoff/read): this records *what the
    agent did*, for self-grounding, including non-file effects and observations.
    """

    def __init__(self, *, session_id: str, project_dir: Path) -> None:
        self.session_id = session_id
        self.project_dir = Path(project_dir).expanduser().resolve()
        self.path = self.project_dir / "action_ledger.jsonl"
        self._lock = threading.Lock()
        # In-memory mirror so reads (digest, reminder build) don't hit disk on
        # the hot per-turn path. Disk is the durable backstop / cross-process
        # source; this process is the only writer for its session.
        self._rows: list[dict[str, Any]] = []
        # Count of bash effects captured heuristically — drives the "run git
        # status" caveat in the reminder so we're honest about partial coverage.
        self.shell_effect_count = 0
        # mkdir once, not on every append (hot path).
        self._dir_ready = False
        # Repo roots with a git before/after capture currently in flight. Under
        # parallel tool execution, two mutating commands in the SAME repo would
        # each see the other's changes in their after-snapshot and mis-attribute
        # them; we let only one command attribute files per repo at a time and
        # fall the others back to command-only recording.
        self._git_inflight: set[str] = set()

    def begin_git_capture(self, repo_root: str) -> bool:
        """Claim exclusive git-delta attribution for ``repo_root``. Returns
        False if another capture is already in flight for that repo (caller
        should then skip the delta and record the command only)."""
        with self._lock:
            if repo_root in self._git_inflight:
                return False
            self._git_inflight.add(repo_root)
            return True

    def end_git_capture(self, repo_root: str) -> None:
        with self._lock:
            self._git_inflight.discard(repo_root)

    def ensure(self) -> None:
        try:
            self.project_dir.mkdir(parents=True, exist_ok=True)
            self._dir_ready = True
        except Exception:  # noqa: BLE001
            logger.debug("session ledger ensure() failed", exc_info=True)
        # Hydrate from disk so the ledger survives resume / app restart. The
        # forgetting incident itself happened on a RESUMED turn — without this
        # the on-disk ledger is write-only and the standing reminder goes
        # silent after a restart, defeating the whole point.
        self._load_from_disk()
        # Backfill from the artifact manifest so sessions that predate the
        # ledger (or whose ledger file is missing) aren't blank on resume.
        self._backfill_from_manifest()

    def _load_from_disk(self) -> None:
        self._rows = []
        self.shell_effect_count = 0
        try:
            if not self.path.exists():
                return
            for line in self.path.read_text(encoding="utf-8", errors="replace").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                self._rows.append(row)
                if row.get("kind") == "shell_effect":
                    self.shell_effect_count += 1
        except Exception:  # noqa: BLE001
            logger.debug("session ledger hydrate failed", exc_info=True)

    def _backfill_from_manifest(self) -> None:
        """Seed effects from the existing artifact manifest for sessions that
        predate the ledger, so a resumed old session isn't blank.

        In-memory only (not persisted): the manifest stays the source for these
        pre-ledger writes, and ``effects()`` dedups by path so a later real
        ledger row for the same file supersedes the backfilled one. Only paths
        not already present in ``_rows`` are added, so this is a no-op once the
        ledger has its own rows.
        """
        try:
            manifest = self.project_dir / "manifest.jsonl"
            if not manifest.exists():
                return
            have_paths = {r.get("path") for r in self._rows if r.get("path")}
            for line in manifest.read_text(encoding="utf-8", errors="replace").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    m = json.loads(line)
                except json.JSONDecodeError:
                    continue
                path = m.get("path")
                op = str(m.get("operation") or "")
                if not path or path in have_paths:
                    continue
                if op not in ("write", "edit", "create", "update"):
                    continue  # skip legacy / subagent_artifact handoff imports
                verb = "created" if op in ("write", "create") else "edited"
                kind = "file_create" if verb == "created" else "file_edit"
                name = _short_path(str(path))
                lines = m.get("lines")
                summary = f"{verb} {name}" + (f" ({lines} lines)" if lines else "")
                self._rows.append({
                    "kind": kind,
                    "class": "effect",
                    "operation": "create" if verb == "created" else "edit",
                    "summary": summary,
                    "path": path,
                    "dir": _parent_dir(str(path)),
                    "creatorId": m.get("creatorId") or self.session_id,
                    "createdAt": int(m.get("createdAt") or 0),
                    "source": "manifest_backfill",
                })
                have_paths.add(path)
        except Exception:  # noqa: BLE001
            logger.debug("session ledger manifest backfill failed", exc_info=True)

    # ── recording ────────────────────────────────────────────────────────

    def record_effect(
        self,
        *,
        kind: str,
        operation: str,
        summary: str,
        path: str | None = None,
        repo: str | None = None,
        tool_call_id: str | None = None,
        creator_id: str | None = None,
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        row = {
            "kind": kind,
            "class": "effect",
            "operation": operation,
            "summary": summary,
            "path": path,
            "repo": repo,
            "dir": _parent_dir(path) if path else None,
            "toolCallId": tool_call_id,
            "creatorId": creator_id or self.session_id,
            "createdAt": int(time.time() * 1000),
        }
        if extra:
            row.update(extra)
        self._append(row)
        if kind == "shell_effect":
            self.shell_effect_count += 1
        return row

    def record_observation(
        self,
        *,
        kind: str,
        target: str,
        result_chars: int = 0,
        tool_call_id: str | None = None,
        creator_id: str | None = None,
    ) -> dict[str, Any]:
        row = {
            "kind": kind,
            "class": "observation",
            "target": target,
            "resultChars": int(result_chars or 0),
            "toolCallId": tool_call_id,  # archiveRef into raw_messages.jsonl
            "creatorId": creator_id or self.session_id,
            "createdAt": int(time.time() * 1000),
        }
        self._append(row)
        return row

    def record_shell_git_effect(
        self,
        *,
        command: str,
        delta: list[dict[str, str]],
        repo: str | None = None,
        creator_id: str | None = None,
        tool_call_id: str | None = None,
    ) -> dict[str, Any] | None:
        """Record a mutating bash command with the EXACT files it changed,
        derived from a `git status --porcelain` before/after delta. Higher
        fidelity than the command-pattern heuristic (which is the fallback when
        no git repo / no delta)."""
        if not delta:
            return None
        files = ", ".join(_short_path(d["path"]) for d in delta[:6])
        more = f" +{len(delta) - 6} more" if len(delta) > 6 else ""
        short = command if len(command) <= 80 else command[:77] + "…"
        return self.record_effect(
            kind="shell_effect",
            operation="shell",
            summary=f"ran `{short}` → changed {files}{more}",
            repo=repo,
            tool_call_id=tool_call_id,
            creator_id=creator_id,
            extra={"command": command, "gitDelta": delta},
        )

    def record_pinned_fact(
        self, text: str, *, source: str = "preserve_facts", creator_id: str | None = None
    ) -> dict[str, Any] | None:
        text = (text or "").strip()
        if not text:
            return None
        row = {
            "kind": "pinned_fact",
            "class": "effect",
            "operation": "pin",
            "summary": text,
            "source": source,
            "creatorId": creator_id or self.session_id,
            "createdAt": int(time.time() * 1000),
        }
        self._append(row)
        return row

    def record_from_tool(
        self,
        *,
        tool_name: str,
        tool_args: dict[str, Any],
        result_text: str,
        result_chars: int,
        is_error: bool,
        tool_call_id: str | None = None,
        creator_id: str | None = None,
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """Classify a finished tool call and record the appropriate row.

        Returns the row written, or ``None`` if the tool isn't ledger-worthy or
        the call errored. Best-effort: never raises (callers are on the agent
        loop's hot path).

        ``extra`` carries auxiliary effect-row fields the caller has already
        computed — notably the file-change diff stats
        (``additions``/``deletions``/``diff``/``diffTruncated``) for a mutating
        file tool — which are merged onto the effect row so the panel/JSON can
        render a ``+N −M`` MarginMark and a diff peek. Ignored for observations.
        """
        try:
            if is_error:
                return None
            if tool_name == "bash":
                return self._record_bash(
                    tool_args, result_text, result_chars, tool_call_id, creator_id
                )
            klass = classify_tool(tool_name)
            if klass == "effect":
                return self._record_file_or_image_effect(
                    tool_name, tool_args, result_text, tool_call_id, creator_id,
                    extra=extra,
                )
            if klass == "observation":
                # Only persist research lookups; local reads/greps are noise.
                if tool_name not in RESEARCH_TOOLS:
                    return None
                return self.record_observation(
                    kind=tool_name,
                    target=self._observation_target(tool_name, tool_args),
                    result_chars=result_chars,
                    tool_call_id=tool_call_id,
                    creator_id=creator_id,
                )
            return None
        except Exception:  # noqa: BLE001
            logger.debug("session ledger record_from_tool failed", exc_info=True)
            return None

    def _record_file_or_image_effect(
        self,
        tool_name: str,
        tool_args: dict[str, Any],
        result_text: str,
        tool_call_id: str | None,
        creator_id: str | None = None,
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        if tool_name == "generate_image":
            m = re.search(r"File saved to `([^`]+)`", result_text or "")
            path = m.group(1) if m else (tool_args.get("path") or "")
            name = _short_path(path) or "image"
            return self.record_effect(
                kind="generate_image",
                operation="create",
                summary=f"generated image {name}",
                path=path or None,
                tool_call_id=tool_call_id,
                creator_id=creator_id,
            )
        path = str(tool_args.get("path") or "")
        if not path:
            return None
        name = _short_path(path)
        op = "create" if tool_name == "write_file" else "edit"
        # Pull "(N lines)" out of the write/edit confirmation when present.
        lines_m = re.search(r"\((\d[\d,]*)\s+lines?\)", result_text or "")
        verb = "created" if op == "create" else "edited"
        if lines_m:
            summary = f"{verb} {name} ({lines_m.group(1)} lines)"
        else:
            summary = f"{verb} {name}"
        return self.record_effect(
            kind=f"file_{op}",
            operation=op,
            summary=summary,
            path=path,
            tool_call_id=tool_call_id,
            creator_id=creator_id,
            # Diff stats (additions/deletions/diff/diffTruncated) land on the
            # row for the panel's +N/−M MarginMark + diff peek. Never folded
            # into `summary`, so the standing reminder stays diff-free.
            extra=extra or None,
        )

    def _record_bash(
        self,
        tool_args: dict[str, Any],
        result_text: str,
        result_chars: int,
        tool_call_id: str | None,
        creator_id: str | None = None,
    ) -> dict[str, Any] | None:
        command = str(tool_args.get("command") or tool_args.get("cmd") or "").strip()
        if classify_bash_command(command) == "effect":
            short = command if len(command) <= 120 else command[:117] + "…"
            return self.record_effect(
                kind="shell_effect",
                operation="shell",
                summary=f"ran `{short}`",
                tool_call_id=tool_call_id,
                creator_id=creator_id,
            )
        # Non-mutating shell (ls, cat, git status, …) — noise, don't persist.
        return None

    @staticmethod
    def _observation_target(tool_name: str, tool_args: dict[str, Any]) -> str:
        if tool_name in ("web_search",):
            return str(tool_args.get("query") or "")
        if tool_name in ("web_fetch", "fetch_url"):
            return str(tool_args.get("url") or tool_args.get("query") or "")
        if tool_name == "grep":
            return str(tool_args.get("pattern") or "")
        return str(
            tool_args.get("path") or tool_args.get("pattern")
            or tool_args.get("query") or ""
        )

    # ── reading ──────────────────────────────────────────────────────────

    def _snapshot(self) -> list[dict[str, Any]]:
        """A shallow copy of the rows taken under the lock, so reads never
        iterate a list that a concurrent ``record_*`` is appending to (the
        compaction projection / git capture can append from a worker thread
        while the event loop builds the reminder)."""
        with self._lock:
            return list(self._rows)

    def effects(self, creator_id: str | None = None) -> list[dict[str, Any]]:
        """Effect rows, newest-first, deduped: file effects collapse to the
        newest row per path; pinned facts are excluded (surfaced separately).

        When ``creator_id`` is given, only that creator's effects are returned —
        the bridge passes its own session id so a sub-agent's writes (recorded
        into the same shared ledger) don't flood the parent's reminder."""
        latest_by_path: dict[str, dict[str, Any]] = {}
        non_file: list[dict[str, Any]] = []
        for row in self._snapshot():
            if row.get("class") != "effect" or row.get("kind") == "pinned_fact":
                continue
            if creator_id is not None and row.get("creatorId") != creator_id:
                continue
            path = row.get("path")
            if path:
                prev = latest_by_path.get(path)
                if prev is None or int(row.get("createdAt") or 0) >= int(
                    prev.get("createdAt") or 0
                ):
                    # Preserve original op: if the file was ever *created* this
                    # session, keep "created" as the headline verb.
                    if prev is not None and prev.get("operation") == "create":
                        row = {**row, "operation": "create", "summary": row.get("summary")}
                    latest_by_path[path] = row
            else:
                non_file.append(row)
        merged = list(latest_by_path.values()) + non_file
        merged.sort(key=lambda r: int(r.get("createdAt") or 0), reverse=True)
        return merged

    def pinned_facts(self, creator_id: str | None = None) -> list[str]:
        seen: set[str] = set()
        out: list[str] = []
        for row in self._snapshot():
            if row.get("kind") != "pinned_fact":
                continue
            if creator_id is not None and row.get("creatorId") != creator_id:
                continue
            txt = str(row.get("summary") or "").strip()
            if txt and txt not in seen:
                seen.add(txt)
                out.append(txt)
        return out

    def has_effects(self, creator_id: str | None = None) -> bool:
        return any(
            r.get("class") == "effect"
            and r.get("kind") != "pinned_fact"
            and (creator_id is None or r.get("creatorId") == creator_id)
            for r in self._snapshot()
        )

    def digest(self, creator_id: str | None = None) -> str:
        """Stable hash of the current effect set + pinned facts. The reminder
        producer re-emits only when this changes (plus a turn floor)."""
        effs = self.effects(creator_id=creator_id)
        key = "|".join(
            f"{r.get('kind')}:{r.get('path') or r.get('summary')}" for r in effs
        )
        key += "##" + "|".join(self.pinned_facts(creator_id=creator_id))
        return hashlib.sha1(key.encode("utf-8", "replace")).hexdigest()[:16]

    # ── persistence ──────────────────────────────────────────────────────

    def _append(self, row: dict[str, Any]) -> None:
        row.setdefault("sessionId", self.session_id)
        try:
            line = json.dumps(row, ensure_ascii=False, sort_keys=True)
        except Exception:  # noqa: BLE001
            line = None
        # Mutate the in-memory list AND write the file under the same lock so a
        # concurrent reader (snapshot) never sees a half-updated list, and two
        # writers never interleave file lines.
        with self._lock:
            self._rows.append(row)
            try:
                if not self._dir_ready:
                    self.project_dir.mkdir(parents=True, exist_ok=True)
                    self._dir_ready = True
                if line is not None:
                    with self.path.open("a", encoding="utf-8") as fh:
                        fh.write(line + "\n")
            except Exception:  # noqa: BLE001
                logger.debug("session ledger append failed", exc_info=True)
