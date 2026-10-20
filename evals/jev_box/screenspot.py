"""ScreenSpot-v2 GUI grounding with Jev Box questions.

Metric is the benchmark's: the predicted click point (box centre) falls inside
the ground-truth element box. Results are cached per (model, variant, item) so
re-runs are free.

Usage:
  uv run python evals/jev_box/screenspot.py --data ~/work/eval-benchmark-data/screenspot_v2 \
      --subset desktop,web,mobile --variant plain --limit 0 --workers 8
  uv run python evals/jev_box/screenspot.py ... --report
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent))
import api  # noqa: E402

HERE = Path(__file__).resolve().parent
CACHE = HERE / "data" / "screenspot_cache.jsonl"

PROMPTS = {
    "plain": "the element in `screen` to click in order to: {instruction}",
    "ui": "the user interface control in `screen` (button, icon, link, field, or menu item) that a person would click to {instruction}",
}


def load_items(data: Path, subsets: list[str]) -> list[dict]:
    items = []
    for s in subsets:
        for i, it in enumerate(json.load(open(data / f"screenspot_{s}_v2.json"))):
            it = dict(it)
            it["subset"] = s
            it["id"] = f"{s}:{i}:{it['img_filename']}"
            items.append(it)
    return items


def cache_key(model: str, variant: str, item_id: str) -> str:
    return hashlib.sha1(f"{model}|{variant}|{item_id}".encode()).hexdigest()


def read_cache() -> dict:
    out = {}
    if CACHE.exists():
        for line in CACHE.read_text().splitlines():
            if line.strip():
                r = json.loads(line)
                out[r["key"]] = r
    return out


def append_cache(rec: dict) -> None:
    CACHE.parent.mkdir(parents=True, exist_ok=True)
    with CACHE.open("a") as fh:
        fh.write(json.dumps(rec) + "\n")


def crop_window(src: Image.Image, cx: float, cy: float, frac: float) -> tuple[int, int, int, int]:
    """A crop of `frac` of each source dimension centred on (cx, cy) fractions, clamped."""
    W, H = src.size
    cw, ch = max(api.MIN_SIDE, round(W * frac)), max(api.MIN_SIDE, round(H * frac))
    x0 = min(max(0, round(cx * W - cw / 2)), max(0, W - cw))
    y0 = min(max(0, round(cy * H - ch / 2)), max(0, H - ch))
    return (x0, y0, min(W, x0 + cw), min(H, y0 + ch))


def run_item(item: dict, img_dir: Path, model: str, variant: str) -> dict:
    src = Image.open(img_dir / item["img_filename"])
    W, H = src.size
    two_stage = variant.endswith("+crop")
    prompt_name = variant.replace("+crop", "")
    instr = PROMPTS[prompt_name].format(instruction=item["instruction"].strip().rstrip("."))

    prep = api.prepare(src)
    ref, art = api.asset(prep.png)
    t0 = time.perf_counter()
    res = api.ask({"screen": ref}, {"target": {"type": "box", "instructions": instr}}, {ref: art}, model=model)
    ans = res.answers["target"]
    cx, cy = api.box_center(ans)
    stage1 = {"box": ans["box"], "confidence": ans.get("confidence"), "usage": res.usage, "latency_ms": res.latency_ms}
    stage2 = None
    if two_stage:
        x0, y0, x1, y1 = crop_window(src, cx, cy, 0.4)
        crop = src.crop((x0, y0, x1, y1))
        prep2 = api.prepare(crop)
        ref2, art2 = api.asset(prep2.png)
        res2 = api.ask({"screen": ref2}, {"target": {"type": "box", "instructions": instr}}, {ref2: art2}, model=model)
        a2 = res2.answers["target"]
        b = a2["box"]
        # map crop fractions back to full-image fractions
        box = {
            "left": (x0 + b["left"] * (x1 - x0)) / W,
            "right": (x0 + b["right"] * (x1 - x0)) / W,
            "top": (y0 + b["top"] * (y1 - y0)) / H,
            "bottom": (y0 + b["bottom"] * (y1 - y0)) / H,
        }
        stage2 = {"box": box, "confidence": a2.get("confidence"), "usage": res2.usage, "latency_ms": res2.latency_ms, "crop": [x0, y0, x1, y1]}
        cx, cy = (box["left"] + box["right"]) / 2, (box["top"] + box["bottom"]) / 2
    total_ms = (time.perf_counter() - t0) * 1000

    bx, by, bw, bh = item["bbox"]
    px, py = cx * W, cy * H
    hit = bx <= px <= bx + bw and by <= py <= by + bh
    final_box = (stage2 or stage1)["box"]
    pl, pt, pr, pb = final_box["left"] * W, final_box["top"] * H, final_box["right"] * W, final_box["bottom"] * H
    ix = max(0.0, min(pr, bx + bw) - max(pl, bx))
    iy = max(0.0, min(pb, by + bh) - max(pt, by))
    inter = ix * iy
    union = (pr - pl) * (pb - pt) + bw * bh - inter
    return {
        "key": cache_key(model, variant, item["id"]),
        "id": item["id"],
        "subset": item["subset"],
        "data_type": item["data_type"],
        "data_source": item["data_source"],
        "instruction": item["instruction"],
        "model": model,
        "variant": variant,
        "img_size": [W, H],
        "scale": prep.scale,
        "gt_bbox": item["bbox"],
        "gt_area_frac": (bw * bh) / (W * H),
        "pred_center": [px, py],
        "hit": hit,
        "iou": inter / union if union else 0.0,
        "confidence": (stage2 or stage1)["confidence"],
        "stage1": stage1,
        "stage2": stage2,
        "total_ms": total_ms,
    }


def report(recs: list[dict]) -> None:
    def summarize(rows: list[dict]) -> str:
        if not rows:
            return "-"
        acc = sum(r["hit"] for r in rows) / len(rows)
        return f"{acc*100:5.1f}% (n={len(rows)})"

    groups = defaultdict(list)
    for r in recs:
        groups[(r["model"], r["variant"])].append(r)
    for (model, variant), rows in sorted(groups.items()):
        print(f"\n== {model} / {variant}: overall {summarize(rows)}")
        by = defaultdict(list)
        for r in rows:
            by[(r["subset"], r["data_type"])].append(r)
        for k in sorted(by):
            print(f"   {k[0]:8s} {k[1]:5s} {summarize(by[k])}")
        bys = defaultdict(list)
        for r in rows:
            bys[r["subset"]].append(r)
        for k in sorted(bys):
            print(f"   {k:8s} all   {summarize(bys[k])}")
        # size buckets: GT area as fraction of image
        buckets = [(0, 0.0005), (0.0005, 0.002), (0.002, 0.01), (0.01, 1.0)]
        for lo, hi in buckets:
            sel = [r for r in rows if lo <= r["gt_area_frac"] < hi]
            print(f"   target area [{lo:.4f},{hi:.4f}) {summarize(sel)}")
        # confidence
        for t in (0.9, 0.7, 0.5, 0.3):
            sel = [r for r in rows if (r["confidence"] or 0) >= t]
            print(f"   conf>={t}: coverage {len(sel)/len(rows)*100:5.1f}%  precision {summarize(sel)}")
        hits_c = [r["confidence"] or 0 for r in rows if r["hit"]]
        miss_c = [r["confidence"] or 0 for r in rows if not r["hit"]]
        if hits_c and miss_c:
            pairs = sum(1 for h in hits_c for m in miss_c if h > m) + 0.5 * sum(1 for h in hits_c for m in miss_c if h == m)
            print(f"   AUROC(confidence→hit) {pairs/(len(hits_c)*len(miss_c)):.3f}   mean conf hit {sum(hits_c)/len(hits_c):.2f} miss {sum(miss_c)/len(miss_c):.2f}")
        toks = [r["stage1"]["usage"].get("input_tokens", 0) + ((r["stage2"] or {}).get("usage", {}).get("input_tokens", 0)) for r in rows]
        lat = sorted(r["total_ms"] for r in rows)
        print(f"   tokens/item mean {sum(toks)/len(toks):.0f}  cost/item ${sum(toks)/len(toks)*0.042/1e6:.6f}  latency p50 {lat[len(lat)//2]:.0f} ms p90 {lat[int(len(lat)*0.9)]:.0f} ms")


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--data", default=str(Path.home() / "work/eval-benchmark-data/screenspot_v2"))
    p.add_argument("--subset", default="desktop,web,mobile")
    p.add_argument("--variant", default="plain")
    p.add_argument("--model", default="jev-1.14.0")
    p.add_argument("--limit", type=int, default=0)
    p.add_argument("--workers", type=int, default=8)
    p.add_argument("--report", action="store_true")
    a = p.parse_args()

    data = Path(a.data).expanduser()
    img_dir = data / "screenspotv2_image"
    subsets = a.subset.split(",")
    items = load_items(data, subsets)
    if a.limit:
        # deterministic spread across subsets
        per = max(1, a.limit // len(subsets))
        items = [it for s in subsets for it in [i for i in items if i["subset"] == s][:per]]
    cache = read_cache()
    variants = a.variant.split(",")
    if not a.report:
        todo = [(it, v) for v in variants for it in items if cache_key(a.model, v, it["id"]) not in cache]
        print(f"{len(items)} items × {len(variants)} variants; {len(todo)} to run")
        done = 0
        with ThreadPoolExecutor(max_workers=a.workers) as ex:
            futs = {ex.submit(run_item, it, img_dir, a.model, v): (it, v) for it, v in todo}
            for f in as_completed(futs):
                it, v = futs[f]
                try:
                    rec = f.result()
                except Exception as e:  # keep going; report failures
                    print(f"  FAIL {it['id']} {v}: {e}")
                    continue
                append_cache(rec)
                cache[rec["key"]] = rec
                done += 1
                if done % 50 == 0:
                    print(f"  {done}/{len(todo)}")
    recs = [cache[cache_key(a.model, v, it["id"])] for v in variants for it in items if cache_key(a.model, v, it["id"]) in cache]
    report(recs)


if __name__ == "__main__":
    main()
