"""Split-half temperature scaling: how much of a judge's out-of-distribution
calibration error a per-suite calibration map recovers.

    uv run python evals/jev_judge/calibrate.py results/*_all_jev.jsonl

For each (judge, suite, question): shuffle cases, fit a single temperature T
on half A by minimizing log loss of the labeled answer, apply it to half B,
and report ECE before and after, averaged over 20 random splits. T > 1 means
the raw probabilities were overconfident.
"""
from __future__ import annotations

import json
import math
import random
import statistics
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from analyze import ece, label_key, load  # noqa: E402


def logits(probs: dict[str, float]) -> dict[str, float]:
    eps = 1e-4
    return {k: math.log(min(1 - eps, max(eps, v))) for k, v in probs.items()}


def apply_T(lg: dict[str, float], T: float) -> dict[str, float]:
    z = {k: v / T for k, v in lg.items()}
    m = max(z.values())
    den = sum(math.exp(v - m) for v in z.values())
    return {k: math.exp(v - m) / den for k, v in z.items()}


def fit_T(items: list[tuple[dict[str, float], str]]) -> float:
    best, best_ll = 1.0, float("inf")
    for T in [0.3 + 0.05 * i for i in range(0, 95)]:
        ll = 0.0
        for lg, truth in items:
            p = apply_T(lg, T)
            ll -= math.log(max(1e-6, p.get(truth, 1e-6)))
        if ll < best_ll:
            best, best_ll = T, ll
    return best


def main() -> None:
    rows = load(sys.argv[1:])
    groups: dict[tuple, list] = defaultdict(list)
    for r in rows:
        if r.get("error") or not r.get("answers"):
            continue
        for q, ans in r["answers"].items():
            if q not in r["labels"]:
                continue
            qtype = r["question_types"][q]
            truth = label_key(qtype, r["labels"][q])
            probs = ans["probs"]
            if truth not in probs:
                probs = {**probs, truth: 0.0}
            groups[(r["judge"], r["suite"], q)].append((logits(probs), truth))
    print("| judge | suite | question | n | ECE raw | ECE after T (held-out) | median T | acc raw | acc after |")
    print("|---|---|---|---|---|---|---|---|---|")
    for (judge, suite, q), items in sorted(groups.items()):
        if len(items) < 60:
            continue
        rng = random.Random(1)
        raws, afters, Ts, acc_r, acc_a = [], [], [], [], []
        for _ in range(20):
            idx = list(range(len(items)))
            rng.shuffle(idx)
            half = len(idx) // 2
            A = [items[i] for i in idx[:half]]
            B = [items[i] for i in idx[half:]]
            T = fit_T(A)
            Ts.append(T)
            conf_r, cor_r, conf_a, cor_a = [], [], [], []
            for lg, truth in B:
                p0 = apply_T(lg, 1.0)
                p1 = apply_T(lg, T)
                conf_r.append(max(p0.values())); cor_r.append(max(p0, key=p0.get) == truth)
                conf_a.append(max(p1.values())); cor_a.append(max(p1, key=p1.get) == truth)
            raws.append(ece(conf_r, cor_r)); afters.append(ece(conf_a, cor_a))
            acc_r.append(sum(cor_r) / len(cor_r)); acc_a.append(sum(cor_a) / len(cor_a))
        print(f"| {judge} | {suite} | {q} | {len(items)} | {statistics.fmean(raws):.3f} | {statistics.fmean(afters):.3f} | {statistics.median(Ts):.2f} | {statistics.fmean(acc_r):.3f} | {statistics.fmean(acc_a):.3f} |")


if __name__ == "__main__":
    main()
