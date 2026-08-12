"""Receipt persistence + undo ledger tests (bridge/voice/receipts.py)."""

from __future__ import annotations

import json

from bridge.voice.receipts import Receipt, ReceiptStore, UndoLedger

# ── Receipt shape ─────────────────────────────────────────────────────────


def test_receipt_to_dict_camelcase():
    r = Receipt.new(
        heard="play vienna",
        lane="brain",
        verb="spotify.play",
        args={"query": "vienna"},
        ok=True,
        summary="▶ Vienna — Billy Joel",
        undoable=False,
        voice_session_id="voice-abc123",
    )
    d = r.to_dict()
    assert d["id"].startswith("rcpt-")
    assert isinstance(d["ts"], int) and d["ts"] > 0
    assert d["heard"] == "play vienna"
    assert d["lane"] == "brain"
    assert d["verb"] == "spotify.play"
    assert d["args"] == {"query": "vienna"}
    assert d["ok"] is True
    assert d["summary"] == "▶ Vienna — Billy Joel"
    assert d["undoable"] is False
    assert d["voiceSessionId"] == "voice-abc123"
    # optional keys are omitted, not null (TS `?:` fields)
    assert "undone" not in d
    assert "voice_session_id" not in d


def test_receipt_optional_keys_omitted():
    r = Receipt.new(
        heard="",
        lane="floor",
        verb="spotify.pause",
        args={},
        ok=True,
        summary="paused",
        undoable=False,
    )
    d = r.to_dict()
    assert "voiceSessionId" not in d
    assert "undone" not in d
    r.undone = True
    assert r.to_dict()["undone"] is True


def test_receipt_roundtrip():
    r = Receipt.new(
        heard="mute",
        lane="floor",
        verb="system.volume",
        args={"mute": True},
        ok=True,
        summary="muted",
        undoable=True,
        voice_session_id="voice-xyz",
    )
    r2 = Receipt.from_dict(r.to_dict())
    assert r2 == r


# ── ReceiptStore ──────────────────────────────────────────────────────────


def _mk(store: ReceiptStore, n: int) -> list[Receipt]:
    out = []
    for i in range(n):
        r = Receipt.new(
            heard=f"utterance {i}",
            lane="brain",
            verb=f"verb.{i}",
            args={},
            ok=True,
            summary=f"did {i}",
            undoable=False,
        )
        r.ts = 1000 + i  # deterministic ordering independent of clock
        store.append(r)
        out.append(r)
    return out


def test_store_append_and_recent_newest_first(tmp_path):
    store = ReceiptStore(tmp_path / "receipts.jsonl")
    made = _mk(store, 5)
    recent = store.recent(limit=3)
    assert [r.id for r in recent] == [made[4].id, made[3].id, made[2].id]


def test_store_recent_on_missing_file(tmp_path):
    store = ReceiptStore(tmp_path / "nope" / "receipts.jsonl")
    assert store.recent(limit=10) == []


def test_store_skips_corrupt_lines(tmp_path):
    path = tmp_path / "receipts.jsonl"
    store = ReceiptStore(path)
    made = _mk(store, 2)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write("{torn json...\n")
        fh.write("[1,2,3]\n")  # valid JSON, wrong shape
    made2 = _mk(store, 1)
    recent = store.recent(limit=10)
    assert [r.id for r in recent] == [made2[0].id, made[1].id, made[0].id]


def test_store_mark_undone(tmp_path):
    path = tmp_path / "receipts.jsonl"
    store = ReceiptStore(path)
    made = _mk(store, 3)
    updated = store.mark_undone(made[1].id)
    assert updated is not None and updated.undone is True
    # persisted — a fresh store sees it
    fresh = ReceiptStore(path).recent(limit=10)
    by_id = {r.id: r for r in fresh}
    assert by_id[made[1].id].undone is True
    assert by_id[made[0].id].undone is False
    assert by_id[made[2].id].undone is False
    # file is still valid JSONL after the atomic rewrite
    for line in path.read_text().splitlines():
        json.loads(line)


def test_store_mark_undone_missing_id(tmp_path):
    store = ReceiptStore(tmp_path / "receipts.jsonl")
    _mk(store, 1)
    assert store.mark_undone("rcpt-doesnotexist") is None


# ── UndoLedger ────────────────────────────────────────────────────────────


async def _noop():
    return None


def test_undo_ledger_take_is_single_use():
    ledger = UndoLedger()
    ledger.remember("r1", _noop)
    assert ledger.has("r1")
    assert ledger.take("r1") is _noop
    assert ledger.take("r1") is None
    assert not ledger.has("r1")


def test_undo_ledger_capacity_evicts_oldest():
    ledger = UndoLedger(capacity=20)
    for i in range(25):
        ledger.remember(f"r{i}", _noop)
    assert len(ledger) == 20
    # the first five fell off the front
    for i in range(5):
        assert not ledger.has(f"r{i}")
    for i in range(5, 25):
        assert ledger.has(f"r{i}")


def test_undo_ledger_rememer_same_id_moves_to_end():
    ledger = UndoLedger(capacity=2)
    ledger.remember("a", _noop)
    ledger.remember("b", _noop)
    ledger.remember("a", _noop)  # refresh a — b is now oldest
    ledger.remember("c", _noop)
    assert ledger.has("a") and ledger.has("c")
    assert not ledger.has("b")
