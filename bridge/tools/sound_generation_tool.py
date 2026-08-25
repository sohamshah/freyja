"""ElevenLabs sound effect generation tool."""

from __future__ import annotations

import logging
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx

from bridge.tools.base import ToolDefinition, ToolResult, ToolTier

logger = logging.getLogger(__name__)

ELEVENLABS_SOUND_GEN_URL = "https://api.elevenlabs.io/v1/sound-generation"
DEFAULT_MODEL_ID = "eleven_text_to_sound_v2"
DEFAULT_OUTPUT_FORMAT = "mp3_44100_128"
DEFAULT_AUDIO_DIR = Path.home() / "Music" / "Freyja"

SUPPORTED_OUTPUT_FORMATS = {
    "mp3_22050_32",
    "mp3_24000_48",
    "mp3_44100_32",
    "mp3_44100_64",
    "mp3_44100_96",
    "mp3_44100_128",
    "mp3_44100_192",
    "pcm_8000",
    "pcm_16000",
    "pcm_22050",
    "pcm_24000",
    "pcm_32000",
    "pcm_44100",
    "pcm_48000",
    "ulaw_8000",
    "alaw_8000",
    "opus_48000_32",
    "opus_48000_64",
    "opus_48000_96",
    "opus_48000_128",
    "opus_48000_192",
}


def _resolve_api_key() -> str | None:
    """Resolve ElevenLabs API key from env or ~/.freyja/.env."""
    key = os.environ.get("ELEVENLABS_API_KEY") or os.environ.get("XI_API_KEY")
    if key:
        return key.strip()

    # Fallback: check ~/.freyja/.env or .env in workspace
    for env_path in [Path.home() / ".freyja" / ".env", Path(".env")]:
        if env_path.is_file():
            try:
                for line in env_path.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    k, v = line.split("=", 1)
                    k = k.strip()
                    v = v.strip().strip("'\"")
                    if k in ("ELEVENLABS_API_KEY", "XI_API_KEY") and v:
                        return v
            except Exception:
                pass
    return None


def _format_to_extension(output_format: str) -> str:
    """Map ElevenLabs output_format to file extension."""
    if output_format.startswith("mp3"):
        return ".mp3"
    if output_format.startswith("pcm") or output_format.startswith("ulaw") or output_format.startswith("alaw"):
        return ".wav"
    if output_format.startswith("opus"):
        return ".opus"
    return ".mp3"


class GenerateSoundEffectTool:
    """Generate high-quality sound effects from text descriptions using ElevenLabs Sound Generation API."""

    def __init__(
        self,
        project_output_dir: Path | str | None = None,
        artifact_store: Any | None = None,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._project_output_dir = Path(project_output_dir).expanduser().resolve() if project_output_dir else None
        self._artifact_store = artifact_store
        self._http_client = http_client

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="generate_sound_effect",
            summary="Generate sound effects, ambient audio, or foley from text with ElevenLabs",
            tier=ToolTier.HOT,
            description="""Generate production-ready sound effects, foley, atmospheric audio, instrument hits, ambient soundscapes, sci-fi sounds, and game audio from descriptive text prompts using the ElevenLabs Sound Generation API.

Supports duration control (0.5 to 30.0s), prompt influence (prompt adherence), seamless looping audio, and multiple audio formats (mp3, pcm, opus).

Requires the ELEVENLABS_API_KEY environment variable or configured in ~/.freyja/.env.""",
            parameters={
                "type": "object",
                "properties": {
                    "prompt": {
                        "type": "string",
                        "description": "Descriptive text prompt for the sound effect (e.g. 'Cinematic braam hit with deep sub-bass and metallic reverb', 'Footsteps walking in crisp autumn leaves', 'Retro 8-bit arcade laser blast', 'Thunderstorm with heavy rain against a window pane').",
                    },
                    "duration_seconds": {
                        "type": "number",
                        "description": "Duration of the sound in seconds (0.5 to 30.0). If omitted, optimal duration is inferred by the model from the prompt.",
                        "minimum": 0.5,
                        "maximum": 30.0,
                    },
                    "prompt_influence": {
                        "type": "number",
                        "description": "Prompt adherence between 0.0 and 1.0 (default 0.3). Higher values follow the prompt more strictly with less variability.",
                        "minimum": 0.0,
                        "maximum": 1.0,
                        "default": 0.3,
                    },
                    "loop": {
                        "type": "boolean",
                        "description": "Whether to create a sound effect that loops seamlessly. Default false.",
                        "default": False,
                    },
                    "output_format": {
                        "type": "string",
                        "description": "Audio output format (default 'mp3_44100_128'). Other options include 'mp3_44100_192', 'mp3_22050_32', 'mp3_44100_64', 'pcm_44100', 'opus_48000_128'.",
                        "default": DEFAULT_OUTPUT_FORMAT,
                    },
                    "save_path": {
                        "type": "string",
                        "description": "Custom local file path or filename to save the generated audio. If omitted, saves under the session project audio directory.",
                    },
                    "model_id": {
                        "type": "string",
                        "description": f"ElevenLabs sound generation model ID (default '{DEFAULT_MODEL_ID}').",
                        "default": DEFAULT_MODEL_ID,
                    },
                },
                "required": ["prompt"],
            },
        )

    async def execute(self, call_id: str, arguments: dict[str, Any]) -> ToolResult:
        prompt = str(arguments.get("prompt") or "").strip()
        if not prompt:
            return ToolResult(
                call_id=call_id,
                content="Error: 'prompt' parameter is required for sound effect generation.",
                is_error=True,
            )

        api_key = _resolve_api_key()
        if not api_key:
            return ToolResult(
                call_id=call_id,
                content=(
                    "Error: ELEVENLABS_API_KEY environment variable is not set.\n\n"
                    "To generate sound effects with ElevenLabs, set your API key:\n"
                    "  · In ~/.freyja/.env: ELEVENLABS_API_KEY=your_key_here\n"
                    "  · Or export ELEVENLABS_API_KEY in your environment."
                ),
                is_error=True,
            )

        duration_seconds = arguments.get("duration_seconds")
        if duration_seconds is not None:
            try:
                duration_seconds = float(duration_seconds)
                if not (0.5 <= duration_seconds <= 30.0):
                    return ToolResult(
                        call_id=call_id,
                        content=f"Error: duration_seconds must be between 0.5 and 30.0 seconds (got {duration_seconds}).",
                        is_error=True,
                    )
            except (ValueError, TypeError):
                return ToolResult(
                    call_id=call_id,
                    content=f"Error: Invalid duration_seconds value: {duration_seconds}",
                    is_error=True,
                )

        prompt_influence = arguments.get("prompt_influence", 0.3)
        try:
            prompt_influence = float(prompt_influence)
            prompt_influence = max(0.0, min(1.0, prompt_influence))
        except (ValueError, TypeError):
            prompt_influence = 0.3

        loop = bool(arguments.get("loop", False))
        output_format = str(arguments.get("output_format") or DEFAULT_OUTPUT_FORMAT).strip()
        if output_format not in SUPPORTED_OUTPUT_FORMATS:
            output_format = DEFAULT_OUTPUT_FORMAT

        model_id = str(arguments.get("model_id") or DEFAULT_MODEL_ID).strip()

        # Build request payload
        payload: dict[str, Any] = {
            "text": prompt,
            "prompt_influence": prompt_influence,
            "loop": loop,
        }
        if duration_seconds is not None:
            payload["duration_seconds"] = duration_seconds
        if model_id:
            payload["model_id"] = model_id

        headers = {
            "xi-api-key": api_key,
            "Content-Type": "application/json",
            "Accept": "audio/*",
        }
        params = {
            "output_format": output_format,
        }

        try:
            if self._http_client:
                response = await self._http_client.post(
                    ELEVENLABS_SOUND_GEN_URL,
                    json=payload,
                    headers=headers,
                    params=params,
                    timeout=60.0,
                )
            else:
                async with httpx.AsyncClient() as client:
                    response = await client.post(
                        ELEVENLABS_SOUND_GEN_URL,
                        json=payload,
                        headers=headers,
                        params=params,
                        timeout=60.0,
                    )

            if response.status_code == 401:
                return ToolResult(
                    call_id=call_id,
                    content="Error: ElevenLabs API returned 401 Unauthorized. Please verify your ELEVENLABS_API_KEY.",
                    is_error=True,
                )
            if response.status_code == 402:
                return ToolResult(
                    call_id=call_id,
                    content="Error: ElevenLabs API returned 402 Payment Required. Your account has insufficient credits for sound generation.",
                    is_error=True,
                )
            if response.status_code == 422:
                detail = response.text
                try:
                    data = response.json()
                    detail = data.get("detail", {}).get("message") or data.get("message") or response.text
                except Exception:
                    pass
                return ToolResult(
                    call_id=call_id,
                    content=f"Error: ElevenLabs validation error (422): {detail}",
                    is_error=True,
                )
            if response.status_code != 200:
                return ToolResult(
                    call_id=call_id,
                    content=f"Error: ElevenLabs API returned HTTP {response.status_code}: {response.text[:300]}",
                    is_error=True,
                )

            audio_bytes = response.content
            if not audio_bytes:
                return ToolResult(
                    call_id=call_id,
                    content="Error: ElevenLabs API returned empty audio response.",
                    is_error=True,
                )

            # Determine output file path
            ext = _format_to_extension(output_format)
            save_path_arg = arguments.get("save_path")

            if save_path_arg:
                target_path = Path(save_path_arg).expanduser()
                if not target_path.is_absolute():
                    base = self._project_output_dir or Path.cwd()
                    target_path = base / target_path
                if target_path.suffix == "":
                    target_path = target_path.with_suffix(ext)
            else:
                timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                # Slugify prompt for filename
                slug = re.sub(r"[^\w\-_]+", "_", prompt[:30]).strip("_").lower() or "sfx"
                filename = f"sfx_{slug}_{timestamp}{ext}"
                if self._project_output_dir:
                    out_dir = self._project_output_dir / "audio"
                else:
                    out_dir = DEFAULT_AUDIO_DIR
                target_path = out_dir / filename

            target_path.parent.mkdir(parents=True, exist_ok=True)
            target_path.write_bytes(audio_bytes)

            size_kb = len(audio_bytes) / 1024.0

            # The artifact manifest row is written by the bridge's tool-result
            # hook, not here — same as generate_image. That hook knows which
            # session (or sub-agent) is emitting, which is what `creatorId`
            # means; a tool has no way to know it and would have to invent one.

            loop_str = " (seamless loop)" if loop else ""
            dur_str = f"{duration_seconds}s" if duration_seconds is not None else "auto"

            return ToolResult(
                call_id=call_id,
                content=(
                    f"Generated sound effect successfully!\n\n"
                    f"  · File: `{target_path}`\n"
                    f"  · Size: {size_kb:.1f} KB ({len(audio_bytes):,} bytes)\n"
                    f"  · Format: {output_format}{loop_str}\n"
                    f"  · Duration: {dur_str}\n"
                    f"  · Prompt: \"{prompt}\"\n\n"
                    f"Playback on macOS:\n"
                    f"  `afplay \"{target_path}\"`"
                ),
                is_error=False,
            )

        except httpx.TimeoutException:
            return ToolResult(
                call_id=call_id,
                content="Error: ElevenLabs API request timed out after 60 seconds.",
                is_error=True,
            )
        except Exception as exc:
            logger.exception("Error executing generate_sound_effect: %s", exc)
            return ToolResult(
                call_id=call_id,
                content=f"Error executing sound generation: {exc}",
                is_error=True,
            )
