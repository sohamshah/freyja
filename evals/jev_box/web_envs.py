"""Grounding and blind operation on the headless web environments.

Two experiments that need no desktop (they run while the Mac is locked):

  ground   On each environment's start pages, every labelled control in view
           is a target: its rectangle from the DOM is the truth, and
           `vision.locate` is asked for it by a short description from a page
           screenshot. Optionally the planner model is asked for the same
           control's pixel coordinates: that is a planner click today.

  blind    Tasks run through the real operator loop with a surface that
           exposes no elements and no text, as an app with an empty
           accessibility tree does. Every action then comes from the planner's
           screenshot, grounded by Jev (`--grounder jev`, the new path) or by
           the planner's own coordinates (`--grounder llm`, the old path).
           Reward is the environment's own ground truth.

Usage:
  uv run python evals/jev_box/web_envs.py ground [--pages 12] [--per-page 25] [--llm claude-opus-5-5 --llm-limit 80]
  uv run python evals/jev_box/web_envs.py blind --grounder jev --tasks basics/form_save,basics/rows_one [--reps 1]
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import dataclasses
import json
import random
import re
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / "evals")]

from computer_use.run import _bootstrap, _load_env, make_server, task_url  # noqa: E402

DATA = ROOT / "evals/jev_box/data"

CONTROLS_JS = r"""
(() => {
  const sel = 'button, a[href], input, select, textarea, summary, [role=button], [role=link], [role=tab], [role=menuitem], [role=checkbox], [role=radio], [role=option], [role=switch]';
  const out = [];
  const vw = innerWidth, vh = innerHeight;
  const seen = new Set();
  for (const el of document.querySelectorAll(sel)) {
    const r = el.getBoundingClientRect();
    if (r.width < 10 || r.height < 10 || r.left < 0 || r.top < 0 || r.right > vw || r.bottom > vh) continue;
    const cs = getComputedStyle(el);
    if (cs.visibility === 'hidden' || cs.display === 'none' || cs.opacity === '0') continue;
    const cx = r.left + r.width / 2, cy = r.top + r.height / 2;
    const top = document.elementFromPoint(cx, cy);
    if (!top || !(el === top || el.contains(top) || top.contains(el))) continue;
    if (el.tagName === 'INPUT' && el.type === 'hidden') continue;
    let label = el.getAttribute('aria-label') || el.getAttribute('placeholder') || '';
    if (!label && el.tagName === 'INPUT' && ['button', 'submit', 'reset'].includes(el.type)) label = el.value;
    if (!label && el.labels && el.labels.length) label = el.labels[0].innerText;
    if (!label) label = el.innerText || el.title || el.alt || '';
    label = label.trim().replace(/\s+/g, ' ').slice(0, 60);
    if (!label) continue;
    let kind = (el.getAttribute('role') || el.tagName.toLowerCase());
    if (el.tagName === 'INPUT') kind = (el.type || 'text') === 'text' || el.type === 'email' || el.type === 'search' || el.type === 'tel' || el.type === 'number' ? 'text field' : (el.type || 'text');
    else if (kind === 'a') kind = 'link';
    else if (kind === 'select') kind = 'dropdown';
    else if (kind === 'textarea') kind = 'text area';
    const key = kind + '|' + label;
    if (seen.has(key)) { out.push({dup: key}); continue; }
    seen.add(key);
    out.push({label, kind, rect: [r.left, r.top, r.width, r.height]});
  }
  const dups = new Set(out.filter(o => o.dup).map(o => o.dup));
  return out.filter(o => !o.dup && !dups.has(o.kind + '|' + o.label));
})()
"""


def describe(c: dict) -> str:
    kind = c["kind"]
    if kind in ("text field", "text area", "dropdown"):
        return f"the '{c['label']}' {kind}"
    if kind in ("checkbox", "radio", "switch"):
        return f"the '{c['label']}' {kind}"
    if kind in ("submit", "button", "link", "tab", "menuitem", "option", "summary"):
        word = {"submit": "button", "menuitem": "menu item", "summary": "disclosure"}.get(kind, kind)
        return f"the '{c['label']}' {word}"
    return f"the '{c['label']}' control"


def inside(p: tuple[float, float], r: list[float]) -> bool:
    return r[0] <= p[0] <= r[0] + r[2] and r[1] <= p[1] <= r[1] + r[3]


def planner_completer(model: str):
    from bridge.freyja_bridge import build_provider
    from bridge.tools.jev_operator.handoff import completer_from_provider

    return completer_from_provider(build_provider(model, "off"))


async def llm_point(completer, image: bytes, mime: str, size: tuple[int, int], description: str) -> dict:
    from engine.types import ImageBlock, Message

    system = (
        "You are the planner behind a fast operator. The screenshot shows one browser page; "
        "coordinates are pixels in that image (its size is screenshot_size). "
        'Return JSON only: {"x": int, "y": int} for the centre of the control described.'
    )
    payload = {"screenshot_size": {"width": size[0], "height": size[1]}, "control": description}
    user = Message.user_with_images(
        json.dumps(payload), [ImageBlock(source_type="base64", data=base64.b64encode(image).decode("ascii"), media_type=mime)]
    )
    t0 = time.perf_counter()
    text = await completer([user], system, 200)
    m = re.search(r"\{[^{}]*\}", text or "")
    try:
        out = json.loads(m.group(0)) if m else {}
    except ValueError:
        out = {}
    return {"x": out.get("x"), "y": out.get("y"), "latency_ms": int((time.perf_counter() - t0) * 1000)}


# ─── ground ───────────────────────────────────────────────────────


async def ground(a) -> Path:
    from computer_use.browser import Browser
    from computer_use.tasks import all_tasks

    from bridge.decisions.provider import TypeSafeProvider
    from bridge.tools.jev_operator import vision

    provider = TypeSafeProvider()
    completer = planner_completer(a.llm) if a.llm else None
    rng = random.Random(a.seed)
    tasks = all_tasks()
    pages: list = []
    seen: set[tuple[str, str]] = set()
    for t in tasks:
        key = (t.env, t.start.split("?")[0])
        if key in seen or t.chain:
            continue
        seen.add(key)
        pages.append(t)
    rng.shuffle(pages)
    pages = pages[: a.pages]
    server = make_server()
    browser = await Browser.launch(width=1280, height=900)
    out_path = DATA / f"web_ground_{time.strftime('%Y%m%d-%H%M%S')}.jsonl"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    llm_budget = a.llm_limit
    try:
        for task in pages:
            ep = server.new_episode(task.env, dict(task.config))
            ctx = await browser.new_context()
            page = await browser.new_page(context=ctx)
            try:
                await page.navigate(task_url(server, task, ep), timeout=20)
                await asyncio.sleep(1.2)
                controls = await page.evaluate(CONTROLS_JS)
                if isinstance(controls, str):
                    controls = json.loads(controls)
                controls = [c for c in controls if c.get("label")]
                rng.shuffle(controls)
                controls = controls[: a.per_page]
                png = await page.screenshot(fmt="png")
                jpg = await page.screenshot()
                vw, vh = browser.viewport
                print(f"{task.env}/{task.start[:40]}: {len(controls)} targets")
                with out_path.open("a") as fh:
                    for c in controls:
                        desc = describe(c)
                        row = {
                            "env": task.env,
                            "page": task.start,
                            "kind": c["kind"],
                            "label": c["label"],
                            "description": desc,
                            "rect": c["rect"],
                            "area_frac": (c["rect"][2] * c["rect"][3]) / (vw * vh),
                            "variants": {},
                        }
                        for name, (image, closer) in {
                            "png": (png, True),
                            "png_single": (png, False),
                            "jpeg70": (jpg, True),
                        }.items():
                            try:
                                loc = await vision.locate(provider, image, desc, look_closer=closer)
                            except Exception as exc:  # noqa: BLE001
                                row["variants"][name] = {"error": str(exc)[:200]}
                                continue
                            point = (loc.x * vw, loc.y * vh)
                            row["variants"][name] = {
                                "hit": inside(point, c["rect"]),
                                "visible": round(loc.visible, 3),
                                "confidence": round(loc.confidence, 3),
                                "stages": loc.stages,
                                "input_tokens": loc.input_tokens,
                                "latency_ms": loc.latency_ms,
                                "point": [round(point[0]), round(point[1])],
                            }
                        if completer is not None and llm_budget > 0:
                            llm_budget -= 1
                            g = await llm_point(completer, jpg, "image/jpeg", (vw, vh), desc)
                            if isinstance(g.get("x"), (int, float)) and isinstance(g.get("y"), (int, float)):
                                g["hit"] = inside((float(g["x"]), float(g["y"])), c["rect"])
                            else:
                                g["hit"] = False
                            row["variants"]["llm_" + a.llm] = g
                        fh.write(json.dumps(row) + "\n")
                        v = row["variants"]
                        print(f"  {desc[:48]:48s} " + " ".join(f"{k}={'1' if r.get('hit') else '0'}" for k, r in v.items()))
            finally:
                await browser.dispose_context(ctx)
    finally:
        await browser.close()
    return out_path


def report_ground(path: Path) -> None:
    rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    names = sorted({k for r in rows for k in r["variants"]})

    def acc(sel, name):
        got = [r["variants"][name] for r in sel if name in r["variants"] and "error" not in r["variants"][name]]
        return f"{sum(1 for g in got if g.get('hit'))/len(got)*100:5.1f}% ({len(got)})" if got else "   -   "

    print(f"\n{len(rows)} targets from {path.name}")
    print(f"{'':26s}" + "".join(f"{n:>24s}" for n in names))
    print(f"{'all':26s}" + "".join(f"{acc(rows, n):>24s}" for n in names))
    for env in sorted({r["env"] for r in rows}):
        sel = [r for r in rows if r["env"] == env]
        print(f"{env:26s}" + "".join(f"{acc(sel, n):>24s}" for n in names))
    for kind in sorted({r["kind"] for r in rows}):
        sel = [r for r in rows if r["kind"] == kind]
        if len(sel) >= 6:
            print(f"{kind:26s}" + "".join(f"{acc(sel, n):>24s}" for n in names))
    for lo, hi in [(0, 0.001), (0.001, 0.003), (0.003, 0.01), (0.01, 1)]:
        sel = [r for r in rows if lo <= r["area_frac"] < hi]
        print(f"{'area [%g,%g)' % (lo, hi):26s}" + "".join(f"{acc(sel, n):>24s}" for n in names))
    for n in names:
        got = [r["variants"][n] for r in rows if n in r["variants"] and "error" not in r["variants"][n]]
        if not got:
            continue
        lat = sorted(g.get("latency_ms", 0) for g in got)
        toks = [g.get("input_tokens", 0) for g in got]
        line = f"{n}: latency p50 {lat[len(lat)//2]} ms p90 {lat[int(len(lat)*0.9)]} ms"
        if any(toks):
            line += f", tokens/lookup {sum(toks)/len(toks):.0f} (${sum(toks)/len(toks)*0.042/1e6:.6f})"
        if "confidence" in got[0]:
            for t in (0.9, 0.7, 0.5):
                sel = [g for g in got if g.get("confidence", 0) >= t]
                if sel:
                    line += f"; conf>={t}: {len(sel)/len(got)*100:.0f}% cov {sum(1 for g in sel if g['hit'])/len(sel)*100:.0f}% prec"
        print(line)


# ─── blind ────────────────────────────────────────────────────────


def blind_surface_class(original) -> type:
    Base = original()

    class BlindSurface(Base):
        """The page, with nothing readable: no controls, no text. What an app
        with an empty accessibility tree gives the operator."""

        name = "pixels"

        async def observe(self, target: Any):
            o = await super().observe(target)
            return dataclasses.replace(o, elements=[], screen_text="", dialog=None, menu_open=False, page_text="")

    return BlindSurface


def install_llm_grounder(model: str) -> None:
    """Swap Jev's grounding for the planner model's own pixel guess, through
    the same loop, so the two runs differ only in who finds the control."""
    import io

    from PIL import Image

    from bridge.decisions.types import BoxAnswer
    from bridge.tools.jev_operator import vision

    completer = planner_completer(model)

    async def locate(provider, image: bytes, description: str, *, model: str = "", look_closer: bool = True):
        im = Image.open(io.BytesIO(image))
        t0 = time.perf_counter()
        g = await llm_point(completer, image, "image/jpeg" if image[:2] == b"\xff\xd8" else "image/png", im.size, description)
        ok = isinstance(g.get("x"), (int, float)) and isinstance(g.get("y"), (int, float))
        x = float(g["x"]) / im.width if ok else 0.5
        y = float(g["y"]) / im.height if ok else 0.5
        box = BoxAnswer(left=x, top=y, right=x, bottom=y, confidence=1.0 if ok else 0.0)
        return vision.Located(x=x, y=y, box=box, visible=1.0 if ok else 0.0, confidence=box.confidence, stages=1, input_tokens=0, latency_ms=int((time.perf_counter() - t0) * 1000))

    vision.locate = locate  # type: ignore[assignment]


async def blind(a) -> Path:
    from computer_use import envs, jev_policy
    from computer_use.browser import Browser
    from computer_use.jev_policy import JevConfig, run_jev
    from computer_use.tasks import select

    from bridge.tools.jev_operator.act import Actuator

    try:
        from bridge.tools.jev_operator import vision

        visible_min = vision.VISIBLE_MIN
    except ImportError:  # a jev under test from before grounding existed
        visible_min = 0.5

    original = jev_policy._surface_class
    jev_policy._surface_class = lambda: blind_surface_class(original)

    async def no_focus(self, bundle: str) -> None:
        return None

    Actuator.focus = no_focus  # type: ignore[assignment]
    if a.grounder == "llm":
        install_llm_grounder(a.llm)
    tasks = select(a.tasks.split(","))
    server = make_server()
    browser = await Browser.launch(width=1280, height=900)
    out_dir = DATA / f"web_blind_{a.tag or a.grounder}_{time.strftime('%Y%m%d-%H%M%S')}"
    out_dir.mkdir(parents=True, exist_ok=True)
    rows = []
    try:
        for task in tasks:
            for rep in range(a.reps):
                ep = server.new_episode(task.env, dict(task.config))
                goal = task.goal.replace("{base}", f"{server.base}/{task.env}")
                if task.env not in envs.NAMES:
                    goal = goal.replace("{run}", ep.id)
                ctx = await browser.new_context()
                page = await browser.new_page(context=ctx)
                t0 = time.time()
                try:
                    await page.navigate(task_url(server, task, ep), timeout=20)
                    await asyncio.sleep(1.0)
                    run = await asyncio.wait_for(
                        run_jev(
                            page,
                            goal,
                            JevConfig(max_steps=a.max_steps, max_runtime_s=600, allow_irreversible=task.allow_irreversible, llm_model=a.llm, llm_max_calls=a.llm_calls),
                            log_dir=out_dir / "runs",
                        ),
                        timeout=720,
                    )
                except Exception as exc:  # noqa: BLE001
                    run = {"status": "harness_error", "text": f"{type(exc).__name__}: {exc}", "steps": 0, "jev_calls": 0, "llm_calls": 0}
                finally:
                    await asyncio.sleep(0.6)
                    await browser.dispose_context(ctx)
                if task.env in envs.NAMES:
                    state, events = envs.backend(task.env).view(ep), list(ep.events)
                else:
                    state, events = {}, server.events_for_run(ep.id)
                try:
                    chk = task.check(state, events, run)
                    reward, why, marks = chk.reward, chk.why, chk.marks
                except Exception as exc:  # noqa: BLE001
                    reward, why, marks = 0.0, f"check raised {exc}", {}
                log = run.get("log")
                locates = []
                if log and Path(log).exists():
                    for line in Path(log).read_text().splitlines():
                        try:
                            e = json.loads(line)
                        except ValueError:
                            continue
                        if e.get("event") == "locate":
                            locates.append(e)
                row = {
                    "task": task.id,
                    "rep": rep,
                    "grounder": a.grounder,
                    "reward": reward,
                    "marks": marks,
                    "why": why,
                    "status": run.get("status"),
                    "steps": run.get("steps"),
                    "jev_calls": run.get("jev_calls"),
                    "llm_calls": run.get("llm_calls"),
                    "llm_tokens": run.get("llm_tokens"),
                    "secs": round(time.time() - t0, 1),
                    "locates": len(locates),
                    "locate_found": sum(1 for e in locates if e.get("visible", 0) >= visible_min),
                    "log": log,
                    "summary": (run.get("summary") or run.get("text") or "")[:300],
                }
                rows.append(row)
                (out_dir / f"{task.id.replace('/', '__')}-{rep}.json").write_text(json.dumps(row, indent=1, default=str))
                print(f"{'PASS' if reward >= 1 else 'FAIL'} {task.id:28s} reward {reward:.2f} status {row['status']:16s} steps {row['steps']} llm {row['llm_calls']} locates {row['locates']} ({row['locate_found']} found) {row['secs']}s  {why[:80]}")
    finally:
        await browser.close()
    (out_dir / "summary.json").write_text(json.dumps(rows, indent=1, default=str))
    n = len(rows)
    print(f"\n{a.tag or a.grounder}: {sum(1 for r in rows if r['reward'] >= 1)}/{n} tasks passed, mean reward {sum(r['reward'] for r in rows)/max(1, n):.2f}, mean LLM calls {sum(r['llm_calls'] or 0 for r in rows)/max(1, n):.1f}")
    return out_dir


def main() -> None:
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)
    g = sub.add_parser("ground")
    g.add_argument("--pages", type=int, default=12)
    g.add_argument("--per-page", type=int, default=25)
    g.add_argument("--seed", type=int, default=7)
    g.add_argument("--llm", default="")
    g.add_argument("--llm-limit", type=int, default=80)
    g.add_argument("--report", default="")
    b = sub.add_parser("blind")
    b.add_argument("--grounder", choices=["jev", "llm"], default="jev")
    b.add_argument("--tasks", default="basics/form_save,basics/rows_one,basics/rows_two,basics/modal_settings,basics/dropdown_region,widgets/twin_cards")
    b.add_argument("--reps", type=int, default=1)
    b.add_argument("--max-steps", type=int, default=20)
    b.add_argument("--llm-calls", type=int, default=14)
    b.add_argument("--llm", default="claude-opus-5-5")
    b.add_argument("--jev-root", default="", help="run the operator from another checkout (a baseline)")
    b.add_argument("--tag", default="")
    a = p.parse_args()
    _bootstrap(getattr(a, "jev_root", "") or None)
    _load_env()
    if a.cmd == "ground":
        if a.report:
            report_ground(Path(a.report))
        else:
            report_ground(asyncio.run(ground(a)))
    else:
        asyncio.run(blind(a))


if __name__ == "__main__":
    main()
