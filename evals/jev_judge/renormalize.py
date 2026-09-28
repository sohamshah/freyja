"""Re-derive normalized answers from the raw text stored in LLM cache entries
after a normalizer fix, without calling any API."""
import json, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from judges import normalize_llm_answers, parse_llm_json
from schema import CACHE_DIR, CASES_DIR, read_cases
cases = {}
for f in CASES_DIR.glob("*.jsonl"):
    for c in read_cases(f):
        cases[c.id] = c
n = changed = dropped = 0
for f in CACHE_DIR.glob("*/**/*.json"):
    if f.parts[-3] == "jev":
        continue
    d = json.loads(f.read_text())
    case = cases.get(d["case_id"])
    if not case or not isinstance(d.get("raw"), str):
        continue
    n += 1
    try:
        new = {k: {"probs": v.probs, "answer": v.answer, "extra": v.extra} for k, v in normalize_llm_answers(case, parse_llm_json(d["raw"])).items()}
    except Exception as exc:
        dropped += 1; f.unlink(); continue
    if new != d["answers"]:
        changed += 1
    d["answers"] = new
    f.write_text(json.dumps(d, ensure_ascii=False))
print(f"{n} LLM cache entries, {changed} changed, {dropped} dropped (unparseable)")
