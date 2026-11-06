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
# everything before it. As in a real transcript, the compaction entry
# sits in front of the entries it kept but is stamped when the
# compaction ran (6.8, mid-turn): after every kept entry, before a4.
ENTRIES = [
    _entry("c0", None, 6.8),
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


@pytest.mark.parametrize(
    "anchor", [e["id"] for e in ENTRIES if "message" in e]
)
def test_truncate_entries_before_keeps_compaction_summary_at_every_anchor(anchor):
    # The renderer stamps a message just before the engine appends its
    # entry. Branching at any message keeps the summary and every entry
    # before the anchor, though the summary is stamped after most of them.
    idx = next(i for i, e in enumerate(ENTRIES) if e["id"] == anchor)
    kept, head = tp.truncate_entries_before(ENTRIES, ENTRIES[idx]["timestamp"] - 0.01)
    assert [e["id"] for e in kept] == [e["id"] for e in ENTRIES[:idx]]
    assert head == ENTRIES[idx - 1]["id"]


def test_truncate_entries_before_after_a_real_compaction():
    from engine.session import TranscriptManager
    from engine.types import Message

    tm = TranscriptManager()
    for i, role in enumerate(["user", "assistant", "user", "assistant"]):
        tm.append_message(Message(role=role, content=f"m{i}"))
    for i, e in enumerate(tm.entries):
        e.timestamp = 100.0 + i
    tm.append_compaction("summary", first_kept_id=tm.entries[2].id, tokens_before=1_000)
    tm.append_message(Message(role="user", content="m4"))
    entries = [e.to_dict() for e in tm.entries]
    # The engine puts the summary in front of the entries it kept and
    # stamps it with the time of compaction, after them.
    assert entries[0]["is_compaction"]
    assert entries[0]["timestamp"] > entries[1]["timestamp"]

    def texts(kept):
        return [e.get("compaction_summary") or e["message"]["content"] for e in kept]

    kept, _ = tp.truncate_entries_before(entries, 101.99)
    assert texts(kept) == ["summary"]
    kept, _ = tp.truncate_entries_before(entries, 102.99)
    assert texts(kept) == ["summary", "m2"]
    kept, head = tp.truncate_entries_before(entries, entries[-1]["timestamp"] - 0.001)
    assert texts(kept) == ["summary", "m2", "m3"]
    assert head == entries[-2]["id"]


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


@pytest.mark.parametrize(
    "anchor", [e["id"] for e in ENTRIES if "message" in e]
)
def test_find_message_entry_index_after_compaction_at_every_anchor(anchor):
    # Edit, rerun, delete and pin resolve their anchor here. The summary
    # carries no message, so its late stamp never wins the match.
    entries = _fake_session().transcript.entries
    idx = next(i for i, e in enumerate(ENTRIES) if e["id"] == anchor)
    assert _find_message_entry_index(entries, 0, ENTRIES[idx]["timestamp"] - 0.01) == idx


def test_truncate_session_by_cutoff_drops_the_whole_later_turn():
    sess = _fake_session()
    ok, target = _truncate_session_at_message_ordinal(sess, 2, cutoff_ts=4.99)
    assert ok and target.id == "u2" and target.message.role == "user"
    assert [e.id for e in sess.transcript.entries] == ["c0", "u1", "a1", "t1", "a2"]
    assert sess.transcript.branched_from == "a2"


def test_truncate_session_by_cutoff_at_first_message_wipes_everything():
    sess = _fake_session()
    ok, target = _truncate_session_at_message_ordinal(sess, 0, cutoff_ts=1.99)
    # Cutoff before the first message entry (u1); the compaction entry
    # has no message so the first message-bearing entry is index 1.
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


# ── anchors on messages compaction summarized away ────────────────────
#
# The renderer keeps every message; the engine swapped the oldest ones
# for one summary entry. An anchor on a summarized message would resolve
# to the first kept message, which is a different message.

# Before compaction: u1 a1 u2 a2. The compaction kept u2 a2, and u3
# came after it.
BEFORE = [
    _entry("u1", "user", 100.0),
    _entry("a1", "assistant", 101.0),
    _entry("u2", "user", 102.0),
    _entry("a2", "assistant", 103.0),
]
SUMMARY = _entry("c1", None, 150.0, summarized_through=101.0)
AFTER = [SUMMARY, BEFORE[2], BEFORE[3], _entry("u3", "user", 160.0)]
# Renderer stamps: just before the engine appended each entry.
ON_U1, ON_A1, ON_U2 = 99.99, 100.99, 101.99


def _old_style(entries):
    """The same transcript as written before summaries carried
    ``summarized_through``."""
    out = json.loads(json.dumps(entries))
    for e in out:
        e.pop("summarized_through", None)
    return out


def _write_before_snapshot(sessions_dir, session_id, stamp_ms, entries):
    _write(
        sessions_dir / "compactions" / f"{session_id}-{stamp_ms}-before.json",
        json.dumps(_transcript(session_id, entries)),
    )


def test_compaction_entry_records_the_newest_message_it_summarized():
    from engine.session import TranscriptEntry, TranscriptManager
    from engine.types import Message

    tm = TranscriptManager()
    for i, role in enumerate(["user", "assistant", "user", "assistant"]):
        tm.append_message(Message(role=role, content=f"m{i}"))
    for i, e in enumerate(tm.entries):
        e.timestamp = 100.0 + i
    tm.append_compaction("s1", first_kept_id=tm.entries[2].id, tokens_before=1_000)
    assert tm.entries[0].summarized_through == 101.0
    assert TranscriptEntry.from_dict(tm.entries[0].to_dict()).summarized_through == 101.0
    # Folding only the old summary in covers the same messages.
    tm.append_compaction("s2", first_kept_id=tm.entries[1].id, tokens_before=1_000)
    assert tm.entries[0].summarized_through == 101.0
    # Folding more in covers up to the newest of them.
    tm.append_compaction("s3", first_kept_id=tm.entries[2].id, tokens_before=1_000)
    assert tm.entries[0].summarized_through == 102.0


@pytest.mark.parametrize("old_style", [False, True])
def test_anchor_summarized_tells_summarized_from_kept(sessions_dir, old_style):
    entries = _old_style(AFTER) if old_style else AFTER
    if old_style:
        # The summary predates the field: the snapshot taken when the
        # compaction started tells what it folded in.
        _write_before_snapshot(sessions_dir, "s", 140_000, BEFORE)
    assert tp.anchor_summarized("s", entries, ON_U1) is True
    assert tp.anchor_summarized("s", entries, ON_A1) is True
    assert tp.anchor_summarized("s", entries, ON_U2) is False
    assert tp.anchor_summarized("s", entries, 102.99) is False
    assert tp.anchor_summarized("s", entries, 159.99) is False
    assert tp.anchor_summarized("s", ENTRIES[1:], 1.99) is False


def test_anchor_summarized_is_unknown_for_an_old_summary_without_a_snapshot(sessions_dir):
    entries = _old_style(AFTER)
    # An anchor on the first kept message, or on one the summary folded
    # in: both resolve to u2 and nothing tells them apart.
    assert tp.anchor_summarized("s", entries, ON_U1) is None
    assert tp.anchor_summarized("s", entries, ON_U2) is None
    # Past the first kept message it is plain.
    assert tp.anchor_summarized("s", entries, 102.99) is False
    # A snapshot from an earlier attempt that never saw u2 cannot tell.
    _write_before_snapshot(sessions_dir, "s", 100_500, BEFORE[:1])
    assert tp.anchor_summarized("s", entries, ON_U2) is None


def _fingerprint(role: str, text: str, next_user_s: float | None = None) -> dict:
    """What the renderer sends about the message it anchors on (store.ts,
    ``messageFingerprint``)."""
    fp: dict = {"role": role, "textHead": text, "textTail": text}
    if next_user_s is not None:
        fp["nextUserCreatedAt"] = next_user_s * 1000
    return fp


# u2 as the renderer shows it; u3 is the next user message.
ON_U2_FP = _fingerprint("user", "user@102.0", 159.99)


def test_old_summary_without_a_snapshot_still_acts_on_the_first_message_it_kept(sessions_dir):
    entries = _old_style(AFTER)
    # u2 holds the anchor's text and lands before the next user message.
    assert tp.anchor_summarized("s", entries, ON_U2, ON_U2_FP) is False
    # u1 and a1 went into the summary: u2 is not theirs.
    assert tp.anchor_summarized("s", entries, ON_U1, _fingerprint("user", "user@100.0", ON_U2)) is None
    assert tp.anchor_summarized("s", entries, ON_A1, _fingerprint("assistant", "assistant@101.0", ON_U2)) is None
    # A summarized message that said what u2 says comes before u2.
    assert tp.anchor_summarized("s", entries, ON_U1, _fingerprint("user", "user@102.0", ON_U2)) is None


def _calls(eid: str, ts: float, call_id: str) -> dict:
    return {
        "id": eid, "is_compaction": False, "timestamp": ts,
        "message": {"role": "assistant", "content": "", "tool_calls": [{"id": call_id, "name": "bash", "arguments": {}}]},
    }


# One reply on screen, stamped 100.4: a memo opened the turn, then two
# tool steps and the answer.
TURN = [
    _entry("u1", "user", 100.0),
    _entry("m1", "user", 100.5),
    _calls("a1", 101.0, "t1"),
    _entry("r1", "tool_result", 101.5),
    _calls("a2", 102.0, "t2"),
    _entry("r2", "tool_result", 102.5),
    _entry("a3", "assistant", 103.0),
]
ON_REPLY, REPLY_FP = 100.4, {"role": "assistant", "toolCallId": "t1"}


def test_fingerprint_of_a_reply_names_the_step_it_starts_with(sessions_dir):
    summary = _entry("c1", None, 150.0)
    # Kept from the memo: the whole reply is there.
    assert tp.anchor_summarized("s", [summary] + TURN[1:], ON_REPLY, REPLY_FP) is False
    # Kept from its second step: its first went into the summary.
    assert tp.anchor_summarized("s", [summary] + TURN[4:], ON_REPLY, REPLY_FP) is None


def test_fork_at_the_first_message_an_old_summary_kept(sessions_dir):
    tp.save_transcript("old", _transcript("old", _old_style(AFTER)))
    assert tp.clone_transcript("old", "f1", cutoff_ts=ON_U2, fingerprint=ON_U2_FP)
    assert [e["id"] for e in tp.load_transcript("f1")["transcript"]["entries"]] == ["c1"]


def test_old_summary_that_only_folded_an_older_one_reads_the_older_snapshot(sessions_dir):
    # c2 replaced c1 and nothing else, so c2's own snapshot holds no
    # message it summarized: c1's snapshot does.
    c1 = _old_style([SUMMARY])[0]
    c2 = _entry("c2", None, 170.0)
    _write_before_snapshot(sessions_dir, "s", 140_000, BEFORE)
    _write_before_snapshot(sessions_dir, "s", 165_000, [c1] + AFTER[1:])
    entries = [c2] + AFTER[1:]
    assert tp.anchor_summarized("s", entries, ON_A1) is True
    assert tp.anchor_summarized("s", entries, ON_U2) is False


def test_fork_at_a_summarized_message_builds_from_the_before_snapshot(sessions_dir):
    tp.save_transcript("src", _transcript("src", AFTER))
    _write_before_snapshot(sessions_dir, "src", 140_000, BEFORE)

    assert tp.clone_transcript("src", "f1", cutoff_ts=ON_A1)
    dst = tp.load_transcript("f1")
    assert [e["id"] for e in dst["transcript"]["entries"]] == ["u1"]
    assert dst["transcript"]["head_id"] == "u1"
    assert dst["metadata"]["forked_from"] == "src"

    assert tp.clone_transcript("src", "f2", cutoff_ts=ON_U1)
    assert tp.load_transcript("f2")["transcript"]["entries"] == []

    # On the first kept message the summary is right as it is.
    assert tp.clone_transcript("src", "f3", cutoff_ts=ON_U2)
    assert [e["id"] for e in tp.load_transcript("f3")["transcript"]["entries"]] == ["c1"]


def test_fork_at_a_summarized_message_without_a_snapshot_is_refused(sessions_dir):
    tp.save_transcript("src", _transcript("src", AFTER))
    with pytest.raises(tp.SummarizedAnchorError):
        tp.clone_transcript("src", "f1", cutoff_ts=ON_A1)
    assert tp.load_transcript("f1") is None
    # An old summary on its first kept message: it cannot tell, so no.
    tp.save_transcript("old", _transcript("old", _old_style(AFTER)))
    with pytest.raises(tp.SummarizedAnchorError):
        tp.clone_transcript("old", "f2", cutoff_ts=ON_U2)
    assert tp.clone_transcript("old", "f3", cutoff_ts=102.99)


def _live(monkeypatch, entries):
    """A bridge session over a real engine transcript. Turns it would
    run are recorded, not run."""
    import bridge.freyja_bridge as fb
    from engine.session import Session, TranscriptManager

    events: list[dict] = []
    monkeypatch.setattr(fb, "emit", events.append)

    async def _no_drain(*_a, **_k):
        return None

    monkeypatch.setattr(fb, "_drain_after_turn", _no_drain)
    session = Session.create(system_prompt="t")
    session.transcript = TranscriptManager.from_dict(
        {"entries": json.loads(json.dumps(entries)), "head_id": entries[-1]["id"]}
    )
    sess = fb._BridgeSession.__new__(fb._BridgeSession)
    sess.id = "s"
    sess.session = session
    sess.pending_task = None
    sess.project_session_id = None
    sess.turns = []

    async def run_turn(content, attachments=None, *, pre_formed_message=None, **_k):
        sess.turns.append(pre_formed_message if pre_formed_message is not None else content)

    sess.run_turn = run_turn
    sess._save_transcript = lambda: None
    state = SimpleNamespace(get=lambda sid: sess if sid == "s" else None)
    return fb, sess, state, events


async def _send(fb, sess, state, cmd):
    await fb._handle_command(state, {"sessionId": "s", "messageOrdinal": 0, **cmd})
    if sess.pending_task is not None:
        await sess.pending_task


@pytest.mark.parametrize(
    "cmd",
    [
        {"type": "rerun_user_message"},
        {"type": "edit_user_message", "content": "again"},
        {"type": "delete_messages_from"},
        {"type": "toggle_entry_pin", "pinned": True},
    ],
)
async def test_live_commands_refuse_a_summarized_anchor(sessions_dir, monkeypatch, cmd):
    fb, sess, state, events = _live(monkeypatch, AFTER)
    await _send(fb, sess, state, {**cmd, "messageCreatedAt": ON_A1 * 1000})
    # Nothing ran, nothing was cut, nothing was pinned.
    assert sess.turns == []
    entries = sess.session.transcript.entries
    assert [e.id for e in entries] == ["c1", "u2", "a2", "u3"]
    assert not any(e.compaction_excluded for e in entries)
    # A refused pin puts the renderer's optimistic toggle back.
    assert [e["pinned"] for e in events if e["type"] == "entry_pin_changed"] == (
        [False] if cmd["type"] == "toggle_entry_pin" else []
    )
    (err,) = [e for e in events if e["type"] == "error"]
    assert "compaction summary" in err["message"]


async def test_live_commands_still_act_on_the_first_kept_message(sessions_dir, monkeypatch):
    fb, sess, state, events = _live(monkeypatch, AFTER)
    await _send(fb, sess, state, {"type": "toggle_entry_pin", "pinned": True, "messageCreatedAt": ON_U2 * 1000})
    assert [e.id for e in sess.session.transcript.entries if e.compaction_excluded] == ["u2"]
    await _send(fb, sess, state, {"type": "rerun_user_message", "messageCreatedAt": ON_U2 * 1000})
    assert sess.turns == ["user@102.0"]
    assert [e.id for e in sess.session.transcript.entries] == ["c1"]
    assert not [e for e in events if e["type"] == "error"]


async def test_live_commands_act_on_the_first_message_an_old_summary_kept(sessions_dir, monkeypatch):
    import bridge.project_paths as pp

    monkeypatch.setattr(pp, "project_output_dir", lambda sid: sessions_dir / "projects" / str(sid))
    fb, sess, state, events = _live(monkeypatch, _old_style(AFTER))
    tp.save_transcript("s", _transcript("s", _old_style(AFTER)))
    anchor = {"messageCreatedAt": ON_U2 * 1000, "messageFingerprint": ON_U2_FP}
    await fb._handle_command(state, {"type": "branch_session", "sessionId": "s", "messageOrdinal": 2, **anchor})
    assert [e["type"] for e in events if e["type"] in ("session_branched", "error")] == ["session_branched"]
    await _send(fb, sess, state, {"type": "toggle_entry_pin", "pinned": True, **anchor})
    assert [e.id for e in sess.session.transcript.entries if e.compaction_excluded] == ["u2"]
    await _send(fb, sess, state, {"type": "rerun_user_message", **anchor})
    assert sess.turns == ["user@102.0"]
    assert not [e for e in events if e["type"] == "error"]


async def test_branch_at_a_summarized_message_without_a_snapshot_says_why(sessions_dir, monkeypatch):
    import bridge.project_paths as pp

    monkeypatch.setattr(pp, "project_output_dir", lambda sid: sessions_dir / "projects" / str(sid))
    fb, sess, state, events = _live(monkeypatch, AFTER)
    tp.save_transcript("s", _transcript("s", AFTER))
    await fb._handle_command(
        state,
        {"type": "branch_session", "sessionId": "s", "messageOrdinal": 1, "messageCreatedAt": ON_A1 * 1000},
    )
    assert not [e for e in events if e["type"] == "session_branched"]
    (err,) = [e for e in events if e["type"] == "error"]
    assert "compaction summary" in err["message"]
    assert not [p for p in sessions_dir.iterdir() if "-branch-" in p.name]


class _SummaryProvider:
    name = "anthropic"
    model_id = "claude-sonnet-5-5"
    context_window = 200_000

    def complete(self, messages, **_kw):
        from engine.providers import APIUsage, ProviderResponse

        return ProviderResponse(
            content="<summary>We read the files.</summary>",
            stop_reason="end_turn",
            usage=APIUsage(input_tokens=20_000, output_tokens=800),
        )


async def test_fork_at_a_message_summarize_context_folded_in_is_cut_from_its_snapshot(monkeypatch, tmp_path):
    # The agent's own compaction snapshots the transcript as the
    # runtime's do, so a fork at a message it summarized has it as written.
    import sys
    import time
    from types import ModuleType

    import bridge.freyja_bridge as fb
    from bridge.tools.summarize_context_tool import SummarizeContextTool
    from engine.compaction import SummaryCompaction
    from engine.session import Session, TranscriptManager
    from engine.types import Message

    monkeypatch.setattr(tp, "SESSIONS_DIR", tmp_path / "sessions")
    telemetry = ModuleType("bridge.compaction_telemetry")
    telemetry.append_telemetry = lambda *_a, **_k: None
    monkeypatch.setitem(sys.modules, "bridge.compaction_telemetry", telemetry)
    monkeypatch.setattr(fb, "emit", lambda _ev: None)
    state = fb._BridgeState(workspace=str(tmp_path), default_model="claude-sonnet-5-5")
    sess = fb._BridgeSession("s", workspace=str(tmp_path), model_id="claude-sonnet-5-5",
                             reasoning_level="auto", coordination_strategy="bus", state=state)
    sess._append_compaction_log = lambda _row: None
    transcript = TranscriptManager()
    for i in range(16):
        transcript.append_message(Message(role=("user", "assistant")[i % 2], content=f"m{i} " + "x" * 2_000))
    t0 = time.time() - 100
    for i, e in enumerate(transcript.entries):
        e.timestamp = t0 + i
    sess.session = Session(id="s", transcript=transcript, system_prompt="")
    tool = SummarizeContextTool(
        get_session=lambda: sess.session,
        get_provider=_SummaryProvider,
        get_compactor=SummaryCompaction,
        on_system_event=sess._emit_summarize_event,
    )

    result = await tool.execute("sc1", {"scope": "all"})

    assert not result.is_error, result.content
    assert transcript.entries[0].summarized_through >= t0 + 1
    sess._save_transcript()
    # A fork before m1, which the summary folded in.
    assert tp.clone_transcript("s", "f1", cutoff_ts=t0 + 0.99)
    assert [e["message"]["content"][:3] for e in tp.load_transcript("f1")["transcript"]["entries"]] == ["m0 "]


# ── compaction records a fork keeps ────────────────────────────────────


def _rows(path: Path) -> list[dict]:
    return [json.loads(l) for l in path.read_text().splitlines() if l.strip()]


def test_fork_drops_compaction_log_rows_written_after_the_branch_point(tmp_path):
    # compactions.jsonl stamps its rows in seconds, the cutoff is in ms.
    src = tmp_path / "projects" / "src"
    _write(
        src / "compactions.jsonl",
        json.dumps({"ts": 1.0, "subtype": "context_pruning"})
        + "\n"
        + json.dumps({"ts": 9.0, "subtype": "context_pruning"})
        + "\n",
    )
    dst = tmp_path / "projects" / "dst"
    assert tp.clone_project_dir(src, dst, id_remap={}, cutoff_ms=5000)
    assert [r["ts"] for r in _rows(dst / "compactions.jsonl")] == [1.0]


async def test_fork_keeps_the_records_of_the_summary_it_keeps(sessions_dir, monkeypatch):
    # The source compacted at 150 s and kept u2 a2. A fork before a2
    # keeps that summary, so it keeps that compaction's snapshots, log
    # rows and events, and none of the compaction that started at 170 s.
    import bridge.project_paths as pp

    projects = sessions_dir / "projects"
    monkeypatch.setattr(pp, "project_output_dir", lambda sid: projects / str(sid))
    fb, sess, state, events = _live(monkeypatch, AFTER)
    tp.save_transcript("s", _transcript("s", AFTER))
    root = sessions_dir / "compactions"
    for stamp, phase in [(90_000, "before"), (140_000, "before"), (150_020, "after"), (170_000, "before")]:
        _write(root / f"s-{stamp}-{phase}.md", "snapshot\n")
        _write(root / f"s-{stamp}-{phase}.json", json.dumps(_transcript("s", BEFORE)))

    def system_event(subtype, t):
        return {"type": "system_event", "sessionId": "s", "subtype": subtype, "_t": t}

    _write(
        sessions_dir / "s.events.jsonl",
        "".join(
            json.dumps(r) + "\n"
            for r in [
                system_event("compaction_start", 140_001),
                {"type": "text_delta", "sessionId": "s", "_t": 140_500},
                system_event("context_pruning", 145_000),
                system_event("compaction_complete", 150_010),
                system_event("compaction_receipt", 150_015),
                system_event("compaction_start", 170_001),
            ]
        ),
    )
    _write(
        projects / "s" / "compactions.jsonl",
        "".join(
            json.dumps(r) + "\n"
            for r in [
                {"ts": 140.0, "subtype": "compaction_start"},
                {"ts": 145.0, "subtype": "context_pruning"},
                {"ts": 150.01, "subtype": "compaction_complete"},
                {"ts": 170.0, "subtype": "compaction_start"},
            ]
        ),
    )

    await fb._handle_command(
        state,
        {"type": "branch_session", "sessionId": "s", "messageOrdinal": 2, "messageCreatedAt": 102_990},
    )
    (branched,) = [e for e in events if e["type"] == "session_branched"]
    new = branched["newSessionId"]
    assert [e["id"] for e in tp.load_transcript(new)["transcript"]["entries"]] == ["c1", "u2"]
    # The renderer keeps compaction events up to here too.
    assert 150_015 < branched["compactionRecordsUntil"] <= 170_000

    assert sorted(p.name for p in root.iterdir() if p.name.startswith(f"{new}-")) == sorted(
        f"{new}-{stamp}-{phase}.{ext}"
        for stamp, phase in [(90_000, "before"), (140_000, "before"), (150_020, "after")]
        for ext in ("md", "json")
    )
    events_rows = _rows(sessions_dir / f"{new}.events.jsonl")
    assert [r.get("subtype") for r in events_rows] == [
        "compaction_start",
        "compaction_complete",
        "compaction_receipt",
    ]
    assert [r["subtype"] for r in _rows(projects / new / "compactions.jsonl")] == [
        "compaction_start",
        "compaction_complete",
    ]


async def test_fork_from_a_snapshot_keeps_no_records_of_the_summary_it_left_out(sessions_dir, monkeypatch):
    import bridge.project_paths as pp

    projects = sessions_dir / "projects"
    monkeypatch.setattr(pp, "project_output_dir", lambda sid: projects / str(sid))
    fb, sess, state, events = _live(monkeypatch, AFTER)
    tp.save_transcript("s", _transcript("s", AFTER))
    _write_before_snapshot(sessions_dir, "s", 140_000, BEFORE)
    await fb._handle_command(
        state,
        {"type": "branch_session", "sessionId": "s", "messageOrdinal": 1, "messageCreatedAt": ON_A1 * 1000},
    )
    (branched,) = [e for e in events if e["type"] == "session_branched"]
    new = branched["newSessionId"]
    assert [e["id"] for e in tp.load_transcript(new)["transcript"]["entries"]] == ["u1"]
    assert branched["compactionRecordsUntil"] == branched["messageCreatedAt"]
    assert not [p for p in (sessions_dir / "compactions").iterdir() if p.name.startswith(f"{new}-")]


# ── follow-ups slid into a running turn ────────────────────────────────
#
# The renderer shows the j-th follow-up of an ``inbox_injected`` event
# at ``at + j`` ms (store.ts, ``inbox_injected``) and the rest of the
# turn at ``at + n``. Branch, edit, rerun and delete anchor on those
# stamps, so each must resolve to the follow-up's own entry.


class _Clock:
    """``time`` for the bridge module with a fixed ``time()``."""

    def __init__(self, now: float):
        self.now = now

    def time(self) -> float:
        return self.now

    def __getattr__(self, name):
        import time as _time

        return getattr(_time, name)


def _injecting_session(monkeypatch):
    import time as _time

    import bridge.freyja_bridge as fb
    from bridge.inbox import SessionInbox
    from engine.session import Session
    from engine.types import ToolCall

    events: list[dict] = []
    monkeypatch.setattr(fb, "emit", events.append)
    session = Session.create(system_prompt="t")
    session.add_user_message("run the checks")
    session.add_assistant_message("", tool_calls=[ToolCall(id="t1", name="bash", arguments={})])
    session.add_tool_result("t1", "ok")
    # The step's tool result landed a fraction of a ms before the
    # drain, in the same ms, as it does when the next step starts.
    base = float(int(_time.time()) - 60)
    for entry, stamp in zip(session.transcript.entries, (base - 10, base - 5, base + 0.0002)):
        entry.timestamp = stamp
    monkeypatch.setattr(fb, "time", _Clock(base + 0.0004))
    sess = fb._BridgeSession.__new__(fb._BridgeSession)
    sess.id = "s"
    sess.inbox = SessionInbox(session_id="s")
    sess.runner = None
    sess._save_inbox = lambda: None
    sess._clear_tool_yield = lambda: None
    return fb, sess, session, events


def _followup(text):
    from bridge.inbox import KIND_FOLLOWUP, InboxMessage, new_message_id

    return InboxMessage(
        id=new_message_id(), from_session="operator", from_label="operator",
        from_role="operator", content=text, kind=KIND_FOLLOWUP,
    )


def _memo(text):
    from bridge.inbox import KIND_MEMO, InboxMessage, new_message_id

    return InboxMessage(
        id=new_message_id(), from_session="sub_a", from_label="researcher",
        from_role="agent", content=text, kind=KIND_MEMO,
    )


def _text(entry) -> str:
    content = entry.message.content
    return content if isinstance(content, str) else ""


@pytest.mark.parametrize("iteration", [3, 1])
async def test_anchors_on_injected_followups_resolve_to_them(monkeypatch, iteration):
    fb, sess, session, events = _injecting_session(monkeypatch)
    sess.inbox.push(_memo("[sub-agent memo] done"))
    sess.inbox.push(_followup("use postgres"))
    sess.inbox.push(_followup("and add tests"))
    await sess._drain_inbox_into_session(session, iteration)
    # The next step's reply, after a provider call.
    session.add_assistant_message("on it")
    session.transcript.entries[-1].timestamp = fb.time.now + 2.0

    (injected,) = [e for e in events if e["type"] == "inbox_injected"]
    at = injected["at"]
    entries = session.transcript.entries
    for j, text in enumerate(["use postgres", "and add tests"]):
        idx = fb._find_message_entry_index(entries, 0, (at + j) / 1000)
        assert _text(entries[idx]).endswith(text)
        kept, _ = tp.truncate_entries_before([e.to_dict() for e in entries], (at + j) / 1000)
        assert [e["id"] for e in kept] == [e.id for e in entries[:idx]]
    # The rest of the turn shows at at + 2: it resolves to the reply.
    idx = fb._find_message_entry_index(entries, 0, (at + 2) / 1000)
    assert _text(entries[idx]) == "on it"
