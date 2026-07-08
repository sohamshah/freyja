"""Galdr voice service — the bridge-side brain of the voice agent.

Owns everything about voice that is NOT audio: minting ephemeral client
secrets (the API key never leaves this process — the renderer only ever
holds a ~10-minute ephemeral value), dispatching `act` tool calls through
the verb registry, tier enforcement with confirm tokens, receipts + undo,
the typed-command floor, transcript journaling, and panic detection.
Audio itself lives entirely in the renderer (WebRTC ⇄ OpenAI Realtime).

Import discipline: ``bridge.freyja_bridge`` (for ``emit``/``log``/
``_schedule_or_queue_turn``) and ``bridge.voice.verbs`` (built by the
adapters agent) are imported lazily inside methods — the former to avoid
a module cycle at boot, the latter so this module is importable and
testable before/without the adapters landing.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import secrets
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Optional

import httpx

from bridge.voice.floor import parse as floor_parse
from bridge.voice.floor import scan_for_panic
from bridge.voice.prompts import build_instructions
from bridge.voice.receipts import Receipt, ReceiptStore, UndoLedger

_MINT_URL = "https://api.openai.com/v1/realtime/client_secrets"
_WEBRTC_URL = "https://api.openai.com/v1/realtime/calls"
_TRANSCRIPTION_MODEL = "gpt-realtime-whisper"
_CONFIRM_TTL_SEC = 90.0

# Persisted config keys — everything else in the VoiceConfig payload
# (available/*, capability flags) is computed live, never written.
_CONFIG_KEYS = ("enabled", "model", "voice", "vadMode", "idleTimeoutSec")
_CONFIG_DEFAULTS: dict[str, Any] = {
    "enabled": True,
    "model": "gpt-realtime-2.1-mini",
    "voice": "marin",
    "vadMode": "semantic_vad",
    "idleTimeoutSec": 25,
}
_AVAILABLE_MODELS = (
    "gpt-realtime-2.1-mini",
    "gpt-realtime-2.1",
    "gpt-realtime",
    "gpt-realtime-mini",
)
_AVAILABLE_VOICES = ("marin", "cedar", "alloy", "echo", "shimmer", "coral")
_VAD_MODES = ("semantic_vad", "server_vad")


def _args_hash(args: dict[str, Any]) -> str:
    canonical = json.dumps(args or {}, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# Human templates for confirm-tier verbs. The confirm summary is spoken
# aloud by the model and shown verbatim in the HUD confirm row, so it
# must read as prose ("Quit Slack"), not as a machine dump
# ("app.quit name=Slack"). Verbs without a template (or whose template
# raises on odd args) fall back to the raw form; the raw verb+args
# always live on the receipt's args field regardless.
_CONFIRM_SUMMARY_TEMPLATES: dict[str, Any] = {
    "app.quit": lambda args: f"Quit {args['name']}",
}


def _describe(verb: str, args: dict[str, Any]) -> str:
    """Human line for confirm prompts: `Quit Slack`."""
    template = _CONFIRM_SUMMARY_TEMPLATES.get(verb)
    if template is not None:
        try:
            return str(template(args or {}))
        except Exception:  # noqa: BLE001 — fall back to the raw form
            pass
    if not args:
        return verb
    parts = " ".join(f"{k}={v}" for k, v in args.items())
    return f"{verb} {parts}"


class VoiceService:
    """Process-level voice service, hung off ``_BridgeState.voice``.

    ``start()``/``stop()`` are cheap and non-fatal by contract — no
    network until the first ``voice_session_start``. Test seams: the
    keyword-only constructor args inject the storage dir, a fake verb
    registry, and an emit collector.
    """

    def __init__(
        self,
        state: Any,
        *,
        base_dir: Optional[Path] = None,
        registry: Any = None,
        emit_fn: Any = None,
    ) -> None:
        self._state = state
        self._base = Path(base_dir) if base_dir is not None else Path.home() / ".freyja" / "voice"
        self._config_path = self._base / "config.json"
        self._transcripts_path = self._base / "transcripts.jsonl"
        self.receipts = ReceiptStore(self._base / "receipts.jsonl")
        self.undo_ledger = UndoLedger(capacity=20)
        self._registry = registry
        self._emit_fn = emit_fn
        self._config: dict[str, Any] = dict(_CONFIG_DEFAULTS)
        # Strong refs — asyncio holds only weak refs to tasks, and a
        # GC'd task would silently drop a mid-flight verb execution.
        self._tasks: set[asyncio.Task] = set()
        # token -> (verb, args-hash, monotonic deadline); single-use.
        self._confirm_tokens: dict[str, tuple[str, str, float]] = {}
        self._active_session_id: Optional[str] = None
        self._session_started_at: float = 0.0
        self._session_receipt_count: int = 0
        # Mint generation counter: a voice_session_start that finishes
        # minting after a newer start began is stale — its result is
        # dropped so the renderer never sees two competing ready events.
        self._mint_generation: int = 0
        # Panic dedupe: one voice_panic per session — the renderer ends
        # the session on panic, so per-session ≡ per-utterance here.
        self._panicked_sessions: set[str] = set()

    # ── plumbing ─────────────────────────────────────────────────────────

    def _emit(self, event: dict[str, Any]) -> None:
        fn = self._emit_fn
        if fn is None:
            # Lazy — freyja_bridge imports this module at boot; importing
            # it back at module scope would cycle.
            from bridge.freyja_bridge import emit as fn

            self._emit_fn = fn
        fn(event)

    def _log(self, level: str, message: str) -> None:
        try:
            from bridge.freyja_bridge import log

            log(level, message)
        except Exception:  # noqa: BLE001 — logging must never take voice down
            print(f"[voice:{level}] {message}", file=sys.stderr)

    def spawn(self, name: str, coro: Any) -> asyncio.Task:
        """Run a handler as a task so the bridge command loop never blocks
        behind a verb (osascript runs take seconds). Task-internal failures
        surface as voice_error — the try/except in _handle_command only
        covers synchronous task creation."""
        task = asyncio.create_task(coro, name=f"voice-{name}")
        self._tasks.add(task)

        def _done(t: asyncio.Task) -> None:
            self._tasks.discard(t)
            if t.cancelled():
                return
            exc = t.exception()
            if exc is not None:
                self._log("warn", f"voice {name} handler failed: {exc}")
                try:
                    self._emit(
                        {
                            "type": "voice_error",
                            "code": f"{name}_failed",
                            "message": str(exc),
                        }
                    )
                except Exception:  # noqa: BLE001
                    pass

        task.add_done_callback(_done)
        return task

    def _ensure_registry(self) -> Any:
        # Lazy: bridge.voice.verbs is the adapters agent's module; tests
        # inject a fake via the constructor and never import it.
        if self._registry is None:
            from bridge.voice.verbs import build_default_registry

            registry = build_default_registry()
            self._registry = registry
            self._register_mission_spawn(registry)
        return self._registry

    # ── lifecycle ────────────────────────────────────────────────────────

    async def start(self) -> None:
        # Cheap + non-fatal by contract: config is one small disk read,
        # timers wiring is a setattr. No network — mint is lazy.
        try:
            self._config = self._load_config()
        except Exception as exc:  # noqa: BLE001
            self._log("warn", f"voice config load failed, using defaults: {exc}")
            self._config = dict(_CONFIG_DEFAULTS)
        try:
            from bridge.voice.adapters import timers

            timers.set_emitter(lambda ev: self._emit(ev))
        except Exception as exc:  # noqa: BLE001 — adapters may not be present
            self._log("debug", f"voice timers not wired: {exc}")

    async def stop(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        self._tasks.clear()
        self._confirm_tokens.clear()
        self._active_session_id = None

    # ── config ───────────────────────────────────────────────────────────

    def _load_config(self) -> dict[str, Any]:
        cfg = dict(_CONFIG_DEFAULTS)
        try:
            raw = json.loads(self._config_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return cfg
        except Exception:  # noqa: BLE001 — corrupt file must not brick voice
            return cfg
        if isinstance(raw, dict):
            self._apply_patch(cfg, raw)
        return cfg

    def _save_config(self) -> None:
        self._base.mkdir(parents=True, exist_ok=True)
        payload = {k: self._config[k] for k in _CONFIG_KEYS}
        tmp = self._config_path.with_name(self._config_path.name + ".tmp")
        tmp.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, self._config_path)

    @staticmethod
    def _apply_patch(cfg: dict[str, Any], patch: dict[str, Any]) -> None:
        """Validated merge of settable keys; junk is ignored, not fatal."""
        if isinstance(patch.get("enabled"), bool):
            cfg["enabled"] = patch["enabled"]
        # `model` is deliberately a free string (contract §10 reserves the
        # key for future non-OpenAI seats) — no allowlist check.
        if isinstance(patch.get("model"), str) and patch["model"].strip():
            cfg["model"] = patch["model"].strip()
        if isinstance(patch.get("voice"), str) and patch["voice"].strip():
            cfg["voice"] = patch["voice"].strip()
        if patch.get("vadMode") in _VAD_MODES:
            cfg["vadMode"] = patch["vadMode"]
        timeout = patch.get("idleTimeoutSec")
        if isinstance(timeout, (int, float)) and not isinstance(timeout, bool):
            cfg["idleTimeoutSec"] = int(max(5, min(600, timeout)))

    def get_config(self) -> dict[str, Any]:
        cfg = {k: self._config[k] for k in _CONFIG_KEYS}
        cfg["available"] = {
            "models": list(_AVAILABLE_MODELS),
            "voices": list(_AVAILABLE_VOICES),
        }
        cfg["hasApiKey"] = bool(os.environ.get("OPENAI_API_KEY", "").strip())
        cfg["spotifySearch"] = bool(
            os.environ.get("SPOTIFY_CLIENT_ID", "").strip()
            and os.environ.get("SPOTIFY_CLIENT_SECRET", "").strip()
        )
        return cfg

    async def handle_get_config(self, cmd: dict[str, Any]) -> None:
        self._emit({"type": "voice_config", "config": self.get_config()})

    async def handle_set_config(self, cmd: dict[str, Any]) -> None:
        patch = cmd.get("patch")
        if isinstance(patch, dict):
            self._apply_patch(self._config, patch)
            try:
                self._save_config()
            except Exception as exc:  # noqa: BLE001
                self._log("warn", f"voice config save failed: {exc}")
                self._emit(
                    {
                        "type": "voice_error",
                        "code": "config_save_failed",
                        "message": f"could not persist voice config: {exc}",
                    }
                )
        self._emit({"type": "voice_config", "config": self.get_config()})

    # ── session mint / close ─────────────────────────────────────────────

    def _build_session_config(self, registry: Any) -> dict[str, Any]:
        """The FULL session config bakes at mint (contract §0) — the
        renderer only ever needs the ephemeral value."""
        return {
            "type": "realtime",
            "model": str(self._config["model"]),
            "instructions": build_instructions(registry.catalog_markdown()),
            "output_modalities": ["audio"],
            "audio": {
                "input": {
                    "transcription": {"model": _TRANSCRIPTION_MODEL},
                    "turn_detection": {"type": str(self._config["vadMode"])},
                },
                "output": {"voice": str(self._config["voice"])},
            },
            "tools": [registry.openai_tool_schema()],
            "tool_choice": "auto",
        }

    async def _mint(self, api_key: str, payload: dict[str, Any]) -> dict[str, Any]:
        """POST /v1/realtime/client_secrets — 10 s timeout, one retry on
        network errors / 5xx; 4xx is terminal (a retry won't fix auth)."""
        last_exc: Optional[Exception] = None
        for _attempt in range(2):
            try:
                async with httpx.AsyncClient(timeout=10.0) as client:
                    resp = await client.post(
                        _MINT_URL,
                        headers={"Authorization": f"Bearer {api_key}"},
                        json=payload,
                    )
            except Exception as exc:  # noqa: BLE001 — network/timeout, retry once
                last_exc = exc
                continue
            if resp.status_code >= 500:
                last_exc = RuntimeError(f"OpenAI returned {resp.status_code}: {resp.text[:200]}")
                continue
            if resp.status_code >= 400:
                raise RuntimeError(f"OpenAI returned {resp.status_code}: {resp.text[:200]}")
            data = resp.json()
            if not isinstance(data, dict):
                raise RuntimeError("mint response was not a JSON object")
            return data
        raise last_exc if last_exc is not None else RuntimeError("mint failed")

    async def handle_session_start(self, cmd: dict[str, Any]) -> None:
        if not self._config.get("enabled", True):
            # The persisted disable is enforced HERE, not just in the UI:
            # Alt+Space is registered unconditionally (contract §7.4), so
            # without this gate a disabled config still opened a live mic.
            self._emit(
                {
                    "type": "voice_error",
                    "code": "voice_disabled",
                    "message": (
                        "voice is disabled in settings — enable it there "
                        "before starting a session."
                    ),
                }
            )
            return
        api_key = os.environ.get("OPENAI_API_KEY", "").strip()
        if not api_key:
            self._emit(
                {
                    "type": "voice_error",
                    "code": "no_api_key",
                    "message": (
                        "OPENAI_API_KEY is not set — add it to .env and "
                        "restart the bridge to enable voice."
                    ),
                }
            )
            return
        try:
            registry = self._ensure_registry()
            payload = {"session": self._build_session_config(registry)}
        except Exception as exc:  # noqa: BLE001 — e.g. verbs module missing
            self._emit(
                {
                    "type": "voice_error",
                    "code": "registry_unavailable",
                    "message": f"voice verb registry failed to build: {exc}",
                }
            )
            return
        self._mint_generation += 1
        generation = self._mint_generation
        try:
            data = await self._mint(api_key, payload)
        except Exception as exc:  # noqa: BLE001
            if generation != self._mint_generation:
                return  # superseded mid-flight — the newer start owns the reply
            self._emit(
                {
                    "type": "voice_error",
                    "code": "mint_failed",
                    "message": (
                        f"could not mint a realtime client secret: {exc} — "
                        "check network and OPENAI_API_KEY validity."
                    ),
                }
            )
            return
        if generation != self._mint_generation:
            # A newer voice_session_start superseded this mint while it
            # was in flight — drop the stale secret on the floor rather
            # than clobbering the newer session's bookkeeping.
            return
        value = data.get("value")
        if not value:
            self._emit(
                {
                    "type": "voice_error",
                    "code": "mint_failed",
                    "message": "mint response had no client secret value",
                }
            )
            return
        if self._active_session_id:
            # A new session replaces the old one: tell the renderer the
            # old id is dead so receipts/stats never split across two ids.
            self._emit(
                {
                    "type": "voice_session_closed",
                    "voiceSessionId": self._active_session_id,
                    "reason": "superseded",
                    "receiptsCount": self._session_receipt_count,
                    "seconds": (
                        round(time.time() - self._session_started_at, 1)
                        if self._session_started_at
                        else 0
                    ),
                }
            )
            self._panicked_sessions.discard(self._active_session_id)
        voice_session_id = f"voice-{uuid.uuid4().hex[:12]}"
        self._active_session_id = voice_session_id
        self._session_started_at = time.time()
        self._session_receipt_count = 0
        self._emit(
            {
                "type": "voice_session_ready",
                "voiceSessionId": voice_session_id,
                "clientSecret": value,
                "model": str(self._config["model"]),
                "expiresAt": data.get("expires_at"),
                "webrtcUrl": _WEBRTC_URL,
            }
        )

    async def handle_session_end(self, cmd: dict[str, Any]) -> None:
        voice_session_id = str(cmd.get("voiceSessionId") or self._active_session_id or "")
        reason = str(cmd.get("reason") or "ended")
        stats = cmd.get("stats") if isinstance(cmd.get("stats"), dict) else {}
        seconds = stats.get("seconds")
        is_active = bool(voice_session_id) and voice_session_id == self._active_session_id
        if not isinstance(seconds, (int, float)) or isinstance(seconds, bool):
            seconds = (
                round(time.time() - self._session_started_at, 1)
                if is_active and self._session_started_at
                else 0
            )
        self._emit(
            {
                "type": "voice_session_closed",
                "voiceSessionId": voice_session_id,
                "reason": reason,
                "receiptsCount": self._session_receipt_count if is_active else 0,
                "seconds": seconds,
            }
        )
        if is_active:
            self._active_session_id = None
            self._session_started_at = 0.0
            self._session_receipt_count = 0
        self._panicked_sessions.discard(voice_session_id)

    # ── act dispatch ─────────────────────────────────────────────────────

    async def handle_tool_call(self, cmd: dict[str, Any]) -> None:
        voice_session_id = cmd.get("voiceSessionId") or self._active_session_id
        call_id = str(cmd.get("callId") or f"call-{int(time.time() * 1000)}")
        name = cmd.get("name")
        heard = str(cmd.get("heard") or "")
        if name != "act":
            # Nothing was attempted against a verb — no receipt, just tell
            # the model to self-correct.
            self._emit_tool_result(
                voice_session_id,
                call_id,
                ok=False,
                output=json.dumps(
                    {
                        "ok": False,
                        "error": "unknown_tool",
                        "summary": f"only the `act` tool exists; got {name!r}",
                    }
                ),
            )
            return
        try:
            parsed = json.loads(cmd.get("argumentsJson") or "{}")
            if not isinstance(parsed, dict):
                raise ValueError("arguments must be a JSON object")
        except Exception as exc:  # noqa: BLE001
            self._emit_tool_result(
                voice_session_id,
                call_id,
                ok=False,
                output=json.dumps(
                    {
                        "ok": False,
                        "error": "bad_arguments",
                        "summary": f"argumentsJson was not a JSON object: {exc}",
                    }
                ),
            )
            return
        verb = str(parsed.get("verb") or "")
        args = dict(parsed.get("args")) if isinstance(parsed.get("args"), dict) else {}
        confirm_token = parsed.get("confirm_token")
        # The act schema puts confirm_token at top level, but the realtime
        # model reliably tucks it INSIDE args instead (observed live
        # 2026-07-08: a five-deep "awaiting confirmation" loop on app.quit —
        # every re-call carried a valid token in args and validation never
        # saw it). Accept both placements, and strip the key from args so
        # the verb and the scope hash see the same args the token was
        # issued against.
        args_token = args.pop("confirm_token", None)
        if not isinstance(confirm_token, str):
            confirm_token = args_token if isinstance(args_token, str) else None
        lane = "mission" if verb == "mission.spawn" else "brain"
        await self._execute(
            verb=verb,
            args=args,
            confirm_token=confirm_token,
            lane=lane,
            heard=heard,
            call_id=call_id,
            voice_session_id=voice_session_id,
        )

    async def _execute(
        self,
        *,
        verb: str,
        args: dict[str, Any],
        confirm_token: Optional[str],
        lane: str,
        heard: str,
        call_id: str,
        voice_session_id: Optional[str],
    ) -> None:
        """Shared execution core for brain (tool_call) and floor (typed)
        lanes: tier gate → run → receipt → voice_tool_result."""
        try:
            registry = self._ensure_registry()
        except Exception as exc:  # noqa: BLE001
            self._emit_tool_result(
                voice_session_id,
                call_id,
                ok=False,
                output=json.dumps(
                    {"ok": False, "error": "registry_unavailable", "summary": str(exc)}
                ),
            )
            return
        vb = registry.get(verb) if verb else None
        if vb is None:
            summary = f"unknown verb: {verb or '(empty)'}"
            receipt = self._record(
                voice_session_id=voice_session_id,
                heard=heard,
                lane=lane,
                verb=verb or "?",
                args=args,
                ok=False,
                summary=summary,
                undoable=False,
            )
            self._emit_tool_result(
                voice_session_id,
                call_id,
                ok=False,
                output=json.dumps(
                    {
                        "ok": False,
                        "error": "unknown_verb",
                        "summary": f"{summary} — use only verbs from the catalog",
                    }
                ),
                receipt=receipt,
            )
            return

        if getattr(vb, "tier", "auto") == "confirm":
            if not self._consume_confirm_token(confirm_token, verb, args):
                token = self._issue_confirm_token(verb, args)
                summary = _describe(verb, args)
                receipt = self._record(
                    voice_session_id=voice_session_id,
                    heard=heard,
                    lane=lane,
                    verb=verb,
                    args=args,
                    ok=False,
                    summary=f"awaiting confirmation: {summary}",
                    undoable=False,
                )
                # Pinned sentence (contract §4); the token value rides at
                # the end — the model has no other channel to learn it.
                output = (
                    f"CONFIRM REQUIRED: {summary}. Ask the user to confirm aloud, "
                    f"then call act again with confirm_token {token}."
                )
                self._emit_tool_result(
                    voice_session_id,
                    call_id,
                    ok=False,
                    output=output,
                    receipt=receipt,
                    needs_confirm={"token": token, "summary": summary},
                )
                return

        try:
            result = await vb.run(dict(args))
        except Exception as exc:  # noqa: BLE001 — adapter bugs become tool errors
            summary = f"{verb} failed: {exc}"
            receipt = self._record(
                voice_session_id=voice_session_id,
                heard=heard,
                lane=lane,
                verb=verb,
                args=args,
                ok=False,
                summary=summary,
                undoable=False,
            )
            self._emit_tool_result(
                voice_session_id,
                call_id,
                ok=False,
                output=json.dumps({"ok": False, "error": str(exc), "summary": summary}),
                receipt=receipt,
            )
            return

        ok = bool(getattr(result, "ok", False))
        summary = str(getattr(result, "summary", "") or "")
        undo = getattr(result, "undo", None)
        receipt = self._record(
            voice_session_id=voice_session_id,
            heard=heard,
            lane=lane,
            verb=verb,
            args=args,
            ok=ok,
            summary=summary,
            undoable=undo is not None,
        )
        if undo is not None:
            self.undo_ledger.remember(receipt.id, undo)
        body: dict[str, Any] = {"ok": ok, "summary": summary}
        data = getattr(result, "data", None)
        if data:
            body["data"] = data
        error = getattr(result, "error", None)
        if error:
            body["error"] = error
        self._emit_tool_result(
            voice_session_id,
            call_id,
            ok=ok,
            output=json.dumps(body, ensure_ascii=False, default=str),
            receipt=receipt,
            say=getattr(result, "say", None),
        )

    # ── confirm tokens ───────────────────────────────────────────────────

    def _issue_confirm_token(self, verb: str, args: dict[str, Any]) -> str:
        now = time.monotonic()
        # Prune expired tokens so refused confirms don't accumulate.
        self._confirm_tokens = {
            t: entry for t, entry in self._confirm_tokens.items() if entry[2] > now
        }
        token = secrets.token_hex(8)
        self._confirm_tokens[token] = (verb, _args_hash(args), now + _CONFIRM_TTL_SEC)
        return token

    def _consume_confirm_token(
        self, token: Optional[str], verb: str, args: dict[str, Any]
    ) -> bool:
        if not token:
            return False
        entry = self._confirm_tokens.get(token)
        if entry is None:
            return False
        entry_verb, entry_hash, deadline = entry
        if time.monotonic() > deadline:
            self._confirm_tokens.pop(token, None)
            return False
        if entry_verb != verb or entry_hash != _args_hash(args):
            # Scope mismatch does NOT burn the token: a model that mangles
            # one re-call (wrong arg spelling, extra key) can still succeed
            # on the next attempt within the TTL instead of forcing the
            # operator through a fresh confirmation round.
            return False
        # Single-use: consumed only on successful validation.
        self._confirm_tokens.pop(token, None)
        return True

    # ── typed commands (floor) ───────────────────────────────────────────

    async def handle_typed_command(self, cmd: dict[str, Any]) -> None:
        text = str(cmd.get("text") or "").strip()
        call_id = f"typed-{int(time.time() * 1000)}"
        intent = floor_parse(text)
        if intent is None:
            self._emit_tool_result(
                self._active_session_id,
                call_id,
                ok=False,
                output=json.dumps(
                    {
                        "ok": False,
                        "error": "not_floor",
                        "summary": (
                            "typed commands are floor-only: pause/resume/next/"
                            "previous, volume/mute, what's playing, stop"
                        ),
                    }
                ),
            )
            return
        if intent.panic:
            # Panic is a session-end signal, not a verb — the renderer
            # cancels the response and closes on voice_panic.
            self._emit_panic(self._active_session_id or "", matched=text)
            return
        await self._execute(
            verb=intent.verb,
            args=dict(intent.args),
            confirm_token=None,
            lane="floor",
            heard=text,
            call_id=call_id,
            voice_session_id=self._active_session_id,
        )

    # ── transcripts + panic ──────────────────────────────────────────────

    async def handle_transcript(self, cmd: dict[str, Any]) -> None:
        voice_session_id = str(cmd.get("voiceSessionId") or self._active_session_id or "")
        role = cmd.get("role")
        text = str(cmd.get("text") or "")
        final = bool(cmd.get("final"))
        if final and role in ("user", "assistant") and text.strip():
            self._journal_transcript_line(
                {
                    "voiceSessionId": voice_session_id,
                    "role": role,
                    "text": text,
                }
            )
        # Panic scans finals AND partials — a partial "stop" must cut the
        # assistant off mid-sentence, not after the transcript settles.
        if role == "user" and text.strip():
            matched = scan_for_panic(text)
            if matched:
                self._emit_panic(voice_session_id, matched)

    def _journal_transcript_line(self, entry: dict[str, Any]) -> None:
        """Append one line to transcripts.jsonl — the single chronological
        narrative of every voice session (user/assistant finals plus tool
        executions), the file a later reviewer reads to reconstruct what
        was said and what actually happened."""
        try:
            self._transcripts_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self._transcripts_path, "a", encoding="utf-8") as fh:
                fh.write(
                    json.dumps(
                        {"ts": int(time.time() * 1000), **entry}, ensure_ascii=False
                    )
                    + "\n"
                )
        except Exception as exc:  # noqa: BLE001
            self._log("warn", f"voice transcript journal failed: {exc}")

    def _emit_panic(self, voice_session_id: str, matched: str) -> None:
        if voice_session_id:
            if voice_session_id in self._panicked_sessions:
                return
            self._panicked_sessions.add(voice_session_id)
        self._emit(
            {
                "type": "voice_panic",
                "voiceSessionId": voice_session_id,
                "matched": matched,
            }
        )

    # ── receipts + undo ──────────────────────────────────────────────────

    async def handle_receipts_list(self, cmd: dict[str, Any]) -> None:
        limit = cmd.get("limit")
        if not isinstance(limit, int) or isinstance(limit, bool) or limit <= 0:
            limit = 50
        receipts = self.receipts.recent(min(limit, 500))
        self._emit(
            {
                "type": "voice_receipts",
                "receipts": [r.to_dict() for r in receipts],
            }
        )

    async def handle_undo(self, cmd: dict[str, Any]) -> None:
        receipt_id = str(cmd.get("receiptId") or "")
        call_id = f"undo-{int(time.time() * 1000)}"
        closure = self.undo_ledger.take(receipt_id)
        if closure is None:
            self._emit_tool_result(
                self._active_session_id,
                call_id,
                ok=False,
                output=json.dumps(
                    {
                        "ok": False,
                        "error": "not_undoable",
                        "summary": (
                            "nothing to undo — the action is not undoable, was "
                            "already undone, or its window (last 20 actions, "
                            "this bridge process) has passed"
                        ),
                    }
                ),
            )
            return
        try:
            result = await closure()
            ok = bool(getattr(result, "ok", False))
            summary = str(getattr(result, "summary", "") or "undone")
        except Exception as exc:  # noqa: BLE001
            ok = False
            summary = f"undo failed: {exc}"
        original: Optional[Receipt] = None
        if ok:
            try:
                original = self.receipts.mark_undone(receipt_id)
            except Exception as exc:  # noqa: BLE001
                self._log("warn", f"voice mark_undone failed: {exc}")
        else:
            # The pop is single-use; a failed undo puts the closure back so
            # the operator can retry (the action itself still stands).
            self.undo_ledger.remember(receipt_id, closure)
        undo_receipt = self._record(
            voice_session_id=self._active_session_id,
            heard="",
            lane="undo",
            verb=original.verb if original is not None else "undo",
            args={"receiptId": receipt_id},
            ok=ok,
            summary=summary,
            undoable=False,
        )
        if original is not None:
            # Re-emit the original so the UI can flip its row to undone.
            self._emit({"type": "voice_receipt", "receipt": original.to_dict()})
        self._emit_tool_result(
            self._active_session_id,
            call_id,
            ok=ok,
            output=json.dumps({"ok": ok, "summary": summary}, ensure_ascii=False),
            receipt=undo_receipt,
        )

    # ── mission.spawn (needs bridge session access, so it lives here) ────

    def _register_mission_spawn(self, registry: Any) -> None:
        from bridge.voice.verbs import Verb, VerbResult

        service = self

        async def _run(args: dict[str, Any]) -> Any:
            prompt = str(args.get("prompt") or "").strip()
            if not prompt:
                return VerbResult(
                    ok=False,
                    summary="mission needs a prompt",
                    error="missing_prompt",
                )
            title = str(args.get("title") or "").strip()
            # Lazy — freyja_bridge imports this module at boot.
            from bridge.freyja_bridge import _schedule_or_queue_turn

            session_id = f"voice-mission-{int(time.time() * 1000):x}"
            sess = await service._state.ensure_session(
                session_id, model_id=service._state.default_model
            )
            _schedule_or_queue_turn(sess, prompt, None)
            label = title or prompt[:40]
            return VerbResult(
                ok=True,
                summary=f"mission spawned: {label}",
                data={"sessionId": session_id},
            )

        registry.register(
            Verb(
                name="mission.spawn",
                description=(
                    "hand multi-step work to a full Freyja agent session; "
                    "returns the new sessionId"
                ),
                params={
                    "prompt": {
                        "type": "string",
                        "description": "complete, self-contained task brief",
                    },
                    "title": {"type": "string", "description": "short session label"},
                },
                required=["prompt"],
                tier="auto",
                run=_run,
            )
        )

    # ── shared emit helpers ──────────────────────────────────────────────

    def _record(
        self,
        *,
        voice_session_id: Optional[str],
        heard: str,
        lane: str,
        verb: str,
        args: dict[str, Any],
        ok: bool,
        summary: str,
        undoable: bool,
    ) -> Receipt:
        receipt = Receipt.new(
            heard=heard,
            lane=lane,
            verb=verb,
            args=args,
            ok=ok,
            summary=summary,
            undoable=undoable,
            voice_session_id=voice_session_id or None,
        )
        try:
            self.receipts.append(receipt)
        except Exception as exc:  # noqa: BLE001 — a full disk must not kill the verb
            self._log("warn", f"voice receipt append failed: {exc}")
        if voice_session_id and voice_session_id == self._active_session_id:
            self._session_receipt_count += 1
        # Interleave the execution into the transcript journal so one file
        # reads as the full session: what was said AND what was done.
        self._journal_transcript_line(
            {
                "voiceSessionId": voice_session_id or "",
                "role": "tool",
                "verb": verb,
                "ok": ok,
                "text": summary,
                "lane": lane,
            }
        )
        self._emit({"type": "voice_receipt", "receipt": receipt.to_dict()})
        return receipt

    def _emit_tool_result(
        self,
        voice_session_id: Optional[str],
        call_id: str,
        *,
        ok: bool,
        output: str,
        receipt: Optional[Receipt] = None,
        say: Optional[str] = None,
        needs_confirm: Optional[dict[str, Any]] = None,
    ) -> None:
        event: dict[str, Any] = {
            "type": "voice_tool_result",
            "callId": call_id,
            "ok": ok,
            "output": output,
        }
        if voice_session_id:
            event["voiceSessionId"] = voice_session_id
        if say:
            event["say"] = say
        if receipt is not None:
            event["receipt"] = receipt.to_dict()
        if needs_confirm is not None:
            event["needsConfirm"] = needs_confirm
        self._emit(event)
