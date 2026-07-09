"""VoiceService tests (bridge/voice/service.py).

Everything runs against the constructor test seams: a fake verb registry
(bridge.voice.verbs is the adapters agent's module and is never imported
here), an emit collector, and a tmp_path storage dir. The mint tests
monkeypatch httpx.AsyncClient on the service module.
"""

from __future__ import annotations

import asyncio
import json
import time
from types import SimpleNamespace

import pytest

import bridge.voice.service as voice_service_module
from bridge.voice.service import _MINT_URL, _WEBRTC_URL, VoiceService

# ── fakes ─────────────────────────────────────────────────────────────────


class FakeResult:
    """Duck-types bridge.voice.verbs.VerbResult."""

    def __init__(self, ok=True, summary="", say=None, data=None, undo=None, error=None):
        self.ok = ok
        self.summary = summary
        self.say = say
        self.data = data or {}
        self.undo = undo
        self.error = error


class FakeVerb:
    def __init__(self, name, run, tier="auto"):
        self.name = name
        self.description = f"fake {name}"
        self.params = {}
        self.required = []
        self.tier = tier
        self.run = run


class FakeRegistry:
    def __init__(self, verbs=()):
        self._verbs = {v.name: v for v in verbs}

    def register(self, verb):
        self._verbs[verb.name] = verb

    def get(self, name):
        return self._verbs.get(name)

    def all(self):
        return list(self._verbs.values())

    def catalog_markdown(self):
        return "- fake.verb() — a fake verb for tests"

    def openai_tool_schema(self):
        return {
            "type": "function",
            "name": "act",
            "description": "act",
            "parameters": {
                "type": "object",
                "properties": {
                    "verb": {"type": "string", "enum": sorted(self._verbs)},
                    "args": {"type": "object"},
                    "confirm_token": {"type": "string"},
                },
                "required": ["verb"],
            },
        }


class FakeResponse:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.text = text

    def json(self):
        return self._payload


def install_fake_httpx(monkeypatch, post_results):
    """Each post() consumes one entry of post_results — an Exception to
    raise or a FakeResponse to return. Returns the captured-calls list."""
    calls = []

    class FakeClient:
        def __init__(self, timeout=None):
            self.timeout = timeout

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, headers=None, json=None):
            calls.append({"url": url, "headers": headers, "json": json})
            result = post_results.pop(0)
            if isinstance(result, Exception):
                raise result
            return result

    monkeypatch.setattr(voice_service_module.httpx, "AsyncClient", FakeClient)
    return calls


def make_service(tmp_path, verbs=(), state=None):
    events = []
    svc = VoiceService(
        state if state is not None else SimpleNamespace(default_model="test-model"),
        base_dir=tmp_path / "voice",
        registry=FakeRegistry(verbs),
        emit_fn=events.append,
    )
    return svc, events


def events_of(events, ev_type):
    return [e for e in events if e.get("type") == ev_type]


# ── config ────────────────────────────────────────────────────────────────


async def test_config_defaults(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("SPOTIFY_CLIENT_ID", raising=False)
    monkeypatch.delenv("SPOTIFY_CLIENT_SECRET", raising=False)
    svc, events = make_service(tmp_path)
    await svc.start()
    await svc.handle_get_config({})
    (ev,) = events_of(events, "voice_config")
    cfg = ev["config"]
    assert cfg["enabled"] is True
    assert cfg["model"] == "gpt-realtime-2.1-mini"
    assert cfg["voice"] == "marin"
    assert cfg["vadMode"] == "semantic_vad"
    assert cfg["idleTimeoutSec"] == 25
    assert cfg["available"]["models"] == [
        "gpt-realtime-2.1-mini",
        "gpt-realtime-2.1",
        "gpt-realtime",
        "gpt-realtime-mini",
    ]
    assert cfg["available"]["voices"] == ["marin", "cedar", "alloy", "echo", "shimmer", "coral"]
    assert cfg["hasApiKey"] is False
    assert cfg["spotifySearch"] is False


async def test_config_roundtrip_persists(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("SPOTIFY_CLIENT_ID", "cid")
    monkeypatch.setenv("SPOTIFY_CLIENT_SECRET", "csec")
    svc, events = make_service(tmp_path)
    await svc.start()
    await svc.handle_set_config(
        {"patch": {"model": "gpt-realtime", "voice": "cedar", "idleTimeoutSec": 40}}
    )
    (ev,) = events_of(events, "voice_config")
    assert ev["config"]["model"] == "gpt-realtime"
    assert ev["config"]["voice"] == "cedar"
    assert ev["config"]["idleTimeoutSec"] == 40
    assert ev["config"]["hasApiKey"] is True
    assert ev["config"]["spotifySearch"] is True
    # a fresh service over the same dir loads the persisted values
    svc2, events2 = make_service(tmp_path)
    await svc2.start()
    await svc2.handle_get_config({})
    (ev2,) = events_of(events2, "voice_config")
    assert ev2["config"]["model"] == "gpt-realtime"
    assert ev2["config"]["voice"] == "cedar"
    assert ev2["config"]["idleTimeoutSec"] == 40


async def test_config_patch_validation(tmp_path):
    svc, events = make_service(tmp_path)
    await svc.start()
    await svc.handle_set_config(
        {
            "patch": {
                "vadMode": "psychic_vad",  # not a mode — ignored
                "idleTimeoutSec": 100000,  # clamped
                "enabled": "yes",  # not a bool — ignored
                "model": "",  # empty — ignored
                "bogus": 1,  # unknown — ignored
            }
        }
    )
    (ev,) = events_of(events, "voice_config")
    cfg = ev["config"]
    assert cfg["vadMode"] == "semantic_vad"
    assert cfg["idleTimeoutSec"] == 600
    assert cfg["enabled"] is True
    assert cfg["model"] == "gpt-realtime-2.1-mini"
    assert "bogus" not in cfg


# ── session start (mint) ──────────────────────────────────────────────────


async def test_session_start_without_api_key(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    svc, events = make_service(tmp_path)
    await svc.handle_session_start({})
    (err,) = events_of(events, "voice_error")
    assert err["code"] == "no_api_key"
    assert "OPENAI_API_KEY" in err["message"]
    assert not events_of(events, "voice_session_ready")


async def test_session_start_mint_payload_and_ready_event(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    svc, events = make_service(tmp_path)
    await svc.start()
    calls = install_fake_httpx(
        monkeypatch,
        [FakeResponse(payload={"value": "ek_test_123", "expires_at": 1750000000, "session": {}})],
    )
    await svc.handle_session_start({})

    assert len(calls) == 1
    call = calls[0]
    assert call["url"] == _MINT_URL
    assert call["headers"]["Authorization"] == "Bearer sk-test"
    session = call["json"]["session"]
    assert session["type"] == "realtime"
    assert session["model"] == "gpt-realtime-2.1-mini"
    # instructions bake the verb catalog verbatim
    assert "- fake.verb() — a fake verb for tests" in session["instructions"]
    assert "You are Freyja" in session["instructions"]
    assert session["output_modalities"] == ["audio"]
    assert session["audio"]["input"]["transcription"]["model"] == "gpt-realtime-whisper"
    assert session["audio"]["input"]["turn_detection"]["type"] == "semantic_vad"
    assert session["audio"]["output"]["voice"] == "marin"
    assert session["tools"] == [svc._registry.openai_tool_schema()]
    assert session["tool_choice"] == "auto"

    (ready,) = events_of(events, "voice_session_ready")
    assert ready["voiceSessionId"].startswith("voice-")
    assert ready["clientSecret"] == "ek_test_123"
    assert ready["model"] == "gpt-realtime-2.1-mini"
    assert ready["expiresAt"] == 1750000000
    assert ready["webrtcUrl"] == _WEBRTC_URL == "https://api.openai.com/v1/realtime/calls"


async def test_session_start_retries_once_on_network_error(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    svc, events = make_service(tmp_path)
    calls = install_fake_httpx(
        monkeypatch,
        [
            ConnectionError("network down"),
            FakeResponse(payload={"value": "ek_retry", "expires_at": 1}),
        ],
    )
    await svc.handle_session_start({})
    assert len(calls) == 2
    (ready,) = events_of(events, "voice_session_ready")
    assert ready["clientSecret"] == "ek_retry"


async def test_session_start_mint_failure_emits_error(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    svc, events = make_service(tmp_path)
    calls = install_fake_httpx(
        monkeypatch,
        [ConnectionError("down"), ConnectionError("still down")],
    )
    await svc.handle_session_start({})
    assert len(calls) == 2
    (err,) = events_of(events, "voice_error")
    assert err["code"] == "mint_failed"
    assert "still down" in err["message"]


async def test_session_start_4xx_is_terminal_no_retry(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-bad")
    svc, events = make_service(tmp_path)
    calls = install_fake_httpx(
        monkeypatch, [FakeResponse(status_code=401, text="bad key")]
    )
    await svc.handle_session_start({})
    assert len(calls) == 1
    (err,) = events_of(events, "voice_error")
    assert err["code"] == "mint_failed"
    assert "401" in err["message"]


async def test_session_start_refused_when_voice_disabled(tmp_path, monkeypatch):
    """enabled=false must be enforced bridge-side — Alt+Space is
    registered unconditionally, so this gate is what keeps a disabled
    config from opening a live mic."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    svc, events = make_service(tmp_path)
    await svc.start()
    calls = install_fake_httpx(monkeypatch, [])
    await svc.handle_set_config({"patch": {"enabled": False}})
    events.clear()
    await svc.handle_session_start({})
    (err,) = events_of(events, "voice_error")
    assert err["code"] == "voice_disabled"
    assert not events_of(events, "voice_session_ready")
    assert calls == []  # never even reached the mint


async def test_session_start_supersedes_previous_active_session(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    svc, events = make_service(tmp_path)
    install_fake_httpx(
        monkeypatch,
        [
            FakeResponse(payload={"value": "ek_A", "expires_at": 1}),
            FakeResponse(payload={"value": "ek_B", "expires_at": 2}),
        ],
    )
    await svc.handle_session_start({})
    (ready_a,) = events_of(events, "voice_session_ready")
    events.clear()
    await svc.handle_session_start({})
    # the old session is closed as superseded BEFORE the new ready lands
    (closed,) = events_of(events, "voice_session_closed")
    assert closed["voiceSessionId"] == ready_a["voiceSessionId"]
    assert closed["reason"] == "superseded"
    (ready_b,) = events_of(events, "voice_session_ready")
    assert ready_b["voiceSessionId"] != ready_a["voiceSessionId"]
    assert events.index(closed) < events.index(ready_b)
    assert svc._active_session_id == ready_b["voiceSessionId"]


async def test_concurrent_session_starts_drop_stale_mint(tmp_path, monkeypatch):
    """Two overlapping starts: the older mint finishing last must be
    dropped, never emitted as a second ready or allowed to clobber the
    newer session's bookkeeping."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    svc, events = make_service(tmp_path)
    gate_a = asyncio.Event()
    mint_calls = []

    async def fake_mint(api_key, payload):
        mint_calls.append(1)
        if len(mint_calls) == 1:
            await gate_a.wait()
            return {"value": "ek_A", "expires_at": 1}
        return {"value": "ek_B", "expires_at": 2}

    monkeypatch.setattr(svc, "_mint", fake_mint)
    task_a = asyncio.create_task(svc.handle_session_start({}))
    await asyncio.sleep(0)  # A is parked on its mint await
    await svc.handle_session_start({})  # B mints and completes first
    gate_a.set()
    await task_a
    readies = events_of(events, "voice_session_ready")
    assert [r["clientSecret"] for r in readies] == ["ek_B"]
    assert svc._active_session_id == readies[0]["voiceSessionId"]
    assert not events_of(events, "voice_session_closed")
    assert not events_of(events, "voice_error")


async def test_session_end_reports_stats(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    svc, events = make_service(
        tmp_path, verbs=[FakeVerb("spotify.pause", run=_ok_run("paused"))]
    )
    install_fake_httpx(monkeypatch, [FakeResponse(payload={"value": "ek", "expires_at": 1})])
    await svc.handle_session_start({})
    (ready,) = events_of(events, "voice_session_ready")
    vsid = ready["voiceSessionId"]
    await svc.handle_tool_call(_act_cmd(vsid, "c1", "spotify.pause"))
    await svc.handle_session_end(
        {"voiceSessionId": vsid, "reason": "idle", "stats": {"seconds": 12}}
    )
    (closed,) = events_of(events, "voice_session_closed")
    assert closed["voiceSessionId"] == vsid
    assert closed["reason"] == "idle"
    assert closed["receiptsCount"] == 1
    assert closed["seconds"] == 12


# ── act dispatch ──────────────────────────────────────────────────────────


def _ok_run(summary, **kw):
    async def run(args):
        return FakeResult(ok=True, summary=summary, **kw)

    return run


def _act_cmd(vsid, call_id, verb, args=None, confirm_token=None, heard=""):
    payload = {"verb": verb}
    if args is not None:
        payload["args"] = args
    if confirm_token is not None:
        payload["confirm_token"] = confirm_token
    return {
        "voiceSessionId": vsid,
        "callId": call_id,
        "name": "act",
        "argumentsJson": json.dumps(payload),
        "heard": heard,
    }


async def test_act_happy_path(tmp_path):
    ran = []

    async def run(args):
        ran.append(args)
        return FakeResult(
            ok=True, summary="▶ Vienna — Billy Joel", say="playing", data={"uri": "x"}
        )

    svc, events = make_service(tmp_path, verbs=[FakeVerb("spotify.play", run=run)])
    await svc.handle_tool_call(
        _act_cmd("voice-1", "call-1", "spotify.play", args={"query": "vienna"}, heard="play vienna")
    )
    assert ran == [{"query": "vienna"}]
    (result,) = events_of(events, "voice_tool_result")
    assert result["callId"] == "call-1"
    assert result["voiceSessionId"] == "voice-1"
    assert result["ok"] is True
    assert result["say"] == "playing"
    output = json.loads(result["output"])
    assert output == {"ok": True, "summary": "▶ Vienna — Billy Joel", "data": {"uri": "x"}}
    # receipt: emitted live, attached to the result, and persisted
    (receipt_ev,) = events_of(events, "voice_receipt")
    receipt = receipt_ev["receipt"]
    assert receipt == result["receipt"]
    assert receipt["lane"] == "brain"
    assert receipt["verb"] == "spotify.play"
    assert receipt["heard"] == "play vienna"
    assert receipt["ok"] is True
    assert receipt["undoable"] is False
    stored = svc.receipts.recent(limit=1)
    assert stored[0].id == receipt["id"]


async def test_act_unknown_verb(tmp_path):
    svc, events = make_service(tmp_path, verbs=[FakeVerb("spotify.play", run=_ok_run("ok"))])
    await svc.handle_tool_call(_act_cmd("voice-1", "call-2", "email.send", args={}))
    (result,) = events_of(events, "voice_tool_result")
    assert result["ok"] is False
    output = json.loads(result["output"])
    assert output["error"] == "unknown_verb"
    assert "email.send" in output["summary"]
    # refusal still leaves a receipt
    (receipt_ev,) = events_of(events, "voice_receipt")
    assert receipt_ev["receipt"]["ok"] is False
    assert receipt_ev["receipt"]["verb"] == "email.send"


async def test_act_wrong_tool_name(tmp_path):
    svc, events = make_service(tmp_path)
    await svc.handle_tool_call(
        {"voiceSessionId": "voice-1", "callId": "c", "name": "hack", "argumentsJson": "{}"}
    )
    (result,) = events_of(events, "voice_tool_result")
    assert result["ok"] is False
    assert json.loads(result["output"])["error"] == "unknown_tool"
    # nothing was attempted against a verb — no receipt
    assert not events_of(events, "voice_receipt")


async def test_act_bad_arguments_json(tmp_path):
    svc, events = make_service(tmp_path)
    await svc.handle_tool_call(
        {"voiceSessionId": "voice-1", "callId": "c", "name": "act", "argumentsJson": "{oops"}
    )
    (result,) = events_of(events, "voice_tool_result")
    assert result["ok"] is False
    assert json.loads(result["output"])["error"] == "bad_arguments"


async def test_act_verb_exception_becomes_tool_error(tmp_path):
    async def run(args):
        raise RuntimeError("osascript exploded")

    svc, events = make_service(tmp_path, verbs=[FakeVerb("app.open", run=run)])
    await svc.handle_tool_call(_act_cmd("voice-1", "c", "app.open", args={"name": "Mail"}))
    (result,) = events_of(events, "voice_tool_result")
    assert result["ok"] is False
    output = json.loads(result["output"])
    assert "osascript exploded" in output["error"]
    (receipt_ev,) = events_of(events, "voice_receipt")
    assert receipt_ev["receipt"]["ok"] is False


# ── confirm tokens ────────────────────────────────────────────────────────


def test_describe_human_templates_and_fallback():
    """Confirm summaries read as prose for templated verbs, raw
    verb+args otherwise — never a machine dump on the HUD confirm row."""
    from bridge.voice.service import _describe

    assert _describe("app.quit", {"name": "Slack"}) == "Quit Slack"
    # template blows up on odd args → raw fallback, not a crash
    assert _describe("app.quit", {}) == "app.quit"
    assert _describe("custom.verb", {"a": 1}) == "custom.verb a=1"
    assert _describe("custom.verb", {}) == "custom.verb"
    # slice-2 confirm verbs read as prose too
    assert _describe("slack.send", {"channel": "#general", "text": "hi"}) == (
        "Send to #general: hi"
    )
    assert _describe("slack.send", {"user": "Ada", "text": "hi"}) == "Send to @Ada: hi"
    assert _describe("slack.send", {"text": "hi"}) == "slack.send text=hi"  # no target → raw
    assert _describe("computer.do", {"task": "open settings"}) == (
        "Drive the Mac: open settings"
    )


async def test_confirm_full_cycle(tmp_path):
    ran = []

    async def run(args):
        ran.append(args)
        return FakeResult(ok=True, summary="quit Safari")

    svc, events = make_service(tmp_path, verbs=[FakeVerb("app.quit", run=run, tier="confirm")])

    # 1. no token → refused with CONFIRM REQUIRED + needsConfirm
    await svc.handle_tool_call(_act_cmd("voice-1", "c1", "app.quit", args={"name": "Safari"}))
    assert ran == []
    (r1,) = events_of(events, "voice_tool_result")
    assert r1["ok"] is False
    nc = r1["needsConfirm"]
    token = nc["token"]
    assert len(token) == 16  # secrets.token_hex(8)
    assert nc["summary"] == "Quit Safari"
    assert r1["output"] == (
        f"CONFIRM REQUIRED: Quit Safari. If the user already clearly "
        f"assented to exactly this in their last utterance, call act "
        f"again with confirm_token {token} right now — one spoken yes "
        f"is one yes. Otherwise ask aloud once, then call act again "
        f"with confirm_token {token}."
    )
    (rc1,) = events_of(events, "voice_receipt")
    assert rc1["receipt"]["summary"] == "awaiting confirmation: Quit Safari"

    # 2. same verb+args with the token → runs
    events.clear()
    await svc.handle_tool_call(
        _act_cmd("voice-1", "c2", "app.quit", args={"name": "Safari"}, confirm_token=token)
    )
    assert ran == [{"name": "Safari"}]
    (r2,) = events_of(events, "voice_tool_result")
    assert r2["ok"] is True
    assert "needsConfirm" not in r2

    # 3. token was single-use — replaying it refuses again with a NEW token
    events.clear()
    await svc.handle_tool_call(
        _act_cmd("voice-1", "c3", "app.quit", args={"name": "Safari"}, confirm_token=token)
    )
    assert len(ran) == 1
    (r3,) = events_of(events, "voice_tool_result")
    assert r3["ok"] is False
    assert r3["needsConfirm"]["token"] != token


async def test_confirm_token_accepted_inside_args(tmp_path):
    """Production repro (2026-07-08): the realtime model reliably re-calls
    with the confirm_token tucked INSIDE args instead of top-level, which
    looped 'awaiting confirmation' five deep. Both placements must
    validate, and the stripped token must not reach the verb or poison
    the scope hash."""
    ran = []

    async def run(args):
        ran.append(args)
        return FakeResult(ok=True, summary="quit Spotify")

    svc, events = make_service(tmp_path, verbs=[FakeVerb("app.quit", run=run, tier="confirm")])

    await svc.handle_tool_call(_act_cmd("voice-1", "c1", "app.quit", args={"name": "Spotify"}))
    (r1,) = events_of(events, "voice_tool_result")
    token = r1["needsConfirm"]["token"]

    # Re-call with the token as an args key — exactly what the model sent.
    events.clear()
    await svc.handle_tool_call(
        _act_cmd(
            "voice-1",
            "c2",
            "app.quit",
            args={"name": "Spotify", "confirm_token": token},
        )
    )
    (r2,) = events_of(events, "voice_tool_result")
    assert r2["ok"] is True, r2
    # The verb saw clean args — no token leaked through.
    assert ran == [{"name": "Spotify"}]


async def test_confirm_token_survives_one_mangled_recall(tmp_path):
    """A scope-mismatched attempt must NOT burn the pending token: the
    model can mangle one re-call (extra key, changed arg) and still
    succeed on the next attempt with the same token."""
    ran = []

    async def run(args):
        ran.append(args)
        return FakeResult(ok=True, summary="quit Spotify")

    svc, events = make_service(tmp_path, verbs=[FakeVerb("app.quit", run=run, tier="confirm")])

    await svc.handle_tool_call(_act_cmd("voice-1", "c1", "app.quit", args={"name": "Spotify"}))
    (r1,) = events_of(events, "voice_tool_result")
    token = r1["needsConfirm"]["token"]

    # Mangled re-call: args drifted, so validation fails — but the token
    # must survive for the follow-up.
    events.clear()
    await svc.handle_tool_call(
        _act_cmd("voice-1", "c2", "app.quit", args={"name": "spotify.app"}, confirm_token=token)
    )
    assert ran == []

    events.clear()
    await svc.handle_tool_call(
        _act_cmd("voice-1", "c3", "app.quit", args={"name": "Spotify"}, confirm_token=token)
    )
    (r3,) = events_of(events, "voice_tool_result")
    assert r3["ok"] is True, r3
    assert ran == [{"name": "Spotify"}]


async def test_confirm_token_scoped_to_verb_and_args(tmp_path):
    ran = []

    async def run(args):
        ran.append(args)
        return FakeResult(ok=True, summary="quit")

    svc, events = make_service(tmp_path, verbs=[FakeVerb("app.quit", run=run, tier="confirm")])
    await svc.handle_tool_call(_act_cmd("voice-1", "c1", "app.quit", args={"name": "Safari"}))
    token = events_of(events, "voice_tool_result")[0]["needsConfirm"]["token"]
    events.clear()
    # different args with the Safari token → refused, token burned
    await svc.handle_tool_call(
        _act_cmd("voice-1", "c2", "app.quit", args={"name": "Mail"}, confirm_token=token)
    )
    assert ran == []
    (r,) = events_of(events, "voice_tool_result")
    assert r["ok"] is False and "needsConfirm" in r


async def test_confirm_token_expires(tmp_path):
    ran = []

    async def run(args):
        ran.append(args)
        return FakeResult(ok=True, summary="quit")

    svc, events = make_service(tmp_path, verbs=[FakeVerb("app.quit", run=run, tier="confirm")])
    await svc.handle_tool_call(_act_cmd("voice-1", "c1", "app.quit", args={"name": "Safari"}))
    token = events_of(events, "voice_tool_result")[0]["needsConfirm"]["token"]
    # force the 90 s TTL past
    verb, args_hash, _deadline = svc._confirm_tokens[token]
    svc._confirm_tokens[token] = (verb, args_hash, time.monotonic() - 1)
    events.clear()
    await svc.handle_tool_call(
        _act_cmd("voice-1", "c2", "app.quit", args={"name": "Safari"}, confirm_token=token)
    )
    assert ran == []
    (r,) = events_of(events, "voice_tool_result")
    assert r["ok"] is False and "needsConfirm" in r


# ── undo ──────────────────────────────────────────────────────────────────


async def test_undo_cycle(tmp_path):
    undone = []

    async def undo():
        undone.append(True)
        return FakeResult(ok=True, summary="volume restored to 40")

    async def run(args):
        return FakeResult(ok=True, summary="volume 80", undo=undo)

    svc, events = make_service(tmp_path, verbs=[FakeVerb("system.volume", run=run)])
    await svc.handle_tool_call(_act_cmd("voice-1", "c1", "system.volume", args={"level": 80}))
    receipt = events_of(events, "voice_receipt")[0]["receipt"]
    assert receipt["undoable"] is True
    events.clear()

    await svc.handle_undo({"receiptId": receipt["id"]})
    assert undone == [True]
    receipt_events = events_of(events, "voice_receipt")
    # undo lane receipt + original re-emitted with undone=true
    lanes = {e["receipt"]["lane"] for e in receipt_events}
    assert "undo" in lanes
    original = [e["receipt"] for e in receipt_events if e["receipt"]["id"] == receipt["id"]]
    assert original and original[0]["undone"] is True
    undo_receipts = [e["receipt"] for e in receipt_events if e["receipt"]["lane"] == "undo"]
    assert undo_receipts[0]["ok"] is True
    assert undo_receipts[0]["verb"] == "system.volume"
    assert undo_receipts[0]["undoable"] is False
    (result,) = events_of(events, "voice_tool_result")
    assert result["ok"] is True
    assert result["callId"].startswith("undo-")
    # persisted store agrees
    stored = {r.id: r for r in svc.receipts.recent(limit=10)}
    assert stored[receipt["id"]].undone is True

    # second undo of the same receipt: closure is gone
    events.clear()
    await svc.handle_undo({"receiptId": receipt["id"]})
    (r2,) = events_of(events, "voice_tool_result")
    assert r2["ok"] is False
    assert json.loads(r2["output"])["error"] == "not_undoable"


async def test_undo_unknown_receipt(tmp_path):
    svc, events = make_service(tmp_path)
    await svc.handle_undo({"receiptId": "rcpt-nope"})
    (r,) = events_of(events, "voice_tool_result")
    assert r["ok"] is False
    assert json.loads(r["output"])["error"] == "not_undoable"


async def test_undo_failure_keeps_closure_for_retry(tmp_path):
    attempts = []

    async def undo():
        attempts.append(True)
        if len(attempts) == 1:
            raise RuntimeError("transient")
        return FakeResult(ok=True, summary="restored")

    async def run(args):
        return FakeResult(ok=True, summary="did it", undo=undo)

    svc, events = make_service(tmp_path, verbs=[FakeVerb("system.volume", run=run)])
    await svc.handle_tool_call(_act_cmd("voice-1", "c1", "system.volume", args={}))
    receipt_id = events_of(events, "voice_receipt")[0]["receipt"]["id"]
    events.clear()

    await svc.handle_undo({"receiptId": receipt_id})
    (r1,) = events_of(events, "voice_tool_result")
    assert r1["ok"] is False and "transient" in r1["output"]
    # the failed action is NOT marked undone
    stored = {r.id: r for r in svc.receipts.recent(limit=10)}
    assert stored[receipt_id].undone is False
    events.clear()

    # retry succeeds — the closure was re-remembered
    await svc.handle_undo({"receiptId": receipt_id})
    (r2,) = events_of(events, "voice_tool_result")
    assert r2["ok"] is True
    assert len(attempts) == 2


# ── typed commands (floor) ────────────────────────────────────────────────


async def test_typed_command_floor_verb(tmp_path):
    ran = []

    async def run(args):
        ran.append(args)
        return FakeResult(ok=True, summary="paused")

    svc, events = make_service(tmp_path, verbs=[FakeVerb("spotify.pause", run=run)])
    await svc.handle_typed_command({"text": "pause the music"})
    assert ran == [{}]
    (result,) = events_of(events, "voice_tool_result")
    assert result["ok"] is True
    assert result["callId"].startswith("typed-")
    (receipt_ev,) = events_of(events, "voice_receipt")
    assert receipt_ev["receipt"]["lane"] == "floor"
    assert receipt_ev["receipt"]["heard"] == "pause the music"


async def test_typed_command_non_floor_refused(tmp_path):
    svc, events = make_service(tmp_path, verbs=[FakeVerb("spotify.pause", run=_ok_run("p"))])
    await svc.handle_typed_command({"text": "email bob about the launch"})
    (result,) = events_of(events, "voice_tool_result")
    assert result["ok"] is False
    output = json.loads(result["output"])
    assert output["error"] == "not_floor"
    # no verb attempted, no receipt, and definitely no model involved
    assert not events_of(events, "voice_receipt")


async def test_typed_command_panic(tmp_path):
    svc, events = make_service(tmp_path)
    svc._active_session_id = "voice-live"
    await svc.handle_typed_command({"text": "stop"})
    (panic,) = events_of(events, "voice_panic")
    assert panic["voiceSessionId"] == "voice-live"
    assert panic["matched"] == "stop"
    assert not events_of(events, "voice_tool_result")


# ── transcripts + panic scan ──────────────────────────────────────────────


async def test_transcript_journal_finals_only(tmp_path):
    svc, events = make_service(tmp_path)
    await svc.handle_transcript(
        {"voiceSessionId": "v1", "role": "user", "text": "play vien", "final": False}
    )
    await svc.handle_transcript(
        {"voiceSessionId": "v1", "role": "user", "text": "play vienna", "final": True}
    )
    await svc.handle_transcript(
        {"voiceSessionId": "v1", "role": "assistant", "text": "Vienna, playing.", "final": True}
    )
    lines = [
        json.loads(line)
        for line in (tmp_path / "voice" / "transcripts.jsonl").read_text().splitlines()
    ]
    assert [(entry["role"], entry["text"]) for entry in lines] == [
        ("user", "play vienna"),
        ("assistant", "Vienna, playing."),
    ]
    assert all(entry["voiceSessionId"] == "v1" for entry in lines)


async def test_transcript_panic_on_partial_dedupes_per_session(tmp_path):
    svc, events = make_service(tmp_path)
    await svc.handle_transcript(
        {"voiceSessionId": "v1", "role": "user", "text": "freyja stop", "final": False}
    )
    await svc.handle_transcript(
        {"voiceSessionId": "v1", "role": "user", "text": "freyja stop", "final": True}
    )
    panics = events_of(events, "voice_panic")
    assert len(panics) == 1
    assert panics[0]["voiceSessionId"] == "v1"
    assert panics[0]["matched"] == "stop"
    # a different session panics independently
    await svc.handle_transcript(
        {"voiceSessionId": "v2", "role": "user", "text": "stop", "final": False}
    )
    assert len(events_of(events, "voice_panic")) == 2


async def test_transcript_assistant_text_never_panics(tmp_path):
    svc, events = make_service(tmp_path)
    await svc.handle_transcript(
        {"voiceSessionId": "v1", "role": "assistant", "text": "stop", "final": True}
    )
    assert not events_of(events, "voice_panic")


# ── receipts list ─────────────────────────────────────────────────────────


async def test_receipts_list(tmp_path):
    svc, events = make_service(
        tmp_path,
        verbs=[
            FakeVerb("spotify.pause", run=_ok_run("paused")),
            FakeVerb("spotify.next", run=_ok_run("next")),
        ],
    )
    await svc.handle_tool_call(_act_cmd("v1", "c1", "spotify.pause"))
    await svc.handle_tool_call(_act_cmd("v1", "c2", "spotify.next"))
    events.clear()
    await svc.handle_receipts_list({"limit": 1})
    (ev,) = events_of(events, "voice_receipts")
    assert len(ev["receipts"]) == 1
    assert ev["receipts"][0]["verb"] == "spotify.next"  # newest first
    events.clear()
    await svc.handle_receipts_list({})
    (ev2,) = events_of(events, "voice_receipts")
    assert [r["verb"] for r in ev2["receipts"]] == ["spotify.next", "spotify.pause"]


# ── mission.spawn wiring (fake state; real registration path is B's) ─────


async def test_mission_lane_for_mission_spawn(tmp_path):
    """mission.spawn calls land in the `mission` receipt lane."""

    async def run(args):
        return FakeResult(ok=True, summary="mission spawned: test", data={"sessionId": "s1"})

    svc, events = make_service(tmp_path, verbs=[FakeVerb("mission.spawn", run=run)])
    await svc.handle_tool_call(
        _act_cmd("v1", "c1", "mission.spawn", args={"prompt": "do the thing"})
    )
    (receipt_ev,) = events_of(events, "voice_receipt")
    assert receipt_ev["receipt"]["lane"] == "mission"
    (result,) = events_of(events, "voice_tool_result")
    assert json.loads(result["output"])["data"] == {"sessionId": "s1"}


# ── spawn() error surfacing ───────────────────────────────────────────────


async def test_spawn_surfaces_handler_exceptions_as_voice_error(tmp_path):
    svc, events = make_service(tmp_path)

    async def boom():
        raise RuntimeError("kaput")

    task = svc.spawn("tool_call", boom())
    with pytest.raises(RuntimeError):
        await task
    await asyncio.sleep(0)  # let the done-callback fire
    errors = events_of(events, "voice_error")
    assert errors and errors[0]["code"] == "tool_call_failed"
    assert "kaput" in errors[0]["message"]


# ── mission machinery: spawn + report-back, status, computer.do ───────────
# (slice 2). Real service-side registration runs against the FakeRegistry;
# _dispatch_turn is the seam standing in for freyja_bridge's
# _schedule_or_queue_turn, planting a controllable pending_task exactly
# like the real one does.


def _mission_state(computer_enabled=True):
    sessions = {}

    async def ensure_session(session_id, model_id=None, **kwargs):
        sess = SimpleNamespace(
            id=session_id,
            model_id=model_id,
            pending_task=None,
            session=SimpleNamespace(transcript=SimpleNamespace(entries=[])),
        )
        sessions[session_id] = sess
        return sess

    return SimpleNamespace(
        default_model="test-model",
        computer_enabled=computer_enabled,
        ensure_session=ensure_session,
        sessions=sessions,
    )


def _entry(role, content):
    return SimpleNamespace(message=SimpleNamespace(role=role, content=content))


def _mission_rig(tmp_path, monkeypatch, *, computer_enabled=True):
    """Service with real service-verb registration + fake dispatch/notify.

    Returns (svc, events, rig) where rig.gate releases the mission turn,
    rig.finish(text)/rig.fail(exc) choose its outcome, rig.prompts logs
    dispatched prompts, rig.notes logs notifications."""
    state = _mission_state(computer_enabled=computer_enabled)
    svc, events = make_service(tmp_path, state=state)
    svc._register_service_verbs(svc._registry)

    rig = SimpleNamespace(
        state=state,
        gate=asyncio.Event(),
        prompts=[],
        notes=[],
        final_text="mission report text",
        exc=None,
    )

    async def fake_notify(title, text):
        rig.notes.append((title, text))
        return True, ""

    monkeypatch.setattr(voice_service_module, "_post_notification", fake_notify)

    def fake_dispatch(sess, prompt):
        rig.prompts.append(prompt)

        async def turn():
            await rig.gate.wait()
            if rig.exc is not None:
                raise rig.exc
            sess.session.transcript.entries.append(_entry("user", prompt))
            sess.session.transcript.entries.append(_entry("assistant", rig.final_text))

        sess.pending_task = asyncio.create_task(turn())

    monkeypatch.setattr(svc, "_dispatch_turn", fake_dispatch)
    return svc, events, rig


async def _drain_watchers(svc):
    tasks = list(svc._tasks)
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


async def test_mission_spawn_report_back(tmp_path, monkeypatch):
    svc, events, rig = _mission_rig(tmp_path, monkeypatch)
    svc._active_session_id = "voice-live"
    rig.final_text = "Deploy is green and the PR is merged."

    await svc.handle_tool_call(
        _act_cmd(
            "voice-live",
            "c1",
            "mission.spawn",
            args={"prompt": "check the deploy status and merge the PR"},
        )
    )
    (result,) = events_of(events, "voice_tool_result")
    assert result["ok"] is True
    body = json.loads(result["output"])
    session_id = body["data"]["sessionId"]
    # Title = first ~6 words of the prompt.
    title = "check the deploy status and merge…"
    assert body["summary"] == f"mission spawned: {title}"
    assert rig.state.sessions[session_id].model_id == "test-model"

    # Report-back: release the turn, let the watcher finish.
    events.clear()
    rig.gate.set()
    await _drain_watchers(svc)

    receipts = [e["receipt"] for e in events_of(events, "voice_receipt")]
    (report,) = [r for r in receipts if r["verb"] == "mission.report"]
    assert report["lane"] == "mission"
    assert report["ok"] is True
    assert report["undoable"] is False
    assert report["summary"] == f"{title}: Deploy is green and the PR is merged."

    assert rig.notes == [("Freyja — mission", report["summary"])]

    (update,) = events_of(events, "voice_mission_update")
    assert update == {
        "type": "voice_mission_update",
        "voiceSessionId": "voice-live",
        "missionSessionId": session_id,
        "title": title,
        "text": "Deploy is green and the PR is merged.",
    }


async def test_mission_report_back_failure(tmp_path, monkeypatch):
    svc, events, rig = _mission_rig(tmp_path, monkeypatch)
    rig.exc = RuntimeError("turn exploded")

    await svc.handle_tool_call(
        _act_cmd("v1", "c1", "mission.spawn", args={"prompt": "do the risky thing"})
    )
    events.clear()
    rig.gate.set()
    await _drain_watchers(svc)

    receipts = [e["receipt"] for e in events_of(events, "voice_receipt")]
    (report,) = [r for r in receipts if r["verb"] == "mission.report"]
    assert report["ok"] is False
    assert "turn exploded" in report["summary"]
    (update,) = events_of(events, "voice_mission_update")
    assert update["voiceSessionId"] == ""  # no live voice session at report time
    assert "turn exploded" in update["text"]
    # The watcher failure must not surface as a voice_error crash.
    assert not events_of(events, "voice_error")


async def test_mission_status_counts_states(tmp_path, monkeypatch):
    svc, events, rig = _mission_rig(tmp_path, monkeypatch)

    await svc.handle_tool_call(_act_cmd("v1", "c0", "mission.status"))
    assert json.loads(events_of(events, "voice_tool_result")[0]["output"])["summary"] == (
        "no missions yet"
    )
    events.clear()

    await svc.handle_tool_call(
        _act_cmd("v1", "c1", "mission.spawn", args={"prompt": "first mission"})
    )
    events.clear()
    await svc.handle_tool_call(_act_cmd("v1", "c2", "mission.status"))
    (st,) = events_of(events, "voice_tool_result")
    body = json.loads(st["output"])
    assert body["summary"] == "1 running"
    (mission,) = body["data"]["missions"]
    assert mission["state"] == "running"
    assert mission["title"] == "first mission"
    assert mission["prompt_head"] == "first mission"
    # status receipts ride the mission lane
    (receipt_ev,) = events_of(events, "voice_receipt")
    assert receipt_ev["receipt"]["lane"] == "mission"

    events.clear()
    rig.gate.set()
    await _drain_watchers(svc)
    events.clear()
    await svc.handle_tool_call(_act_cmd("v1", "c3", "mission.status"))
    body = json.loads(events_of(events, "voice_tool_result")[0]["output"])
    assert body["summary"] == "1 done"
    assert body["data"]["missions"][0]["last_text"] == "mission report text"


async def test_computer_do_disabled_refuses_after_confirm(tmp_path, monkeypatch):
    svc, events, rig = _mission_rig(tmp_path, monkeypatch, computer_enabled=False)

    # Confirm tier: first call never runs the verb.
    await svc.handle_tool_call(
        _act_cmd("v1", "c1", "computer.do", args={"task": "open the settings pane"})
    )
    (r1,) = events_of(events, "voice_tool_result")
    assert r1["ok"] is False
    assert r1["needsConfirm"]["summary"] == "Drive the Mac: open the settings pane"
    token = r1["needsConfirm"]["token"]

    # Confirmed — but the gate is off, so it refuses with setup guidance.
    events.clear()
    await svc.handle_tool_call(
        _act_cmd(
            "v1",
            "c2",
            "computer.do",
            args={"task": "open the settings pane"},
            confirm_token=token,
        )
    )
    (r2,) = events_of(events, "voice_tool_result")
    assert r2["ok"] is False
    body = json.loads(r2["output"])
    assert body["summary"] == "computer control is disabled — enable it in settings"
    assert body["data"] == {"setup": "computer"}
    assert rig.state.sessions == {}  # nothing was spawned
    assert rig.prompts == []


async def test_computer_do_enabled_spawns_computer_mission(tmp_path, monkeypatch):
    svc, events, rig = _mission_rig(tmp_path, monkeypatch, computer_enabled=True)

    await svc.handle_tool_call(
        _act_cmd("v1", "c1", "computer.do", args={"task": "open the settings pane"})
    )
    token = events_of(events, "voice_tool_result")[0]["needsConfirm"]["token"]
    events.clear()
    await svc.handle_tool_call(
        _act_cmd(
            "v1",
            "c2",
            "computer.do",
            args={"task": "open the settings pane"},
            confirm_token=token,
        )
    )
    (r2,) = events_of(events, "voice_tool_result")
    assert r2["ok"] is True
    body = json.loads(r2["output"])
    title = "computer: open the settings pane"
    assert body["summary"] == f"mission spawned: {title}"
    session_id = body["data"]["sessionId"]
    assert session_id in rig.state.sessions
    # The mission prompt instructs computer-tool use around the task.
    (prompt,) = rig.prompts
    assert "screenshot, click, type, read_ax_tree" in prompt
    assert "open the settings pane" in prompt
    assert "stop when the task is done" in prompt
    (receipt_ev,) = events_of(events, "voice_receipt")
    assert receipt_ev["receipt"]["lane"] == "mission"

    # Same report-back watcher as mission.spawn.
    events.clear()
    rig.gate.set()
    await _drain_watchers(svc)
    (update,) = events_of(events, "voice_mission_update")
    assert update["title"] == title
    assert update["missionSessionId"] == session_id


# ── build_context_summary (continue a voice session by text) ─────────────


async def test_build_context_summary_recaps_the_exchange(tmp_path):
    svc, _ = make_service(tmp_path)
    tp = svc._transcripts_path
    tp.parent.mkdir(parents=True, exist_ok=True)
    rows = [
        {"ts": 1, "voiceSessionId": "voice-aaaaaaaaaaaa", "role": "user", "text": "play vienna"},
        {"ts": 2, "voiceSessionId": "voice-aaaaaaaaaaaa", "role": "tool", "verb": "spotify.play", "ok": True, "text": "▶ Vienna"},
        {"ts": 3, "voiceSessionId": "voice-aaaaaaaaaaaa", "role": "assistant", "text": "Playing."},
        {"ts": 4, "voiceSessionId": "voice-bbbbbbbbbbbb", "role": "user", "text": "other session"},
    ]
    tp.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    s = svc.build_context_summary("voice-aaaaaaaaaaaa")
    assert "You said: play vienna" in s
    assert "[did spotify.play: ▶ Vienna]" in s
    assert "Freyja said: Playing." in s
    assert "other session" not in s  # scoped to the one session


async def test_build_context_summary_marks_failures(tmp_path):
    svc, _ = make_service(tmp_path)
    tp = svc._transcripts_path
    tp.parent.mkdir(parents=True, exist_ok=True)
    tp.write_text(
        json.dumps({"voiceSessionId": "voice-cccccccccccc", "role": "tool", "verb": "app.quit", "ok": False, "text": "couldn't quit"}) + "\n",
        encoding="utf-8",
    )
    s = svc.build_context_summary("voice-cccccccccccc")
    assert "[did app.quit — failed: couldn't quit]" in s


async def test_build_context_summary_empty_and_missing(tmp_path):
    svc, _ = make_service(tmp_path)
    assert svc.build_context_summary("") == ""
    assert svc.build_context_summary("voice-none") == ""  # no journal file yet


async def test_build_context_summary_keeps_recent_tail_when_long(tmp_path):
    svc, _ = make_service(tmp_path)
    tp = svc._transcripts_path
    tp.parent.mkdir(parents=True, exist_ok=True)
    rows = [
        {"voiceSessionId": "voice-dddddddddddd", "role": "user", "text": f"line {i} " + "x" * 100}
        for i in range(100)
    ]
    tp.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    s = svc.build_context_summary("voice-dddddddddddd", limit_chars=500)
    assert s.startswith("…\n")
    assert len(s) <= 502
    assert "line 99" in s  # the tail is kept
