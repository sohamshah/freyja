"""Grounding on real macOS windows, with accessibility frames as ground truth.

For each app the script opens (or finds) a window, reads its accessibility
tree exactly as the operator does, captures the window exactly as the
operator does, and asks `vision.locate` for every labelled control in view
by a short description ("the 'Equals' button"). A hit is the returned point
falling inside the control's accessibility frame. Optionally the planner
model is asked for the same control's pixel coordinates from the same
capture: that is what a planner click is today.

Usage:
  uv run python evals/jev_box/macos_ax.py --apps Calculator,Finder,"System Settings",Notes \
      --per-app 30 --llm claude-opus-5-5 --llm-limit 80
  uv run python evals/jev_box/macos_ax.py --report evals/jev_box/data/macos_ax_<ts>.jsonl
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import random
import re
import subprocess
import sys
import time
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from bridge.decisions.provider import TypeSafeProvider  # noqa: E402
from bridge.tools.jev_operator import vision  # noqa: E402
from bridge.tools.jev_operator.observe import build_observation  # noqa: E402

ROLE_WORDS = {
    "AXButton": "button",
    "AXCheckBox": "checkbox",
    "AXRadioButton": "radio button",
    "AXPopUpButton": "pop-up button",
    "AXMenuButton": "menu button",
    "AXMenuBarItem": "menu bar item",
    "AXMenuItem": "menu item",
    "AXTextField": "text field",
    "AXTextArea": "text area",
    "AXLink": "link",
    "AXRow": "row",
    "AXCell": "cell",
    "AXTab": "tab",
    "AXToolbarButton": "toolbar button",
    "AXDisclosureTriangle": "disclosure triangle",
    "AXSlider": "slider",
    "AXIncrementor": "stepper",
    "AXComboBox": "combo box",
    "AXStaticText": "text",
    "AXImage": "image",
    "AXOutline": "outline",
    "AXList": "list",
}
BUNDLES = {
    "Calculator": "com.apple.calculator",
    "Finder": "com.apple.finder",
    "System Settings": "com.apple.systempreferences",
    "Notes": "com.apple.Notes",
    "TextEdit": "com.apple.TextEdit",
    "Calendar": "com.apple.iCal",
    "Reminders": "com.apple.reminders",
    "Contacts": "com.apple.AddressBook",
    "Font Book": "com.apple.FontBook",
    "Activity Monitor": "com.apple.ActivityMonitor",
    "Dictionary": "com.apple.Dictionary",
    "Maps": "com.apple.Maps",
    "Shortcuts": "com.apple.shortcuts",
}


def load_env() -> None:
    env = ROOT / ".env"
    if env.exists():
        for line in env.read_text().splitlines():
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def window_for(native, bundle: str):
    wins = [w for w in native.list_windows() if w.bundle == bundle and w.bounds.w >= 200 and w.bounds.h >= 150]
    if not wins:
        return None
    return max(wins, key=lambda w: w.bounds.w * w.bounds.h)


def open_app(native, app: str, bundle: str, opened: list[str]):
    win = window_for(native, bundle)
    if win is not None:
        return win
    if app == "Finder":
        subprocess.run(["open", str(Path.home())], check=False)
    elif app == "TextEdit":
        p = Path("/tmp/jev_box_sample.txt")
        p.write_text("The quick brown fox jumps over the lazy dog.\n" * 5)
        subprocess.run(["open", "-a", app, str(p)], check=False)
    else:
        subprocess.run(["open", "-a", app], check=False)
    opened.append(app)
    for _ in range(40):
        time.sleep(0.5)
        win = window_for(native, bundle)
        if win is not None:
            time.sleep(1.5)
            return window_for(native, bundle)
    return None


def candidates(obs, win, per_app: int, rng: random.Random) -> list:
    wx, wy, ww, wh = win.bounds.x, win.bounds.y, win.bounds.w, win.bounds.h
    seen: dict[tuple[str, str], int] = defaultdict(int)
    out = []
    for e in obs.elements:
        if e.kind not in ("click", "type") or not e.label or e.offscreen or not e.enabled:
            continue
        x, y, w, h = e.bounds
        if w < 10 or h < 10 or x < wx - 1 or y < wy - 1 or x + w > wx + ww + 1 or y + h > wy + wh + 1:
            continue
        if e.role not in ROLE_WORDS or len(e.label) > 60:
            continue
        seen[(e.role, e.label)] += 1
        out.append(e)
    out = [e for e in out if seen[(e.role, e.label)] == 1]
    rng.shuffle(out)
    return out[:per_app]


def describe(e) -> str:
    return f"the '{e.label}' {ROLE_WORDS.get(e.role, 'control')}"


def inside(point: tuple[float, float], bounds: tuple[float, float, float, float]) -> bool:
    x, y, w, h = bounds
    return x <= point[0] <= x + w and y <= point[1] <= y + h


async def ask_llm(completer, image: bytes, mime: str, size: tuple[int, int], description: str) -> dict:
    """The planner's own pixel guess, framed as the replan door frames it."""
    from engine.types import ImageBlock, Message

    system = (
        "You are the planner behind a desktop operator. The screenshot shows only one app window; "
        "coordinates are pixels in that image (its size is screenshot_size). "
        'Return JSON only: {"x": int, "y": int} for the centre of the control described.'
    )
    payload = {"screenshot_size": {"width": size[0], "height": size[1]}, "control": description}
    user = Message.user_with_images(
        json.dumps(payload), [ImageBlock(source_type="base64", data=base64.b64encode(image).decode("ascii"), media_type=mime)]
    )
    t0 = time.perf_counter()
    text = await completer([user], system, 200)
    ms = int((time.perf_counter() - t0) * 1000)
    m = re.search(r"\{[^{}]*\}", text or "")
    try:
        out = json.loads(m.group(0)) if m else {}
    except ValueError:
        out = {}
    return {"x": out.get("x"), "y": out.get("y"), "latency_ms": ms, "raw": (text or "")[:200]}


async def run(a) -> Path:
    import freyja_native as native

    load_env()
    provider = TypeSafeProvider()
    if not provider.available:
        raise SystemExit("TYPESAFE_AI_API_KEY is not set")
    completer = None
    if a.llm:
        from bridge.freyja_bridge import build_provider
        from bridge.tools.jev_operator.handoff import completer_from_provider

        completer = completer_from_provider(build_provider(a.llm, "off"))
    rng = random.Random(a.seed)
    out_path = ROOT / "evals/jev_box/data" / f"macos_ax_{time.strftime('%Y%m%d-%H%M%S')}.jsonl"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    opened: list[str] = []
    llm_budget = a.llm_limit
    shots_dir = out_path.with_suffix("")
    shots_dir.mkdir(exist_ok=True)
    try:
        for app in a.apps:
            bundle = BUNDLES[app]
            win = open_app(native, app, bundle, opened)
            if win is None:
                print(f"{app}: no window")
                continue
            tree = json.loads(native.read_ax_tree(win.pid, max_depth=25))
            obs = build_observation(tree, app_name=app, bundle=bundle, pid=win.pid)
            els = candidates(obs, win, a.per_app, rng)
            png = native.screenshot(window_id=win.id, max_dim=1568, format="png")
            jpg = native.screenshot(window_id=win.id, max_dim=1280, format="jpeg", quality=75)
            (shots_dir / f"{app.replace(' ', '_')}.png").write_bytes(png.png)
            wx, wy, ww, wh = win.bounds.x, win.bounds.y, win.bounds.w, win.bounds.h
            print(f"{app}: window {int(ww)}x{int(wh)} at ({int(wx)},{int(wy)}), capture {png.width}x{png.height}, {len(els)} targets")
            with out_path.open("a") as fh:
                for e in els:
                    desc = describe(e)
                    row = {
                        "app": app,
                        "role": e.role,
                        "label": e.label,
                        "description": desc,
                        "bounds": list(e.bounds),
                        "window": [wx, wy, ww, wh],
                        "area_frac": (e.bounds[2] * e.bounds[3]) / (ww * wh),
                        "variants": {},
                    }
                    variants = {
                        "png1568": (png.png, True),
                        "png1568_single": (png.png, False),
                        "jpeg1280": (jpg.png, True),
                    }
                    for name, (image, closer) in variants.items():
                        try:
                            loc = await vision.locate(provider, image, desc, look_closer=closer)
                        except Exception as exc:  # keep the row; report the failure
                            row["variants"][name] = {"error": str(exc)[:200]}
                            continue
                        point = (wx + loc.x * ww, wy + loc.y * wh)
                        row["variants"][name] = {
                            "hit": inside(point, e.bounds),
                            "visible": round(loc.visible, 3),
                            "confidence": round(loc.confidence, 3),
                            "stages": loc.stages,
                            "input_tokens": loc.input_tokens,
                            "latency_ms": loc.latency_ms,
                            "point": [round(point[0]), round(point[1])],
                        }
                    if completer is not None and llm_budget > 0:
                        llm_budget -= 1
                        g = await ask_llm(completer, jpg.png, jpg.mime_type, (jpg.width, jpg.height), desc)
                        if isinstance(g.get("x"), (int, float)) and isinstance(g.get("y"), (int, float)):
                            point = (wx + float(g["x"]) * ww / jpg.width, wy + float(g["y"]) * wh / jpg.height)
                            g["hit"] = inside(point, e.bounds)
                            g["point"] = [round(point[0]), round(point[1])]
                        else:
                            g["hit"] = False
                        row["variants"]["llm_" + a.llm] = g
                    fh.write(json.dumps(row) + "\n")
                    v = row["variants"]
                    print(
                        f"  {'HIT ' if v.get('png1568', {}).get('hit') else 'miss'} {desc[:50]:50s} "
                        + " ".join(f"{k}={'1' if r.get('hit') else '0'}" for k, r in v.items())
                    )
    finally:
        if not a.keep_open:
            for app in opened:
                subprocess.run(["osascript", "-e", f'tell application "{app}" to quit'], check=False)
    return out_path


def report(path: Path) -> None:
    rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    names = sorted({k for r in rows for k in r["variants"]})
    print(f"\n{len(rows)} targets from {path.name}")

    def acc(sel, name):
        got = [r["variants"][name] for r in sel if name in r["variants"] and "error" not in r["variants"][name]]
        if not got:
            return "   -   "
        return f"{sum(1 for g in got if g.get('hit'))/len(got)*100:5.1f}% ({len(got)})"

    print(f"{'':28s}" + "".join(f"{n:>22s}" for n in names))
    print(f"{'all':28s}" + "".join(f"{acc(rows, n):>22s}" for n in names))
    for app in sorted({r["app"] for r in rows}):
        sel = [r for r in rows if r["app"] == app]
        print(f"{app:28s}" + "".join(f"{acc(sel, n):>22s}" for n in names))
    for role in sorted({r["role"] for r in rows}):
        sel = [r for r in rows if r["role"] == role]
        if len(sel) >= 5:
            print(f"{role:28s}" + "".join(f"{acc(sel, n):>22s}" for n in names))
    for lo, hi in [(0, 0.001), (0.001, 0.003), (0.003, 0.01), (0.01, 1)]:
        sel = [r for r in rows if lo <= r["area_frac"] < hi]
        print(f"{'area [%g,%g)' % (lo, hi):28s}" + "".join(f"{acc(sel, n):>22s}" for n in names))
    for n in names:
        got = [r["variants"][n] for r in rows if n in r["variants"] and "error" not in r["variants"][n]]
        if not got:
            continue
        lat = sorted(g.get("latency_ms", 0) for g in got)
        toks = [g.get("input_tokens", 0) for g in got]
        line = f"{n}: latency p50 {lat[len(lat)//2]} ms p90 {lat[int(len(lat)*0.9)]} ms"
        if any(toks):
            line += f", tokens/lookup {sum(toks)/len(toks):.0f}, ${sum(toks)/len(toks)*0.042/1e6:.6f}/lookup"
        if "visible" in got[0]:
            for t in (0.9, 0.7, 0.5):
                sel = [g for g in got if g.get("confidence", 0) >= t]
                if sel:
                    line += f"; conf>={t}: {len(sel)/len(got)*100:.0f}% cov, {sum(1 for g in sel if g['hit'])/len(sel)*100:.0f}% prec"
            errs = sum(1 for r in rows if n in r["variants"] and "error" in r["variants"][n])
            if errs:
                line += f"; {errs} errors"
        print(line)


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--apps", default="Calculator,Finder,System Settings,Notes,TextEdit,Calendar,Reminders,Contacts")
    p.add_argument("--per-app", type=int, default=30)
    p.add_argument("--seed", type=int, default=7)
    p.add_argument("--llm", default="")
    p.add_argument("--llm-limit", type=int, default=100)
    p.add_argument("--keep-open", action="store_true")
    p.add_argument("--report", default="")
    a = p.parse_args()
    if a.report:
        report(Path(a.report))
        return
    a.apps = [s.strip() for s in a.apps.split(",") if s.strip()]
    path = asyncio.run(run(a))
    report(path)


if __name__ == "__main__":
    main()
