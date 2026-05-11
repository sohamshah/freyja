#!/usr/bin/env python3
"""freyja anthem · v2 — meta vision piece.

Not a use-case demo (build.py covers that). This one is about the
*system* — many minds, the rune anchor, the page writing itself.
Restrained Freyja base, surgical YouTube-poop chaos at peak moments.

Output: tools/freyja_anthem/freyja_meta.mp4 (1920x1080, 30fps).

Run: python3 tools/freyja_anthem/build_meta.py
"""

from __future__ import annotations

import argparse
import colorsys
import math
import random
import struct
import subprocess
import sys
import wave
from pathlib import Path
from typing import Callable, Optional

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont, ImageOps


# ============================================================
# Config
# ============================================================
HERE = Path(__file__).resolve().parent
FRAMES_DIR = HERE / "frames_meta"
AUDIO_PATH = HERE / "audio_meta.wav"
OUT_PATH = HERE / "freyja_meta.mp4"

W, H = 1920, 1080
FPS = 30
DURATION = 55.0
TOTAL_FRAMES = int(DURATION * FPS)

FONT_DIR = Path("/opt/homebrew/Caskroom/font-jetbrains-mono/2.304/fonts/ttf")
FONT_PATHS = {
    "thin": FONT_DIR / "JetBrainsMono-Thin.ttf",
    "extra_light": FONT_DIR / "JetBrainsMono-ExtraLight.ttf",
    "light": FONT_DIR / "JetBrainsMono-Light.ttf",
    "regular": FONT_DIR / "JetBrainsMono-Regular.ttf",
    "medium": FONT_DIR / "JetBrainsMono-Medium.ttf",
    "bold": FONT_DIR / "JetBrainsMono-Bold.ttf",
    "italic": FONT_DIR / "JetBrainsMono-Italic.ttf",
    "light_italic": FONT_DIR / "JetBrainsMono-LightItalic.ttf",
}

BG_TOP = (8, 10, 14)
BG_BOT = (3, 4, 6)
FG_0 = (245, 245, 247)
FG_1 = (210, 213, 218)
FG_2 = (152, 156, 164)
FG_3 = (96, 100, 108)
FG_4 = (60, 62, 68)
ACCENT = (127, 184, 232)
OK = (112, 184, 103)
WARN = (217, 162, 73)
DANGER = (193, 106, 106)
RUNE = (227, 232, 255)


_FONTS: dict[tuple[str, int], ImageFont.FreeTypeFont] = {}


def font(weight: str, size: int) -> ImageFont.FreeTypeFont:
    key = (weight, size)
    if key not in _FONTS:
        path = FONT_PATHS.get(weight, FONT_PATHS["regular"])
        _FONTS[key] = ImageFont.truetype(str(path), size)
    return _FONTS[key]


def text_w(s: str, f: ImageFont.FreeTypeFont) -> int:
    return int(f.getlength(s))


# ============================================================
# Math helpers
# ============================================================
def clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


def lerp(a: float, b: float, t: float) -> float:
    return a + (b - a) * t


def ease_out_cubic(t: float) -> float:
    t = clamp(t)
    return 1 - (1 - t) ** 3


def ease_in_cubic(t: float) -> float:
    t = clamp(t)
    return t ** 3


def ease_in_out_cubic(t: float) -> float:
    t = clamp(t)
    return 4 * t**3 if t < 0.5 else 1 - (-2 * t + 2) ** 3 / 2


def ease_out_expo(t: float) -> float:
    t = clamp(t)
    return 1.0 if t >= 1 else 1 - 2 ** (-10 * t)


def smoothstep(t: float) -> float:
    t = clamp(t)
    return t * t * (3 - 2 * t)


def pulse(t: float, period: float, mid: float = 0.5, amp: float = 0.5) -> float:
    return mid + amp * math.sin(2 * math.pi * t / period)


# ============================================================
# Background
# ============================================================
_BASE_BG: Optional[Image.Image] = None
_VIGNETTE: Optional[Image.Image] = None
_GRAIN: list[Image.Image] = []


def base_bg() -> Image.Image:
    global _BASE_BG
    if _BASE_BG is None:
        img = Image.new("RGB", (W, H), BG_BOT)
        d = ImageDraw.Draw(img)
        for y in range(H):
            ratio = y / H
            r = int(lerp(BG_TOP[0], BG_BOT[0], ratio))
            g = int(lerp(BG_TOP[1], BG_BOT[1], ratio))
            b = int(lerp(BG_TOP[2], BG_BOT[2], ratio))
            d.line([(0, y), (W, y)], fill=(r, g, b))
        _BASE_BG = img.convert("RGBA")
    return _BASE_BG.copy()


def vignette() -> Image.Image:
    global _VIGNETTE
    if _VIGNETTE is None:
        mask = Image.new("L", (W, H), 0)
        md = ImageDraw.Draw(mask)
        cx, cy = W // 2, H // 2
        max_r = math.hypot(W // 2, H // 2)
        for step in range(100, 0, -1):
            r = step * max_r / 100
            a = int(110 * (step / 100) ** 2.5)
            md.ellipse((cx - r, cy - r, cx + r, cy + r), fill=255 - a)
        vig = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        vig.putalpha(ImageChops.invert(mask))
        _VIGNETTE = vig
    return _VIGNETTE


def grain_frames(n: int = 12, strength: int = 10) -> list[Image.Image]:
    global _GRAIN
    if _GRAIN:
        return _GRAIN
    rnd = random.Random(0xBEEF)
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
# The Fehu rune ᚠ — the visual anchor
# ============================================================
def draw_rune(
    img: Image.Image,
    cx: int,
    cy: int,
    height: int,
    stroke: int = 6,
    color: tuple = RUNE,
    progress: float = 1.0,
    alpha: int = 255,
):
    """Draw the Fehu rune at (cx, cy) with the given total height.
    progress in [0, 1] reveals strokes in writing order:
      0.0 → empty
      0.0..0.5 → vertical bar drawing top-down
      0.5..0.75 → upper diagonal drawing left-to-right
      0.75..1.0 → lower diagonal drawing left-to-right
    """
    if progress <= 0:
        return
    draw = ImageDraw.Draw(img)
    half_h = height / 2
    # Vertical bar
    top = (cx - height * 0.20, cy - half_h)
    bot = (cx - height * 0.20, cy + half_h)
    # Upper diagonal: from on-bar at y=-0.65*half_h to up-right
    upper_start = (cx - height * 0.20, cy - half_h * 0.78)
    upper_end = (cx + height * 0.42, cy - half_h * 0.38)
    # Lower diagonal: from on-bar at y=-0.15*half_h to up-right
    lower_start = (cx - height * 0.20, cy - half_h * 0.16)
    lower_end = (cx + height * 0.42, cy + half_h * 0.22)

    color_rgba = (*color, alpha) if len(color) == 3 else color

    def draw_partial(p1, p2, t):
        if t <= 0:
            return
        t = clamp(t)
        x = lerp(p1[0], p2[0], t)
        y = lerp(p1[1], p2[1], t)
        draw.line([p1, (x, y)], fill=color_rgba, width=stroke)

    bar_t = clamp(progress / 0.5)
    upper_t = clamp((progress - 0.5) / 0.25)
    lower_t = clamp((progress - 0.75) / 0.25)
    draw_partial(top, bot, bar_t)
    if upper_t > 0:
        draw_partial(upper_start, upper_end, upper_t)
    if lower_t > 0:
        draw_partial(lower_start, lower_end, lower_t)


def rune_silhouette(height: int, stroke: int = 6, color=RUNE, alpha=255) -> Image.Image:
    """Render the rune as a transparent image cropped to fit. Used for
    particle bursts and reassemblies."""
    pad = stroke * 2
    w = int(height * 0.62) + pad * 2
    h = height + pad * 2
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    draw_rune(img, w // 2, h // 2, height, stroke=stroke, color=color, alpha=alpha)
    return img


# ============================================================
# Glitch effects
# ============================================================
def glitch_rgb_split(img: Image.Image, offset_x: int = 8, alpha: float = 1.0) -> Image.Image:
    """Cheap chromatic-aberration look: shift R left, B right, keep G."""
    r, g, b, a = img.split()
    r_shifted = ImageChops.offset(r, -offset_x, 0)
    b_shifted = ImageChops.offset(b, offset_x, 0)
    base = Image.merge("RGBA", (r_shifted, g, b_shifted, a))
    if alpha < 1.0:
        return Image.blend(img, base, alpha)
    return base


def glitch_invert(img: Image.Image) -> Image.Image:
    rgb = img.convert("RGB")
    inverted = ImageOps.invert(rgb).convert("RGBA")
    inverted.putalpha(img.split()[-1])
    return inverted


def glitch_hue_shift(img: Image.Image, degrees: float) -> Image.Image:
    """HSV hue rotation. Works on RGB; alpha preserved."""
    rgb = img.convert("RGB")
    px = rgb.load()
    shift = (degrees / 360.0) % 1.0
    for y in range(0, H, 2):
        for x in range(0, W, 2):
            r, g, b = px[x, y]
            h, s, v = colorsys.rgb_to_hsv(r / 255, g / 255, b / 255)
            h = (h + shift) % 1.0
            nr, ng, nb = colorsys.hsv_to_rgb(h, s, v)
            color = (int(nr * 255), int(ng * 255), int(nb * 255))
            px[x, y] = color
            if x + 1 < W:
                px[x + 1, y] = color
            if y + 1 < H:
                px[x, y + 1] = color
            if x + 1 < W and y + 1 < H:
                px[x + 1, y + 1] = color
    out = rgb.convert("RGBA")
    out.putalpha(img.split()[-1])
    return out


def glitch_horizontal_slices(img: Image.Image, seed: int, intensity: float) -> Image.Image:
    """Datamoshing-style: pick a few horizontal strips and shift them
    sideways by random amounts."""
    if intensity <= 0:
        return img
    out = img.copy()
    rnd = random.Random(seed)
    n_slices = int(8 * intensity)
    for _ in range(n_slices):
        y1 = rnd.randint(0, H - 30)
        height = rnd.randint(4, 30)
        y2 = min(H, y1 + height)
        offset = rnd.randint(-int(80 * intensity), int(80 * intensity))
        strip = img.crop((0, y1, W, y2))
        out.paste(strip, (offset, y1))
    return out


def glitch_strobe(img: Image.Image, brightness: float) -> Image.Image:
    """Multiply brightness for a 1-frame flash."""
    if brightness <= 0:
        return Image.new("RGBA", (W, H), (0, 0, 0, 255))
    if brightness == 1.0:
        return img
    rgb = img.convert("RGB")
    px = rgb.load()
    for y in range(0, H, 2):
        for x in range(0, W, 2):
            r, g, b = px[x, y]
            r = min(255, int(r * brightness))
            g = min(255, int(g * brightness))
            b = min(255, int(b * brightness))
            color = (r, g, b)
            px[x, y] = color
            if x + 1 < W:
                px[x + 1, y] = color
    out = rgb.convert("RGBA")
    out.putalpha(img.split()[-1])
    return out


def glitch_mirror_quadrants(img: Image.Image) -> Image.Image:
    """Kaleidoscope of the top-left quadrant mirrored across both axes."""
    quad = img.crop((0, 0, W // 2, H // 2))
    out = Image.new("RGBA", (W, H), (0, 0, 0, 255))
    out.paste(quad, (0, 0))
    out.paste(quad.transpose(Image.FLIP_LEFT_RIGHT), (W // 2, 0))
    out.paste(quad.transpose(Image.FLIP_TOP_BOTTOM), (0, H // 2))
    out.paste(
        quad.transpose(Image.FLIP_LEFT_RIGHT).transpose(Image.FLIP_TOP_BOTTOM),
        (W // 2, H // 2),
    )
    return out


# ============================================================
# Particle system (minds orbiting the rune, then bursting)
# ============================================================
class Particle:
    __slots__ = ("x0", "y0", "x", "y", "r0", "angle0", "phase", "size", "color")

    def __init__(self, angle: float, radius: float, phase: float, size: float, color: tuple):
        self.x0 = W // 2 + radius * math.cos(angle)
        self.y0 = H // 2 + radius * math.sin(angle)
        self.x = self.x0
        self.y = self.y0
        self.r0 = radius
        self.angle0 = angle
        self.phase = phase
        self.size = size
        self.color = color


def make_orbit(n: int, seed: int = 7) -> list[Particle]:
    rnd = random.Random(seed)
    parts = []
    for _ in range(n):
        a = rnd.uniform(0, 2 * math.pi)
        r = rnd.uniform(120, 340)
        ph = rnd.uniform(0, 1.0)
        size = rnd.uniform(1.5, 3.2)
        color = (
            (lerp(127, 245, ph),
             lerp(184, 245, ph),
             lerp(232, 247, ph))
        )
        color = tuple(int(c) for c in color)
        parts.append(Particle(a, r, ph, size, color))
    return parts


def draw_orbit(draw: ImageDraw.ImageDraw, parts: list[Particle], t: float, speed: float = 1.0):
    """Particles orbit the center at angular velocity proportional to
    1/radius (Keplerian-ish — outer ones slower)."""
    for p in parts:
        omega = speed * (200.0 / max(50.0, p.r0))
        ang = p.angle0 + omega * t
        # Slight radial breathing
        r = p.r0 * (1.0 + 0.04 * math.sin(2 * math.pi * (t * 0.18 + p.phase)))
        x = W // 2 + r * math.cos(ang)
        y = H // 2 + r * math.sin(ang)
        p.x, p.y = x, y
        a = int(140 + 110 * abs(math.sin(2 * math.pi * (t * 0.4 + p.phase))))
        draw.ellipse(
            (x - p.size, y - p.size, x + p.size, y + p.size),
            fill=(*p.color, a),
        )


# ============================================================
# Card primitive (compact, for swarms)
# ============================================================
def draw_mini_card(
    img: Image.Image,
    cx: int,
    cy: int,
    w: int,
    h: int,
    state: str = "ready",
    title: str | None = None,
    glow: float = 0.0,
):
    """Tiny stateful card — used in swarms and rapid-cycle hero shots."""
    d = ImageDraw.Draw(img)
    x1, y1, x2, y2 = cx - w // 2, cy - h // 2, cx + w // 2, cy + h // 2
    # Material per state (compact palette)
    if state == "triage":
        fill = (158, 168, 158, 230)
        edge = (110, 118, 110, 200)
    elif state == "ready":
        fill = (216, 219, 200, 245)
        edge = (140, 142, 126, 200)
    elif state == "running":
        fill = (22, 26, 30, 240)
        edge = (255, 255, 255, 60)
    elif state == "done_unverified":
        fill = (42, 30, 14, 240)
        edge = (*WARN, 130)
    elif state == "done":
        fill = (18, 32, 20, 240)
        edge = (*OK, 130)
    elif state == "failed":
        fill = (38, 14, 14, 240)
        edge = (*DANGER, 160)
    else:
        fill = (60, 60, 60, 230)
        edge = (255, 255, 255, 60)
    radius = max(2, min(8, w // 14))
    d.rounded_rectangle((x1, y1, x2, y2), radius=radius, fill=fill, outline=edge, width=1)
    if title and w >= 80:
        tf = font("regular", max(7, min(12, w // 12)))
        tw = text_w(title, tf)
        if state in ("ready", "triage"):
            tc = (28, 30, 22, 220)
        else:
            tc = (245, 245, 247, 220)
        d.text((cx - tw // 2, y1 + (h - tf.size) // 2 - 2), title, font=tf, fill=tc)
    if glow > 0 and state in ("running", "done"):
        glow_color = OK if state == "done" else ACCENT
        gw = int(2 + 4 * glow)
        d.rounded_rectangle(
            (x1 - gw, y1 - gw, x2 + gw, y2 + gw),
            radius=radius + gw,
            outline=(*glow_color, int(80 * glow)),
            width=1,
        )


# ============================================================
# SCENES
# ============================================================
def scene_summon(img: Image.Image, t: float, dur: float):
    """0:00-0:04 — black silence; deep low rumble. The rune draws
    itself stroke-by-stroke at center. A whisper of orbital dots fades
    in by the end."""
    if t < 0.4:
        return  # pure black
    progress = ease_in_out_cubic(min(1.0, (t - 0.4) / 2.8))
    draw_rune(img, W // 2, H // 2, height=420, stroke=8, color=RUNE, progress=progress)
    # Subtle name underneath at the end
    if t > 3.0:
        a = ease_out_cubic(min(1.0, (t - 3.0) / 1.0))
        f = font("light", 22)
        s = "ᚠ · fehu"
        d = ImageDraw.Draw(img)
        d.text(
            (W // 2 - 60, H // 2 + 260),
            "freyja",
            font=f,
            fill=(*FG_2, int(150 * a)),
        )


def scene_orbit(img: Image.Image, t: float, dur: float, parts: list[Particle]):
    """0:04-0:10 — rune at center, many minds orbit. Tempo accelerates."""
    d = ImageDraw.Draw(img)
    # Particles speed grows over scene
    speed = lerp(0.3, 1.8, smoothstep(t / dur))
    draw_orbit(d, parts, t + 1.0, speed=speed)
    draw_rune(img, W // 2, H // 2, height=420, stroke=8, color=RUNE)
    # Mid-scene label
    if 1.5 < t < 5.5:
        a = ease_out_cubic(min(1.0, (t - 1.5) / 0.6)) * (1 - ease_in_cubic(max(0, (t - 4.5) / 1.0)))
        f = font("light_italic", 24)
        label = "many minds · one anchor"
        lw = text_w(label, f)
        d.text(
            (W // 2 - lw // 2, H - 120),
            label,
            font=f,
            fill=(*FG_2, int(220 * a)),
        )


def scene_burst(img: Image.Image, t: float, dur: float, parts: list[Particle]):
    """0:10-0:14 — rune shatters into cards that fly outward, color
    shift, glitch flash. The board materializes from the explosion."""
    d = ImageDraw.Draw(img)
    # The rune is still there but is "exploding"
    explode_t = ease_out_expo(min(1.0, t / 1.0))
    # Draw rune fragments expanding
    n_frag = 48
    rnd = random.Random(99)
    for i in range(n_frag):
        # Each fragment a tiny rectangle
        ang = rnd.uniform(0, 2 * math.pi)
        speed = rnd.uniform(120, 760)
        x = W // 2 + math.cos(ang) * speed * explode_t
        y = H // 2 + math.sin(ang) * speed * explode_t
        size = rnd.uniform(8, 22)
        col_pick = rnd.choice([ACCENT, OK, WARN, RUNE])
        a = int(255 * (1 - explode_t * 0.7))
        d.rectangle(
            (x - size / 2, y - size / 2, x + size / 2, y + size / 2),
            fill=(*col_pick, a),
        )

    # After 1s, cards start to land in a grid
    if t > 1.0:
        appear_t = ease_out_cubic(min(1.0, (t - 1.0) / 1.5))
        cols = 12
        rows = 6
        cw = 130
        ch = 80
        grid_w = cols * cw + (cols - 1) * 12
        grid_h = rows * ch + (rows - 1) * 12
        gx0 = (W - grid_w) // 2
        gy0 = (H - grid_h) // 2
        states = ["triage", "ready", "running", "done_unverified", "done", "failed"]
        rnd2 = random.Random(31)
        for r in range(rows):
            for c in range(cols):
                if appear_t * cols * rows < r * cols + c:
                    continue
                cx = gx0 + c * (cw + 12) + cw // 2
                cy = gy0 + r * (ch + 12) + ch // 2
                state = rnd2.choice(states)
                draw_mini_card(img, cx, cy, cw, ch, state=state)


def scene_cycle(img: Image.Image, t: float, dur: float):
    """0:14-0:20 — one hero card cycles through every state rapidly.
    Background: dozens of cards each cycling on their own clock."""
    # Hero card cycles every 0.2s (5 states per second)
    states_order = ["triage", "ready", "running", "done_unverified", "done"]
    period = 0.2
    state_idx = int((t / period)) % len(states_order)
    hero_state = states_order[state_idx]
    hero_title = hero_state.upper()
    # Big hero card center
    draw_mini_card(img, W // 2, H // 2, 280, 160, state=hero_state, title=hero_title, glow=1.0)

    # Background cards cycling at their own rates
    rnd = random.Random(2024)
    for i in range(80):
        cx = rnd.randint(80, W - 80)
        cy = rnd.randint(80, H - 80)
        # Avoid the hero zone
        if abs(cx - W // 2) < 200 and abs(cy - H // 2) < 130:
            continue
        cw = rnd.randint(48, 110)
        ch = max(28, cw // 2)
        own_period = rnd.uniform(0.15, 0.6)
        own_phase = rnd.uniform(0, 1.0)
        idx = int((t / own_period + own_phase)) % len(states_order)
        draw_mini_card(img, cx, cy, cw, ch, state=states_order[idx])

    # Italic label
    d = ImageDraw.Draw(img)
    f = font("light_italic", 26)
    if t > 1.5:
        a = ease_out_cubic(min(1.0, (t - 1.5) / 0.8))
        label = "every card is a small life"
        lw = text_w(label, f)
        d.text(
            (W // 2 - lw // 2, H - 140),
            label,
            font=f,
            fill=(*FG_1, int(220 * a)),
        )


AGENT_TYPES = [
    ("specifier", "expands triage cards"),
    ("explore", "research, web, files"),
    ("code", "isolated code changes"),
    ("verify", "second pair of eyes"),
    ("plan", "implementation strategy"),
    ("review", "find regressions"),
    ("test", "validation runner"),
    ("browser-qa", "frontend behaviour"),
    ("memory-curator", "skill hygiene"),
]


def scene_chorus(img: Image.Image, t: float, dur: float):
    """0:20-0:26 — 9-cell grid of agent types, each running its own
    tiny tool stream. A chorus of specializations."""
    cols = 3
    rows = 3
    cell_w = W // cols
    cell_h = (H - 100) // rows
    d = ImageDraw.Draw(img)
    for i, (name, desc) in enumerate(AGENT_TYPES):
        r = i // cols
        c = i % cols
        cx = c * cell_w
        cy = 50 + r * cell_h
        # Cell appears with stagger
        appear_at = 0.1 + i * 0.12
        if t < appear_at:
            continue
        a = ease_out_cubic(min(1.0, (t - appear_at) / 0.4))
        # Cell border (subtle)
        d.rectangle(
            (cx + 12, cy + 12, cx + cell_w - 12, cy + cell_h - 12),
            outline=(255, 255, 255, int(28 * a)),
            width=1,
        )
        # Title
        tf = font("medium", 26)
        d.text((cx + 32, cy + 32), name, font=tf, fill=(*FG_0, int(230 * a)))
        df = font("light_italic", 14)
        d.text((cx + 32, cy + 64), desc, font=df, fill=(*FG_3, int(180 * a)))
        # A tiny tool stream
        stream = [
            ("├", f"$ run {name.replace('-', '_')}"),
            ("├", "Fetching context"),
            ("├", "kanban heartbeat"),
            ("└", "complete"),
        ]
        per_line = 0.32
        local_t = (t - appear_at)
        line_idx = min(len(stream), int(local_t / per_line))
        sf = font("regular", 12)
        gf = font("light", 12)
        sy = cy + 110
        for k, (g, txt) in enumerate(stream):
            if k > line_idx:
                break
            d.text((cx + 36, sy), g, font=gf, fill=(*FG_3, int(180 * a)))
            d.text((cx + 56, sy), txt, font=sf, fill=(*FG_1, int(210 * a)))
            sy += 22


def scene_galaxy(img: Image.Image, t: float, dur: float, parts: list[Particle]):
    """0:26-0:32 — galaxy of points swirling, with the rune emerging
    in negative space."""
    d = ImageDraw.Draw(img)
    # Many particles in spiral arms
    rnd = random.Random(101)
    base_density = lerp(60, 600, ease_out_cubic(min(1.0, t / 2.5)))
    n = int(base_density)
    for i in range(n):
        rnd.seed(i + 101)
        a0 = rnd.uniform(0, 2 * math.pi)
        r0 = rnd.uniform(80, 600)
        # Spiral: angle increases with radius
        spiral = a0 + r0 * 0.006
        rotate = t * 0.45
        ang = spiral + rotate
        # Radial breathing
        breath = 1.0 + 0.06 * math.sin(2 * math.pi * (t * 0.25 + i * 0.07))
        x = W // 2 + r0 * breath * math.cos(ang)
        y = H // 2 + r0 * breath * math.sin(ang)
        if not (0 <= x < W and 0 <= y < H):
            continue
        # Color: random pick from palette with brightness modulation
        pick = (i * 7) % 5
        cols = [ACCENT, RUNE, FG_1, OK, WARN]
        col = cols[pick]
        # Inside the negative-space rune zone, darken/skip
        # Rune zone approx: rectangle around center, with some shape
        rx = x - W // 2
        ry = y - H // 2
        if -84 < rx < -56 and -180 < ry < 180:
            continue
        if 56 < ry - rx and -160 < ry < 160 and -54 < rx < 90:
            # rough upper diagonal
            continue
        bright = 0.6 + 0.4 * math.sin(2 * math.pi * (t * 0.6 + i * 0.21))
        alpha_v = int(180 * bright)
        size_v = 1.2 + (rnd.random() * 1.5)
        d.ellipse(
            (x - size_v, y - size_v, x + size_v, y + size_v),
            fill=(*col, alpha_v),
        )
    # The rune in negative space — drawn slightly later for emergence
    if t > 1.0:
        emerge = ease_out_cubic(min(1.0, (t - 1.0) / 2.0))
        draw_rune(
            img, W // 2, H // 2, height=520, stroke=10,
            color=RUNE, alpha=int(220 * emerge), progress=1.0,
        )


def scene_awe(img: Image.Image, t: float, dur: float):
    """0:32-0:38 — big FREYJA reveal. Layered cascades."""
    d = ImageDraw.Draw(img)
    # Background cascade: tiny system event lines streaming downward
    rnd = random.Random(2025)
    cascades = [
        "kanban update · card_017 → running",
        "✓ verified by gpt-5.5",
        "↗ dispatched specifier",
        "⟲ reclaimed card_011",
        "$ pytest -q",
        "kanban heartbeat",
        "Reading routing.py",
        "definition_of_done · 3 checks",
        "complete · 12 tools",
        "the page writes itself.",
    ]
    cascade_n = 18
    for i in range(cascade_n):
        col_x = int(W * (i / cascade_n)) + 60
        line = cascades[i % len(cascades)]
        f = font("light", 13)
        # Vertical scroll
        scroll = (t * (60 + (i % 4) * 20)) % H
        for k in range(8):
            y_pos = (k * 80 + scroll) % H
            alpha_y = int(120 * (1 - abs(y_pos - H // 2) / H))
            d.text((col_x, y_pos), line, font=f, fill=(*FG_3, max(0, alpha_y)))

    # Center: FREYJA huge
    big_t = ease_out_cubic(min(1.0, t / 1.0))
    bf = font("light", 280)
    label = "FREYJA"
    lw = text_w(label, bf)
    # Slight scale-up animation
    scale_factor = lerp(0.92, 1.0, big_t)
    big_size = int(280 * scale_factor)
    bf2 = font("light", big_size)
    lw2 = text_w(label, bf2)
    cy = H // 2 - bf2.size // 2
    d.text((W // 2 - lw2 // 2, cy), label, font=bf2, fill=(*FG_0, int(255 * big_t)))
    # Subtitle
    if t > 1.6:
        sub_t = ease_out_cubic(min(1.0, (t - 1.6) / 1.0))
        sf = font("light_italic", 28)
        sub = "a system that thinks at scale"
        sw = text_w(sub, sf)
        d.text(
            (W // 2 - sw // 2, cy + bf2.size + 10),
            sub,
            font=sf,
            fill=(*FG_2, int(220 * sub_t)),
        )


def scene_pageful(img: Image.Image, t: float, dur: float):
    """0:38-0:44 — calm beat. The phrase 'the page writes itself' writes
    itself onto the page, three echoes in slightly offset positions."""
    d = ImageDraw.Draw(img)
    phrase = "the page writes itself."
    cps = 22.0
    f = font("light_italic", 56)
    pw = text_w(phrase, f)
    base_y = H // 2 - 80
    base_x = W // 2 - pw // 2

    # Three echoes typing at different phases
    for echo in range(3):
        echo_start = echo * 0.5
        if t < echo_start:
            continue
        typed_t = t - echo_start
        typed_chars = int(typed_t * cps)
        text_seg = phrase[:typed_chars]
        if not text_seg:
            continue
        # Echoes are offset and tinted
        offsets = [(0, 0, FG_0, 230), (4, 12, FG_2, 130), (-3, 22, FG_3, 80)]
        ox, oy, col, alpha_v = offsets[echo]
        d.text(
            (base_x + ox, base_y + oy),
            text_seg,
            font=f,
            fill=(*col, alpha_v),
        )

    # Late: small running progress bars stream across the bottom
    if t > 2.5:
        n_bars = 22
        for i in range(n_bars):
            offset_phase = i * 0.07
            local = (t - 2.5 + offset_phase) * 0.6
            local = local - math.floor(local)
            y = H - 120 + i * 6
            x1 = 80
            x2 = W - 80
            d.line(
                [(x1, y), (lerp(x1, x2, local), y)],
                fill=(*ACCENT, 90),
                width=1,
            )


def scene_reassembly(img: Image.Image, t: float, dur: float, parts: list[Particle]):
    """0:44-0:52 — particles stream INWARD from the edges, reforming
    around the rune. The name freyja types itself letter by letter at
    the bottom."""
    d = ImageDraw.Draw(img)
    incoming_t = ease_out_cubic(min(1.0, t / 4.0))
    rnd = random.Random(202)
    for i in range(220):
        rnd.seed(i + 202)
        end_a = rnd.uniform(0, 2 * math.pi)
        end_r = rnd.uniform(130, 330)
        end_x = W // 2 + end_r * math.cos(end_a)
        end_y = H // 2 + end_r * math.sin(end_a)
        # Start far away off-screen
        start_a = rnd.uniform(0, 2 * math.pi)
        start_r = rnd.uniform(1000, 1400)
        start_x = W // 2 + start_r * math.cos(start_a)
        start_y = H // 2 + start_r * math.sin(start_a)
        x = lerp(start_x, end_x, incoming_t)
        y = lerp(start_y, end_y, incoming_t)
        # Final orbit shuffle once they arrive
        if incoming_t >= 1.0:
            tail = (t - 4.0) * 0.6 * (200 / max(50.0, end_r))
            ang = end_a + tail
            x = W // 2 + end_r * math.cos(ang)
            y = H // 2 + end_r * math.sin(ang)
        col_pick = [ACCENT, RUNE, FG_1][i % 3]
        d.ellipse((x - 2.2, y - 2.2, x + 2.2, y + 2.2), fill=(*col_pick, 200))

    # Rune at center, fully drawn, brightening over time
    bright = ease_out_cubic(min(1.0, t / 3.5))
    draw_rune(
        img, W // 2, H // 2, height=460, stroke=10,
        color=RUNE, alpha=int(220 * bright), progress=1.0,
    )

    # "freyja" types at the bottom
    if t > 2.0:
        typed_chars = int((t - 2.0) * 2.4)
        s = "freyja"[:typed_chars]
        f = font("light", 64)
        sw = text_w(s, f)
        d.text(
            (W // 2 - sw // 2, H - 220),
            s,
            font=f,
            fill=FG_0,
        )
        if typed_chars < len("freyja"):
            blink = (int(t * 4) % 2) == 0
            if blink:
                fw_typed = text_w(s, f)
                cx0 = W // 2 - sw // 2 + fw_typed + 6
                d.rectangle((cx0, H - 220 + 12, cx0 + 4, H - 220 + 64), fill=FG_1)


def scene_silence(img: Image.Image, t: float, dur: float):
    """0:52-0:55 — rune only. Slowly fades."""
    d = ImageDraw.Draw(img)
    fade = 1.0 - ease_in_cubic(min(1.0, t / dur))
    draw_rune(
        img, W // 2, H // 2, height=460, stroke=10,
        color=RUNE, alpha=int(220 * fade), progress=1.0,
    )
    if t < 1.0:
        a = 1.0 - ease_out_cubic(t)
        f = font("light", 64)
        s = "freyja"
        sw = text_w(s, f)
        d.text(
            (W // 2 - sw // 2, H - 220),
            s,
            font=f,
            fill=(*FG_0, int(255 * a)),
        )


# ============================================================
# Timeline + glitch director
# ============================================================
ORBIT = make_orbit(96)


class Scene:
    def __init__(self, name: str, start: float, duration: float, draw_fn: Callable):
        self.name = name
        self.start = start
        self.duration = duration
        self.draw_fn = draw_fn

    def end(self) -> float:
        return self.start + self.duration


def scene_orbit_wrap(img, t, dur):
    scene_orbit(img, t, dur, ORBIT)


def scene_burst_wrap(img, t, dur):
    scene_burst(img, t, dur, ORBIT)


def scene_galaxy_wrap(img, t, dur):
    scene_galaxy(img, t, dur, ORBIT)


def scene_reassembly_wrap(img, t, dur):
    scene_reassembly(img, t, dur, ORBIT)


SCENES = [
    Scene("summon",     0.0,  4.0,  scene_summon),
    Scene("orbit",      4.0,  6.0,  scene_orbit_wrap),
    Scene("burst",      10.0, 4.0,  scene_burst_wrap),
    Scene("cycle",      14.0, 6.0,  scene_cycle),
    Scene("chorus",     20.0, 6.0,  scene_chorus),
    Scene("galaxy",     26.0, 6.0,  scene_galaxy_wrap),
    Scene("awe",        32.0, 6.0,  scene_awe),
    Scene("pageful",    38.0, 6.0,  scene_pageful),
    Scene("reassembly", 44.0, 8.0,  scene_reassembly_wrap),
    Scene("silence",    52.0, 3.0,  scene_silence),
]


# ============================================================
# Glitch director — picks effects per frame based on timeline
# ============================================================
def glitch_director(img: Image.Image, frame_idx: int, t_global: float) -> Image.Image:
    """Returns the image with optional glitch effects applied. Effects
    are bursty and timed — surgical, not constant."""
    # Specific marked frames with hard glitches
    # Burst hit at 10.0 (frame 300): bright flash + RGB split
    if 300 <= frame_idx <= 302:
        img = glitch_strobe(img, 2.4)
        img = glitch_rgb_split(img, offset_x=22)
        return img
    if frame_idx == 303:
        return glitch_invert(img)
    # Cycle scene flashes: every 6 frames between frame 420 and 600, brief invert
    if 420 <= frame_idx < 600:
        if (frame_idx - 420) % 18 == 0:
            return glitch_invert(img)
        if (frame_idx - 420) % 18 == 1:
            return glitch_rgb_split(img, offset_x=6)
    # Chorus scene: small RGB shimmer every 15 frames
    if 600 <= frame_idx < 780:
        if (frame_idx - 600) % 30 == 0:
            return glitch_rgb_split(img, offset_x=4, alpha=0.7)
    # Galaxy: occasional hue rotation pulse
    if 780 <= frame_idx < 960:
        if (frame_idx - 780) % 25 == 0:
            return glitch_hue_shift(img, 12)
    # Awe scene: peak moment — strobe + mirror at frame 1000
    if frame_idx == 1000:
        return glitch_strobe(img, 2.0)
    if frame_idx == 1001:
        return glitch_rgb_split(img, offset_x=30)
    if 960 <= frame_idx < 1140:
        # Pulsing RGB split, very subtle
        if (frame_idx - 960) % 14 == 0:
            return glitch_rgb_split(img, offset_x=3, alpha=0.5)
    # Pageful: subtle slice glitches
    if 1140 <= frame_idx < 1320:
        if (frame_idx - 1140) % 40 == 5:
            return glitch_horizontal_slices(img, seed=frame_idx, intensity=0.5)
    # Reassembly: clean — no glitch
    return img


# ============================================================
# Per-frame render
# ============================================================
def render_frame(frame_idx: int) -> Image.Image:
    t_global = frame_idx / FPS
    img = base_bg()
    active = None
    for sc in SCENES:
        if sc.start <= t_global < sc.end():
            active = sc
            break
    # crossfade fallback (rare gaps)
    if active is None:
        # find closest
        return img.convert("RGB")
    local_t = t_global - active.start
    scene_layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    active.draw_fn(scene_layer, local_t, active.duration)
    # Soft fade-in / fade-out at scene edges
    fade_in = clamp(local_t / 0.3)
    fade_out = clamp((active.duration - local_t) / 0.3)
    edge_alpha = min(fade_in, fade_out)
    if edge_alpha < 1.0:
        a = scene_layer.split()[-1]
        a = a.point(lambda v: int(v * edge_alpha))
        scene_layer.putalpha(a)
    img = Image.alpha_composite(img, scene_layer)
    # Grain
    grains = grain_frames()
    img = Image.alpha_composite(img, grains[frame_idx % len(grains)])
    # Vignette
    img = Image.alpha_composite(img, vignette())
    # Glitch director (post-comp)
    img = glitch_director(img, frame_idx, t_global)
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

    # ── Sub-bass drone ─────────────────────────────────────
    # Deep rumble for the whole piece, ducked during pageful (calm beat).
    for i in range(total):
        t = i / SAMPLE_RATE
        env = 0.0
        if t < 4.0:
            env = ease_out_cubic(t / 4.0) * 0.55
        elif t < 10.0:
            env = 0.55
        elif t < 14.0:
            env = 0.65
        elif t < 20.0:
            env = 0.55
        elif t < 26.0:
            env = 0.60
        elif t < 32.0:
            env = 0.70
        elif t < 38.0:
            env = 0.90  # awe scene swell
        elif t < 44.0:
            env = 0.45  # pageful calm
        elif t < 52.0:
            env = 0.70  # reassembly
        else:
            env = 0.70 * (1 - (t - 52.0) / 3.0)
        env = clamp(env, 0.0, 1.0)
        v = 0.0
        v += 3800 * env * math.sin(2 * math.pi * 41.2 * t)  # E1 fundamental
        v += 2200 * env * math.sin(2 * math.pi * 82.4 * t)
        v += 1100 * env * math.sin(2 * math.pi * 164.8 * t)
        v += 600 * env * math.sin(2 * math.pi * 246.9 * t)  # B3
        lfo = 0.82 + 0.18 * math.sin(2 * math.pi * t * 0.09)
        v *= lfo
        mix(i, v)

    # Helpers
    rnd = random.Random(0xCAFE)

    def click(t_sec, gain=1.0, decay=70.0, length=700):
        center = int(t_sec * SAMPLE_RATE)
        for k in range(length):
            env = math.exp(-k / decay)
            v = rnd.uniform(-1.0, 1.0) * 4500 * env * gain
            mix(center + k, v)

    def chime(t_sec, freq, gain=1.0, decay=2.5, length_s=1.4):
        center = int(t_sec * SAMPLE_RATE)
        length = int(SAMPLE_RATE * length_s)
        for k in range(length):
            tl = k / SAMPLE_RATE
            env = math.exp(-tl * decay)
            v = (
                math.sin(2 * math.pi * freq * tl) * 0.7
                + math.sin(2 * math.pi * freq * 2.4 * tl) * 0.25
                + math.sin(2 * math.pi * freq * 4.1 * tl) * 0.10
            ) * 5000 * env * gain
            mix(center + k, v)

    def thump(t_sec, freq=58.0, gain=1.0, length_s=0.4):
        center = int(t_sec * SAMPLE_RATE)
        length = int(SAMPLE_RATE * length_s)
        for k in range(length):
            tl = k / SAMPLE_RATE
            env = math.exp(-tl * 4.0)
            v = math.sin(2 * math.pi * freq * tl) * 9000 * env * gain
            mix(center + k, v)

    def noise_sweep(t_sec, length_s=0.8, gain=1.0):
        center = int(t_sec * SAMPLE_RATE)
        length = int(SAMPLE_RATE * length_s)
        for k in range(length):
            tl = k / length
            env = math.sin(math.pi * tl)
            v = rnd.uniform(-1.0, 1.0) * 6000 * env * gain
            mix(center + k, v)

    # ── Rune-completion pluck (summon) ─────────────────────
    chime(3.6, 220.0, gain=0.9, decay=2.0)
    chime(3.8, 330.0, gain=0.6, decay=2.5)

    # ── Orbit heartbeats (overlapping) ─────────────────────
    for i, off in enumerate([0.0, 0.13, 0.41, 0.69]):
        t0 = 4.5 + off
        while t0 < 10.0:
            gain = 0.5 + 0.05 * i
            thump(t0, freq=58.0 + i * 7, gain=gain * 0.6)
            t0 += 1.2 - i * 0.05

    # ── Burst hit (10.0) ───────────────────────────────────
    noise_sweep(9.6, length_s=0.6, gain=1.2)
    thump(10.0, freq=42.0, gain=2.0, length_s=0.8)
    chime(10.05, 660.0, gain=1.4, decay=1.0)
    chime(10.05, 880.0, gain=1.0, decay=1.2)

    # ── Cycle ticks ────────────────────────────────────────
    t0 = 14.0
    while t0 < 20.0:
        click(t0, gain=0.5, length=400, decay=40)
        t0 += 0.2

    # ── Chorus motifs ──────────────────────────────────────
    # 9 short tones at staggered times
    base = 20.2
    intervals = [0.0, 0.55, 1.1, 1.6, 2.1, 2.55, 3.0, 3.45, 3.85]
    freqs = [392, 440, 494, 523, 587, 659, 698, 784, 880]
    for k, (off, f) in enumerate(zip(intervals, freqs)):
        chime(base + off, f, gain=0.55, decay=2.5)

    # ── Galaxy sustained drone overlay ─────────────────────
    # already in the main drone; add a high overlay
    for i in range(int(SAMPLE_RATE * 6.0)):
        t = 26.0 + i / SAMPLE_RATE
        env = ease_in_out_cubic(min(1.0, (t - 26.0) / 2.0)) * (1 - ease_in_cubic(max(0, (t - 30.0) / 2.0)))
        v = 1500 * env * math.sin(2 * math.pi * 660.0 * (t - 26.0))
        mix(int(t * SAMPLE_RATE), v)

    # ── AWE peak hit (32.0) ────────────────────────────────
    thump(31.6, freq=36.0, gain=2.5, length_s=1.4)
    noise_sweep(31.8, length_s=0.3, gain=0.8)
    chime(32.0, 523.25, gain=1.4, decay=0.7, length_s=2.5)
    chime(32.0, 659.25, gain=0.9, decay=0.7, length_s=2.5)
    chime(32.0, 783.99, gain=0.6, decay=0.7, length_s=2.5)
    # Sustained pad
    for k in range(int(SAMPLE_RATE * 5.0)):
        t = 32.5 + k / SAMPLE_RATE
        env = ease_out_cubic(min(1.0, (t - 32.5) / 1.5)) * (1 - ease_in_cubic(max(0, (t - 36.5) / 1.5)))
        v = 0.0
        v += 1800 * env * math.sin(2 * math.pi * 261.6 * (t - 32.5))
        v += 1100 * env * math.sin(2 * math.pi * 329.6 * (t - 32.5))
        mix(int(t * SAMPLE_RATE), v)

    # ── Pageful calm (38-44): just the drone, light texture ─
    for k in range(int(SAMPLE_RATE * 6.0)):
        t = 38.0 + k / SAMPLE_RATE
        env = 0.25 * (1 - abs((t - 41.0) / 3.0))
        v = 700 * env * math.sin(2 * math.pi * 220.0 * (t - 38.0))
        mix(int(t * SAMPLE_RATE), v)

    # ── Reassembly: bells ringing in ──────────────────────
    chime(44.5, 523.25, gain=0.9, decay=1.5)
    chime(45.5, 659.25, gain=0.7, decay=1.6)
    chime(47.0, 783.99, gain=0.9, decay=1.5)
    chime(48.5, 1046.5, gain=0.7, decay=1.4)
    chime(50.0, 1318.5, gain=0.8, decay=1.3)
    # Final swell
    chime(51.5, 523.25, gain=1.0, decay=0.5, length_s=3.0)

    # ── Silence: a single fading note ─────────────────────
    chime(52.5, 220.0, gain=0.7, decay=1.0, length_s=2.5)

    # Write
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
    print(f"freyja anthem v2 · {W}x{H} · {FPS} fps · {DURATION:.0f}s")
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
