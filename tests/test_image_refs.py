"""Images stored and sent by reference.

Two halves:
- engine/media_store.py: transcripts, raw logs, snapshots and the event
  mirror keep an image's SHA-256 instead of its base64; loading restores
  the identical string.
- engine/anthropic_files.py: the Anthropic provider uploads each image once
  and refers to it by file ID, switching from inline exactly once.

conftest points FREYJA_HOME at a per-test temp dir, so the media store and
upload index are isolated.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
from io import BytesIO
from types import SimpleNamespace

import pytest
from PIL import Image

from engine.anthropic_files import AnthropicImageFiles, file_refs_enabled
from engine.media_store import (
    externalize_images,
    get_image,
    image_path,
    image_sha256,
    put_image,
)
from engine.types import ImageBlock, Message, TextBlock, content_block_from_dict


def _noisy_png(w: int = 100, h: int = 100) -> str:
    """Random pixels so the PNG stays large enough (> 16 KB base64) to upload."""
    img = Image.frombytes("RGB", (w, h), os.urandom(w * h * 3))
    buf = BytesIO()
    img.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode("ascii")


def _image(data: str | None = None) -> ImageBlock:
    return ImageBlock(source_type="base64", data=data or _noisy_png(), media_type="image/png")


# ── media store ────────────────────────────────────────────────────────────


def test_put_and_get_round_trip_the_identical_string():
    data = _noisy_png()
    sha = put_image(data, "image/png")
    assert sha and image_path(sha, "image/png").exists()
    assert get_image(sha, "image/png") == data
    # Content-addressed: storing again is a no-op with the same key.
    assert put_image(data, "image/png") == sha


def test_non_canonical_base64_is_never_stored():
    data = _noisy_png()
    wrapped = data[:76] + "\n" + data[76:]  # decodes fine, but wouldn't round-trip
    assert put_image(wrapped, "image/png") is None
    assert put_image("not base64!", "image/png") is None


def test_externalize_moves_nested_images_and_keeps_everything_else():
    data = _noisy_png()
    doc = {
        "entries": [
            {
                "message": {
                    "role": "tool_result",
                    "content": [
                        {"type": "text", "text": "shot"},
                        {
                            "type": "tool_result",
                            "tool_use_id": "t",
                            "content": [
                                {
                                    "type": "image",
                                    "source_type": "base64",
                                    "media_type": "image/png",
                                    "data": data,
                                },
                            ],
                        },
                        {
                            "type": "image",
                            "source_type": "url",
                            "url": "https://x/y.png",
                            "media_type": "image/png",
                        },
                        {
                            "type": "image",
                            "source_type": "base64",
                            "media_type": "image/png",
                            "data": "bad\n==",
                        },
                    ],
                }
            }
        ]
    }
    externalize_images(doc)
    content = doc["entries"][0]["message"]["content"]
    nested = content[1]["content"][0]
    assert "data" not in nested and nested["sha256"] == image_sha256(_image(data))
    assert content[2]["url"] == "https://x/y.png"  # URL images untouched
    assert content[3]["data"] == "bad\n=="  # unstorable stays inline


def test_deserialize_resolves_reference_and_marks_missing_blob():
    data = _noisy_png()
    sha = put_image(data, "image/png")
    block = content_block_from_dict({"type": "image", "media_type": "image/png", "sha256": sha})
    assert isinstance(block, ImageBlock) and block.data == data

    missing = content_block_from_dict(
        {"type": "image", "media_type": "image/png", "sha256": "ab" * 32}
    )
    assert isinstance(missing, TextBlock)
    assert "missing from the media store" in missing.text


def test_saved_transcript_holds_hashes_and_restores_identically(tmp_path, monkeypatch):
    import bridge.transcript_persistence as tp
    from engine.session import Session

    monkeypatch.setattr(tp, "SESSIONS_DIR", tmp_path)
    data = _noisy_png()
    session = Session.create(system_prompt="t")
    session.add_user_message([TextBlock(text="look"), _image(data)])

    tp.save_transcript("s1", session.serialize_transcript())
    raw = (tmp_path / "s1.transcript.json").read_text()
    assert data not in raw and '"sha256"' in raw

    restored = Session.create(system_prompt="t")
    restored.restore_transcript(tp.load_transcript("s1"))
    images = [
        b
        for m in restored.get_messages()
        if isinstance(m.content, list)
        for b in m.content
        if isinstance(b, ImageBlock)
    ]
    assert [b.data for b in images] == [data]


def test_event_mirror_moves_image_payloads_without_touching_the_live_event():
    from bridge.freyja_bridge import _externalize_event_images

    data = _noisy_png()
    event = {
        "type": "tool_result",
        "images": [{"id": "i1", "dataBase64": data, "mimeType": "image/png"}],
        "pngBase64": data,
        "mimeType": "image/png",
    }
    row = _externalize_event_images(event)
    assert "pngBase64" not in row and row["pngSha256"]
    assert (
        "dataBase64" not in row["images"][0] and row["images"][0]["dataSha256"] == row["pngSha256"]
    )
    assert event["pngBase64"] == data and event["images"][0]["dataBase64"] == data


# ── file references ────────────────────────────────────────────────────────


class _FakeFiles:
    def __init__(self, fail: bool = False, delay: float = 0.0, delays: list[float] | None = None):
        self.fail, self.delay, self.uploads = fail, delay, []
        self.delays = list(delays or [])

    async def upload(self, *, file, extra_body=None):
        await asyncio.sleep(self.delays.pop(0) if self.delays else self.delay)
        if self.fail:
            raise RuntimeError("upload refused")
        self.uploads.append((file[0], extra_body))
        return SimpleNamespace(id=f"file_{len(self.uploads):03d}")


def _files(fake: _FakeFiles, key: str = "sk-test") -> AnthropicImageFiles:
    return AnthropicImageFiles(SimpleNamespace(beta=SimpleNamespace(files=fake)), key)


def _conversation(*image_positions: str) -> tuple[list[Message], dict[str, ImageBlock]]:
    """Messages with an image in the 'old' (earlier) and/or 'new' (last) message."""
    blocks = {pos: _image() for pos in image_positions}
    messages = [Message(role="user", content="start")]
    if "old" in blocks:
        messages.append(Message(role="user", content=[TextBlock(text="old"), blocks["old"]]))
        messages.append(Message(role="assistant", content="ok"))
    messages.append(
        Message(
            role="tool_result",
            content=[TextBlock(text="new"), blocks.get("new") or TextBlock(text="-")],
        )
    )
    return messages, blocks


@pytest.mark.asyncio
async def test_newest_image_goes_inline_first_then_by_file_id():
    fake = _FakeFiles(delay=0.01)
    files = _files(fake)
    messages, blocks = _conversation("new")

    await files.prepare(messages)
    assert files.source_for(blocks["new"]) is None  # this request: inline, no wait
    await asyncio.sleep(0.05)  # background upload lands
    assert files.source_for(blocks["new"]) == {"type": "file", "file_id": "file_001"}
    assert fake.uploads[0][1] == {"expires_in_seconds": 30 * 24 * 3600}


@pytest.mark.asyncio
async def test_backlog_images_are_uploaded_before_the_request():
    fake = _FakeFiles(delay=0.01)
    files = _files(fake)
    messages, blocks = _conversation("old", "new")

    await files.prepare(messages)
    # The older image was waited for; the newest one was not (though its
    # background upload may have landed meanwhile, which is fine: it has
    # never been sent, so going by file ID from the start costs nothing).
    assert files.source_for(blocks["old"])["type"] == "file"
    await asyncio.sleep(0.05)
    assert len(fake.uploads) == 2


@pytest.mark.asyncio
async def test_failed_upload_pins_the_image_inline():
    fake = _FakeFiles(fail=True)
    files = _files(fake)
    messages, blocks = _conversation("old")

    await files.prepare(messages)
    await files.prepare(messages)
    assert files.source_for(blocks["old"]) is None
    assert fake.uploads == []  # no retry storm


@pytest.mark.asyncio
async def test_small_images_are_never_uploaded():
    fake = _FakeFiles()
    files = _files(fake)
    tiny = ImageBlock(
        source_type="base64", data=base64.b64encode(b"x" * 300).decode(), media_type="image/png"
    )
    await files.prepare(
        [Message(role="user", content=[tiny]), Message(role="user", content="next")]
    )
    assert fake.uploads == [] and files.source_for(tiny) is None


@pytest.mark.asyncio
async def test_uploads_are_shared_through_the_index_and_scoped_to_the_key():
    fake = _FakeFiles()
    messages, blocks = _conversation("old")
    await _files(fake).prepare(messages)

    other_process = _files(_FakeFiles())
    assert other_process.source_for(blocks["old"]) == {"type": "file", "file_id": "file_001"}
    assert _files(_FakeFiles(), key="sk-other").source_for(blocks["old"]) is None


@pytest.mark.asyncio
async def test_expiring_uploads_are_not_used(monkeypatch):
    import engine.anthropic_files as af

    files = _files(_FakeFiles())
    messages, blocks = _conversation("old")
    await files.prepare(messages)
    now = af.time.time()
    monkeypatch.setattr(af.time, "time", lambda: now + af.FILE_TTL_SECONDS - 3600)
    assert files.source_for(blocks["old"]) is None


@pytest.mark.asyncio
async def test_missing_file_drops_request_ids_for_everyone():
    files = _files(_FakeFiles())
    messages, blocks = _conversation("old")
    await files.prepare(messages)
    request = {
        "messages": [
            {
                "role": "user",
                "content": [{"type": "image", "source": {"type": "file", "file_id": "file_001"}}],
            }
        ]
    }

    assert files.forget_request_files(request) == 1
    assert files.source_for(blocks["old"]) is None
    assert _files(_FakeFiles()).source_for(blocks["old"]) is None  # tombstone persisted


def test_file_refs_only_for_first_party_api(monkeypatch):
    assert file_refs_enabled(None)
    assert file_refs_enabled("https://api.anthropic.com")
    assert not file_refs_enabled("https://my-proxy.example.com/anthropic")
    monkeypatch.setenv("FREYJA_ANTHROPIC_FILE_REFS", "0")
    assert not file_refs_enabled(None)


# ── provider ───────────────────────────────────────────────────────────────


def _provider(monkeypatch, **cfg):
    from engine.anthropic_provider import AnthropicConfig, AnthropicProvider

    return AnthropicProvider(AnthropicConfig(api_key="sk-test", model="claude-opus-5-5", **cfg))


@pytest.mark.asyncio
async def test_provider_sends_uploaded_images_by_file_id(monkeypatch):
    provider = _provider(monkeypatch)
    # The newest image's upload is slow, so it can't land while the
    # backlog is waited for.
    provider._image_files = _files(_FakeFiles(delays=[0.01, 0.5]))  # noqa: SLF001
    messages, blocks = _conversation("old")
    messages.append(Message(role="user", content=[TextBlock(text="newest"), (newest := _image())]))

    await provider._image_files.prepare(messages)  # noqa: SLF001
    converted = json.dumps(provider._convert_messages(messages))  # noqa: SLF001
    assert '"source": {"type": "file", "file_id": "file_001"}' in converted
    assert blocks["old"].data not in converted
    assert newest.data in converted  # not waited for: inline


def test_provider_has_no_file_refs_behind_a_proxy(monkeypatch):
    assert _provider(monkeypatch, base_url="https://proxy.example.com")._image_files is None  # noqa: SLF001
    assert _provider(monkeypatch)._image_files is not None  # noqa: SLF001


def test_missing_file_404_is_retryable_but_model_404_is_not(monkeypatch):
    from engine.providers import ModelNotFoundError, ProviderError

    class _Err(Exception):
        def __init__(self, m):
            super().__init__(m)
            self.status_code = 404

    provider = _provider(monkeypatch)
    gone = provider._convert_api_error(
        _Err(  # noqa: SLF001
            "Error code: 404 - {'type': 'error', 'error': {'type': 'not_found_error', "
            "'message': 'File `file_011CZZ` not found.'}}"
        )
    )
    assert isinstance(gone, ProviderError) and not isinstance(gone, ModelNotFoundError)
    assert gone.retryable and gone.code == "file_not_found"
    model = provider._convert_api_error(
        _Err(  # noqa: SLF001
            "Error code: 404 - {'type': 'error', 'error': {'type': 'not_found_error', "
            "'message': 'model: claude-nope'}}"
        )
    )
    assert isinstance(model, ModelNotFoundError)


@pytest.mark.asyncio
async def test_missing_file_is_retryable_only_if_ids_were_dropped(monkeypatch):
    class _Err(Exception):
        status_code = 404

        def __str__(self):
            return (
                "Error code: 404 - {'type': 'error', 'error': {'type': 'not_found_error', "
                "'message': 'File `file_001` not found.'}}"
            )

    provider = _provider(monkeypatch)
    provider._image_files = _files(_FakeFiles())  # noqa: SLF001
    messages, _ = _conversation("old")
    await provider._image_files.prepare(messages)  # noqa: SLF001
    request = {"messages": provider._convert_messages(messages)}  # noqa: SLF001

    first = provider._request_error(_Err(), request)  # noqa: SLF001
    again = provider._request_error(_Err(), request)  # noqa: SLF001
    assert first.code == "file_not_found" and first.retryable
    assert not again.retryable  # nothing left to drop: no loop


@pytest.mark.asyncio
async def test_runner_resends_at_once_on_missing_file():
    """Handled before the generic error path, so no retry backoff and no
    model fallback: the provider already dropped the stale file IDs."""
    import time as _time

    from engine.providers import APIUsage, ProviderError, ProviderResponse
    from engine.runner import AsyncAgentRunner
    from engine.session import Session

    class _Provider:
        name, model_id, context_window = "anthropic", "claude-opus-5-5", 1_000_000

        def __init__(self):
            self.calls = 0

        async def complete_async(self, **kwargs):
            self.calls += 1
            if self.calls == 1:
                raise ProviderError(
                    "File `file_001` not found.", status=404, code="file_not_found", retryable=True
                )
            return ProviderResponse(
                content="done",
                tool_calls=None,
                stop_reason="end_turn",
                usage=APIUsage(input_tokens=1, output_tokens=1),
                model=self.model_id,
            )

    provider = _Provider()
    started = _time.perf_counter()
    result = await AsyncAgentRunner(provider).run(
        Session.create(system_prompt="t"), "go", stream=False
    )
    assert result.success and provider.calls == 2
    assert _time.perf_counter() - started < 0.5
