"""JudgeBench (ScalerLab/JudgeBench) -> suite `judgebench`.

Pairs of GPT-4o responses to knowledge / reasoning / math / coding questions where
exactly one response is objectively correct. Label "A>B" or "B>A".

Run: .venv/bin/python evals/jev_judge/build/judgebench.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import RAW_DIR, Case, download, gh_raw, write_suite  # noqa: E402
from schema import Question  # noqa: E402

REPO = "ScalerLab/JudgeBench"
FILE = "data/dataset=judgebench,response_model=gpt-4o-2024-05-13.jsonl"
CATEGORY_BY_SOURCE = {
    "mmlu-pro": "knowledge",
    "livebench-reasoning": "compute",
    "livebench-math": "compute",
    "livecodebench": "compute",
}
QUESTION = Question(
    name="correct",
    type="choice",
    instructions="Which response reaches the correct final answer? Exactly one of them is correct.",
    options={
        "response_A": "response_A reaches the correct final answer",
        "response_B": "response_B reaches the correct final answer",
        "neither_or_equal": "neither, or cannot tell",
    },
)
SOURCE = {
    "name": "JudgeBench",
    "license": "no LICENSE file in the GitHub repo (research artifact; underlying items from MMLU-Pro (MIT), "
               "LiveBench (Apache-2.0), LiveCodeBench (CC-BY-4.0))",
    "paper": "Tan et al. 2024, arXiv:2410.12784",
    "availability": "fully available; all 350 gpt-4o pairs used (claude-3.5-sonnet file not used)",
    "urls": [gh_raw(REPO, FILE)],
}


def source_family(source: str) -> str:
    for prefix in CATEGORY_BY_SOURCE:
        if source.startswith(prefix):
            return prefix
    raise ValueError(f"unknown JudgeBench source {source!r}")


def main() -> None:
    path = download(gh_raw(REPO, FILE), RAW_DIR / "judgebench" / "dataset=gpt-4o-2024-05-13.jsonl")
    rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    cases: list[Case] = []
    for r in rows:
        family = source_family(r["source"])
        label = {"A>B": "response_A", "B>A": "response_B"}[r["label"]]
        cases.append(Case(
            id=f"judgebench-{len(cases) + 1:04d}",
            suite="judgebench",
            category=CATEGORY_BY_SOURCE[family],
            state={
                "question": r["question"],
                "response_A": r["response_A"],
                "response_B": r["response_B"],
            },
            questions=[QUESTION],
            labels={"correct": label},
            label_source="judgebench_objective_correctness",
            meta={
                "source": r["source"],
                "source_family": family,
                "pair_id": r["pair_id"],
                "original_id": r["original_id"],
                "response_model": r["response_model"],
            },
        ))
    write_suite("judgebench", cases, "correct", source=SOURCE)
    from collections import Counter
    print("  by category:", dict(Counter(c.category for c in cases)))
    print("  by source family:", dict(Counter(c.meta["source_family"] for c in cases)))


if __name__ == "__main__":
    main()
