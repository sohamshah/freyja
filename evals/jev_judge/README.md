# Jev as a judge: accuracy on distinct cases with real outcomes

LangChain's "Jev-as-a-Judge" experiment (2026-09-18) judged five frozen agent
runs 100 times each. It measured repeatability, latency, and cost well, and
accuracy not at all: 100% on five cases is five decisions. This benchmark asks
the question that experiment left open. Across a few hundred *distinct* cases
with ground truth that does not come from an LLM, how often is Jev right, how
well do its probabilities track its error rate, and where does it stop working?

Everything lives under this directory. Nothing here is wired into Freyja's
runtime; `bridge/decisions/` (TypeSafe provider, question types) is imported
read-only.

## Hypotheses

Stated before running, so the results can confirm or refute them.

| # | Hypothesis | Where it is tested |
|---|---|---|
| H1 | On literal, local questions whose evidence is in the state (did the agent do X, is the tool result relevant, does the output follow the stated constraint), Jev's accuracy is within 5 points of a frontier LLM judge. | suites `llmbar_natural`, `freyja_literal`, `arb_*` |
| H2 | On questions that need computation, multi-step reasoning, or knowledge outside the state, Jev is far below the LLM judge and its probabilities are overconfident. | suite `judgebench` (math, coding, reasoning); `llmbar_adversarial` |
| H3 | On the holistic question "will the user accept this turn without correction", Jev is little better than the base rate; probabilities carry some ranking signal (AUROC > 0.6) but poor calibration. | suite `freyja_acceptance` |
| H4 | Jev's repeatability advantage holds: per-case variance of its Score output is at least 10× lower than an LLM judge at default sampling. | repeatability run, 30 cases × 20 repetitions |
| H5 | A p ≥ 0.9 threshold on Noul questions is safe (precision ≥ 0.95) in every suite; the same threshold on Choice/Score questions is not. | all suites, coverage/precision at threshold |
| H6 | Adding an explicit `expected_behavior` field to the state (as LangChain did) raises Jev's accuracy more than any rewording of the question. | format ablation on `freyja_*` |

## Suites

Each suite is a JSONL file in `data/cases/`. A case is one JSON object:

```json
{
  "id": "freyja_acceptance-0001",
  "suite": "freyja_acceptance",
  "category": "holistic",              // literal | holistic | compute | knowledge | safety
  "state": { ... },                    // JSON the judge sees; serialized verbatim for Jev
  "questions": [
    {"name": "accepted", "type": "noul",
     "instructions": "Will the user accept this response without asking for a correction?"}
  ],
  "labels": {"accepted": false},       // ground truth per question; noul → bool, choice → option name, score → level name
  "label_source": "user_followup",     // how the truth was obtained
  "meta": {"session": "...", "turn": 7}
}
```

Ground truth never comes from an LLM judging the same thing. Sources:

| Suite | Cases | Truth | What it tests |
|---|---|---|---|
| `freyja_acceptance` | real Freyja turns from `~/.freyja/sessions` | the user's actual next message: correction/complaint vs acceptance/continuation (LLM-classified, manually verified on a sample; the follow-up is hidden from the judge) | H3, H6 |
| `freyja_literal` | same turns, literal rubric items derived from the turn itself (used the tool the user named, touched only the files named, asked before a destructive action, answered in the requested form) | programmatic or manual labels from the trace | H1, H6 |
| `freyja_toolfail` | real tool calls before execution | `isError` of the actual result | prediction, not judgment; a control |
| `arb_*` | AgentRewardBench web-agent trajectories | expert success / side-effect / repetition labels | H1, long-state limits |
| `trail` | TRAIL agent traces | expert step-level error annotations | H2 (error localization) |
| `llmbar_natural`, `llmbar_adversarial` | LLMBar pairwise instruction-following | objective labels; adversarial split has superficially better but wrong outputs | H1 vs literal-reading failure |
| `judgebench` | JudgeBench pairs (knowledge, reasoning, math, coding) | objective correctness | H2 |
| `clinc150`, `banking77` | many-way intent classification (150 + out-of-scope; 77 fine-grained) | dataset labels | many-way Choice with a null option |
| `freyja_tasktype` | opening request of a real session → capability the turn actually used | tool calls in the session log | routing from the request alone |

## Judges

All judges receive the same `state` and the same `questions`.

- **Jev** (`jev-1.13.0`) through `bridge.decisions.provider.TypeSafeProvider`. State is sent as JSON. Questions are sent typed. Every Choice includes a null option.
- **LLM judges** (Anthropic and OpenAI through their SDKs): the state serialized as JSON, the questions listed, and a request for a JSON object with, per question, the answer and a probability. Default sampling parameters, as in the LangChain run. One cheap model and one frontier model.

Responses are cached in `data/cache/` keyed by judge, case id, format variant, and repetition index, so reruns are free and analysis is reproducible.

## Metrics

Per suite and per judge:

- accuracy of the argmax answer against the label;
- AUROC and Brier score of the probability assigned to the labeled answer (Noul and two-option Choice);
- ECE with 10 equal-width bins, with a noise floor from label-shuffled resampling;
- precision and coverage at p ≥ 0.9 and p ≥ 0.95;
- latency p50/p95 and cost per call (Jev: input tokens × $0.042/MTok; LLMs: provider list price);
- repeatability: mean per-case variance of the Score output over repetitions.

Results are written to `results/` as JSONL (raw) and `RESULTS.md` (tables).

## Layout

```
evals/jev_judge/
  README.md          this file
  schema.py          Case / Question dataclasses, JSONL io, validation
  judges.py          JevJudge, AnthropicJudge, OpenAIJudge; caching; cost
  run.py             run a suite × judge × format × repetitions
  analyze.py         metrics and tables
  build/             one script per suite that writes data/cases/<suite>.jsonl
  data/cases/        normalized cases (committed; small)
  data/raw/          downloaded sources (not committed)
  data/cache/        judge responses (not committed)
  results/           run outputs
```

Run everything from the repo root with `uv run python evals/jev_judge/run.py ...`.
