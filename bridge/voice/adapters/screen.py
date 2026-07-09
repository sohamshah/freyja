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

# Grounding prompt for locate_in_image — returns a click point, not prose.
_LOCATE_PROMPT = (
    "You are a precise UI element locator. The user names one on-screen "
    "target. Reply with ONLY minified JSON and nothing else: "
    '{"found": true, "x": <0..1>, "y": <0..1>} where x,y are the CENTER '
    "of that target as fractions of image width and height (0,0 = top-"
    'left, 1,1 = bottom-right). If the target is not visible, reply '
    '{"found": false}. Never add prose, code fences, or explanation.'
)


async def _vision_json(image_b64: str, system: str, user_text: str) -> Any:
    """Shared one-shot vision call → parsed JSON (or the raw string when
    the body isn't JSON). Raises on transport/HTTP error so callers can
    turn it into a terse verb failure. The image is sent inline; nothing
    is persisted."""
    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("no_api_key")
    payload = {
        "model": os.environ.get("FREYJA_VOICE_LOOK_MODEL", "").strip() or _DEFAULT_LOOK_MODEL,
        "messages": [
            {"role": "system", "content": system},
            {
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image_b64}"}},
                    {"type": "text", "text": user_text},
                ],
            },
        ],
    }
    async with httpx.AsyncClient(timeout=_LOOK_TIMEOUT_SEC) as client:
        resp = await client.post(
            _COMPLETIONS_URL, headers={"Authorization": f"Bearer {api_key}"}, json=payload
        )
    if resp.status_code >= 400:
        raise RuntimeError(f"vision call failed (HTTP {resp.status_code})")
    body = resp.json()
    text = str(((body.get("choices") or [{}])[0].get("message") or {}).get("content") or "").strip()
    # Tolerant parse: strip code fences, pull the first {...} object.
    cleaned = text.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    import json as _json
    import re as _re

    try:
        return _json.loads(cleaned)
    except Exception:  # noqa: BLE001
        m = _re.search(r"\{.*\}", cleaned, _re.DOTALL)
        if m:
            try:
                return _json.loads(m.group(0))
            except Exception:  # noqa: BLE001
                pass
    return text


async def locate_in_image(jpeg_bytes: bytes, target: str) -> tuple[float, float] | None:
    """Vision-ground a natural-language target to normalized (x, y)
    fractions of the image, or None if not found. This is the escape
    hatch for accessibility-opaque apps (Arc, Electron, canvas UIs) where
    the AX tree yields no clickable refs: the caller screenshots, we point
    at the pixel. Fractions are resolution-independent, so the caller
    scales them into whatever coordinate space it clicks in."""
    b64 = base64.b64encode(jpeg_bytes).decode("ascii")
    result = await _vision_json(b64, _LOCATE_PROMPT, f"Locate: {target}")
    if not isinstance(result, dict) or not result.get("found"):
        return None
    try:
        fx = float(result["x"])
        fy = float(result["y"])
    except (KeyError, TypeError, ValueError):
        return None
    return max(0.0, min(1.0, fx)), max(0.0, min(1.0, fy))


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
