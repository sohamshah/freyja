"""Typed questions and answers for the decision layer.

A decision request is a `state` (text or JSON) plus a mapping of question
ids to typed questions. The provider returns one probability-shaped answer
per question. Nothing here generates text: the model can only pick among
options the caller defined.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Union


@dataclass(frozen=True)
class Noul:
    """Yes/no question. The answer is P(yes)."""

    instructions: str
    criteria: dict[str, str] | None = None


@dataclass(frozen=True)
class Choice:
    """Categorical question over a closed option set (max 255 options).

    `options` maps option name to a short description (or None). Callers
    should always include a null option such as `other` or `unclear` so the
    model is never forced to pick a fitting option when none applies.
    """

    instructions: str
    options: dict[str, Any]


@dataclass(frozen=True)
class Score:
    """Ordinal question over 2..10 ordered levels. The answer is the
    probability-weighted level index."""

    instructions: str
    levels: tuple[str, ...]


Question = Union[Noul, Choice, Score]


def question_payload(q: Question) -> dict[str, Any]:
    if isinstance(q, Noul):
        body: dict[str, Any] = {"type": "noul", "instructions": q.instructions}
        if q.criteria:
            body["criteria"] = dict(q.criteria)
        return body
    if isinstance(q, Choice):
        if not 2 <= len(q.options) <= 255:
            raise ValueError(f"Choice needs 2..255 options, got {len(q.options)}")
        return {"type": "choice", "instructions": q.instructions, "criteria": dict(q.options)}
    if isinstance(q, Score):
        if not 2 <= len(q.levels) <= 10:
            raise ValueError(f"Score needs 2..10 levels, got {len(q.levels)}")
        return {"type": "score", "instructions": q.instructions, "criteria": list(q.levels)}
    raise TypeError(f"unsupported question type: {type(q).__name__}")


@dataclass(frozen=True)
class NoulAnswer:
    p: float

    @property
    def confidence(self) -> float:
        return abs(self.p - 0.5) * 2


@dataclass(frozen=True)
class ChoiceAnswer:
    choice: str
    probabilities: dict[str, float]
    confidence: float

    def p(self, option: str) -> float:
        return float(self.probabilities.get(option, 0.0))


@dataclass(frozen=True)
class ScoreAnswer:
    score: float
    probabilities: list[float]
    legend: list[str]
    confidence: float


Answer = Union[NoulAnswer, ChoiceAnswer, ScoreAnswer]


@dataclass
class Answers:
    answers: dict[str, Answer]
    model: str
    provider: str
    latency_ms: int
    input_tokens: int = 0
    output_tokens: int = 0
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    def __getitem__(self, key: str) -> Answer:
        return self.answers[key]

    def noul(self, key: str) -> float:
        a = self.answers[key]
        if not isinstance(a, NoulAnswer):
            raise TypeError(f"{key} is not a Noul answer")
        return a.p

    def choice(self, key: str) -> ChoiceAnswer:
        a = self.answers[key]
        if not isinstance(a, ChoiceAnswer):
            raise TypeError(f"{key} is not a Choice answer")
        return a

    def score(self, key: str) -> ScoreAnswer:
        a = self.answers[key]
        if not isinstance(a, ScoreAnswer):
            raise TypeError(f"{key} is not a Score answer")
        return a

    def compact(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for k, a in self.answers.items():
            if isinstance(a, NoulAnswer):
                out[k] = round(a.p, 4)
            elif isinstance(a, ChoiceAnswer):
                out[k] = {"choice": a.choice, "confidence": round(a.confidence, 4)}
            else:
                out[k] = {"score": round(a.score, 4), "confidence": round(a.confidence, 4)}
        return out


def parse_answers(
    payload: dict[str, Any], *, provider: str, latency_ms: int
) -> Answers:
    parsed: dict[str, Answer] = {}
    for key, body in (payload.get("answers") or {}).items():
        kind = body.get("type")
        if kind == "noul":
            parsed[key] = NoulAnswer(p=float(body["noul"]))
        elif kind == "choice":
            probs = {str(k): float(v) for k, v in (body.get("probabilities") or {}).items()}
            parsed[key] = ChoiceAnswer(
                choice=str(body.get("choice")),
                probabilities=probs,
                confidence=float(body.get("confidence", _peakedness(list(probs.values())))),
            )
        elif kind == "score":
            probs_raw = body.get("probabilities") or []
            probs = (
                [float(v) for v in probs_raw.values()]
                if isinstance(probs_raw, dict)
                else [float(v) for v in probs_raw]
            )
            parsed[key] = ScoreAnswer(
                score=float(body.get("score", 0.0)),
                probabilities=probs,
                legend=[str(x) for x in body.get("legend") or []],
                confidence=float(body.get("confidence", _peakedness(probs))),
            )
        else:
            raise ValueError(f"unknown answer type {kind!r} for {key!r}")
    usage = payload.get("usage") or {}
    return Answers(
        answers=parsed,
        model=str(payload.get("model", "")),
        provider=provider,
        latency_ms=latency_ms,
        input_tokens=int(usage.get("input_tokens", 0) or 0),
        output_tokens=int(usage.get("output_tokens", 0) or 0),
        raw=payload,
    )


def _peakedness(probs: list[float]) -> float:
    n = len(probs)
    if n < 2:
        return 1.0
    return max(0.0, (n * max(probs) - 1.0) / (n - 1.0))
