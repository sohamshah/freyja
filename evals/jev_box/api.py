"""Minimal client for System One image + Box requests, independent of the bridge.

Everything here follows https://docs.typesafe.ai/images and /primitives/box:
images travel in a top-level `assets` map and are referenced by opaque strings
anywhere text can go; Box answers are fractional edges of the image.
"""

from __future__ import annotations

import base64
import io
import os
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

import httpx
from PIL import Image

API_URL = "https://api.typesafe.ai/v1/systemone"
MAX_SIDE = 1568
MIN_SIDE = 280


def api_key() -> str:
    key = os.environ.get("TYPESAFE_AI_API_KEY") or os.environ.get("TYPESAFE_API_KEY")
    if key:
        return key
    env = Path(__file__).resolve().parents[2] / ".env"
    if env.exists():
        for line in env.read_text().splitlines():
            if line.startswith("TYPESAFE_AI_API_KEY=") or line.startswith("TYPESAFE_API_KEY="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    raise RuntimeError("TYPESAFE_AI_API_KEY not set")


@dataclass
class Prepared:
    png: bytes
    width: int
    height: int
    scale: float  # prepared px per source px


def prepare(img: Image.Image, max_side: int = MAX_SIDE) -> Prepared:
    """Fit the image inside the API's size window, keeping aspect ratio."""
    w, h = img.size
    scale = 1.0
    if max(w, h) > max_side:
        scale = max_side / max(w, h)
    if min(w, h) * scale < MIN_SIDE:
        scale = MIN_SIDE / min(w, h)
    if scale != 1.0:
        img = img.resize((max(1, round(w * scale)), max(1, round(h * scale))), Image.LANCZOS)
    buf = io.BytesIO()
    img.convert("RGB").save(buf, "PNG", optimize=False)
    return Prepared(buf.getvalue(), img.width, img.height, scale)


def asset(png: bytes) -> tuple[str, dict]:
    return str(uuid.uuid4()), {"type": "image", "format": "png", "data_base64": base64.b64encode(png).decode("ascii")}


@dataclass
class Result:
    answers: dict
    usage: dict
    latency_ms: float
    model: str
    raw: dict


_client: httpx.Client | None = None


def _http() -> httpx.Client:
    global _client
    if _client is None:
        _client = httpx.Client(timeout=httpx.Timeout(60.0, connect=10.0))
    return _client


def ask(
    state,
    questions: dict,
    assets: dict | None = None,
    model: str = "jev-1.14.0",
    retries: int = 3,
) -> Result:
    headers = {"Authorization": f"Bearer {api_key()}", "Content-Type": "application/json"}
    body: dict = {"model": model, "state": state, "questions": questions}
    if assets:
        body["assets"] = assets
        headers["TypeSafe-Danger-Zone-Images"] = "true"
    last: Exception | None = None
    for attempt in range(retries):
        t0 = time.perf_counter()
        try:
            r = _http().post(API_URL, headers=headers, json=body)
        except httpx.HTTPError as e:
            last = e
            time.sleep(1.5 * (attempt + 1))
            continue
        ms = (time.perf_counter() - t0) * 1000
        if r.status_code == 429 or r.status_code >= 500:
            last = RuntimeError(f"{r.status_code}: {r.text[:300]}")
            time.sleep(float(r.headers.get("retry-after", 1.5 * (attempt + 1))))
            continue
        if r.status_code != 200:
            raise RuntimeError(f"{r.status_code}: {r.text[:500]}")
        data = r.json()
        return Result(data.get("answers", {}), data.get("usage", {}), ms, data.get("model", model), data)
    raise RuntimeError(f"request failed after {retries} attempts: {last}")


def box_px(answer: dict, width: int, height: int) -> tuple[int, int, int, int]:
    b = answer["box"]
    return (round(b["left"] * width), round(b["top"] * height), round(b["right"] * width), round(b["bottom"] * height))


def box_center(answer: dict) -> tuple[float, float]:
    b = answer["box"]
    return ((b["left"] + b["right"]) / 2, (b["top"] + b["bottom"]) / 2)
