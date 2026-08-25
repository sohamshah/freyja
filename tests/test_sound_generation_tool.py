"""Tests for ElevenLabs sound effect generation tool."""

import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import httpx

from bridge.tools.registry import build_desktop_registry
from bridge.tools.sound_generation_tool import (
    DEFAULT_MODEL_ID,
    DEFAULT_OUTPUT_FORMAT,
    GenerateSoundEffectTool,
    _format_to_extension,
    _resolve_api_key,
)


class TestSoundGenerationTool(unittest.IsolatedAsyncioTestCase):
    async def test_sound_effect_tool_definition(self):
        tool = GenerateSoundEffectTool()
        definition = tool.definition
        self.assertEqual(definition.name, "generate_sound_effect")
        self.assertEqual(definition.tier.value, "hot")
        self.assertIn("prompt", definition.parameters["required"])
        props = definition.parameters["properties"]
        self.assertIn("duration_seconds", props)
        self.assertIn("prompt_influence", props)
        self.assertIn("loop", props)
        self.assertIn("output_format", props)
        self.assertIn("save_path", props)
        self.assertIn("model_id", props)

    async def test_sound_effect_missing_prompt(self):
        tool = GenerateSoundEffectTool()
        result = await tool.execute(call_id="call_1", arguments={})
        self.assertTrue(result.is_error)
        self.assertIn("prompt", result.content)

    async def test_sound_effect_missing_api_key(self):
        tool = GenerateSoundEffectTool()
        with patch.dict(os.environ, {}, clear=True):
            with patch("pathlib.Path.is_file", return_value=False):
                result = await tool.execute(call_id="call_2", arguments={"prompt": "laser blast"})
                self.assertTrue(result.is_error)
                self.assertIn("ELEVENLABS_API_KEY", result.content)

    async def test_sound_effect_invalid_duration(self):
        tool = GenerateSoundEffectTool()
        with patch.dict(os.environ, {"ELEVENLABS_API_KEY": "test_key"}):
            result = await tool.execute(
                call_id="call_3",
                arguments={"prompt": "thunder", "duration_seconds": 45.0},
            )
            self.assertTrue(result.is_error)
            self.assertIn("between 0.5 and 30.0", result.content)

    async def test_sound_effect_successful_generation(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            mock_audio = b"\xff\xfb\x90\x44" + b"\x00" * 1024  # Mock MP3 bytes

            mock_response = MagicMock(spec=httpx.Response)
            mock_response.status_code = 200
            mock_response.content = mock_audio

            mock_client = AsyncMock(spec=httpx.AsyncClient)
            mock_client.post.return_value = mock_response

            mock_artifact_store = AsyncMock()

            tool = GenerateSoundEffectTool(
                project_output_dir=tmp_dir,
                artifact_store=mock_artifact_store,
                http_client=mock_client,
            )

            with patch.dict(os.environ, {"ELEVENLABS_API_KEY": "test_xi_key"}):
                result = await tool.execute(
                    call_id="call_success",
                    arguments={
                        "prompt": "Deep cinematic impact hit",
                        "duration_seconds": 3.5,
                        "prompt_influence": 0.4,
                        "loop": False,
                        "output_format": "mp3_44100_128",
                    },
                )

                self.assertFalse(result.is_error)
                self.assertIn("Generated sound effect successfully!", result.content)
                self.assertIn("Deep cinematic impact hit", result.content)
                self.assertIn("3.5s", result.content)
                self.assertIn("mp3_44100_128", result.content)

                # Verify API payload
                mock_client.post.assert_called_once()
                call_kwargs = mock_client.post.call_args.kwargs
                self.assertEqual(call_kwargs["json"]["text"], "Deep cinematic impact hit")
                self.assertEqual(call_kwargs["json"]["duration_seconds"], 3.5)
                self.assertEqual(call_kwargs["json"]["prompt_influence"], 0.4)
                self.assertFalse(call_kwargs["json"]["loop"])
                self.assertEqual(call_kwargs["headers"]["xi-api-key"], "test_xi_key")
                self.assertEqual(call_kwargs["params"]["output_format"], "mp3_44100_128")

                # Verify output file exists
                audio_files = list((tmp_dir / "audio").glob("*.mp3"))
                self.assertEqual(len(audio_files), 1)
                self.assertEqual(audio_files[0].read_bytes(), mock_audio)

                # Verify artifact store recording
                # The tool must NOT record the artifact itself. It used to try,
            # with `await` on a synchronous method and two required keyword
            # arguments missing — a TypeError swallowed by a bare except, so
            # generated sound effects never reached manifest.jsonl. An
            # AsyncMock accepts any signature, which is exactly why the old
            # assertion passed while the real call could not work. The bridge
            # writes the row now; see _handle_tool_result in freyja_bridge.
            mock_artifact_store.record_file.assert_not_called()

    async def test_sound_effect_custom_save_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            custom_file = tmp_dir / "custom_sfx" / "laser.wav"

            mock_audio = b"RIFF" + b"\x00" * 512  # Mock WAV

            mock_response = MagicMock(spec=httpx.Response)
            mock_response.status_code = 200
            mock_response.content = mock_audio

            mock_client = AsyncMock(spec=httpx.AsyncClient)
            mock_client.post.return_value = mock_response

            tool = GenerateSoundEffectTool(
                project_output_dir=tmp_dir,
                http_client=mock_client,
            )

            with patch.dict(os.environ, {"XI_API_KEY": "test_xi_key_2"}):
                result = await tool.execute(
                    call_id="call_custom",
                    arguments={
                        "prompt": "Sci-fi laser shot",
                        "output_format": "pcm_44100",
                        "save_path": str(custom_file),
                    },
                )

                self.assertFalse(result.is_error)
                self.assertTrue(custom_file.is_file())
                self.assertEqual(custom_file.read_bytes(), mock_audio)

    async def test_sound_effect_api_errors(self):
        # 401 Unauthorized
        mock_response_401 = MagicMock(spec=httpx.Response)
        mock_response_401.status_code = 401
        mock_client_401 = AsyncMock(spec=httpx.AsyncClient)
        mock_client_401.post.return_value = mock_response_401

        tool_401 = GenerateSoundEffectTool(http_client=mock_client_401)
        with patch.dict(os.environ, {"ELEVENLABS_API_KEY": "bad_key"}):
            result = await tool_401.execute(call_id="call_401", arguments={"prompt": "explosion"})
            self.assertTrue(result.is_error)
            self.assertIn("401 Unauthorized", result.content)

        # 402 Payment Required
        mock_response_402 = MagicMock(spec=httpx.Response)
        mock_response_402.status_code = 402
        mock_client_402 = AsyncMock(spec=httpx.AsyncClient)
        mock_client_402.post.return_value = mock_response_402

        tool_402 = GenerateSoundEffectTool(http_client=mock_client_402)
        with patch.dict(os.environ, {"ELEVENLABS_API_KEY": "key"}):
            result = await tool_402.execute(call_id="call_402", arguments={"prompt": "explosion"})
            self.assertTrue(result.is_error)
            self.assertIn("402 Payment Required", result.content)

    def test_registry_includes_generate_sound_effect(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            registry = build_desktop_registry(
                workspace=str(tmp_dir),
                project_output_dir=str(tmp_dir),
            )
            tool = registry.get("generate_sound_effect")
            self.assertIsNotNone(tool)
            self.assertEqual(tool.definition.name, "generate_sound_effect")
            self.assertIn("generate_sound_effect", registry.list_summaries())

    def test_format_to_extension(self):
        self.assertEqual(_format_to_extension("mp3_44100_128"), ".mp3")
        self.assertEqual(_format_to_extension("mp3_22050_32"), ".mp3")
        self.assertEqual(_format_to_extension("pcm_44100"), ".wav")
        self.assertEqual(_format_to_extension("opus_48000_128"), ".opus")


if __name__ == "__main__":
    unittest.main()


class TestMediaArtifactRecording(unittest.IsolatedAsyncioTestCase):
    """The scrape that puts generated media into the artifact manifest.

    generate_image and generate_sound_effect only report their output path in
    the human-readable result text, so the bridge's tool-result hook parses it
    back out to write the manifest row. That join is prose-to-regex and breaks
    silently: an artifact that never reaches manifest.jsonl never appears in
    the session library or the artifact browser, and nothing errors.
    """

    def test_path_is_scraped_from_both_result_formats(self):
        from bridge.freyja_bridge import MEDIA_ARTIFACT_PATH_RE

        image = MEDIA_ARTIFACT_PATH_RE.search("File saved to `/tmp/img.png`")
        self.assertIsNotNone(image)
        self.assertEqual(image.group(1), "/tmp/img.png")

        # What generate_sound_effect actually emits — an indented bullet.
        sfx = MEDIA_ARTIFACT_PATH_RE.search("  · File: `/tmp/audio/sfx_boom.mp3`")
        self.assertIsNotNone(sfx)
        self.assertEqual(sfx.group(1), "/tmp/audio/sfx_boom.mp3")

    def test_no_path_is_not_a_match(self):
        from bridge.freyja_bridge import MEDIA_ARTIFACT_PATH_RE

        self.assertIsNone(MEDIA_ARTIFACT_PATH_RE.search("Error: out of credits"))
        self.assertIsNone(MEDIA_ARTIFACT_PATH_RE.search(""))

    def test_both_generators_are_covered(self):
        from bridge.freyja_bridge import MEDIA_ARTIFACT_TOOLS

        self.assertIn("generate_image", MEDIA_ARTIFACT_TOOLS)
        self.assertIn("generate_sound_effect", MEDIA_ARTIFACT_TOOLS)

    def test_the_success_message_still_matches_the_scrape(self):
        # Pins the tool's prose to the regex. If the success text is reworded
        # without updating MEDIA_ARTIFACT_PATH_RE, this fails here rather than
        # silently dropping every future sound effect from the manifest.
        import inspect

        from bridge.freyja_bridge import MEDIA_ARTIFACT_PATH_RE
        from bridge.tools import sound_generation_tool

        source = inspect.getsource(sound_generation_tool)
        self.assertIn('f"  · File: `{target_path}`\\n"', source)
        rendered = "  · File: `/tmp/out.mp3`\n"
        self.assertEqual(MEDIA_ARTIFACT_PATH_RE.search(rendered).group(1), "/tmp/out.mp3")

    async def test_the_real_store_accepts_the_bridge_call(self):
        # The tool used to record the artifact itself with `await` on a
        # synchronous method and two required keyword arguments missing. The
        # test mock was an AsyncMock, which accepts any signature, so the
        # broken call looked fine. This drives the REAL store.
        import tempfile
        from pathlib import Path

        from bridge.artifact_store import SessionArtifactStore

        project = Path(tempfile.mkdtemp())
        store = SessionArtifactStore(session_id="s1", project_dir=project)
        store.ensure()
        target = project / "audio" / "sfx_boom.mp3"
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
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["source"], "generate_sound_effect")
        self.assertEqual(rows[0]["fileType"], "mp3")
        self.assertTrue(rows[0]["exists"])
