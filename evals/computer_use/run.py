#!/usr/bin/env python3
"""Run computer-use episodes against the mock environments and score them.

    uv run python evals/computer_use/run.py list
    uv run python evals/computer_use/run.py run airline vertex --reps 3 --jobs 4 --tag after
    uv run python evals/computer_use/run.py run airline/select --quirk upsell_modal=off
    uv run python evals/computer_use/run.py run airline --jev-root ../baseline --tag before
    uv run python evals/computer_use/run.py report results/<dir> [results/<dir> ...]
    uv run python evals/computer_use/run.py serve            # browse the mocks yourself

Every episode runs in a fresh headless browser context against a fresh server
episode, so runs never touch a real browser, window or account. `--jev-root`
imports jev from another checkout (a baseline) while the envs stay the same.
Results: one JSON per episode plus summary.json under results/<timestamp>-<tag>/.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
import traceback
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]


def _bootstrap(jev_root: str | None) -> None:
    """Put the jev under test first on the path and import it before anything
    else can import another copy (scripts/jev_harness.py adds its own repo)."""
    root = Path(jev_root).expanduser().resolve() if jev_root else REPO
    sys.path[:0] = [str(root), str(REPO / "evals")]
    import bridge.tools.jev_operator.dom_surface  # noqa: F401,PLC0415
    import bridge.tools.jev_operator.loop  # noqa: F401,PLC0415

    sys.path.insert(1, str(REPO))  # the envs' own imports (scripts/, tests/fixtures)
    _load_env()


def _load_env() -> None:
    for p in (REPO / ".env", Path.home() / "personal" / "freyja" / ".env"):
        if not p.exists():
            continue
        for line in p.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
        return


def make_server() -> Any:
    from computer_use.envs.airline import backend as airline  # noqa: PLC0415
    from computer_use.server import EnvServer, EnvSpec  # noqa: PLC0415
    from computer_use.tasks import FIXTURES  # noqa: PLC0415

    return EnvServer(
        [
            EnvSpec("airline", HERE / "envs" / "airline" / "site", airline.backend, airline.init),
            EnvSpec("vertex", FIXTURES),
            EnvSpec("basics", FIXTURES),
        ]
    ).start()


def _parse_quirks(pairs: list[str]) -> dict[str, bool]:
    out = {}
    for p in pairs:
        k, _, v = p.partition("=")
        out[k.strip()] = v.strip().lower() not in ("0", "off", "false", "no")
    return out


async def run_episode(
    server: Any, browser: Any, task: Any, rep: int, out_dir: Path, args: Any
) -> dict[str, Any]:
    from computer_use.envs.airline import backend as airline  # noqa: PLC0415
    from computer_use.jev_policy import JevConfig, run_jev  # noqa: PLC0415

    cfg = dict(task.config)
    if args.quirk:
        q = dict(cfg.get("quirks") or {})
        q.update(_parse_quirks(args.quirk))
        cfg["quirks"] = q
    ep = server.new_episode(task.env, cfg)
    goal = task.goal
    if task.env == "airline":
        url = server.start_url(ep, f"/airline/{task.start}")
    else:
        sep = "&" if "?" in task.start else "?"
        url = f"{server.base}/{task.env}/{task.start}{sep}run={ep.id}"
        goal = goal.replace("{base}", f"{server.base}/{task.env}").replace("{run}", ep.id)
    ctx = await browser.new_context()
    page = await browser.new_page(context=ctx)
    row: dict[str, Any] = {
        "task": task.id,
        "env": task.env,
        "rep": rep,
        "episode": ep.id,
        "tag": args.tag,
        "quirks": cfg.get("quirks"),
        "goal": goal,
    }
    t0 = time.time()
    try:
        await page.navigate(url, timeout=20)
        await asyncio.sleep(1.0)
        cfg_j = JevConfig(
            max_steps=task.max_steps,
            max_runtime_s=task.timeout_s,
            allow_irreversible=task.allow_irreversible,
            notes=args.notes or "",
        )
        if task.chain:
            run = {
                "status": "done",
                "text": "",
                "steps": 0,
                "jev_calls": 0,
                "llm_calls": 0,
                "llm_ms": 0,
                "links": [],
            }
            for i, g in enumerate(task.chain):
                one = await asyncio.wait_for(
                    run_jev(page, g, cfg_j, log_dir=out_dir / "runs"), timeout=task.timeout_s + 120
                )
                for k in ("steps", "jev_calls", "llm_calls", "llm_ms"):
                    run[k] += one.get(k) or 0
                run["links"].append({"goal": g[:80], "status": one["status"], "log": one["log"]})
                run["text"] += f"\n--- goal {i + 1}: {one['status']}\n{one['text']}"
                run["status"], run["log"] = one["status"], one["log"]
                if one["status"] != "done":
                    break
        else:
            run = await asyncio.wait_for(
                run_jev(page, goal, cfg_j, log_dir=out_dir / "runs"), timeout=task.timeout_s + 120
            )
    except Exception as exc:  # noqa: BLE001  (a crashed episode scores 0 and says why)
        run = {
            "status": "harness_error",
            "text": f"{type(exc).__name__}: {exc}",
            "error": traceback.format_exc()[-2000:],
        }
    finally:
        await asyncio.sleep(0.6)  # the page's last beacons
        try:
            row["final_url"] = page.url
            shot = await page.screenshot()
            (out_dir / "frames").mkdir(parents=True, exist_ok=True)
            (out_dir / "frames" / f"{task.id.replace('/', '__')}-{rep}.jpg").write_bytes(shot)
        except Exception:  # noqa: BLE001
            pass
        await browser.dispose_context(ctx)
    if task.env == "airline":
        state = airline.session_view(ep)
        state["_total"] = airline.total(ep)
        events = list(ep.events)
    else:
        state = {}
        events = server.events_for_run(ep.id)
    try:
        chk = task.check(state, events, run)
    except Exception as exc:  # noqa: BLE001
        from computer_use.tasks import Check  # noqa: PLC0415

        chk = Check(0.0, {}, [], f"check raised {type(exc).__name__}: {exc}")
    row.update(
        {
            "reward": chk.reward,
            "marks": chk.marks,
            "forbidden": chk.forbidden,
            "why": chk.why,
            "status": run.get("status"),
            "secs": round(time.time() - t0, 1),
            "steps": run.get("steps"),
            "jev_calls": run.get("jev_calls"),
            "llm_calls": run.get("llm_calls"),
            "llm_ms": run.get("llm_ms"),
            "log": run.get("log"),
            "result": run.get("text", "")[:6000],
            "server_events": events[-80:],
            "state": {k: v for k, v in state.items() if k != "quirks"},
        }
    )
    if run.get("error"):
        row["error"] = run["error"]
    (out_dir / f"{task.id.replace('/', '__')}-{rep}.json").write_text(
        json.dumps(row, indent=1, default=str)
    )
    return row


async def run_many(names: list[str], args: Any) -> int:
    from computer_use.browser import Browser  # noqa: PLC0415
    from computer_use.tasks import select  # noqa: PLC0415

    tasks = select(names)
    if not tasks:
        print("no tasks match", names)
        return 2
    stamp = time.strftime("%Y%m%d-%H%M%S")
    out_dir = Path(args.out or HERE / "results" / f"{stamp}-{args.tag or 'run'}")
    out_dir.mkdir(parents=True, exist_ok=True)
    server = make_server()
    browser = await Browser.launch(width=1280, height=900)
    sem = asyncio.Semaphore(args.jobs)
    rows: list[dict[str, Any]] = []

    async def one(t: Any, rep: int) -> None:
        async with sem:
            r = await run_episode(server, browser, t, rep, out_dir, args)
            rows.append(r)
            print(
                f"{'PASS' if r['reward'] else 'FAIL'}  {r['task']:<28} rep={rep} "
                f"{str(r['status']):<18} {r['secs']:>6}s "
                f"steps={r['steps']} llm={r['llm_calls']}  {r['why'][:150]}",
                flush=True,
            )

    try:
        await asyncio.gather(*(one(t, rep) for rep in range(args.reps) for t in tasks))
    finally:
        await browser.close()
        server.stop()
    summary = summarize(rows)
    (out_dir / "summary.json").write_text(
        json.dumps(
            {
                "tag": args.tag,
                "jev_root": args.jev_root,
                "quirks": args.quirk,
                "summary": summary,
                "rows": [_slim(r) for r in rows],
            },
            indent=1,
        )
    )
    print_summary(summary)
    print(f"results: {out_dir}")
    return 0


def _slim(r: dict[str, Any]) -> dict[str, Any]:
    return {
        k: r.get(k)
        for k in (
            "task",
            "rep",
            "reward",
            "status",
            "secs",
            "steps",
            "llm_calls",
            "llm_ms",
            "why",
            "marks",
            "log",
        )
    }


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    by: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        by.setdefault(r["task"], []).append(r)
    out = {}
    for t, rs in sorted(by.items()):
        marks: dict[str, list[bool]] = {}
        for r in rs:
            for k, v in (r.get("marks") or {}).items():
                marks.setdefault(k, []).append(bool(v))
        out[t] = {
            "n": len(rs),
            "success": sum(r["reward"] for r in rs) / len(rs),
            "mean_secs": round(sum(r["secs"] for r in rs) / len(rs), 1),
            "mean_llm_calls": round(sum((r.get("llm_calls") or 0) for r in rs) / len(rs), 1),
            "marks": {k: round(sum(v) / len(v), 2) for k, v in marks.items()},
            "statuses": [r["status"] for r in rs],
        }
    return out


def print_summary(summary: dict[str, Any]) -> None:
    print(f"\n{'task':<30} {'n':>3} {'success':>8} {'secs':>7} {'llm':>5}  statuses")
    tot_n = tot_s = 0.0
    for t, s in summary.items():
        print(
            f"{t:<30} {s['n']:>3} {s['success']:>8.2f} {s['mean_secs']:>7} "
            f"{s['mean_llm_calls']:>5}  {','.join(map(str, s['statuses']))}"
        )
        tot_n += s["n"]
        tot_s += s["success"] * s["n"]
    if tot_n:
        print(f"{'ALL':<30} {int(tot_n):>3} {tot_s / tot_n:>8.2f}")


def report(dirs: list[str]) -> int:
    """Compare summaries side by side (e.g. before vs after)."""
    sums = []
    for d in dirs:
        p = Path(d) / "summary.json"
        sums.append((Path(d).name, json.loads(p.read_text())["summary"]))
    tasks = sorted({t for _, s in sums for t in s})
    head = f"{'task':<30}" + "".join(f" {name[-22:]:>24}" for name, _ in sums)
    print(head)
    for t in tasks:
        cells = []
        for _, s in sums:
            x = s.get(t)
            cell = f"{x['success']:.2f} ({x['n']}) {x['mean_secs']}s" if x else "-"
            cells.append(f" {cell:>24}")
        print(f"{t:<30}" + "".join(cells))
    return 0


async def serve_forever() -> None:
    server = make_server()
    ep = server.new_episode("airline", {})
    print(
        f"airline: {server.start_url(ep, '/airline/index.html')}\n"
        f"fixtures: {server.base}/vertex/questionnaire.html"
    )
    try:
        while True:
            await asyncio.sleep(3600)
    finally:
        server.stop()


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list")
    r = sub.add_parser("run")
    r.add_argument("names", nargs="*")
    r.add_argument("--reps", type=int, default=1)
    r.add_argument("--jobs", type=int, default=3)
    r.add_argument("--tag", default="")
    r.add_argument("--out")
    r.add_argument("--jev-root", help="import jev from this checkout (a baseline)")
    r.add_argument("--quirk", action="append", default=[], help="name=on|off, repeatable")
    r.add_argument(
        "--notes", help="jev-site notes text given to every run (a mitigation under test)"
    )
    rp = sub.add_parser("report")
    rp.add_argument("dirs", nargs="+")
    sub.add_parser("serve")
    args = ap.parse_args()
    _bootstrap(getattr(args, "jev_root", None))
    if args.cmd == "list":
        from computer_use.tasks import all_tasks  # noqa: PLC0415

        for t in all_tasks():
            print(f"{t.id:<30} {t.goal[:110]}")
        return 0
    if args.cmd == "report":
        return report(args.dirs)
    if args.cmd == "serve":
        asyncio.run(serve_forever())
        return 0
    import logging  # noqa: PLC0415

    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    return asyncio.run(run_many(args.names, args))


if __name__ == "__main__":
    sys.exit(main())
