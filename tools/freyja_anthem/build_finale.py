#!/usr/bin/env python3
"""freyja finale · 50s.

Living icon → ASCII-dither dissolve → four abstract coordination
modes. The deliberate invested piece.

Storyboard
----------
0:00–0:10  living icon — the canonical Freyja contour mark. Outer
           rings wobble and twist as if alive; inner rings stay
           still. The icon breathes from the surface, not the core.
0:10–0:12  ASCII-dither ripple dissolves outward from the icon.
           The image becomes coarse monospace characters; behind
           them the next scene fades in.
0:12–0:20  TASK · isolated cells. Sealed Petri-dish chambers, each
           agent inside its own walls.
0:20–0:28  GOAL · target + judgment loop. Trajectories arc up to a
           bullseye that judges, sends a verdict back, the cycle
           restarts.
0:28–0:36  KANBAN · cards flow horizontally through lanes; status
           transitions cascade.
0:36–0:44  BUS · constellation with pulses traveling edges; the
           shared frequency.
0:44–0:50  Finale — all four motifs converge to the wordmark.

Output: tools/freyja_anthem/freyja_finale.mp4 (1920x1080, 30fps).
"""

from __future__ import annotations

import argparse
import math
import random
import struct
import subprocess
import sys
import wave
from pathlib import Path
from typing import Callable, Optional

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont


# ============================================================
# Config
# ============================================================
HERE = Path(__file__).resolve().parent
FRAMES_DIR = HERE / "frames_finale"
AUDIO_PATH = HERE / "audio_finale.wav"
OUT_PATH = HERE / "freyja_finale.mp4"

W, H = 1920, 1080
FPS = 30
DURATION = 50.0
TOTAL_FRAMES = int(DURATION * FPS)

DEPARTURE = Path("/Users/sohamshah/Library/Fonts/DepartureMono-Regular.otf")
JB_LIGHT = Path("/opt/homebrew/Caskroom/font-jetbrains-mono/2.304/fonts/ttf/JetBrainsMono-Light.ttf")
JB_LIGHT_ITALIC = Path("/opt/homebrew/Caskroom/font-jetbrains-mono/2.304/fonts/ttf/JetBrainsMono-LightItalic.ttf")
JB_REGULAR = Path("/opt/homebrew/Caskroom/font-jetbrains-mono/2.304/fonts/ttf/JetBrainsMono-Regular.ttf")
JB_MEDIUM = Path("/opt/homebrew/Caskroom/font-jetbrains-mono/2.304/fonts/ttf/JetBrainsMono-Medium.ttf")

BG_TOP = (10, 12, 16)
BG_BOT = (4, 5, 8)
CONTOUR_OUTER = (76, 96, 118)
CONTOUR_INNER = (170, 196, 224)
FG_0 = (245, 245, 247)
FG_1 = (210, 213, 218)
FG_2 = (152, 156, 164)
FG_3 = (96, 100, 108)
FG_4 = (62, 64, 70)
ACCENT = (127, 184, 232)
OK = (112, 184, 103)
WARN = (217, 162, 73)
DANGER = (193, 106, 106)
PAPER_CREAM = (216, 219, 200)
PAPER_INK = (32, 34, 22)


# ============================================================
# Font cache
# ============================================================
_FONTS: dict[tuple[str, int], ImageFont.FreeTypeFont] = {}


def font(path: Path, size: int) -> ImageFont.FreeTypeFont:
    key = (str(path), size)
    if key not in _FONTS:
        _FONTS[key] = ImageFont.truetype(str(path), size)
    return _FONTS[key]


def text_w(s: str, f: ImageFont.FreeTypeFont) -> int:
    return int(f.getlength(s))


# ============================================================
# Math
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


def ease_in_cubic(t):
    t = clamp(t)
    return t ** 3


def ease_in_out_cubic(t):
    t = clamp(t)
    return 4 * t**3 if t < 0.5 else 1 - (-2 * t + 2) ** 3 / 2


def ease_out_expo(t):
    t = clamp(t)
    return 1 if t >= 1 else 1 - 2 ** (-10 * t)


def smoothstep(t):
    t = clamp(t)
    return t * t * (3 - 2 * t)


# ============================================================
# Caches
# ============================================================
_BG: Optional[Image.Image] = None
_VIGNETTE: Optional[Image.Image] = None
_GRAIN: list[Image.Image] = []


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
            a = int(100 * (step / 100) ** 2.5)
            md.ellipse((cx - r, cy - r, cx + r, cy + r), fill=255 - a)
        vig = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        vig.putalpha(ImageChops.invert(mask))
        _VIGNETTE = vig
    return _VIGNETTE


def grain_frames(n: int = 10, strength: int = 8) -> list[Image.Image]:
    global _GRAIN
    if _GRAIN:
        return _GRAIN
    rnd = random.Random(0xACE)
    for _ in range(n):
        layer = Image.new("L", (W, H), 0)
        px = layer.load()
        for y in range(0, H, 2):
            for x in range(0, W, 2):
                v = rnd.randint(0, strength * 2)
                if v > strength:
                    px[x, y] = v - strength
        rgba = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        rgba.putalpha(layer)
        _GRAIN.append(rgba)
    return _GRAIN


# ============================================================
# Living icon — the canonical contour mark, with outer-ring wobble
# ============================================================
N_RINGS = 9
N_VERTS = 360

# Precomputed per-ring wobble phase tables. The wobble is a multi-
# octave sum of sines with stable random phases — different per
# ring so each ring "lives" independently.
_WOBBLE_RNG = random.Random(0xF1F1)
WOBBLE_HARMONICS = [
    # (spatial_freq, temporal_freq)
    (2, 0.45),
    (3, 0.65),
    (5, 0.85),
    (7, 1.15),
]
_WOBBLE_PHASES = [
    [_WOBBLE_RNG.uniform(0, 2 * math.pi) for _ in WOBBLE_HARMONICS]
    for _ in range(N_RINGS)
]


def ring_wobble_amp(ring_idx: int) -> float:
    """Wobble amplitude decays from outer (idx=0, strong) to inner
    (idx=N-1, near zero). The inner rings should feel rock-still."""
    t = ring_idx / max(1, N_RINGS - 1)
    # Cubic falloff: outer is full, inner is ~0
    return (1.0 - t) ** 2.4


def ring_base_radius(ring_idx: int, max_r: float) -> float:
    """Concentric contour radii. The innermost has a small but non-
    zero radius so the center has a visible 'eye'."""
    t = ring_idx / (N_RINGS - 0.4)
    return max_r * (0.18 + 0.82 * (1.0 - t))


def ring_offset(ring_idx: int, max_r: float) -> tuple[float, float]:
    """The inner contours drift slightly off-center toward the upper-
    right, matching the canonical icon's asymmetric core."""
    t = ring_idx / max(1, N_RINGS - 1)
    return (-max_r * 0.03 * t, -max_r * 0.05 * t)


def base_shape(theta: float) -> float:
    """The canonical kidney/heart contour, normalized to ~1.0 radius.
    Tuned to match the actual icon's outline: gentle top, slight
    bulge on the lower-left, mild upper-right dimple."""
    r = 1.0
    r += 0.085 * math.sin(2 * theta + 0.6)
    r += 0.052 * math.sin(3 * theta + 1.5)
    r += 0.030 * math.sin(5 * theta + 0.9)
    r += 0.018 * math.cos(7 * theta + 2.1)
    r += 0.045 * math.cos(theta - 2.5)  # lower-left thicken
    return r


def ring_wobble(theta: float, ring_idx: int, t: float) -> float:
    """The time-varying perturbation. Returns an additive delta to be
    multiplied by the ring's amplitude before being added to the
    base radius."""
    phases = _WOBBLE_PHASES[ring_idx]
    w = 0.0
    weights = [0.06, 0.040, 0.025, 0.015]
    for (sf, tf), ph, weight in zip(WOBBLE_HARMONICS, phases, weights):
        w += weight * math.sin(sf * theta + tf * t + ph)
    return w


def draw_living_icon(
    canvas: Image.Image,
    cx: int,
    cy: int,
    max_radius: float,
    t: float,
    alpha: int = 255,
    ring_alpha_overrides: Optional[list[float]] = None,
):
    """Render the icon with per-ring wobble. The inner rings are
    nearly still; the outer rings twist and breathe."""
    layer = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    for i in range(N_RINGS):
        ring_alpha = 1.0
        if ring_alpha_overrides is not None and i < len(ring_alpha_overrides):
            ring_alpha = ring_alpha_overrides[i]
        if ring_alpha <= 0:
            continue
        base_r = ring_base_radius(i, max_radius)
        amp = ring_wobble_amp(i) * 0.20  # global scale of wobble effect
        off_x, off_y = ring_offset(i, max_radius)
        # Color graduates outer → inner
        mix = i / max(1, N_RINGS - 1)
        color = lerp_color(CONTOUR_OUTER, CONTOUR_INNER, mix)
        line_alpha = int(alpha * ring_alpha)
        color_rgba = (*color, line_alpha)
        # Build polygon points
        points = []
        for k in range(N_VERTS):
            theta = (k / N_VERTS) * 2 * math.pi
            r = base_r * (base_shape(theta) + amp * ring_wobble(theta, i, t))
            x = cx + off_x + r * math.cos(theta)
            y = cy + off_y + r * math.sin(theta)
            points.append((x, y))
        # Close loop
        line_w = max(2, 5 - i // 3)
        d.line(points + [points[0]], fill=color_rgba, width=line_w, joint="curve")
    return Image.alpha_composite(canvas, layer)


def scene_living(img: Image.Image, t: float, dur: float):
    """0:00–0:10 — the canonical icon, with outer rings wiggling."""
    cx, cy = W // 2, int(H * 0.46)
    max_r = 270
    # Bloom-in: outer rings appear first, then inner. We finish the
    # bloom by t=1.0 so wobble can take over.
    ring_alphas = [1.0] * N_RINGS
    if t < 1.0:
        for i in range(N_RINGS):
            start = i * 0.08
            ring_alphas[i] = ease_out_cubic(clamp((t - start) / 0.5))
    out = draw_living_icon(img, cx, cy, max_r, t, ring_alpha_overrides=ring_alphas)
    # Copy back to img (alpha composite)
    img.paste(out)

    d = ImageDraw.Draw(img)
    # Wordmark in Departure Mono — appears 2s in, holds until the
    # very end of the living scene.
    if t > 2.0:
        wm_t = ease_out_cubic(min(1.0, (t - 2.0) / 1.2))
        wf = font(DEPARTURE, 80)
        word = "freyja"
        ww = text_w(word, wf)
        wy = cy + max_r + 80
        d.text((W // 2 - ww // 2, wy), word, font=wf, fill=(*FG_0, int(255 * wm_t)))
        # Subtitle in Departure
        if t > 4.0:
            st = ease_out_cubic(min(1.0, (t - 4.0) / 1.0))
            sf = font(DEPARTURE, 20)
            sub = "a coordination system for many minds"
            sw = text_w(sub, sf)
            d.text((W // 2 - sw // 2, wy + 100), sub, font=sf, fill=(*FG_2, int(170 * st)))
    # Final 1.5s: hold but start to "wake" — sub-noise on outer rings
    # increases slightly. Already baked into the wobble; no explicit
    # change needed.


# ============================================================
# ASCII ripple dissolve — t=10..12
# ============================================================
ASCII_RAMP = " .:;|+*#%@$"


def render_ascii_layer(
    source: Image.Image,
    char_size: int = 16,
    f_size: int = 14,
    tint: tuple = (220, 220, 230),
) -> Image.Image:
    """Convert `source` to an ASCII grid drawn on a transparent layer.
    Each char_size x char_size patch becomes one character.
    Returned image is RGBA, same size as `source`."""
    src = source.convert("RGB")
    px = src.load()
    layer = Image.new("RGBA", source.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    ff = font(JB_REGULAR, f_size)
    rows = H // char_size
    cols = W // char_size
    for r in range(rows):
        for c in range(cols):
            x0 = c * char_size
            y0 = r * char_size
            # Sample 4 corners + center for brightness; mean.
            samples = [
                px[x0 + char_size // 2, y0 + char_size // 2],
                px[x0 + 2, y0 + 2],
                px[min(W - 1, x0 + char_size - 2), y0 + 2],
                px[x0 + 2, min(H - 1, y0 + char_size - 2)],
                px[min(W - 1, x0 + char_size - 2), min(H - 1, y0 + char_size - 2)],
            ]
            avg_r = sum(s[0] for s in samples) // len(samples)
            avg_g = sum(s[1] for s in samples) // len(samples)
            avg_b = sum(s[2] for s in samples) // len(samples)
            lum = int(0.299 * avg_r + 0.587 * avg_g + 0.114 * avg_b)
            if lum < 6:
                continue
            idx = int(lum / 256 * len(ASCII_RAMP))
            idx = min(idx, len(ASCII_RAMP) - 1)
            ch = ASCII_RAMP[idx]
            # Color: blend underlying tint into reference tint
            color = lerp_color(tint, (avg_r, avg_g, avg_b), 0.55)
            d.text((x0 + 1, y0 - 2), ch, font=ff, fill=(*color, 220))
    return layer


# Cache the ASCII layer (computed once from a fixed icon frame so
# the dissolve is deterministic + cheap to play back).
_ASCII_LAYER: Optional[Image.Image] = None


def get_ascii_dissolve_layer() -> Image.Image:
    global _ASCII_LAYER
    if _ASCII_LAYER is None:
        # Render the icon at the dissolve start frame (t=10) using
        # a black background so the ASCII picks up only icon pixels.
        base = Image.new("RGBA", (W, H), (0, 0, 0, 255))
        snapshot = draw_living_icon(base, W // 2, int(H * 0.46), 270, 10.0)
        # Also include the wordmark to dissolve.
        d = ImageDraw.Draw(snapshot)
        wf = font(DEPARTURE, 80)
        word = "freyja"
        ww = text_w(word, wf)
        wy = int(H * 0.46) + 270 + 80
        d.text((W // 2 - ww // 2, wy), word, font=wf, fill=FG_0)
        sf = font(DEPARTURE, 20)
        sub = "a coordination system for many minds"
        sw = text_w(sub, sf)
        d.text((W // 2 - sw // 2, wy + 100), sub, font=sf, fill=FG_2)
        _ASCII_LAYER = render_ascii_layer(snapshot, char_size=14, f_size=13)
    return _ASCII_LAYER


def scene_ascii_ripple(img: Image.Image, t: float, dur: float):
    """0:10–0:12 — ASCII pattern of icon dissolves outward in a
    radial ripple; behind it, the first abstract scene fades in.

    Implementation: render the next scene under the dissolve, then
    overlay the ASCII layer with a radial alpha mask that's full
    inside the ripple radius and zero outside. As t grows, the
    ripple shrinks from "covers the screen" to "covers nothing"."""
    # Underneath: the first abstract scene starts emerging. Pass the
    # ripple's local_t so the cells progressively bloom in step with
    # the ripple expanding — by the end of the dissolve they're well
    # into their reveal.
    underlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
    scene_task(underlay, t * 1.5, 8.0, intro_only=True)
    img.paste(Image.alpha_composite(img, underlay))

    ascii_layer = get_ascii_dissolve_layer()
    progress = clamp(t / dur)
    # The mask: radius shrinks from max to 0 over the dissolve, but
    # we use an INVERSE — the ASCII is visible OUTSIDE a growing
    # disk of "cleared" area at the center. So center clears first.
    cleared_r = ease_in_out_cubic(progress) * math.hypot(W // 2, H // 2)
    # Build alpha mask
    mask = Image.new("L", (W, H), 0)
    md = ImageDraw.Draw(mask)
    cx, cy = W // 2, int(H * 0.46)
    # Filled large rectangle, then punch out the cleared center.
    md.rectangle((0, 0, W, H), fill=255)
    md.ellipse((cx - cleared_r, cy - cleared_r, cx + cleared_r, cy + cleared_r), fill=0)
    # Soft edge: blur the mask so the transition is feathered.
    mask = mask.filter(ImageFilter.GaussianBlur(20))
    # Apply the mask to the ASCII layer's alpha channel.
    masked_ascii = ascii_layer.copy()
    ascii_alpha = masked_ascii.split()[-1]
    combined_alpha = ImageChops.multiply(ascii_alpha, mask)
    masked_ascii.putalpha(combined_alpha)
    img.paste(Image.alpha_composite(img, masked_ascii))


# ============================================================
# TASK — sealed Petri-dish cells (0:12–0:20)
# ============================================================
TASK_LABELS = [
    "solo",
    "owned",
    "contained",
    "sealed",
    "private",
    "no siblings",
    "self-paced",
    "alone",
]


def scene_task(img: Image.Image, t: float, dur: float, intro_only: bool = False):
    """A grid of 12 sealed cells. Each cell has its own little
    occupant — a small breathing contour, a typing character, a
    progress bar — animated on its own clock. No edges between
    cells. The walls are hairlines."""
    d = ImageDraw.Draw(img)
    cols, rows = 4, 3
    margin_x, margin_y = 160, 200
    gutter = 24
    cell_w = (W - 2 * margin_x - (cols - 1) * gutter) // cols
    cell_h = (H - 2 * margin_y - (rows - 1) * gutter) // rows

    appear_t = ease_out_cubic(min(1.0, t / 1.2))
    rnd = random.Random(13)

    for r in range(rows):
        for c in range(cols):
            idx = r * cols + c
            x0 = margin_x + c * (cell_w + gutter)
            y0 = margin_y + r * (cell_h + gutter)
            x1 = x0 + cell_w
            y1 = y0 + cell_h
            # Stagger reveal
            cell_start = idx * 0.04
            cell_alpha = ease_out_cubic(clamp((t - cell_start) / 0.4))
            wall_a = int(28 * cell_alpha)
            d.rectangle((x0, y0, x1, y1), outline=(255, 255, 255, wall_a), width=1)
            # Inner faint glow
            inner_a = int(8 * cell_alpha)
            d.rectangle((x0 + 1, y0 + 1, x1 - 1, y1 - 1), outline=(*ACCENT, inner_a // 2), width=1)
            # The occupant: a small breathing dot at center
            cx = (x0 + x1) // 2
            cy = (y0 + y1) // 2
            phase = rnd.uniform(0, 2 * math.pi)
            speed = rnd.uniform(0.6, 1.2)
            pulse_size = 5 + 4 * (0.5 + 0.5 * math.sin(2 * math.pi * (t * speed + phase / 6)))
            agent_color = lerp_color(FG_3, ACCENT, 0.4 + 0.4 * math.sin(t * speed + phase))
            d.ellipse(
                (cx - pulse_size, cy - pulse_size, cx + pulse_size, cy + pulse_size),
                fill=(*agent_color, int(220 * cell_alpha)),
            )
            # A tiny progress arc at the bottom of the cell
            bar_y = y1 - 18
            bar_x0 = x0 + 16
            bar_x1 = x1 - 16
            bar_len = bar_x1 - bar_x0
            local_phase = rnd.uniform(0, 1.0)
            bar_progress = (t * speed / 3.0 + local_phase) % 1.0
            d.line(
                [(bar_x0, bar_y), (bar_x1, bar_y)],
                fill=(*FG_4, int(180 * cell_alpha)),
                width=1,
            )
            d.line(
                [(bar_x0, bar_y), (bar_x0 + int(bar_len * bar_progress), bar_y)],
                fill=(*ACCENT, int(160 * cell_alpha)),
                width=1,
            )
            # Cell label upper-left
            lf = font(DEPARTURE, 11)
            label = f"agent_{idx + 1:02d}"
            d.text((x0 + 8, y0 + 6), label, font=lf, fill=(*FG_3, int(160 * cell_alpha)))
            # Personality tag
            if cell_alpha > 0.5:
                ptf = font(JB_LIGHT_ITALIC, 12)
                ptag = TASK_LABELS[idx % len(TASK_LABELS)]
                d.text(
                    (x1 - text_w(ptag, ptf) - 8, y0 + 6),
                    ptag,
                    font=ptf,
                    fill=(*FG_4, int(140 * cell_alpha)),
                )

    if intro_only:
        return

    # Mode title at the top
    if t > 0.4:
        tt = ease_out_cubic(min(1.0, (t - 0.4) / 0.6))
        df = font(DEPARTURE, 30)
        title = "task · isolated"
        tw = text_w(title, df)
        d.text((W // 2 - tw // 2, 80), title, font=df, fill=(*FG_1, int(220 * tt)))
        if t > 1.0:
            st = ease_out_cubic(min(1.0, (t - 1.0) / 0.6))
            sf = font(JB_LIGHT_ITALIC, 20)
            sub = "each agent works alone · no siblings · no shared channel"
            sw = text_w(sub, sf)
            d.text((W // 2 - sw // 2, 124), sub, font=sf, fill=(*FG_3, int(180 * st)))


# ============================================================
# GOAL — target + judgment loop (0:20–0:28)
# ============================================================
def draw_target(d: ImageDraw.ImageDraw, cx: int, cy: int, r: int, t: float):
    """A concentric-ring target / bullseye with a slow pulse."""
    pulse = 0.85 + 0.15 * math.sin(2 * math.pi * t / 1.5)
    for i in range(4):
        rr = int(r * (1 - i * 0.18))
        a = 60 + i * 40
        d.ellipse(
            (cx - rr, cy - rr, cx + rr, cy + rr),
            outline=(*ACCENT, int(a * pulse)),
            width=2,
        )
    # Center hot point
    inner = int(r * 0.16)
    d.ellipse(
        (cx - inner, cy - inner, cx + inner, cy + inner),
        fill=(*ACCENT, int(200 * pulse)),
    )


def scene_goal(img: Image.Image, t: float, dur: float):
    """Trajectories arc upward toward a bullseye target. The target
    pulses 'judging,' then a verdict line drops back down, then a
    new arc emerges. The cycle is the goal loop."""
    d = ImageDraw.Draw(img)

    # Target near top
    tx, ty = W // 2, int(H * 0.32)
    target_r = 110
    draw_target(d, tx, ty, target_r, t)

    # Three concurrent arcs, each on its own cycle
    arc_cycle = 2.8
    arc_starts = [0.0, 0.95, 1.85]
    starts_xy = [(int(W * 0.20), H - 240), (int(W * 0.50), H - 240), (int(W * 0.80), H - 240)]
    for arc_idx, (start_offset, start_xy) in enumerate(zip(arc_starts, starts_xy)):
        local_t = (t - start_offset) % arc_cycle
        if (t - start_offset) < 0:
            continue
        # Phase A (0–1.2s): arc ascending
        # Phase B (1.2–1.6s): target judging (flash + verdict drops)
        # Phase C (1.6–2.8s): verdict line drops back to start, fade
        sx, sy = start_xy
        # Particle trail going up
        if local_t < 1.4:
            arc_t = ease_out_cubic(min(1.0, local_t / 1.4))
            # Quadratic arc: parabola from start to target
            for k in range(40):
                trail_t = clamp(arc_t - k * 0.025)
                if trail_t <= 0:
                    continue
                # Parabolic path
                x = lerp(sx, tx, trail_t)
                y_base = lerp(sy, ty, trail_t)
                y_arc = -240 * math.sin(math.pi * trail_t)
                y = y_base + y_arc
                a = int(200 * (1 - k / 40))
                size = 3 + 2 * (1 - k / 40)
                d.ellipse((x - size, y - size, x + size, y + size), fill=(*FG_1, a))
        # Phase B/C: judgment and verdict
        if 1.4 <= local_t < 1.8:
            # Flash
            flash_t = (local_t - 1.4) / 0.4
            flash_a = int(120 * (1 - flash_t))
            d.ellipse(
                (tx - target_r - 20, ty - target_r - 20, tx + target_r + 20, ty + target_r + 20),
                outline=(*ACCENT, flash_a),
                width=3,
            )
        if 1.6 <= local_t < 2.6:
            verdict_t = (local_t - 1.6) / 1.0
            ratio = ease_in_out_cubic(verdict_t)
            # Drop a small dot from target back to start position
            x = lerp(tx, sx, ratio)
            y = lerp(ty, sy, ratio)
            d.ellipse((x - 4, y - 4, x + 4, y + 4), fill=(*OK, 200))
            # Thin verdict line
            d.line(
                [(tx, ty), (x, y)],
                fill=(*OK, int(60 * (1 - ratio))),
                width=1,
            )

    # Mode title
    if t > 0.4:
        tt = ease_out_cubic(min(1.0, (t - 0.4) / 0.6))
        df = font(DEPARTURE, 30)
        title = "goal · the loop"
        tw = text_w(title, df)
        d.text((W // 2 - tw // 2, 80), title, font=df, fill=(*FG_1, int(220 * tt)))
        if t > 1.0:
            st = ease_out_cubic(min(1.0, (t - 1.0) / 0.6))
            sf = font(JB_LIGHT_ITALIC, 20)
            sub = "the goal is the gravity · judge · iterate · judge again"
            sw = text_w(sub, sf)
            d.text((W // 2 - sw // 2, 124), sub, font=sf, fill=(*FG_3, int(180 * st)))


# ============================================================
# KANBAN — flowing cards through lanes (0:28–0:36)
# ============================================================
def scene_kanban(img: Image.Image, t: float, dur: float):
    """Four horizontal lanes (triage → ready → running → done). Cards
    slide left to right through them. Some get rejected and loop back."""
    d = ImageDraw.Draw(img)

    lanes = [
        ("triage",    PAPER_CREAM, (158, 168, 158)),
        ("ready",     PAPER_CREAM, PAPER_CREAM),
        ("running",   (22, 26, 30), (22, 26, 30)),
        ("done",      (18, 32, 20), (18, 32, 20)),
    ]
    lane_y_start = 240
    lane_h = 130
    lane_pad = 20
    lane_x0 = 160
    lane_x1 = W - 160
    lane_w = lane_x1 - lane_x0

    # Draw lane labels + dividers
    for i, (name, _, _) in enumerate(lanes):
        y = lane_y_start + i * (lane_h + lane_pad)
        df = font(DEPARTURE, 18)
        d.text(
            (lane_x0 - 140, y + lane_h // 2 - 10),
            name,
            font=df,
            fill=FG_2,
        )
        # Lane background — very faint
        d.rectangle((lane_x0, y, lane_x1, y + lane_h), outline=(255, 255, 255, 14), width=1)
        # Station markers
        for k in range(1, 4):
            sx = lane_x0 + k * lane_w // 4
            d.line([(sx, y), (sx, y + lane_h)], fill=(255, 255, 255, 16), width=1)

    # Cards flow through. Each card has a (lane_offset_phase, speed)
    # and renders as a small rectangle that progresses left → right.
    rnd = random.Random(11)
    n_cards = 14
    cards = []
    for i in range(n_cards):
        cards.append({
            "lane": i % 4,
            "phase": rnd.uniform(0, 1.0),
            "speed": rnd.uniform(0.16, 0.22),
            "title": f"card_{i + 12:03d}",
            "color_seed": rnd.randint(0, 5),
        })

    for card in cards:
        local = (t * card["speed"] + card["phase"]) % 1.0
        progress = local
        lane_idx = card["lane"]
        y_top = lane_y_start + lane_idx * (lane_h + lane_pad)
        y_mid = y_top + lane_h // 2
        # x position
        cx = lane_x0 + int(progress * lane_w)
        cw, ch = 90, 60
        # State per lane
        lane_name, _, paper = lanes[lane_idx]
        if lane_idx == 0:
            fill = (158, 168, 158, 220)
        elif lane_idx == 1:
            fill = (216, 219, 200, 230)
        elif lane_idx == 2:
            fill = (24, 28, 34, 230)
        elif lane_idx == 3:
            fill = (18, 32, 20, 230)
        else:
            fill = (40, 40, 40, 200)
        # Subtle outline
        edge = (255, 255, 255, 60) if lane_idx >= 2 else (50, 55, 50, 200)
        d.rounded_rectangle(
            (cx - cw // 2, y_mid - ch // 2, cx + cw // 2, y_mid + ch // 2),
            radius=4, fill=fill, outline=edge, width=1,
        )
        # Tiny title text
        tf = font(JB_LIGHT, 10)
        ink = (32, 34, 22, 220) if lane_idx <= 1 else (220, 222, 226, 220)
        d.text(
            (cx - cw // 2 + 8, y_mid - 14),
            card["title"],
            font=tf,
            fill=ink,
        )
        # Mini progress bar
        d.line(
            [(cx - cw // 2 + 8, y_mid + 12), (cx + cw // 2 - 8, y_mid + 12)],
            fill=ink, width=1,
        )

    # Mode title
    if t > 0.4:
        tt = ease_out_cubic(min(1.0, (t - 0.4) / 0.6))
        df = font(DEPARTURE, 30)
        title = "kanban · flow"
        tw = text_w(title, df)
        d.text((W // 2 - tw // 2, 80), title, font=df, fill=(*FG_1, int(220 * tt)))
        if t > 1.0:
            st = ease_out_cubic(min(1.0, (t - 1.0) / 0.6))
            sf = font(JB_LIGHT_ITALIC, 20)
            sub = "the board runs the work · the page writes itself"
            sw = text_w(sub, sf)
            d.text((W // 2 - sw // 2, 124), sub, font=sf, fill=(*FG_3, int(180 * st)))


# ============================================================
# BUS — message-bus constellation (0:36–0:44)
# ============================================================
def make_bus_nodes(seed: int = 17, n: int = 22):
    rnd = random.Random(seed)
    nodes = []
    for _ in range(n):
        x = rnd.randint(int(W * 0.18), int(W * 0.82))
        y = rnd.randint(int(H * 0.30), int(H * 0.82))
        nodes.append((x, y))
    return nodes


def make_bus_edges(nodes, max_dist=380):
    edges = []
    for i, (x1, y1) in enumerate(nodes):
        for j, (x2, y2) in enumerate(nodes):
            if j <= i:
                continue
            dist = math.hypot(x2 - x1, y2 - y1)
            if dist < max_dist:
                edges.append((i, j, dist))
    return edges


_BUS_NODES = make_bus_nodes()
_BUS_EDGES = make_bus_edges(_BUS_NODES)


def scene_bus(img: Image.Image, t: float, dur: float):
    """A constellation of nodes connected by edges; pulses travel
    along edges; when pulses arrive at a node it flashes; ripples
    radiate outward periodically."""
    d = ImageDraw.Draw(img)

    # Draw edges (faint)
    for (i, j, dist) in _BUS_EDGES:
        x1, y1 = _BUS_NODES[i]
        x2, y2 = _BUS_NODES[j]
        d.line([(x1, y1), (x2, y2)], fill=(255, 255, 255, 16), width=1)

    # Pulses traveling along edges
    pulse_cycle = 2.0
    rnd = random.Random(23)
    for (i, j, dist) in _BUS_EDGES:
        # Each edge has its own pulse phase + speed
        rnd.seed(i * 31 + j * 17)
        phase = rnd.uniform(0, 1.0)
        speed = rnd.uniform(0.7, 1.4)
        local = (t * speed + phase) % pulse_cycle
        # Only pulse during the first half of the cycle
        if local > 1.0:
            continue
        x1, y1 = _BUS_NODES[i]
        x2, y2 = _BUS_NODES[j]
        px = lerp(x1, x2, local)
        py = lerp(y1, y2, local)
        a = int(200 * math.sin(math.pi * local))
        d.ellipse((px - 4, py - 4, px + 4, py + 4), fill=(*ACCENT, a))

    # Nodes with flash on receive
    for i, (x, y) in enumerate(_BUS_NODES):
        # Flash phase: each node has its own
        rnd.seed(i)
        node_phase = rnd.uniform(0, 1.5)
        flash = (math.sin(2 * math.pi * (t * 1.1 + node_phase)) + 1) / 2
        size = 5 + 6 * flash
        a = int(180 + 70 * flash)
        d.ellipse((x - size, y - size, x + size, y + size), fill=(*ACCENT, a))
        # Outer ring (flash halo)
        if flash > 0.7:
            r2 = size + 10
            ha = int(70 * (flash - 0.7) / 0.3)
            d.ellipse(
                (x - r2, y - r2, x + r2, y + r2),
                outline=(*ACCENT, ha),
                width=2,
            )

    # Occasionally a "broadcast" ripple from one node
    broadcast_period = 3.0
    broadcaster_idx = int(t / broadcast_period) % len(_BUS_NODES)
    local_b = (t % broadcast_period) / broadcast_period
    bx, by = _BUS_NODES[broadcaster_idx]
    if local_b < 0.7:
        ring_t = local_b / 0.7
        ring_r = 30 + 320 * ease_out_cubic(ring_t)
        ring_a = int(120 * (1 - ring_t))
        d.ellipse(
            (bx - ring_r, by - ring_r, bx + ring_r, by + ring_r),
            outline=(*ACCENT, ring_a),
            width=2,
        )

    # Mode title
    if t > 0.4:
        tt = ease_out_cubic(min(1.0, (t - 0.4) / 0.6))
        df = font(DEPARTURE, 30)
        title = "bus · the shared frequency"
        tw = text_w(title, df)
        d.text((W // 2 - tw // 2, 80), title, font=df, fill=(*FG_1, int(220 * tt)))
        if t > 1.0:
            st = ease_out_cubic(min(1.0, (t - 1.0) / 0.6))
            sf = font(JB_LIGHT_ITALIC, 20)
            sub = "many minds broadcasting · ripples meeting · constellation alive"
            sw = text_w(sub, sf)
            d.text((W // 2 - sw // 2, 124), sub, font=sf, fill=(*FG_3, int(180 * st)))


# ============================================================
# FINALE — composite + wordmark (0:44–0:50)
# ============================================================
def scene_finale(img: Image.Image, t: float, dur: float):
    """All four motifs overlap briefly at the corners, then resolve
    to the canonical icon + wordmark, holding for the last beat."""
    d = ImageDraw.Draw(img)
    # First half: a soft echo of each mode in the corners
    if t < 2.0:
        echo_t = ease_in_out_cubic(min(1.0, t / 1.4))
        echo_alpha = int(180 * (1 - smoothstep(max(0, (t - 1.4) / 0.6))))
        # Task echo: a tiny grid of cells in the top-left
        for r in range(3):
            for c in range(3):
                x0 = 80 + c * 38
                y0 = 200 + r * 38
                d.rectangle((x0, y0, x0 + 30, y0 + 30), outline=(255, 255, 255, echo_alpha // 4), width=1)
        # Goal echo: a small bullseye in the top-right
        gx, gy = W - 200, 280
        for i in range(3):
            rr = 50 - i * 12
            d.ellipse((gx - rr, gy - rr, gx + rr, gy + rr), outline=(*ACCENT, echo_alpha // 3), width=1)
        # Kanban echo: 3 small cards moving in bottom-left
        for k in range(3):
            cx = 140 + k * 70
            cy = H - 300
            d.rounded_rectangle((cx - 25, cy - 16, cx + 25, cy + 16), radius=3,
                                outline=(255, 255, 255, echo_alpha // 4), width=1)
        # Bus echo: 5 nodes with edges in bottom-right
        nodes = [(W - 320, H - 320), (W - 220, H - 360), (W - 150, H - 290),
                 (W - 230, H - 230), (W - 320, H - 240)]
        for i, (x1, y1) in enumerate(nodes):
            for j, (x2, y2) in enumerate(nodes):
                if j <= i:
                    continue
                d.line([(x1, y1), (x2, y2)], fill=(*ACCENT, echo_alpha // 6), width=1)
        for x, y in nodes:
            d.ellipse((x - 4, y - 4, x + 4, y + 4), fill=(*ACCENT, echo_alpha))

    # The canonical icon reappears at center, growing in
    icon_t = ease_in_out_cubic(min(1.0, max(0, t - 1.2) / 1.8))
    if icon_t > 0:
        cx, cy = W // 2, int(H * 0.46)
        max_r = int(270 * icon_t)
        out = draw_living_icon(img, cx, cy, max_r, t + 44.0)
        img.paste(out)

    # Wordmark in Departure, persistent for the last 2.5s
    if t > 2.5:
        wm_t = ease_out_cubic(min(1.0, (t - 2.5) / 1.0))
        wf = font(DEPARTURE, 100)
        word = "freyja"
        ww = text_w(word, wf)
        wy = int(H * 0.46) + 290
        d.text((W // 2 - ww // 2, wy), word, font=wf, fill=(*FG_0, int(255 * wm_t)))
    # Subtle tagline
    if t > 4.0:
        st = ease_out_cubic(min(1.0, (t - 4.0) / 1.0))
        out_t = 1.0 - clamp((t - 5.4) / 0.6)
        st = st * ease_in_out_cubic(out_t)
        sf = font(DEPARTURE, 22)
        sub = "the page writes itself."
        sw = text_w(sub, sf)
        d.text((W // 2 - sw // 2, int(H * 0.46) + 290 + 130), sub, font=sf, fill=(*FG_2, int(180 * st)))


# ============================================================
# Timeline
# ============================================================
class Scene:
    def __init__(self, name: str, start: float, duration: float, draw_fn: Callable):
        self.name = name
        self.start = start
        self.duration = duration
        self.draw_fn = draw_fn

    def end(self) -> float:
        return self.start + self.duration


SCENES = [
    Scene("living",   0.0,  10.0, scene_living),
    Scene("ripple",   10.0, 2.0,  scene_ascii_ripple),
    Scene("task",     12.0, 8.0,  scene_task),
    Scene("goal",     20.0, 8.0,  scene_goal),
    Scene("kanban",   28.0, 8.0,  scene_kanban),
    Scene("bus",      36.0, 8.0,  scene_bus),
    Scene("finale",   44.0, 6.0,  scene_finale),
]


def render_frame(frame_idx: int) -> Image.Image:
    t_global = frame_idx / FPS
    img = base_bg()
    active = None
    for sc in SCENES:
        if sc.start <= t_global < sc.end():
            active = sc
            break
    if active is None:
        return img.convert("RGB")
    local_t = t_global - active.start
    # Each scene draws on a fresh layer
    layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
    active.draw_fn(layer, local_t, active.duration)
    # Soft edge fades to smooth scene transitions (except for the
    # ASCII ripple itself, which manages its own visibility).
    fade_in = clamp(local_t / 0.4)
    fade_out = clamp((active.duration - local_t) / 0.4)
    edge_alpha = min(fade_in, fade_out)
    if edge_alpha < 1.0 and active.name != "ripple":
        a = layer.split()[-1]
        a = a.point(lambda v: int(v * edge_alpha))
        layer.putalpha(a)
    img = Image.alpha_composite(img, layer)
    # Grain
    grains = grain_frames()
    img = Image.alpha_composite(img, grains[frame_idx % len(grains)])
    # Vignette
    img = Image.alpha_composite(img, vignette())
    return img.convert("RGB")


# ============================================================
# Audio
# ============================================================
SAMPLE_RATE = 44100


def synth_audio(out_path: Path):
    total = int(SAMPLE_RATE * DURATION)
    buf = [0] * total

    def mix(i: int, v: float):
        if 0 <= i < total:
            s = buf[i] + int(v)
            if s > 32767:
                s = 32767
            elif s < -32768:
                s = -32768
            buf[i] = s

    # ── Drone — a low pad that breathes through the whole piece ───
    # Living section: gentle A minor pad with LFO synced to wobble
    # Mode sections: drone modulates per section to color the mood
    for i in range(total):
        t = i / SAMPLE_RATE
        env = 0.0
        if t < 1.0:
            env = ease_out_cubic(t) * 0.55
        elif t < 10.0:
            env = 0.55
        elif t < 12.0:
            env = 0.55 + 0.15 * ease_in_out_cubic((t - 10.0) / 2.0)
        elif t < 44.0:
            # Mode sections — slight variations in tonality per
            # mode (we sit on a low E minor pad through these).
            env = 0.50
        elif t < 49.5:
            env = 0.65
        else:
            env = 0.65 * (1.0 - (t - 49.5) / 0.5)
        env = clamp(env, 0.0, 1.0)
        # Breath LFO: 4.5s for the living section, then changes
        lfo = 0.82 + 0.18 * math.sin(2 * math.pi * t / 4.5)
        v = 0.0
        v += 2400 * env * lfo * math.sin(2 * math.pi * 110.0 * t)   # A2
        v += 1500 * env * lfo * math.sin(2 * math.pi * 164.8 * t)   # E3
        v += 1100 * env * lfo * math.sin(2 * math.pi * 220.0 * t)   # A3
        v += 600 * env * lfo * math.sin(2 * math.pi * 277.2 * t)    # C#4
        mix(i, v)

    # Helpers
    rnd = random.Random(0xCAFE)

    def click(t_sec: float, gain: float = 1.0, decay: float = 70.0, length: int = 700):
        center = int(t_sec * SAMPLE_RATE)
        for k in range(length):
            env = math.exp(-k / decay)
            v = rnd.uniform(-1.0, 1.0) * 4500 * env * gain
            mix(center + k, v)

    def chime(t_sec: float, freq: float, gain: float = 1.0, decay: float = 2.0, length_s: float = 2.0):
        center = int(t_sec * SAMPLE_RATE)
        length = int(SAMPLE_RATE * length_s)
        for k in range(length):
            tl = k / SAMPLE_RATE
            env = math.exp(-tl * decay)
            v = (
                math.sin(2 * math.pi * freq * tl) * 0.7
                + math.sin(2 * math.pi * freq * 2.4 * tl) * 0.22
                + math.sin(2 * math.pi * freq * 4.1 * tl) * 0.08
            ) * 3200 * env * gain
            mix(center + k, v)

    def thump(t_sec: float, freq: float = 58.0, gain: float = 1.0, length_s: float = 0.5):
        center = int(t_sec * SAMPLE_RATE)
        length = int(SAMPLE_RATE * length_s)
        for k in range(length):
            tl = k / SAMPLE_RATE
            env = math.exp(-tl * 4.5)
            v = math.sin(2 * math.pi * freq * tl) * 8500 * env * gain
            mix(center + k, v)

    def noise_burst(t_sec: float, length_s: float = 0.4, gain: float = 1.0, hi_pass: float = 0.6):
        center = int(t_sec * SAMPLE_RATE)
        length = int(SAMPLE_RATE * length_s)
        for k in range(length):
            tl = k / length
            env = (math.sin(math.pi * tl) ** 2) * hi_pass
            v = rnd.uniform(-1.0, 1.0) * 5500 * env * gain
            mix(center + k, v)

    # ── Living section: two soft bells for word + subtitle reveals ──
    chime(2.4, 440.0, gain=0.55, decay=1.4, length_s=3.0)
    chime(4.2, 660.0, gain=0.35, decay=1.5, length_s=2.5)

    # ── Ripple dissolve: noise sweep + a sharp pluck ──
    noise_burst(10.0, length_s=1.6, gain=1.4, hi_pass=0.8)
    chime(10.0, 880.0, gain=0.7, decay=0.8, length_s=2.0)

    # ── TASK section (12-20): scattered clicks, each cell ticking ──
    rnd2 = random.Random(7)
    for cell_idx in range(12):
        phase = rnd2.uniform(0, 0.8)
        speed = rnd2.uniform(0.45, 0.95)
        t0 = 12.4 + phase
        while t0 < 20.0:
            click(t0, gain=0.35, decay=45, length=300)
            t0 += 1.0 / speed
    # Soft sustained pad on top
    for k in range(int(SAMPLE_RATE * 8.0)):
        t = 12.0 + k / SAMPLE_RATE
        env = ease_in_out_cubic(min(1.0, (t - 12.0) / 0.8)) * (1 - ease_in_cubic(max(0, (t - 19.2) / 0.8)))
        v = 1100 * env * math.sin(2 * math.pi * 329.6 * (t - 12.0))
        mix(int(t * SAMPLE_RATE), v)

    # ── GOAL section (20-28): rising tones + judgment plucks ──
    # Three iterations: each is a rising tone followed by a "judged" pluck
    for cyc, t0 in enumerate([20.4, 23.2, 26.0]):
        # Rising sweep
        for k in range(int(SAMPLE_RATE * 1.4)):
            tl = k / SAMPLE_RATE
            env = math.sin(math.pi * tl / 1.4)
            f0 = lerp(220, 440, tl / 1.4)  # rising
            v = 1500 * env * math.sin(2 * math.pi * f0 * tl)
            mix(int((t0 + tl) * SAMPLE_RATE), v)
        # Judgment pluck
        chime(t0 + 1.4, 880.0, gain=0.9, decay=2.0, length_s=1.2)
        # Verdict drop (soft thump on the way back down)
        thump(t0 + 1.7, freq=110, gain=0.5, length_s=0.4)

    # ── KANBAN section (28-36): rhythmic ticking ──
    bpm = 110
    interval = 60.0 / bpm
    t0 = 28.4
    while t0 < 36.0:
        click(t0, gain=0.45, decay=55, length=500)
        # Off-beat lighter click for the lub-dub feel
        click(t0 + interval * 0.5, gain=0.20, decay=30, length=200)
        t0 += interval

    # Lane changes — chord swells every 2 seconds
    for i, t0 in enumerate([28.0, 30.0, 32.0, 34.0]):
        chime(t0, 261.6 + i * 100, gain=0.4, decay=1.2, length_s=2.5)

    # ── BUS section (36-44): shimmering pulses, network chatter ──
    rnd3 = random.Random(101)
    # Random short bell flashes representing pulses arriving at nodes
    t0 = 36.2
    while t0 < 44.0:
        f = rnd3.choice([660, 784, 880, 1046, 1318])
        chime(t0, f, gain=0.30, decay=2.5, length_s=1.2)
        t0 += rnd3.uniform(0.15, 0.45)
    # A few sustained higher tones to give shimmer
    for k in range(int(SAMPLE_RATE * 8.0)):
        t = 36.0 + k / SAMPLE_RATE
        env = ease_in_out_cubic(min(1.0, (t - 36.0) / 1.0)) * (1 - ease_in_cubic(max(0, (t - 43.0) / 1.0)))
        v = 900 * env * math.sin(2 * math.pi * 1318.5 * (t - 36.0))
        v += 500 * env * math.sin(2 * math.pi * 1568.0 * (t - 36.0))
        mix(int(t * SAMPLE_RATE), v)

    # ── FINALE (44-50): chord that resolves all four ──
    # A C-major-ish chord swell with the canonical icon's bell tones
    thump(44.0, freq=44.0, gain=2.0, length_s=1.4)
    chime(44.0, 523.25, gain=1.2, decay=0.7, length_s=4.0)
    chime(44.0, 659.25, gain=0.9, decay=0.7, length_s=4.0)
    chime(44.0, 783.99, gain=0.7, decay=0.7, length_s=4.0)
    # Sustained pad
    for k in range(int(SAMPLE_RATE * 5.0)):
        t = 44.5 + k / SAMPLE_RATE
        env = ease_out_cubic(min(1.0, (t - 44.5) / 1.5)) * (1 - ease_in_cubic(max(0, (t - 48.5) / 1.5)))
        v = 1500 * env * math.sin(2 * math.pi * 261.63 * (t - 44.5))
        v += 900 * env * math.sin(2 * math.pi * 392.0 * (t - 44.5))
        mix(int(t * SAMPLE_RATE), v)
    # Final farewell bell
    chime(48.0, 220.0, gain=0.8, decay=0.9, length_s=2.0)

    # Write WAV
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
    print(f"freyja finale · {W}x{H} · {FPS} fps · {DURATION:.0f}s")
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
