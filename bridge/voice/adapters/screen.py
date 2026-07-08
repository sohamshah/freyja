"""Screen-look verb (slice 2): screen.look — Freyja's eyes.

The morning-session gap this closes: "come check this out" had no verb —
the voice brain literally could not see the screen. ``screen.look``
captures the display via ``screencapture`` (works inside the packaged
app, whose bundle owns the Screen Recording TCC grant; bare dev runs may
be denied → clean failure, never a crash), downscales it to keep vision
tokens sane, and asks a one-shot vision model what matters.

The realtime voice model never receives the image — only the two-line
answer rides back through the verb result, so the exchange stays cheap
and the screenshot never leaves this process except as the single vision
call. The temp file is deleted in ``finally`` regardless of outcome.
"""

from __future__ import annotations

import asyncio
import base64
import io
import os
import tempfile
from typing import Any

import httpx

from bridge.voice.adapters import mac
from bridge.voice.verbs import Verb, VerbRegistry, VerbResult

_COMPLETIONS_URL = "https://api.openai.com/v1/chat/completions"
_DEFAULT_LOOK_MODEL = "gpt-5-mini"
_MAX_WIDTH = 1568  # vision models tile beyond this — bigger is just more tokens
_JPEG_QUALITY = 70
_LOOK_TIMEOUT_SEC = 25.0
_CAPTURE_FAIL_SUMMARY = "screen capture unavailable — needs Screen Recording permission"
_DEFAULT_QUESTION = "What matters on this screen right now?"

_SYSTEM_PROMPT = (
    "You are Freyja's eyes. Two sentences max, terse, letterpress; "
    "answer the question or describe what matters."
)


def _downscale(path: str) -> bytes:
    """Screenshot → JPEG bytes, ≤ _MAX_WIDTH wide. Runs in a thread (PIL
    is synchronous); module-level so tests can monkeypatch the seam."""
    from PIL import Image  # lazy — keep adapter import free of PIL at boot

    with Image.open(path) as img:
        rgb = img.convert("RGB")
        if rgb.width > _MAX_WIDTH:
            height = max(1, round(rgb.height * _MAX_WIDTH / rgb.width))
            rgb = rgb.resize((_MAX_WIDTH, height))
        buf = io.BytesIO()
        rgb.save(buf, format="JPEG", quality=_JPEG_QUALITY)
        return buf.getvalue()


async def _look(args: dict[str, Any]) -> VerbResult:
    question = str(args.get("question") or "").strip()
    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not api_key:
        # A voice session can't even mint without the key, so this only
        # fires on typed-floor/dev paths — still, fail with words.
        return VerbResult(ok=False, summary="screen look needs OPENAI_API_KEY", error="no_api_key")
    fd, tmppath = tempfile.mkstemp(prefix="freyja-look-", suffix=".png")
    os.close(fd)
    try:
        # -x: no shutter sound; -C: include the cursor (often the point).
        ok, out = await mac.run_exec(["screencapture", "-x", "-C", tmppath])
        captured = False
        if ok:
            try:
                captured = os.path.getsize(tmppath) > 0
            except OSError:
                captured = False
        if not captured:
            return VerbResult(
                ok=False,
                summary=_CAPTURE_FAIL_SUMMARY,
                error=out or "screencapture wrote nothing",
            )
        jpeg = await asyncio.to_thread(_downscale, tmppath)
        b64 = base64.b64encode(jpeg).decode("ascii")
        payload = {
            "model": os.environ.get("FREYJA_VOICE_LOOK_MODEL", "").strip()
            or _DEFAULT_LOOK_MODEL,
            "messages": [
                {"role": "system", "content": _SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:image/jpeg;base64,{b64}"},
                        },
                        {"type": "text", "text": question or _DEFAULT_QUESTION},
                    ],
                },
            ],
        }
        async with httpx.AsyncClient(timeout=_LOOK_TIMEOUT_SEC) as client:
            resp = await client.post(
                _COMPLETIONS_URL,
                headers={"Authorization": f"Bearer {api_key}"},
                json=payload,
            )
        if resp.status_code >= 400:
            return VerbResult(
                ok=False,
                summary=f"look failed: OpenAI returned {resp.status_code}",
                # Deliberately NOT the response body: provider error JSON
                # is third-party text that would land verbatim in receipts
                # and transcripts (it can even echo a redacted key prefix).
                error=f"vision call failed (HTTP {resp.status_code})",
            )
        body = resp.json()
        choices = body.get("choices") or [{}]
        text = str((choices[0].get("message") or {}).get("content") or "").strip()
        if not text:
            return VerbResult(ok=False, summary="look came back empty", error="empty_response")
        return VerbResult(ok=True, summary=text[:80], data={"text": text})
    except Exception as exc:  # noqa: BLE001 — network/PIL failures become terse summaries
        err = str(exc).splitlines()[0][:120] if str(exc) else exc.__class__.__name__
        return VerbResult(ok=False, summary=f"look failed: {err}", error=err)
    finally:
        try:
            os.unlink(tmppath)
        except OSError:
            pass


def register(registry: VerbRegistry) -> None:
    registry.register(
        Verb(
            name="screen.look",
            description="Look at the screen and answer a question about it (or describe it)",
            params={
                "question": {
                    "type": "string",
                    "description": "what to look for; omit for a general description",
                }
            },
            required=[],
            tier="auto",
            run=_look,
        )
    )
