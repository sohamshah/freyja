"""Export a GUI-grounding dataset from the computer-use environments.

One screenshot per start page at 1280 x 900, and for every labelled control
in view a case: a short description as a person would give it ("the 'Save'
button", "the 'Work email' text field") and the control's rectangle from
the DOM as the gold answer. Written in the shape the evals platform's
offline runner takes (suites/gui_grounding in ~/work/services/evals).

Usage:
  uv run python evals/jev_box/export_grounding_cases.py --out /path/to/suites/gui_grounding [--per-page 40]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / "evals")]

from computer_use.run import _bootstrap, _load_env, make_server, task_url  # noqa: E402

_bootstrap(None)
_load_env()

from computer_use import envs  # noqa: E402
from computer_use.browser import Browser  # noqa: E402
from computer_use.tasks import all_tasks  # noqa: E402
from jev_box.web_envs import CONTROLS_JS, describe  # noqa: E402


async def export(out: Path, per_page: int, seed: int) -> None:
    rng = random.Random(seed)
    pages: list = []
    seen: set[tuple[str, str]] = set()
    for t in all_tasks():
        key = (t.env, t.start.split("?")[0])
        # Only the environments the evals platform vendors (the `basics`
        # fixtures are test pages, not envs).
        if key in seen or t.chain or t.env not in envs.NAMES:
            continue
        seen.add(key)
        pages.append(t)
    shots = out / "shots"
    shots.mkdir(parents=True, exist_ok=True)
    server = make_server()
    browser = await Browser.launch(width=1280, height=900)
    cases = []
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
                controls = controls[:per_page]
                if not controls:
                    continue
                name = f"{task.env}__{Path(task.start.split('?')[0]).stem}"
                (shots / f"{name}.png").write_bytes(await page.screenshot(fmt="png"))
                vw, vh = browser.viewport
                for i, c in enumerate(controls):
                    r = c["rect"]
                    cases.append(
                        {
                            "id": f"{name}-{i:02d}",
                            "env": task.env,
                            "page": task.start.split("?")[0],
                            "image": f"shots/{name}.png",
                            "image_size": [vw, vh],
                            "description": describe(c),
                            "kind": c["kind"],
                            "expected": {
                                "box": [round(r[0], 1), round(r[1], 1), round(r[2], 1), round(r[3], 1)],
                                "label": c["label"],
                            },
                        }
                    )
                print(f"{name}: {len(controls)} cases")
            finally:
                await browser.dispose_context(ctx)
    finally:
        await browser.close()
    (out / "cases.json").write_text(
        json.dumps(
            {
                "title": "GUI grounding on the computer-use environments",
                "viewport": [1280, 900],
                "cases": cases,
            },
            indent=1,
        )
        + "\n"
    )
    print(f"{len(cases)} cases, {len(list(shots.glob('*.png')))} screenshots -> {out}")


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--out", required=True)
    p.add_argument("--per-page", type=int, default=40)
    p.add_argument("--seed", type=int, default=7)
    a = p.parse_args()
    asyncio.run(export(Path(a.out).expanduser(), a.per_page, a.seed))


if __name__ == "__main__":
    main()
