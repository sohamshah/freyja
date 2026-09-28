"""LLMBar (princeton-nlp/LLMBar) -> suites `llmbar_natural`, `llmbar_adversarial`.

Pairwise instruction-following with objective labels. Each item: input, output_1,
output_2, label in {1, 2}. Normalized to a Choice with a null option.

Run: .venv/bin/python evals/jev_judge/build/llmbar.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import RAW_DIR, Case, download, gh_raw, write_suite  # noqa: E402
from schema import Question  # noqa: E402

REPO = "princeton-nlp/LLMBar"
SUBSETS = {
    "Natural": "Dataset/LLMBar/Natural/dataset.json",
    "Neighbor": "Dataset/LLMBar/Adversarial/Neighbor/dataset.json",
    "GPTInst": "Dataset/LLMBar/Adversarial/GPTInst/dataset.json",
    "GPTOut": "Dataset/LLMBar/Adversarial/GPTOut/dataset.json",
    "Manual": "Dataset/LLMBar/Adversarial/Manual/dataset.json",
}
QUESTION = Question(
    name="better",
    type="choice",
    instructions=(
        "Which output follows the instruction better? Judge only whether the output does what "
        "the instruction asks; ignore length, style, and confidence. Pick neither_or_equal only "
        "if both are equally good or both fail."
    ),
    options={
        "output_1": "output_1 follows the instruction better",
        "output_2": "output_2 follows the instruction better",
        "neither_or_equal": "both equally good, or both fail",
    },
)
SOURCE = {
    "name": "LLMBar",
    "license": "MIT (github.com/princeton-nlp/LLMBar LICENSE)",
    "paper": "Zeng et al. 2023, arXiv:2310.07641",
    "availability": "fully available; all 100 Natural + 319 Adversarial items used",
    "urls": [gh_raw(REPO, p) for p in SUBSETS.values()],
}


def build_cases(suite: str, subsets: list[str]) -> list[Case]:
    raw = RAW_DIR / "llmbar"
    cases: list[Case] = []
    for subset in subsets:
        path = download(gh_raw(REPO, SUBSETS[subset]), raw / f"{subset}.json")
        items = json.loads(path.read_text())
        for i, it in enumerate(items):
            label = int(it["label"])
            assert label in (1, 2), it
            cases.append(Case(
                id=f"{suite}-{len(cases) + 1:04d}",
                suite=suite,
                category="literal",
                state={
                    "instruction": it["input"],
                    "output_1": it["output_1"],
                    "output_2": it["output_2"],
                },
                questions=[QUESTION],
                labels={"better": f"output_{label}"},
                label_source="llmbar_objective_label",
                meta={"source": "LLMBar", "subset": subset, "index_in_subset": i},
            ))
    return cases


def main() -> None:
    nat = build_cases("llmbar_natural", ["Natural"])
    write_suite("llmbar_natural", nat, "better", source=SOURCE)
    adv = build_cases("llmbar_adversarial", ["Neighbor", "GPTInst", "GPTOut", "Manual"])
    write_suite("llmbar_adversarial", adv, "better", source=SOURCE)


if __name__ == "__main__":
    main()
