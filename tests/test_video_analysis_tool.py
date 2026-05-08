"""Unit tests for AnalyzeVideoTool — local file + YouTube + missing-key paths."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from bridge.tools.video_analysis_tool import (
    AnalyzeVideoTool,
    DEFAULT_VIDEO_MODEL,
    _format_offset,
    _looks_like_youtube_url,
)
from engine.types import TextBlock


# ── tiny fake of the bits of the genai client surface we touch ──────


class _FakeFiles:
    def __init__(self, *, ready_after: int = 1) -> None:
        self.uploaded_paths: list[str] = []
        self.get_calls: list[str] = []
        self._ready_after = ready_after
        self._poll_count = 0

    def upload(self, *, file: str) -> Any:
        self.uploaded_paths.append(file)
        return SimpleNamespace(
            name="files/abc123",
            uri="https://generativelanguage.googleapis.com/v1beta/files/abc123",
            mime_type="video/mp4",
            state=SimpleNamespace(name="PROCESSING"),
        )

    def get(self, *, name: str) -> Any:
        self.get_calls.append(name)
        self._poll_count += 1
        state_name = "ACTIVE" if self._poll_count >= self._ready_after else "PROCESSING"
        return SimpleNamespace(
            name=name,
            uri="https://generativelanguage.googleapis.com/v1beta/files/abc123",
            mime_type="video/mp4",
            state=SimpleNamespace(name=state_name),
        )


class _FakeAioModels:
    def __init__(self, *, response_text: str = "ok") -> None:
        self.last_kwargs: dict[str, Any] | None = None
        self._response_text = response_text

    async def generate_content(self, **kwargs: Any) -> Any:
        self.last_kwargs = kwargs
        return SimpleNamespace(text=self._response_text, candidates=[])


class _FakeClient:
    def __init__(self, *, ready_after: int = 1, response_text: str = "ok") -> None:
        self.files = _FakeFiles(ready_after=ready_after)
        self.aio = SimpleNamespace(models=_FakeAioModels(response_text=response_text))
        # Capture the api_key the tool passed in so we can assert on it.
        self.api_key: str | None = None


def _client_factory(*, response_text: str = "summary text", ready_after: int = 1):
    """Return a (factory, captured_client) pair so tests can inspect the client."""
    captured: dict[str, Any] = {}

    def factory(*, api_key: str) -> _FakeClient:
        client = _FakeClient(ready_after=ready_after, response_text=response_text)
        client.api_key = api_key
        captured["client"] = client
        return client

    return factory, captured


# ── tests ──────────────────────────────────────────────────────────


def test_youtube_url_detector():
    assert _looks_like_youtube_url("https://www.youtube.com/watch?v=abc")
    assert _looks_like_youtube_url("youtu.be/abc123")
    assert _looks_like_youtube_url("https://m.youtube.com/watch?v=abc")
    assert not _looks_like_youtube_url("https://vimeo.com/123")
    assert not _looks_like_youtube_url("/local/file.mp4")


def test_format_offset_renders_seconds():
    assert _format_offset(0) == "0s"
    assert _format_offset(125) == "125s"
    assert _format_offset(12.5) == "12.5s"
    assert _format_offset(None) is None
    assert _format_offset(-3) is None
    assert _format_offset("abc") is None


@pytest.mark.asyncio
async def test_youtube_url_passthrough_no_upload(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    factory, captured = _client_factory(response_text="recap of the video")
    tool = AnalyzeVideoTool(client_factory=factory)

    result = await tool.execute(
        "call-1",
        {
            "prompt": "summarize",
            "youtube_url": "https://www.youtube.com/watch?v=9hE5-98ZeCg",
            "fps": 0.5,
            "start_seconds": 30,
            "end_seconds": 90,
            "media_resolution": "low",
        },
    )

    assert not result.is_error
    assert isinstance(result.content, list)
    assert any(isinstance(block, TextBlock) for block in result.content)
    assert "recap of the video" in result.content[0].text
    assert "youtube" in result.content[0].text.lower()

    client = captured["client"]
    assert client.api_key == "test-key"
    # YouTube path should NOT have touched the Files API at all.
    assert client.files.uploaded_paths == []
    assert client.files.get_calls == []
    # And should have called generate_content with our default model.
    kwargs = client.aio.models.last_kwargs
    assert kwargs is not None
    assert kwargs["model"] == DEFAULT_VIDEO_MODEL
    contents = kwargs["contents"]
    # contents = [video_part, prompt]
    assert len(contents) == 2
    video_part = contents[0]
    assert getattr(video_part.file_data, "file_uri", "").endswith("9hE5-98ZeCg")
    # video_metadata propagated through.
    md = getattr(video_part, "video_metadata", None)
    assert md is not None
    assert md.fps == 0.5
    assert md.start_offset == "30s"
    assert md.end_offset == "90s"
    # media_resolution mapped through the config.
    config = kwargs["config"]
    assert config.media_resolution.value == "MEDIA_RESOLUTION_LOW"


@pytest.mark.asyncio
async def test_local_file_upload_polls_until_active(monkeypatch, tmp_path):
    # Need TWO get() calls before ACTIVE so we exercise the polling loop.
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    factory, captured = _client_factory(response_text="local file summary", ready_after=2)
    tool = AnalyzeVideoTool(client_factory=factory)

    video_path = tmp_path / "clip.mp4"
    video_path.write_bytes(b"\x00" * 64)  # non-empty placeholder

    # Speed up the polling loop so the test isn't gated on real seconds.
    real_sleep = asyncio.sleep

    async def _instant_sleep(_seconds: float) -> None:
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", _instant_sleep)

    result = await tool.execute(
        "call-2",
        {"prompt": "transcribe", "video_path": str(video_path)},
    )

    assert not result.is_error
    assert isinstance(result.content, list)
    text = result.content[0].text
    assert "local file summary" in text
    assert "clip.mp4" in text  # source label

    client = captured["client"]
    assert client.files.uploaded_paths == [str(video_path)]
    # Exactly one get() per ready_after-1 (initial state was PROCESSING) before
    # the file flips to ACTIVE.
    assert len(client.files.get_calls) >= 1
    kwargs = client.aio.models.last_kwargs
    contents = kwargs["contents"]
    video_part = contents[0]
    assert "files/abc123" in getattr(video_part.file_data, "file_uri", "")


@pytest.mark.asyncio
async def test_missing_api_key_returns_friendly_error(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    tool = AnalyzeVideoTool()

    result = await tool.execute(
        "call-3",
        {
            "prompt": "summarize",
            "youtube_url": "https://www.youtube.com/watch?v=abc",
        },
    )

    assert result.is_error
    assert "GEMINI_API_KEY" in str(result.content)


@pytest.mark.asyncio
async def test_rejects_both_inputs(monkeypatch, tmp_path):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    tool = AnalyzeVideoTool()
    video_path = tmp_path / "x.mp4"
    video_path.write_bytes(b"\x00")

    result = await tool.execute(
        "call-4",
        {
            "prompt": "summarize",
            "video_path": str(video_path),
            "youtube_url": "https://www.youtube.com/watch?v=abc",
        },
    )

    assert result.is_error
    assert "exactly one" in str(result.content)


@pytest.mark.asyncio
async def test_response_schema_sets_json_mime(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    factory, captured = _client_factory(response_text='{"ok": true}')
    tool = AnalyzeVideoTool(client_factory=factory)

    schema = {"type": "object", "properties": {"ok": {"type": "boolean"}}}
    result = await tool.execute(
        "call-5",
        {
            "prompt": "produce JSON",
            "youtube_url": "https://www.youtube.com/watch?v=abc",
            "response_schema": schema,
        },
    )

    assert not result.is_error
    config = captured["client"].aio.models.last_kwargs["config"]
    assert config.response_mime_type == "application/json"
    assert config.response_schema == schema


def test_tool_definition_shape():
    tool = AnalyzeVideoTool()
    d = tool.definition
    assert d.name == "analyze_video"
    assert d.parameters["required"] == ["prompt"]
    props = d.parameters["properties"]
    assert "video_path" in props
    assert "youtube_url" in props
    assert "fps" in props
    assert "media_resolution" in props
    assert "response_schema" in props
