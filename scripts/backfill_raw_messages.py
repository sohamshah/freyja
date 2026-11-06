"""Find the messages each session's raw message log is missing, and add them to
a recovered log beside it.

Until e8f7528 a restored session (after an app or gateway restart, or a
reload) stopped writing ``raw_messages.jsonl``, and until 1d1abf3 a sub-agent
wrote no raw log. Exports and recall read that log. Most of the missing
messages are still in the session's transcript
(``sessions/<id>.transcript.json``) or in the snapshots of its compactions
(``sessions/compactions/<id>-<ms>-before.json`` and ``-after.json``). This
finds each message there that the raw log does not have and writes it, in the
raw log's row format, to a separate file that exports read:

    projects/<id>/raw_messages.recovered.jsonl    a root session
    sessions/<id>.raw_messages.recovered.jsonl    a sub-agent, or a session
                                                  whose project dir is another's

The raw logs are never rewritten or reordered: the briefing reads their
mtimes, and the bridge appends to them while it runs. Each added row has
``"recovered": true``, ``"recovered_from"`` (the file it was read from) and
``"whole"``. A tool result that halving or a cheap ``summarize_context`` scope
cut is put back whole from ``~/.freyja/truncated`` when its marker names a
file that matches it (``"restored_from"``). Otherwise the row keeps the
compacted text and has ``"whole": false``. Messages a compaction summarized
while the raw log was not written, with no snapshot, are gone: only their
summary in ``compactions.jsonl`` is left. The script counts them too.

    uv run python scripts/backfill_raw_messages.py              # dry run
    uv run python scripts/backfill_raw_messages.py --apply      # append
    uv run python scripts/backfill_raw_messages.py --session ID --days 21 --roots-only

A run adds only what is still missing, so it can run again.
"""

from __future__ import annotations

import argparse
import bisect
import json
import os
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

HOME = Path(os.environ.get("FREYJA_HOME") or os.path.expanduser("~/.freyja"))

RECOVERED_NAME = "raw_messages.recovered.jsonl"

# A transcript entry and the raw row the bridge wrote for it are stamped
# within a few milliseconds of each other (measured: at most 2.2 ms).
_SAME_MESSAGE_S = 0.1

# What halving leaves in a result (engine/session.py), with the file since
# b6c4149 and without it before.
_HALVED_RE = re.compile(
    r"\n\n\[Content truncated - ([\d,]+) total chars, middle removed\. Full output saved to: "
    r"(.+?) \(read the removed part from that file, e\.g\. with grep or sed\)\]\n\n"
)
_HALVED_ANY_RE = re.compile(r"\[Content truncated - [\d,]+ total chars, middle removed\.")
# What a cheap summarize_context scope leaves (bridge/tools/summarize_context_tool.py),
# with the file since fb4f5b8 and without it before.
_CONDENSED_RE = re.compile(
    r"\[(?:tool result condensed|exploration result condensed \([^)]*\)): ([\d,]+) chars"
    r" · full output saved to: (.+?) \(read it from that file, e\.g\. with grep or sed\)"
    r"( · \d+ \w+(?: and \d+ \w+)* removed from context [^\]]*?)? · preview: (.*)\]",
    re.DOTALL,
)
_CONDENSED_ANY_RE = re.compile(r"\[(?:tool|exploration) result condensed\b")
# The truncator's cut, made when the tool ran: the model received it so, and
# the export reads the saved file itself (src/main/sessionRecord.ts).
_CUT_WHEN_RUN_RE = re.compile(r"\[Content truncated -- showing ~[\d,]+ of [\d,]+ tokens")
# Other rewrites of history that keep nothing to restore from.
_REMOVED_RE = re.compile(
    r"omitted from model history|was removed from history to save context"
)


def _sidecar_id(session_id: str) -> str:
    """bridge/transcript_persistence.py _sanitize_session_id."""
    return "".join(c if c.isalnum() or c in "_-." else "_" for c in session_id)[:160]


def _project_id(session_id: str) -> str:
    """bridge/project_paths.py safe_session_id."""
    raw = (session_id or "session").strip() or "session"
    return re.sub(r"[^A-Za-z0-9_.-]+", "-", raw).strip(".-") or "session"


def _snapshot_id(session_id: str) -> str:
    """bridge/freyja_bridge.py _write_compaction_snapshot."""
    return "".join(c for c in session_id if c.isalnum() or c in ("-", "_", "."))[:120]


def _rows(path: Path) -> Iterator[dict[str, Any]]:
    try:
        fh = path.open("rb")
    except OSError:
        return
    with fh:
        for line in fh:
            try:
                row = json.loads(line)
            except ValueError:
                continue  # a line an interrupted write cut short
            if isinstance(row, dict):
                yield row


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _rel(path: Path, home: Path) -> str:
    try:
        return str(path.relative_to(home))
    except ValueError:
        return str(path)


def _call_ids(message: dict[str, Any]) -> tuple[str, ...]:
    return tuple(
        str(call["id"])
        for call in message.get("tool_calls") or []
        if isinstance(call, dict) and call.get("id")
    )


def _identity(message: dict[str, Any]) -> tuple[str, str, tuple[str, ...]]:
    """What a message is matched on besides its time: its role and the tool
    calls it makes or answers."""
    return (str(message.get("role")), str(message.get("tool_call_id") or ""), _call_ids(message))


@dataclass
class _Logged:
    """What a session's raw logs (and its recovered rows) already hold."""

    stamps: dict[tuple[str, str, tuple[str, ...]], list[float]] = field(default_factory=dict)
    result_ids: set[str] = field(default_factory=set)
    call_ids: set[str] = field(default_factory=set)
    last_live_ts: float = 0.0

    def add(self, row: dict[str, Any], *, live: bool) -> None:
        message = row.get("message")
        if not isinstance(message, dict):
            return
        ts = row.get("ts")
        if isinstance(ts, (int, float)):
            self.stamps.setdefault(_identity(message), []).append(float(ts))
            if live:
                self.last_live_ts = max(self.last_live_ts, float(ts))
        if message.get("tool_call_id"):
            self.result_ids.add(str(message["tool_call_id"]))
        self.call_ids.update(_call_ids(message))

    def has(self, message: dict[str, Any], ts: float) -> bool:
        """The log holds this message: a row like it stamped when it was
        appended, or (so a call is never added twice) a row with one of its
        tool call ids."""
        if message.get("tool_call_id") and str(message["tool_call_id"]) in self.result_ids:
            return True
        if any(call_id in self.call_ids for call_id in _call_ids(message)):
            return True
        stamps = self.stamps.get(_identity(message), [])
        i = bisect.bisect_left(stamps, ts - _SAME_MESSAGE_S)
        return i < len(stamps) and stamps[i] <= ts + _SAME_MESSAGE_S


def _saved_text(file: str, home: Path) -> str | None:
    """A file the engine saved under ``truncated/``, and only there: the path
    comes from text in a transcript."""
    try:
        real = Path(file).resolve()
        root = (home / "truncated").resolve()
        if root not in real.parents:
            return None
        return real.read_text(encoding="utf-8")
    except (OSError, ValueError):
        return None


@dataclass
class _Recovered:
    message: dict[str, Any]
    whole: bool
    kind: str  # "as_saved", "restored", "cut_when_run", "compacted"
    restored_from: str | None = None


def _texts(message: dict[str, Any]) -> list[str]:
    content = message.get("content")
    if isinstance(content, str):
        return [content]
    out: list[str] = []
    for block in content if isinstance(content, list) else []:
        if isinstance(block, dict) and isinstance(block.get("text"), str):
            out.append(block["text"])
    return out


def recover_message(message: dict[str, Any], home: Path = HOME) -> _Recovered:
    """The message as it was appended, as far as the transcript allows: a
    halved or condensed result put back whole from the file its marker names,
    when that file matches it."""
    content = message.get("content")
    if message.get("role") == "tool_result" and isinstance(content, str):
        halved = _HALVED_RE.search(content)
        if halved:
            total = int(halved.group(1).replace(",", ""))
            saved = _saved_text(halved.group(2), home)
            head, tail = content[: halved.start()], content[halved.end():]
            if saved is not None and len(saved) == total and saved.startswith(head) and saved.endswith(tail):
                return _Recovered({**message, "content": saved}, True, "restored", halved.group(2))
        condensed = _CONDENSED_RE.fullmatch(content)
        if condensed:
            total = int(condensed.group(1).replace(",", ""))
            saved = _saved_text(condensed.group(2), home)
            if saved is not None and len(saved) == total:
                first = saved.strip().splitlines()[0][:200] if saved.strip() else ""
                if repr(first) == condensed.group(4):
                    # The file has the text only: images it held stay lost.
                    whole = condensed.group(3) is None
                    return _Recovered(
                        {**message, "content": saved}, whole, "restored" if whole else "compacted",
                        condensed.group(2),
                    )
    texts = _texts(message)
    if any(_HALVED_ANY_RE.search(t) or _CONDENSED_ANY_RE.match(t) or _REMOVED_RE.search(t) for t in texts):
        return _Recovered(message, False, "compacted")
    if message.get("role") == "tool_result" and any(_CUT_WHEN_RUN_RE.search(t) for t in texts):
        return _Recovered(message, True, "cut_when_run")
    return _Recovered(message, True, "as_saved")


_RANK = {"as_saved": 0, "cut_when_run": 0, "restored": 1, "compacted": 2}


@dataclass
class SessionReport:
    session_id: str
    target: Path
    rows: list[dict[str, Any]]
    from_transcript: int = 0
    from_snapshots: int = 0
    kinds: dict[str, int] = field(default_factory=dict)
    results: dict[str, int] = field(default_factory=dict)
    lost_entries: int = 0
    is_root: bool = True


def _snapshots_by_session(home: Path) -> dict[str, list[tuple[int, Path]]]:
    """Each session's compaction snapshots, oldest first, by the session id
    the snapshot itself records."""
    out: dict[str, list[tuple[int, Path]]] = {}
    pattern = re.compile(r"-(\d+)-(?:before|after)\.json$")
    for path in sorted((home / "sessions" / "compactions").glob("*.json")):
        m = pattern.search(path.name)
        data = _read_json(path)
        if not data or data.get("version") != 1 or not isinstance(data.get("session_id"), str):
            continue
        out.setdefault(data["session_id"], []).append((int(m.group(1)) if m else 0, path))
    for paths in out.values():
        paths.sort()
    return out


def backfill_session(
    transcript_path: Path,
    session_id: str,
    snapshots: list[tuple[int, Path]],
    home: Path = HOME,
) -> SessionReport:
    """What a session's raw log is missing, as rows to append."""
    sources: list[tuple[str, Path, dict[str, Any]]] = []
    for _stamp, path in snapshots:
        data = _read_json(path)
        if data:
            sources.append(("snapshot", path, data))
    # The transcript is read before the raw logs: a message it holds was
    # appended, and logged, before the transcript was saved.
    data = _read_json(transcript_path)
    if data and data.get("version") == 1:
        sources.append(("transcript", transcript_path, data))
    metadata = next((d.get("metadata") for _k, _p, d in reversed(sources) if isinstance(d.get("metadata"), dict)), {})

    sidecar = home / "sessions" / _sidecar_id(session_id)
    project = home / "projects" / _project_id(session_id)
    project_session = metadata.get("project_session_id") or metadata.get("parent_session_id")
    is_root = not (
        metadata.get("parent_session_id")
        or metadata.get("subagent_id")
        or (project_session and project_session != session_id)
        or (home / "sessions" / f"{_sidecar_id(session_id)}.subagent.json").exists()
    )
    target = (project / RECOVERED_NAME) if is_root else Path(f"{sidecar}.{RECOVERED_NAME}")

    logged = _Logged()
    for path in (Path(f"{sidecar}.raw_messages.jsonl"), project / "raw_messages.jsonl"):
        for row in _rows(path):
            logged.add(row, live=True)
    for path in (Path(f"{sidecar}.{RECOVERED_NAME}"), project / RECOVERED_NAME):
        for row in _rows(path):
            logged.add(row, live=False)
    for stamps in logged.stamps.values():
        stamps.sort()

    # Each entry once, in its least compacted version (the earliest source
    # wins a tie: halving and condensing only ever add up).
    best: dict[str, tuple[int, int, str, Path, float, _Recovered]] = {}
    for order, (kind, path, data) in enumerate(sources):
        for entry in (data.get("transcript") or {}).get("entries") or []:
            message = entry.get("message")
            if not isinstance(message, dict) or not entry.get("id"):
                continue
            ts = float(entry.get("timestamp") or 0.0)
            if logged.has(message, ts):
                continue
            recovered = recover_message(message, home)
            candidate = (_RANK[recovered.kind], order, kind, path, ts, recovered)
            held = best.get(entry["id"])
            if held is None or candidate[:2] < held[:2]:
                best[entry["id"]] = candidate

    report = SessionReport(session_id, target, [], is_root=is_root)
    for _rank, _order, kind, path, ts, recovered in sorted(best.values(), key=lambda c: c[4]):
        row: dict[str, Any] = {
            "ts": ts,
            "session_id": session_id,
            "turn_id": None,
            "message": recovered.message,
            "recovered": True,
            "recovered_from": _rel(path, home),
            "whole": recovered.whole,
        }
        if recovered.restored_from:
            row["restored_from"] = _rel(Path(recovered.restored_from), home)
        report.rows.append(row)
        if kind == "transcript":
            report.from_transcript += 1
        else:
            report.from_snapshots += 1
        report.kinds[recovered.kind] = report.kinds.get(recovered.kind, 0) + 1
        if recovered.message.get("role") == "tool_result":
            report.results[recovered.kind] = report.results.get(recovered.kind, 0) + 1

    # Compactions that ran after the raw log's last row, with no snapshot
    # from before them: what they summarized is gone but for the summary.
    before_stamps = [stamp / 1000 for stamp, p in snapshots if p.name.endswith("-before.json")]
    for path in (Path(f"{sidecar}.compactions.jsonl"), project / "compactions.jsonl"):
        for row in _rows(path):
            ts = row.get("ts")
            if row.get("subtype") != "compaction_complete" or not isinstance(ts, (int, float)):
                continue
            if ts <= logged.last_live_ts:
                continue
            if any(ts - 1800 <= s <= ts for s in before_stamps):
                continue
            report.lost_entries += int(row.get("entries_removed") or 0)
    return report


def _sessions(home: Path, only: str | None, days: float | None) -> Iterator[tuple[Path, str]]:
    """Every session with a transcript, as (transcript path, session id). A
    session known only from snapshots was deleted (or a test's)."""
    cutoff = time.time() - days * 86400 if days else None
    for path in sorted((home / "sessions").glob("*.transcript.json")):
        if cutoff is not None and path.stat().st_mtime < cutoff:
            continue
        data = _read_json(path)
        if not data:
            continue
        sid = data.get("session_id") if isinstance(data.get("session_id"), str) else path.name[: -len(".transcript.json")]
        if only and sid != only:
            continue
        yield path, sid


def _append(target: Path, rows: list[dict[str, Any]]) -> None:
    """Append rows as the bridge writes raw log rows: images to the media
    store, a row with a lone surrogate escaped."""
    from bridge.transcript_persistence import jsonl_line
    from engine.media_store import externalize_images

    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("a", encoding="utf-8") as fh:
        for row in rows:
            externalize_images(row)
            fh.write(jsonl_line(row))
            fh.write("\n")


def main(argv: list[str] | None = None, home: Path = HOME) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--apply", action="store_true", help="append the rows (default: dry run)")
    ap.add_argument("--session", help="only this session id")
    ap.add_argument("--days", type=float, help="only transcripts saved in the last N days")
    ap.add_argument("--roots-only", action="store_true",
                    help="leave sub-agents out (theirs are most of the rows: copies of their transcripts)")
    ap.add_argument("--quiet", action="store_true", help="print the totals only")
    args = ap.parse_args(argv)

    snapshots = _snapshots_by_session(home)

    totals = {"root": [0, 0, 0], "sub-agent": [0, 0, 0]}  # sessions, with missing, messages
    kinds: dict[str, int] = {}
    results: dict[str, int] = {}
    from_transcript = from_snapshots = lost = 0
    for transcript_path, sid in _sessions(home, args.session, args.days):
        report = backfill_session(transcript_path, sid, snapshots.get(sid, []), home)
        if args.roots_only and not report.is_root:
            continue
        group = totals["root" if report.is_root else "sub-agent"]
        group[0] += 1
        lost += report.lost_entries
        if not report.rows:
            continue
        group[1] += 1
        group[2] += len(report.rows)
        from_transcript += report.from_transcript
        from_snapshots += report.from_snapshots
        for k, v in report.kinds.items():
            kinds[k] = kinds.get(k, 0) + v
        for k, v in report.results.items():
            results[k] = results.get(k, 0) + v
        if not args.quiet:
            res = report.results
            print(
                f"{sid}: {len(report.rows)} missing ({report.from_transcript} transcript, "
                f"{report.from_snapshots} snapshots); results whole {res.get('as_saved', 0)}, "
                f"restored whole {res.get('restored', 0)}, cut when run {res.get('cut_when_run', 0)}, "
                f"compacted only {res.get('compacted', 0)}"
                + (f"; ~{report.lost_entries} entries summarized away" if report.lost_entries else "")
                + f" -> {_rel(report.target, home)}"
            )
        if args.apply:
            _append(report.target, report.rows)

    mode = "appended" if args.apply else "would append"
    print()
    for name, (count, missing, messages) in totals.items():
        print(f"{name} sessions: {count}, missing messages in {missing}: {messages:,}")
    print(f"{mode} {from_transcript + from_snapshots:,} rows "
          f"({from_transcript:,} from transcripts, {from_snapshots:,} from compaction snapshots)")
    print(f"tool results: {sum(results.values()):,}; whole as saved {results.get('as_saved', 0):,}, "
          f"restored whole from truncated/ {results.get('restored', 0):,}, "
          f"cut when run (the export reads its file) {results.get('cut_when_run', 0):,}, "
          f"only the compacted text {results.get('compacted', 0):,}")
    print(f"other messages with compacted text: {kinds.get('compacted', 0) - results.get('compacted', 0):,}")
    print(f"entries summarized while no raw log was written, with no snapshot (lost; summary only): ~{lost:,}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
