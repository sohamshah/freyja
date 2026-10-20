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


def test_a_transport_failure_is_retried_once(monkeypatch):
    """The decision service once stalled for 30 s, and every jev run in flight
    ended "error" on that one request. A transport failure gets one more try, and
    there is only one retry in all."""
    import asyncio

    import httpx

    from bridge.decisions import provider as prov
    from bridge.decisions.types import Choice

    monkeypatch.setattr(prov, "TRANSPORT_RETRY_S", 0.0)
    calls = []

    def post_with(failures):
        async def post(self, url, **kw):
            calls.append(url)
            if len(calls) <= failures:
                raise httpx.ReadTimeout("timed out")
            return httpx.Response(500, text="boom", request=httpx.Request("POST", url))

        return post

    p = prov.TypeSafeProvider(api_key="k")
    qs = {"op": Choice(instructions="pick", options={"a": "A", "b": "B"})}
    monkeypatch.setattr(httpx.AsyncClient, "post", post_with(1))
    with pytest.raises(prov.DecisionError, match="HTTP 500"):
        asyncio.run(p.decide({}, qs))
    assert len(calls) == 2, "one retry after the timeout, none after the HTTP error"

    calls.clear()
    monkeypatch.setattr(httpx.AsyncClient, "post", post_with(2))
    with pytest.raises(prov.DecisionError, match="transport error: ReadTimeout"):
        asyncio.run(p.decide({}, qs))
    assert len(calls) == 2


def test_a_server_error_answer_is_retried_once(monkeypatch):
    """The service answers some failures itself, as HTTP 499 "internal_server_error";
    two eval runs in an hour ended "error" on one. A 400 means the request is wrong
    and is not sent again."""
    import asyncio

    import httpx

    from bridge.decisions import provider as prov
    from bridge.decisions.types import Choice

    monkeypatch.setattr(prov, "TRANSPORT_RETRY_S", 0.0)
    calls = []
    answer = {"type": "choice", "choice": "a", "probabilities": {"a": 0.9, "b": 0.1}}
    ok = {"answers": {"op": answer}}

    def post_with(first):
        async def post(self, url, **kw):
            calls.append(url)
            req = httpx.Request("POST", url)
            if len(calls) == 1:
                return httpx.Response(first, text='{"detail": "internal_error"}', request=req)
            return httpx.Response(200, json=ok, request=req)

        return post

    p = prov.TypeSafeProvider(api_key="k")
    qs = {"op": Choice(instructions="pick", options={"a": "A", "b": "B"})}
    monkeypatch.setattr(httpx.AsyncClient, "post", post_with(499))
    assert asyncio.run(p.decide({}, qs)).choice("op").choice == "a" and len(calls) == 2

    calls.clear()
    monkeypatch.setattr(httpx.AsyncClient, "post", post_with(400))
    with pytest.raises(prov.DecisionError, match="HTTP 400"):
        asyncio.run(p.decide({}, qs))
    assert len(calls) == 1


def test_images_travel_as_assets_with_the_opt_in_header(monkeypatch):
    """An image is listed once under `assets` and stands in the state by its
    reference; Box questions carry no criteria; the answer is four edges. The
    service refuses images without its opt-in header, so the header goes with
    every request that has an asset and with no other."""
    import asyncio
    import base64

    import httpx

    from bridge.decisions import provider as prov
    from bridge.decisions.types import BOX_MODEL, Box, BoxAnswer, ImageAsset, Noul

    sent = []

    async def post(self, url, **kw):
        sent.append(kw)
        req = httpx.Request("POST", url)
        body = {
            "model": BOX_MODEL,
            "answers": {
                "seen": {"type": "noul", "noul": 0.93},
                "where": {"type": "box", "box": {"top": 0.25, "bottom": 0.35, "left": 0.4, "right": 0.6}, "confidence": 0.81},
            },
            "usage": {"input_tokens": 4321, "output_tokens": 30},
        }
        return httpx.Response(200, json=body, request=req)

    monkeypatch.setattr(httpx.AsyncClient, "post", post)
    p = prov.TypeSafeProvider(api_key="k")
    shot = ImageAsset(b"\x89PNG...", format="png")
    out = asyncio.run(
        p.decide(
            {"screen": shot.ref},
            {"seen": Noul("Is the Save button visible in `screen`?"), "where": Box("the Save button in `screen`")},
            model=BOX_MODEL,
            assets=[shot],
        )
    )
    kw = sent[0]
    assert kw["headers"][prov.IMAGES_HEADER] == "true"
    assert kw["json"]["model"] == BOX_MODEL
    assert kw["json"]["state"] == {"screen": shot.ref}
    assert kw["json"]["assets"] == {shot.ref: {"type": "image", "format": "png", "data_base64": base64.b64encode(b"\x89PNG...").decode()}}
    assert kw["json"]["questions"]["where"] == {"type": "box", "instructions": "the Save button in `screen`"}
    box = out.box("where")
    assert isinstance(box, BoxAnswer) and box.center == (0.5, 0.3) and box.confidence == 0.81
    assert box.pixels(1000, 500) == (400, 125, 600, 175) and abs(box.area - 0.02) < 1e-9
    assert out.compact()["where"] == {"box": [0.4, 0.25, 0.6, 0.35], "confidence": 0.81}
    assert out.input_tokens == 4321

    sent.clear()
    asyncio.run(p.decide({"a": 1}, {"seen": Noul("is it?")}))
    assert prov.IMAGES_HEADER not in sent[0]["headers"] and "assets" not in sent[0]["json"]
    assert ImageAsset(b"x", format="jpeg").payload()["format"] == "jpg"
    assert ImageAsset(b"x").ref != ImageAsset(b"x").ref, "a fresh reference per image, so it cannot occur in the text"
