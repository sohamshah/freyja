"""Branch / fork a session: transcript truncation by wall-clock, id
remapping, sidecar cloning and project-dir cloning.

These are the primitives behind the `branch_session` command. The
renderer anchors a branch on a message's ``createdAt`` (ms); the bridge
keeps every engine entry created before that instant. Ordinal counting
is only a legacy fallback — it never lined up with the renderer's
user/assistant numbering once a turn produced tool results or the
transcript was compacted.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import bridge.transcript_persistence as tp
from bridge.freyja_bridge import (
    _find_message_entry_index,
    _message_cutoff_ts,
    _truncate_session_at_message_ordinal,
)


def _entry(eid: str, role: str | None, ts: float, **extra) -> dict:
    d: dict = {"id": eid, "is_compaction": role is None, "timestamp": ts}
    if role is None:
        d["compaction_summary"] = "earlier stuff"
    else:
        d["message"] = {"role": role, "content": f"{role}@{ts}"}
    d.update(extra)
    return d


# A tool-heavy, compacted transcript: one renderer "assistant" message
# spans several engine entries, and the compaction entry stands in for
# everything before it.
ENTRIES = [
    _entry("c0", None, 1.0),
    _entry("u1", "user", 2.0),
    _entry("a1", "assistant", 3.0),
    _entry("t1", "tool_result", 3.5),
    _entry("a2", "assistant", 4.0),
    _entry("u2", "user", 5.0),
    _entry("a3", "assistant", 6.0),
    _entry("t2", "tool_result", 6.5),
    _entry("a4", "assistant", 7.0),
]


def _transcript(session_id: str, entries: list[dict], **meta) -> dict:
    metadata = {"model_id": "m", "reasoning_level": "high", "coordination_strategy": "bus"}
    metadata.update(meta)
    return {
        "version": 1,
        "session_id": session_id,
        "created_at": 1.0,
        "last_activity": 7.0,
        "compaction_count": 1,
        "tool_tokens": 0,
        "metadata": metadata,
        "transcript": {"entries": entries, "head_id": entries[-1]["id"]},
    }


@pytest.fixture
def sessions_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(tp, "SESSIONS_DIR", tmp_path)
    return tmp_path


# ── truncation ─────────────────────────────────────────────────────────


def test_truncate_entries_before_keeps_everything_older_than_cutoff():
    # Renderer user message #2 was stamped at 4.99s; the engine appended
    # its entry at 5.0s. Everything strictly before is kept, compaction
    # entry included, and head_id lands on the last kept entry.
    kept, head = tp.truncate_entries_before(ENTRIES, 4.99)
    assert [e["id"] for e in kept] == ["c0", "u1", "a1", "t1", "a2"]
    assert head == "a2"


def test_truncate_entries_before_treats_missing_timestamp_as_old():
    entries = [_entry("x", "user", 0.0)]
    del entries[0]["timestamp"]
    entries.append(_entry("y", "user", 9.0))
    kept, head = tp.truncate_entries_before(entries, 5.0)
    assert [e["id"] for e in kept] == ["x"]
    assert head == "x"


def test_clone_transcript_cutoff_beats_legacy_ordinal(sessions_dir):
    tp.save_transcript("src", _transcript("src", ENTRIES))
    assert tp.clone_transcript("src", "dst", truncate_to_message_ordinal=2, cutoff_ts=4.99)
    dst = tp.load_transcript("dst")
    assert dst is not None
    ids = [e["id"] for e in dst["transcript"]["entries"]]
    # The legacy ordinal (2) would have kept only c0,u1,a1 — the timestamp
    # keeps the whole first assistant turn including its tool results.
    assert ids == ["c0", "u1", "a1", "t1", "a2"]
    assert dst["transcript"]["head_id"] == "a2"
    assert dst["session_id"] == "dst"
    assert dst["metadata"]["forked_from"] == "src"


def test_clone_transcript_whole_copy_and_remap(sessions_dir):
    entries = ENTRIES + [
        _entry(
            "a5",
            "assistant",
            8.0,
        )
    ]
    entries[-1]["message"]["content"] = [
        {"type": "tool_result", "tool_use_id": "x", "content": "spawned sub_old_1"},
        {"type": "text", "text": "sub_old_1 finished; see session-src for details"},
    ]
    tp.save_transcript(
        "session-src",
        _transcript("session-src", entries, project_session_id="session-src"),
    )
    remap = {"session-src": "session-src-branch-1", "sub_old_1": "sub_old_1-branch-1-0"}
    assert tp.clone_transcript(
        "session-src",
        "session-src-branch-1",
        id_remap=remap,
        metadata_overrides={"project_session_id": "session-src-branch-1"},
    )
    dst = tp.load_transcript("session-src-branch-1")
    assert dst is not None
    assert len(dst["transcript"]["entries"]) == len(entries)
    assert dst["metadata"]["project_session_id"] == "session-src-branch-1"
    assert dst["metadata"]["forked_from"] == "session-src"
    last = dst["transcript"]["entries"][-1]["message"]["content"]
    # Exact-match ids are rewritten; prose containing an id is not.
    assert last[0]["content"] == "spawned sub_old_1"
    assert "sub_old_1 finished" in last[1]["text"]
    # Source untouched.
    src = tp.load_transcript("session-src")
    assert src is not None and src["session_id"] == "session-src"
    assert "forked_from" not in src["metadata"]


def test_clone_transcript_legacy_ordinal_still_works(sessions_dir):
    tp.save_transcript("src", _transcript("src", ENTRIES))
    assert tp.clone_transcript("src", "dst", truncate_to_message_ordinal=3)
    dst = tp.load_transcript("dst")
    assert dst is not None
    assert [e["id"] for e in dst["transcript"]["entries"]] == ["c0", "u1", "a1", "t1"]


def test_clone_transcript_missing_source(sessions_dir):
    assert tp.clone_transcript("nope", "dst") is False
    assert tp.load_transcript("dst") is None


# ── remap ──────────────────────────────────────────────────────────────


def test_remap_ids_rewrites_values_and_keys_only_on_exact_match():
    remap = {"old": "new", "child": "child2"}
    obj = {
        "old": {"sessionId": "old", "parentSessionId": "child", "note": "old news"},
        "list": ["old", "older", 3, None],
        "subagents": {"child": {"id": "child"}},
    }
    out = tp.remap_ids(obj, remap)
    assert out == {
        "new": {"sessionId": "new", "parentSessionId": "child2", "note": "old news"},
        "list": ["new", "older", 3, None],
        "subagents": {"child2": {"id": "child2"}},
    }
    # Original is not mutated.
    assert "old" in obj and obj["list"][0] == "old"


# ── sidecars ───────────────────────────────────────────────────────────


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_clone_session_sidecars_remaps_and_truncates(sessions_dir):
    old, new = "session-src", "session-src-branch-1"
    remap = {old: new, "sub_a": "sub_a-branch-1-0"}
    tp.save_goal_state(old, {"goalState": {"owner": old}, "judgeRules": {}, "verdictHistory": []})
    tp.save_inbox_state(
        old,
        {
            "sessionId": old,
            "unread": [
                {"id": "m1", "fromSession": "sub_a", "timestamp": 1000, "content": "early"},
                {"id": "m2", "fromSession": "sub_a", "timestamp": 5000, "content": "late"},
            ],
            "delivered": [{"id": "m0", "fromSession": old, "timestamp": 500}],
        },
    )
    tp.save_subagent_state(old, {"sessionId": old, "parentSessionId": "sub_a", "label": "x"})
    _write(
        sessions_dir / f"{old}.events.jsonl",
        json.dumps({"type": "tool_result", "sessionId": old})
        + "\n"
        + json.dumps({"type": "session_spawned", "sessionId": "sub_a", "parentSessionId": old})
        + "\n",
    )
    _write(
        sessions_dir / f"{old}.tasks.jsonl",
        json.dumps({"ts": 1000, "kind": "create", "task": {"id": "task_001", "createdBy": old}})
        + "\n"
        + json.dumps({"ts": 6000, "kind": "update", "task": {"id": "task_001"}})
        + "\n",
    )
    _write(
        sessions_dir / f"{old}.kanban.jsonl",
        json.dumps({"ts": 2000, "kind": "create", "task": {"id": "card_001", "assignee": "sub_a"}})
        + "\n",
    )

    cloned = tp.clone_session_sidecars(old, new, id_remap=remap, cutoff_ms=3000)
    assert set(cloned) == {
        ".goal.json",
        ".inbox.json",
        ".subagent.json",
        ".events.jsonl",
        ".tasks.jsonl",
        ".kanban.jsonl",
    }

    goal = tp.load_goal_state(new)
    assert goal is not None and goal["sessionId"] == new and goal["goalState"]["owner"] == new

    inbox = tp.load_inbox_state(new)
    assert inbox is not None and inbox["sessionId"] == new
    assert [m["id"] for m in inbox["unread"]] == ["m1"]  # m2 is after the cutoff
    assert inbox["unread"][0]["fromSession"] == "sub_a-branch-1-0"
    assert inbox["delivered"][0]["fromSession"] == new

    sub = tp.load_subagent_state(new)
    assert sub == {"sessionId": new, "parentSessionId": "sub_a-branch-1-0", "label": "x"}

    events = [
        json.loads(l)
        for l in (sessions_dir / f"{new}.events.jsonl").read_text().splitlines()
        if l.strip()
    ]
    # No timestamps on events → copied whole, ids rewritten.
    assert [e["sessionId"] for e in events] == [new, "sub_a-branch-1-0"]
    assert events[1]["parentSessionId"] == new

    tasks = [
        json.loads(l)
        for l in (sessions_dir / f"{new}.tasks.jsonl").read_text().splitlines()
        if l.strip()
    ]
    assert len(tasks) == 1 and tasks[0]["ts"] == 1000
    assert tasks[0]["task"]["createdBy"] == new

    kanban = (sessions_dir / f"{new}.kanban.jsonl").read_text()
    assert "sub_a-branch-1-0" in kanban and "sub_a\"" not in kanban

    # Sources untouched.
    assert tp.load_goal_state(old)["goalState"]["owner"] == old
    assert len(tp.load_inbox_state(old)["unread"]) == 2


def test_clone_session_sidecars_with_nothing_on_disk(sessions_dir):
    assert tp.clone_session_sidecars("ghost", "ghost-2", id_remap={"ghost": "ghost-2"}) == []


# ── project dir ────────────────────────────────────────────────────────


def test_clone_project_dir_copies_tree_and_rewrites_ledgers(tmp_path):
    old, new = "session-src", "session-src-branch-1"
    remap = {old: new, "sub_a": "sub_a-branch-1-0"}
    src = tmp_path / "projects" / old
    _write(src / "artifacts" / "report.md", "# hi\n")
    _write(src / "jev-lab" / "notes.txt", "keep me\n")
    _write(
        src / "manifest.jsonl",
        json.dumps({"id": "art_1", "creatorId": "sub_a", "createdAt": 1000})
        + "\n"
        + json.dumps({"id": "art_2", "creatorId": old, "createdAt": 9000})
        + "\n",
    )
    _write(
        src / "action_ledger.jsonl",
        json.dumps({"sessionId": old, "creatorId": old, "createdAt": 1500})
        + "\n"
        + "this line is not json\n"
        + json.dumps({"sessionId": old, "creatorId": old, "createdAt": 9500})
        + "\n",
    )
    _write(src / "working_memory.json", json.dumps({"version": 1, "sessionId": old, "entities": {}}))
    _write(src / "compactions.jsonl", json.dumps({"session_id": old, "turn_id": "t1"}) + "\n")

    dst = tmp_path / "projects" / new
    assert tp.clone_project_dir(src, dst, id_remap=remap, cutoff_ms=5000)

    assert (dst / "artifacts" / "report.md").read_text() == "# hi\n"
    assert (dst / "jev-lab" / "notes.txt").read_text() == "keep me\n"

    manifest = [json.loads(l) for l in (dst / "manifest.jsonl").read_text().splitlines() if l.strip()]
    assert [m["id"] for m in manifest] == ["art_1"]
    assert manifest[0]["creatorId"] == "sub_a-branch-1-0"

    ledger_lines = (dst / "action_ledger.jsonl").read_text().splitlines()
    assert ledger_lines[1] == "this line is not json"
    rows = [json.loads(l) for l in ledger_lines if l.strip() and l.startswith("{")]
    assert len(rows) == 1 and rows[0]["sessionId"] == new and rows[0]["creatorId"] == new

    wm = json.loads((dst / "working_memory.json").read_text())
    assert wm["sessionId"] == new

    comp = json.loads((dst / "compactions.jsonl").read_text().strip())
    assert comp["session_id"] == new

    # Source untouched.
    assert json.loads((src / "working_memory.json").read_text())["sessionId"] == old
    assert len((src / "manifest.jsonl").read_text().splitlines()) == 2


def test_clone_project_dir_refuses_missing_or_existing(tmp_path):
    assert tp.clone_project_dir(tmp_path / "missing", tmp_path / "dst", id_remap={}) is False
    src = tmp_path / "src"
    _write(src / "a.txt", "x")
    dst = tmp_path / "dst"
    dst.mkdir()
    assert tp.clone_project_dir(src, dst, id_remap={}) is False


# ── live-session truncation (edit / rerun / delete / pin) ──────────────


class _FakeTranscript:
    def __init__(self, entries):
        self.entries = entries
        self.branched_from = None

    def branch_from(self, entry_id):
        idx = next(i for i, e in enumerate(self.entries) if e.id == entry_id)
        self.entries = self.entries[: idx + 1]
        self.branched_from = entry_id
        return True


def _fake_session():
    entries = [
        SimpleNamespace(
            id=e["id"],
            message=(SimpleNamespace(**e["message"]) if "message" in e else None),
            timestamp=e["timestamp"],
        )
        for e in ENTRIES
    ]
    return SimpleNamespace(transcript=_FakeTranscript(entries))


def test_message_cutoff_ts_parses_renderer_ms():
    assert _message_cutoff_ts({"messageCreatedAt": 4990}) == 4.99
    assert _message_cutoff_ts({}) is None
    assert _message_cutoff_ts({"messageCreatedAt": 0}) is None
    assert _message_cutoff_ts({"messageCreatedAt": "4990"}) is None
    assert _message_cutoff_ts({"messageCreatedAt": True}) is None


def test_find_message_entry_index_prefers_timestamp_over_ordinal():
    sess = _fake_session()
    entries = sess.transcript.entries
    # Renderer ordinal 2 (its 3rd user/assistant message = the 2nd user
    # message) counted engine-side lands on a tool_result; the timestamp
    # lands on the user entry it actually means.
    assert _find_message_entry_index(entries, 2) == 3
    assert entries[_find_message_entry_index(entries, 2, 4.99)].id == "u2"
    assert _find_message_entry_index(entries, 2, 99.0) is None


def test_truncate_session_by_cutoff_drops_the_whole_later_turn():
    sess = _fake_session()
    ok, target = _truncate_session_at_message_ordinal(sess, 2, cutoff_ts=4.99)
    assert ok and target.id == "u2" and target.message.role == "user"
    assert [e.id for e in sess.transcript.entries] == ["c0", "u1", "a1", "t1", "a2"]
    assert sess.transcript.branched_from == "a2"


def test_truncate_session_by_cutoff_at_first_message_wipes_everything():
    sess = _fake_session()
    ok, target = _truncate_session_at_message_ordinal(sess, 0, cutoff_ts=1.99)
    # Cutoff before the first message entry (u1); the compaction entry at
    # 1.0 has no message so the first message-bearing entry is index 1.
    assert ok and target.id == "u1"
    assert [e.id for e in sess.transcript.entries] == ["c0"]


def test_truncate_session_legacy_ordinal_unchanged():
    sess = _fake_session()
    ok, target = _truncate_session_at_message_ordinal(sess, 2)
    assert ok and target.id == "t1"
    assert [e.id for e in sess.transcript.entries] == ["c0", "u1", "a1"]


def test_truncate_session_cutoff_after_everything_is_a_noop_failure():
    sess = _fake_session()
    ok, target = _truncate_session_at_message_ordinal(sess, 0, cutoff_ts=100.0)
    assert not ok and target is None
    assert len(sess.transcript.entries) == len(ENTRIES)


# ── event mirror stamps + cutoff, compaction snapshots ─────────────────


def test_event_mirror_lines_carry_wallclock_ms(tmp_path, monkeypatch):
    import bridge.freyja_bridge as fb

    monkeypatch.setattr(fb, "_SESSION_EVENT_DIR", tmp_path)
    try:
        fb._append_session_event_jsonl("session-x", {"type": "tool_result", "sessionId": "session-x"})
        fb._append_session_event_jsonl("session-x", {"type": "usage", "sessionId": "session-x", "_t": 42})
    finally:
        fb._close_session_event_file("session-x")
    rows = [json.loads(l) for l in (tmp_path / "session-x.events.jsonl").read_text().splitlines()]
    assert rows[0]["type"] == "tool_result"
    assert isinstance(rows[0]["_t"], int) and rows[0]["_t"] > 1_700_000_000_000
    # An existing stamp is never overwritten.
    assert rows[1]["_t"] == 42


def test_clone_session_sidecars_truncates_stamped_events_keeps_legacy(sessions_dir):
    old, new = "session-src", "session-src-branch-1"
    _write(
        sessions_dir / f"{old}.events.jsonl",
        json.dumps({"type": "a", "sessionId": old})  # legacy: no stamp → kept
        + "\n"
        + json.dumps({"type": "b", "sessionId": old, "_t": 1000})
        + "\n"
        + json.dumps({"type": "c", "sessionId": old, "_t": 5000})
        + "\n",
    )
    tp.clone_session_sidecars(old, new, id_remap={old: new}, cutoff_ms=3000)
    rows = [json.loads(l) for l in (sessions_dir / f"{new}.events.jsonl").read_text().splitlines()]
    assert [r["type"] for r in rows] == ["a", "b"]
    assert all(r["sessionId"] == new for r in rows)
    # Whole fork keeps everything.
    tp.clone_session_sidecars(old, "session-src-branch-2", id_remap={old: "session-src-branch-2"})
    rows = (sessions_dir / "session-src-branch-2.events.jsonl").read_text().splitlines()
    assert len(rows) == 3


def test_clone_compaction_snapshots(sessions_dir):
    old, new = "session-src", "session-src-branch-1"
    root = sessions_dir / "compactions"
    _write(root / f"{old}-1000-before.md", f"# snap\n- session: `{old}`\n")
    _write(root / f"{old}-1000-before.json", json.dumps({"session_id": old, "n": 1}))
    _write(root / f"{old}-9000-after.md", "late\n")
    _write(root / f"{old}-9000-after.json", json.dumps({"session_id": old, "n": 2}))
    _write(root / f"{old}x-1000-before.md", "different session with a longer id\n")

    cloned = tp.clone_session_sidecars(old, new, id_remap={old: new}, cutoff_ms=5000)
    assert "compactions/" in cloned
    names = sorted(p.name for p in root.iterdir() if p.name.startswith(new))
    assert names == [f"{new}-1000-before.json", f"{new}-1000-before.md"]
    assert json.loads((root / f"{new}-1000-before.json").read_text())["session_id"] == new
    assert f"`{new}`" in (root / f"{new}-1000-before.md").read_text()
    # Whole fork copies every snapshot.
    assert tp.clone_compaction_snapshots(old, "session-src-branch-2", id_remap={old: "session-src-branch-2"}) == 4
    # Sources untouched.
    assert (root / f"{old}-9000-after.md").read_text() == "late\n"
