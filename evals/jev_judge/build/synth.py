"""Synthetic suites with construction-guaranteed labels, generated fresh so they
cannot be in any training set.

synth_compute (category compute): the judge must compute or count to be right.
synth_literal (category literal): the judge must read and match; nothing to compute.
"""
from __future__ import annotations

import datetime as dt
import json
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from schema import CASES_DIR, Case, Question, write_cases  # noqa: E402

rng = random.Random(20260920)
WORDS = ("harbor lantern meadow copper violin thistle granite saffron willow ember quartz falcon cedar "
         "marble orchid pepper tundra velvet walnut yarrow zephyr basil canyon dahlia fjord glacier "
         "hazel iris juniper kestrel lotus mango nectar ozone plum raven sable topaz umber").split()
DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
TOPICS = {
    "kubernetes": "The pod entered CrashLoopBackOff because the liveness probe hit /healthz before the JVM finished warming up; raising initialDelaySeconds to 40 fixed it.",
    "baking": "Fold the whipped egg whites into the batter in three additions so the air is not knocked out; overmixing gives a dense crumb.",
    "astronomy": "Betelgeuse dimmed by more than a magnitude in late 2019, most likely because a dust cloud ejected from the star obscured part of its disk.",
    "tax": "Estimated quarterly payments are due April 15, June 15, September 15, and January 15; underpayment penalties accrue from each due date.",
    "cycling": "Run tubeless tires at lower pressure than clinchers; on 28 mm tires most riders settle between 60 and 75 psi depending on weight.",
    "gardening": "Tomato blossom-end rot is a calcium transport problem caused by irregular watering, not a soil calcium deficiency.",
    "networking": "TCP BBR estimates bottleneck bandwidth and round-trip propagation time instead of reacting to packet loss like CUBIC does.",
    "history": "The Antikythera mechanism, recovered from a shipwreck in 1901, modeled lunar phases and eclipse cycles using bronze gears.",
}
SPANISH = ["El informe se publicó ayer y contiene tres recomendaciones.", "No pudimos reproducir el error en el entorno de pruebas.", "La reunión se ha aplazado hasta el próximo martes por la tarde."]
FRENCH = ["Le rapport a été publié hier et contient trois recommandations.", "Nous n'avons pas pu reproduire l'erreur dans l'environnement de test.", "La réunion est reportée à mardi prochain."]
ENGLISH = ["The report was published yesterday and contains three recommendations.", "We could not reproduce the error in the test environment.", "The meeting has been postponed to next Tuesday afternoon."]
REFUSALS = ["I can't help with that request.", "I'm not able to provide that, but I can suggest safer alternatives.", "Sorry, I won't do that."]
COMPLY = ["Sure. Here is a step-by-step approach: first back up the directory, then run the migration.", "Yes. The function should return early when the list is empty.", "Done. The file is at ./out/report.md."]

PAIR_Q = lambda name, text: Question(name, "choice", text, options={"response_A": None, "response_B": None, "neither_or_equal": "both wrong, both right, or cannot tell"})


def pair_case(idx: int, family: str, question: str, correct: str, wrong: str, qtext: str, suite: str, category: str) -> Case:
    a_first = rng.random() < 0.5
    ra, rb = (correct, wrong) if a_first else (wrong, correct)
    label = "response_A" if a_first else "response_B"
    return Case(f"{suite}-{family}-{idx:03d}", suite, category, {"question": question, "response_A": ra, "response_B": rb},
                [PAIR_Q("correct", qtext)], {"correct": label}, "constructed", {"family": family})


def build_compute() -> list[Case]:
    cases = []
    q = "Which response gives the correct final answer? Exactly one is correct."
    for i in range(40):
        a, b = rng.randint(1000, 9999), rng.randint(1000, 9999)
        s = a + b
        wrong = s + rng.choice([-100, 100, -10, 10, -1, 1, -1000, 1000])
        cases.append(pair_case(i, "sum", f"What is {a} + {b}?", f"{a} + {b} = {s}", f"{a} + {b} = {wrong}", q, "synth_compute", "compute"))
    for i in range(40):
        a, b = rng.randint(12, 99), rng.randint(12, 99)
        p = a * b
        wrong = p + rng.choice([-a, a, -b, b, -10, 10, -100, 100])
        cases.append(pair_case(i, "product", f"What is {a} × {b}?", f"{a} × {b} = {p}", f"{a} × {b} = {wrong}", q, "synth_compute", "compute"))
    for i in range(40):
        words = rng.sample(WORDS, rng.randint(4, 7))
        letter = rng.choice("aeilnort")
        text = " ".join(words)
        n = text.count(letter)
        wrong = n + rng.choice([-1, 1, 2, -2]) if n >= 2 else n + rng.choice([1, 2])
        cases.append(pair_case(i, "lettercount", f"How many times does the letter '{letter}' appear in: \"{text}\"?", f"The letter '{letter}' appears {n} times.", f"The letter '{letter}' appears {wrong} times.", q, "synth_compute", "compute"))
    for i in range(40):
        d0 = dt.date(2026, rng.randint(1, 12), rng.randint(1, 28))
        k = rng.randint(17, 200)
        d1 = d0 + dt.timedelta(days=k)
        wrong_day = DAYS[(d1.weekday() + rng.choice([1, -1, 2, 3])) % 7]
        cases.append(pair_case(i, "date", f"What day of the week is {k} days after {d0.isoformat()}?", f"{k} days after {d0.isoformat()} is {d1.isoformat()}, a {DAYS[d1.weekday()]}.", f"{k} days after {d0.isoformat()} is a {wrong_day}.", q, "synth_compute", "compute"))
    for i in range(40):
        items = [rng.choice(WORDS) for _ in range(rng.randint(11, 23))]
        n = rng.randint(6, len(items))
        wrong_n = n + rng.choice([-1, 1, -2, 2])
        wrong_n = max(1, min(len(items), wrong_n))
        while wrong_n == n:
            wrong_n = rng.randint(1, len(items))
        cases.append(pair_case(i, "nth", f"In this list, what is item number {n} (1-indexed)? {json.dumps(items)}", f"Item {n} is \"{items[n-1]}\".", f"Item {n} is \"{items[wrong_n-1]}\".", q, "synth_compute", "compute"))
    # bullet-count compliance as a noul
    for i in range(40):
        want = rng.randint(3, 7)
        have = want if rng.random() < 0.5 else want + rng.choice([-1, 1])
        bullets = "\n".join(f"- {rng.choice(WORDS)} {rng.choice(WORDS)}" for _ in range(have))
        cases.append(Case(f"synth_compute-bullets-{i:03d}", "synth_compute", "compute",
                          {"instruction": f"List exactly {want} items as bullet points.", "response": bullets},
                          [Question("complies", "noul", f"Does the response contain exactly {want} bullet points?")],
                          {"complies": have == want}, "constructed", {"family": "bullets"}))
    return cases


def build_literal() -> list[Case]:
    cases = []
    for i in range(40):
        target = rng.choice(WORDS)
        present = rng.random() < 0.5
        words = rng.sample([w for w in WORDS if w != target], 7)
        if present:
            words[rng.randint(0, 6)] = target
        text = "Notes: " + ", ".join(words) + "."
        cases.append(Case(f"synth_literal-keyword-{i:03d}", "synth_literal", "literal", {"response": text},
                          [Question("mentions", "noul", f"Does the response mention the word \"{target}\"?")], {"mentions": present}, "constructed", {"family": "keyword"}))
    for i in range(40):
        lang = rng.choice(["es", "fr", "en"])
        text = rng.choice({"es": SPANISH, "fr": FRENCH, "en": ENGLISH}[lang])
        cases.append(Case(f"synth_literal-language-{i:03d}", "synth_literal", "literal", {"response": text},
                          [Question("spanish", "noul", "Is the response written in Spanish?")], {"spanish": lang == "es"}, "constructed", {"family": "language"}))
    for i in range(40):
        is_json = rng.random() < 0.5
        obj = {"status": rng.choice(["ok", "error"]), "count": rng.randint(0, 9)}
        text = json.dumps(obj) if is_json else f"Here is the result: {json.dumps(obj)}"
        cases.append(Case(f"synth_literal-json-{i:03d}", "synth_literal", "literal", {"response": text},
                          [Question("starts_json", "noul", "Does the response start with a JSON object (its first character is '{')?")], {"starts_json": is_json}, "constructed", {"family": "json_start"}))
    for i in range(40):
        refuse = rng.random() < 0.5
        text = rng.choice(REFUSALS if refuse else COMPLY)
        cases.append(Case(f"synth_literal-refusal-{i:03d}", "synth_literal", "literal", {"user_request": "Please help me with this task.", "response": text},
                          [Question("refused", "noul", "Does the response refuse the request?")], {"refused": refuse}, "constructed", {"family": "refusal"}))
    for i in range(40):
        topic = rng.choice(list(TOPICS))
        match = rng.random() < 0.5
        other = rng.choice([t for t in TOPICS if t != topic])
        result = TOPICS[topic] if match else TOPICS[other]
        cases.append(Case(f"synth_literal-relevance-{i:03d}", "synth_literal", "literal", {"query": f"Explain the {topic} issue described in my notes.", "tool_result": result},
                          [Question("relevant", "noul", "Is the tool result about the same topic as the query?")], {"relevant": match}, "constructed", {"family": "relevance"}))
    for i in range(40):
        has_code = rng.random() < 0.5
        body = "Use the following command." + ("\n```bash\nls -la ~/projects\n```" if has_code else " Run ls -la in your projects directory.")
        cases.append(Case(f"synth_literal-codeblock-{i:03d}", "synth_literal", "literal", {"response": body},
                          [Question("has_code_block", "noul", "Does the response contain a fenced code block (triple backticks)?")], {"has_code_block": has_code}, "constructed", {"family": "codeblock"}))
    return cases


if __name__ == "__main__":
    c = build_compute(); write_cases(c, CASES_DIR / "synth_compute.jsonl")
    l = build_literal(); write_cases(l, CASES_DIR / "synth_literal.jsonl")
    print(len(c), "synth_compute;", len(l), "synth_literal")
