"""Metrics over result files.

    uv run python evals/jev_judge/analyze.py results/*.jsonl
    uv run python evals/jev_judge/analyze.py results/x.jsonl --by category --threshold 0.9
    uv run python evals/jev_judge/analyze.py results/repeat.jsonl --repeat

Per (judge, suite[, group], question): n, accuracy, base rate, AUROC (binary),
Brier, ECE (10 bins) with a finite-sample noise floor, precision/coverage at a
probability threshold, latency p50/p95, cost per call. `--repeat` reports the
per-case variance of the probability across repetitions.
"""
from __future__ import annotations

import argparse
import json
import math
import random
import statistics
import sys
from collections import defaultdict
from pathlib import Path


def load(paths: list[str]) -> list[dict]:
    rows = []
    for p in paths:
        for path in sorted(Path().glob(p)) if any(ch in p for ch in "*?[") else [Path(p)]:
            with path.open() as fh:
                for line in fh:
                    if line.strip():
                        rows.append(json.loads(line))
    return rows


def label_key(qtype: str, label) -> str:
    if qtype == "noul":
        return "yes" if label else "no"
    return str(label)


def ece(conf: list[float], correct: list[bool], bins: int = 10) -> float:
    n = len(conf)
    if n == 0:
        return float("nan")
    sums = [0.0] * bins
    hits = [0] * bins
    cnt = [0] * bins
    for c, ok in zip(conf, correct):
        b = min(bins - 1, max(0, int(math.ceil(c * bins)) - 1))
        sums[b] += c
        hits[b] += 1 if ok else 0
        cnt[b] += 1
    return sum(cnt[b] / n * abs(hits[b] / cnt[b] - sums[b] / cnt[b]) for b in range(bins) if cnt[b])


def ece_noise_floor(conf: list[float], draws: int = 60, seed: int = 0) -> float:
    """ECE a perfectly calibrated model would show on this many samples: simulate
    outcomes from the stated probabilities and average the resulting ECE."""
    rng = random.Random(seed)
    vals = []
    for _ in range(draws):
        sim = [rng.random() < c for c in conf]
        vals.append(ece(conf, sim))
    return statistics.fmean(vals) if vals else float("nan")


def auroc(scores: list[float], labels: list[bool]) -> float:
    pos = [s for s, l in zip(scores, labels) if l]
    neg = [s for s, l in zip(scores, labels) if not l]
    if not pos or not neg:
        return float("nan")
    # rank-based with ties = 0.5
    wins = 0.0
    for p in pos:
        for q in neg:
            wins += 1.0 if p > q else 0.5 if p == q else 0.0
    return wins / (len(pos) * len(neg))


def pct(xs: list[float], q: float) -> float:
    if not xs:
        return float("nan")
    xs = sorted(xs)
    k = min(len(xs) - 1, max(0, int(round(q * (len(xs) - 1)))))
    return xs[k]


def fmt(x, nd=3):
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return "—"
    if isinstance(x, float):
        return f"{x:.{nd}f}"
    return str(x)


def summarize(rows: list[dict], by: str | None, threshold: float) -> list[dict]:
    groups: dict[tuple, list[dict]] = defaultdict(list)
    for r in rows:
        if r.get("error") or not r.get("answers"):
            groups[(r["judge"] + "@" + r.get("variant", "default"), r["suite"], r.get(by, "") if by else "", "_errors")].append(r)
            continue
        for qname, ans in r["answers"].items():
            if qname not in r["labels"]:
                continue
            g = r["judge"] + "@" + r.get("variant", "default"), r["suite"], (r.get(by) if by in r else r.get("meta", {}).get(by, "")) if by else "", qname
            groups[g].append({**r, "_q": qname, "_ans": ans})
    out = []
    for (judge, suite, grp, qname), rs in sorted(groups.items()):
        if qname == "_errors":
            out.append({"judge": judge, "suite": suite, "group": grp, "question": "(errors)", "n": len(rs)})
            continue
        # one row per case (first rep) for accuracy-type metrics
        first = {}
        for r in rs:
            first.setdefault(r["case_id"], r)
        rs1 = list(first.values())
        correct, conf, p_yes, is_yes, brier = [], [], [], [], []
        for r in rs1:
            qtype = r["question_types"][r["_q"]]
            truth = label_key(qtype, r["labels"][r["_q"]])
            probs = r["_ans"]["probs"]
            pred = r["_ans"]["answer"]
            ok = pred == truth
            correct.append(ok)
            conf.append(max(probs.values()) if probs else 0.0)
            if qtype == "noul":
                p_yes.append(probs.get("yes", 0.0)); is_yes.append(truth == "yes")
            # multi-class Brier
            brier.append(sum((probs.get(o, 0.0) - (1.0 if o == truth else 0.0)) ** 2 for o in set(probs) | {truth}))
        n = len(rs1)
        acc = sum(correct) / n if n else float("nan")
        base = None
        if p_yes:
            base = sum(is_yes) / n
        else:
            cnt = defaultdict(int)
            for r in rs1:
                cnt[label_key(r["question_types"][r["_q"]], r["labels"][r["_q"]])] += 1
            base = max(cnt.values()) / n if n else float("nan")
        sel = [i for i, c in enumerate(conf) if c >= threshold]
        prec = sum(correct[i] for i in sel) / len(sel) if sel else float("nan")
        lat = [r["latency_ms"] for r in rs if r.get("latency_ms") is not None]
        cost = [r["cost_usd"] for r in rs if r.get("cost_usd") is not None]
        out.append({
            "judge": judge, "suite": suite, "group": grp, "question": qname, "n": n,
            "acc": acc, "base": base,
            "auroc": auroc(p_yes, is_yes) if p_yes else float("nan"),
            "brier": statistics.fmean(brier) if brier else float("nan"),
            "ece": ece(conf, correct), "ece_floor": ece_noise_floor(conf),
            "cov@t": len(sel) / n if n else float("nan"), "prec@t": prec,
            "lat_p50": pct(lat, 0.5), "lat_p95": pct(lat, 0.95),
            "cost": statistics.fmean(cost) if cost else None,
            "chars_p50": pct([r["state_chars"] for r in rs1], 0.5),
        })
    return out


def table(rows: list[dict], threshold: float) -> str:
    cols = ["judge", "suite", "group", "question", "n", "acc", "base", "auroc", "brier", "ece", "ece_floor", f"cov@{threshold}", f"prec@{threshold}", "lat_p50", "lat_p95", "cost", "chars_p50"]
    keymap = {f"cov@{threshold}": "cov@t", f"prec@{threshold}": "prec@t"}
    lines = ["| " + " | ".join(cols) + " |", "|" + "---|" * len(cols)]
    for r in rows:
        vals = []
        for c in cols:
            k = keymap.get(c, c)
            v = r.get(k)
            if k in ("lat_p50", "lat_p95"):
                vals.append(fmt(v, 0))
            elif k == "cost":
                vals.append("—" if v is None else f"${v:.5f}")
            elif k == "chars_p50":
                vals.append(fmt(v, 0))
            else:
                vals.append(fmt(v))
        lines.append("| " + " | ".join(vals) + " |")
    return "\n".join(lines)


def repeatability(rows: list[dict]) -> str:
    per = defaultdict(lambda: defaultdict(list))  # (judge, suite, q) -> case -> [p]
    for r in rows:
        if r.get("error") or not r.get("answers"):
            continue
        for q, ans in r["answers"].items():
            qtype = r["question_types"][q]
            probs = ans["probs"]
            if qtype == "noul":
                p = probs.get("yes", 0.0)
            elif qtype == "score":
                levels = list(probs.keys())
                p = sum(i * probs[l] for i, l in enumerate(levels)) / max(1, len(levels) - 1)
            else:
                p = max(probs.values())
            per[(r["judge"] + "@" + r.get("variant", "default"), r["suite"], q)][r["case_id"]].append(p)
    lines = ["| judge | suite | question | cases | reps | mean var | mean sd | argmax flip rate |", "|---|---|---|---|---|---|---|---|"]
    flips = defaultdict(list)
    for r in rows:
        if r.get("error") or not r.get("answers"):
            continue
        for q, ans in r["answers"].items():
            flips[(r["judge"] + "@" + r.get("variant", "default"), r["suite"], q, r["case_id"])].append(ans["answer"])
    for (judge, suite, q), cases in sorted(per.items()):
        vs = [statistics.pvariance(ps) for ps in cases.values() if len(ps) > 1]
        sds = [statistics.pstdev(ps) for ps in cases.values() if len(ps) > 1]
        reps = statistics.median(len(ps) for ps in cases.values())
        fl = []
        for cid, ps in cases.items():
            answers = flips[(judge, suite, q, cid)]
            maj = max(set(answers), key=answers.count)
            fl.append(sum(a != maj for a in answers) / len(answers))
        lines.append(f"| {judge} | {suite} | {q} | {len(cases)} | {int(reps)} | {statistics.fmean(vs) if vs else float('nan'):.6f} | {statistics.fmean(sds) if sds else float('nan'):.4f} | {statistics.fmean(fl):.3f} |")
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("paths", nargs="+")
    ap.add_argument("--by", default=None, help="extra grouping key: category, or a meta key (source, subset, error_category, benchmark, followup_class)")
    ap.add_argument("--threshold", type=float, default=0.9)
    ap.add_argument("--repeat", action="store_true")
    ap.add_argument("--json", default=None, help="also write summary rows to this JSON file")
    args = ap.parse_args()
    rows = load(args.paths)
    if args.repeat:
        print(repeatability(rows))
        return
    summary = summarize(rows, args.by, args.threshold)
    print(table(summary, args.threshold))
    if args.json:
        Path(args.json).write_text(json.dumps(summary, indent=1))


if __name__ == "__main__":
    main()
