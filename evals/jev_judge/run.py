"""Run judges over suites.

    uv run python evals/jev_judge/run.py --suite llmbar_natural,judgebench --judge jev --judge anthropic:claude-haiku-4-5
    uv run python evals/jev_judge/run.py --suite freyja_acceptance --judge jev --variant expected
    uv run python evals/jev_judge/run.py --suite llmbar_natural --judge jev --reps 20 --limit 30 --seed 7   # repeatability

Writes results/<timestamp>_<tag>.jsonl with one line per (judge, case, variant, rep),
including the case labels and question types so analyze.py needs nothing else.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import random
import sys
import time
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))

from judges import judge_cached, make_judge  # noqa: E402
from schema import CASES_DIR, RESULTS_DIR, Case, read_cases, state_text  # noqa: E402


def load_suite(name: str, limit: int | None, seed: int) -> list[Case]:
    cases = list(read_cases(CASES_DIR / f"{name}.jsonl"))
    if limit and limit < len(cases):
        rng = random.Random(seed)
        cases = rng.sample(cases, limit)
    return cases


async def run(args: argparse.Namespace) -> Path:
    suites = [s for s in args.suite.split(",") if s]
    judges = [make_judge(spec, args.concurrency) for spec in args.judge]
    variants = [v for v in args.variant.split(",") if v]
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    tag = args.tag or ("_".join(suites)[:40] + "__" + "_".join(j.name.replace(":", "-") for j in judges))
    out_path = RESULTS_DIR / f"{time.strftime('%Y%m%d-%H%M%S')}_{tag}.jsonl"

    jobs = []
    for suite in suites:
        for case in load_suite(suite, args.limit, args.seed):
            for judge in judges:
                for variant in variants:
                    for rep in range(args.reps):
                        jobs.append((suite, case, judge, variant, rep))
    print(f"{len(jobs)} judgments across {len(suites)} suite(s), {len(judges)} judge(s), {len(variants)} variant(s), {args.reps} rep(s)")

    done = 0
    errors = 0
    t0 = time.time()
    fh = out_path.open("w")
    async with httpx.AsyncClient() as client:
        async def one(suite, case, judge, variant, rep):
            nonlocal done, errors
            j = await judge_cached(judge, client, case, variant, rep, refresh=args.refresh)
            rec = j.to_dict()
            rec.pop("raw", None)
            rec.update({
                "suite": suite,
                "category": case.category,
                "labels": case.labels,
                "question_types": {q.name: q.type for q in case.questions},
                "state_chars": len(state_text(case.state)),
                "meta": {k: v for k, v in case.meta.items() if k in ("source", "subset", "error_category", "followup_class", "benchmark", "tool", "family", "named_tool", "scope", "origin", "n_options")},
            })
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
            done += 1
            if j.error:
                errors += 1
            if done % 50 == 0 or done == len(jobs):
                el = time.time() - t0
                print(f"  {done}/{len(jobs)} done, {errors} errors, {el:.0f}s", flush=True)

        await asyncio.gather(*(one(*job) for job in jobs))
    fh.close()
    print(f"wrote {out_path}")
    return out_path


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--suite", required=True, help="comma-separated suite names (files in data/cases)")
    ap.add_argument("--judge", action="append", required=True, help="jev | anthropic:<model> | openai:<model> (repeatable)")
    ap.add_argument("--variant", default="default", help="comma-separated state format variants")
    ap.add_argument("--reps", type=int, default=1)
    ap.add_argument("--limit", type=int, default=None, help="random subsample per suite")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--concurrency", type=int, default=None)
    ap.add_argument("--refresh", action="store_true", help="ignore cache")
    ap.add_argument("--tag", default=None)
    args = ap.parse_args()
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
