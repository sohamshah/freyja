"""Append-only decision log.

Every call is recorded with its spec version, provider, a hash of the state,
the compact answers, the gate verdict, and latency. The `outcome` slot is
filled later (operator approve/deny, judge verdict, confirmed injection) so
reliability diagrams can be drawn per spec version.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any

ENV_LOG_PATH = "DECISIONS_LOG_PATH"


def default_log_path() -> Path:
    override = os.environ.get(ENV_LOG_PATH)
    if override:
        return Path(override).expanduser()
    return Path.home() / ".freyja" / "decisions" / "log.jsonl"


def state_digest(state: Any) -> str:
    blob = json.dumps(state, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()[:16]


class DecisionLog:
    def __init__(self, path: Path | str | None = None) -> None:
        self.path = Path(path) if path else default_log_path()

    def append(self, record: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        record = {"ts": time.time(), **record}
        with open(self.path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False, default=_jsonable) + "\n")

    def record_outcome(self, decision_id: str, outcome: str, note: str = "") -> None:
        self.append({"kind": "outcome", "decision_id": decision_id, "outcome": outcome, "note": note})

    def read(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        out: list[dict[str, Any]] = []
        with open(self.path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    out.append(json.loads(line))
        return out


def _jsonable(obj: Any) -> Any:
    if is_dataclass(obj) and not isinstance(obj, type):
        return asdict(obj)
    if isinstance(obj, (set, tuple)):
        return list(obj)
    return str(obj)
