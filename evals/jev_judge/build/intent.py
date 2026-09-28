"""Hard many-way intent classification: CLINC150 (150 intents + out-of-scope) and
Banking77 (77 fine-grained banking intents). One Choice question per case with
every intent as an option plus a null option.

Both datasets are public (2019–2020) and may be in Jev's training data; they are
included because they are the standard hard intent benchmarks and Laya's authors
reported Jev at 0.870 on Banking77, which gives a cross-check on this harness.
"""
from __future__ import annotations

import csv
import json
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _common import download, write_suite  # noqa: E402
from schema import RAW_DIR, Case, Question  # noqa: E402

SEED = 20260920
B77_URL = "https://raw.githubusercontent.com/PolyAI-LDN/task-specific-datasets/master/banking_data/test.csv"
CLINC_URL = "https://raw.githubusercontent.com/clinc/oos-eval/master/data/data_full.json"


def pretty(label: str) -> str:
    return label.replace("_", " ")


def build_banking77(n: int = 1000) -> list[Case]:
    path = RAW_DIR / "banking77" / "test.csv"
    download(B77_URL, path)
    rows = list(csv.DictReader(path.open()))
    intents = sorted({r["category"] for r in rows})
    options = {i: pretty(i) for i in intents}
    options["none_of_these"] = "the query does not match any listed intent"
    q = Question("intent", "choice",
                 "Which banking intent does the customer's message express? Pick the single best match; "
                 "pick none_of_these only if no listed intent fits.", options=options)
    rng = random.Random(SEED)
    rng.shuffle(rows)
    cases = []
    for i, r in enumerate(rows[:n]):
        cases.append(Case(f"banking77-{i:04d}", "banking77", "classification", {"customer_message": r["text"]},
                          [q], {"intent": r["category"]}, "dataset_label", {"n_options": len(options)}))
    return cases


def build_clinc150(n_in: int = 1000, n_oos: int = 300) -> list[Case]:
    path = RAW_DIR / "clinc150" / "data_full.json"
    download(CLINC_URL, path)
    d = json.loads(path.read_text())
    intents = sorted({lab for _, lab in d["test"]})
    options = {i: pretty(i) for i in intents}
    options["out_of_scope"] = "the request does not belong to any listed intent"
    q = Question("intent", "choice",
                 "Which intent does the user's utterance express? Pick the single best match; pick out_of_scope "
                 "if the utterance is not covered by any listed intent.", options=options)
    rng = random.Random(SEED)
    test = list(d["test"]); rng.shuffle(test)
    oos = list(d["oos_test"]); rng.shuffle(oos)
    cases = []
    for i, (text, lab) in enumerate(test[:n_in]):
        cases.append(Case(f"clinc150-in-{i:04d}", "clinc150", "classification", {"utterance": text}, [q],
                          {"intent": lab}, "dataset_label", {"n_options": len(options), "scope": "in"}))
    for i, (text, _) in enumerate(oos[:n_oos]):
        cases.append(Case(f"clinc150-oos-{i:04d}", "clinc150", "classification", {"utterance": text}, [q],
                          {"intent": "out_of_scope"}, "dataset_label", {"n_options": len(options), "scope": "oos"}))
    return cases


if __name__ == "__main__":
    b = build_banking77()
    write_suite("banking77", b, "intent", source={"url": B77_URL, "license": "CC-BY-4.0", "split": "test", "sampled": len(b)})
    c = build_clinc150()
    write_suite("clinc150", c, "intent", source={"url": CLINC_URL, "license": "CC-BY-3.0", "split": "test + oos_test", "sampled": len(c)})
    print(len(b), "banking77;", len(c), "clinc150")
