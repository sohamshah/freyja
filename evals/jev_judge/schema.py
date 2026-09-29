"""Case schema shared by the suite builders, the runner, and the analysis.

A case is one judgment problem: a `state` the judge reads, one or more typed
`questions` about it, and ground-truth `labels` that did not come from an LLM
answering the same questions.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterator

ROOT = Path(__file__).resolve().parent
CASES_DIR = ROOT / "data" / "cases"
RAW_DIR = ROOT / "data" / "raw"
CACHE_DIR = ROOT / "data" / "cache"
RESULTS_DIR = ROOT / "results"

CATEGORIES = ("literal", "holistic", "compute", "knowledge", "safety", "classification")
QUESTION_TYPES = ("noul", "choice", "score")


@dataclass
class Question:
    name: str
    type: str  # noul | choice | score
    instructions: str
    # choice: option name -> short description (or None). Must include a null option.
    options: dict[str, str | None] | None = None
    # score: ordered level names, low to high, 2..10 of them
    levels: list[str] | None = None

    def validate(self) -> None:
        if self.type not in QUESTION_TYPES:
            raise ValueError(f"{self.name}: bad type {self.type}")
        if self.type == "choice":
            if not self.options or not 2 <= len(self.options) <= 255:
                raise ValueError(f"{self.name}: choice needs 2..255 options")
        if self.type == "score":
            if not self.levels or not 2 <= len(self.levels) <= 10:
                raise ValueError(f"{self.name}: score needs 2..10 levels")


@dataclass
class Case:
    id: str
    suite: str
    category: str
    state: Any
    questions: list[Question]
    labels: dict[str, Any]
    label_source: str
    meta: dict[str, Any] = field(default_factory=dict)

    def validate(self) -> None:
        if self.category not in CATEGORIES:
            raise ValueError(f"{self.id}: bad category {self.category}")
        names = set()
        for q in self.questions:
            q.validate()
            names.add(q.name)
        for name, value in self.labels.items():
            if name not in names:
                raise ValueError(f"{self.id}: label for unknown question {name}")
            q = next(q for q in self.questions if q.name == name)
            if q.type == "noul" and not isinstance(value, bool):
                raise ValueError(f"{self.id}: noul label must be bool")
            if q.type == "choice" and value not in q.options:
                raise ValueError(f"{self.id}: choice label {value!r} not an option")
            if q.type == "score" and value not in q.levels:
                raise ValueError(f"{self.id}: score label {value!r} not a level")

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)

    @staticmethod
    def from_dict(d: dict[str, Any]) -> "Case":
        qs = [Question(**q) for q in d["questions"]]
        return Case(
            id=d["id"], suite=d["suite"], category=d["category"], state=d["state"],
            questions=qs, labels=d["labels"], label_source=d["label_source"],
            meta=d.get("meta", {}),
        )


def write_cases(cases: list[Case], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as fh:
        for c in cases:
            c.validate()
            fh.write(c.to_json() + "\n")


def read_cases(path: Path) -> Iterator[Case]:
    with path.open() as fh:
        for line in fh:
            line = line.strip()
            if line:
                yield Case.from_dict(json.loads(line))


def state_text(state: Any) -> str:
    """The exact string a judge sees. Strings pass through; anything else is JSON."""
    if isinstance(state, str):
        return state
    return json.dumps(state, ensure_ascii=False, indent=1)


def approx_tokens(text: str) -> int:
    return max(1, len(text) // 4)
