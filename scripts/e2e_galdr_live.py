"""End-to-end check of the GPT-Live voice seat (docs/GALDR-LIVE.md).

Real everything except the operator: headless Chrome runs the renderer's
VoiceEngine over real WebRTC to GPT-Live; a WebAudio "mic" plays a TTS'd
spoken request (no capture device, so no macOS mic prompt); this process plays the bridge — the real VoiceService
creates the session from the browser's SDP offer and executes the
backend's tool calls through the real verb registry and tier gate.

Ask only read-only things: the verbs really run on this Mac.

    uv run --extra dev --with aiohttp python scripts/e2e_galdr_live.py \\
        "What app do I have in front right now?"

    # visual loop without touching the real screen (no Screen Recording
    # prompt): computer.see returns a synthetic, full-size screenshot
    uv run --extra dev --with aiohttp --with pillow python scripts/e2e_galdr_live.py \\
        --fake-screen "What's on my screen right now?"

    # --electron runs the page on the app's own Electron/Chromium
    # (node_modules/electron) instead of system Chrome

    # multi-monitor without touching the real screens: a synthetic
    # three-display desk (laptop + two monitors above it), distinct
    # content on each, driven through the real computer.see display code
    uv run --extra dev --with aiohttp --with pillow python scripts/e2e_galdr_live.py \\
        --fake-desk "What do you see on each of my screens?" 30

Needs OPENAI_API_KEY (env or ~/.freyja/.env) and Google Chrome (or
node_modules/electron with --electron).
Receipts/transcripts go to a temp dir, never ~/.freyja/voice.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import wave
from pathlib import Path
from types import SimpleNamespace

import httpx
from aiohttp import web

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bridge.voice.service import VoiceService  # noqa: E402
from bridge.voice.verbs import VerbResult  # noqa: E402

CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
PORT = 8765


def load_key() -> None:
    if os.environ.get("OPENAI_API_KEY"):
        return
    env = Path.home() / ".freyja" / ".env"
    for line in env.read_text().splitlines():
        m = re.match(r'\s*(?:export\s+)?OPENAI_API_KEY\s*=\s*"?([^"\s]+)"?', line)
        if m:
            os.environ["OPENAI_API_KEY"] = m.group(1)
            return
    sys.exit("OPENAI_API_KEY not found")


def speech_wav(text: str, path: Path, tail_sec: float = 2.0) -> float:
    r = httpx.post(
        "https://api.openai.com/v1/audio/speech",
        headers={"Authorization": f"Bearer {os.environ['OPENAI_API_KEY']}"},
        json={"model": "gpt-4o-mini-tts", "voice": "alloy", "input": text, "response_format": "pcm"},
        timeout=60,
    )
    r.raise_for_status()
    import array
    import random

    # A real mic never sends digital zeros: lay the speech over a faint
    # (~-60 dBFS) noise floor that runs for the whole capture.
    rnd = random.Random(3)
    speech = array.array("h", r.content)
    lead, tail = 24000 * 3, int(24000 * tail_sec)
    samples = array.array("h", (rnd.randint(-30, 30) for _ in range(lead + len(speech) + tail)))
    for i, v in enumerate(speech):
        samples[lead + i] = max(-32768, min(32767, samples[lead + i] + v))
    pcm = samples.tobytes()
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(24000)
        w.writeframes(pcm)
    return len(pcm) / 48000


def fake_screenshot() -> tuple[str, int, int]:
    """A 1280x800 'Mail' screen, noisy enough to weigh what a real
    grid screenshot weighs — the size is the point (data-channel limits)."""
    import base64
    import io
    import random

    from PIL import Image, ImageDraw

    w, h = 1280, 800
    im = Image.new("RGB", (w, h), (236, 236, 236))
    px = im.load()
    rnd = random.Random(7)
    for y in range(0, 90):
        for x in range(w):
            px[x, y] = (rnd.randrange(180, 256),) * 3
    d = ImageDraw.Draw(im)
    d.rectangle([0, 90, 300, h], fill=(222, 226, 232))
    d.text((20, 110), "Inbox (3)", fill="black")
    d.rectangle([320, 110, 1260, 170], outline="black")
    d.text((340, 130), "Priya Raman — Dinner Friday? Are we still on for 7pm at Nopa", fill="black")
    d.rectangle([320, 180, 1260, 240], outline="black")
    d.text((340, 200), "GitHub — [freyja] CI passed on feat/galdr-live", fill="black")
    d.rectangle([20, 740, 160, 780], fill=(40, 120, 220))
    d.text((50, 752), "Compose", fill="white")
    for gx in range(0, w, 100):
        d.line([(gx, 0), (gx, h)], fill=(255, 0, 0))
        d.text((gx + 2, 2), str(gx), fill=(255, 0, 0))
    for gy in range(0, h, 100):
        d.line([(0, gy), (w, gy)], fill=(255, 0, 0))
        d.text((2, gy + 2), str(gy), fill=(255, 0, 0))
    buf = io.BytesIO()
    im.save(buf, "PNG")
    return base64.b64encode(buf.getvalue()).decode(), w, h


def install_fake_desk() -> None:
    """Swap the native capture layer and display geometry for a synthetic
    desk shaped like the operator's: laptop at the origin, a 1920x1080
    monitor above-left and one above-right, each showing something
    different. computer.see's real multi-display path runs on top."""
    import io

    from PIL import Image, ImageDraw

    import bridge.tools.computer_tools as ct

    desk = {
        1: (ct.DisplayGeometry(0, 0, 1728, 1117, True), (236, 236, 240),
            ["Freyja", "Session: Galdr on GPT-Live", "Voice: 3 receipts today"]),
        2: (ct.DisplayGeometry(923, -1080, 1920, 1080, False), (255, 255, 255),
            ["github.com — Pull requests", "#1098 fix(models): document cascade", "#1097 feat: voice multi-monitor"]),
        3: (ct.DisplayGeometry(-997, -1080, 1920, 1080, False), (18, 18, 18),
            ["Terminal — zsh", "$ pytest", "1977 passed, 10 failed (kanban)"]),
    }

    def render(display_id: int, max_dim: int | None) -> tuple[bytes, int, int]:
        geo, bg, lines = desk[display_id]
        im = Image.new("RGB", (int(geo.w), int(geo.h)), bg)
        d = ImageDraw.Draw(im)
        ink = (230, 230, 230) if sum(bg) < 200 else (20, 20, 20)
        try:
            from PIL import ImageFont

            font = ImageFont.load_default(size=56)
        except TypeError:
            font = None
        for i, line in enumerate(lines):
            d.text((80, 120 + i * 110), line, fill=ink, font=font)
        if max_dim and max(im.size) > max_dim:
            scale = max_dim / max(im.size)
            im = im.resize((round(im.width * scale), round(im.height * scale)))
        buf = io.BytesIO()
        im.save(buf, "PNG")
        return buf.getvalue(), im.width, im.height

    class FakeNative:
        class Permissions:
            @staticmethod
            def screen_recording():
                return True

            @staticmethod
            def accessibility():
                return True

        @staticmethod
        def list_displays():
            return [
                SimpleNamespace(id=i, width=int(g.w), height=int(g.h), scale=1.0, is_primary=i == 1)
                for i, (g, _bg, _l) in desk.items()
            ]

        @staticmethod
        def screenshot(display_id=None, window_id=None, max_dim=None, format="png", quality=75):
            png, w, h = render(display_id or 1, max_dim)
            return SimpleNamespace(png=png, width=w, height=h, format="png", mime_type="image/png", byte_len=len(png), capture_ms=1.0)

        @staticmethod
        def cursor_position():
            return (400, 400)

        @staticmethod
        def list_windows(include_helpers=False):
            return []

        @staticmethod
        def get_frontmost_window():
            return None

        @staticmethod
        def read_ax_tree(pid, max_depth=8):
            raise RuntimeError("AX not available in the fake desk")

    ct._import_native = lambda: FakeNative
    ct.display_geometry = lambda: {i: g for i, (g, _bg, _l) in desk.items()}


async def main(utterance: str, listen_sec: float, fake_screen: bool, electron: bool, fake_desk: bool = False) -> int:
    load_key()
    work = Path(tempfile.mkdtemp(prefix="galdr-live-e2e-"))
    wav = work / "utterance.wav"
    audio_sec = speech_wav(utterance, wav, tail_sec=listen_sec + 5)

    bundle = work / "harness.js"
    subprocess.run(
        [
            str(ROOT / "node_modules" / ".bin" / "esbuild"),
            str(ROOT / "scripts" / "e2e" / "galdr-live-harness.ts"),
            "--bundle",
            "--format=esm",
            f"--outfile={bundle}",
            "--log-level=warning",
        ],
        check=True,
    )

    events: list[dict] = []
    svc = VoiceService(
        # The fake desk fakes every native call, so the computer verbs'
        # settings gate can open; otherwise it stays shut like a fresh app.
        SimpleNamespace(default_model="e2e", computer_enabled=fake_desk),
        base_dir=work / "voice",
        emit_fn=events.append,
    )
    svc._log = lambda level, msg: print(f"  [bridge:{level}] {msg}")  # type: ignore[method-assign]
    await svc.start()
    if fake_desk:
        install_fake_desk()
        print("  fake desk: 3 synthetic displays (laptop + two monitors above)")
    if fake_screen:
        b64, sw, sh = fake_screenshot()
        print(f"  fake screenshot: {sw}x{sh}, {len(b64) // 1024} KiB base64")

        async def fake_see(_args):
            return VerbResult(
                ok=True, summary="screenshot taken", image_b64=b64, image_w=sw, image_h=sh
            )

        registry = svc._ensure_registry()
        registry.get("computer.see").run = fake_see
        # screen.look would capture the REAL screen for its vision pass —
        # drop it so the backend has to look through computer.see.
        registry._verbs.pop("screen.look", None)
    t0 = time.monotonic()
    timeline: list[str] = []

    def mark(line: str) -> None:
        stamp = f"[{time.monotonic() - t0:5.1f}s] {line}"
        timeline.append(stamp)
        print(stamp, flush=True)

    async def index(_req):
        return web.Response(
            text='<!doctype html><meta charset=utf-8><script type=module src="/harness.js"></script>',
            content_type="text/html",
        )

    async def harness(_req):
        return web.FileResponse(bundle, headers={"Content-Type": "text/javascript"})

    async def utterance_wav(_req):
        return web.FileResponse(wav, headers={"Content-Type": "audio/wav"})

    async def connect(req):
        body = await req.json()
        events.clear()
        await svc.handle_session_start({})
        ready = next(e for e in events if e["type"] == "voice_session_ready")
        mark(f"bridge: voice_session_ready transport={ready.get('transport')} backend={ready.get('backendModel')}")
        await svc.handle_live_connect({"voiceSessionId": ready["voiceSessionId"], "sdp": body["sdp"]})
        answer = next(e for e in events if e["type"] == "voice_live_answer")
        return web.json_response(answer)

    async def tool(req):
        body = await req.json()
        delay = float(os.environ.get("E2E_TOOL_DELAY", "0") or 0)
        if delay:
            await asyncio.sleep(delay)  # simulate a slow verb
        events.clear()
        await svc.handle_tool_call(
            {
                "voiceSessionId": svc._active_session_id,
                "callId": body["callId"],
                "name": body["name"],
                "argumentsJson": body["argumentsJson"],
                "heard": utterance,
            }
        )
        result = next(e for e in events if e["type"] == "voice_tool_result")
        return web.json_response(result)

    async def log(req):
        body = await req.json()
        kind, data = body["kind"], body["data"]
        if kind == "usage":
            return web.Response(text="ok")
        mark(f"{kind}: {json.dumps(data)[:240]}")
        return web.Response(text="ok")

    app = web.Application(client_max_size=64 * 1024 * 1024)
    app.add_routes(
        [
            web.get("/", index),
            web.get("/harness.js", harness),
            web.get("/utterance.wav", utterance_wav),
            web.post("/connect", connect),
            web.post("/tool", tool),
            web.post("/log", log),
        ]
    )
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", PORT).start()

    url = f"http://127.0.0.1:{PORT}/"
    if electron:
        cmd = [
            str(ROOT / "node_modules" / ".bin" / "electron"),
            f"--user-data-dir={work / 'electron'}",
            str(ROOT / "scripts" / "e2e" / "galdr-live-electron.cjs"),
        ]
    else:
        cmd = [
            CHROME,
            "--headless=new",
            f"--user-data-dir={work / 'chrome'}",
            "--autoplay-policy=no-user-gesture-required",
            "--remote-debugging-port=0",
            url,
        ]
    chrome = subprocess.Popen(
        cmd,
        env={**os.environ, "HARNESS_URL": url},
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    runtime = "electron" if electron else "chrome"
    mark(f"{runtime} up; utterance {audio_sec:.1f}s: {utterance!r}")
    try:
        await asyncio.sleep(audio_sec - 5)  # the wav's tail IS the listen window
    finally:
        chrome.terminate()
        chrome.wait(timeout=10)
        await runner.cleanup()

    joined = "\n".join(timeline)
    # "On it." doesn't count: the voice must speak AFTER the backend's
    # final text arrived, i.e. it relayed the result.
    last_backend = max((i for i, l in enumerate(timeline) if "] backend:" in l), default=None)
    relayed = last_backend is not None and any(
        "] assistant:" in l for l in timeline[last_backend + 1 :]
    )
    checks = {
        "connected": "connected:" in joined,
        "heard the operator": "user:" in joined,
        "backend called a tool": "toolCall:" in joined,
        "voice relayed the backend's answer": relayed,
        "no start failure": "start_failed" not in joined,
    }
    print("\n" + "\n".join(f"{'PASS' if ok else 'FAIL'}  {name}" for name, ok in checks.items()))
    print(f"artifacts: {work}")
    return 0 if all(checks.values()) else 1


if __name__ == "__main__":
    argv = sys.argv[1:]
    fake = "--fake-screen" in argv
    electron = "--electron" in argv
    fake_desk = "--fake-desk" in argv
    argv = [a for a in argv if a not in ("--fake-screen", "--electron", "--fake-desk")]
    utterance = argv[0] if argv else "What app do I have in front right now?"
    listen = float(argv[1]) if len(argv) > 1 else 14.0
    sys.exit(asyncio.run(main(utterance, listen, fake, electron, fake_desk)))
