"""Find a described control in a window capture with a Jev Box question.

The planner names what to click ("the blue Save button"); Jev says where it
is. Grounding a description is a far easier vision task than planning from
pixels, and it keeps the planner's pixel coordinates, which it guesses, out
of the click. Measured on ScreenSpot-v2 (1,272 desktop, web and mobile
targets): 85.9% of click points land inside the target with one Box
question, 89.9% with the second look below, at about 0.3 s and $0.0002 per
lookup. See evals/jev_box/RESULTS.md.
"""

from __future__ import annotations

import asyncio
import io
import time
from dataclasses import dataclass
from typing import Any

from PIL import Image, ImageChops

from bridge.decisions.provider import DecisionProvider
from bridge.decisions.types import BOX_MODEL, Box, BoxAnswer, ImageAsset, Noul

# The service takes 280 to 1568 pixels per side.
MAX_SIDE = 1568
MIN_SIDE = 280
# A first box under this share of the image is a small target: asking again on
# a crop around it raised small-target accuracy from 61% to 80%, while asking
# again on large targets lowered theirs, so the second look is conditional.
SMALL_BOX = 0.005
CROP_FRAC = 0.4
# Below this P(visible) the box is not trusted: a Box answer is always a box,
# even for something that is not on the screen.
VISIBLE_MIN = 0.5
# Below this box confidence the click is left to the planner, which places
# its own on a fresh screenshot. On ScreenSpot-v2 the boxes at or above it
# are inside the target 92% of the time and cover 85% of targets; the ones
# below it are the look-alike rows and dense tables where Box is weakest.
CONFIDENCE_MIN = 0.5
# Window captures for grounding: the largest the service takes, lossless.
CAPTURE_MAX_DIM = MAX_SIDE


@dataclass(frozen=True)
class Located:
    x: float  # fractions of the capture, left to right and top to bottom
    y: float
    box: BoxAnswer
    visible: float
    confidence: float
    stages: int
    input_tokens: int
    latency_ms: int

    @property
    def seen(self) -> bool:
        return self.visible >= VISIBLE_MIN

    @property
    def found(self) -> bool:
        return self.seen and self.confidence >= CONFIDENCE_MIN


def _fit(img: Image.Image, max_side: int = MAX_SIDE) -> Image.Image:
    w, h = img.size
    scale = 1.0
    if max(w, h) > max_side:
        scale = max_side / max(w, h)
    if min(w, h) * scale < MIN_SIDE:
        scale = MIN_SIDE / min(w, h)
    if scale != 1.0:
        img = img.resize((max(1, round(w * scale)), max(1, round(h * scale))), Image.LANCZOS)
    return img


def _png(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.convert("RGB").save(buf, "PNG")
    return buf.getvalue()


def _crop_window(size: tuple[int, int], cx: float, cy: float) -> tuple[int, int, int, int]:
    W, H = size
    cw = min(W, max(MIN_SIDE, round(W * CROP_FRAC)))
    ch = min(H, max(MIN_SIDE, round(H * CROP_FRAC)))
    x0 = min(max(0, round(cx * W - cw / 2)), W - cw)
    y0 = min(max(0, round(cy * H - ch / 2)), H - ch)
    return (x0, y0, x0 + cw, y0 + ch)


async def locate(
    provider: DecisionProvider,
    image: bytes,
    description: str,
    *,
    model: str = BOX_MODEL,
    look_closer: bool = True,
) -> Located:
    """Where `description` is in `image` (PNG or JPEG bytes of one window).

    One request asks whether the target is visible at all and where it is.
    When the first box is small and `look_closer` is set, a second request
    asks again on a crop around it at the capture's own resolution."""
    t0 = time.perf_counter()
    src = Image.open(io.BytesIO(image))
    src.load()
    first = _fit(src)
    shot = ImageAsset(_png(first))
    what = description.strip().rstrip(".")
    answers = await provider.decide(
        {"screen": shot.ref},
        {
            "visible": Noul(f"Is {what} visible in `screen`?"),
            "target": Box(f"{what} in `screen`"),
        },
        model=model,
        assets=[shot],
    )
    visible = answers.noul("visible")
    box = answers.box("target")
    tokens = answers.input_tokens
    stages = 1
    if look_closer and box.area < SMALL_BOX and min(src.size) > MIN_SIDE:
        cx, cy = box.center
        x0, y0, x1, y1 = _crop_window(src.size, cx, cy)
        crop = ImageAsset(_png(_fit(src.crop((x0, y0, x1, y1)))))
        again = await provider.decide(
            {"screen": crop.ref},
            {"target": Box(f"{what} in `screen`")},
            model=model,
            assets=[crop],
        )
        b = again.box("target")
        W, H = src.size
        box = BoxAnswer(
            left=(x0 + b.left * (x1 - x0)) / W,
            top=(y0 + b.top * (y1 - y0)) / H,
            right=(x0 + b.right * (x1 - x0)) / W,
            bottom=(y0 + b.bottom * (y1 - y0)) / H,
            confidence=b.confidence,
        )
        tokens += again.input_tokens
        stages = 2
    x, y = box.center
    return Located(
        x=x,
        y=y,
        box=box,
        visible=visible,
        confidence=box.confidence,
        stages=stages,
        input_tokens=tokens,
        latency_ms=int((time.perf_counter() - t0) * 1000),
    )


async def capture(actuator: Any) -> bytes | None:
    """A lossless capture of the target window at the service's largest size,
    or None where the desktop is not reachable (the headless harness)."""
    window_id = getattr(actuator, "window_id", None)
    native = getattr(actuator, "native", None)
    if not window_id or native is None:
        return None
    try:
        frame = await asyncio.to_thread(
            native.screenshot, window_id=window_id, max_dim=CAPTURE_MAX_DIM, format="png"
        )
    except Exception:
        return None
    data = getattr(frame, "png", None)
    return data if isinstance(data, bytes) and data else None


THUMB = 512
PIXEL_DELTA = 24


def frame_change(before: bytes, after: bytes) -> float:
    """The share of the window that changed between two captures, on
    grayscale thumbnails so antialiasing does not count. A toggle flipping or
    a name typed into a field moves about 0.1% of a 1280x900 page; a caret
    0.003%; an unchanged page re-captured, 0."""
    a = Image.open(io.BytesIO(before)).convert("L")
    b = Image.open(io.BytesIO(after)).convert("L")
    size = (THUMB, max(1, round(THUMB * a.height / max(1, a.width))))
    a = a.resize(size, Image.BILINEAR)
    b = b.resize(size, Image.BILINEAR)
    diff = ImageChops.difference(a, b).point(lambda v: 255 if v > PIXEL_DELTA else 0)
    hist = diff.histogram()
    changed = hist[255] if len(hist) > 255 else 0
    return changed / float(size[0] * size[1])
