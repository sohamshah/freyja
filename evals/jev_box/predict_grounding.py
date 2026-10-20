"""Freyja's grounder over the evals platform's gui-grounding cases, as a
predictions file the platform grades (`python -m runner.cli run-predictions
--set gui-grounding --file predictions.jsonl`).

Usage:
  uv run python evals/jev_box/predict_grounding.py --suite ~/work/services/evals/suites/gui_grounding \
      [--out predictions.jsonl] [--workers 6] [--no-closer]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from bridge.decisions.provider import TypeSafeProvider  # noqa: E402
from bridge.tools.jev_operator import vision  # noqa: E402

USD_PER_TOKEN = 0.042 / 1e6


def load_env() -> None:
    env = ROOT / ".env"
    if env.exists():
        for line in env.read_text().splitlines():
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


async def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--suite", required=True)
    p.add_argument("--out", default="")
    p.add_argument("--workers", type=int, default=6)
    p.add_argument("--no-closer", action="store_true")
    a = p.parse_args()
    load_env()
    suite = Path(a.suite).expanduser()
    cases = json.loads((suite / "cases.json").read_text())["cases"]
    out = Path(a.out) if a.out else suite / "predictions.jsonl"
    provider = TypeSafeProvider()
    sha = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, capture_output=True, text=True).stdout.strip()
    system = f"freyja-vision-locate{'-single' if a.no_closer else ''}@{sha}"
    sem = asyncio.Semaphore(a.workers)
    images: dict[str, bytes] = {}

    async def one(c: dict) -> dict:
        async with sem:
            img = images.setdefault(c["image"], (suite / c["image"]).read_bytes())
            w, h = c["image_size"]
            try:
                loc = await vision.locate(provider, img, c["description"], look_closer=not a.no_closer)
            except Exception as exc:  # noqa: BLE001
                return {"item": c["id"], "prediction": None, "system": system, "error": str(exc)[:200]}
            return {
                "item": c["id"],
                "prediction": {
                    "x": loc.x * w,
                    "y": loc.y * h,
                    "box": [loc.box.left * w, loc.box.top * h, loc.box.right * w, loc.box.bottom * h],
                    "confidence": loc.confidence,
                    "visible": loc.visible,
                    "stages": loc.stages,
                },
                "latency_ms": float(loc.latency_ms),
                "cost_usd": loc.input_tokens * USD_PER_TOKEN,
                "system": system,
            }

    rows = await asyncio.gather(*(one(c) for c in cases))
    with out.open("w") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")
    errs = sum(1 for r in rows if r.get("prediction") is None)
    print(f"{len(rows)} predictions ({errs} without a point) -> {out}  system {system}")


if __name__ == "__main__":
    asyncio.run(main())
