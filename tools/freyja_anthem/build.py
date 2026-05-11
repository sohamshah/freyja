#!/usr/bin/env python3
"""freyja anthem — render a ~60s cinematic teaser grounded in the
Freyja aesthetic. Frames are drawn with PIL, audio is synthesized in
pure Python, the two are combined via ffmpeg.

Output: tools/freyja_anthem/freyja_anthem.mp4 (1920x1080, 30fps).

Run: python3 tools/freyja_anthem/build.py
Optional flags:
    --fast       lowers res to 1280x720 for quick previews
    --no-audio   skips audio synthesis (silent video)
    --frames-only renders frames but stops before ffmpeg
"""

from __future__ import annotations

import argparse
import math
import os
import random
import shutil
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
FRAMES_DIR = HERE / "frames"
AUDIO_PATH = HERE / "audio.wav"
OUT_PATH = HERE / "freyja_anthem.mp4"

W, H = 1920, 1080
FPS = 30
DURATION = 60.0
TOTAL_FRAMES = int(DURATION * FPS)

FONT_DIR = Path("/opt/homebrew/Caskroom/font-jetbrains-mono/2.304/fonts/ttf")
FONT_PATHS = {
    "regular": FONT_DIR / "JetBrainsMono-Regular.ttf",
    "light": FONT_DIR / "JetBrainsMono-Light.ttf",
    "medium": FONT_DIR / "JetBrainsMono-Medium.ttf",
    "bold": FONT_DIR / "JetBrainsMono-Bold.ttf",
    "italic": FONT_DIR / "JetBrainsMono-Italic.ttf",
    "light_italic": FONT_DIR / "JetBrainsMono-LightItalic.ttf",
    "thin": FONT_DIR / "JetBrainsMono-Thin.ttf",
}

# Colors — mirror the Freyja palette (see src/renderer/styles/globals.css).
BG_TOP = (10, 12, 16)
BG_BOT = (4, 5, 8)
PANEL_BG = (12, 14, 18, 255)
HAIRLINE = (255, 255, 255, 22)
HAIRLINE_BRIGHT = (255, 255, 255, 50)
HAIRLINE_FAINT = (255, 255, 255, 12)
FG_0 = (245, 245, 247)
FG_1 = (215, 218, 224)
FG_2 = (160, 165, 172)
FG_3 = (102, 105, 112)
FG_4 = (66, 68, 74)
ACCENT = (127, 184, 232)
ACCENT_DIM = (127, 184, 232, 140)
OK = (112, 184, 103)
WARN = (217, 162, 73)
DANGER = (193, 106, 106)
PAPER_CREAM = (216, 219, 200)
PAPER_TRIAGE = (158, 168, 158)
PAPER_INK = (32, 34, 22)
PAPER_INK_DIM = (84, 90, 76)
PAPER_INK_VERY_DIM = (134, 138, 124)
DARK_GRADIENT_TOP = (28, 32, 36)
DARK_GRADIENT_MID = (16, 18, 20)
DARK_GRADIENT_BOT = (8, 9, 10)
AMBER_TINT = (40, 28, 14)
GREEN_TINT = (14, 30, 18)
RED_TINT = (38, 14, 14)


# ============================================================
# Font cache
# ============================================================
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
# Easings
# ============================================================
def clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


def ease_out_cubic(t: float) -> float:
    t = clamp(t)
    return 1 - (1 - t) ** 3


def ease_in_out_cubic(t: float) -> float:
    t = clamp(t)
    return 4 * t**3 if t < 0.5 else 1 - (-2 * t + 2) ** 3 / 2


def ease_out_expo(t: float) -> float:
    t = clamp(t)
    return 1 if t >= 1 else 1 - 2 ** (-10 * t)


def smoothstep(t: float) -> float:
    t = clamp(t)
    return t * t * (3 - 2 * t)


def pulse(t: float, period: float, mid: float = 0.5, amp: float = 0.5) -> float:
    """Continuous pulse via sine; output in [mid-amp, mid+amp]."""
    return mid + amp * math.sin(2 * math.pi * t / period)


# ============================================================
# Color helpers
# ============================================================
def lerp(a: float, b: float, t: float) -> float:
    return a + (b - a) * t


def lerp_color(a: tuple, b: tuple, t: float) -> tuple:
    if len(a) == 4 and len(b) == 4:
        return (
            int(lerp(a[0], b[0], t)),
            int(lerp(a[1], b[1], t)),
            int(lerp(a[2], b[2], t)),
            int(lerp(a[3], b[3], t)),
        )
    return (
        int(lerp(a[0], b[0], t)),
        int(lerp(a[1], b[1], t)),
        int(lerp(a[2], b[2], t)),
    )


def with_alpha(rgb: tuple, a: int) -> tuple:
    return (rgb[0], rgb[1], rgb[2], int(a))


# ============================================================
# Pre-rendered overlays (vignette, grain, base gradient)
# ============================================================
_BASE_BG: Optional[Image.Image] = None
_VIGNETTE: Optional[Image.Image] = None
_GRAIN_FRAMES: list[Image.Image] = []


def base_bg() -> Image.Image:
    """Cached background — vertical gradient with a hint of accent in
    a soft top-left radial. Restrained, not flashy."""
    global _BASE_BG
    if _BASE_BG is not None:
        return _BASE_BG.copy()
    img = Image.new("RGB", (W, H), BG_BOT)
    px = img.load()
    for y in range(H):
        ratio = y / H
        r = int(lerp(BG_TOP[0], BG_BOT[0], ratio))
        g = int(lerp(BG_TOP[1], BG_BOT[1], ratio))
        b = int(lerp(BG_TOP[2], BG_BOT[2], ratio))
        for x in range(W):
            px[x, y] = (r, g, b)
    # Very faint top-left radial of warm accent
    overlay = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    odraw = ImageDraw.Draw(overlay)
    cx, cy = W * 0.15, H * 0.15
    max_r = max(W, H) * 0.9
    for step in range(40, 0, -1):
        r = step * max_r / 40
        a = int(7 * (step / 40))
        odraw.ellipse(
            (cx - r, cy - r, cx + r, cy + r),
            fill=(72, 110, 150, a),
        )
    img = Image.alpha_composite(img.convert("RGBA"), overlay)
    _BASE_BG = img
    return _BASE_BG.copy()


def vignette() -> Image.Image:
    """Cached vignette mask — darkens corners gently."""
    global _VIGNETTE
    if _VIGNETTE is not None:
        return _VIGNETTE
    mask = Image.new("L", (W, H), 0)
    md = ImageDraw.Draw(mask)
    cx, cy = W // 2, H // 2
    max_r = math.hypot(W // 2, H // 2)
    for step in range(100, 0, -1):
        r = step * max_r / 100
        a = int(80 * (step / 100) ** 2.5)
        md.ellipse((cx - r, cy - r, cx + r, cy + r), fill=255 - a)
    vig = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    vig.putalpha(ImageChops.invert(mask))
    _VIGNETTE = vig
    return _VIGNETTE


def grain_frames(n: int = 8, strength: int = 8) -> list[Image.Image]:
    """Cached set of grain frames — cycled to avoid static-looking noise."""
    global _GRAIN_FRAMES
    if _GRAIN_FRAMES:
        return _GRAIN_FRAMES
    rnd = random.Random(0xFEDA)
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
        # Scale alpha a bit so grain isn't overpowering
        _GRAIN_FRAMES.append(rgba)
    return _GRAIN_FRAMES


# ============================================================
# Drawing primitives
# ============================================================
def draw_hairline(draw: ImageDraw.ImageDraw, x1, y1, x2, y2, color=HAIRLINE, width=1):
    draw.line([(x1, y1), (x2, y2)], fill=color, width=width)


def draw_dot(draw: ImageDraw.ImageDraw, x, y, r, color, alpha: int = 255):
    if alpha == 255 and len(color) == 3:
        draw.ellipse((x - r, y - r, x + r, y + r), fill=color)
    else:
        if len(color) == 3:
            color = (*color, alpha)
        draw.ellipse((x - r, y - r, x + r, y + r), fill=color)


def draw_text(
    draw: ImageDraw.ImageDraw,
    x: int,
    y: int,
    text: str,
    f: ImageFont.FreeTypeFont,
    color=FG_1,
    align: str = "left",
    alpha: int = 255,
):
    if not text:
        return
    rgba = color if len(color) == 4 else (*color, alpha)
    if align == "center":
        w = text_w(text, f)
        x = x - w // 2
    elif align == "right":
        w = text_w(text, f)
        x = x - w
    draw.text((x, y), text, font=f, fill=rgba)


def draw_caret(draw: ImageDraw.ImageDraw, x, y, h, on: bool, color=FG_0):
    if not on:
        return
    draw.rectangle((x, y, x + max(1, h // 12), y + h), fill=color)


def typed(text: str, t: float, cps: float = 24.0, hold: float = 0.0) -> str:
    """Reveal `text` letter-by-letter starting at t=0. After fully
    revealed, holds for `hold` seconds before returning full text."""
    if t < 0:
        return ""
    chars = int(t * cps)
    return text[: min(len(text), chars)]


def rounded_rect(
    draw: ImageDraw.ImageDraw,
    box: tuple,
    radius: int,
    fill=None,
    outline=None,
    width: int = 1,
):
    draw.rounded_rectangle(box, radius=radius, fill=fill, outline=outline, width=width)


# ============================================================
# Kanban card render — the visual anchor of the whole piece
# ============================================================
def draw_paper_card(
    img: Image.Image,
    box: tuple,
    title: str,
    body: list[str],
    card_id: str,
    state: str = "ready",
    progress: float = 0.0,
    show_heartbeat: bool = False,
    heartbeat_t: float = 0.0,
    spec_lines: list[str] | None = None,
    age_label: str | None = None,
    verify_byline: str | None = None,
    rejection: str | None = None,
    mission: bool = False,
    fade: float = 1.0,
):
    """Render an index-card-paper kanban card. State controls the
    material: triage (dim paper), ready (warm cream), running (dark
    gradient), done_unverified (amber tint), done (green tint),
    failed/crashed (red tint)."""
    x1, y1, x2, y2 = box
    w = x2 - x1
    h = y2 - y1

    # Build the card on its own RGBA buffer so we can fade it.
    card = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    cd = ImageDraw.Draw(card)

    # Material per state
    if state == "ready":
        # Cream paper with subtle gradient and dark ink
        for y in range(h):
            t = y / max(1, h)
            r = int(lerp(224, 200, t))
            g = int(lerp(227, 203, t))
            b = int(lerp(211, 188, t))
            cd.line([(0, y), (w, y)], fill=(r, g, b, 255))
        ink = PAPER_INK
        ink_dim = PAPER_INK_DIM
        ink_very_dim = PAPER_INK_VERY_DIM
        # Subtle inner border
        cd.rounded_rectangle((0, 0, w - 1, h - 1), radius=8, outline=(140, 142, 126, 180), width=1)
        # Top highlight
        cd.line([(8, 1), (w - 8, 1)], fill=(255, 252, 240, 180), width=1)
    elif state == "triage":
        # Cooler grayish paper — looks "unfinished"
        for y in range(h):
            t = y / max(1, h)
            r = int(lerp(178, 152, t))
            g = int(lerp(188, 162, t))
            b = int(lerp(178, 152, t))
            cd.line([(0, y), (w, y)], fill=(r, g, b, 255))
        ink = (36, 42, 32)
        ink_dim = (76, 84, 70)
        ink_very_dim = (118, 124, 110)
        cd.rounded_rectangle((0, 0, w - 1, h - 1), radius=8, outline=(110, 118, 110, 180), width=1)
    elif state == "running":
        # Dark gradient
        for y in range(h):
            if y < h * 0.4:
                t = y / (h * 0.4)
                r = int(lerp(38, 22, t))
                g = int(lerp(42, 24, t))
                b = int(lerp(46, 28, t))
            else:
                t = (y - h * 0.4) / (h * 0.6)
                r = int(lerp(22, 10, t))
                g = int(lerp(24, 11, t))
                b = int(lerp(28, 12, t))
            cd.line([(0, y), (w, y)], fill=(r, g, b, 255))
        ink = FG_0
        ink_dim = FG_2
        ink_very_dim = FG_3
        cd.rounded_rectangle((0, 0, w - 1, h - 1), radius=10, outline=(255, 255, 255, 36), width=1)
    elif state == "done_unverified":
        for y in range(h):
            if y < h * 0.4:
                t = y / (h * 0.4)
                r = int(lerp(60, 38, t))
                g = int(lerp(42, 26, t))
                b = int(lerp(18, 12, t))
            else:
                t = (y - h * 0.4) / (h * 0.6)
                r = int(lerp(38, 22, t))
                g = int(lerp(26, 15, t))
                b = int(lerp(12, 8, t))
            cd.line([(0, y), (w, y)], fill=(r, g, b, 255))
        ink = FG_0
        ink_dim = (220, 188, 140)
        ink_very_dim = (180, 144, 88)
        cd.rounded_rectangle((0, 0, w - 1, h - 1), radius=10, outline=(*WARN, 90), width=1)
    elif state == "done":
        for y in range(h):
            if y < h * 0.4:
                t = y / (h * 0.4)
                r = int(lerp(30, 18, t))
                g = int(lerp(48, 28, t))
                b = int(lerp(28, 18, t))
            else:
                t = (y - h * 0.4) / (h * 0.6)
                r = int(lerp(18, 10, t))
                g = int(lerp(28, 14, t))
                b = int(lerp(18, 10, t))
            cd.line([(0, y), (w, y)], fill=(r, g, b, 255))
        ink = FG_0
        ink_dim = (160, 198, 156)
        ink_very_dim = FG_3
        cd.rounded_rectangle((0, 0, w - 1, h - 1), radius=10, outline=(*OK, 100), width=1)
    elif state == "failed":
        for y in range(h):
            if y < h * 0.4:
                t = y / (h * 0.4)
                r = int(lerp(58, 32, t))
                g = int(lerp(20, 12, t))
                b = int(lerp(20, 12, t))
            else:
                t = (y - h * 0.4) / (h * 0.6)
                r = int(lerp(32, 18, t))
                g = int(lerp(12, 7, t))
                b = int(lerp(12, 7, t))
            cd.line([(0, y), (w, y)], fill=(r, g, b, 255))
        ink = FG_0
        ink_dim = (210, 150, 150)
        ink_very_dim = FG_3
        cd.rounded_rectangle((0, 0, w - 1, h - 1), radius=10, outline=(*DANGER, 130), width=2)
    else:
        ink = FG_1
        ink_dim = FG_2
        ink_very_dim = FG_3

    # Scale font sizes with the card so a shrunken card during scene 7
    # doesn't get its title clipped against the right edge.
    scale_factor = 1.0
    if mission:
        # Reference width for the mission card is 760 (full hero size).
        scale_factor = max(0.55, min(1.0, w / 760))

    pad_x = 22 if not mission else max(14, int(28 * scale_factor))
    pad_y = 18 if not mission else max(12, int(22 * scale_factor))

    # Header row: id + state
    id_font = font("medium", 14 if not mission else max(11, int(16 * scale_factor)))
    state_font = font("medium", 11 if not mission else max(9, int(13 * scale_factor)))
    cd.text((pad_x, pad_y), card_id, font=id_font, fill=ink_dim)
    state_label = state.upper().replace("_", " ")
    id_width = text_w(card_id, id_font)
    cd.text(
        (pad_x + id_width + 14, pad_y + 1),
        state_label,
        font=state_font,
        fill=ink_very_dim if state in ("ready", "triage") else ink_dim,
    )
    if mission:
        # "MISSION" stamp upper right, very subtle
        m_font = font("bold", max(9, int(11 * scale_factor)))
        cd.text(
            (w - pad_x - text_w("MISSION", m_font), pad_y + 2),
            "MISSION",
            font=m_font,
            fill=(ink_very_dim if state in ("ready", "triage") else (*WARN, 220)),
        )

    # Title — pick a size that fits the available card width.
    title_pad = pad_x * 2
    available_w = w - title_pad
    title_size = 22 if not mission else int(28 * scale_factor)
    while title_size > 11:
        test_font = font("medium", title_size)
        if text_w(title, test_font) <= available_w - 8:
            break
        title_size -= 1
    title_font = font("medium", title_size)
    cd.text((pad_x, pad_y + 26 if not mission else pad_y + int(30 * scale_factor)), title, font=title_font, fill=ink)

    # Body lines
    body_y = pad_y + (60 if not mission else 70)
    body_font = font("regular", 14 if not mission else 16)
    for line in body:
        cd.text((pad_x, body_y), line, font=body_font, fill=ink_dim)
        body_y += 22

    # Spec lines (checklist) for triage→ready transitions
    if spec_lines:
        spec_y = body_y + 6
        spec_label_font = font("medium", 10)
        spec_font = font("regular", 12)
        cd.text(
            (pad_x, spec_y),
            "definition_of_done:",
            font=spec_label_font,
            fill=ink_very_dim,
        )
        spec_y += 16
        for line in spec_lines:
            cd.rectangle(
                (pad_x, spec_y + 3, pad_x + 8, spec_y + 11),
                outline=ink_very_dim,
                width=1,
            )
            cd.text((pad_x + 14, spec_y), line, font=spec_font, fill=ink_dim)
            spec_y += 16
        body_y = spec_y + 6

    # Progress bar at the bottom
    if state == "running" or state == "done_unverified":
        bar_y = h - pad_y - 8
        cd.rectangle((pad_x, bar_y, w - pad_x, bar_y + 3), fill=(255, 255, 255, 24))
        fill_w = int((w - 2 * pad_x) * progress)
        bar_color = (
            ACCENT if state == "running" else WARN
        )
        cd.rectangle((pad_x, bar_y, pad_x + fill_w, bar_y + 3), fill=bar_color)
    elif state == "done":
        bar_y = h - pad_y - 6
        cd.rectangle((pad_x, bar_y, w - pad_x, bar_y + 2), fill=(*OK, 200))

    # Heartbeat dot bottom-right
    if show_heartbeat and state == "running":
        hb_period = 1.2
        hb = pulse(heartbeat_t, hb_period, 0.6, 0.4)
        dot_a = int(180 * hb)
        cd.ellipse(
            (w - pad_x - 14, h - pad_y - 18, w - pad_x - 6, h - pad_y - 10),
            fill=(*ACCENT, dot_a),
        )

    # Age label (mission card)
    if age_label and mission:
        age_font = font("regular", 12)
        cd.text(
            (w - pad_x - text_w(age_label, age_font), h - pad_y - 16),
            age_label,
            font=age_font,
            fill=ink_very_dim,
        )

    # Verifier byline (after done)
    if verify_byline:
        bf = font("light_italic", 12)
        cd.text(
            (pad_x, h - pad_y - 18),
            verify_byline,
            font=bf,
            fill=(160, 198, 156, 220) if state == "done" else ink_very_dim,
        )

    # Rejection callout
    if rejection:
        rj_y = h - pad_y - 50
        cd.rectangle((pad_x, rj_y, pad_x + 3, rj_y + 38), fill=(*DANGER, 200))
        rj_label_font = font("medium", 10)
        cd.text(
            (pad_x + 10, rj_y),
            "REJECTED — verifier note",
            font=rj_label_font,
            fill=(*DANGER, 220),
        )
        rj_font = font("italic", 12)
        cd.text((pad_x + 10, rj_y + 16), rejection, font=rj_font, fill=ink_dim)

    # Composite drop shadow
    shadow = Image.new("RGBA", (w + 60, h + 80), (0, 0, 0, 0))
    sd = ImageDraw.Draw(shadow)
    sd.rounded_rectangle(
        (24, 28, w + 24, h + 32),
        radius=12,
        fill=(0, 0, 0, 110 if mission else 80),
    )
    shadow = shadow.filter(ImageFilter.GaussianBlur(16 if mission else 10))

    # Apply card fade
    if fade < 1.0:
        alpha_layer = card.split()[-1]
        alpha_layer = alpha_layer.point(lambda v: int(v * fade))
        card.putalpha(alpha_layer)
        sx, sy, sa = shadow.split()[:3], None, shadow.split()[-1]
        sa = shadow.split()[-1].point(lambda v: int(v * fade))
        shadow.putalpha(sa)

    img.alpha_composite(shadow, (x1 - 24, y1 - 28))
    img.alpha_composite(card, (x1, y1))


# ============================================================
# Tool-stream lines (inside a card)
# ============================================================
def draw_tool_stream(
    img: Image.Image,
    box: tuple,
    lines: list[tuple[str, str]],
    reveal_count: int,
    last_line_typing: float = 1.0,
):
    """Lines is list of (glyph, text). reveal_count is how many lines
    are fully visible; the next one types at `last_line_typing` ratio
    (0..1). Renders on dark background."""
    draw = ImageDraw.Draw(img)
    x1, y1, x2, y2 = box
    f = font("regular", 14)
    gf = font("light", 14)
    cx = x1
    cy = y1
    for i, (glyph, text) in enumerate(lines):
        if i < reveal_count:
            text_to_draw = text
        elif i == reveal_count:
            chars = int(len(text) * last_line_typing)
            text_to_draw = text[:chars]
        else:
            break
        draw.text((cx, cy), glyph, font=gf, fill=FG_3)
        draw.text((cx + 18, cy), text_to_draw, font=f, fill=FG_1 if i == reveal_count - 0 else FG_2)
        if i == reveal_count and chars < len(text):
            # blinking caret while typing
            caret_x = cx + 18 + text_w(text_to_draw, f) + 2
            draw.rectangle((caret_x, cy + 3, caret_x + 1, cy + 18), fill=FG_1)
        cy += 22


# ============================================================
# Scene system
# ============================================================
class Scene:
    def __init__(self, name: str, start: float, duration: float, draw_fn: Callable):
        self.name = name
        self.start = start
        self.duration = duration
        self.draw_fn = draw_fn

    def end(self) -> float:
        return self.start + self.duration


# ============================================================
# Scene 1: Awakening
# ============================================================
def scene_awakening(img: Image.Image, t: float, dur: float):
    draw = ImageDraw.Draw(img)
    cx = W // 2
    cy = H // 2

    # Phase A (0.0–1.0s): blinking caret in the void
    if t < 1.0:
        blink_on = (int(t * 2.5) % 2) == 0
        # tiny caret at center
        if blink_on:
            f = font("light", 60)
            ascent, _ = f.getmetrics()
            draw.rectangle((cx - 2, cy - ascent // 2, cx + 2, cy + ascent // 2 - 4), fill=FG_2)
        return

    # Phase B (1.0–3.5s): type "freyja"
    t2 = t - 1.0
    title = typed("freyja", t2, cps=4.0)
    title_font = font("light", 110)
    tw = text_w(title, title_font)
    draw.text((cx - tw // 2, cy - 80), title, font=title_font, fill=FG_0)
    # caret at end while typing
    if len(title) < len("freyja"):
        blink_on = (int(t * 4) % 2) == 0
        if blink_on:
            cx_after = cx - tw // 2 + tw + 6
            draw.rectangle((cx_after, cy - 60, cx_after + 4, cy + 30), fill=FG_2)

    # Phase C (3.0–5.0s): horizontal rule under the title widens out
    if t >= 3.0:
        rule_t = ease_out_cubic((t - 3.0) / 1.2)
        full_w = int(420 * rule_t)
        rule_y = cy + 40
        draw.line(
            [(cx - full_w // 2, rule_y), (cx + full_w // 2, rule_y)],
            fill=(255, 255, 255, int(120 * rule_t)),
            width=1,
        )

    # Phase D (3.6–5.0): subtitle fades in beneath
    if t >= 3.6:
        sub_t = ease_out_cubic((t - 3.6) / 1.0)
        sf = font("light_italic", 22)
        sub = "a coordination system for many minds"
        sw = text_w(sub, sf)
        draw.text(
            (cx - sw // 2, cy + 60),
            sub,
            font=sf,
            fill=(*FG_2, int(220 * sub_t)),
        )


# ============================================================
# Scene 2: The mission appears
# ============================================================
MISSION_TITLE = "Build a model routing system"
MISSION_PROMPT = "Pick the right LLM for each task. Tested, with fallbacks."


def scene_mission(img: Image.Image, t: float, dur: float):
    draw = ImageDraw.Draw(img)

    # Prompt area (top, types in)
    prompt_y = 240
    prompt_x = 220
    pf = font("regular", 22)
    pf_label = font("medium", 11)
    draw.text((prompt_x, prompt_y - 26), "USER PROMPT", font=pf_label, fill=FG_3)
    draw.text((prompt_x - 30, prompt_y + 2), ">", font=pf, fill=ACCENT)
    typed_text = typed(MISSION_PROMPT, t, cps=24)
    draw.text((prompt_x, prompt_y), typed_text, font=pf, fill=FG_0)
    if len(typed_text) < len(MISSION_PROMPT):
        blink = (int(t * 3.5) % 2) == 0
        if blink:
            cx = prompt_x + text_w(typed_text, pf) + 4
            draw.rectangle((cx, prompt_y + 4, cx + 3, prompt_y + 28), fill=FG_2)

    # After 2.0s, mission card materializes
    if t >= 2.2:
        appear_t = ease_out_cubic((t - 2.2) / 1.2)
        # Position: center, slightly below prompt
        card_w, card_h = 760, 220
        cx = W // 2
        cy = 600
        x1 = cx - card_w // 2
        y1 = cy - card_h // 2
        x2 = x1 + card_w
        y2 = y1 + card_h
        # Slide-up entrance
        y_offset = int(40 * (1 - appear_t))
        # Age label updates
        secs = max(0, int((t - 2.2)))
        age_label = f"00:00:{secs:02d}"
        draw_paper_card(
            img,
            (x1, y1 + y_offset, x2, y2 + y_offset),
            title=MISSION_TITLE,
            body=[MISSION_PROMPT],
            card_id="card_001",
            state="ready",
            mission=True,
            age_label=age_label,
            fade=appear_t,
        )
        # Subtle label beneath
        if t >= 3.4:
            sub_t = ease_out_cubic((t - 3.4) / 1.0)
            lf = font("light_italic", 18)
            label = "the mission has an anchor"
            lw = text_w(label, lf)
            draw.text(
                (W // 2 - lw // 2, y2 + 40),
                label,
                font=lf,
                fill=(*FG_3, int(200 * sub_t)),
            )


# ============================================================
# Scene 3: Decomposition
# ============================================================
CHILD_CARDS = [
    ("card_002", "Papers & Preprints", ["Find recent academic work."]),
    ("card_003", "OSS Frameworks", ["vllm · litellm · ray serve."]),
    ("card_004", "Industry Trends", ["Bedrock · Azure · OpenAI."]),
    ("card_005", "Agentic Routing", ["When routing matters most."]),
]


def scene_decomposition(img: Image.Image, t: float, dur: float):
    """Mission card moves to top, child cards slide in below."""
    draw = ImageDraw.Draw(img)

    # Mission card pinned at top, scaled smaller
    mission_t = ease_in_out_cubic(min(1.0, t / 1.6))
    mission_w_full = 760
    mission_w_small = 560
    mission_w = int(lerp(mission_w_full, mission_w_small, mission_t))
    mission_h_full = 220
    mission_h_small = 130
    mission_h = int(lerp(mission_h_full, mission_h_small, mission_t))
    mission_cy_full = 600
    mission_cy_small = 160
    mission_cy = int(lerp(mission_cy_full, mission_cy_small, mission_t))
    mission_cx = W // 2
    mx1 = mission_cx - mission_w // 2
    my1 = mission_cy - mission_h // 2
    mx2 = mx1 + mission_w
    my2 = my1 + mission_h

    secs = int(t + 1)
    age_label = f"00:00:{secs:02d}"
    draw_paper_card(
        img,
        (mx1, my1, mx2, my2),
        title=MISSION_TITLE,
        body=[MISSION_PROMPT] if mission_t < 0.5 else [],
        card_id="card_001",
        state="ready",
        mission=True,
        age_label=age_label,
    )

    # Child cards: stagger in starting at t=1.0
    child_w = 380
    child_h = 220
    base_y = 380
    spacing = (W - 200 - 4 * child_w) // 3
    start_x = 100

    for i, (cid, ctitle, cbody) in enumerate(CHILD_CARDS):
        stagger = 1.0 + i * 0.25
        if t < stagger:
            continue
        local_t = t - stagger
        slide_t = ease_out_cubic(min(1.0, local_t / 0.8))
        x1 = start_x + i * (child_w + spacing)
        y_offset = int(60 * (1 - slide_t))
        y1 = base_y + y_offset
        x2 = x1 + child_w
        y2 = y1 + child_h
        draw_paper_card(
            img,
            (x1, y1, x2, y2),
            title=ctitle,
            body=cbody,
            card_id=cid,
            state="triage",
            fade=slide_t,
        )
        # Connecting hairline from mission to child
        if slide_t > 0.5:
            line_alpha = int(80 * (slide_t - 0.5) * 2)
            mid_x = mission_cx + (i - 1.5) * 80
            draw_hairline(
                draw, mid_x, my2 + 6, x1 + child_w // 2, y1 - 6,
                color=(*ACCENT, line_alpha), width=1,
            )

    # Label beneath
    if t >= 3.0:
        sub_t = ease_out_cubic((t - 3.0) / 0.8)
        lf = font("light_italic", 18)
        label = "decomposition · triage"
        lw = text_w(label, lf)
        draw.text(
            (W // 2 - lw // 2, base_y + child_h + 40),
            label,
            font=lf,
            fill=(*FG_3, int(200 * sub_t)),
        )


SPEC_LINES = [
    ["Surface 3 most cited", "Group by year + venue"],
    ["Compare API surfaces", "Note failure modes"],
    ["Pricing per token", "Region availability"],
    ["Routing strategies", "Cost-aware fallback"],
]


def scene_specifier(img: Image.Image, t: float, dur: float):
    """Specifier dot arcs to each card, expands spec, card becomes ready."""
    draw = ImageDraw.Draw(img)

    # Static elements: mission card + child cards
    mission_w = 560
    mission_h = 130
    mission_cx = W // 2
    mx1 = mission_cx - mission_w // 2
    my1 = 160 - mission_h // 2
    mx2 = mx1 + mission_w
    my2 = my1 + mission_h
    age_secs = int(t + 7)
    draw_paper_card(
        img,
        (mx1, my1, mx2, my2),
        title=MISSION_TITLE,
        body=[],
        card_id="card_001",
        state="ready",
        mission=True,
        age_label=f"00:00:{age_secs:02d}",
    )

    child_w = 380
    child_h = 280  # taller to fit spec list
    base_y = 380
    spacing = (W - 200 - 4 * child_w) // 3
    start_x = 100

    # Each card transitions triage → ready when the specifier dot arrives.
    # Specifier visits one card every 0.6s with a small overlap.
    for i, (cid, ctitle, cbody) in enumerate(CHILD_CARDS):
        arrival_t = 0.5 + i * 0.45
        spec_local_t = (t - arrival_t) / 0.8
        is_ready = t > arrival_t + 0.8
        x1 = start_x + i * (child_w + spacing)
        y1 = base_y
        x2 = x1 + child_w
        y2 = y1 + child_h
        spec_lines_visible = []
        if t > arrival_t:
            # Type out spec lines progressively
            for k, line in enumerate(SPEC_LINES[i]):
                line_start = arrival_t + 0.15 + k * 0.18
                if t > line_start:
                    chars = int((t - line_start) * 30)
                    spec_lines_visible.append(line[:chars])
                else:
                    break
        state = "ready" if is_ready else "triage"
        draw_paper_card(
            img,
            (x1, y1, x2, y2),
            title=ctitle,
            body=cbody,
            card_id=cid,
            state=state,
            spec_lines=spec_lines_visible if t > arrival_t else None,
        )
        # Specifier dot: arcs from top-center to card top-center
        if t < arrival_t and t > arrival_t - 0.7:
            arc_t = ease_in_out_cubic((t - (arrival_t - 0.7)) / 0.7)
            sx = lerp(mission_cx, x1 + child_w // 2, arc_t)
            sy_baseline_top = my2
            sy_baseline_bot = y1
            # Add arc — go up and over
            arc_height = -50
            sy = (
                lerp(sy_baseline_top, sy_baseline_bot, arc_t)
                + arc_height * math.sin(math.pi * arc_t)
            )
            draw_dot(draw, sx, sy, 6, ACCENT)
            # trail
            for trail in range(1, 6):
                trail_t = max(0.0, arc_t - 0.04 * trail)
                tx = lerp(mission_cx, x1 + child_w // 2, trail_t)
                ty = (
                    lerp(sy_baseline_top, sy_baseline_bot, trail_t)
                    + arc_height * math.sin(math.pi * trail_t)
                )
                draw_dot(draw, tx, ty, 4, ACCENT, alpha=max(0, 180 - trail * 30))

    # Label
    if t >= 0.6:
        sub_t = ease_out_cubic(min(1.0, (t - 0.6) / 1.0))
        lf = font("light_italic", 18)
        label = "specifier expands"
        lw = text_w(label, lf)
        draw.text(
            (W // 2 - lw // 2, base_y + child_h + 40),
            label,
            font=lf,
            fill=(*FG_3, int(200 * sub_t)),
        )


WORKER_TOOL_STREAMS = [
    [("├", "Fetching arxiv.org/abs/2502.0..."),
     ("├", "Reading routing-survey.pdf"),
     ("├", "Searching web · transformer routing"),
     ("├", "kanban heartbeat"),
     ("└", "kanban update · summary")],
    [("├", "Searching web · 'vllm router'"),
     ("├", "Fetching docs.litellm.ai/release"),
     ("├", "Reading ray-serve readme"),
     ("├", "kanban heartbeat"),
     ("└", "kanban update · summary")],
    [("├", "Fetching aws.amazon.com/bedrock"),
     ("├", "$ mkdir -p notes/industry"),
     ("├", "Fetching learn.microsoft.com/..."),
     ("├", "kanban heartbeat"),
     ("└", "kanban complete")],
    [("├", "Searching web · 'agentic routing'"),
     ("├", "Fetching zylos.ai/research/20..."),
     ("├", "Reading agentic-patterns.md"),
     ("├", "kanban heartbeat"),
     ("└", "kanban update · summary")],
]


def scene_workers(img: Image.Image, t: float, dur: float):
    """Worker dots arc into cards; cards become running; tool streams play."""
    draw = ImageDraw.Draw(img)

    # Mission card
    mission_w = 560
    mission_h = 130
    mission_cx = W // 2
    mx1 = mission_cx - mission_w // 2
    my1 = 160 - mission_h // 2
    mx2 = mx1 + mission_w
    my2 = my1 + mission_h
    age_secs = int(t + 15)
    draw_paper_card(
        img,
        (mx1, my1, mx2, my2),
        title=MISSION_TITLE,
        body=[],
        card_id="card_001",
        state="ready",
        mission=True,
        age_label=f"00:00:{age_secs:02d}",
    )

    # Narrator italic line at top
    narrator_y = my2 + 30
    nf = font("light_italic", 18)
    narrator_t = ease_out_cubic(min(1.0, t / 0.8))
    narrator = "dispatching explore on card_002, card_003, card_004, card_005"
    nw = text_w(narrator, nf)
    draw.text(
        (W // 2 - nw // 2, narrator_y),
        narrator,
        font=nf,
        fill=(*FG_3, int(180 * narrator_t)),
    )

    child_w = 380
    child_h = 320
    base_y = 420
    spacing = (W - 200 - 4 * child_w) // 3
    start_x = 100

    for i, (cid, ctitle, cbody) in enumerate(CHILD_CARDS):
        start_running = 0.6 + i * 0.18
        x1 = start_x + i * (child_w + spacing)
        y1 = base_y
        x2 = x1 + child_w
        y2 = y1 + child_h
        if t < start_running:
            state = "ready"
        else:
            state = "running"
        local_t = max(0.0, t - start_running)
        # Tool stream: each line reveals over 0.6s
        per_line = 0.65
        line_idx = int(local_t / per_line)
        last_progress = (local_t - line_idx * per_line) / per_line
        last_progress = clamp(last_progress)
        # Progress bar = overall running progress
        running_progress = clamp(local_t / (per_line * 5))
        draw_paper_card(
            img,
            (x1, y1, x2, y2),
            title=ctitle,
            body=cbody,
            card_id=cid,
            state=state,
            progress=running_progress,
            show_heartbeat=(state == "running"),
            heartbeat_t=t,
        )
        # Render tool stream lines in the lower half of the card
        if state == "running":
            stream_box = (x1 + 22, y1 + 130, x2 - 22, y2 - 36)
            draw_tool_stream(
                img, stream_box, WORKER_TOOL_STREAMS[i],
                reveal_count=line_idx, last_line_typing=last_progress,
            )
        # Worker dot arc just before card flips to running
        if start_running - 0.6 < t < start_running:
            arc_t = ease_in_out_cubic((t - (start_running - 0.6)) / 0.6)
            sx = lerp(mission_cx, x1 + child_w // 2, arc_t)
            sy = lerp(my2, y1, arc_t) + -40 * math.sin(math.pi * arc_t)
            draw_dot(draw, sx, sy, 5, ACCENT)


# ============================================================
# Scene 6: Verification
# ============================================================
VERIFY_TOOLS = [
    ("├", "$ pytest tests/test_routing.py"),
    ("├", "Reading models/routing.py"),
    ("├", "kanban update · status=done"),
    ("└", "✓ verified"),
]
REJECT_TOOLS = [
    ("├", "$ pytest tests/test_industry.py"),
    ("├", "Reading regions.json"),
    ("├", "kanban update · status=running"),
    ("└", "✗ missing Bedrock fallback"),
]
ROUND2_TOOLS = [
    ("├", "Fetching aws.amazon.com/bedrock/regions"),
    ("├", "Writing notes/industry.md"),
    ("├", "kanban heartbeat"),
    ("└", "kanban complete"),
]


def scene_verification(img: Image.Image, t: float, dur: float):
    """Cards transition done_unverified → verified, with one rejection."""
    draw = ImageDraw.Draw(img)

    # Mission card
    mission_w = 560
    mission_h = 130
    mission_cx = W // 2
    mx1 = mission_cx - mission_w // 2
    my1 = 160 - mission_h // 2
    mx2 = mx1 + mission_w
    my2 = my1 + mission_h
    age_secs = int(t + 25)
    draw_paper_card(
        img,
        (mx1, my1, mx2, my2),
        title=MISSION_TITLE,
        body=[],
        card_id="card_001",
        state="ready",
        mission=True,
        age_label=f"00:00:{age_secs:02d}",
    )

    child_w = 380
    child_h = 320
    base_y = 420
    spacing = (W - 200 - 4 * child_w) // 3
    start_x = 100

    # Per-card timeline:
    # card_002 (idx 0): t=0.0 → done_unverified; t=0.8 → verify in; t=2.0 → done
    # card_003 (idx 1): t=0.4 → done directly (no verification)
    # card_004 (idx 2): t=0.5 → done_unverified; t=1.3 → verify in;
    #                   t=2.5 → rejected; t=3.0 → running round 2; t=5.0 → done
    # card_005 (idx 3): t=0.8 → done directly

    def card_state(i):
        if i == 0:
            if t < 0.0:
                return "running"
            if t < 2.2:
                return "done_unverified"
            return "done"
        if i == 1:
            if t < 0.4:
                return "running"
            return "done"
        if i == 2:
            if t < 0.5:
                return "running"
            if t < 2.5:
                return "done_unverified"
            if t < 5.0:
                return "running"  # round 2
            return "done"
        if i == 3:
            if t < 0.8:
                return "running"
            return "done"
        return "ready"

    for i, (cid, ctitle, cbody) in enumerate(CHILD_CARDS):
        x1 = start_x + i * (child_w + spacing)
        y1 = base_y
        x2 = x1 + child_w
        y2 = y1 + child_h
        state = card_state(i)
        # Verifier byline + extras
        byline = None
        rejection = None
        if i in (0, 2) and state == "done":
            byline = "verified · gpt-5.5"
        if i == 2 and state == "running" and t > 2.5:
            rejection = "missing Bedrock fallback path"
        draw_paper_card(
            img,
            (x1, y1, x2, y2),
            title=ctitle,
            body=cbody,
            card_id=cid,
            state=state,
            verify_byline=byline,
            rejection=rejection,
            show_heartbeat=(state == "running"),
            heartbeat_t=t,
            progress=clamp(t / 5.0),
        )
        # Tool streams inside cards based on state
        if state == "done_unverified":
            # Show verifier arriving
            tools = VERIFY_TOOLS if i == 0 else REJECT_TOOLS
            local_t = (t - (0.8 if i == 0 else 1.3))
            per_line = 0.32
            line_idx = max(0, int(local_t / per_line))
            last_progress = clamp((local_t - line_idx * per_line) / per_line)
            stream_box = (x1 + 22, y1 + 130, x2 - 22, y2 - 36)
            draw_tool_stream(img, stream_box, tools, reveal_count=line_idx, last_line_typing=last_progress)
        elif state == "running" and i == 2 and t > 2.5:
            # Round 2 worker
            local_t = t - 3.0
            per_line = 0.35
            line_idx = max(0, int(local_t / per_line))
            last_progress = clamp((local_t - line_idx * per_line) / per_line)
            stream_box = (x1 + 22, y1 + 130, x2 - 22, y2 - 64)
            draw_tool_stream(img, stream_box, ROUND2_TOOLS, reveal_count=line_idx, last_line_typing=last_progress)

    # Label at the bottom
    if t >= 0.6:
        sub_t = ease_out_cubic(min(1.0, (t - 0.6) / 1.5))
        lf = font("light_italic", 20)
        label = "completion is a promotion · not a self-declaration"
        lw = text_w(label, lf)
        draw.text(
            (W // 2 - lw // 2, base_y + child_h + 40),
            label,
            font=lf,
            fill=(*FG_3, int(220 * sub_t)),
        )


# ============================================================
# Scene 7: Whole board pulse
# ============================================================
def scene_whole_board(img: Image.Image, t: float, dur: float):
    """Camera pulls back; all cards visible; heartbeats sync."""
    draw = ImageDraw.Draw(img)

    # Scale-down: at t=0 mid-size, scale down further as t progresses
    scale = lerp(1.0, 0.78, ease_in_out_cubic(min(1.0, t / 1.5)))

    # Compute scaled positions
    def s(v):
        return int(W // 2 + (v - W // 2) * scale)

    def sy(v):
        return int(H // 2 + (v - H // 2) * scale)

    # Mission card
    mission_w = int(560 * scale)
    mission_h = int(130 * scale)
    mission_cx = W // 2
    mission_cy = sy(160)
    mx1 = mission_cx - mission_w // 2
    my1 = mission_cy - mission_h // 2
    mx2 = mx1 + mission_w
    my2 = my1 + mission_h
    age_secs = int(t + 50)
    draw_paper_card(
        img,
        (mx1, my1, mx2, my2),
        title=MISSION_TITLE,
        body=[],
        card_id="card_001",
        state="ready",
        mission=True,
        age_label=f"00:00:{age_secs:02d}",
    )

    child_w = int(380 * scale)
    child_h = int(320 * scale)
    base_y = sy(420)
    spacing = (W - 200 - 4 * child_w) // 3
    start_x = (W - 4 * child_w - 3 * spacing) // 2

    final_states = ["done", "done", "done", "done"]
    bylines = ["verified · gpt-5.5", None, "verified · gpt-5.5", None]
    for i, (cid, ctitle, cbody) in enumerate(CHILD_CARDS):
        x1 = start_x + i * (child_w + spacing)
        y1 = base_y
        x2 = x1 + child_w
        y2 = y1 + child_h
        draw_paper_card(
            img,
            (x1, y1, x2, y2),
            title=ctitle,
            body=cbody,
            card_id=cid,
            state=final_states[i],
            verify_byline=bylines[i],
        )

    # Synthesis cards on 2nd tier — appear partway through
    if t > 0.8:
        synth_t = ease_out_cubic(min(1.0, (t - 0.8) / 1.5))
        synth_w = int(420 * scale)
        synth_h = int(240 * scale)
        synth_y = base_y + child_h + 60
        synth_x_start = W // 2 - synth_w - 40
        synthesis = [
            (
                "card_006",
                "Synthesis · routing tree",
                "running",
                [("├", "Reading siblings"),
                 ("├", "Writing routing.md"),
                 ("└", "kanban heartbeat")],
            ),
            (
                "card_007",
                "Synthesis · audit notes",
                "running",
                [("├", "Reading siblings"),
                 ("├", "Writing audit.md"),
                 ("└", "kanban heartbeat")],
            ),
        ]
        for j, (cid, ctitle, cstate, tool_lines) in enumerate(synthesis):
            x1 = synth_x_start + j * (synth_w + 80)
            y1 = synth_y
            x2 = x1 + synth_w
            y2 = y1 + synth_h
            draw_paper_card(
                img,
                (x1, y1, x2, y2),
                title=ctitle,
                body=[],
                card_id=cid,
                state=cstate,
                progress=synth_t * 0.7,
                show_heartbeat=True,
                heartbeat_t=t,
                fade=synth_t,
            )
            # Tool stream reveal — three lines unfold over 1.5s
            per_line = 0.4
            line_idx = min(len(tool_lines), int((t - 0.8) / per_line))
            last_progress = clamp(((t - 0.8) - line_idx * per_line) / per_line)
            if synth_t > 0.6:
                stream_box = (x1 + 22, y1 + 90, x2 - 22, y2 - 30)
                draw_tool_stream(
                    img, stream_box, tool_lines,
                    reveal_count=line_idx, last_line_typing=last_progress,
                )

    # Final label, more declarative
    if t >= 2.0:
        sub_t = ease_out_cubic(min(1.0, (t - 2.0) / 1.0))
        lf = font("light", 22)
        label = "the board runs the work."
        lw = text_w(label, lf)
        draw.text(
            (W // 2 - lw // 2, H - 100),
            label,
            font=lf,
            fill=(*FG_1, int(230 * sub_t)),
        )


# ============================================================
# Scene 8: Tagline
# ============================================================
def scene_tagline(img: Image.Image, t: float, dur: float):
    draw = ImageDraw.Draw(img)
    # Strong fade-in from black
    fade_t = ease_out_cubic(min(1.0, t / 0.4))
    cx = W // 2
    cy = H // 2

    # "freyja"
    tf = font("light", 110)
    label = "freyja"
    lw = text_w(label, tf)
    draw.text(
        (cx - lw // 2, cy - 80),
        label,
        font=tf,
        fill=(*FG_0, int(255 * fade_t)),
    )

    # Underline rule expands
    if t >= 0.3:
        rule_t = ease_out_cubic(min(1.0, (t - 0.3) / 0.6))
        full_w = int(440 * rule_t)
        rule_y = cy + 40
        draw.line(
            [(cx - full_w // 2, rule_y), (cx + full_w // 2, rule_y)],
            fill=(255, 255, 255, int(150 * rule_t)),
            width=1,
        )

    # Tagline
    if t >= 0.7:
        sub_t = ease_out_cubic(min(1.0, (t - 0.7) / 0.6))
        sf = font("light_italic", 26)
        tagline = "the page writes itself."
        tw = text_w(tagline, sf)
        draw.text(
            (cx - tw // 2, cy + 60),
            tagline,
            font=sf,
            fill=(*FG_1, int(220 * sub_t)),
        )

    # Final blinking caret
    if t >= 1.4:
        blink = (int(t * 2.5) % 2) == 0
        if blink:
            sub_after_x = cx + text_w("the page writes itself.", font("light_italic", 26)) // 2 + 8
            draw.rectangle((sub_after_x, cy + 64, sub_after_x + 3, cy + 92), fill=FG_1)


# ============================================================
# Timeline
# ============================================================
SCENES = [
    Scene("awakening",     0.0,  5.0,  scene_awakening),
    Scene("mission",       5.0,  6.0,  scene_mission),
    Scene("decomposition", 11.0, 7.0,  scene_decomposition),
    Scene("specifier",     18.0, 6.0,  scene_specifier),
    Scene("workers",       24.0, 11.0, scene_workers),
    Scene("verification",  35.0, 14.0, scene_verification),
    Scene("whole_board",   49.0, 8.0,  scene_whole_board),
    Scene("tagline",       57.0, 3.0,  scene_tagline),
]


def render_frame(frame_idx: int) -> Image.Image:
    t_global = frame_idx / FPS
    img = base_bg().convert("RGBA")

    # Find the active scene + cross-fade overlap
    active_scenes = []
    for sc in SCENES:
        if sc.start <= t_global < sc.end() + 0.4:  # short trailing crossfade
            active_scenes.append(sc)

    for sc in active_scenes:
        local_t = t_global - sc.start
        if local_t < 0:
            continue
        # Crossfade alpha
        if local_t > sc.duration:
            # In the trailing fade-out region
            fade_out = clamp(1 - (local_t - sc.duration) / 0.4)
        else:
            fade_in = clamp(local_t / 0.3)
            fade_out = 1.0
            local_alpha = min(fade_in, 1.0)

        # Render scene onto a fresh transparent layer for proper compositing
        scene_layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        sc.draw_fn(scene_layer, local_t, sc.duration)
        if local_t > sc.duration:
            # Fade scene out
            alpha = scene_layer.split()[-1]
            alpha = alpha.point(lambda v: int(v * fade_out))
            scene_layer.putalpha(alpha)
        img = Image.alpha_composite(img, scene_layer)

    # Apply grain (cycled)
    grains = grain_frames()
    grain_idx = frame_idx % len(grains)
    img = Image.alpha_composite(img, grains[grain_idx])

    # Apply vignette
    img = Image.alpha_composite(img, vignette())

    return img.convert("RGB")


# ============================================================
# Audio synthesis (pure Python)
# ============================================================
SAMPLE_RATE = 44100


def synth_audio(out_path: Path) -> None:
    """Synthesize the soundtrack: a low drone, heartbeat ticks, light
    typing texture, and a final swell. All pure-Python — no numpy."""
    total_samples = int(SAMPLE_RATE * DURATION)
    # Use a Python list of ints to build the waveform, then pack.
    buf = [0] * total_samples

    # Helper to add a sample with overflow clamp
    def mix(i: int, v: float):
        if 0 <= i < total_samples:
            s = buf[i] + v
            if s > 32767:
                s = 32767
            elif s < -32768:
                s = -32768
            buf[i] = s

    # ── Drone: layered sines with slow LFO ─────────────────
    # The drone fades in early, holds across the piece, swells in the final
    # 10s, and fades cleanly at the end.
    base_freqs = [55.0, 110.0, 138.5, 220.0]  # A1, A2, C#3, A3 (minor-ish)
    base_amps = [3500, 2400, 800, 700]
    for i in range(total_samples):
        t = i / SAMPLE_RATE
        env = 0.0
        if t < 4.0:
            env = ease_out_cubic(t / 4.0) * 0.55
        elif t < 49.0:
            env = 0.55 + 0.10 * math.sin(2 * math.pi * t / 7.0)
        elif t < 57.0:
            env = 0.65 + 0.25 * ease_in_out_cubic((t - 49.0) / 8.0)
        elif t < 59.5:
            env = 0.90
        else:
            env = 0.90 * (1.0 - (t - 59.5) / 0.5)
        env = clamp(env, 0.0, 1.0)
        v = 0.0
        for f, a in zip(base_freqs, base_amps):
            v += a * env * math.sin(2 * math.pi * f * t)
        # subtle LFO on amplitude
        lfo = 0.85 + 0.15 * math.sin(2 * math.pi * t * 0.07)
        v *= lfo
        mix(i, v)

    # ── Type clicks during text reveal scenes ──────────────
    # Short dampened noise bursts during scenes 1 & 2.
    rnd = random.Random(0x5A1A)

    def click_at(t_sec: float, gain: float = 1.0):
        center = int(t_sec * SAMPLE_RATE)
        length = 700
        for k in range(length):
            env = math.exp(-k / 90.0)
            sample = rnd.uniform(-1.0, 1.0) * 4500 * env * gain
            mix(center + k, sample)

    # Scene 1: typing "freyja" — 6 clicks over ~1.5s starting at t=1.0
    for k in range(6):
        click_at(1.0 + k * 0.25, gain=0.6)

    # Scene 2: typing user prompt — many clicks over ~2.0s starting at t=5.0
    prompt_len = len(MISSION_PROMPT)
    cps = 24
    for k in range(prompt_len):
        if k % 2 == 0:  # every other char to avoid being too dense
            click_at(5.0 + k / cps, gain=0.45)

    # ── Heartbeat ticks during workers + verification ──────
    # Soft thump at ~50bpm starting at t=24, fading out at t=49.
    def heartbeat_at(t_sec: float, gain: float = 1.0):
        center = int(t_sec * SAMPLE_RATE)
        for k in range(8000):
            t_local = k / SAMPLE_RATE
            env = math.exp(-t_local * 7.0)
            v = math.sin(2 * math.pi * 60.0 * t_local) * 7000 * env * gain
            mix(center + k, v)

    beats_start = 24.0
    beats_end = 49.0
    bpm = 50
    interval = 60.0 / bpm
    t_beat = beats_start
    while t_beat < beats_end:
        # gain ramps up then down
        gain = 0.6
        if t_beat < beats_start + 2.0:
            gain = 0.6 * ease_out_cubic((t_beat - beats_start) / 2.0)
        elif t_beat > beats_end - 2.0:
            gain = 0.6 * (1.0 - ease_out_cubic((t_beat - (beats_end - 2.0)) / 2.0))
        heartbeat_at(t_beat, gain)
        # double-tap pattern (lub-dub)
        heartbeat_at(t_beat + 0.18, gain * 0.55)
        t_beat += interval

    # ── Verifier seal bell tones — small chimes at verification successes ──
    def chime_at(t_sec: float, freq: float, gain: float = 1.0, decay: float = 0.9):
        center = int(t_sec * SAMPLE_RATE)
        length = int(SAMPLE_RATE * 1.4)
        for k in range(length):
            t_local = k / SAMPLE_RATE
            env = math.exp(-t_local * decay)
            # Bell = fundamental + 2.4x harmonic + slight 4x
            v = (
                math.sin(2 * math.pi * freq * t_local) * 0.7
                + math.sin(2 * math.pi * freq * 2.4 * t_local) * 0.25
                + math.sin(2 * math.pi * freq * 4.1 * t_local) * 0.10
            ) * 4500 * env * gain
            mix(center + k, v)

    # card_002 seals (around t=37.2 absolute)
    chime_at(35.0 + 2.2, 880.0, gain=0.7, decay=2.5)
    # card_003 (no verify) — softer chime at "done"
    chime_at(35.0 + 0.4, 660.0, gain=0.45, decay=3.0)
    # card_005 (no verify)
    chime_at(35.0 + 0.8, 660.0, gain=0.45, decay=3.0)
    # card_004 rejection — short dampened low tone
    chime_at(35.0 + 2.5, 330.0, gain=0.5, decay=4.5)
    # card_004 final seal
    chime_at(35.0 + 5.0, 880.0, gain=0.7, decay=2.5)

    # ── Final swell + tagline note ─────────────────────────
    chime_at(57.5, 523.25, gain=1.0, decay=1.5)  # C5 — clean final note
    chime_at(58.0, 783.99, gain=0.6, decay=2.0)  # G5 above

    # Write WAV
    with wave.open(str(out_path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(SAMPLE_RATE)
        wf.writeframes(struct.pack("<" + "h" * len(buf), *(int(v) for v in buf)))


# ============================================================
# Orchestration
# ============================================================
def main():
    global W, H, TOTAL_FRAMES
    ap = argparse.ArgumentParser()
    ap.add_argument("--fast", action="store_true", help="render at 1280x720")
    ap.add_argument("--no-audio", action="store_true", help="skip audio synthesis")
    ap.add_argument("--frames-only", action="store_true")
    ap.add_argument("--start-frame", type=int, default=0)
    ap.add_argument("--end-frame", type=int, default=None)
    args = ap.parse_args()

    if args.fast:
        W, H = 1280, 720

    end_frame = args.end_frame if args.end_frame is not None else TOTAL_FRAMES
    print(f"freyja anthem · {W}x{H} · {FPS} fps · {DURATION:.0f}s")
    print(f"rendering frames {args.start_frame}..{end_frame - 1} -> {FRAMES_DIR}")

    FRAMES_DIR.mkdir(parents=True, exist_ok=True)

    # Render frames
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
        print("frames-only mode; stopping before audio/ffmpeg.")
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
