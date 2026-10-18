"""Values the planner works out reach the field it names.

The planner reads pages the screen no longer shows (an order's price, a policy's
rate, a sign-in code in an email) and writes "Type 86.00 into the Amount field
[16]". About half its typing sub-goals leave out the quotes. Jev types only
listed text, and the text door saw the goal and the screen but not the sub-goal,
so it returned no text: every desk refund run, and a sign-in run, stopped there
with the right value in the sub-goal (28 times in one baseline round).
"""

from __future__ import annotations

import asyncio
import json

from bridge.tools.jev_operator.decide import Thresholds, interpret
from bridge.tools.jev_operator.handoff import LLMHelper
from bridge.tools.jev_operator.observe import typed_values
from tests.test_jev_env_patterns import _answers, _obs
from tests.test_jev_dom_surface import action
from tests.test_jev_operator import FormNative, ScriptedProvider, make_operator


def test_typed_values_reads_the_unquoted_value_a_subgoal_types():
    assert typed_values("Type 86.00 into the Amount field [16].") == ["86.00"]
    assert typed_values(
        "Click the Decision dropdown [15] and choose 'Approve refund', then type 19.80 into Amount [16]."
    ) == ["19.80"]
    assert typed_values("Type exactly 19.80 into the Amount field [16]") == ["19.80"]
    assert typed_values("Type the text 587734 into the '6-digit code' field [3]") == ["587734"]
    assert typed_values(
        "Type edsger.dijkstra@quillmere.example.com into the Search people field [13]"
    ) == ["edsger.dijkstra@quillmere.example.com"]
    assert typed_values(
        "Type 17.2 into the Q2 Section field [21], then click Yes [22] and type 12.1 into [24]."
    ) == ["17.2", "12.1"]
    # Quoted text is read by quoted_literals; a word with no digit or @ is an
    # instruction, not text ("the query"); a key press is not typing.
    assert typed_values('Type "86.00" into the Amount field [16]') == []
    assert typed_values("Type the query into the Search field [2]") == []
    assert typed_values("Click Search [3], then press enter in the field") == []


def test_the_typing_rescue_takes_an_unquoted_value():
    """Jev spreads a typing step over the steps typing does itself; the rescue
    took the planner's quoted text only, so "Type 86.00 into ..." went to a replan."""
    obs = _obs([action(1, "fill", "Amount"), action(2, "click", "Submit decision")])
    field = obs.elements[0]
    spread = {"scroll_down": 0.42, "click": 0.40, "type": 0.15, "done": 0.03}
    a = _answers(
        spread,
        type_target=(str(field.index), 0.95),
        text_source=("lit0", 0.97),
        click_target=("none", 0.9),
    )
    d = interpret(
        a,
        obs,
        literals=["86.00"],
        launch_candidates=[],
        thresholds=Thresholds(),
        subgoal=f"Type 86.00 into the Amount field [{field.index}].",
    )
    assert d.operation == "type" and d.target is field and d.text_literal == "86.00"
    assert "taken as type" in d.reasons[-1]


def test_a_value_the_planner_works_out_is_a_typing_option(tmp_path, monkeypatch):
    monkeypatch.setattr("bridge.tools.jev_operator.loop.RUN_DIR", tmp_path)

    async def planner(messages, system_prompt, max_tokens):
        if "planner" in system_prompt:
            return json.dumps(
                {"status": "continue", "subgoal": "Type 86.00 into the Username field [1].", "note": ""}
            )
        return json.dumps({"satisfied": True, "summary": "ok"})

    provider = ScriptedProvider([("need_help", None), ("done", None)])
    op, _ = make_operator(
        FormNative(), provider, goal="refund the order the policy allows", llm=LLMHelper(planner, max_calls=4)
    )
    asyncio.run(op.run())
    opts = list(provider.calls[1]["questions"]["text_source"].options.values())
    assert "type exactly: 86.00" in opts


def test_the_text_door_sees_the_subgoal_and_the_planner_notes(tmp_path, monkeypatch):
    monkeypatch.setattr("bridge.tools.jev_operator.loop.RUN_DIR", tmp_path)
    seen: list[tuple[str, dict]] = []

    async def door(messages, system_prompt, max_tokens):
        seen.append((system_prompt, json.loads(messages[0].content)))
        return json.dumps({"text": "Lumen Robotics", "values": []})

    op, _ = make_operator(FormNative(), ScriptedProvider([]), goal="enable the model", llm=LLMHelper(door))
    op._subgoal = "Type Lumen Robotics into Business name [1]"
    op._planner_notes = ["The business is Lumen Robotics, from the account page."]
    d = type("D", (), {"target": type("T", (), {"label": "Business name", "role": "AXTextField", "value": ""})()})()
    assert asyncio.run(op._field_text(d, type("O", (), {"screen_text": ""})())) == "Lumen Robotics"
    system, payload = seen[0]
    assert payload["subgoal"] == op._subgoal and payload["planner_notes"] == op._planner_notes
    assert "subgoal" in system and "planner_notes" in system

    # Without a sub-goal or notes the door's prompt and payload are as before.
    seen.clear()
    asyncio.run(
        LLMHelper(door).field_text(
            goal="g", field_label="Name", field_role="AXTextField", field_value="", screen_text="", history=[]
        )
    )
    assert "subgoal" not in seen[0][1] and "planner_notes" not in seen[0][0]
