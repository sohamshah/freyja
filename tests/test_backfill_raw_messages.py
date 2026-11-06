"""scripts/backfill_raw_messages.py finds what a raw log is missing and adds it
beside the log, never into it.

Until e8f7528 a restored session stopped writing raw_messages.jsonl, and
sub-agents had none before 1d1abf3. The messages are in the transcript and in
compaction snapshots; the script writes them, marked recovered, to
raw_messages.recovered.jsonl, and puts halved or condensed results back whole
from ~/.freyja/truncated when the marker names a file that matches.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "backfill_raw_messages",
    Path(__file__).resolve().parents[1] / "scripts" / "backfill_raw_messages.py",
)
backfill = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = backfill
_SPEC.loader.exec_module(backfill)

WHOLE = "".join(f"line {i} of the output\n" for i in range(300))


def _entry(eid, ts, message, parent=None):
    return {"id": eid, "is_compaction": False, "timestamp": ts, "message": message,
            **({"parent_id": parent} if parent else {})}


def _transcript(home: Path, sid: str, entries, metadata=None, name=None):
    path = home / "sessions" / f"{name or sid}.transcript.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "version": 1, "session_id": sid, "created_at": 1, "last_activity": 2,
        "metadata": metadata or {}, "transcript": {"entries": entries, "head_id": entries[-1]["id"]},
    }))
    return path


def _raw(path: Path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as fh:
        for ts, message in rows:
            fh.write(json.dumps({"ts": ts, "session_id": "s", "turn_id": "t", "message": message}) + "\n")


def _halved(saved: Path) -> str:
    """What halving leaves of WHOLE, naming the file it saved."""
    head, tail = WHOLE[:2000], WHOLE[-1000:]
    return (head + f"\n\n[Content truncated - {len(WHOLE):,} total chars, middle removed. "
            f"Full output saved to: {saved} (read the removed part from that file, "
            "e.g. with grep or sed)]\n\n" + tail)


@pytest.fixture
def home(tmp_path, monkeypatch):
    # The media store (images in a row) is found from FREYJA_HOME.
    monkeypatch.setenv("FREYJA_HOME", str(tmp_path))
    (tmp_path / "truncated").mkdir()
    return tmp_path


def _setup_root(home: Path):
    """A root session whose raw log ends at its first restart (ts 3)."""
    saved = home / "truncated" / "bash_1.txt"
    saved.write_text(WHOLE)
    call = {"id": "c2", "name": "bash", "arguments": {"command": "cat big"}}
    entries = [
        _entry("e1", 1.0, {"role": "user", "content": "first"}),
        _entry("e2", 2.0, {"role": "assistant", "content": "ok"}),
        # After the restart: none of these reached the raw log.
        _entry("e3", 10.0, {"role": "user", "content": "second"}),
        _entry("e4", 11.0, {"role": "assistant", "content": "", "tool_calls": [call]}),
        _entry("e5", 12.0, {"role": "tool_result", "tool_call_id": "c2", "content": _halved(saved)}),
        _entry("e6", 13.0, {"role": "tool_result", "tool_call_id": "c3",
                            "content": "[tool result condensed: 9,000 chars · preview: 'x']"}),
    ]
    _transcript(home, "session-a", entries)
    raw = home / "projects" / "session-a" / "raw_messages.jsonl"
    _raw(raw, [(1.0012, {"role": "user", "content": "first"}),
               (2.0009, {"role": "assistant", "content": "ok"})])
    # A compaction folded in a message the transcript no longer has; its
    # snapshot still has it.
    snap = home / "sessions" / "compactions" / "session-a-9000-before.json"
    snap.parent.mkdir(parents=True)
    snap.write_text(json.dumps({
        "version": 1, "session_id": "session-a", "metadata": {},
        "transcript": {"entries": [entries[0], _entry("e2b", 5.0, {"role": "user", "content": "folded"})]},
    }))
    return raw


def test_a_dry_run_reports_what_is_missing_and_writes_nothing(home, capsys):
    raw = _setup_root(home)
    before = raw.read_bytes()

    assert backfill.main([], home=home) == 0

    out = capsys.readouterr().out
    assert "session-a: 5 missing (4 transcript, 1 snapshots)" in out
    assert "restored whole 1" in out and "compacted only 1" in out
    assert raw.read_bytes() == before
    assert not (raw.parent / "raw_messages.recovered.jsonl").exists()


def test_apply_appends_recovered_rows_beside_the_log_and_a_second_run_adds_nothing(home, capsys):
    raw = _setup_root(home)
    before = raw.read_bytes()

    backfill.main(["--apply"], home=home)

    assert raw.read_bytes() == before
    recovered = raw.parent / "raw_messages.recovered.jsonl"
    rows = [json.loads(line) for line in recovered.read_text().splitlines()]
    assert [r["ts"] for r in rows] == [5.0, 10.0, 11.0, 12.0, 13.0]
    assert all(r["recovered"] and r["session_id"] == "session-a" for r in rows)
    assert rows[0]["recovered_from"] == "sessions/compactions/session-a-9000-before.json"
    assert rows[1]["recovered_from"] == "sessions/session-a.transcript.json"
    result = rows[3]
    assert result["message"]["content"] == WHOLE
    assert (result["whole"], result["restored_from"]) == (True, "truncated/bash_1.txt")
    assert rows[4]["whole"] is False
    assert rows[4]["message"]["content"].startswith("[tool result condensed")

    capsys.readouterr()
    backfill.main(["--apply"], home=home)
    assert len(recovered.read_text().splitlines()) == 5
    assert "appended 0 rows" in capsys.readouterr().out


def test_a_file_that_does_not_match_the_result_is_not_used(home):
    other = home / "truncated" / "bash_2.txt"
    other.write_text("something else entirely\n" * 500)
    message = {"role": "tool_result", "tool_call_id": "c", "content": _halved(other)}

    recovered = backfill.recover_message(message, home)

    assert (recovered.whole, recovered.kind) == (False, "compacted")
    assert recovered.message is message


def test_a_file_outside_the_truncated_store_is_never_read(home, tmp_path_factory):
    outside = tmp_path_factory.mktemp("elsewhere") / "secret.txt"
    outside.write_text(WHOLE)
    message = {"role": "tool_result", "tool_call_id": "c", "content": _halved(outside)}

    assert backfill.recover_message(message, home).kind == "compacted"


def test_a_condensed_result_is_restored_from_the_file_its_preview_names(home):
    saved = home / "truncated" / "grep_1.txt"
    saved.write_text(WHOLE)
    message = {"role": "tool_result", "tool_call_id": "c", "content": (
        f"[exploration result condensed (grep): {len(WHOLE):,} chars · full output saved to: "
        f"{saved} (read it from that file, e.g. with grep or sed) · preview: 'line 0 of the output']"
    )}

    recovered = backfill.recover_message(message, home)

    assert (recovered.whole, recovered.kind, recovered.message["content"]) == (True, "restored", WHOLE)


def test_a_sub_agents_rows_go_beside_its_sidecars(home):
    _transcript(home, "sub_1a_1", [
        _entry("e1", 1.0, {"role": "user", "content": "the task"}),
        _entry("e2", 2.0, {"role": "assistant", "content": "done"}),
    ], metadata={"parent_session_id": "session-a", "project_session_id": "session-a"})
    recovered = home / "sessions" / "sub_1a_1.raw_messages.recovered.jsonl"

    backfill.main(["--apply", "--roots-only"], home=home)
    assert not recovered.exists()
    backfill.main(["--apply"], home=home)

    rows = recovered.read_text().splitlines()
    assert [json.loads(r)["message"]["content"] for r in rows] == ["the task", "done"]
    assert not (home / "projects" / "session-a").exists()
