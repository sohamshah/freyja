"""Video analysis tool backed by Google Gemini.

Accepts either a local video file path or a public YouTube URL and returns the
model's analysis as text (or structured JSON when ``response_schema`` is given).

Local files go through Gemini's Files API: the tool uploads the file, polls
``client.files.get`` until ``state == ACTIVE``, then references the file in
``generate_content``. YouTube URLs are passed directly via ``Part.from_uri``
(public videos only — no download needed).

Default model is ``gemini-3-flash-preview``: per the official video-understanding
docs it's the canonical choice for video, with ``gemini-3.1-pro-preview`` as the
"best quality" option. Both can be overridden per-call.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from pathlib import Path
from typing import Any

from bridge.tools.base import TextBlock, ToolDefinition, ToolResult, ToolTier

logger = logging.getLogger(__name__)


DEFAULT_VIDEO_MODEL = "gemini-3-flash-preview"

# Acceptable model ids the user can pass — anything not on this list is still
# accepted but emits a heads-up so the agent gets feedback if it picks a stale
# id (e.g. the now-shut-down `gemini-3-pro-preview`).
RECOMMENDED_MODELS = {
    "gemini-3-flash-preview",          # default per docs
    "gemini-3.1-flash",
    "gemini-3.1-flash-lite",
    "gemini-3.1-flash-lite-preview",
    "gemini-3.1-pro-preview",          # highest quality
    "gemini-2.5-flash",                 # cheaper fallback
    "gemini-2.5-pro",
}

MEDIA_RESOLUTION_VALUES = {
    "default": "MEDIA_RESOLUTION_UNSPECIFIED",
    "low": "MEDIA_RESOLUTION_LOW",
    "medium": "MEDIA_RESOLUTION_MEDIUM",
    "high": "MEDIA_RESOLUTION_HIGH",
}

# Two recognized video MIME types families. Anything else flows through with
# the OS-detected mime so we don't gate on a tight allowlist.
VIDEO_MIME_BY_EXT = {
    ".mp4": "video/mp4",
    ".mov": "video/quicktime",
    ".avi": "video/x-msvideo",
    ".mkv": "video/x-matroska",
    ".webm": "video/webm",
    ".flv": "video/x-flv",
    ".mpeg": "video/mpeg",
    ".mpg": "video/mpeg",
    ".wmv": "video/x-ms-wmv",
    ".3gpp": "video/3gpp",
}

# Files API hard cap from the Gemini docs is 2 GB per file. We block earlier
# than that with a friendly message instead of letting the upload fail late.
FILES_API_MAX_BYTES = 2 * 1024 * 1024 * 1024

YOUTUBE_HOST_PATTERN = re.compile(
    r"^(https?://)?(www\.|m\.)?(youtube\.com|youtu\.be)/", re.IGNORECASE
)


def _looks_like_youtube_url(value: str) -> bool:
    return bool(YOUTUBE_HOST_PATTERN.match(value.strip()))


def _mime_for_path(path: Path) -> str:
    return VIDEO_MIME_BY_EXT.get(path.suffix.lower(), "video/mp4")


def _format_offset(seconds: float | int | None) -> str | None:
    """Gemini's start_offset / end_offset want a Duration string like '125s'."""
    if seconds is None:
        return None
    try:
        value = float(seconds)
    except (TypeError, ValueError):
        return None
    if value < 0:
        return None
    return f"{value:g}s"


class AnalyzeVideoTool:
    """Analyze a local video file or YouTube URL with Gemini."""

    def __init__(self, client_factory: Any | None = None) -> None:
        # Tests inject a fake client_factory(api_key=...) → object with the
        # genai.Client surface we touch (.files.upload, .files.get,
        # .aio.models.generate_content). In production this stays None and we
        # build a real ``google.genai.Client`` per call.
        self._client_factory = client_factory

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="analyze_video",
            summary="Analyze a video file or YouTube URL with Gemini",
            tier=ToolTier.WARM,
            description="""Analyze a video using Google Gemini's video understanding API.

Accepts EITHER a local video file path OR a public YouTube URL — pick one.
Returns the model's analysis as text (or JSON when response_schema is given).

When to reach for this:
- The user shares a screen recording / clip / lecture / demo and wants a
  summary, transcript, action list, or specific moment lookup.
- The user pastes a YouTube URL and asks something about its contents.

Guidelines:
- Write a focused prompt: "summarize", "list every UI click with timestamp",
  "extract the spoken instructions as JSON". Vague prompts get vague answers.
- For long videos (>10 min) prefer media_resolution=low and consider clipping
  via start_seconds/end_seconds — token cost grows ~linearly with duration.
- Audio is processed automatically alongside video; no separate flag needed.
- Use timestamps in MM:SS format when asking about specific moments so the
  model knows you mean clock-time.
- Output is text by default; pass response_schema (JSON Schema) to get a
  parseable JSON object back.""",
            parameters={
                "type": "object",
                "properties": {
                    "prompt": {
                        "type": "string",
                        "description": "What the model should do with the video (summarize, transcribe, extract steps, etc.).",
                    },
                    "video_path": {
                        "type": "string",
                        "description": (
                            "Local path to a video file. Mutually exclusive with youtube_url. "
                            "Supports ~ expansion. Goes through Gemini's Files API "
                            "(upload → poll for ACTIVE → reference). Max 2 GB per file."
                        ),
                    },
                    "youtube_url": {
                        "type": "string",
                        "description": (
                            "Public YouTube URL. Mutually exclusive with video_path. "
                            "Passed directly to Gemini — no download. Public videos only "
                            "(no private/unlisted)."
                        ),
                    },
                    "model": {
                        "type": "string",
                        "description": (
                            f"Gemini model id. Defaults to {DEFAULT_VIDEO_MODEL}. "
                            "Use gemini-3.1-pro-preview for best quality on tricky video."
                        ),
                        "default": DEFAULT_VIDEO_MODEL,
                    },
                    "fps": {
                        "type": "number",
                        "description": (
                            "Frames-per-second to sample. Default 1. Drop below 1 for long "
                            "static videos (e.g. lectures); raise for fine temporal detail."
                        ),
                    },
                    "start_seconds": {
                        "type": "number",
                        "description": "Optional clip start, in seconds.",
                    },
                    "end_seconds": {
                        "type": "number",
                        "description": "Optional clip end, in seconds.",
                    },
                    "media_resolution": {
                        "type": "string",
                        "enum": sorted(MEDIA_RESOLUTION_VALUES.keys()),
                        "description": (
                            "Per-frame token budget. 'low' (~100 tokens/sec at 1fps) is the "
                            "right default for long videos. 'high' (~280 tokens/frame) only "
                            "when fine detail matters."
                        ),
                        "default": "default",
                    },
                    "max_output_tokens": {
                        "type": "integer",
                        "minimum": 1,
                        "description": "Cap on the response length. Defaults to 8192.",
                    },
                    "temperature": {
                        "type": "number",
                        "minimum": 0,
                        "maximum": 2,
                        "description": "Generation temperature. Defaults to 0.4.",
                    },
                    "response_schema": {
                        "type": "object",
                        "description": (
                            "Optional JSON Schema. When set, the model returns "
                            "application/json conforming to it; the tool result still "
                            "ships text but it is guaranteed-parseable JSON."
                        ),
                    },
                    "system_prompt": {
                        "type": "string",
                        "description": "Optional system instruction prepended to the request.",
                    },
                },
                "required": ["prompt"],
            },
        )

    async def execute(self, call_id: str, arguments: dict[str, Any]) -> ToolResult:
        prompt = str(arguments.get("prompt", "")).strip()
        if not prompt:
            return self._error(call_id, "prompt is required")

        video_path = str(arguments.get("video_path") or "").strip()
        youtube_url = str(arguments.get("youtube_url") or "").strip()
        if bool(video_path) == bool(youtube_url):
            return self._error(
                call_id,
                "Provide exactly one of video_path or youtube_url.",
            )

        api_key = os.environ.get("GEMINI_API_KEY", "").strip()
        if not api_key:
            return self._error(
                call_id,
                "GEMINI_API_KEY is not set. Add it to your environment to enable video analysis.",
            )

        model = (str(arguments.get("model") or "").strip() or DEFAULT_VIDEO_MODEL)
        if model not in RECOMMENDED_MODELS:
            logger.info(
                "analyze_video using non-recommended model %r (recommended: %s)",
                model,
                ", ".join(sorted(RECOMMENDED_MODELS)),
            )

        try:
            client = self._build_client(api_key)
        except Exception as exc:  # noqa: BLE001
            return self._error(call_id, f"Failed to construct Gemini client: {exc}")

        from google.genai import types as gtypes

        try:
            video_part, source_label = await self._build_video_part(
                client,
                gtypes,
                video_path=video_path,
                youtube_url=youtube_url,
                fps=arguments.get("fps"),
                start_seconds=arguments.get("start_seconds"),
                end_seconds=arguments.get("end_seconds"),
            )
        except _UserVisibleError as exc:
            return self._error(call_id, str(exc))
        except Exception as exc:  # noqa: BLE001
            logger.warning("analyze_video failed to prepare video: %s", exc, exc_info=True)
            return self._error(call_id, f"Could not prepare video for Gemini: {exc}")

        config = self._build_generate_config(gtypes, arguments)

        try:
            response = await client.aio.models.generate_content(
                model=model,
                contents=[video_part, prompt],
                config=config,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("analyze_video generate_content failed: %s", exc, exc_info=True)
            return self._error(call_id, f"Gemini generate_content failed: {exc}")

        text = self._extract_text(response)
        if not text:
            return self._error(
                call_id,
                "Gemini returned no text. The video may have been blocked or empty.",
            )

        header = (
            f"[Video analysis · {source_label} · {model}]\n"
            if source_label
            else f"[Video analysis · {model}]\n"
        )
        return ToolResult(
            call_id=call_id,
            content=[TextBlock(text=header + text)],
        )

    # ── helpers ──────────────────────────────────────────────────────

    def _build_client(self, api_key: str) -> Any:
        if self._client_factory is not None:
            return self._client_factory(api_key=api_key)
        from google import genai
        return genai.Client(api_key=api_key)

    async def _build_video_part(
        self,
        client: Any,
        gtypes: Any,
        *,
        video_path: str,
        youtube_url: str,
        fps: Any,
        start_seconds: Any,
        end_seconds: Any,
    ) -> tuple[Any, str]:
        video_metadata = self._build_video_metadata(gtypes, fps, start_seconds, end_seconds)

        if youtube_url:
            if not _looks_like_youtube_url(youtube_url):
                raise _UserVisibleError(
                    "youtube_url does not look like a YouTube link. Pass a full https URL."
                )
            file_data = gtypes.FileData(file_uri=youtube_url.strip())
            part = gtypes.Part(file_data=file_data, video_metadata=video_metadata)
            return part, f"youtube · {youtube_url.strip()}"

        path = Path(video_path).expanduser()
        if not path.exists():
            raise _UserVisibleError(f"video_path does not exist: {path}")
        if not path.is_file():
            raise _UserVisibleError(f"video_path is not a file: {path}")

        size = path.stat().st_size
        if size == 0:
            raise _UserVisibleError(f"video_path is empty: {path}")
        if size > FILES_API_MAX_BYTES:
            raise _UserVisibleError(
                f"video file is {size / (1024 ** 3):.2f} GB; Gemini Files API caps at 2 GB."
            )

        mime = _mime_for_path(path)
        upload = await asyncio.to_thread(client.files.upload, file=str(path))
        file_obj = await self._wait_for_active(client, upload, label=path.name)
        # Pin the original mime type if the upload reply omits it.
        file_uri = getattr(file_obj, "uri", None) or getattr(upload, "uri", None)
        file_mime = getattr(file_obj, "mime_type", None) or mime
        if not file_uri:
            raise _UserVisibleError(
                "Gemini Files API upload returned no URI. Try again or use a YouTube URL."
            )

        file_data = gtypes.FileData(file_uri=file_uri, mime_type=file_mime)
        part = gtypes.Part(file_data=file_data, video_metadata=video_metadata)
        return part, f"file · {path.name}"

    async def _wait_for_active(
        self,
        client: Any,
        file_obj: Any,
        *,
        label: str,
        timeout_seconds: float = 240.0,
        poll_seconds: float = 4.0,
    ) -> Any:
        from google.genai import types as gtypes

        name = getattr(file_obj, "name", None)
        if not name:
            raise _UserVisibleError("Gemini upload returned no file name; cannot poll for ACTIVE.")

        deadline = asyncio.get_event_loop().time() + timeout_seconds
        current = file_obj
        while True:
            state = getattr(current, "state", None)
            state_name = getattr(state, "name", state)
            if state_name == gtypes.FileState.ACTIVE or state_name == "ACTIVE":
                return current
            if state_name == gtypes.FileState.FAILED or state_name == "FAILED":
                raise _UserVisibleError(
                    f"Gemini reported processing FAILED for {label!r}. Try a smaller/different file."
                )
            if asyncio.get_event_loop().time() > deadline:
                raise _UserVisibleError(
                    f"Gemini did not finish processing {label!r} within {int(timeout_seconds)}s."
                )
            await asyncio.sleep(poll_seconds)
            current = await asyncio.to_thread(client.files.get, name=name)

    def _build_video_metadata(
        self,
        gtypes: Any,
        fps: Any,
        start_seconds: Any,
        end_seconds: Any,
    ) -> Any | None:
        kwargs: dict[str, Any] = {}
        if fps not in (None, ""):
            try:
                kwargs["fps"] = float(fps)
            except (TypeError, ValueError):
                pass
        start = _format_offset(start_seconds)
        end = _format_offset(end_seconds)
        if start is not None:
            kwargs["start_offset"] = start
        if end is not None:
            kwargs["end_offset"] = end
        if not kwargs:
            return None
        return gtypes.VideoMetadata(**kwargs)

    def _build_generate_config(self, gtypes: Any, arguments: dict[str, Any]) -> Any:
        kwargs: dict[str, Any] = {
            "max_output_tokens": int(arguments.get("max_output_tokens") or 8192),
            "temperature": float(arguments.get("temperature") or 0.4),
        }

        media_resolution = MEDIA_RESOLUTION_VALUES.get(
            str(arguments.get("media_resolution") or "default").lower()
        )
        if media_resolution and media_resolution != "MEDIA_RESOLUTION_UNSPECIFIED":
            kwargs["media_resolution"] = getattr(gtypes.MediaResolution, media_resolution)

        system_prompt = str(arguments.get("system_prompt") or "").strip()
        if system_prompt:
            kwargs["system_instruction"] = system_prompt

        response_schema = arguments.get("response_schema")
        if isinstance(response_schema, dict) and response_schema:
            kwargs["response_mime_type"] = "application/json"
            kwargs["response_schema"] = response_schema

        return gtypes.GenerateContentConfig(**kwargs)

    @staticmethod
    def _extract_text(response: Any) -> str:
        # Modern SDK exposes response.text directly. Fall back to walking
        # candidates so unusual shapes still produce something useful.
        text = getattr(response, "text", None)
        if isinstance(text, str) and text.strip():
            return text.strip()

        chunks: list[str] = []
        for candidate in getattr(response, "candidates", None) or []:
            content = getattr(candidate, "content", None)
            for part in getattr(content, "parts", None) or []:
                value = getattr(part, "text", None)
                if isinstance(value, str) and value:
                    chunks.append(value)
        joined = "\n".join(chunks).strip()
        if joined:
            return joined

        # Last-ditch: stringify the whole response so the agent can see
        # what came back instead of a silent empty result.
        try:
            return json.dumps(response.to_dict(), default=str)
        except Exception:  # noqa: BLE001
            return ""

    @staticmethod
    def _error(call_id: str, message: str) -> ToolResult:
        return ToolResult(
            call_id=call_id,
            content=f"Error: {message}",
            is_error=True,
        )


class _UserVisibleError(RuntimeError):
    """Raised inside the tool when we want the message surfaced verbatim."""
