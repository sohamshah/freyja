"""Versioned decision specs and their gates.

A spec is a YAML file naming one harness decision: the question battery,
how sensitive the state it sends is, which rollout mode it is in, and how
its answers collapse into `act | abstain | escalate`. Calibration history is
keyed by (id, version), so any change to phrasing or thresholds should bump
`version` rather than edit in place.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import yaml

from bridge.decisions.types import Answers, Choice, ChoiceAnswer, Noul, NoulAnswer, Question, Score

Mode = Literal["shadow", "advise", "gate"]
Privacy = Literal["public", "workspace", "personal", "secret"]
Action = Literal["act", "abstain", "escalate"]

SPECS_DIR = Path(__file__).resolve().parent / "specs"
_MODES = ("shadow", "advise", "gate")
_PRIVACY = ("public", "workspace", "personal", "secret")


@dataclass(frozen=True)
class Gate:
    """Collapses answers into a verdict.

    `score` is a linear combination of features: a noul key contributes its
    P(yes); a `choice_key=option` key contributes P(option). `escalate_if_any`
    escalates when any single named feature exceeds its threshold, so one
    decisive predicate (for example a credential request) is never averaged
    away by quiet ones.
    """

    score: dict[str, float] = field(default_factory=dict)
    act_below: float | None = None
    escalate_above: float | None = None
    min_confidence: dict[str, float] = field(default_factory=dict)
    escalate_if_any: dict[str, float] = field(default_factory=dict)


@dataclass(frozen=True)
class Verdict:
    action: Action
    score: float | None
    reasons: tuple[str, ...] = ()


@dataclass(frozen=True)
class DecisionSpec:
    id: str
    version: int
    mode: Mode
    privacy: Privacy
    questions: dict[str, Question]
    frame: str | None = None
    model: str | None = None
    shuffle_options: bool = False
    dynamic_options: tuple[str, ...] = ()
    gate: Gate | None = None
    description: str = ""

    @property
    def key(self) -> str:
        return f"{self.id}@{self.version}"


def _parse_question(key: str, body: dict[str, Any]) -> Question:
    kind = body.get("type")
    instructions = str(body.get("instructions", "")).strip()
    if not instructions:
        raise ValueError(f"question {key!r} has no instructions")
    if kind == "noul":
        criteria = body.get("criteria")
        return Noul(instructions, {str(k): str(v) for k, v in criteria.items()} if criteria else None)
    if kind == "choice":
        options = body.get("options") or {}
        return Choice(instructions, dict(options) if options else {"_dynamic": None, "_unset": None})
    if kind == "score":
        return Score(instructions, tuple(str(x) for x in body.get("levels") or ()))
    raise ValueError(f"question {key!r}: unknown type {kind!r}")


def spec_from_dict(data: dict[str, Any]) -> DecisionSpec:
    mode = str(data.get("mode", "shadow"))
    privacy = str(data.get("privacy", "secret"))
    if mode not in _MODES:
        raise ValueError(f"mode must be one of {_MODES}, got {mode!r}")
    if privacy not in _PRIVACY:
        raise ValueError(f"privacy must be one of {_PRIVACY}, got {privacy!r}")
    questions = {str(k): _parse_question(str(k), v) for k, v in (data.get("questions") or {}).items()}
    if not questions:
        raise ValueError(f"spec {data.get('id')!r} has no questions")
    gate_raw = data.get("gate")
    gate = None
    if gate_raw:
        gate = Gate(
            score={str(k): float(v) for k, v in (gate_raw.get("score") or {}).items()},
            act_below=gate_raw.get("act_below"),
            escalate_above=gate_raw.get("escalate_above"),
            min_confidence={str(k): float(v) for k, v in (gate_raw.get("min_confidence") or {}).items()},
            escalate_if_any={str(k): float(v) for k, v in (gate_raw.get("escalate_if_any") or {}).items()},
        )
        for feature in (*gate.score, *gate.escalate_if_any):
            _validate_feature(feature, questions)
    dynamic = tuple(str(x) for x in data.get("dynamic_options") or ())
    for k in dynamic:
        if not isinstance(questions.get(k), Choice):
            raise ValueError(f"dynamic_options names {k!r}, which is not a choice question")
    return DecisionSpec(
        id=str(data["id"]),
        version=int(data.get("version", 1)),
        mode=mode,  # type: ignore[arg-type]
        privacy=privacy,  # type: ignore[arg-type]
        questions=questions,
        frame=(str(data["frame"]).strip() if data.get("frame") else None),
        model=data.get("model"),
        shuffle_options=bool(data.get("shuffle_options", False)),
        dynamic_options=dynamic,
        gate=gate,
        description=str(data.get("description", "")).strip(),
    )


def _validate_feature(feature: str, questions: dict[str, Question]) -> None:
    key, _, option = feature.partition("=")
    q = questions.get(key)
    if option:
        if not isinstance(q, Choice):
            raise ValueError(f"gate feature {feature!r}: {key!r} is not a choice question")
        if "_dynamic" not in q.options and option not in q.options:
            raise ValueError(f"gate feature {feature!r}: unknown option {option!r}")
    elif not isinstance(q, Noul):
        raise ValueError(f"gate feature {feature!r} is not a noul question")


def feature_value(feature: str, answers: Answers) -> float | None:
    key, _, option = feature.partition("=")
    ans = answers.answers.get(key)
    if option:
        return ans.p(option) if isinstance(ans, ChoiceAnswer) else None
    return ans.p if isinstance(ans, NoulAnswer) else None


def load_spec(path: Path | str) -> DecisionSpec:
    with open(path, encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    return spec_from_dict(data)


def load_specs(directory: Path | str = SPECS_DIR) -> dict[str, DecisionSpec]:
    out: dict[str, DecisionSpec] = {}
    for path in sorted(Path(directory).glob("*.yaml")):
        spec = load_spec(path)
        if spec.id in out:
            raise ValueError(f"duplicate spec id {spec.id!r} in {path}")
        out[spec.id] = spec
    return out


def evaluate_gate(spec: DecisionSpec, answers: Answers) -> Verdict:
    gate = spec.gate
    if gate is None:
        return Verdict("abstain", None, ("no gate defined",))
    reasons: list[str] = []
    score: float | None = None
    if gate.score:
        score = 0.0
        for feature, weight in gate.score.items():
            value = feature_value(feature, answers)
            if value is None:
                return Verdict("abstain", None, (f"missing answer for feature {feature!r}",))
            score += weight * value
        reasons.append(f"score={score:.3f}")
    for feature, threshold in gate.escalate_if_any.items():
        value = feature_value(feature, answers)
        if value is not None and value >= threshold:
            return Verdict("escalate", score, tuple(reasons + [f"{feature}={value:.2f} >= {threshold}"]))
    low_conf = []
    for key, threshold in gate.min_confidence.items():
        ans = answers.answers.get(key)
        conf = getattr(ans, "confidence", None)
        if conf is None or conf < threshold:
            low_conf.append(f"{key} confidence {conf if conf is None else round(conf, 3)} < {threshold}")
    if score is not None and gate.escalate_above is not None and score > gate.escalate_above:
        return Verdict("escalate", score, tuple(reasons + [f"> escalate_above {gate.escalate_above}"]))
    if low_conf:
        return Verdict("abstain", score, tuple(reasons + low_conf))
    if score is not None:
        if gate.act_below is not None and score < gate.act_below:
            return Verdict("act", score, tuple(reasons + [f"< act_below {gate.act_below}"]))
        return Verdict("abstain", score, tuple(reasons + ["between bands"]))
    return Verdict("act", None, tuple(reasons + ["confidence thresholds met"]))


def choice_or_null(answer: ChoiceAnswer, *, null_options: tuple[str, ...] = ("other", "none", "unclear")) -> str | None:
    """Return the chosen option, or None when the model picked a null option."""
    return None if answer.choice in null_options else answer.choice
