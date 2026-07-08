"""Voice receipts — the audit trail for every action the voice agent takes.

Every verb execution (including refusals awaiting confirmation and undo
runs) becomes one ``Receipt``, appended to ``~/.freyja/voice/receipts.jsonl``
and mirrored to the renderer as a ``voice_receipt`` event. The persisted
record is the durable half; undo *closures* are process-lifetime only and
live in the in-memory ``UndoLedger`` (a closure over live adapter state —
e.g. "restore volume to 40" — can't survive a restart, so ``undoable`` in
the persisted receipt reflects at-time-of-action, not current reality).
"""

from __future__ import annotations

import json
import os
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional


@dataclass
class Receipt:
    """One voice action. ``to_dict()`` emits the camelCase shape the TS
    ``Receipt`` type pins (contract §2) — python stays snake_case inside."""

    id: str
    ts: int  # epoch ms
    heard: str  # best-known utterance text ("" if typed)
    lane: str  # "floor" | "brain" | "mission" | "undo"
    verb: str  # e.g. "spotify.play"
    args: dict[str, Any] = field(default_factory=dict)
    ok: bool = True
    summary: str = ""
    undoable: bool = False
    undone: bool = False
    voice_session_id: Optional[str] = None

    @classmethod
    def new(
        cls,
        *,
        heard: str,
        lane: str,
        verb: str,
        args: dict[str, Any],
        ok: bool,
        summary: str,
        undoable: bool,
        voice_session_id: Optional[str] = None,
    ) -> "Receipt":
        return cls(
            id=f"rcpt-{uuid.uuid4().hex[:12]}",
            ts=int(time.time() * 1000),
            heard=heard,
            lane=lane,
            verb=verb,
            args=dict(args or {}),
            ok=ok,
            summary=summary,
            undoable=undoable,
            voice_session_id=voice_session_id,
        )

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "id": self.id,
            "ts": self.ts,
            "heard": self.heard,
            "lane": self.lane,
            "verb": self.verb,
            "args": self.args,
            "ok": self.ok,
            "summary": self.summary,
            "undoable": self.undoable,
        }
        # Optional keys are omitted (not null) to match the TS `?:` fields.
        if self.undone:
            d["undone"] = True
        if self.voice_session_id:
            d["voiceSessionId"] = self.voice_session_id
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Receipt":
        return cls(
            id=str(d.get("id", "")),
            ts=int(d.get("ts", 0)),
            heard=str(d.get("heard", "")),
            lane=str(d.get("lane", "brain")),
            verb=str(d.get("verb", "")),
            args=dict(d.get("args") or {}),
            ok=bool(d.get("ok", False)),
            summary=str(d.get("summary", "")),
            undoable=bool(d.get("undoable", False)),
            undone=bool(d.get("undone", False)),
            voice_session_id=d.get("voiceSessionId") or None,
        )


def _default_receipts_path() -> Path:
    return Path.home() / ".freyja" / "voice" / "receipts.jsonl"


class ReceiptStore:
    """Append-JSONL receipt persistence. One line per receipt; corrupt
    lines are skipped on read so a torn write can never brick the store."""

    def __init__(self, path: Optional[Path] = None) -> None:
        self._path = Path(path) if path is not None else _default_receipts_path()

    @property
    def path(self) -> Path:
        return self._path

    def append(self, receipt: Receipt) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(receipt.to_dict(), ensure_ascii=False, default=str)
        with open(self._path, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")

    def _read_all(self) -> list[Receipt]:
        try:
            raw = self._path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return []
        receipts: list[Receipt] = []
        for line in raw.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
                if isinstance(d, dict):
                    receipts.append(Receipt.from_dict(d))
            except Exception:  # noqa: BLE001 — torn/corrupt line, skip it
                continue
        return receipts

    def recent(self, limit: int = 50) -> list[Receipt]:
        """Newest-first slice — file order is append order, so reverse."""
        receipts = self._read_all()
        receipts.reverse()
        return receipts[: max(1, int(limit))]

    def mark_undone(self, receipt_id: str) -> Optional[Receipt]:
        """Flip ``undone`` on one receipt, atomically rewriting the file
        (read-modify-rename; the file is small — recent-N UI scale)."""
        receipts = self._read_all()
        updated: Optional[Receipt] = None
        for r in receipts:
            if r.id == receipt_id:
                r.undone = True
                updated = r
                break
        if updated is None:
            return None
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_name(self._path.name + ".tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            for r in receipts:
                fh.write(json.dumps(r.to_dict(), ensure_ascii=False, default=str) + "\n")
        os.replace(tmp, self._path)
        return updated


UndoClosure = Callable[[], Awaitable[Any]]


class UndoLedger:
    """Last-N undo closures, keyed by receipt id. Process-lifetime only —
    closures capture live adapter state and cannot be persisted."""

    def __init__(self, capacity: int = 20) -> None:
        self._capacity = max(1, capacity)
        self._closures: "OrderedDict[str, UndoClosure]" = OrderedDict()

    def remember(self, receipt_id: str, closure: UndoClosure) -> None:
        self._closures[receipt_id] = closure
        self._closures.move_to_end(receipt_id)
        while len(self._closures) > self._capacity:
            self._closures.popitem(last=False)

    def take(self, receipt_id: str) -> Optional[UndoClosure]:
        """Pop the closure — single-use by design (an undo that ran, or
        failed and was re-remembered by the caller, must not double-fire)."""
        return self._closures.pop(receipt_id, None)

    def has(self, receipt_id: str) -> bool:
        return receipt_id in self._closures

    def __len__(self) -> int:
        return len(self._closures)
