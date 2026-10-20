"""Smoke test: one real screenshot of this Mac, a few Noul/Choice/Box questions.

Usage: uv run python evals/jev_box/smoke.py [--model jev-1.14.0] [--shot path.png]
Writes an annotated copy next to the screenshot so the boxes can be eyeballed.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).resolve().parent))
import api  # noqa: E402


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--model", default="jev-1.14.0")
    p.add_argument("--shot", default=None)
    a = p.parse_args()

    if a.shot:
        shot = Path(a.shot)
    else:
        shot = Path(tempfile.gettempdir()) / "jev_smoke.png"
        subprocess.run(["screencapture", "-x", "-t", "png", str(shot)], check=True)
    src = Image.open(shot)
    prep = api.prepare(src)
    print(f"screenshot {src.size} -> {prep.width}x{prep.height} (scale {prep.scale:.3f}), {len(prep.png)/1024:.0f} KB png")

    ref, art = api.asset(prep.png)
    questions = {
        "menubar_visible": {"type": "noul", "instructions": "Is a macOS menu bar visible along the top edge of `screen`?"},
        "dock_visible": {"type": "noul", "instructions": "Is the macOS Dock visible in `screen`?"},
        "frontmost_kind": {
            "type": "choice",
            "instructions": "What kind of application window is largest in `screen`?",
            "criteria": {
                "terminal": "A terminal or shell window with monospace text.",
                "browser": "A web browser window with tabs and an address bar.",
                "editor": "A code or text editor.",
                "chat": "A chat or messaging application.",
                "other": "Something else, or no window is visible.",
            },
        },
        "apple_menu": {"type": "box", "instructions": "the Apple logo menu item at the far left of the menu bar in `screen`"},
        "clock": {"type": "box", "instructions": "the clock (time of day) at the right end of the menu bar in `screen`"},
        "dock": {"type": "box", "instructions": "the macOS Dock in `screen`"},
    }
    res = api.ask({"screen": ref}, questions, {ref: art}, model=a.model)
    print(f"model {res.model}  latency {res.latency_ms:.0f} ms  usage {json.dumps(res.usage)}")
    for name, ans in res.answers.items():
        print(f"  {name}: {json.dumps(ans)}")

    img = Image.open(__import__('io').BytesIO(prep.png))
    draw = ImageDraw.Draw(img)
    colors = {"apple_menu": "red", "clock": "lime", "dock": "cyan"}
    for name, ans in res.answers.items():
        if ans.get("type") == "box" or "box" in ans:
            x0, y0, x1, y1 = api.box_px(ans, img.width, img.height)
            draw.rectangle([x0, y0, x1, y1], outline=colors.get(name, "yellow"), width=3)
            draw.text((x0 + 4, y0 + 4), f"{name} {ans.get('confidence', 0):.2f}", fill=colors.get(name, "yellow"))
    out = shot.with_name(shot.stem + "_boxes.png")
    img.save(out)
    print("annotated:", out)


if __name__ == "__main__":
    main()
