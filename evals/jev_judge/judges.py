"""Judges: Jev (TypeSafe System One API) and LLM judges (Anthropic, OpenAI).

Every judge takes a Case and returns a Judgment with, per question, a
probability distribution over the answer set, the argmax answer, latency, and
token usage. Responses are cached on disk so reruns and re-analysis are free.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import httpx

from schema import CACHE_DIR, Case, Question, state_text

REPO_ROOT = Path(__file__).resolve().parents[2]


def load_env(path: Path = REPO_ROOT / ".env") -> dict[str, str]:
    """First occurrence of each KEY wins, matching how the bridge reads .env."""
    out: dict[str, str] = {}
    if not path.exists():
        return out
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = k.strip()
        if k and k not in out:
            out[k] = v.strip().strip('"').strip("'")
    return out


ENV = load_env()

# USD per million tokens (input, output). Edit if provider list prices change.
PRICES: dict[str, tuple[float, float]] = {
    "jev-1.13.0": (0.042, 0.0),
    "claude-haiku-4-5": (1.00, 5.00),
    "claude-sonnet-4-6": (3.00, 15.00),
    "claude-opus-4-8": (15.00, 75.00),
    "gpt-5.5": (2.50, 10.00),
    # gpt-5.6-luna: list price not confirmed; LangChain measured $0.00039/call on ~1k-token prompts
}


@dataclass
class Answer:
    probs: dict[str, float]  # option -> probability; noul uses "yes"/"no"
    answer: str              # argmax option (noul: "yes"/"no")
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class Judgment:
    judge: str
    model: str
    case_id: str
    variant: str
    rep: int
    answers: dict[str, Answer]
    latency_ms: int
    input_tokens: int | None
    output_tokens: int | None
    cost_usd: float | None
    error: str | None = None
    raw: Any = None

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        return d


def cost_for(model: str, inp: int | None, out: int | None) -> float | None:
    if model not in PRICES or inp is None:
        return None
    pi, po = PRICES[model]
    return (inp * pi + (out or 0) * po) / 1_000_000


def cache_path(judge: str, variant: str, case_id: str, rep: int) -> Path:
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", case_id)
    return CACHE_DIR / judge / variant / f"{safe}__rep{rep}.json"


def question_state(case: Case, variant: str) -> Any:
    """Apply a format variant to the state. `default` sends it as built."""
    if variant == "default":
        return case.state
    if variant == "expected":
        # LangChain's design: restate the request as an explicit expected behavior field.
        if isinstance(case.state, dict) and "user_request" in case.state:
            s = dict(case.state)
            s["expected_behavior"] = (
                "The assistant should do exactly what the user_request asks, report what it did, "
                "and not claim results that the actions do not support."
            )
            return s
        return case.state
    if variant == "text":
        return state_text(case.state)
    raise ValueError(f"unknown variant {variant}")


# --------------------------------------------------------------------------- Jev

class JevJudge:
    name = "jev"

    def __init__(self, model: str = "jev-1.13.0", concurrency: int = 8, timeout: float = 60.0):
        self.model = model
        self.key = ENV.get("TYPESAFE_AI_API_KEY") or os.environ.get("TYPESAFE_AI_API_KEY", "")
        if not self.key:
            raise RuntimeError("TYPESAFE_AI_API_KEY missing")
        self.url = "https://api.typesafe.ai/v1/systemone"
        self.sem = asyncio.Semaphore(concurrency)
        self.timeout = timeout

    @staticmethod
    def payload(q: Question) -> dict[str, Any]:
        if q.type == "noul":
            return {"type": "noul", "instructions": q.instructions}
        if q.type == "choice":
            return {"type": "choice", "instructions": q.instructions, "criteria": dict(q.options or {})}
        return {"type": "score", "instructions": q.instructions, "criteria": list(q.levels or [])}

    def parse(self, case: Case, data: dict[str, Any]) -> dict[str, Answer]:
        out: dict[str, Answer] = {}
        for q in case.questions:
            a = data["answers"][q.name]
            if q.type == "noul":
                p = float(a["noul"])
                out[q.name] = Answer({"yes": p, "no": round(1 - p, 4)}, "yes" if p >= 0.5 else "no")
            elif q.type == "choice":
                probs = {k: float(v) for k, v in a["probabilities"].items()}
                out[q.name] = Answer(probs, a["choice"], {"confidence": a.get("confidence")})
            else:
                legend = a.get("legend") or {}
                probs = {legend.get(k, k): float(v) for k, v in a["probabilities"].items()}
                best = max(probs, key=probs.get)
                out[q.name] = Answer(probs, best, {"score": a.get("score"), "confidence": a.get("confidence")})
        return out

    async def judge(self, client: httpx.AsyncClient, case: Case, variant: str, rep: int) -> Judgment:
        body = {
            "model": self.model,
            "state": question_state(case, variant),
            "questions": {q.name: self.payload(q) for q in case.questions},
        }
        async with self.sem:
            t0 = time.perf_counter()
            last_err = None
            for attempt in range(4):
                try:
                    r = await client.post(self.url, headers={"Authorization": f"Bearer {self.key}"}, json=body, timeout=self.timeout)
                    if r.status_code == 429 or r.status_code >= 500:
                        last_err = f"HTTP {r.status_code}: {r.text[:200]}"
                        await asyncio.sleep(1.5 * (attempt + 1))
                        continue
                    latency = int((time.perf_counter() - t0) * 1000)
                    if r.status_code != 200:
                        return Judgment(self.name, self.model, case.id, variant, rep, {}, latency, None, None, None, error=f"HTTP {r.status_code}: {r.text[:300]}")
                    data = r.json()
                    usage = data.get("usage", {})
                    inp, out = usage.get("input_tokens"), usage.get("output_tokens")
                    return Judgment(self.name, self.model, case.id, variant, rep, self.parse(case, data), latency, inp, out, cost_for(self.model, inp, 0), raw=data)
                except (httpx.HTTPError, KeyError, ValueError) as exc:
                    last_err = repr(exc)
                    await asyncio.sleep(1.0 * (attempt + 1))
            latency = int((time.perf_counter() - t0) * 1000)
            return Judgment(self.name, self.model, case.id, variant, rep, {}, latency, None, None, None, error=last_err)


# --------------------------------------------------------------------------- LLM judges

JUDGE_SYSTEM = (
    "You are an evaluator. You will be given a STATE (JSON) and one or more QUESTIONS about it. "
    "Answer each question using only the information in the state. Respond with a single JSON object "
    "and nothing else."
)


def llm_prompt(case: Case, variant: str) -> str:
    qs = []
    for q in case.questions:
        if q.type == "noul":
            qs.append(
                f'- "{q.name}" (yes/no): {q.instructions}\n'
                f'  Respond as {{"answer": "yes"|"no", "p_yes": <probability that the answer is yes, 0..1>}}'
            )
        elif q.type == "choice":
            opts = ", ".join(f'"{k}"' + (f" ({v})" if v else "") for k, v in (q.options or {}).items())
            qs.append(
                f'- "{q.name}" (choose one of: {opts}): {q.instructions}\n'
                f'  Respond as {{"answer": <option>, "probabilities": {{<option>: <probability>, ...}}}} with probabilities summing to 1'
            )
        else:
            lv = ", ".join(f'"{l}"' for l in (q.levels or []))
            qs.append(
                f'- "{q.name}" (ordered levels, low to high: {lv}): {q.instructions}\n'
                f'  Respond as {{"answer": <level>, "probabilities": {{<level>: <probability>, ...}}}} with probabilities summing to 1'
            )
    return (
        "STATE:\n" + state_text(question_state(case, variant)) +
        "\n\nQUESTIONS:\n" + "\n".join(qs) +
        "\n\nReturn one JSON object whose keys are the question names. Probabilities must reflect how likely each answer is to be correct."
    )


def parse_llm_json(text: str) -> dict[str, Any]:
    """Accept fenced JSON, JSON after reasoning text, or a trailing JSON object."""
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.S | re.M).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    starts = [m.start() for m in re.finditer(r"\{", text)]
    ends = [m.end() for m in re.finditer(r"\}", text)]
    for i in starts:
        for j in reversed(ends):
            if j <= i:
                break
            try:
                obj = json.loads(text[i:j])
                if isinstance(obj, dict):
                    return obj
            except json.JSONDecodeError:
                continue
    raise ValueError("no JSON object in response")


def normalize_llm_answers(case: Case, data: dict[str, Any]) -> dict[str, Answer]:
    out: dict[str, Answer] = {}
    lowered = {str(k).lower(): v for k, v in data.items()}
    for q in case.questions:
        a = data.get(q.name) or lowered.get(q.name.lower())
        if a is None and len(case.questions) == 1 and any(k in data for k in ("answer", "probabilities", "p_yes")):
            a = data  # the model skipped the question-name wrapper
        if a is None:
            raise ValueError(f"missing answer for {q.name}")
        if q.type == "noul":
            p = a.get("p_yes")
            ans = str(a.get("answer", "")).lower().strip()
            if isinstance(ans, str) and ans in ("true", "false"):
                ans = "yes" if ans == "true" else "no"
            if p is None:
                p = 0.9 if ans == "yes" else 0.1
            p = min(1.0, max(0.0, float(p)))
            # Models often report p_yes as confidence in their own answer; a "no" with p_yes 0.95 means P(yes) = 0.05.
            if ans in ("yes", "no") and (ans == "yes") != (p >= 0.5):
                p = 1.0 - p
            out[q.name] = Answer({"yes": p, "no": round(1 - p, 4)}, "yes" if p >= 0.5 else "no")
        else:
            options = list((q.options or {}).keys()) if q.type == "choice" else list(q.levels or [])
            probs_in = a.get("probabilities") or {}
            probs = {o: float(probs_in.get(o, 0.0)) for o in options}
            ans = a.get("answer")
            if sum(probs.values()) <= 0:
                probs = {o: (1.0 if o == ans else 0.0) for o in options}
            z = sum(probs.values()) or 1.0
            probs = {o: v / z for o, v in probs.items()}
            best = max(probs, key=probs.get)
            if ans not in options:
                ans = best
            out[q.name] = Answer(probs, str(ans))
    return out


class AnthropicJudge:
    def __init__(self, model: str = "claude-haiku-4-5", concurrency: int = 4, max_tokens: int = 4000):
        import anthropic  # local import keeps schema-only users dependency-free
        self.model = model
        self.name = f"anthropic:{model}"
        key = ENV.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_API_KEY")
        self.client = anthropic.AsyncAnthropic(api_key=key)
        self.sem = asyncio.Semaphore(concurrency)
        self.max_tokens = max_tokens

    async def judge(self, _client: httpx.AsyncClient, case: Case, variant: str, rep: int) -> Judgment:
        prompt = llm_prompt(case, variant)
        async with self.sem:
            t0 = time.perf_counter()
            last_err = None
            for attempt in range(4):
                try:
                    msg = await self.client.messages.create(
                        model=self.model, max_tokens=self.max_tokens, system=JUDGE_SYSTEM,
                        messages=[{"role": "user", "content": prompt}],
                    )
                    latency = int((time.perf_counter() - t0) * 1000)
                    text = "".join(getattr(b, "text", "") for b in msg.content)
                    inp, out = msg.usage.input_tokens, msg.usage.output_tokens
                    try:
                        answers = normalize_llm_answers(case, parse_llm_json(text))
                        err = None
                    except (ValueError, json.JSONDecodeError) as exc:
                        answers, err = {}, f"parse: {exc}"
                    return Judgment(self.name, self.model, case.id, variant, rep, answers, latency, inp, out, cost_for(self.model, inp, out), error=err, raw=text)
                except Exception as exc:  # rate limits, overloads
                    last_err = repr(exc)[:300]
                    await asyncio.sleep(2.0 * (attempt + 1))
            latency = int((time.perf_counter() - t0) * 1000)
            return Judgment(self.name, self.model, case.id, variant, rep, {}, latency, None, None, None, error=last_err)


class OpenAIJudge:
    def __init__(self, model: str = "gpt-5.6-luna", concurrency: int = 4, max_tokens: int = 8000):
        import openai
        self.model = model
        self.name = f"openai:{model}"
        key = ENV.get("OPENAI_API_KEY") or os.environ.get("OPENAI_API_KEY")
        self.client = openai.AsyncOpenAI(api_key=key)
        self.sem = asyncio.Semaphore(concurrency)
        self.max_tokens = max_tokens

    async def judge(self, _client: httpx.AsyncClient, case: Case, variant: str, rep: int) -> Judgment:
        prompt = llm_prompt(case, variant)
        async with self.sem:
            t0 = time.perf_counter()
            last_err = None
            for attempt in range(4):
                try:
                    resp = await self.client.chat.completions.create(
                        model=self.model,
                        messages=[{"role": "system", "content": JUDGE_SYSTEM}, {"role": "user", "content": prompt}],
                        max_completion_tokens=self.max_tokens,
                        response_format={"type": "json_object"},
                    )
                    latency = int((time.perf_counter() - t0) * 1000)
                    text = resp.choices[0].message.content or ""
                    inp = resp.usage.prompt_tokens if resp.usage else None
                    out = resp.usage.completion_tokens if resp.usage else None
                    try:
                        answers = normalize_llm_answers(case, parse_llm_json(text))
                        err = None
                    except (ValueError, json.JSONDecodeError) as exc:
                        answers, err = {}, f"parse: {exc}"
                    return Judgment(self.name, self.model, case.id, variant, rep, answers, latency, inp, out, cost_for(self.model, inp, out), error=err, raw=text)
                except Exception as exc:
                    last_err = repr(exc)[:300]
                    await asyncio.sleep(2.0 * (attempt + 1))
            latency = int((time.perf_counter() - t0) * 1000)
            return Judgment(self.name, self.model, case.id, variant, rep, {}, latency, None, None, None, error=last_err)


def make_judge(spec: str, concurrency: int | None = None):
    """`jev`, `anthropic:<model>`, or `openai:<model>`."""
    if spec == "jev":
        return JevJudge(concurrency=concurrency or 8)
    provider, _, model = spec.partition(":")
    if provider == "anthropic":
        return AnthropicJudge(model=model or "claude-haiku-4-5", concurrency=concurrency or 4)
    if provider == "openai":
        return OpenAIJudge(model=model or "gpt-5.6-luna", concurrency=concurrency or 4)
    raise ValueError(f"unknown judge spec {spec}")


async def judge_cached(judge, client: httpx.AsyncClient, case: Case, variant: str, rep: int, refresh: bool = False) -> Judgment:
    p = cache_path(judge.name, variant, case.id, rep)
    if p.exists() and not refresh:
        d = json.loads(p.read_text())
        if not d.get("error"):
            d["answers"] = {k: Answer(**v) for k, v in d["answers"].items()}
            return Judgment(**d)
    j = await judge.judge(client, case, variant, rep)
    if not j.error:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(j.to_dict(), ensure_ascii=False))
    return j
