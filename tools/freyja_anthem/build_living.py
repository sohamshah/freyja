#!/usr/bin/env python3
"""freyja living system · single continuous swarm.

One particle system. ~350 agents. They reorganize themselves through
the coordination modes — isolation, kanban flow, goal convergence,
bus constellation — as a single continuous evolution. The icon at
center is the eternal anchor, pulsing waves outward at peak beats.

Output: tools/freyja_anthem/freyja_living.mp4 (1920x1080, 30fps).
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
FRAMES_DIR = HERE / "frames_living"
AUDIO_PATH = HERE / "audio_living.wav"
OUT_PATH = HERE / "freyja_living.mp4"

W, H = 1920, 1080
FPS = 30
DURATION = 50.0
TOTAL_FRAMES = int(DURATION * FPS)
N_PARTICLES = 360

DEPARTURE = Path("/Users/sohamshah/Library/Fonts/DepartureMono-Regular.otf")
JB_LIGHT = Path("/opt/homebrew/Caskroom/font-jetbrains-mono/2.304/fonts/ttf/JetBrainsMono-Light.ttf")
JB_LIGHT_ITALIC = Path("/opt/homebrew/Caskroom/font-jetbrains-mono/2.304/fonts/ttf/JetBrainsMono-LightItalic.ttf")
JB_REGULAR = Path("/opt/homebrew/Caskroom/font-jetbrains-mono/2.304/fonts/ttf/JetBrainsMono-Regular.ttf")

BG_TOP = (12, 14, 18)
BG_BOT = (3, 4, 6)
CONTOUR_OUTER = (76, 96, 118)
CONTOUR_INNER = (170, 196, 224)
FG_0 = (245, 245, 247)
FG_1 = (210, 213, 218)
FG_2 = (152, 156, 164)
FG_3 = (96, 100, 108)
FG_4 = (62, 64, 70)
ACCENT = (127, 184, 232)
ACCENT_WARM = (180, 205, 235)
OK = (112, 184, 103)
WARN = (217, 162, 73)
DANGER = (193, 106, 106)
VIOLET = (160, 138, 200)
TEAL = (100, 178, 170)


_FONTS: dict[tuple[str, int], ImageFont.FreeTypeFont] = {}


def font(path: Path, size: int) -> ImageFont.FreeTypeFont:
    key = (str(path), size)
    if key not in _FONTS:
        _FONTS[key] = ImageFont.truetype(str(path), size)
    return _FONTS[key]


def text_w(s: str, f) -> int:
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
        # Soft top-left bias of accent for depth
        overlay = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        od = ImageDraw.Draw(overlay)
        cx, cy = int(W * 0.35), int(H * 0.30)
        for step in range(30, 0, -1):
            r = step * 700 / 30
            a = int(4 * (step / 30))
            od.ellipse(
                (cx - r, cy - r, cx + r, cy + r),
                fill=(70, 110, 145, a),
            )
        _BG = Image.alpha_composite(img.convert("RGBA"), overlay)
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
            a = int(110 * (step / 100) ** 2.5)
            md.ellipse((cx - r, cy - r, cx + r, cy + r), fill=255 - a)
        vig = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        vig.putalpha(ImageChops.invert(mask))
        _VIGNETTE = vig
    return _VIGNETTE


def grain_frames(n: int = 10, strength: int = 7) -> list[Image.Image]:
    global _GRAIN
    if _GRAIN:
        return _GRAIN
    rnd = random.Random(0x12CE)
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
# The icon — the anchor — pulse-capable
# ============================================================
N_RINGS = 9
N_VERTS = 280

_WOBBLE_RNG = random.Random(0xF1F1)
WOBBLE_HARMONICS = [(2, 0.45), (3, 0.65), (5, 0.85), (7, 1.15)]
_WOBBLE_PHASES = [
    [_WOBBLE_RNG.uniform(0, 2 * math.pi) for _ in WOBBLE_HARMONICS]
    for _ in range(N_RINGS)
]


def ring_wobble_amp(ring_idx: int) -> float:
    t = ring_idx / max(1, N_RINGS - 1)
    return (1.0 - t) ** 2.4


def ring_base_radius(ring_idx: int, max_r: float) -> float:
    t = ring_idx / (N_RINGS - 0.4)
    return max_r * (0.18 + 0.82 * (1.0 - t))


def ring_offset(ring_idx: int, max_r: float) -> tuple[float, float]:
    t = ring_idx / max(1, N_RINGS - 1)
    return (-max_r * 0.03 * t, -max_r * 0.05 * t)


def base_shape(theta: float) -> float:
    r = 1.0
    r += 0.085 * math.sin(2 * theta + 0.6)
    r += 0.052 * math.sin(3 * theta + 1.5)
    r += 0.030 * math.sin(5 * theta + 0.9)
    r += 0.018 * math.cos(7 * theta + 2.1)
    r += 0.045 * math.cos(theta - 2.5)
    return r


def ring_wobble(theta: float, ring_idx: int, t: float) -> float:
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
    pulse_boost: float = 0.0,
) -> Image.Image:
    """Render the icon with per-ring wobble. `pulse_boost` adds a
    momentary expansion (used when the system 'sends a wave')."""
    layer = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    for i in range(N_RINGS):
        base_r = ring_base_radius(i, max_radius) * (1.0 + 0.08 * pulse_boost * (1.0 - i / N_RINGS))
        amp = ring_wobble_amp(i) * 0.20
        off_x, off_y = ring_offset(i, max_radius)
        mix = i / max(1, N_RINGS - 1)
        color = lerp_color(CONTOUR_OUTER, CONTOUR_INNER, mix)
        # Pulse brightens outer rings briefly
        if pulse_boost > 0 and i < 3:
            brighten = int(35 * pulse_boost * (1.0 - i / 3))
            color = tuple(min(255, c + brighten) for c in color)
        color_rgba = (*color, alpha)
        points = []
        for k in range(N_VERTS):
            theta = (k / N_VERTS) * 2 * math.pi
            r = base_r * (base_shape(theta) + amp * ring_wobble(theta, i, t))
            x = cx + off_x + r * math.cos(theta)
            y = cy + off_y + r * math.sin(theta)
            points.append((x, y))
        line_w = max(2, 5 - i // 3)
        d.line(points + [points[0]], fill=color_rgba, width=line_w, joint="curve")
    return Image.alpha_composite(canvas, layer)


def draw_icon_aura(
    canvas: Image.Image,
    cx: int,
    cy: int,
    radius: float,
    intensity: float = 1.0,
):
    """A soft pulsing halo behind the icon — the system's heartbeat
    visualized as light bleeding outward."""
    if intensity <= 0:
        return canvas
    layer = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    for step in range(15, 0, -1):
        r = radius + step * 30
        a = int(8 * intensity * (step / 15) ** 1.4)
        d.ellipse((cx - r, cy - r, cx + r, cy + r), fill=(*ACCENT, a))
    return Image.alpha_composite(canvas, layer)


# ============================================================
# Particle system
# ============================================================
class Particle:
    __slots__ = (
        "idx", "x", "y", "vx", "vy", "history",
        "base_color", "size", "alpha",
        "mode_state",  # current state label, used to color in mode chrome
    )

    def __init__(self, idx: int, rnd: random.Random):
        self.idx = idx
        # Initial position scattered around center
        angle = rnd.uniform(0, 2 * math.pi)
        radius = rnd.uniform(80, 250)
        self.x = W // 2 + radius * math.cos(angle)
        self.y = H // 2 + radius * math.sin(angle)
        self.vx = 0.0
        self.vy = 0.0
        self.history: list[tuple[float, float]] = []
        self.size = rnd.uniform(1.4, 3.0)
        # Color: most are accent-blue, some are warmer/cooler for variety
        pick = rnd.random()
        if pick < 0.70:
            self.base_color = ACCENT
        elif pick < 0.85:
            self.base_color = ACCENT_WARM
        elif pick < 0.95:
            self.base_color = TEAL
        else:
            self.base_color = VIOLET
        self.alpha = 220
        self.mode_state = "default"


def make_swarm() -> list[Particle]:
    rnd = random.Random(0xDEADBEEF)
    return [Particle(i, rnd) for i in range(N_PARTICLES)]


SWARM = make_swarm()


def step_particle(p: Particle, tx: float, ty: float, damping: float = 0.82, k: float = 0.06):
    """Spring physics: accelerate toward (tx, ty), damped velocity."""
    ax = (tx - p.x) * k
    ay = (ty - p.y) * k
    p.vx = p.vx * damping + ax
    p.vy = p.vy * damping + ay
    p.x += p.vx
    p.y += p.vy
    p.history.append((p.x, p.y))
    if len(p.history) > 7:
        p.history.pop(0)


# ============================================================
# Mode target functions
# ============================================================
ICON_CX, ICON_CY = W // 2, int(H * 0.50)


def task_target(p: Particle, t: float) -> tuple[float, float]:
    """24 cells in a 6x4 grid around the icon. Particles swirl inside
    their cell, never crossing walls."""
    cells_cols, cells_rows = 6, 4
    cell_w = 220
    cell_h = 150
    gutter_x = 24
    gutter_y = 20
    grid_w = cells_cols * cell_w + (cells_cols - 1) * gutter_x
    grid_h = cells_rows * cell_h + (cells_rows - 1) * gutter_y
    grid_x0 = (W - grid_w) // 2
    grid_y0 = (H - grid_h) // 2 + 30

    cell_idx = p.idx % (cells_cols * cells_rows)
    cell_col = cell_idx % cells_cols
    cell_row = cell_idx // cells_cols
    cell_cx = grid_x0 + cell_col * (cell_w + gutter_x) + cell_w // 2
    cell_cy = grid_y0 + cell_row * (cell_h + gutter_y) + cell_h // 2

    # Per-particle orbit inside its cell
    orbit_radius = (cell_w * 0.32) * (0.6 + 0.4 * ((p.idx * 7) % 5) / 5)
    orbit_speed = 0.4 + ((p.idx * 11) % 10) / 25
    angle = (p.idx * 0.37) + t * orbit_speed
    return (cell_cx + orbit_radius * math.cos(angle),
            cell_cy + orbit_radius * math.sin(angle) * 0.55)


def kanban_target(p: Particle, t: float) -> tuple[float, float]:
    """5 horizontal lanes. Particles flow left to right through
    stations, cycling colors as they cross."""
    lanes = 5
    lane_idx = p.idx % lanes
    lane_y_start = 200
    lane_spacing = 150
    lane_y = lane_y_start + lane_idx * lane_spacing
    # X cycles with phase
    phase = (p.idx * 0.0731) % 1.0
    speed = 0.10 + lane_idx * 0.012
    x_progress = (t * speed + phase) % 1.0
    x = 100 + x_progress * (W - 200)
    # Slight vertical jitter
    jitter = 8 * math.sin(t * 2.2 + p.idx * 0.4)
    return (x, lane_y + jitter)


def kanban_state_for(p: Particle, t: float) -> str:
    """Which station is this card currently in? Determines color."""
    phase = (p.idx * 0.0731) % 1.0
    lane_idx = p.idx % 5
    speed = 0.10 + lane_idx * 0.012
    x_progress = (t * speed + phase) % 1.0
    # 4 stations
    if x_progress < 0.25:
        return "triage"
    elif x_progress < 0.50:
        return "ready"
    elif x_progress < 0.75:
        return "running"
    else:
        return "done"


def goal_target(p: Particle, t: float) -> tuple[float, float]:
    """Particles arc up to a target above the icon, then drop back
    down on a verdict trajectory."""
    target_x = W // 2
    target_y = int(H * 0.22)
    # Each particle on its own cycle, all converging
    cycle = 2.6
    phase = (p.idx * 0.137) % cycle
    local = (t + phase) % cycle
    start_x = 100 + (p.idx * 73) % (W - 200)
    start_y = H - 120
    if local < 1.2:
        # Ascending
        arc_t = local / 1.2
        x = lerp(start_x, target_x, arc_t)
        y_base = lerp(start_y, target_y, arc_t)
        y = y_base - 280 * math.sin(math.pi * arc_t)
    elif local < 1.5:
        # At target (judging)
        x = target_x + 20 * math.sin(p.idx + t * 6)
        y = target_y + 20 * math.cos(p.idx + t * 6)
    else:
        # Verdict drop
        verdict_t = (local - 1.5) / 1.1
        x = lerp(target_x, start_x, verdict_t)
        y = lerp(target_y, start_y, verdict_t) + 40 * math.sin(math.pi * verdict_t)
    return (x, y)


# Stable node positions for bus mode
_BUS_RNG = random.Random(0x101)
_BUS_NODES_BASE = []
for i in range(N_PARTICLES):
    a = _BUS_RNG.uniform(0, 2 * math.pi)
    r = _BUS_RNG.uniform(180, 460)
    # Bias toward upper portion so it doesn't overlap title area too much
    _BUS_NODES_BASE.append((a, r, _BUS_RNG.uniform(0, 2 * math.pi)))


def bus_target(p: Particle, t: float) -> tuple[float, float]:
    """Particles settle into stable network positions with subtle drift."""
    a, r, phase = _BUS_NODES_BASE[p.idx]
    rotate = t * 0.08
    breath = 1.0 + 0.04 * math.sin(2 * math.pi * (t * 0.2 + phase / 8))
    x = ICON_CX + r * breath * math.cos(a + rotate)
    y = ICON_CY + r * breath * math.sin(a + rotate)
    return (x, y)


def composite_target(p: Particle, t: float) -> tuple[float, float]:
    """In the grand section, each particle has its own assigned mode.
    All four modes show at the same time."""
    group = p.idx % 4
    if group == 0:
        return task_target(p, t)
    elif group == 1:
        return kanban_target(p, t)
    elif group == 2:
        return goal_target(p, t)
    else:
        return bus_target(p, t)


def converge_target(p: Particle, t: float) -> tuple[float, float]:
    """Final: spiral inward to form a halo around the icon."""
    # Each particle settles to a point on a circle of radius 320 around icon
    a = (p.idx / N_PARTICLES) * 2 * math.pi + t * 0.15
    r = 280 + 30 * math.sin(p.idx * 0.3 + t * 0.5)
    return (ICON_CX + r * math.cos(a), ICON_CY + r * math.sin(a))


# ============================================================
# Mode chrome — auxiliary visuals per mode
# ============================================================
def draw_task_chrome(d: ImageDraw.ImageDraw, t: float, alpha: float):
    cells_cols, cells_rows = 6, 4
    cell_w = 220
    cell_h = 150
    gutter_x = 24
    gutter_y = 20
    grid_w = cells_cols * cell_w + (cells_cols - 1) * gutter_x
    grid_h = cells_rows * cell_h + (cells_rows - 1) * gutter_y
    grid_x0 = (W - grid_w) // 2
    grid_y0 = (H - grid_h) // 2 + 30
    wall_alpha = int(alpha * 50)
    for r in range(cells_rows):
        for c in range(cells_cols):
            x0 = grid_x0 + c * (cell_w + gutter_x)
            y0 = grid_y0 + r * (cell_h + gutter_y)
            d.rectangle((x0, y0, x0 + cell_w, y0 + cell_h),
                       outline=(255, 255, 255, wall_alpha), width=1)


def draw_kanban_chrome(d: ImageDraw.ImageDraw, t: float, alpha: float):
    lanes = 5
    lane_y_start = 200
    lane_spacing = 150
    lane_x0 = 100
    lane_x1 = W - 100
    line_alpha = int(alpha * 38)
    for lane_idx in range(lanes):
        ly = lane_y_start + lane_idx * lane_spacing
        # Lane backbone
        d.line([(lane_x0, ly), (lane_x1, ly)], fill=(255, 255, 255, line_alpha), width=1)
        # Station dividers (4 stations)
        for k in range(1, 4):
            sx = lane_x0 + (lane_x1 - lane_x0) * k // 4
            d.line([(sx, ly - 40), (sx, ly + 40)], fill=(255, 255, 255, line_alpha // 2), width=1)
    # Station labels at top
    station_labels = ["triage", "ready", "running", "done"]
    lf = font(DEPARTURE, 13)
    for k, label in enumerate(station_labels):
        sx = lane_x0 + (lane_x1 - lane_x0) * (k + 1) // 4 - (lane_x1 - lane_x0) // 8
        ly = lane_y_start - 50
        d.text((sx, ly), label, font=lf, fill=(*FG_3, int(alpha * 220)))


def draw_goal_chrome(d: ImageDraw.ImageDraw, t: float, alpha: float):
    """Target / bullseye + accent ring at the top."""
    target_x = W // 2
    target_y = int(H * 0.22)
    pulse = 0.85 + 0.15 * math.sin(2 * math.pi * t / 1.4)
    for i in range(5):
        rr = int(130 * (1 - i * 0.16))
        a = int(60 * alpha + 50 * (1 - i * 0.2)) * (1 if pulse > 0.5 else 1)
        a = int(a * pulse)
        d.ellipse((target_x - rr, target_y - rr, target_x + rr, target_y + rr),
                  outline=(*ACCENT, a), width=2)
    # Hot center
    hot_r = int(28 * pulse)
    d.ellipse((target_x - hot_r, target_y - hot_r, target_x + hot_r, target_y + hot_r),
              fill=(*ACCENT, int(200 * pulse * alpha)))


def draw_bus_edges(d: ImageDraw.ImageDraw, particles: list[Particle], alpha: float, max_dist: float = 220):
    """Proximity-based edges. Only draw edges shorter than max_dist."""
    # Spatial hash for performance
    cell_size = int(max_dist)
    grid: dict[tuple[int, int], list[int]] = {}
    for i, p in enumerate(particles):
        gx = int(p.x // cell_size)
        gy = int(p.y // cell_size)
        grid.setdefault((gx, gy), []).append(i)
    seen = set()
    edges_drawn = 0
    for i, p in enumerate(particles):
        gx = int(p.x // cell_size)
        gy = int(p.y // cell_size)
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                neighbors = grid.get((gx + dx, gy + dy), [])
                for j in neighbors:
                    if j <= i:
                        continue
                    key = (i, j)
                    if key in seen:
                        continue
                    seen.add(key)
                    q = particles[j]
                    dist = math.hypot(p.x - q.x, p.y - q.y)
                    if dist > max_dist:
                        continue
                    a = int(alpha * 80 * (1 - dist / max_dist))
                    if a <= 0:
                        continue
                    d.line([(p.x, p.y), (q.x, q.y)],
                           fill=(*ACCENT, a), width=1)
                    edges_drawn += 1


# ============================================================
# Timeline + mode mixer
# ============================================================
def current_mode_weights(t: float) -> dict[str, float]:
    """Returns blend weights for which mode chrome to draw + which
    target each particle should pursue. Particles use a 'dominant
    mode' at any moment (no target blending) but chrome blends."""
    # Defines beat structure:
    #   0.0–3.5   emerge — particles from center radial scatter
    #   3.5–9.0   task
    #   9.0–11.0  task→kanban transition (chrome blend)
    #  11.0–17.0  kanban
    #  17.0–19.0  kanban→goal
    #  19.0–25.0  goal
    #  25.0–27.0  goal→bus
    #  27.0–33.0  bus
    #  33.0–35.0  bus→composite
    #  35.0–42.0  composite (all four)
    #  42.0–46.0  converge (halo around icon)
    #  46.0–50.0  resolve (wordmark + icon hold)
    weights = {"emerge": 0, "task": 0, "kanban": 0, "goal": 0,
               "bus": 0, "composite": 0, "converge": 0, "resolve": 0}
    if t < 3.5:
        weights["emerge"] = 1.0
    elif t < 9.0:
        weights["task"] = 1.0
    elif t < 11.0:
        blend = (t - 9.0) / 2.0
        weights["task"] = 1.0 - blend
        weights["kanban"] = blend
    elif t < 17.0:
        weights["kanban"] = 1.0
    elif t < 19.0:
        blend = (t - 17.0) / 2.0
        weights["kanban"] = 1.0 - blend
        weights["goal"] = blend
    elif t < 25.0:
        weights["goal"] = 1.0
    elif t < 27.0:
        blend = (t - 25.0) / 2.0
        weights["goal"] = 1.0 - blend
        weights["bus"] = blend
    elif t < 33.0:
        weights["bus"] = 1.0
    elif t < 35.0:
        blend = (t - 33.0) / 2.0
        weights["bus"] = 1.0 - blend
        weights["composite"] = blend
    elif t < 42.0:
        weights["composite"] = 1.0
    elif t < 46.0:
        blend = (t - 42.0) / 4.0
        weights["composite"] = 1.0 - blend
        weights["converge"] = blend
    else:
        weights["converge"] = max(0.0, 1.0 - (t - 46.0) / 2.0)
        weights["resolve"] = 1.0
    return weights


def primary_mode(t: float) -> str:
    """Which mode are the particles physically pursuing right now?"""
    if t < 3.5:
        return "emerge"
    if t < 11.0:
        return "task"
    if t < 19.0:
        return "kanban"
    if t < 27.0:
        return "goal"
    if t < 35.0:
        return "bus"
    if t < 42.0:
        return "composite"
    return "converge"


def emerge_target(p: Particle, t: float) -> tuple[float, float]:
    """During the emerge phase, particles drift outward in a slow
    spiral from the icon."""
    a = (p.idx / N_PARTICLES) * 2 * math.pi + t * 0.1
    r = 60 + t * 50
    return (ICON_CX + r * math.cos(a), ICON_CY + r * math.sin(a))


def get_target(p: Particle, t: float) -> tuple[float, float]:
    mode = primary_mode(t)
    if mode == "emerge":
        return emerge_target(p, t)
    if mode == "task":
        return task_target(p, t)
    if mode == "kanban":
        return kanban_target(p, t)
    if mode == "goal":
        return goal_target(p, t)
    if mode == "bus":
        return bus_target(p, t)
    if mode == "composite":
        return composite_target(p, t)
    return converge_target(p, t)


# ============================================================
# Pulse beats — moments when the icon sends a wave outward
# ============================================================
PULSE_BEATS = [3.5, 9.0, 17.0, 25.0, 33.0, 42.0]


def pulse_intensity(t: float) -> float:
    """Returns 0-1 intensity for an icon pulse near a beat."""
    best = 0.0
    for beat in PULSE_BEATS:
        dt = t - beat
        if -0.15 < dt < 0.9:
            # Quick rise, slow decay
            if dt < 0:
                phase = (dt + 0.15) / 0.15
                v = phase
            else:
                phase = dt / 0.9
                v = 1.0 - ease_in_cubic(phase)
            best = max(best, v)
    return best


# Pulse waves: when a beat fires, send a ring expanding outward
class PulseWave:
    __slots__ = ("start_t", "intensity", "color")

    def __init__(self, start_t, intensity, color):
        self.start_t = start_t
        self.intensity = intensity
        self.color = color


PULSE_WAVES = [PulseWave(beat, 1.0, ACCENT) for beat in PULSE_BEATS]


def draw_pulse_waves(d: ImageDraw.ImageDraw, t: float):
    for pw in PULSE_WAVES:
        dt = t - pw.start_t
        if dt < 0 or dt > 2.0:
            continue
        # Expanding ring
        r = 60 + dt * 800
        a = int(160 * pw.intensity * (1 - dt / 2.0))
        if a <= 0:
            continue
        d.ellipse(
            (ICON_CX - r, ICON_CY - r, ICON_CX + r, ICON_CY + r),
            outline=(*pw.color, a), width=2,
        )


# ============================================================
# Particle rendering with trails + per-mode coloring
# ============================================================
KANBAN_STATE_COLORS = {
    "triage": (158, 168, 158),
    "ready": (216, 219, 200),
    "running": ACCENT,
    "done": OK,
}


def particle_color(p: Particle, t: float) -> tuple:
    mode = primary_mode(t)
    if mode == "kanban" or (mode == "composite" and p.idx % 4 == 1):
        return KANBAN_STATE_COLORS[kanban_state_for(p, t)]
    return p.base_color


def render_particles(canvas: Image.Image, t: float):
    """Render all particles with trails and connections."""
    layer = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)

    mode = primary_mode(t)
    show_edges = mode in ("bus", "composite")

    if show_edges:
        # Only draw edges between particles that are in 'bus mode'
        if mode == "bus":
            draw_bus_edges(d, SWARM, alpha=1.0, max_dist=200)
        elif mode == "composite":
            # Only the bus-assigned subset
            bus_subset = [p for p in SWARM if p.idx % 4 == 3]
            draw_bus_edges(d, bus_subset, alpha=0.9, max_dist=180)

    # Trails first
    for p in SWARM:
        if len(p.history) < 2:
            continue
        col = particle_color(p, t)
        for k in range(len(p.history) - 1):
            x1, y1 = p.history[k]
            x2, y2 = p.history[k + 1]
            a = int(p.alpha * 0.30 * (k / len(p.history)))
            d.line([(x1, y1), (x2, y2)], fill=(*col, a), width=1)

    # Particles
    for p in SWARM:
        col = particle_color(p, t)
        sz = p.size
        # Slight size pulse synced to mode beats
        beat_intensity = pulse_intensity(t)
        if beat_intensity > 0:
            sz = sz * (1.0 + 0.6 * beat_intensity)
        # Halo
        halo_r = sz * 2.5
        d.ellipse(
            (p.x - halo_r, p.y - halo_r, p.x + halo_r, p.y + halo_r),
            fill=(*col, int(p.alpha * 0.18)),
        )
        # Core
        d.ellipse(
            (p.x - sz, p.y - sz, p.x + sz, p.y + sz),
            fill=(*col, p.alpha),
        )
    return Image.alpha_composite(canvas, layer)


# ============================================================
# Master frame render
# ============================================================
def render_frame(frame_idx: int) -> Image.Image:
    t = frame_idx / FPS
    img = base_bg()

    weights = current_mode_weights(t)
    mode = primary_mode(t)

    # 1. Pre-particle background chrome (mode-specific, faint)
    chrome_layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
    cd = ImageDraw.Draw(chrome_layer)
    if weights.get("task", 0) > 0:
        draw_task_chrome(cd, t, weights["task"])
    if weights.get("kanban", 0) > 0:
        draw_kanban_chrome(cd, t, weights["kanban"])
    if weights.get("goal", 0) > 0:
        draw_goal_chrome(cd, t, weights["goal"])
    if weights.get("composite", 0) > 0:
        # In composite, render all four softly
        draw_task_chrome(cd, t, weights["composite"] * 0.5)
        draw_kanban_chrome(cd, t, weights["composite"] * 0.5)
        draw_goal_chrome(cd, t, weights["composite"] * 0.5)
    img = Image.alpha_composite(img, chrome_layer)

    # 2. Icon aura — pulses brighter on beats
    aura_intensity = 0.4 + 0.6 * pulse_intensity(t)
    img = draw_icon_aura(img, ICON_CX, ICON_CY, 220, intensity=aura_intensity)

    # 3. Pulse waves expanding from icon
    waves_layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
    wd = ImageDraw.Draw(waves_layer)
    draw_pulse_waves(wd, t)
    img = Image.alpha_composite(img, waves_layer)

    # 4. Step particles toward their targets
    for p in SWARM:
        tx, ty = get_target(p, t)
        # Damping varies by mode — task is tighter, bus is loose
        if mode == "task":
            step_particle(p, tx, ty, damping=0.78, k=0.10)
        elif mode == "kanban":
            step_particle(p, tx, ty, damping=0.85, k=0.12)
        elif mode == "goal":
            step_particle(p, tx, ty, damping=0.86, k=0.14)
        elif mode == "bus":
            step_particle(p, tx, ty, damping=0.84, k=0.06)
        elif mode == "composite":
            step_particle(p, tx, ty, damping=0.83, k=0.08)
        elif mode == "converge":
            step_particle(p, tx, ty, damping=0.80, k=0.10)
        else:  # emerge
            step_particle(p, tx, ty, damping=0.86, k=0.05)

    # 5. Render particles with trails
    img = render_particles(img, t)

    # 6. The icon itself — drawn on top so it always reads
    pulse_b = pulse_intensity(t)
    icon_alpha = 255
    if t < 1.5:
        # Initial draw-in
        icon_alpha = int(255 * ease_out_cubic(t / 1.5))
    elif t > 48.0:
        icon_alpha = int(255 * (1 - ease_in_cubic((t - 48.0) / 2.0)))
    if icon_alpha > 0:
        # Icon size grows during converge/resolve
        max_r = 260
        if t > 42.0:
            grow_t = ease_out_cubic(min(1.0, (t - 42.0) / 4.0))
            max_r = int(260 * (1.0 + 0.15 * grow_t))
        img = draw_living_icon(img, ICON_CX, ICON_CY, max_r, t, alpha=icon_alpha, pulse_boost=pulse_b)

    # 7. Mode label — a single line at the top during each mode
    img_d = ImageDraw.Draw(img)
    mode_label = {
        "task": ("task · isolated", "each agent sealed · no shared channel"),
        "kanban": ("kanban · flow", "the board runs the work"),
        "goal": ("goal · the loop", "judge · iterate · judge again"),
        "bus": ("bus · the shared frequency", "many minds broadcasting"),
        "composite": ("freyja · the choice is yours", "four ways many minds can move together"),
        "converge": (None, None),
        "resolve": (None, None),
        "emerge": (None, None),
    }
    label, sub = mode_label.get(mode, (None, None))
    if label is not None:
        # Fade in/out as mode begins/ends within its window
        weight = weights.get(mode, 0.0)
        if weight > 0:
            display_a = int(220 * weight)
            df = font(DEPARTURE, 22)
            lw = text_w(label, df)
            img_d.text((W // 2 - lw // 2, 60), label, font=df, fill=(*FG_1, display_a))
            sf = font(JB_LIGHT_ITALIC, 16)
            sw = text_w(sub, sf)
            img_d.text((W // 2 - sw // 2, 96), sub, font=sf, fill=(*FG_3, int(160 * weight)))

    # 8. Final wordmark during resolve
    if t > 44.0:
        wm_t = ease_out_cubic(clamp((t - 44.0) / 1.5))
        if t > 48.5:
            wm_t = wm_t * (1.0 - ease_in_cubic((t - 48.5) / 1.5))
        wf = font(DEPARTURE, 88)
        word = "freyja"
        ww = text_w(word, wf)
        wy = H - 220
        img_d.text((W // 2 - ww // 2, wy), word, font=wf, fill=(*FG_0, int(255 * wm_t)))
        if t > 45.0:
            st = ease_out_cubic(clamp((t - 45.0) / 1.2))
            if t > 48.5:
                st = st * (1.0 - ease_in_cubic((t - 48.5) / 1.5))
            sf = font(DEPARTURE, 18)
            sub = "the page writes itself."
            sw = text_w(sub, sf)
            img_d.text((W // 2 - sw // 2, wy + 100), sub, font=sf, fill=(*FG_2, int(180 * st)))

    # 9. Grain + vignette
    grains = grain_frames()
    img = Image.alpha_composite(img, grains[frame_idx % len(grains)])
    img = Image.alpha_composite(img, vignette())
    return img.convert("RGB")


# ============================================================
# Audio — a single continuous arc
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

    # ── Foundational drone — continuous A minor pad with slow swells ──
    # Envelope follows the arc: rises during composite, resolves at end.
    for i in range(total):
        t = i / SAMPLE_RATE
        # Master envelope: shaped by section
        if t < 1.0:
            env = ease_out_cubic(t) * 0.40
        elif t < 9.0:
            env = 0.45
        elif t < 33.0:
            env = 0.55 + 0.08 * math.sin(2 * math.pi * t / 8.0)
        elif t < 42.0:
            env = 0.75
        elif t < 46.0:
            env = 0.85
        elif t < 48.5:
            env = 0.75
        else:
            env = 0.75 * (1.0 - (t - 48.5) / 1.5)
        env = clamp(env, 0.0, 1.0)
        # Tonal shift over time (very subtle) — slight detune wandering
        lfo = 0.85 + 0.15 * math.sin(2 * math.pi * t / 7.0)
        v = 0.0
        v += 2400 * env * lfo * math.sin(2 * math.pi * 110.0 * t)
        v += 1500 * env * lfo * math.sin(2 * math.pi * 164.8 * t)
        v += 1100 * env * lfo * math.sin(2 * math.pi * 220.0 * t)
        v += 700 * env * lfo * math.sin(2 * math.pi * 277.2 * t)
        # Above 33s, add a higher voice for ascension
        if t > 33.0:
            ascend_env = ease_out_cubic(min(1.0, (t - 33.0) / 5.0))
            v += 900 * env * ascend_env * math.sin(2 * math.pi * 440.0 * t)
            v += 600 * env * ascend_env * math.sin(2 * math.pi * 659.25 * t)
        mix(i, v)

    rnd = random.Random(0xBEAD)

    def click(t_sec, gain=1.0, decay=70, length=600, freq=None):
        center = int(t_sec * SAMPLE_RATE)
        for k in range(length):
            env = math.exp(-k / decay)
            if freq:
                v = math.sin(2 * math.pi * freq * (k / SAMPLE_RATE)) * 4000 * env * gain
            else:
                v = rnd.uniform(-1.0, 1.0) * 4000 * env * gain
            mix(center + k, v)

    def chime(t_sec, freq, gain=1.0, decay=2.0, length_s=2.0):
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

    def thump(t_sec, freq=58.0, gain=1.0, length_s=0.5):
        center = int(t_sec * SAMPLE_RATE)
        length = int(SAMPLE_RATE * length_s)
        for k in range(length):
            tl = k / SAMPLE_RATE
            env = math.exp(-tl * 4.5)
            v = math.sin(2 * math.pi * freq * tl) * 8500 * env * gain
            mix(center + k, v)

    # ── A persistent heartbeat — drives the piece, intensifies with energy ──
    # ~75 bpm with double-tap (lub-dub)
    bpm = 75
    interval = 60.0 / bpm
    beat_t = 1.0
    while beat_t < 48.0:
        # Intensity follows mode arc — bigger thumps during composite
        if beat_t < 9.0:
            gain = 0.35
        elif beat_t < 27.0:
            gain = 0.45
        elif beat_t < 35.0:
            gain = 0.55
        elif beat_t < 42.0:
            gain = 0.70
        else:
            gain = 0.55
        thump(beat_t, freq=58.0, gain=gain, length_s=0.45)
        # Lub-dub: a soft second beat 180ms later
        thump(beat_t + 0.18, freq=58.0, gain=gain * 0.5, length_s=0.35)
        beat_t += interval

    # ── Pulse-beat events — major rings synced with icon waves ──
    pulse_chimes = [
        (3.5, 440.0, 0.7),
        (9.0, 523.25, 0.7),
        (17.0, 587.33, 0.7),
        (25.0, 659.25, 0.7),
        (33.0, 783.99, 0.9),
        (42.0, 880.0, 1.1),
    ]
    for t_sec, freq, gain in pulse_chimes:
        thump(t_sec - 0.05, freq=33.0, gain=gain * 1.4, length_s=1.0)
        chime(t_sec, freq, gain=gain, decay=1.5, length_s=2.5)
        chime(t_sec, freq * 1.5, gain=gain * 0.4, decay=1.5, length_s=2.5)

    # ── Texture per mode — adds character without breaking continuity ──
    # Task (3.5-11): scattered light clicks
    t0 = 4.0
    while t0 < 11.0:
        click(t0, gain=0.20, decay=40, length=200)
        t0 += rnd.uniform(0.18, 0.32)

    # Kanban (11-17): regular ticks at 1/16-note feel
    t0 = 11.5
    while t0 < 17.0:
        click(t0, gain=0.25, decay=35, length=150)
        t0 += 0.30

    # Goal (19-25): rising sweeps every cycle
    for sweep_t in [19.2, 21.6, 24.0]:
        for k in range(int(SAMPLE_RATE * 1.0)):
            tl = k / SAMPLE_RATE
            env = math.sin(math.pi * tl) * 1.0
            f0 = lerp(220, 660, tl)
            v = 1200 * env * math.sin(2 * math.pi * f0 * tl)
            mix(int((sweep_t + tl) * SAMPLE_RATE), v)

    # Bus (27-33): sparse high bell flashes
    t0 = 27.5
    while t0 < 33.0:
        f = rnd.choice([880, 1046, 1318, 1568])
        chime(t0, f, gain=0.30, decay=2.5, length_s=1.5)
        t0 += rnd.uniform(0.25, 0.55)

    # Composite (35-42): all four motifs overlap softly
    t0 = 35.5
    while t0 < 42.0:
        if rnd.random() < 0.5:
            click(t0, gain=0.15, decay=35, length=150)
        if rnd.random() < 0.4:
            chime(t0, rnd.choice([523, 659, 784, 880]), gain=0.20, decay=2.5, length_s=1.2)
        t0 += 0.18

    # ── Final swell — big chord under resolve ──
    chime(43.5, 523.25, gain=0.7, decay=0.8, length_s=4.0)
    chime(43.5, 659.25, gain=0.5, decay=0.8, length_s=4.0)
    chime(43.5, 783.99, gain=0.4, decay=0.8, length_s=4.0)
    chime(46.0, 261.63, gain=0.9, decay=0.6, length_s=3.5)
    chime(46.0, 523.25, gain=0.4, decay=0.6, length_s=3.5)
    chime(48.5, 220.0, gain=0.6, decay=1.0, length_s=2.0)

    with wave.open(str(out_path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(SAMPLE_RATE)
        wf.writeframes(struct.pack("<" + "h" * len(buf), *(int(v) for v in buf)))


def kanban_state_for(p: Particle, t: float) -> str:
    return kanban_state_for_pid(p.idx, t)


def kanban_state_for_pid(pid: int, t: float) -> str:
    phase = (pid * 0.0731) % 1.0
    lane_idx = pid % 5
    speed = 0.10 + lane_idx * 0.012
    x_progress = (t * speed + phase) % 1.0
    if x_progress < 0.25:
        return "triage"
    elif x_progress < 0.50:
        return "ready"
    elif x_progress < 0.75:
        return "running"
    else:
        return "done"


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

    # Particle state is global; if we're rendering a partial range
    # for a smoke test, we'd want to fast-forward through the prior
    # frames to put particles in the right place. For full renders
    # this isn't needed.
    end_frame = args.end_frame if args.end_frame is not None else TOTAL_FRAMES
    if args.start_frame > 0:
        print(f"  fast-forwarding particles 0..{args.start_frame - 1}")
        for i in range(args.start_frame):
            t = i / FPS
            for p in SWARM:
                tx, ty = get_target(p, t)
                step_particle(p, tx, ty)

    print(f"freyja living · {W}x{H} · {FPS} fps · {DURATION:.0f}s · {N_PARTICLES} particles")
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
