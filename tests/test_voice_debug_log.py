"""Debug-log credential redaction (bridge/freyja_bridge._write_debug_log).

The bridge journals every emitted event to ~/.freyja/bridge-events.jsonl
(default on). voice_session_ready carries the ephemeral OpenAI realtime
clientSecret and voice_tool_result carries single-use confirm tokens —
live credentials that must never land on disk, even though the stdout
copy legitimately delivers them to the renderer.
"""

from __future__ import annotations

import json

import pytest

import bridge.freyja_bridge as fb


@pytest.fixture
def debug_log(tmp_path, monkeypatch):
    path = tmp_path / "bridge-events.jsonl"
    monkeypatch.setattr(fb, "_DEBUG_LOG_PATH", path)
    monkeypatch.setattr(fb, "_DEBUG_LOG_PREV_PATH", tmp_path / "bridge-events.prev.jsonl")
    monkeypatch.setattr(fb, "_DEBUG_LOG_ENABLED", True)
    return path


def read_lines(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_session_ready_client_secret_never_hits_disk(debug_log):
    event = {
        "type": "voice_session_ready",
        "voiceSessionId": "voice-abc",
        "clientSecret": "ek_live_supersecret",
        "model": "gpt-realtime-2.1-mini",
        "expiresAt": 1750000000,
        "webrtcUrl": "https://api.openai.com/v1/realtime/calls",
    }
    fb._write_debug_log(event)
    (row,) = read_lines(debug_log)
    assert row["clientSecret"] == "<redacted>"
    # the rest of the event is journaled untouched — and the caller's
    # dict (the stdout copy) still holds the real value
    assert row["voiceSessionId"] == "voice-abc"
    assert row["model"] == "gpt-realtime-2.1-mini"
    assert event["clientSecret"] == "ek_live_supersecret"
    assert "ek_live_supersecret" not in debug_log.read_text(encoding="utf-8")


def test_confirm_token_redacted_in_needs_confirm_and_output(debug_log):
    token = "a1b2c3d4e5f60718"
    event = {
        "type": "voice_tool_result",
        "callId": "c1",
        "ok": False,
        "output": (
            f"CONFIRM REQUIRED: Quit Slack. Ask the user to confirm "
            f"aloud, then call act again with confirm_token {token}."
        ),
        "needsConfirm": {"token": token, "summary": "Quit Slack"},
    }
    fb._write_debug_log(event)
    (row,) = read_lines(debug_log)
    assert row["needsConfirm"]["token"] == "<redacted>"
    assert row["needsConfirm"]["summary"] == "Quit Slack"
    assert token not in row["output"]
    assert "CONFIRM REQUIRED: Quit Slack" in row["output"]
    assert token not in debug_log.read_text(encoding="utf-8")
    # stdout copy untouched — the model needs the real token
    assert event["needsConfirm"]["token"] == token


def test_redaction_matches_secretish_keys_only(debug_log):
    event = {
        "type": "voice_session_closed",
        "voiceSessionId": "voice-abc",
        "stats": {"seconds": 12, "inputTokens": 900, "outputTokens": 120},
        "nested": [{"apiKey": "sk-live", "password": "hunter2", "label": "fine"}],
    }
    fb._write_debug_log(event)
    (row,) = read_lines(debug_log)
    # count-style *Tokens fields are NOT credentials
    assert row["stats"] == {"seconds": 12, "inputTokens": 900, "outputTokens": 120}
    assert row["nested"][0]["apiKey"] == "<redacted>"
    assert row["nested"][0]["password"] == "<redacted>"
    assert row["nested"][0]["label"] == "fine"


def test_existing_png_trimming_still_applies(debug_log):
    fb._write_debug_log({"type": "screenshot", "pngBase64": "x" * 5000})
    (row,) = read_lines(debug_log)
    assert row["pngBase64"] == "<5000 b64 chars>"
