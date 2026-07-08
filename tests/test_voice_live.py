"""Live protocol tests against the real OpenAI Realtime API.

Gated hard: they run only with a real key AND explicit opt-in
(`FREYJA_VOICE_LIVE=1`), so CI and normal `pytest` stay hermetic.

    source .env && FREYJA_VOICE_LIVE=1 uv run --extra dev pytest tests/test_voice_live.py -q

Covers the two protocol facts the whole design leans on (contract §0):
  1. the client_secrets mint bakes + echoes the FULL session config, and
  2. the GA WS event names round-trip an `act` function call end-to-end.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from types import SimpleNamespace

import pytest

from bridge.voice.prompts import build_instructions
from bridge.voice.service import VoiceService

pytestmark = pytest.mark.skipif(
    not (os.environ.get("OPENAI_API_KEY") and os.environ.get("FREYJA_VOICE_LIVE") == "1"),
    reason="live voice tests need OPENAI_API_KEY and FREYJA_VOICE_LIVE=1",
)

_LIVE_MODEL = "gpt-realtime-2.1-mini"
_WS_URL = f"wss://api.openai.com/v1/realtime?model={_LIVE_MODEL}"
_VERBS = ["spotify.pause", "spotify.resume", "spotify.next", "system.volume"]
_CATALOG_MD = (
    "- spotify.pause() — pause Spotify playback\n"
    "- spotify.resume() — resume Spotify playback\n"
    "- spotify.next() — skip to the next track\n"
    "- system.volume(level|delta|mute) — set or nudge output volume"
)


def _act_schema() -> dict:
    return {
        "type": "function",
        "name": "act",
        "description": "Execute one device verb from the catalog.",
        "parameters": {
            "type": "object",
            "properties": {
                "verb": {"type": "string", "enum": _VERBS},
                "args": {"type": "object"},
                "confirm_token": {"type": "string"},
            },
            "required": ["verb"],
        },
    }


class _LiveRegistry:
    """Minimal in-memory registry — enough surface for the mint path.
    (bridge.voice.verbs may not be importable while agents build in
    parallel, and the live tests should stay hermetic regardless.)"""

    def catalog_markdown(self) -> str:
        return _CATALOG_MD

    def openai_tool_schema(self) -> dict:
        return _act_schema()

    def get(self, name):  # pragma: no cover - mint path never dispatches
        return None

    def all(self):  # pragma: no cover
        return []

    def register(self, verb):  # pragma: no cover
        pass


def _make_service(tmp_path):
    events: list[dict] = []
    svc = VoiceService(
        SimpleNamespace(default_model="test-model"),
        base_dir=tmp_path / "voice",
        registry=_LiveRegistry(),
        emit_fn=events.append,
    )
    return svc, events


# ── 1. live mint ──────────────────────────────────────────────────────────


async def test_live_mint_echoes_full_session_config(tmp_path):
    svc, _events = _make_service(tmp_path)
    await svc.start()
    api_key = os.environ["OPENAI_API_KEY"].strip()
    payload = {"session": svc._build_session_config(svc._registry)}
    data = await svc._mint(api_key, payload)

    assert data.get("value"), "mint must return an ephemeral secret value"
    expires_at = data.get("expires_at")
    assert isinstance(expires_at, int) and expires_at > time.time(), "TTL must be in the future"

    echoed = data.get("session") or {}
    assert echoed.get("model", "").startswith(_LIVE_MODEL)
    assert echoed["audio"]["input"]["transcription"]["model"] == "gpt-realtime-whisper"
    assert echoed["audio"]["input"]["turn_detection"]["type"] == "semantic_vad"
    assert echoed["audio"]["output"]["voice"] == "marin"
    assert [t.get("name") for t in echoed.get("tools", [])] == ["act"]
    assert "You are Freyja" in echoed.get("instructions", "")


async def test_live_session_start_emits_ready(tmp_path):
    svc, events = _make_service(tmp_path)
    await svc.start()
    await svc.handle_session_start({})
    ready = [e for e in events if e.get("type") == "voice_session_ready"]
    errors = [e for e in events if e.get("type") == "voice_error"]
    assert ready, f"expected voice_session_ready, got errors: {errors}"
    ev = ready[0]
    assert ev["voiceSessionId"].startswith("voice-")
    assert ev["clientSecret"]
    assert ev["model"] == _LIVE_MODEL
    assert ev["expiresAt"] > time.time()
    assert ev["webrtcUrl"] == "https://api.openai.com/v1/realtime/calls"


# ── 2. live WS text-mode act round trip ───────────────────────────────────


async def _recv_event(ws, timeout: float = 60.0) -> dict:
    raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
    return json.loads(raw)


async def _wait_for_response_done(ws) -> dict:
    while True:
        ev = await _recv_event(ws)
        if ev.get("type") == "error":
            raise AssertionError(f"realtime error event: {ev}")
        if ev.get("type") == "response.done":
            return ev


def _connect_ws(api_key: str):
    import websockets

    headers = {"Authorization": f"Bearer {api_key}"}
    try:
        # websockets >= 14 (repo pins >= 16)
        return websockets.connect(_WS_URL, additional_headers=headers, open_timeout=30)
    except TypeError:  # pragma: no cover - legacy client fallback
        return websockets.connect(_WS_URL, extra_headers=headers, open_timeout=30)


async def test_live_ws_text_mode_act_round_trip():
    api_key = os.environ["OPENAI_API_KEY"].strip()
    async with _connect_ws(api_key) as ws:
        # Text-only session with the act tool — the exact probe shape that
        # was validated live (contract §0).
        await ws.send(
            json.dumps(
                {
                    "type": "session.update",
                    "session": {
                        "type": "realtime",
                        "output_modalities": ["text"],
                        "instructions": build_instructions(_CATALOG_MD),
                        "tools": [_act_schema()],
                        "tool_choice": "auto",
                    },
                }
            )
        )
        await ws.send(
            json.dumps(
                {
                    "type": "conversation.item.create",
                    "item": {
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_text", "text": "pause the music"}],
                    },
                }
            )
        )
        await ws.send(json.dumps({"type": "response.create"}))

        # Function call arrives inside response.done → response.output[i]
        done = await _wait_for_response_done(ws)
        output = (done.get("response") or {}).get("output") or []
        calls = [item for item in output if item.get("type") == "function_call"]
        assert calls, f"expected a function_call in response.done output, got: {output}"
        call = calls[0]
        assert call["name"] == "act"
        assert call.get("call_id")
        args = json.loads(call["arguments"])
        assert args.get("verb") == "spotify.pause", args

        # Tool result back + response.create → final text confirmation
        await ws.send(
            json.dumps(
                {
                    "type": "conversation.item.create",
                    "item": {
                        "type": "function_call_output",
                        "call_id": call["call_id"],
                        "output": json.dumps({"ok": True, "summary": "paused"}),
                    },
                }
            )
        )
        await ws.send(json.dumps({"type": "response.create"}))

        final_text = ""
        while True:
            ev = await _recv_event(ws)
            if ev.get("type") == "error":
                raise AssertionError(f"realtime error event: {ev}")
            if ev.get("type") == "response.output_text.done":
                final_text += ev.get("text") or ""
            if ev.get("type") == "response.done":
                break
        assert final_text.strip(), "expected a final text confirmation after the tool result"
