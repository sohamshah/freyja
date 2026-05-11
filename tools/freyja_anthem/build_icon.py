#!/usr/bin/env python3
"""freyja icon · breathing contours.

A ~15-second clip of the actual Freyja icon — concentric organic
contour lines, like a topographic map — animated breathing. Wordmark
set in Departure Mono (the app's display font).

Output: tools/freyja_anthem/freyja_icon.mp4 (1920x1080, 30fps).

Run: python3 tools/freyja_anthem/build_icon.py
"""

from __future__ import annotations

import argparse
import math
import struct
import subprocess
import sys
import wave
from pathlib import Path
from typing import Optional

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont


HERE = Path(__file__).resolve().parent
FRAMES_DIR = HERE / "frames_icon"
AUDIO_PATH = HERE / "audio_icon.wav"
OUT_PATH = HERE / "freyja_icon.mp4"

W, H = 1920, 1080
FPS = 30
DURATION = 15.0
TOTAL_FRAMES = int(DURATION * FPS)

# Fonts
DEPARTURE_PATH = Path("/Users/sohamshah/Library/Fonts/DepartureMono-Regular.otf")
JETBRAINS_LIGHT = Path("/opt/homebrew/Caskroom/font-jetbrains-mono/2.304/fonts/ttf/JetBrainsMono-Light.ttf")
JETBRAINS_LIGHT_ITALIC = Path("/opt/homebrew/Caskroom/font-jetbrains-mono/2.304/fonts/ttf/JetBrainsMono-LightItalic.ttf")

# Palette: match the actual icon
BG_TOP = (10, 12, 16)
BG_BOT = (4, 5, 8)
CONTOUR_OUTER = (76, 96, 118)
CONTOUR_INNER = (165, 190, 218)
FG_0 = (245, 245, 247)
FG_2 = (152, 156, 164)
FG_3 = (96, 100, 108)


# ============================================================
# Helpers
# ============================================================
def clamp(x, lo=0.0, hi=1.0):
    return max(lo, min(hi, x))


def lerp(a, b, t):
    return a + (b - a) * t


def lerp_color(a, b, t):
    return (
        int(lerp(a[0], b[0], t)),
        int(lerp(a[1], b[1], t)),
        int(lerp(a[2], b[2], t)),
    )


def ease_out_cubic(t):
    t = clamp(t)
    return 1 - (1 - t) ** 3


def ease_in_out_cubic(t):
    t = clamp(t)
    return 4 * t**3 if t < 0.5 else 1 - (-2 * t + 2) ** 3 / 2


def smoothstep(t):
    t = clamp(t)
    return t * t * (3 - 2 * t)


# ============================================================
# Background
# ============================================================
_BG: Optional[Image.Image] = None
_VIGNETTE: Optional[Image.Image] = None


def base_bg() -> Image.Image:
    global _BG
    if _BG is None:
        img = Image.new("RGB", (W, H), BG_BOT)
        d = ImageDraw.Draw(img)
        for y in range(H):
            ratio = y / H
            r = int(lerp(BG_TOP[0], BG_BOT[0], ratio))
            g = int(lerp(BG_TOP[1], BG_BOT[1], ratio))
            b = int(lerp(BG_TOP[2], BG_BOT[2], ratio))
            d.line([(0, y), (W, y)], fill=(r, g, b))
        _BG = img.convert("RGBA")
    return _BG.copy()


def vignette() -> Image.Image:
    global _VIGNETTE
    if _VIGNETTE is None:
        mask = Image.new("L", (W, H), 0)
        md = ImageDraw.Draw(mask)
        cx, cy = W // 2, H // 2
        max_r = math.hypot(W // 2, H // 2)
        for step in range(100, 0, -1):
            r = step * max_r / 100
            a = int(90 * (step / 100) ** 2.5)
            md.ellipse((cx - r, cy - r, cx + r, cy + r), fill=255 - a)
        vig = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        vig.putalpha(ImageChops.invert(mask))
        _VIGNETTE = vig
    return _VIGNETTE


# ============================================================
# Contour shape — parametric approximation of the icon
# ============================================================
def contour_radius(theta: float, contour_idx: int, t: float) -> float:
    """Returns the radial offset (in normalized units, ~1.0 baseline)
    for a given angle, contour index, and time. Each contour is a
    closed loop; the function returns the local r at that θ.

    Theta is in radians, 0 = right, π/2 = top.

    The shape is organic — irregular but smooth — and slightly
    asymmetric so it reads as the icon, not a generic spiral. Each
    contour has its own tiny phase offset so they don't look
    perfectly stacked.
    """
    # Per-contour drift so the contours wobble independently. Larger
    # drift on outer contours so they breathe more loosely.
    phase = contour_idx * 0.11 + t * 0.18

    # Base organic shape: weighted sum of low-frequency sinusoids.
    # These coefficients were tuned by eye to match the icon's curve:
    # gentle top-bulge, slight bottom-dip, asymmetric lower-left.
    r = 1.0
    r += 0.085 * math.sin(2 * theta + 0.6 + phase)
    r += 0.050 * math.sin(3 * theta + 1.4 + phase * 1.2)
    r += 0.030 * math.sin(5 * theta + 0.9 + phase * 0.8)
    r += 0.018 * math.cos(7 * theta + 2.1 + phase * 0.5)
    # Tilt: lower-left a bit thicker than upper-right.
    r += 0.04 * math.cos(theta - 2.4)
    return r


def draw_contour(
    canvas: Image.Image,
    cx: float,
    cy: float,
    base_radius: float,
    contour_idx: int,
    n_contours: int,
    t: float,
    breath: float,
    width: int = 3,
):
    """Plot one contour as a closed polygon at base_radius with the
    breathing modulation applied. Color graduates from outer (dim)
    to inner (bright)."""
    # Color mix: outer (idx=0) gets CONTOUR_OUTER, inner gets CONTOUR_INNER.
    mix = contour_idx / max(1, n_contours - 1)
    color = lerp_color(CONTOUR_OUTER, CONTOUR_INNER, mix)
    # Brightness pulse on the apex of each breath cycle.
    pulse = 0.5 + 0.5 * math.sin(2 * math.pi * t / 4.0)
    bright_boost = int(20 * pulse * mix)
    color = (
        min(255, color[0] + bright_boost),
        min(255, color[1] + bright_boost),
        min(255, color[2] + bright_boost),
    )

    # Build the polygon vertices.
    n_points = 360
    points = []
    for i in range(n_points):
        theta = (i / n_points) * 2 * math.pi
        r = base_radius * breath * contour_radius(theta, contour_idx, t)
        x = cx + r * math.cos(theta)
        y = cy + r * math.sin(theta)
        points.append((x, y))

    d = ImageDraw.Draw(canvas)
    # Close the loop by appending the first point.
    d.line(points + [points[0]], fill=color, width=width, joint="curve")


def draw_icon(
    canvas: Image.Image,
    cx: float,
    cy: float,
    size: float,
    t: float,
    breath_amp: float = 0.05,
    breath_period: float = 4.0,
    n_contours: int = 8,
    line_width: int = 4,
    alpha: int = 255,
):
    """Render the breathing icon centered at (cx, cy) with given total
    size (height of outer contour ≈ size)."""
    # Each contour stacks inward by a fixed ratio. The outermost has
    # radius ≈ size/2; subsequent contours shrink by a factor.
    base_r = size / 2.0
    # Breath: master scale modulation. Phase 0 = exhale (smaller),
    # phase π = inhale apex (larger). We start mid-cycle so the
    # opening frame is mid-breath rather than full out.
    master_breath = 1.0 + breath_amp * math.sin(2 * math.pi * t / breath_period)

    # Each contour has a slightly offset breath phase so the rings
    # don't all expand together — the inner ones lag the outer.
    layer = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    for i in range(n_contours):
        # The inner contours shrink in radius and breathe with a phase lag.
        ring_ratio = 1.0 - (i / (n_contours - 0.4))
        # Map ring_ratio so the innermost contour is non-zero but small.
        ring_radius = base_r * (0.18 + 0.82 * ring_ratio)
        # Per-ring breath phase: outer (i=0) leads, inner lags.
        ring_phase_offset = (i / n_contours) * 0.6
        ring_breath = 1.0 + breath_amp * math.sin(
            2 * math.pi * (t / breath_period) - ring_phase_offset
        )
        # Outer ring breathes a touch more than inner.
        per_ring_amp = 1.0 - (i / (n_contours * 2.0))
        ring_breath = 1.0 + breath_amp * per_ring_amp * math.sin(
            2 * math.pi * (t / breath_period) - ring_phase_offset
        )
        ring_total = ring_breath * master_breath
        # Line width tapers gently from outer to inner.
        lw = max(2, line_width - i // 3)
        draw_contour(
            layer, cx, cy, ring_radius, i, n_contours, t, ring_total, width=lw,
        )

    # Optional alpha modulation
    if alpha < 255:
        a_layer = layer.split()[-1]
        a_layer = a_layer.point(lambda v: int(v * alpha / 255))
        layer.putalpha(a_layer)

    # Composite onto canvas
    return Image.alpha_composite(canvas, layer)


# ============================================================
# Frame render
# ============================================================
def font(path: Path, size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(str(path), size)


def text_w(s: str, f) -> int:
    return int(f.getlength(s))


def render_frame(frame_idx: int) -> Image.Image:
    t = frame_idx / FPS
    img = base_bg()

    # Position the icon high-centered so the wordmark sits beneath.
    cx, cy = W // 2, int(H * 0.46)
    icon_size = 540

    # Phase A (0–1s): icon fades in from black, contours drawing outward
    # from a tight cluster. Phase B (1–13s): breathing. Phase C (13–15s):
    # subtle fade out.
    if t < 1.0:
        # Reveal: outer contours fade in first, then inner — like the
        # icon "blooming" outward.
        # We render with a per-contour alpha by drawing one ring at a time.
        n = 8
        for i in range(n):
            start = i * 0.10
            local = clamp((t - start) / 0.7)
            a = ease_out_cubic(local)
            if a <= 0:
                continue
            ring_layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
            # Compute ring params
            base_r = icon_size / 2.0
            ring_ratio = 1.0 - (i / (n - 0.4))
            ring_radius = base_r * (0.18 + 0.82 * ring_ratio)
            # No breath yet during reveal
            draw_contour(ring_layer, cx, cy, ring_radius, i, n, t, 1.0, width=max(2, 4 - i // 3))
            a_layer = ring_layer.split()[-1]
            a_layer = a_layer.point(lambda v, av=a: int(v * av))
            ring_layer.putalpha(a_layer)
            img = Image.alpha_composite(img, ring_layer)
    elif t < 13.5:
        # Steady-state breathing
        img = draw_icon(img, cx, cy, icon_size, t, breath_amp=0.045, breath_period=4.5)
    else:
        # Fade out (13.5–15s)
        out_t = 1.0 - clamp((t - 13.5) / 1.5)
        fade_a = int(255 * ease_in_out_cubic(out_t))
        img = draw_icon(img, cx, cy, icon_size, t, breath_amp=0.045, breath_period=4.5, alpha=fade_a)

    # Wordmark — Departure Mono — appears after 1.5s, fades with icon
    if t > 1.5:
        d = ImageDraw.Draw(img)
        wm_t = clamp((t - 1.5) / 1.0)
        wm_alpha = int(255 * ease_out_cubic(wm_t))
        if t > 13.5:
            out_t = 1.0 - clamp((t - 13.5) / 1.5)
            wm_alpha = int(wm_alpha * ease_in_out_cubic(out_t))
        # "freyja" wordmark
        wf = font(DEPARTURE_PATH, 80)
        word = "freyja"
        ww = text_w(word, wf)
        wy = cy + icon_size // 2 + 80
        d.text(
            (W // 2 - ww // 2, wy),
            word,
            font=wf,
            fill=(*FG_0, wm_alpha),
        )
        # Subtle subtitle below — Departure too, smaller, dimmer.
        if t > 3.0:
            sub_t = clamp((t - 3.0) / 1.5)
            sub_a = int(170 * ease_out_cubic(sub_t))
            if t > 13.5:
                out_t = 1.0 - clamp((t - 13.5) / 1.5)
                sub_a = int(sub_a * ease_in_out_cubic(out_t))
            sf = font(DEPARTURE_PATH, 22)
            sub = "a coordination system for many minds"
            sw = text_w(sub, sf)
            d.text(
                (W // 2 - sw // 2, wy + 100),
                sub,
                font=sf,
                fill=(*FG_2, sub_a),
            )

    # Vignette
    img = Image.alpha_composite(img, vignette())
    return img.convert("RGB")


# ============================================================
# Audio — calm ambient under the breathing
# ============================================================
SAMPLE_RATE = 44100


def synth_audio(out_path: Path):
    total = int(SAMPLE_RATE * DURATION)
    buf = [0] * total

    def mix(i, v):
        if 0 <= i < total:
            s = buf[i] + int(v)
            if s > 32767:
                s = 32767
            elif s < -32768:
                s = -32768
            buf[i] = s

    # Calm pad — A2 + E3 + A3 + C#4 — slow LFO on amplitude that
    # matches the 4.5s breath period so the audio breathes with the image.
    base_freqs = [110.0, 164.8, 220.0, 277.2]
    base_amps = [2400, 1500, 1100, 600]
    for i in range(total):
        t = i / SAMPLE_RATE
        # Envelope: fade in over 1s, hold, fade out over last 1.5s
        if t < 1.0:
            env = ease_out_cubic(t)
        elif t < 13.5:
            env = 1.0
        else:
            env = 1.0 - ease_in_out_cubic((t - 13.5) / 1.5)
        env = clamp(env, 0.0, 1.0)
        # Breath LFO: 4.5s period matches the visual.
        breath_lfo = 0.78 + 0.22 * math.sin(2 * math.pi * t / 4.5)
        v = 0.0
        for f, a in zip(base_freqs, base_amps):
            v += a * env * breath_lfo * math.sin(2 * math.pi * f * t)
        mix(i, v)

    # A single soft bell at t=1.5 to mark the wordmark arrival.
    def chime(t_sec, freq, gain=1.0, decay=2.0, length_s=2.0):
        center = int(t_sec * SAMPLE_RATE)
        length = int(SAMPLE_RATE * length_s)
        for k in range(length):
            tl = k / SAMPLE_RATE
            env = math.exp(-tl * decay)
            v = (
                math.sin(2 * math.pi * freq * tl) * 0.7
                + math.sin(2 * math.pi * freq * 2.4 * tl) * 0.2
                + math.sin(2 * math.pi * freq * 4.1 * tl) * 0.08
            ) * 3200 * env * gain
            mix(center + k, v)

    chime(1.6, 440.0, gain=0.6, decay=1.5, length_s=3.5)
    # A second bell at 3.0 for the subtitle arrival.
    chime(3.0, 660.0, gain=0.35, decay=1.4, length_s=3.0)
    # A final farewell bell at the fade out.
    chime(13.6, 220.0, gain=0.5, decay=0.9, length_s=2.0)

    with wave.open(str(out_path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(SAMPLE_RATE)
        wf.writeframes(struct.pack("<" + "h" * len(buf), *(int(v) for v in buf)))


# ============================================================
# Main
# ============================================================
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-audio", action="store_true")
    ap.add_argument("--frames-only", action="store_true")
    ap.add_argument("--start-frame", type=int, default=0)
    ap.add_argument("--end-frame", type=int, default=None)
    args = ap.parse_args()

    end_frame = args.end_frame if args.end_frame is not None else TOTAL_FRAMES
    print(f"freyja icon · {W}x{H} · {FPS} fps · {DURATION:.0f}s")
    print(f"rendering frames {args.start_frame}..{end_frame - 1} -> {FRAMES_DIR}")

    FRAMES_DIR.mkdir(parents=True, exist_ok=True)

    import time as _t
    t0 = _t.time()
    for i in range(args.start_frame, end_frame):
        img = render_frame(i)
        path = FRAMES_DIR / f"frame_{i:05d}.png"
        img.save(path, "PNG", optimize=False, compress_level=1)
        if i % 30 == 0 or i == end_frame - 1:
            elapsed = _t.time() - t0
            done = (i - args.start_frame + 1)
            total = (end_frame - args.start_frame)
            eta = (elapsed / done) * (total - done) if done > 0 else 0
            print(f"  frame {i:4d}/{end_frame - 1} · {elapsed:.1f}s elapsed · ETA {eta:.0f}s")

    if args.frames_only:
        return

    if not args.no_audio:
        print(f"synthesizing audio -> {AUDIO_PATH}")
        synth_audio(AUDIO_PATH)

    print(f"encoding video -> {OUT_PATH}")
    cmd = [
        "ffmpeg", "-y",
        "-framerate", str(FPS),
        "-i", str(FRAMES_DIR / "frame_%05d.png"),
    ]
    if not args.no_audio:
        cmd.extend(["-i", str(AUDIO_PATH)])
    cmd.extend([
        "-c:v", "libx264",
        "-preset", "slow",
        "-crf", "18",
        "-pix_fmt", "yuv420p",
        "-vf", "format=yuv420p",
    ])
    if not args.no_audio:
        cmd.extend(["-c:a", "aac", "-b:a", "192k", "-shortest"])
    cmd.append(str(OUT_PATH))
    print("  $ " + " ".join(cmd))
    subprocess.run(cmd, check=True)
    print(f"done -> {OUT_PATH}")


if __name__ == "__main__":
    main()
