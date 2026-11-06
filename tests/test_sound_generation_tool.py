"""ElevenLabs sound effects: argument validation, the request payload, where the file lands, and the bridge-side scrape that puts the file into the artifact manifest."""

from __future__ import annotations

import inspect
import os
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from bridge.tools.sound_generation_tool import GenerateSoundEffectTool


def _client(status: int = 200, content: bytes = b"") -> AsyncMock:
    response = MagicMock(spec=httpx.Response)
    response.status_code = status
    response.content = content
    client = AsyncMock(spec=httpx.AsyncClient)
    client.post.return_value = response
    return client


@pytest.mark.parametrize(
    ("arguments", "complaint"),
    [
        ({}, "prompt"),
        ({"prompt": "thunder", "duration_seconds": 45.0}, "between 0.5 and 30.0"),
    ],
)
async def test_bad_arguments_are_refused(arguments, complaint):
    with patch.dict(os.environ, {"ELEVENLABS_API_KEY": "test_key"}):
        result = await GenerateSoundEffectTool().execute(call_id="c", arguments=arguments)
    assert result.is_error
    assert complaint in result.content


async def test_missing_api_key_is_a_clear_error():
    with patch.dict(os.environ, {}, clear=True), patch("pathlib.Path.is_file", return_value=False):
        result = await GenerateSoundEffectTool().execute(
            call_id="c", arguments={"prompt": "laser blast"}
        )
    assert result.is_error
    assert "ELEVENLABS_API_KEY" in result.content


async def test_generation_sends_the_payload_saves_the_file_and_leaves_recording_to_the_bridge(tmp_path):
    audio = b"\xff\xfb\x90\x44" + b"\x00" * 1024  # Mock MP3 bytes
    client = _client(content=audio)
    artifact_store = AsyncMock()
    tool = GenerateSoundEffectTool(
        project_output_dir=tmp_path, artifact_store=artifact_store, http_client=client
    )

    with patch.dict(os.environ, {"ELEVENLABS_API_KEY": "test_xi_key"}):
        result = await tool.execute(
            call_id="c",
            arguments={
                "prompt": "Deep cinematic impact hit",
                "duration_seconds": 3.5,
                "prompt_influence": 0.4,
                "loop": False,
                "output_format": "mp3_44100_128",
            },
        )

    assert not result.is_error
    for text in ("Deep cinematic impact hit", "3.5s", "mp3_44100_128"):
        assert text in result.content

    kwargs = client.post.call_args.kwargs
    assert kwargs["json"]["text"] == "Deep cinematic impact hit"
    assert kwargs["json"]["duration_seconds"] == 3.5
    assert kwargs["json"]["prompt_influence"] == 0.4
    assert kwargs["json"]["loop"] is False
    assert kwargs["headers"]["xi-api-key"] == "test_xi_key"
    assert kwargs["params"]["output_format"] == "mp3_44100_128"

    audio_files = list((tmp_path / "audio").glob("*.mp3"))
    assert len(audio_files) == 1
    assert audio_files[0].read_bytes() == audio

    # The tool must NOT record the artifact itself. It used to try, with
    # `await` on a synchronous method and two required keyword arguments
    # missing — a TypeError swallowed by a bare except, so generated sound
    # effects never reached manifest.jsonl. An AsyncMock accepts any
    # signature, which is exactly why the old assertion passed while the real
    # call could not work. The bridge writes the row now; see
    # _handle_tool_result in freyja_bridge.
    artifact_store.record_file.assert_not_called()


async def test_save_path_is_honoured(tmp_path):
    custom_file = tmp_path / "custom_sfx" / "laser.wav"
    audio = b"RIFF" + b"\x00" * 512  # Mock WAV
    tool = GenerateSoundEffectTool(project_output_dir=tmp_path, http_client=_client(content=audio))

    with patch.dict(os.environ, {"XI_API_KEY": "test_xi_key_2"}):
        result = await tool.execute(
            call_id="c",
            arguments={
                "prompt": "Sci-fi laser shot",
                "output_format": "pcm_44100",
                "save_path": str(custom_file),
            },
        )

    assert not result.is_error
    assert custom_file.read_bytes() == audio


@pytest.mark.parametrize(
    ("status", "text"), [(401, "401 Unauthorized"), (402, "402 Payment Required")]
)
async def test_api_errors_are_reported(status, text):
    tool = GenerateSoundEffectTool(http_client=_client(status=status))
    with patch.dict(os.environ, {"ELEVENLABS_API_KEY": "key"}):
        result = await tool.execute(call_id="c", arguments={"prompt": "explosion"})
    assert result.is_error
    assert text in result.content


# ── the scrape that puts generated media into the artifact manifest ──────
#
# generate_image and generate_sound_effect only report their output path in
# the human-readable result text, so the bridge's tool-result hook parses it
# back out to write the manifest row. That join is prose-to-regex and breaks
# silently: an artifact that never reaches manifest.jsonl never appears in
# the session library or the artifact browser, and nothing errors.


def test_media_artifact_path_is_scraped_from_both_generators():
    from bridge.freyja_bridge import MEDIA_ARTIFACT_PATH_RE, MEDIA_ARTIFACT_TOOLS
    from bridge.tools import sound_generation_tool

    assert {"generate_image", "generate_sound_effect"} <= MEDIA_ARTIFACT_TOOLS
    assert MEDIA_ARTIFACT_PATH_RE.search("File saved to `/tmp/img.png`").group(1) == "/tmp/img.png"
    # What generate_sound_effect actually emits — an indented bullet.
    sfx = MEDIA_ARTIFACT_PATH_RE.search("  · File: `/tmp/audio/sfx_boom.mp3`")
    assert sfx.group(1) == "/tmp/audio/sfx_boom.mp3"
    assert MEDIA_ARTIFACT_PATH_RE.search("Error: out of credits") is None
    assert MEDIA_ARTIFACT_PATH_RE.search("") is None
    # Pins the tool's prose to the regex. If the success text is reworded
    # without updating MEDIA_ARTIFACT_PATH_RE, this fails here rather than
    # silently dropping every future sound effect from the manifest.
    assert 'f"  · File: `{target_path}`\\n"' in inspect.getsource(sound_generation_tool)


def test_the_real_store_accepts_the_bridge_call(tmp_path):
    # The tool used to record the artifact itself with `await` on a
    # synchronous method and two required keyword arguments missing. The
    # test mock was an AsyncMock, which accepts any signature, so the
    # broken call looked fine. This drives the REAL store.
    from bridge.artifact_store import SessionArtifactStore

    store = SessionArtifactStore(session_id="s1", project_dir=tmp_path)
    store.ensure()
    target = tmp_path / "audio" / "sfx_boom.mp3"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"audio")

    store.record_file(
        target,
        creator_id="s1",
        creator_label="Main agent",
        operation="create",
        source="generate_sound_effect",
        tool_call_id="call_1",
        metadata={"tool": "generate_sound_effect"},
    )

    rows = store.list()
    assert len(rows) == 1
    assert rows[0]["source"] == "generate_sound_effect"
    assert rows[0]["fileType"] == "mp3"
    assert rows[0]["exists"] is True
