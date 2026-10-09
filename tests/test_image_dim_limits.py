"""Image pixel caps: the many-image rule that bricked session-mufu8atv.

Past 20 images in one request, Anthropic rejects any image with a side
over 2000px ("image dimensions exceed max allowed size for many-image
requests: 2000 pixels"). The session had 26 images; one `view_image` of a
1640x2275 landing-page screenshot made every request 400, and the error
was treated as non-retryable with "try again in a moment" advice.

These tests pin: the size helpers, the transcript fit, the provider's
classification of the 400, the runner shrinking images before each
request and after the 400, `/repair`, and the failure text pointing at it.
"""

from __future__ import annotations

import base64
from io import BytesIO

import pytest
from PIL import Image

from engine.image_fit import (
    MANY_IMAGE_MAX_DIM,
    MAX_IMAGE_DIM,
    image_size,
    request_image_dim_limit,
    shrink_image,
)
from engine.providers import APIUsage, ImageDimensionsTooLargeError, ProviderResponse
from engine.session import Session, TranscriptManager
from engine.types import ImageBlock, Message, TextBlock, ToolResultBlock


def _png(w: int, h: int) -> str:
    buf = BytesIO()
    Image.new("RGB", (w, h), (40, 90, 160)).save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode("ascii")


def _jpeg(w: int, h: int) -> str:
    buf = BytesIO()
    Image.new("RGB", (w, h), (200, 30, 30)).save(buf, format="JPEG")
    return base64.b64encode(buf.getvalue()).decode("ascii")


def _image(w: int, h: int) -> ImageBlock:
    return ImageBlock(source_type="base64", data=_png(w, h), media_type="image/png")


def _transcript_with(images: list[ImageBlock]) -> TranscriptManager:
    tm = TranscriptManager()
    for img in images:
        tm.append_message(Message(role="user", content=[TextBlock(text="look"), img]))
        tm.append_message(Message(role="assistant", content="seen"))
    return tm


def _all_sizes(messages: list[Message]) -> list[tuple[int, int]]:
    sizes = []

    def _walk(container):
        for b in container:
            if isinstance(b, ImageBlock):
                sizes.append(image_size(b.data))
            elif isinstance(b, ToolResultBlock) and isinstance(b.content, list):
                _walk(b.content)

    for m in messages:
        if isinstance(m.content, list):
            _walk(m.content)
    return sizes


# ── helpers ────────────────────────────────────────────────────────────────

def test_request_dim_limit_tightens_past_twenty_images():
    assert request_image_dim_limit(0) == MAX_IMAGE_DIM
    assert request_image_dim_limit(20) == MAX_IMAGE_DIM
    assert request_image_dim_limit(21) == MANY_IMAGE_MAX_DIM == 2000


def test_image_size_reads_png_and_jpeg_and_rejects_garbage():
    assert image_size(_png(1640, 2275)) == (1640, 2275)
    assert image_size(_jpeg(300, 120)) == (300, 120)
    assert image_size("not-an-image") is None


def test_shrink_image_keeps_png_and_fits_long_edge():
    out = shrink_image(_png(1640, 2275), "image/png", 2000)
    assert out is not None
    data, media = out
    assert media == "image/png"
    assert image_size(data) == (1441, 2000)


def test_shrink_image_keeps_jpeg_as_jpeg():
    data, media = shrink_image(_jpeg(3000, 1000), "image/jpeg", 2000)
    assert media == "image/jpeg"
    assert image_size(data) == (2000, 666)


def test_shrink_image_returns_none_for_undecodable():
    assert shrink_image("bm90IGFuIGltYWdl", "image/png", 2000) is None


# ── transcript fit ─────────────────────────────────────────────────────────

def test_fit_for_request_leaves_tall_image_alone_at_twenty_images():
    tm = _transcript_with([_image(40, 40) for _ in range(19)] + [_image(1640, 2275)])
    fit = tm.fit_images_for_request()
    assert fit.image_count == 20
    assert fit.max_dim == MAX_IMAGE_DIM
    assert not fit.changed
    assert (1640, 2275) in _all_sizes(tm.get_messages())


def test_fit_for_request_shrinks_past_twenty_images():
    # The incident shape: 25 small images plus one tall screenshot.
    tm = _transcript_with([_image(40, 40) for _ in range(25)] + [_image(1640, 2275)])
    fit = tm.fit_images_for_request()
    assert fit.image_count == 26
    assert fit.max_dim == 2000
    assert fit.resized == 1 and fit.omitted == 0
    sizes = _all_sizes(tm.get_messages())
    assert len(sizes) == 26
    assert max(max(s) for s in sizes) == 2000
    # A second pass has nothing left to do.
    assert not tm.fit_images_for_request().changed


def test_fit_counts_and_shrinks_images_nested_in_tool_results():
    tm = TranscriptManager()
    nested = [_image(40, 40) for _ in range(21)] + [_image(2600, 900)]
    tm.append_message(Message(role="user", content=[
        ToolResultBlock(tool_use_id="t1", content=nested),
    ]))
    assert tm.count_images() == 22
    fit = tm.fit_images_for_request()
    assert fit.resized == 1
    assert max(max(s) for s in _all_sizes(tm.get_messages())) == 2000


def test_fit_swaps_unshrinkable_image_for_marker(monkeypatch):
    import engine.image_fit as image_fit

    monkeypatch.setattr(image_fit, "shrink_image", lambda *a, **k: None)
    tm = _transcript_with([_image(2400, 100)])
    fit = tm.fit_images_to_dim(2000)
    assert fit.omitted == 1 and fit.resized == 0
    content = tm.get_messages()[0].content
    assert not any(isinstance(b, ImageBlock) for b in content)
    assert any(
        isinstance(b, TextBlock) and "2400x100px is over the 2000px" in b.text
        for b in content
    )


def test_omit_images_strips_top_level_and_nested():
    tm = TranscriptManager()
    tm.append_message(Message(role="user", content=[_image(10, 10), TextBlock(text="hi")]))
    tm.append_message(Message(role="user", content=[
        ToolResultBlock(tool_use_id="t1", content=[_image(10, 10), TextBlock(text="r")]),
    ]))
    assert tm.omit_images("removed by /repair images") == 2
    assert tm.count_images() == 0
    assert "removed by /repair images" in tm.get_messages()[0].content[0].text


# ── provider classification ────────────────────────────────────────────────

class _StubAPIError(Exception):
    def __init__(self, m: str) -> None:
        super().__init__(m)
        self.status_code = 400

    def __str__(self) -> str:
        return self.args[0]


def _anthropic_provider():
    from engine.anthropic_provider import AnthropicConfig, AnthropicProvider

    return AnthropicProvider(AnthropicConfig(api_key="test-key", model="claude-opus-5-5"))


def test_provider_classifies_many_image_dimension_error():
    # Verbatim from the session-mufu8atv failure.
    msg = (
        "Error code: 400 - {'type': 'error', 'error': {'type': "
        "'invalid_request_error', 'message': 'messages.9.content.97.image."
        "source.base64.data: At least one of the image dimensions exceed max "
        "allowed size for many-image requests: 2000 pixels'}, 'request_id': "
        "'req_011'}"
    )
    err = _anthropic_provider()._convert_api_error(_StubAPIError(msg))
    assert isinstance(err, ImageDimensionsTooLargeError)
    assert err.max_dim == 2000
    assert err.retryable is False


def test_provider_classifies_single_image_dimension_error():
    msg = (
        "Error code: 400 - messages.0.content.1.image.source.base64.data: "
        "image dimensions exceed max allowed size: 8000 pixels"
    )
    err = _anthropic_provider()._convert_api_error(_StubAPIError(msg))
    assert isinstance(err, ImageDimensionsTooLargeError)
    assert err.max_dim == 8000


# ── runner ─────────────────────────────────────────────────────────────────

def _ok_response() -> ProviderResponse:
    return ProviderResponse(
        content="done",
        tool_calls=None,
        usage=APIUsage(input_tokens=10, output_tokens=2),
        stop_reason="end_turn",
        model="claude-opus-5-5",
    )


class _RecordingProvider:
    """Records the largest image side in each request; optionally rejects
    the first call with a many-image dimension error."""

    name = "anthropic"
    model_id = "claude-opus-5-5"
    context_window = 1_000_000

    def __init__(self, reject_first_with: int | None = None):
        self.reject_first_with = reject_first_with
        self.max_sides: list[int] = []

    async def complete_async(self, **kwargs):
        sizes = _all_sizes(kwargs.get("messages") or [])
        self.max_sides.append(max((max(s) for s in sizes), default=0))
        if self.reject_first_with and len(self.max_sides) == 1:
            raise ImageDimensionsTooLargeError(
                "At least one of the image dimensions exceed max allowed size "
                f"for many-image requests: {self.reject_first_with} pixels",
                max_dim=self.reject_first_with,
            )
        return _ok_response()


def _session_with(images: list[ImageBlock]) -> Session:
    session = Session.create(system_prompt="test")
    for img in images:
        session.add_user_message([TextBlock(text="look"), img])
        session.transcript.append_message(Message(role="assistant", content="seen"))
    return session


@pytest.mark.asyncio
async def test_runner_shrinks_images_before_a_many_image_request():
    from engine.runner import AsyncAgentRunner

    provider = _RecordingProvider()
    session = _session_with([_image(40, 40) for _ in range(25)] + [_image(1640, 2275)])
    result = await AsyncAgentRunner(provider).run(session, "continue", stream=False)

    assert result.success
    assert provider.max_sides == [2000], "the only request must already fit"


@pytest.mark.asyncio
async def test_runner_recovers_when_provider_rejects_image_dimensions():
    """The provider can count images differently (Bedrock/Vertex count PDFs
    toward the 20). A 400 naming the cap must shrink history and resend
    instead of failing the turn."""
    from engine.runner import AsyncAgentRunner

    provider = _RecordingProvider(reject_first_with=2000)
    # 20 images: our count says the 8000px cap applies, so the tall image
    # goes out unchanged and the provider rejects it.
    session = _session_with([_image(40, 40) for _ in range(19)] + [_image(1640, 2275)])
    result = await AsyncAgentRunner(provider).run(session, "continue", stream=False)

    assert result.success
    assert provider.max_sides == [2275, 2000]


# ── operator-facing failure text ───────────────────────────────────────────

def test_invalid_request_failure_points_to_repair_not_retry():
    from bridge.freyja_bridge import _format_user_facing_runner_failure

    out = _format_user_facing_runner_failure(
        reason="unknown",
        message=(
            "Error code: 400 - {'type': 'error', 'error': {'type': "
            "'invalid_request_error', 'message': 'bad history'}}"
        ),
        already_streamed=False,
    )
    assert "/repair" in out
    assert "Try again in a moment" not in out


def test_a_setting_the_model_refuses_does_not_send_the_user_to_repair():
    """The history is fine when the API refuses a model setting; /repair
    finds nothing (Sonnet 5.5 with a refusal fallback outside its allow-list)."""
    from bridge.freyja_bridge import _format_user_facing_runner_failure

    out = _format_user_facing_runner_failure(
        reason="unknown",
        message=(
            "Error code: 400 - {'type': 'error', 'error': {'type': "
            "'invalid_request_error', 'message': \"'claude-opus-4-8' is not a "
            "valid fallback target for 'claude-sonnet-5-5'.\"}}"
        ),
        already_streamed=False,
    )
    assert "`/repair` will not help" in out
    assert "/model" in out


def test_other_failures_keep_generic_text():
    from bridge.freyja_bridge import _format_user_facing_runner_failure

    out = _format_user_facing_runner_failure(
        reason="unknown", message="socket closed", already_streamed=False,
    )
    assert "/repair" not in out


# ── /repair (bridge) ───────────────────────────────────────────────────────

def _repair_target(session, tmp_path, monkeypatch):
    """A stand-in carrying just what ``_BridgeSession.repair_history`` reads,
    with backups and transcripts redirected into ``tmp_path``."""
    from types import SimpleNamespace

    import bridge.freyja_bridge as fb
    import bridge.transcript_persistence as tp

    monkeypatch.setattr(tp, "SESSIONS_DIR", tmp_path)
    events: list[dict] = []
    # log() also goes through emit; keep only what reaches the chat.
    monkeypatch.setattr(
        fb, "emit", lambda ev: events.append(ev) if ev.get("type") == "system_event" else None,
    )
    target = SimpleNamespace(id="session-test", session=session, pending_task=None, saves=0)
    target._write_repair_backup = lambda data: fb._BridgeSession._write_repair_backup(target, data)

    def _save():
        target.saves += 1

    target._save_transcript = _save
    return target, events


@pytest.mark.asyncio
async def test_repair_fixes_many_image_history_and_keeps_a_backup(tmp_path, monkeypatch):
    import gzip
    import json

    from bridge.freyja_bridge import _BridgeSession

    session = _session_with([_image(40, 40) for _ in range(25)] + [_image(1640, 2275)])
    target, events = _repair_target(session, tmp_path, monkeypatch)

    await _BridgeSession.repair_history(target)

    assert target.saves == 1
    (event,) = events
    assert event["subtype"] == "session_repaired"
    assert event["details"]["chatVisible"] is True
    assert event["details"]["changed"] is True
    assert "resized 1 image to the 2000px per-side limit" in event["message"]
    assert max(max(s) for s in _all_sizes(session.get_messages())) == 2000
    backups = list(tmp_path.glob("session-test.transcript.pre-repair-*.json.gz"))
    assert len(backups) == 1
    # The backup holds the pre-repair history, original screenshot intact.
    with gzip.open(backups[0], "rt") as f:
        restored = Session.create(system_prompt="test")
        restored.restore_transcript(json.load(f))
    assert (1640, 2275) in _all_sizes(restored.get_messages())


@pytest.mark.asyncio
async def test_repair_reports_nothing_found_without_saving(tmp_path, monkeypatch):
    from bridge.freyja_bridge import _BridgeSession

    target, events = _repair_target(_session_with([_image(40, 40)]), tmp_path, monkeypatch)
    await _BridgeSession.repair_history(target)

    assert target.saves == 0
    assert events[0]["details"]["changed"] is False
    assert "/repair images" in events[0]["message"]
    assert not list(tmp_path.iterdir())


@pytest.mark.asyncio
async def test_repair_images_strips_every_image(tmp_path, monkeypatch):
    from bridge.freyja_bridge import _BridgeSession

    session = _session_with([_image(40, 40), _image(50, 50)])
    target, events = _repair_target(session, tmp_path, monkeypatch)
    await _BridgeSession.repair_history(target, drop_images=True)

    assert session.transcript.count_images() == 0
    assert "removed 2 images from the model's history" in events[0]["message"]
    assert target.saves == 1


@pytest.mark.asyncio
async def test_repair_refuses_while_a_turn_runs(tmp_path, monkeypatch):
    import asyncio

    from bridge.freyja_bridge import _BridgeSession

    session = _session_with([_image(40, 40) for _ in range(25)] + [_image(1640, 2275)])
    target, events = _repair_target(session, tmp_path, monkeypatch)
    target.pending_task = asyncio.get_running_loop().create_future()

    await _BridgeSession.repair_history(target)

    assert events[0]["details"]["reason"] == "turn_running"
    assert target.saves == 0
    assert (1640, 2275) in _all_sizes(session.get_messages())
    target.pending_task.cancel()
