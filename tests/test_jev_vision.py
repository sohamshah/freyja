"""The planner names a control; Jev finds it in a capture of the window.

`vision.locate` asks one Box question (and one Noul for whether the target is
there at all), looks again on a crop when the first box is small, and the
operator clicks the box's centre mapped through the window's bounds."""

from __future__ import annotations

import asyncio
import io
import json
from typing import Any

from PIL import Image, ImageDraw

from bridge.decisions.types import Answers, Box, BoxAnswer, NoulAnswer
from bridge.tools.jev_operator import vision
from bridge.tools.jev_operator.handoff import LLMHelper
from tests.test_jev_operator import FormNative, ScriptedProvider, make_operator


def png(width: int, height: int, rect: tuple[int, int, int, int] | None = None) -> bytes:
    img = Image.new("RGB", (width, height), "white")
    if rect:
        ImageDraw.Draw(img).rectangle(rect, fill="black")
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


class SeeingProvider(ScriptedProvider):
    """Answers Box questions from a script of (visible, box) pairs, one per
    request, and everything else as ScriptedProvider does."""

    def __init__(self, script, boxes: list[tuple[float, tuple[float, float, float, float]]], confidence: float = 0.8) -> None:
        super().__init__(script)
        self.boxes = list(boxes)
        self.confidence = confidence
        self.image_calls: list[dict[str, Any]] = []

    async def decide(self, state, questions, *, model=None, assets=None) -> Answers:
        if not any(isinstance(q, Box) for q in questions.values()):
            return await super().decide(state, questions, model=model)
        self.image_calls.append({"state": state, "questions": questions, "model": model, "assets": assets})
        visible, (left, top, right, bottom) = self.boxes.pop(0)
        out: dict[str, Any] = {}
        for name, q in questions.items():
            if isinstance(q, Box):
                out[name] = BoxAnswer(left=left, top=top, right=right, bottom=bottom, confidence=self.confidence)
            else:
                out[name] = NoulAnswer(p=visible)
        return Answers(answers=out, model="jev-1.14.0", provider="scripted", latency_ms=3, input_tokens=4000)


def test_locate_asks_once_for_a_large_target():
    p = SeeingProvider([], [(0.95, (0.4, 0.7, 0.7, 0.8))])
    loc = asyncio.run(vision.locate(p, png(400, 300), "the Continue button."))
    assert loc.found and loc.stages == 1 and (loc.x, loc.y) == (0.55, 0.75)
    call = p.image_calls[0]
    ref = call["assets"][0].ref
    assert call["state"] == {"screen": ref} and call["model"] == "jev-1.14.0"
    assert call["questions"]["target"].instructions == "the Continue button in `screen`"
    assert call["questions"]["visible"].instructions == "Is the Continue button visible in `screen`?"
    image = Image.open(io.BytesIO(call["assets"][0].data))
    assert image.size == (400, 300) and image.format == "PNG"
    assert loc.input_tokens == 4000


def test_locate_fits_the_capture_to_the_service_window():
    p = SeeingProvider([], [(0.9, (0.1, 0.1, 0.5, 0.5))])
    asyncio.run(vision.locate(p, png(3000, 1500), "the thing"))
    image = Image.open(io.BytesIO(p.image_calls[0]["assets"][0].data))
    assert image.size == (1568, 784), "the longest side is brought to the largest the service takes"


def test_locate_looks_again_on_a_crop_for_a_small_target():
    # First box: a small target around (0.5, 0.5) of a 1000x800 capture. The
    # crop is 40% of each side centred there: (300, 240)..(700, 560). The second
    # box, in crop fractions, maps back to the capture.
    p = SeeingProvider([], [(0.9, (0.49, 0.49, 0.51, 0.51)), (0.9, (0.25, 0.5, 0.35, 0.6))])
    loc = asyncio.run(vision.locate(p, png(1000, 800), "the tiny icon"))
    assert loc.stages == 2 and len(p.image_calls) == 2
    crop = Image.open(io.BytesIO(p.image_calls[1]["assets"][0].data))
    assert crop.size == (400, 320)
    assert "visible" not in p.image_calls[1]["questions"], "the second look only asks where"
    assert abs(loc.box.left - 0.4) < 1e-9 and abs(loc.box.top - 0.5) < 1e-9
    assert abs(loc.x - 0.42) < 1e-9 and abs(loc.y - 0.52) < 1e-9
    assert loc.input_tokens == 8000


def test_locate_can_skip_the_second_look():
    p = SeeingProvider([], [(0.9, (0.49, 0.49, 0.51, 0.51))])
    loc = asyncio.run(vision.locate(p, png(1000, 600), "the tiny icon", look_closer=False))
    assert loc.stages == 1 and len(p.image_calls) == 1


def test_an_absent_target_still_has_a_box_but_is_not_found():
    p = SeeingProvider([], [(0.2, (0.4, 0.7, 0.7, 0.8))])
    loc = asyncio.run(vision.locate(p, png(400, 300), "the Continue button"))
    assert not loc.seen and not loc.found and loc.visible == 0.2


def test_a_low_confidence_box_is_seen_but_not_found():
    p = SeeingProvider([], [(0.9, (0.4, 0.7, 0.7, 0.8))], confidence=0.3)
    loc = asyncio.run(vision.locate(p, png(400, 300), "the Add button in the third row"))
    assert loc.seen and not loc.found and loc.confidence == 0.3


def test_frame_change_measures_the_share_of_the_window_that_moved():
    blank = png(400, 300)
    assert vision.frame_change(blank, blank) == 0.0
    assert vision.frame_change(blank, png(400, 300, (0, 0, 199, 299))) > 0.45
    from bridge.tools.jev_operator.loop import PIXEL_CHANGE_MIN

    # A caret is 2x16 px of a 1280x900 page; a checkbox tick about 10x10.
    assert vision.frame_change(png(1280, 900), png(1280, 900, (10, 10, 11, 25))) < PIXEL_CHANGE_MIN, "a caret is not a change"
    assert vision.frame_change(png(1280, 900), png(1280, 900, (10, 10, 20, 20))) > PIXEL_CHANGE_MIN, "a checkbox tick is"


def seeing_planner(target: str, sizes: list | None = None):
    async def planner(messages, system_prompt, max_tokens):
        if "planner" in system_prompt:
            if sizes is not None:
                sizes.append(1)
            return json.dumps({"status": "continue", "subgoal": "x", "direct_action": {"kind": "click", "target": target}, "note": ""})
        return json.dumps({"satisfied": True, "summary": "ok", "subgoal": None})

    return planner


def frames(*images: bytes):
    """A window capture that returns the given images in turn, then the last."""
    queue = list(images)

    async def frame(reason):
        data = queue.pop(0) if len(queue) > 1 else queue[0]
        img = Image.open(io.BytesIO(data))
        from types import SimpleNamespace

        return SimpleNamespace(data=data, width=img.width, height=img.height, mime_type="image/png")

    return frame


def test_planner_click_by_description_is_grounded_by_jev(tmp_path, monkeypatch):
    """The window is 400x300 at (0, 40). 'Continue' is at native (160..280,
    260..290): in window fractions (0.4..0.7, 0.733..0.833). Jev's box is
    taken through the window bounds to the screen, and the window changing
    afterwards counts as progress. The form exposes elements, so the planner
    gets its screenshot on the second replan, as in the pixel-click test."""
    monkeypatch.setattr("bridge.tools.jev_operator.loop.RUN_DIR", tmp_path)
    native = FormNative()
    provider = SeeingProvider([("need_help", None), ("need_help", None), ("done", None)], [(0.94, (0.4, 0.733, 0.7, 0.833))])
    op, _ = make_operator(native, provider, goal="g", llm=LLMHelper(seeing_planner("the Continue button"), max_calls=6))
    op.actuator.frame = frames(png(400, 300), png(400, 300, (0, 0, 399, 150)))
    res = asyncio.run(op.run())
    assert native.clicks == ["Continue"]
    assert res.status == "done"
    clicked = next(h for h in res.history if h["kind"] == "click")
    assert clicked["action"].startswith("planner click on 'the Continue button' at (220, 274)")
    assert clicked["changed"] is True and "of the window changed" in clicked["diff"]
    assert op._replans_since_progress == 0, "a changed window after a grounded click is progress"
    log = [json.loads(line) for line in open(res.log_path or next(tmp_path.glob("*.jsonl")))]
    located = next(e for e in log if e.get("event") == "locate")
    assert located["target"] == "the Continue button" and located["visible"] == 0.94 and located["stages"] == 1


def test_a_target_jev_does_not_see_is_not_clicked(tmp_path, monkeypatch):
    monkeypatch.setattr("bridge.tools.jev_operator.loop.RUN_DIR", tmp_path)
    native = FormNative()
    provider = SeeingProvider([("need_help", None), ("need_help", None), ("done", None)], [(0.12, (0.4, 0.733, 0.7, 0.833))])
    op, _ = make_operator(native, provider, goal="g", llm=LLMHelper(seeing_planner("the Finish button"), max_calls=6))
    op.actuator.frame = frames(png(400, 300))
    res = asyncio.run(op.run())
    assert native.clicks == []
    skipped = next(h for h in res.history if h["kind"] == "click")
    assert "skipped" in skipped["action"] and "p=0.12" in skipped["diff"]


def test_a_described_irreversible_control_is_refused_without_an_element(tmp_path, monkeypatch):
    """With no element under the point (the app exposes none), the planner's
    own words for the control are what the gate can judge."""
    monkeypatch.setattr("bridge.tools.jev_operator.loop.RUN_DIR", tmp_path)

    class Bare(FormNative):
        def read_ax_tree(self, pid, max_depth=8):
            return json.dumps({"role": "AXApplication", "title": "FormApp", "children": [
                {"role": "AXWindow", "title": "Sign in", "focused": True, "bounds": [0, 40, 400, 300], "children": []}
            ]})

    native = Bare()
    provider = SeeingProvider([("need_help", None), ("done", None)], [(0.9, (0.05, 0.733, 0.35, 0.833))])
    op, _ = make_operator(native, provider, goal="g", llm=LLMHelper(seeing_planner("the red Delete account button"), max_calls=6))
    op.actuator.frame = frames(png(400, 300))
    res = asyncio.run(op.run())
    assert native.clicks == []
    refused = next(h for h in res.history if h["kind"] == "click")
    assert refused["action"].endswith("refused") and "looks irreversible" in refused["diff"]


def test_the_planners_own_coordinates_win_on_the_screenshot_it_read(tmp_path, monkeypatch):
    """Frontier planners put their coordinates inside the control about 99% of
    the time on the screenshot they are looking at; Jev is not asked."""
    monkeypatch.setattr("bridge.tools.jev_operator.loop.RUN_DIR", tmp_path)

    async def planner(messages, system_prompt, max_tokens):
        if "planner" in system_prompt:
            return json.dumps({"status": "continue", "subgoal": "x", "direct_action": {"kind": "click", "target": "the Continue button", "x": 220, "y": 235}, "note": ""})
        return json.dumps({"satisfied": True, "summary": "ok", "subgoal": None})

    native = FormNative()
    provider = SeeingProvider([("need_help", None), ("need_help", None), ("done", None)], [(0.1, (0.0, 0.0, 0.1, 0.1))])
    op, _ = make_operator(native, provider, goal="g", llm=LLMHelper(planner, max_calls=6))
    op.actuator.frame = frames(png(400, 300))
    res = asyncio.run(op.run())
    assert native.clicks == ["Continue"] and provider.image_calls == []
    clicked = next(h for h in res.history if h["kind"] == "click")
    assert clicked["action"].startswith("planner click at (220, 275) on 'the Continue button'")


def plan_planner(actions: list[dict]):
    async def planner(messages, system_prompt, max_tokens):
        if "planner" in system_prompt:
            return json.dumps({"status": "continue", "subgoal": "x", "direct_actions": actions, "note": ""})
        return json.dumps({"satisfied": True, "summary": "ok", "subgoal": None})

    return planner


def test_a_plan_grounds_its_later_clicks_on_fresh_captures(tmp_path, monkeypatch):
    """First click by the planner's pixels on 'Continue'; the second names
    'Continue' again (the form shows it still, at window fractions
    (0.4..0.7, 0.733..0.833)) and Jev finds it on the capture taken after
    the first click changed the window."""
    monkeypatch.setattr("bridge.tools.jev_operator.loop.RUN_DIR", tmp_path)
    native = FormNative()
    provider = SeeingProvider(
        [("need_help", None), ("need_help", None), ("done", None)], [(0.9, (0.4, 0.733, 0.7, 0.833))]
    )
    plan = [
        {"kind": "click", "x": 220, "y": 235, "target": "the Continue button"},
        {"kind": "click", "target": "the Continue button"},
    ]
    op, _ = make_operator(native, provider, goal="g", llm=LLMHelper(plan_planner(plan), max_calls=6))
    # Planning frame; the actuator's own post-click frame; the capture the
    # outcome check takes, which the second click is grounded on; the second
    # click's post-click frame; the capture after it.
    op.actuator.frame = frames(
        png(400, 300),
        png(400, 300, (0, 0, 399, 150)),
        png(400, 300, (0, 150, 399, 299)),
        png(400, 300, (0, 150, 399, 299)),
        png(400, 300, (0, 0, 399, 299)),
    )
    res = asyncio.run(op.run())
    assert native.clicks == ["Continue", "Continue"]
    assert len(provider.image_calls) == 1, "only the click the planner could not place asked Jev"
    grounded = Image.open(io.BytesIO(provider.image_calls[0]["assets"][0].data))
    assert grounded.getpixel((10, 290)) == (0, 0, 0), "grounded on the capture after the first click, not the planning one"
    clicks = [h for h in res.history if h["kind"] == "click"]
    assert all(h["changed"] for h in clicks)


def test_a_plan_stops_when_the_window_does_not_change(tmp_path, monkeypatch):
    monkeypatch.setattr("bridge.tools.jev_operator.loop.RUN_DIR", tmp_path)
    native = FormNative()
    provider = SeeingProvider([("need_help", None), ("need_help", None), ("done", None)], [(0.9, (0.4, 0.733, 0.7, 0.833))])
    plan = [
        {"kind": "click", "x": 220, "y": 235, "target": "the Continue button"},
        {"kind": "click", "target": "the Continue button"},
    ]
    op, _ = make_operator(native, provider, goal="g", llm=LLMHelper(plan_planner(plan), max_calls=6))
    op.actuator.frame = frames(png(400, 300))
    asyncio.run(op.run())
    assert native.clicks == ["Continue"] and provider.image_calls == []


def test_a_plan_stops_at_a_target_jev_does_not_see(tmp_path, monkeypatch):
    monkeypatch.setattr("bridge.tools.jev_operator.loop.RUN_DIR", tmp_path)
    native = FormNative()
    provider = SeeingProvider([("need_help", None), ("need_help", None), ("done", None)], [(0.2, (0.4, 0.733, 0.7, 0.833))])
    plan = [
        {"kind": "click", "x": 220, "y": 235, "target": "the Continue button"},
        {"kind": "click", "target": "the Finish button"},
        {"kind": "click", "target": "the Continue button"},
    ]
    op, _ = make_operator(native, provider, goal="g", llm=LLMHelper(plan_planner(plan), max_calls=6))
    op.actuator.frame = frames(png(400, 300), png(400, 300, (0, 0, 399, 150)))
    res = asyncio.run(op.run())
    assert native.clicks == ["Continue"] and len(provider.image_calls) == 1
    assert any("skipped" in h["action"] for h in res.history if h["kind"] == "click")


def test_a_plan_leaves_an_unsure_click_to_the_planner(tmp_path, monkeypatch):
    monkeypatch.setattr("bridge.tools.jev_operator.loop.RUN_DIR", tmp_path)
    native = FormNative()
    provider = SeeingProvider(
        [("need_help", None), ("need_help", None), ("done", None)], [(0.95, (0.4, 0.733, 0.7, 0.833))], confidence=0.3
    )
    plan = [
        {"kind": "click", "x": 220, "y": 235, "target": "the Continue button"},
        {"kind": "click", "target": "the Continue button in the second row"},
    ]
    op, _ = make_operator(native, provider, goal="g", llm=LLMHelper(plan_planner(plan), max_calls=6))
    op.actuator.frame = frames(png(400, 300), png(400, 300, (0, 0, 399, 150)))
    res = asyncio.run(op.run())
    assert native.clicks == ["Continue"] and len(provider.image_calls) == 1
    skipped = next(h for h in res.history if "skipped" in h["action"])
    assert "unsure" in skipped["diff"] and "0.30" in skipped["diff"]
