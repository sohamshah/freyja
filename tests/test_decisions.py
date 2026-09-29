from __future__ import annotations

import json
import random
from pathlib import Path
from typing import Any

import pytest

from bridge.decisions import (
    Choice,
    DecisionClient,
    DecisionLog,
    DecisionUnavailable,
    Noul,
    Score,
    evaluate_gate,
    load_specs,
)
from bridge.decisions.spec import SPECS_DIR, spec_from_dict
from bridge.decisions.types import parse_answers, question_payload


class FakeProvider:
    def __init__(self, name: str = "typesafe", canned: dict[str, Any] | None = None) -> None:
        self.name = name
        self.canned = canned or {}
        self.calls: list[tuple[Any, dict[str, Any]]] = []

    def _answer(self, state: Any, questions: dict[str, Any]) -> Any:
        self.calls.append((state, questions))
        answers: dict[str, Any] = {}
        for key, q in questions.items():
            if key in self.canned:
                answers[key] = self.canned[key]
            elif isinstance(q, Noul):
                answers[key] = {"type": "noul", "noul": 0.5}
            elif isinstance(q, Choice):
                first = next(iter(q.options))
                probs = {k: (1.0 if k == first else 0.0) for k in q.options}
                answers[key] = {"type": "choice", "choice": first, "probabilities": probs, "confidence": 1.0}
            elif isinstance(q, Score):
                answers[key] = {
                    "type": "score",
                    "score": 0.0,
                    "probabilities": [1.0] + [0.0] * (len(q.levels) - 1),
                    "legend": list(q.levels),
                    "confidence": 1.0,
                }
        payload = {"model": "fake-1", "answers": answers, "usage": {"input_tokens": 10, "output_tokens": 2}}
        return parse_answers(payload, provider=self.name, latency_ms=1)

    async def decide(self, state, questions, *, model=None):
        return self._answer(state, questions)

    def decide_sync(self, state, questions, *, model=None):
        return self._answer(state, questions)


def _client(tmp_path: Path, provider: FakeProvider, specs=None) -> DecisionClient:
    return DecisionClient(
        {provider.name: provider},
        log=DecisionLog(tmp_path / "log.jsonl"),
        specs=specs if specs is not None else load_specs(),
        rng=random.Random(0),
    )


def test_shipped_specs_load_and_validate():
    specs = load_specs(SPECS_DIR)
    assert {"command_risk", "tool_result_injection", "ax_action"} <= set(specs)
    for spec in specs.values():
        assert spec.mode == "shadow", f"{spec.id} must ship in shadow mode"
        for key, q in spec.questions.items():
            if isinstance(q, Choice) and key not in spec.dynamic_options:
                assert any(o in q.options for o in ("other", "none", "unclear", "UNCLEAR", "need_vision")), (
                    f"{spec.id}.{key} has no null option"
                )


def test_question_payload_shapes():
    assert question_payload(Noul("q?", {"true": "a", "false": "b"})) == {
        "type": "noul", "instructions": "q?", "criteria": {"true": "a", "false": "b"},
    }
    assert question_payload(Choice("which?", {"a": "x", "other": None}))["criteria"] == {"a": "x", "other": None}
    assert question_payload(Score("how?", ("low", "high")))["criteria"] == ["low", "high"]
    with pytest.raises(ValueError):
        question_payload(Choice("which?", {"only": None}))
    with pytest.raises(ValueError):
        question_payload(Score("how?", ("one",)))


def test_parse_answers_derives_confidence_when_missing():
    payload = {
        "model": "m",
        "answers": {
            "c": {"type": "choice", "choice": "a", "probabilities": {"a": 0.7, "b": 0.2, "other": 0.1}},
            "n": {"type": "noul", "noul": 0.9},
        },
    }
    answers = parse_answers(payload, provider="p", latency_ms=5)
    assert answers.choice("c").confidence == pytest.approx((3 * 0.7 - 1) / 2)
    assert answers.noul("n") == 0.9
    assert answers["n"].confidence == pytest.approx(0.8)


def test_gate_bands_and_confidence():
    spec = spec_from_dict(
        {
            "id": "t", "version": 1, "mode": "gate", "privacy": "public",
            "questions": {
                "a": {"type": "noul", "instructions": "a?"},
                "k": {"type": "choice", "instructions": "k?", "options": {"x": None, "other": None}},
            },
            "gate": {"score": {"a": 1.0}, "act_below": 0.3, "escalate_above": 0.7, "min_confidence": {"k": 0.6}},
        }
    )
    def answers(p: float, conf: float):
        return parse_answers(
            {"answers": {
                "a": {"type": "noul", "noul": p},
                "k": {"type": "choice", "choice": "x", "probabilities": {"x": 0.5, "other": 0.5}, "confidence": conf},
            }}, provider="p", latency_ms=1,
        )
    assert evaluate_gate(spec, answers(0.1, 0.9)).action == "act"
    assert evaluate_gate(spec, answers(0.5, 0.9)).action == "abstain"
    assert evaluate_gate(spec, answers(0.9, 0.9)).action == "escalate"
    assert evaluate_gate(spec, answers(0.1, 0.2)).action == "abstain"
    assert evaluate_gate(spec, answers(0.9, 0.2)).action == "escalate"


def test_privacy_routing_blocks_cloud_for_personal_state(tmp_path):
    cloud = FakeProvider("typesafe")
    client = _client(tmp_path, cloud)
    with pytest.raises(DecisionUnavailable):
        client.decide_sync("ax_action", {"goal": "x"}, options={"click_target": {"1": None, "none": None}, "type_target": {"none": None, "1": None}})
    local = FakeProvider("local")
    client = DecisionClient({"typesafe": cloud, "local": local}, log=DecisionLog(tmp_path / "l.jsonl"))
    d = client.decide_sync(
        "ax_action", {"goal": "x"},
        options={"click_target": {"1": "button", "none": None}, "type_target": {"none": None, "1": None}},
    )
    assert d.answers.provider == "local"
    assert cloud.calls == []


def test_frame_dynamic_options_shuffle_and_log(tmp_path):
    provider = FakeProvider("typesafe")
    client = _client(tmp_path, provider)
    d = client.decide_sync("tool_result_injection", {"tool_name": "web_fetch", "chunk_text": "hello"}, context={"turn": 3})
    state, questions = provider.calls[0]
    assert set(state) == {"reader_guidance", "content"}
    assert state["content"]["tool_name"] == "web_fetch"
    assert set(questions["content_kind"].options) == {"ordinary_content", "boilerplate_warning", "instructions_to_agent", "unclear"}
    assert d.effective_action == "abstain"  # shadow mode never acts
    assert d.verdict.action in {"act", "abstain", "escalate"}
    records = [json.loads(line) for line in (tmp_path / "log.jsonl").read_text().splitlines()]
    assert records[0]["spec"] == "tool_result_injection@2"
    assert records[0]["context"] == {"turn": 3}
    assert records[0]["answers"]["content_kind"]["choice"] in questions["content_kind"].options
    client.log.record_outcome(d.id, "false_alarm")
    assert client.log.read()[-1] == {**client.log.read()[-1], "kind": "outcome", "decision_id": d.id}


def test_gate_choice_features_and_escalate_if_any():
    spec = spec_from_dict(
        {
            "id": "t2", "version": 1, "mode": "gate", "privacy": "public",
            "questions": {
                "exfil": {"type": "noul", "instructions": "e?"},
                "kind": {"type": "choice", "instructions": "k?", "options": {"benign": None, "attack": None, "unclear": None}},
            },
            "gate": {"score": {"kind=attack": 0.5, "exfil": 0.5}, "act_below": 0.3, "escalate_above": 0.6,
                     "escalate_if_any": {"exfil": 0.9}},
        }
    )
    def answers(p_attack: float, p_exfil: float):
        return parse_answers(
            {"answers": {
                "exfil": {"type": "noul", "noul": p_exfil},
                "kind": {"type": "choice", "choice": "benign", "probabilities": {"benign": 1 - p_attack, "attack": p_attack, "unclear": 0.0}},
            }}, provider="p", latency_ms=1,
        )
    assert evaluate_gate(spec, answers(0.1, 0.1)).action == "act"
    assert evaluate_gate(spec, answers(0.9, 0.5)).action == "escalate"
    assert evaluate_gate(spec, answers(0.0, 0.95)).action == "escalate"  # single decisive predicate
    assert evaluate_gate(spec, answers(0.5, 0.3)).action == "abstain"
    with pytest.raises(ValueError):
        spec_from_dict({"id": "bad", "questions": {"k": {"type": "choice", "instructions": "k?", "options": {"a": None, "b": None}}},
                        "mode": "gate", "privacy": "public", "gate": {"score": {"k=zzz": 1.0}}})


def test_dynamic_options_are_required(tmp_path):
    client = DecisionClient({"local": FakeProvider("local")}, log=DecisionLog(tmp_path / "l.jsonl"))
    with pytest.raises(ValueError):
        client.decide_sync("ax_action", {"goal": "x"})
