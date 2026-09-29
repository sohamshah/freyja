# Results: Jev as a judge on 3,600 distinct cases

Run 2026-09-20. Judges: Jev `jev-1.13.0`; Claude Haiku 4.5 (`anthropic:claude-haiku-4-5`, a
cheap non-reasoning LLM); GPT-5.6 Luna (`openai:gpt-5.6-luna`, the cheap reasoning
model LangChain used). All three saw identical states and questions, at default sampling.
Raw judgments are in `results/*.jsonl`; every number below is reproducible from the cache
with `analyze.py`, `calibrate.py`, and the snippets in this file's appendix.

## Setup

3,171 cases in 10 suites plus 480 synthetic cases and derived suites (order-swapped
copies, a 30-case repeatability set, a 7-question decomposition of the acceptance
suite). Ground truth never comes from an LLM answering the same question:

| suite | n | truth | category |
|---|---|---|---|
| `freyja_acceptance` | 623 | the user's actual next message in a real Freyja session, classified as correction vs acceptance (Sonnet-labeled; 50-case manual audit found ~46 clean, ~4 "refine further" borderlines) | holistic |
| `freyja_literal/paths_grounded` | 168 | every path named in the final message appears in the turn's tool calls | literal |
| `freyja_literal/tool_error_occurred` | 150 | any `isError` in the turn (flags removed from the state; judge reads result text) | literal |
| `freyja_toolfail` | 200 | `isError` of the *next* tool call, before execution (a prediction control) | literal |
| `arb_success / side_effect / repetition` | 299 / 298 / 298 | AgentRewardBench expert annotations of web-agent trajectories (text-only, first 2 + last 8 steps) | holistic / literal / literal |
| `trail_error` | 300 | TRAIL expert step-level error annotations on GAIA/SWE-bench agent traces | compute |
| `llmbar_natural / adversarial` | 100 / 319 | LLMBar objective instruction-following labels (pairwise) | literal |
| `judgebench` | 350 | JudgeBench objective correctness (pairwise; MMLU-Pro, LiveBench reasoning/math, LiveCodeBench) | knowledge / compute |
| `synth_literal` | 240 | constructed today: keyword presence, language, JSON start, refusal, relevance, code block | literal |
| `synth_compute` | 240 | constructed today: 4-digit sums, 2-digit products, letter counts, day-of-week after N days, nth list item, exact bullet count | compute |

Excluded: `freyja_literal/named_tool_used` (65 cases). Its labels mapped user phrasing to
tool names too loosely ("commit" labeled as `git_commit` when bash was used; "kanban
tickets" labeled positive when the assistant explicitly declined). Kept on disk, not scored.

Total spend: Jev about $0.35 for 6,000+ judgments; Haiku about $8; Luna not priced
(list price unknown; LangChain measured $0.00039/call on shorter prompts).

## Headline table

acc = argmax accuracy; AUROC on P(yes) for yes/no questions; ECE with 10 bins; "floor" is
the ECE a perfectly calibrated model would show at this sample size; cov,prec@.9 = share
of cases the judge answered with probability ≥ 0.9, and accuracy on those.

| suite / question | n | acc Jev / Haiku / Luna | AUROC Jev / Haiku / Luna | ECE Jev (floor) / Haiku / Luna | cov,prec@.9 Jev | Haiku | Luna |
|---|---|---|---|---|---|---|---|
| synth_literal (6 families) | 240 | 0.98 / 0.99 / 0.98 | 0.99 / 1.00 / 0.98 | 0.04 (0.03) / 0.02 / 0.02 | 0.87,0.99 | 0.97,0.99 | 0.99,0.98 |
| llmbar_natural / better | 100 | 0.93 / 0.88 / 0.93 | — | 0.06 (0.06) / 0.08 / 0.07 | 0.66,1.00 | 0.34,0.97 | 0.80,0.96 |
| llmbar_adversarial / better | 319 | 0.78 / 0.71 / 0.83 | — | 0.05 (0.04) / 0.13 / 0.10 | 0.48,0.93 | 0.38,0.79 | 0.83,0.88 |
| judgebench / correct | 350 | 0.76 / 0.68 / 0.86 | — | 0.07 (0.05) / 0.15 / 0.10 | 0.21,0.92 | 0.53,0.88 | 0.91,0.90 |
| synth_compute / correct (pairs) | 200 | 0.80 / 0.71 / 0.96 | — | 0.07 (0.05) / 0.28 / 0.04 | 0.41,0.99 | 0.99,0.72 | 1.00,0.96 |
| synth_compute / bullets | 40 | 1.00 / 1.00 / 1.00 | 1.00 / 1.00 / 1.00 | — | — | — | — |
| arb_repetition / repetition | 298 | 0.77 / 0.76 / 0.82 | 0.90 / 0.88 / 0.88 | 0.10 (0.04) / 0.15 / 0.16 | 0.60,0.88 | 0.59,0.87 | 0.96,0.83 |
| arb_side_effect / side_effect | 298 | 0.73 / 0.74 / 0.74 (majority 0.72) | 0.68 / 0.68 / 0.63 | 0.07 (0.04) / 0.16 / 0.24 | 0.14,0.84 | 0.53,0.76 | 0.98,0.75 |
| arb_success / success | 299 | 0.69 / 0.69 / 0.75 (majority 0.62) | 0.84 / 0.78 / 0.78 | 0.15 (0.04) / 0.20 / 0.22 | 0.52,0.87 | 0.45,0.89 | 0.87,0.76 |
| trail_error / contains_error | 300 | 0.70 / 0.65 / 0.63 | 0.76 / 0.68 / 0.68 | 0.05 (0.04) / 0.23 / 0.31 | 0.12,0.89 | 0.32,0.76 | 0.85,0.67 |
| freyja_literal / paths_grounded | 168 | 0.74 / 0.76 / 0.77 | 0.86 / 0.79 / 0.85 | 0.06 (0.06) / 0.15 / 0.21 | 0.16,0.96 | 0.65,0.85 | 0.95,0.79 |
| freyja_literal / tool_error_occurred | 150 | 0.77 / 0.77 / 0.75 | 0.91 / 0.81 / 0.80 | 0.13 (0.04) / 0.17 / 0.23 | 0.69,0.90 | 0.87,0.78 | 0.99,0.76 |
| freyja_toolfail / will_fail | 200 | 0.56 / 0.59 / 0.61 | 0.78 / 0.67 / 0.76 | 0.21 (0.05) / 0.26 / 0.31 | 0.14,0.85 | 0.23,0.78 | 0.79,0.65 |
| freyja_acceptance / accepted | 623 | 0.63 / 0.58 / 0.59 (majority 0.65) | 0.61 / 0.61 / 0.59 | 0.03 (0.03) / 0.19 / 0.28 | 0.00,— | 0.04,0.84 | 0.52,0.63 |

Latency (median p50 across suites): Jev 160 ms, Haiku 875 ms, Luna 1,060 ms.
Cost per call (mean over suites): Jev $0.00005, Haiku $0.0019.

### Breakdowns that matter

JudgeBench by source (Jev / Haiku / Luna): MMLU-Pro knowledge 0.79 / 0.58 / 0.80;
LiveBench math 0.80 / 0.80 / 0.86; LiveCodeBench 0.81 / 0.80 / 0.93; LiveBench reasoning
0.66 / 0.71 / 0.91. Jev's knowledge score is as high as the reasoning model's; its
reasoning score is the lowest of the three.

Synthetic compute by family (Jev / Haiku / Luna): day-of-week 0.50 / 0.98 / 1.00;
4-digit sum 0.70 / 0.38 / 1.00; letter count 0.85 / 0.63 / 0.83; 2-digit product
0.98 / 0.73 / 1.00; nth list item 0.98 / 0.88 / 0.98; exact bullet count 1.00 / 1.00 / 1.00.
Jev is at chance on the one family that requires a calculation chain and cannot be
pattern-matched (date arithmetic). It beats the non-reasoning LLM on every arithmetic
family, which suggests it has learned digit-level checks (last digit of a product, carry
patterns) rather than arithmetic.

LLMBar adversarial by subset (Jev / Haiku / Luna): GPTInst 0.87 / 0.80 / 0.92; Neighbor
0.74 / 0.68 / 0.81; Manual 0.76 / 0.72 / 0.89; GPTOut 0.72 / 0.62 / 0.66. The adversarial
outputs are designed to look better while not following the instruction; Jev falls for
them less than Haiku and more than Luna.

## Robustness

**Order swap.** Rerunning the pairwise suites with the two outputs swapped changed
accuracy by at most 1.5 points. Per-case, Jev picked the same *content* in both orders
on 95% of LLMBar natural, 85% of LLMBar adversarial, 88% of JudgeBench, and 69% of
synthetic compute pairs. It was correct in both orders on 71% of JudgeBench and 64% of
synthetic compute pairs; those are the honest single-order-independent numbers. Jev
chose the null option in 7% of JudgeBench pairs.

**Bundling.** Asking the acceptance question alone versus together with six other
questions on the same state moved P(yes) by 0.010 on average (p95 0.030) and flipped the
answer in 2.2% of cases.

**Repeatability** (30 AgentRewardBench cases × 20 repetitions, Score question `quality`
on five levels mapped to 0–1, plus the yes/no `success`):

| judge | quality: mean per-case variance | sd | argmax flips | success: variance | flips |
|---|---|---|---|---|---|
| Jev | 0.00010 | 0.009 | 1.7% | 0.00011 | 0.0% |
| Haiku | 0.00229 | 0.038 | 9.2% | 0.00042 | 0.0% |
| Luna | 0.00888 | 0.076 | 22.5% | 0.12469 | 24.7% |

Jev's variance is 23× lower than Haiku's and 90× lower than Luna's, in line with
LangChain's 92–913×. Luna changed its yes/no `success` answer on a quarter of repeated
calls. For regression tracking this sets the smallest paired shift in mean quality each
judge can detect (α 0.05, power 0.8, ≈ 2.8·sd/√N): at N = 100 traces, Jev 0.003,
Haiku 0.013, Luna 0.026 on a 0–1 scale.

**Calibration map** (split-half temperature scaling on Jev, held-out ECE): arb_success
0.15 → 0.10 (T 1.6), toolfail 0.21 → 0.13 (T 1.8), arb_repetition 0.10 → 0.06 (T 1.6),
tool_error 0.13 → 0.09 (T 1.5). A single temperature recovers a third to a half of the
excess error on the overconfident suites and does not reach the noise floor. Where Jev's
raw ECE was already at the floor (acceptance, paths, trail, LLMBar, JudgeBench) the
fitted T is 0.5–0.9: it under-claims on tasks it knows.

**Decomposition** (`freyja_acceptance_decomp`: seven yes/no questions per state, combined
by cross-validated logistic regression): accuracy 0.68 and AUROC 0.68, against 0.63 and
0.61 for the direct holistic question and 0.65 for always-yes. The most predictive
sub-question was whether the task is design or visual work, an inverted signal (AUROC
0.66): those turns get iterated regardless of quality. Judgments of completion, scope, and
claim support each had AUROC 0.52–0.60.

**Format ablation** (H6): adding a generic `expected_behavior` line to the Freyja states
lowered acceptance accuracy from 0.63 to 0.57 and raised ECE from 0.03 to 0.10, and did not
change the literal suites. A task-specific expectation, as in LangChain's run, was not
tested; the generic form hurts.


## Classification suites (added 2026-09-21)

Three many-way Choice suites, each with a null option, to test the routing decision
Freyja would make before acting. Same three judges, same states and option lists.

| suite | n | options | truth | acc Jev / Haiku / Luna | ECE Jev / Haiku / Luna | cov,prec@.9 Jev | Haiku | Luna |
|---|---|---|---|---|---|---|---|---|
| `clinc150` | 1,300 | 150 intents + out_of_scope | dataset label (1,000 in-scope + 300 out-of-scope) | 0.90 / 0.90 / 0.92 | 0.03 / 0.04 / 0.05 | 0.80,0.96 | 0.81,0.94 | 0.89,0.95 |
| `banking77` | 1,000 | 77 intents + none_of_these | dataset label | 0.78 / 0.75 / 0.87 | 0.10 / 0.10 / 0.05 | 0.67,0.93 | 0.54,0.92 | 0.77,0.96 |
| `freyja_tasktype` / task_type | 942 | 11 capabilities + none_of_these | the capability the agent actually used in that turn, by a fixed precedence | 0.45 / 0.48 / 0.49 (majority 0.21) | 0.40 / 0.18 / 0.39 | 0.61,0.58 | 0.03,0.78 | 0.54,0.57 |
| `freyja_tasktype` / needs_web | 942 | yes/no | web tools used in the turn | 0.90 / 0.92 / 0.92 | 0.05 / 0.01 / 0.06 | 0.54,0.92 | 0.86,0.95 | 0.97,0.93 |
| `freyja_tasktype` / needs_browser_or_computer | 942 | yes/no | browser or computer-use tools used | 0.73 / 0.57 / 0.95 | 0.09 / 0.28 / 0.02 | 0.35,0.99 | 0.44,0.98 | 0.82,0.97 |
| `freyja_tasktype` / needs_edit | 942 | yes/no | write/edit tools used | 0.68 / 0.73 / 0.72 | 0.17 / 0.14 / 0.26 | 0.34,0.70 | 0.55,0.85 | 0.94,0.74 |
| `freyja_tasktype` / needs_delegation | 942 | yes/no | sub-agents spawned | 0.90 / 0.90 / 0.90 (majority 0.89) | 0.11 / 0.03 / 0.08 | 0.16,0.99 | 0.88,0.92 | 0.98,0.91 |

`freyja_tasktype` states are the opening request of a session and nothing else (210
written by the user, 732 task briefs written by a parent agent for a sub-agent). Truth is
what the turn actually used, from the tool calls in the log; classes are assigned by a
precedence list (computer_use > browser > delegation > web_research > media > slack >
widget > file_edit > shell > read_explore > answer_only). Median latency on these suites:
Jev 132 ms, Haiku 900 ms, Luna 1,500 ms.

What the classification suites add:

- **Many-way Choice works.** With 151 options in one call Jev matches Haiku and is 1.5
  points behind the reasoning model (CLINC150 0.90 vs 0.92), at the noise-floor ECE
  (0.033). On the finer-grained Banking77 it is 9 points behind Luna and 3 ahead of Haiku.
  The Laya authors reported Jev at 0.870 on Banking77; our 0.777 uses a 1,000-case sample
  and an added null option, which Jev chose on 4.9% of cases (the LLMs almost never).
- **The null option is used correctly when the data supports it.** On CLINC, Jev routed 86%
  of out-of-scope utterances to `out_of_scope` while misrouting only 3% of in-scope ones
  there (Haiku 85% / 2%, Luna 81% / 1.3%).
- **Task type from the request alone splits into a predictable and an unpredictable part,
  for every judge.** All three land at 0.45–0.49 on the 11-way question (majority 0.21;
  0.72–0.78 when classes are merged to five). Per-class recall for Jev: web_research 0.94,
  media 1.00, browser 0.79, computer_use 0.65, but shell 0.05, read_explore 0.04,
  delegation 0.14. Whether a request will "need the web" or "need a browser" is a
  property of the request; whether the agent will edit files, run shell commands, or
  spawn sub-agents is a property of the agent's policy and cannot be read from the
  request. The yes/no questions confirm this: needs_web 0.90–0.92 for all judges,
  needs_edit 0.68–0.73 for all judges. Human-written and agent-written requests score
  alike (Jev 0.48 vs 0.44).
- **Luna reads intent from terse requests better than Jev.** needs_browser_or_computer
  0.95 vs 0.73: requests like "check the page" or "open the app" are recognized by the
  reasoning model and missed by Jev, which stays near the base rate. This is the clearest
  case in the benchmark of an LLM's world knowledge beating Jev on a literal-looking question.
- **Jev's calibration fails on an ill-posed Choice.** On the 11-way task_type question Jev
  answered with p ≥ 0.9 on 61% of cases and was right on 58% of those (ECE 0.40, same as
  Luna at 0.39); Haiku hedged instead (3% above 0.9). On the acceptance suite, also
  unpredictable from the input, Jev hedged (0% above 0.9). The difference is the question
  type: on many-way Choice Jev is overconfident when the label is not determined by the
  input, which matches the third-party finding that Choice runs hot (T ≈ 3.3) while Noul
  runs cold. Do not trust a many-way Choice probability on a question whose answer depends
  on something outside the state.

## Hypotheses

| # | hypothesis | verdict |
|---|---|---|
| H1 | Within 5 points of a frontier judge on literal, local questions | **Supported.** synth_literal, LLMBar natural, paths_grounded, tool_error, side_effect, repetition: Jev is within 3 points of Luna, and has the best AUROC on tool_error (0.91 vs 0.80) and arb_success (0.84 vs 0.78). |
| H2 | Far below an LLM judge on computation, reasoning, and outside knowledge; overconfident there | **Partly refuted.** Jev trails the reasoning model by 10 points on JudgeBench and 16 on synthetic compute, but beats the non-reasoning LLM on both. Its knowledge (MMLU-Pro pairs 0.79) matches Luna. It is at chance only where a calculation chain is unavoidable (day-of-week). Its ECE on JudgeBench is 0.07 (floor 0.05) and on synthetic compute 0.07 (floor 0.05), against 0.15 and 0.28 for Haiku. |
| H3 | Little better than base rate on "will the user accept this turn"; some ranking signal | **Supported, for all three judges.** Every judge scores below always-yes (0.65); AUROC 0.59–0.61. The outcome is mostly not predictable from the turn. |
| H4 | Score variance ≥10× lower than an LLM judge | **Supported.** 23× vs Haiku, 90× vs Luna. |
| H5 | p ≥ 0.9 gives precision ≥ 0.95 on yes/no everywhere | **Refuted as stated.** Precision at 0.9 is 0.96–1.00 on synth_literal, LLMBar natural, and paths_grounded, but 0.84–0.90 on the agent-trace suites (arb, trail, tool_error, toolfail). The threshold is far more meaningful than the LLM judges' (Luna answers ≥ 0.9 on 79–99% of agent-trace cases and is wrong on 17–35% of them), but it is not a safety guarantee out of distribution. |
| H6 | An explicit expected-behavior field helps more than question wording | **Refuted** for a generic field. |

## What this says about where Jev is smart enough

Smart enough, on this evidence:

- Literal checks on agent output (format, language, refusal, keyword, code block, bullet
  count): 100%; topical relevance 88%; calibrated; 160 ms.
- Instruction-following comparison when the outputs are honest (LLMBar natural 93%).
- Reading tool results for errors and checking that reported paths exist in the trace
  (AUROC 0.86–0.91), with well-ordered probabilities: precision 0.90–0.96 at p ≥ 0.9.
- Recognizing repeated actions and loops in a trajectory (AUROC 0.90).
- Ranking trajectories by likely success for triage (AUROC 0.84, better than either
  LLM), even though its argmax accuracy is only 7 points over majority.
- Being the stable metric: 1–2% answer flips across repetitions versus 9–25% for the LLMs,
  which makes a per-commit regression signal usable at N ≈ 100 traces.
- Knowledge-heavy correctness comparison (MMLU-Pro pairs 0.79), contamination not ruled out.
- Many-way intent routing with an abstain option: 150 intents at 90% with correct out-of-scope handling; 77 fine-grained intents at 78%.
- Request-determined routing signals (needs the web: 0.90, matching the LLMs).

Not smart enough, on this evidence:

- Anything requiring a calculation chain: day-of-week 50%, 4-digit sums 70%, LiveBench
  reasoning 66% (Luna 91%).
- Adversarial pairs where the wrong output looks better (LLMBar adversarial 78% vs 83%;
  GPTOut subset 72%).
- Predicting whether a real user will accept a turn: no judge can, from the turn alone.
- Side effects in web-agent trajectories: at majority rate for all three judges with the
  compressed text-only state used here.
- Predicting whether a tool call will fail before running it: 56%, overconfident (ECE 0.21).
- Absolute probabilities out of distribution: overconfident by T ≈ 1.5–1.8 on agent-trace
  suites; a per-suite temperature recovers part of it; fit a map before thresholds go live.
- Reading intent from terse requests that need world knowledge ("open the app", "check the page"): needs_browser_or_computer 0.73 vs Luna 0.95.
- Many-way Choice on a question the input does not determine (which tools the agent will pick): 0.45 like every judge, but with p ≥ 0.9 on 61% of cases and 58% precision there. Choice does not hedge the way Noul does.

## Limitations

- Acceptance labels have ~5–8% noise from "refine further" follow-ups that are neither
  correction nor acceptance; this caps every judge on that suite, but not near the observed 0.63.
- AgentRewardBench and TRAIL states are heavily compressed (first 2 + last 8 steps, 600-char
  observation excerpts, 24k-char cap). Both the LLMs and Jev see the same compression, so the
  comparison is fair, but absolute numbers understate what a judge with the full trace could do.
- JudgeBench and LLMBar are public since 2024 and may be in Jev's training data. The
  synthetic suites and the Freyja traces cannot be.
- LLM probabilities are verbalized (asked for in the JSON), not logprobs. About 15% of them
  were reported as confidence in the chosen answer rather than P(yes) and were corrected by
  rule; residual ambiguity would inflate LLM ECE somewhat, but not the 2–5× gap.
- One frontier model per family was not run (Sonnet 4.6, GPT-5.5). Luna is a small reasoning model.
- Only 30 cases × 20 repetitions for repeatability.

## Reproduce

```
cd ~/personal/freyja
.venv/bin/python evals/jev_judge/build/freyja.py      # needs ~/.freyja/sessions and ANTHROPIC_API_KEY for the follow-up labeler
.venv/bin/python evals/jev_judge/build/llmbar.py && .venv/bin/python evals/jev_judge/build/judgebench.py
.venv/bin/python evals/jev_judge/build/agentrewardbench.py && .venv/bin/python evals/jev_judge/build/trail.py
.venv/bin/python evals/jev_judge/build/synth.py
S=llmbar_natural,llmbar_adversarial,judgebench,arb_success,arb_side_effect,arb_repetition,trail_error,freyja_acceptance,freyja_literal,freyja_toolfail,synth_compute,synth_literal
.venv/bin/python evals/jev_judge/run.py --suite $S --judge jev --judge anthropic:claude-haiku-4-5 --judge openai:gpt-5.6-luna
.venv/bin/python evals/jev_judge/run.py --suite repeat_arb --judge jev --judge anthropic:claude-haiku-4-5 --judge openai:gpt-5.6-luna --reps 20
.venv/bin/python evals/jev_judge/analyze.py evals/jev_judge/results/*.jsonl --by source
.venv/bin/python evals/jev_judge/analyze.py evals/jev_judge/results/*repeat*.jsonl --repeat
.venv/bin/python evals/jev_judge/calibrate.py evals/jev_judge/results/*_all_jev.jsonl
```

Cached judgments in `data/cache/` make reruns free; `renormalize.py` re-derives LLM answers
from cached raw text after a parser change.
