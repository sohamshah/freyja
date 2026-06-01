# Freyja Skill Learning — Design Brief

_Status: draft v1 · 2026-05-31 · author: agent-pair (extends EVAL-HARNESS-DESIGN.md)_

---

## 0 · Executive summary

This doc describes a self-improvement loop for Freyja that **learns skills the agent should know**, **measures their value empirically through replay**, and **guards against the failure modes that have shipped in every system that's tried this before**.

The core thesis is one sentence: **a skill's value is a measurable quantity, not a label**. Hermes treats skills as text with manually-assigned `confidence: unvalidated|experimental|verified|deprecated` tags. Their entire 10,500-LOC self-improvement loop never empirically tests whether a skill *actually helps*. Ours does — by re-running historical sessions with vs without the candidate and scoring both through the eval harness in `EVAL-HARNESS-DESIGN.md`.

That single change cascades: replay-validated skills are auto-promoted instead of needing operator review for every save; decay becomes a measurable signal instead of a fixed 30-day cutoff; consolidation (umbrella-building) becomes empirical rather than an LLM judgment about prefix clusters; and we get a defensible answer to "is the skill library actually making the agent better, or are we just accumulating text?"

The system uses **more** LLM calls than Hermes — replay-based validation is expensive — but each call buys something measurable. The optimization target is *signal-per-dollar*, not minimum dollar.

We treat the existing eval harness as the substrate for "good outcome." Skill learning rides on top: instead of inventing new outcome metrics, every skill action (promote, demote, archive, patch) is justified in eval-harness terms. The same reward-hacking detector that catches gameable judge scores in agent runs also gates skill promotion against gameable replay scores.

The doc is heavily prescriptive — eight pipelines specified end-to-end with file paths, data shapes, prompts, and budgets — because the failure modes here are well-documented and the design space is well-bounded by what Hermes already taught us.

---

## 1 · Reference points

This design is informed by:

1. **HERMES_DEEP_DIVE.md §28** — the 10,500-LOC, 5-layer self-improvement loop in `~/work/services/hermes-agent`. Their failure modes are documented in their own prompts (the "Do NOT capture" list is the dictionary of mistakes they made before hardening). We port their guardrails wholesale and beat them on validation.
2. **`docs/EVAL-HARNESS-DESIGN.md`** (Freyja, draft v2) — gives us trace-as-substrate, three-layer grader stack, reward-hacking detector, claim-grounding scorer. The skill learning loop delegates all outcome judgment to this.
3. **`bridge/knowledge/{skill_store.py, memory_store.py, models.py}`** (1,800 LOC) — Freyja's existing file-backed knowledge layer. Already has `SkillRecord` with `confidence`, `success_signals`, `failure_signals`, `load_count`, plus `record_review_decision()` plumbing for thumbs-up/down. We extend, not replace.
4. **HERMES_DEEP_DIVE.md §28.5.4** — the Skills Guard security scanner (964 LOC, 88+ threat patterns). Ports cleanly; we keep this verbatim as the L0 security gate.

---

## 2 · What Hermes built (compressed)

Five layers, each on its own cadence:

| Layer | Trigger | What it does | Cost per run |
|---|---|---|---|
| L1 In-prompt guidance | Every turn | Two text blocks (`MEMORY_GUIDANCE`, `SKILLS_GUIDANCE`) tell the model what to save and what not to. Stable for prompt cache. | $0 (text) |
| L2 Cadence counters | Every turn | `_turns_since_memory >= 10` → flag for review. | $0 |
| L3 Background review | Counter trips | Fork an AIAgent with restricted toolset `[memory, skill_manage]`, replay the conversation snapshot, decide what to save. **Same model, same cached system prompt** for 26% prefix-cache cost reduction. | ~$0.05-$0.30 per session |
| L4 Cross-session curator | Every 7 days (deferred on fresh install) | LLM consolidates prefix clusters into umbrellas. Pre-mutation tar.gz snapshot for rollback. ~196-line prompt with 5 hard rules. | ~$2-$10 per run |
| L5 Telemetry sidecar | Continuous | `.usage.json` per-skill: view/use/patch counters, `created_by` provenance, `pinned`, `state ∈ {active, stale, archived}`. | $0 (file I/O) |

**Strengths to port verbatim**:
- The "Do NOT capture" denylist — that list is the result of real production regressions. Reuse, don't rederive.
- Tool whitelist enforcement during background-review forks.
- `stdout` redirection so the operator never sees the review thread's chatter.
- Cached-system-prompt inheritance for prefix-cache hits.
- Pre-mutation snapshot before destructive operations.
- Skills Guard 88-pattern threat scanner (ports as-is — it's 100% deterministic and orthogonal).

**Limitations we eliminate**:
- No empirical outcome measurement (skills are saved based on LLM-judged "this seems useful" with zero verification).
- Time-only decay (30-day stale, 90-day archive — no relation to actual quality).
- Consolidation by **lexical** clustering ("prefix clusters") instead of **behavioral** clustering.
- Their own curator prompt instructs the LLM to *ignore* usage counters ("'use=0' is not evidence a skill is valuable") — a tell that they don't trust their own telemetry.
- No counterfactual evaluation. They never ask "would the agent have done better without this skill loaded?"
- No mechanism to *improve* a skill over time other than patch-on-correction. Skills can only grow content; they can't shed bad content.
- No negative library — rejected candidates are forgotten and can be re-proposed.

---

## 3 · Core thesis: value is measurable

Define a skill's **value contribution** $V(s)$ as:

$$V(s) = \mathbb{E}_{t \in T_s}\Big[\text{Score}(\text{Run}(t, s)) - \text{Score}(\text{Run}(t, \emptyset))\Big]$$

where:
- $T_s$ is the set of tasks for which skill $s$ would have applied (matched by trigger/topic).
- $\text{Run}(t, s)$ is the agent running task $t$ with skill $s$ in context.
- $\text{Run}(t, \emptyset)$ is the same agent running the same task with $s$ deliberately withheld.
- $\text{Score}(\cdot)$ is the multi-dimensional score vector from the eval harness (`EVAL-HARNESS-DESIGN.md §6`).

$V(s)$ is a vector (not a scalar). Per dimension:
- $V_{\text{trajectory}}$: did the skill make the agent more efficient? (Fewer tool calls to the same outcome.)
- $V_{\text{grounding}}$: did the skill prevent hallucinated claims?
- $V_{\text{self\_correction}}$: did the skill help the agent recover from its own errors?
- $V_{\text{safety}}$: did the skill keep the agent inside guardrails?
- $V_{\text{reward\_hacking}}$: did the skill help the agent legitimately succeed, or did it teach gameable shortcuts?

A skill is **net-positive** if $\sum_d w_d V_d(s) > 0$ for some weight vector $w$ (default: equal weights, but `safety` can never be negative — any safety regression archives the skill regardless of other dimensions).

This formulation gives us deterministic, defensible answers to every operational question the system needs:

- **Should we promote this candidate?** Run replay, compute $V$, promote if positive.
- **Is this skill decaying?** Watch the rolling $V$ across recent uses; alert when it crosses zero.
- **Should umbrella $u$ replace its members $\{s_1, ..., s_n\}$?** Run replay on $u$ over $T_{s_1} \cup ... \cup T_{s_n}$; if $V(u)$ exceeds $\max_i V(s_i)$ within tolerance, replace.
- **Which skill is "better" when two cover the same task?** Compare $V$ per dimension.
- **How should we rank skills in the system prompt?** By $V$, descending.

The fundamental change vs Hermes: every operation in the skill loop has a numerical justification. There is no "the LLM said it was worth saving" that we have to trust on its own.

---

## 4 · Architecture overview

Eight pipelines, each with a clear input/output and a single failure surface:

```
┌──────────────────────────────────────────────────────────────────────────────┐
│  LIVE SESSION                                                                │
│  bridge/freyja_bridge.py emits text/tool/thinking events as today.           │
└────────────────────┬──────────────────────────────────────┬──────────────────┘
                     │                                      │
                     │ Per-turn, deterministic              │ Continuous
                     ▼                                      ▼
   ┌──────────────────────────────┐    ┌──────────────────────────────────┐
   │ PIPELINE 1: Signal Detection │    │ PIPELINE 5: Outcome Watcher       │
   │ Pattern-match on user        │    │ When skill S was loaded this turn,│
   │ messages + tool trajectories │    │ watch the next 3 turns for        │
   │ for corrections, struggle,   │    │ correction signals, error loops, │
   │ novel workflow. Emits to    │    │ task completion. Append to        │
   │ .signals.jsonl              │    │ skills/.events.jsonl              │
   └────────────────┬─────────────┘    └──────────────────────────────────┘
                    │ At turn end if any signal fired           │
                    ▼                                            │
   ┌──────────────────────────────────────────────────┐         │
   │ PIPELINE 2: Drafting (Haiku, refusal-biased)     │         │
   │ Reads signal + transcript + negative_library.    │         │
   │ Refuses by default; emits .candidates/<uuid>.yaml │        │
   │ on positive signal.                              │         │
   └────────────────┬─────────────────────────────────┘         │
                    │                                            │
                    ▼                                            │
   ┌──────────────────────────────────────────────────┐         │
   │ PIPELINE 3: Replay Validation (Sonnet + harness) │         │
   │ Find ≤5 historical tasks matching candidate's    │         │
   │ trigger fingerprint. Fork twice (with/without).  │         │
   │ Score both via eval harness. Compute ΔV.         │         │
   │ → if ΔV > threshold & no safety regression:      │         │
   │     auto-promote to .validated/                  │         │
   │ → else: write to .rejected/ with reason          │         │
   └────────────────┬─────────────────────────────────┘         │
                    │                                            │
                    ▼                                            │
   ┌──────────────────────────────────────────────────┐         │
   │ PIPELINE 4: Operator Confirmation                │         │
   │ Desktop toast / Slack Block Kit:                 │         │
   │   "Validated: <name> (+0.34 traj_eff, ±0 safety) │         │
   │   [Promote] [Edit] [Discard]"                    │         │
   │ Auto-promote after 24h if no action AND ΔV > 2σ. │         │
   └────────────────┬─────────────────────────────────┘         │
                    │                                            │
                    ▼                                            ▼
   ┌─────────────────────────────────────────────────────────────────────────┐
   │ ~/.freyja/skills/<name>/SKILL.md   ← canonical store                   │
   │ ~/.freyja/skills/.events.jsonl     ← append-only telemetry             │
   │ ~/.freyja/skills/.value/<name>.json ← computed V vector + history     │
   └────────────────┬──────────────────────────────────┬─────────────────────┘
                    │                                  │
                    │ Daily cron                       │ Per-skill on signal
                    ▼                                  ▼
   ┌──────────────────────────────────┐    ┌──────────────────────────────────┐
   │ PIPELINE 6: Decay Model          │    │ PIPELINE 7: Patch Proposer       │
   │ Multi-factor:                    │    │ When skill S was loaded AND      │
   │   time × outcome_drift           │    │ followed by a correction:        │
   │     × env_drift × replacement    │    │ LLM drafts a patch to S          │
   │ pressure. High-decay skills:    │    │ incorporating the correction.    │
   │ run refresh replay → demote     │    │ Replay-validate against past     │
   │ → archive (never delete).       │    │ uses of S. Promote if non-       │
   └──────────────────────────────────┘    │ regressive on existing T_S.      │
                                           └──────────────────────────────────┘
                            │ Monthly
                            ▼
   ┌─────────────────────────────────────────────────────────────────────────┐
   │ PIPELINE 8: Umbrella Proposer                                          │
   │ LLM scans skill set, proposes umbrellas. Replay-validate each against │
   │ task sets of all members. Promote umbrella iff V(umbrella) ≥          │
   │ max_i V(s_i) - tolerance. Archive members on promotion.               │
   └─────────────────────────────────────────────────────────────────────────┘
```

Two orthogonal services run alongside:

- **L0 Security Gate** — Skills Guard scanner (port from Hermes, 88 patterns). Runs at every write to `.candidates/` and again at promotion. Refuses content matching `exfiltration`, `injection`, `destructive`, `persistence`, `obfuscation` patterns.
- **L0 Structural Validator** — Frontmatter check, name shape (no PR numbers, no dates, no "fix-X-Y"), required fields. Rejects at the drafter output stage — Pipeline 3 never sees malformed candidates.

---

## 5 · Data model

### 5.1 · On-disk layout

```
~/.freyja/skills/
├── <skill-name>/                       ← canonical skill (operator-visible)
│   ├── SKILL.md                          ← frontmatter + body
│   ├── references/                       ← knowledge bank files
│   ├── templates/                        ← scaffolding files
│   ├── scripts/                          ← runnable scripts
│   ├── assets/                           ← supplementary files
│   └── .history/                         ← versioned old SKILL.md (auto)
│       └── 20260530-120000-promote.md
├── .candidates/
│   └── <uuid>.yaml                       ← drafter output awaiting validation
├── .validated/
│   └── <uuid>.yaml                       ← passed Pipeline 3, awaiting Pipeline 4
├── .rejected/
│   └── <uuid>.yaml                       ← negative library — drafter consults
├── .archived/
│   └── <skill-name>/                     ← never deleted, just hidden
├── .events.jsonl                         ← append-only telemetry (all skills)
├── .signals.jsonl                        ← append-only signal detector log
├── .value/
│   └── <skill-name>.json                 ← current V vector + history
├── .replay/
│   ├── budget.json                       ← lifetime $$ remaining per skill
│   └── runs/<run-id>/                    ← per-replay artifacts
│       ├── manifest.json
│       ├── with-skill.transcript.json
│       ├── without-skill.transcript.json
│       └── scorecard.json
└── .negative_library.jsonl              ← patterns we've learned NOT to learn
```

### 5.2 · SKILL.md frontmatter (extends Hermes' format)

```yaml
---
name: code-review-feedback-style
description: User prefers terse single-line review comments without preamble
version: 3                              # bumped on every patch
license: MIT
platforms: [macos, linux]
triggers:                               # for SignalDetector / matcher
  - "code review"
  - "PR feedback"
  - "review this"
tags: [code, review, communication]
metadata:
  freyja:
    created_by: agent                   # agent | operator | bundled | hub
    created_from_signal: correction     # correction | struggle_resolved | novel | operator_initiated
    created_at: 1730000000000
    validated_via_replay: true          # was Pipeline 3 run?
    validation_runs: 5                  # how many past tasks were used
    confidence: verified                # unvalidated | experimental | verified | deprecated
    last_value_check_at: 1730500000000
    archived: false
    archived_reason: null
    pinned: false                       # operator-set, blocks auto-archive
---

# Code review feedback style

[skill body — declarative, not imperative; see Hermes' MEMORY_GUIDANCE rule]
```

### 5.3 · `.events.jsonl` — single append-only telemetry file

One file for all skills, one event per line. Per-skill stats are computed at read time.

```jsonl
{"ts": 1730000000000, "skill": "code-review-style", "event": "drafted", "candidate_id": "abc", "signal_type": "correction", "evidence_session": "freyja:slack:T1:c:C1:1700"}
{"ts": 1730000010000, "skill": "code-review-style", "event": "replay_started", "candidate_id": "abc", "run_id": "rep-001"}
{"ts": 1730000300000, "skill": "code-review-style", "event": "replay_completed", "run_id": "rep-001", "delta_v": {"trajectory": 0.34, "grounding": 0.0, "safety": 0.0}, "verdict": "promote"}
{"ts": 1730000400000, "skill": "code-review-style", "event": "promoted", "actor": "auto", "version": 1}
{"ts": 1730050000000, "skill": "code-review-style", "event": "loaded", "session_id": "comp_19d8..."}
{"ts": 1730050900000, "skill": "code-review-style", "event": "outcome", "session_id": "comp_19d8...", "load_event_ts": 1730050000000, "result": "clean", "subsequent_corrections": 0}
{"ts": 1731000000000, "skill": "code-review-style", "event": "decay_check", "score": 0.07, "factors": {"time": 0.3, "outcome_drift": 0.05, "env_drift": 0.0}}
{"ts": 1732000000000, "skill": "code-review-style", "event": "patched", "version": 2, "patch_from_signal": "correction", "patch_session": "comp_19d9..."}
```

Why append-only:
- No file-locking dance (Hermes' `.usage.json` requires `fcntl`/`msvcrt` + atomic-replace).
- Trivially safe across multiple Freyja processes.
- Time-traveling debugging: "what was the V of this skill on day X?" becomes a one-pass scan.
- Easy backup: copy the file.
- Recoverable from corruption: skip bad lines.

Per-skill rollup is cached in `.value/<name>.json` and recomputed on event-file mtime change.

### 5.4 · `.value/<skill-name>.json` — computed value rollup

```json
{
  "skill": "code-review-style",
  "computed_at": 1731500000000,
  "v_vector": {
    "trajectory": 0.34,
    "grounding": 0.02,
    "self_correction": 0.05,
    "safety": 0.0,
    "reward_hacking": 0.0
  },
  "v_scalar": 0.16,
  "v_confidence_interval_95": [0.04, 0.28],
  "load_count": 23,
  "outcome_distribution": {"clean": 19, "correction": 3, "error_loop": 1},
  "rolling_30d_correction_rate": 0.13,
  "rolling_30d_correction_rate_slope": -0.02,
  "validation_runs": 5,
  "lifetime_replay_cost_usd": 1.47,
  "lifetime_replay_budget_usd": 5.00,
  "last_use_at": 1731450000000,
  "last_positive_outcome_at": 1731450000000,
  "decay_score": 0.07,
  "archived": false
}
```

Most consumers (the ranker in §10, the decay model in §13) read this, not the raw events file.

---

## 6 · Pipeline 1: Signal Detection (deterministic, per-turn)

**File**: `bridge/knowledge/signal_detector.py` (~250 LOC)

The detector runs in the bridge after every assistant turn. It does **no LLM work**. It pattern-matches against three families of signal:

### 6.1 · Correction signals

User said something that means "you got this wrong, course-correct." The pattern set is ported verbatim from Hermes' SKILL_REVIEW_PROMPT § "Signals to look for" — they did the work of finding the right phrases:

```python
CORRECTION_PHRASES = [
    r"\bstop\b",                          # "stop doing X"
    r"\bdon'?t\b",                        # "don't format like this"
    r"\bno,?\s+(not|actually|wait)\b",    # "no, not like that"
    r"\bjust\s+(give|do|tell)\b",         # "just give me the answer"
    r"\bI\s+told\s+you\b",
    r"\b(too|overly|extremely)\s+verbose\b",
    r"\bwhy\s+(are|did)\s+you\b",         # frustration tell
    r"\binstead\b",                       # "instead of X do Y"
    r"\bthat'?s\s+wrong\b",
    r"\byou\s+always\b",                  # "you always Y and I hate it"
    r"\bI\s+hate\s+(when|how)\b",
    r"\bremember\s+(this|that)\b",        # explicit save signal
    # Hermes lists more in their prompt; port the full set
]
```

A match is necessary but not sufficient. For each match:
1. Identify what the previous assistant turn DID (text content + tool calls).
2. Emit a `CorrectionSignal(target=<previous assistant action>, phrase=<matched phrase>)`.

### 6.2 · Struggle-resolved signals

A non-trivial workflow surfaced. Pattern: ≥3 failed tool calls of the same family followed by a success on the same task.

```python
def detect_struggle_resolved(tool_calls: list[ToolCallRecord]) -> StruggleSignal | None:
    # Look at the last K=10 tool calls in this turn.
    # Find runs of ≥3 errors of the same tool family followed by ≥1 success.
    # "Same family" = same tool name OR same shell command verb (rg/grep/find).
    # On match: capture (errors, successful_recipe) as the signal evidence.
    ...
```

The signal evidence is the *recipe that worked* — the success step at the end of the failed-then-resolved sequence. That's what would go into a skill body.

### 6.3 · Novel workflow signals

Track a per-session "fingerprint" of tool-call sequences: `(tool_name, key_args_hash)` tuples. After the turn, compare against existing skills' fingerprints (extracted at promotion time and stored in `.value/<name>.json`).

```python
def detect_novel_workflow(turn: TurnRecord, existing_skills: list[SkillRecord]) -> NovelSignal | None:
    fp = compute_fingerprint(turn.tool_calls)
    if len(turn.tool_calls) < 5:
        return None  # not workflow-y enough
    if any(cosine(fp, s.fingerprint) > 0.7 for s in existing_skills):
        return None  # already covered by an existing skill
    return NovelSignal(fingerprint=fp, evidence=turn.tool_calls)
```

### 6.4 · Signal output

Every detected signal lands in `~/.freyja/skills/.signals.jsonl`:

```jsonl
{"ts": ..., "session_id": "...", "turn_id": "turn-3", "type": "correction", "evidence": {...}, "phrase_matched": "stop doing X"}
{"ts": ..., "session_id": "...", "turn_id": "turn-7", "type": "struggle_resolved", "evidence": {"failed_calls": [...], "successful_recipe": {...}}}
```

The drafter consumes this file at turn-end.

### 6.5 · Why deterministic detection here, not LLM-judged

Hermes' background review uses an LLM to scan the conversation for the exact same patterns we pattern-match. That's a $0.05-$0.30 call to do something a regex can do in microseconds. The LLM call is justifiable for *drafting* (writing a skill body is a creative task); it's not justified for *detection* (is there a signal? yes/no).

This is the only place we **reduce** LLM use vs Hermes. Every other pipeline below uses *more* LLM than Hermes — but on tasks where the LLM is paying for itself.

### 6.6 · Guardrails at the detector level

Two filters:
1. **Signal cooldown**: same `(session_id, signal_type)` pair triggers at most once per turn. Prevents a frustrated user from generating 20 correction signals in one message.
2. **Negative-library precheck**: if the matched correction's surface text matches a pattern in `.negative_library.jsonl` (e.g. user says "this tool is broken" → we've learned not to capture negative tool claims), the signal is logged but flagged `would_discard=true`. The drafter sees the flag and refuses.

---

## 7 · Pipeline 2: Drafting (LLM, refusal-biased)

**File**: `bridge/knowledge/drafter.py` (~400 LOC)

After every turn that produced ≥1 signal, the drafter runs once. It is **explicitly biased toward refusing**.

### 7.1 · Drafter prompt structure

```
You are Freyja's skill drafter. A signal fired this turn that *might* be
worth saving as a skill — but most signals don't warrant skills.

YOUR JOB IS TO REFUSE BY DEFAULT. Only emit a candidate when the signal
unambiguously represents a class-level, transferable insight.

SIGNAL TYPE: {signal_type}
EVIDENCE: {evidence}
SESSION CONTEXT (last 4 messages): {context}

NEGATIVE LIBRARY (patterns we've learned NOT to capture):
{negative_library_excerpt}

REFUSAL CHECKLIST — answer Yes/No on each. ANY Yes → output "DISCARD".

[ ] Does the would-be skill assert that a tool / feature does not work?
[ ] Does it reference a specific PR number, issue number, SHA, version, or date?
[ ] Is the corrected behavior about a missing binary, env var, or fresh-install gap?
[ ] Did the originally-failed action succeed on simple retry? (Then the lesson
    is "retry," not the failure.)
[ ] Is the name you'd give it specific to today's task — `fix-X`, `audit-Y`,
    `debug-Z-today`?
[ ] Does the candidate match a pattern in the negative library above?
[ ] Is the "user correction" actually the user changing their mind, not
    correcting your behavior?

IF ALL NO, emit the candidate as:

```yaml
candidate:
  name: <lowercase-hyphen-classname>     # validated structurally; see below
  description: <one declarative sentence>
  body: |
    <skill body, declarative voice, ≤500 lines>
  triggers: [<phrase>, <phrase>, ...]     # ≤5 entries
  tags: [<tag>, <tag>]                    # ≤4 entries
  signal_type: <copied from input>
  evidence_session: <session_id>
  evidence_turn: <turn_id>
  proposed_value_hypothesis: |
    <one paragraph: why we expect this to be valuable. Replay validator
    uses this to choose comparison tasks.>
```

NAME RULES (validated after your output):
- lowercase letters, digits, hyphens only; length 3-40
- must contain at least one CLASS TOKEN from this set:
    {review, debug, deploy, test, format, communicate, plan, search,
     research, refactor, secure, configure, ingest, transform, validate}
- must NOT contain: pr+digits, issue+digits, fix-followed-by-digits,
    year (2025-2030), "today", "tomorrow", "fix-", "audit-", "debug-"
    when followed by a session-artifact noun

VOICE RULES:
- Declarative: "Reviews use single-line comments" ✓
- NOT imperative: "Always use single-line comments" ✗
- Hermes' MEMORY_GUIDANCE applies here verbatim — see §28.1.1 of the
  Hermes deep dive. (Imperative phrasing in skills acts as a session-
  spanning directive that overrides current user intent.)
```

The prompt fronts the refusal checklist deliberately. The model has to commit to "no" before it can write "yes."

### 7.2 · Drafter model choice

Default: **Haiku 4.5** (cheap, structured-output capable). Haiku is sufficient for what the drafter does — pattern-matching the refusal checklist and writing a short skill body.

Override via config for high-stakes signals: `signal.type == "operator_initiated"` (operator typed `/learn-this`) → escalate to Sonnet. The operator paid the social cost of asking; we pay the API cost of doing it right.

### 7.3 · Structural post-validation

Drafter output is validated before being written to `.candidates/`:

```python
def validate_candidate(c: Candidate) -> Result:
    if not re.match(r"^[a-z][a-z0-9-]{2,39}$", c.name):
        return Err("name shape")
    if re.search(r"\b(pr\d+|issue\d+|fix-\d|2020[0-9]|2030|today|tomorrow)\b", c.name):
        return Err("session-artifact name")
    if not (set(c.name.split("-")) & CLASS_TOKENS):
        return Err("missing class token")
    if len(c.body) < 50 or len(c.body) > 30_000:
        return Err("body length")
    if not c.triggers or len(c.triggers) > 5:
        return Err("triggers count")
    # Skill body imperative-voice check (cheap heuristic)
    if c.body.lower().count(" always ") + c.body.lower().count(" never ") > 3:
        return Warn("imperative-voice — patch may be needed")
    return Ok()
```

Failed structural validation → re-prompt the drafter ONCE with the specific error. Second failure → discard with reason logged.

### 7.4 · Skills Guard pre-validation

L0 security pass. Port Hermes' `tools/skills_guard.py` 88-pattern scanner verbatim. Run against `c.body` + every `c.references[*]` etc. Verdict `dangerous` → discard outright; verdict `caution` → require operator confirmation in Pipeline 4 even if Pipeline 3 auto-approves.

### 7.5 · Drafter output

Survivors land in `~/.freyja/skills/.candidates/<uuid>.yaml`. Telemetry event:

```jsonl
{"ts": ..., "skill": "<proposed-name>", "event": "drafted", "candidate_id": "<uuid>", "signal_type": "correction", "evidence_session": "...", "guard_verdict": "safe"}
```

---

## 8 · Pipeline 3: Replay-Based Validation (the core innovation)

**File**: `bridge/knowledge/replay_validator.py` (~600 LOC)

This is what Hermes doesn't have and what makes our value claim defensible.

### 8.1 · Goal

For a candidate $c$, empirically estimate $V(c)$ before promoting.

### 8.2 · Comparison-task selection

Find ≤5 past sessions where $c$ would have applied. "Would have applied" means:
1. The session involves the same kind of work (semantic similarity of task description, computed via embedding of the first user message).
2. AND the candidate's `triggers` regex-match somewhere in the session's user messages OR tool calls.

The session corpus is `~/.freyja/sessions/` (we already persist transcripts there per `EVAL-HARNESS-DESIGN.md §2`). We exclude sessions younger than 24h (avoid validating against the session that produced the candidate).

```python
def find_comparison_tasks(candidate: Candidate, max: int = 5) -> list[SessionRef]:
    candidate_emb = embed(candidate.description + " " + " ".join(candidate.triggers))
    all_sessions = load_recent_sessions(window_days=60)
    ranked = []
    for s in all_sessions:
        if s.created_at > now() - 86400:
            continue
        first_user_msg = s.first_user_message()
        sim = cosine(candidate_emb, embed(first_user_msg))
        trigger_hit = any(re.search(t, s.full_text(), re.I) for t in candidate.triggers)
        if sim > 0.4 and trigger_hit:
            ranked.append((sim, s))
    return [s for _, s in sorted(ranked, reverse=True)[:max]]
```

### 8.3 · The fork

For each comparison task $t$, run TWO forks:

**Fork A — with-skill**:
- Spawn a fresh `_BridgeSession` with same model + reasoning + workspace as $t$.
- Inject candidate $c$ into the system prompt's skill listing (as if it had been the top match for $t$).
- Replay $t$'s first user message + any prior thread context.
- Let the agent run to `turn_complete` or `iteration_budget_exhausted`.

**Fork B — without-skill (control)**:
- Identical setup, candidate $c$ is **not** in the system prompt.
- Same first user message, same prior context.
- Same iteration budget.

Both forks emit normal events. We capture the resulting transcripts to `~/.freyja/skills/.replay/runs/<run-id>/{with,without}-skill.transcript.json`.

### 8.4 · Determinism handling

Replay isn't bit-deterministic because the LLM is sampling. Three mitigations:

1. **Temperature 0** on both forks. Reduces variance.
2. **Seed if provider supports it** (OpenAI does; Anthropic does not at time of writing).
3. **N=3 runs per fork**, score the median. Costs 6× per comparison-task instead of 2×, but cuts variance.

We document this loudly: replay validation is a *statistical* signal, not a proof. The promote threshold (§8.6) accounts for this.

### 8.5 · Scoring

Run the eval harness scorer (`docs/EVAL-HARNESS-DESIGN.md §5.3`) against both transcripts. Output is a `Scorecard` per fork:

```json
{
  "task_id": "...",
  "fork": "with-skill",
  "scores": {
    "trajectory_efficiency": 0.78,
    "claim_grounding": 0.92,
    "self_correction": 0.81,
    "safety": 1.00,
    "reward_hacking_flag": false
  },
  "tool_calls": 14,
  "total_cost_usd": 0.42,
  "wall_time_sec": 87
}
```

Compute $\Delta V_d(c, t) = \text{Score}_d(\text{with}) - \text{Score}_d(\text{without})$ per dimension $d$.

Average across comparison tasks:
$$\Delta V_d(c) = \frac{1}{|T_c|} \sum_{t \in T_c} \Delta V_d(c, t)$$

Compute 95% confidence interval over the per-task deltas.

### 8.6 · Decision rule

Promote candidate $c$ iff ALL of:

1. $\Delta V_{\text{safety}} \geq 0$ (no safety regression — hard gate).
2. No fork triggered the reward-hacking detector (hard gate).
3. $\sum_d w_d \Delta V_d > 0.05$ (positive net value above noise floor).
4. CI lower bound on $\sum_d w_d \Delta V_d > 0$ (statistical significance at 95%).

If passes: write candidate to `.validated/`, emit telemetry `replay_completed verdict=promote`.

If fails on hard gates: write to `.rejected/` with reason, append pattern to `.negative_library.jsonl`.

If fails on (3) or (4) only: write to `.rejected/` with `reason=marginal`, do NOT add to negative library (the candidate might be useful, we just couldn't prove it on these 5 tasks).

### 8.7 · Cost control

Replay is expensive. Each comparison task costs roughly $2× $ the original session's compute. 5 tasks × 2 forks × N=3 runs = 30× the original session cost. That's a lot.

Defenses:

- **Per-candidate budget cap**: 30× the original session's recorded `cost_usd` field. If the original session cost $0.05, replay budget is $1.50. Most candidates are cheap.
- **Per-skill lifetime budget**: every skill gets $10 of replay over its life. Includes validation, decay-refresh, patch-validation, umbrella-validation. Budget tracked in `.replay/budget.json`.
- **Cheap-replay first**: use Haiku for the agent during replay if the original session was on Sonnet. We're measuring the *skill's* effect, not the model's. Document the cross-model assumption in the scorecard.
- **Massive prompt-cache reuse**: replay reuses the original system prompt verbatim (modulo the skill listing). Anthropic's prefix cache cuts ~70% off the cost of replays.
- **Early-stop**: if after 2 tasks the with-fork has measurably regressed on safety, stop the run.

Total budget for the replay pipeline at full operation: ~$2-5 per candidate, ~$50-100/month for a heavy user. Defensible.

### 8.8 · Output

```jsonl
{"ts": ..., "skill": "<name>", "event": "replay_started", "candidate_id": "...", "run_id": "rep-001", "tasks": ["sess-A", "sess-B", "sess-C"]}
{"ts": ..., "skill": "<name>", "event": "replay_progress", "run_id": "rep-001", "task": "sess-A", "delta_v": {...}}
{"ts": ..., "skill": "<name>", "event": "replay_completed", "run_id": "rep-001", "verdict": "promote", "delta_v": {"trajectory": 0.31, "grounding": 0.04, "safety": 0.0}, "ci_95": [0.05, 0.49], "cost_usd": 1.47}
```

---

## 9 · Pipeline 4: Operator Confirmation

**File**: `bridge/knowledge/confirmation_router.py` (~250 LOC) + UI work.

Replay-validated candidates land in `~/.freyja/skills/.validated/`. The operator sees a non-modal notification.

### 9.1 · Desktop UI

A toast at the bottom of the active session:

```
┌─────────────────────────────────────────────────────────────────┐
│ 💡 Learned: code-review-feedback-style                          │
│                                                                  │
│ You prefer terse single-line review comments.                    │
│                                                                  │
│ Validated on 5 past tasks:                                       │
│   trajectory efficiency:  +0.34   (CI: +0.05 to +0.49)          │
│   claim grounding:        +0.02                                  │
│   safety:                  ±0.00                                 │
│   reward-hacking flag:    none                                   │
│                                                                  │
│ Triggers when: "code review", "PR feedback", "review this"      │
│                                                                  │
│ [ Promote ]   [ Edit & Promote ]   [ Discard ]   [ Why? ]       │
└─────────────────────────────────────────────────────────────────┘
```

- **Promote** → move file from `.validated/` to `<name>/SKILL.md`, set `confidence: verified` (it earned it through replay), emit promoted event.
- **Edit & Promote** → open in `$EDITOR`, then promote. Edits are linted on save.
- **Discard** → move to `.rejected/`, append signal pattern to `.negative_library.jsonl` with `actor=operator`.
- **Why?** → opens replay scorecard with side-by-side transcripts of with/without forks.
- **No action in 24h** → auto-promote IF `delta_v_scalar > 2σ` AND `safety == 0` AND `reward_hacking_flag == false`. Otherwise auto-discard with `reason=stale-validation`.

### 9.2 · Slack flow

Block Kit DM to the operator (not in the channel):

```
💡 Freyja learned a new skill from your thread #channel-name

Name: code-review-feedback-style
Description: You prefer terse single-line review comments.

Validation:
  trajectory efficiency: +0.34
  safety: ±0.00
  reward-hacking: clean

[ Promote ]   [ Discard ]   [ View detail (DM) ]
```

The "View detail" button DMs the full replay scorecard. Same 24h auto-promote rule.

### 9.3 · Operator-initiated path

Slash command in any session: `/learn-this [name]` or `/learn-this` (auto-name).

When fired:
- Synthesize a `OperatorInitiatedSignal` for the most recent assistant turn.
- Skip the refusal checklist in the drafter.
- Run Pipeline 3 anyway (replay validation still useful).
- Auto-promote on success — the operator already endorsed.

### 9.4 · Telemetry

```jsonl
{"ts": ..., "skill": "<name>", "event": "promoted", "actor": "operator|auto", "candidate_id": "..."}
{"ts": ..., "skill": "<name>", "event": "discarded", "actor": "operator|auto", "candidate_id": "...", "reason": "..."}
```

---

## 10 · Pipeline 5: Outcome Watcher (continuous)

**File**: `bridge/knowledge/outcome_watcher.py` (~200 LOC)

For every turn where a skill was loaded, watch the next ≤3 turns or until end-of-session for outcome signals.

### 10.1 · Outcome categories

Per loaded skill, classify the post-load period:

| Outcome | Definition |
|---|---|
| `clean` | No correction signals; task completed (either user said "thanks" / similar OR turn ended with `success=true`). |
| `correction` | A correction signal fired against an action that the loaded skill *would have governed*. |
| `error_loop` | ≥3 consecutive tool errors of the same family AND skill triggers should have warned of this. |
| `irrelevant` | Skill was loaded but its triggers don't fire on the actual subsequent work. (Indicates over-broad triggers.) |
| `helpful` | An agent action in the post-load period explicitly references the skill (e.g. "Per code-review-feedback-style, …"). |

The classifier is partly deterministic (correction signals reuse Pipeline 1), partly LLM-judged for `clean` vs `irrelevant` (a Haiku call at session-end can read the relevant slice). Cost: ~$0.01 per loaded skill per session.

### 10.2 · Outcome event

```jsonl
{"ts": ..., "skill": "code-review-style", "event": "outcome", "session_id": "...", "load_event_ts": ..., "outcome": "clean", "evidence": {"turns_examined": 2}}
```

### 10.3 · Rolling stats

The `.value/<skill>.json` rollup is updated:

- `rolling_30d_outcome_distribution`
- `rolling_30d_correction_rate`
- `rolling_30d_correction_rate_slope` (linear regression over last 30 days)
- `rolling_30d_irrelevance_rate` (suggests trigger over-fit)

These feed the Decay Model (Pipeline 6) and the Patch Proposer (Pipeline 7).

---

## 11 · Pipeline 6: Decay Model (daily cron)

**File**: `bridge/knowledge/decay_model.py` (~300 LOC)

Hermes' decay is `last_activity > 30d → stale, > 90d → archive`. That's not "decay" — that's "garbage collection." Real decay is when a skill *was useful* but *isn't anymore*.

### 11.1 · The decay score

For each skill $s$, compute:

$$D(s) = w_1 \cdot \text{time}(s) + w_2 \cdot \text{outcome\_drift}(s) + w_3 \cdot \text{env\_drift}(s) + w_4 \cdot \text{replacement\_pressure}(s)$$

Each factor is normalized to $[0, 1]$:

**`time(s)`**: days since `last_positive_outcome_at`, normalized by a half-life (default 60 days for `verified` skills, 14 days for `experimental`).

**`outcome_drift(s)`**: $\max(0, \text{rolling\_30d\_correction\_rate}(s) - 0.2)$. Skills that have been correctly fired produce <20% correction rate; above that is drift.

**`env_drift(s)`**: see §11.2 — checks whether code paths / API calls / tool names the skill references still exist.

**`replacement_pressure(s)`**: $\max_{s' \in \text{rivals}(s)} V_{\text{scalar}}(s') - V_{\text{scalar}}(s)$, clipped at $[0, 1]$. Rivals are skills with cosine-similar triggers and overlapping fingerprints.

Default weights: $w = (0.3, 0.4, 0.2, 0.1)$. Outcome drift is the biggest signal because it's the closest to ground truth.

### 11.2 · Environment drift detection

Skills sometimes reference codebase specifics that drift: file paths that get renamed, function names that change, APIs that get deprecated. Detection:

1. At promotion time, extract code-fragment references from SKILL.md (regexes for paths, function calls, env vars, command snippets).
2. Store as `~/.freyja/skills/<name>/.references.json`.
3. Weekly: re-check each reference. Path doesn't exist anymore? Function not found via `grep`? CLI command not available?
4. Count of broken references → drift score.

This is cheap: filesystem checks + grep. No LLM call.

### 11.3 · Decay-triggered actions

Per-skill decay score $D(s)$ thresholds:

- $D < 0.3$: no action.
- $0.3 \leq D < 0.6$: schedule a **refresh replay** — small Pipeline 3 run (1-2 tasks, NOT 5) on RECENT sessions matching the skill's triggers. If $\Delta V$ stays positive → reset decay timer; if not → demote to `experimental`.
- $0.6 \leq D < 0.8$: demote to `experimental` regardless. Surface to operator: "Skill X may have decayed. Run refresh test?"
- $D \geq 0.8$: archive (move to `.archived/`). Never delete. Recoverable via `freyja skill restore <name>`.

### 11.4 · Reactivation

If an archived skill's triggers fire on a future session and the operator says "I want this skill back," restore from `.archived/`. Hermes has a similar restore command; port the surface.

### 11.5 · Telemetry

```jsonl
{"ts": ..., "skill": "<name>", "event": "decay_check", "score": 0.45, "factors": {"time": 0.4, "outcome_drift": 0.5, "env_drift": 0.2, "replacement": 0.0}}
{"ts": ..., "skill": "<name>", "event": "decay_refresh_replay_scheduled", "score": 0.45}
{"ts": ..., "skill": "<name>", "event": "demoted", "from": "verified", "to": "experimental", "reason": "decay"}
```

---

## 12 · Pipeline 7: Patch Proposer (improvement via signal + replay)

**File**: `bridge/knowledge/patch_proposer.py` (~350 LOC)

Hermes' background review allows patches but only as part of the "save what was learned" pass — it's a side-effect of the broader review. We make patching a first-class continuous improvement loop with replay validation.

### 12.1 · When patching fires

Either:
1. A skill was loaded AND the subsequent turns produced a `correction` outcome (Pipeline 5). The correction is targeted at behavior the skill *should have* prevented or governed.
2. A skill failed `env_drift` checks (Pipeline 6.2) — patch can update path references.

### 12.2 · Patch drafting

An LLM (Sonnet) is given:
- Current `SKILL.md` content.
- The corrective signal (specific user message + surrounding context).
- Instruction:

```
You're improving an existing skill. The skill was loaded in a recent session
and the user then corrected the agent's behavior in a way the skill should
have prevented.

Current skill body:
{skill_body}

User correction:
{evidence}

Propose a MINIMAL patch:
- Add one section, or extend one existing section, or add one pitfall bullet.
- Do NOT rewrite the whole skill.
- Do NOT remove existing guidance unless it directly contradicts the correction.
- Output the FULL patched SKILL.md (the patcher will diff and apply).

Voice: declarative, not imperative (per skill voice rules).

If the correction is actually about session-specific state, environment, or
one-off task narrative, output "NO_PATCH" — not every correction warrants
a skill change.
```

### 12.3 · Patch validation

Crucially: **never apply a patch without replay-validating it**.

For each patch $p$ to skill $s$:
1. Take ≤3 *past* sessions where $s$ was loaded successfully (from `.events.jsonl` `outcome: clean` events).
2. Run Pipeline 3 with two forks: `s_old` vs `s_patched`.
3. Promote $s_{\text{patched}}$ iff:
   - $\Delta V \geq 0$ (no regression on tasks $s$ was already handling).
   - $\Delta V \geq 0.05$ on the specific task that triggered the patch.

The first condition prevents "fixed today, broke yesterday." The second confirms the patch actually addresses the trigger.

### 12.4 · Patch history

Pre-patch SKILL.md is preserved in `<skill>/.history/<ts>-patch.md`. Telemetry:

```jsonl
{"ts": ..., "skill": "<name>", "event": "patch_proposed", "version_from": 1, "trigger": "correction", "session_id": "..."}
{"ts": ..., "skill": "<name>", "event": "patch_replay_completed", "delta_v_baseline": {...}, "delta_v_new_task": {...}, "verdict": "apply|reject"}
{"ts": ..., "skill": "<name>", "event": "patched", "version_from": 1, "version_to": 2}
```

### 12.5 · Patch rate limits

Same skill can be patched at most once per 7 days. Prevents thrashing. Operator override via `/skill repatch <name>`.

---

## 13 · Pipeline 8: Umbrella Proposer (consolidation via replay)

**File**: `bridge/knowledge/umbrella_proposer.py` (~400 LOC)

Hermes' curator runs every 7 days, scans for prefix clusters, and uses LLM judgment to consolidate. Our version uses replay to **empirically verify** that the umbrella works.

### 13.1 · Cluster detection

Monthly cron. Detect skill clusters by:
1. Cosine similarity of trigger embeddings $> 0.7$.
2. AND co-load rate $> 30\%$ (per `.events.jsonl`, when skill A loads in a session, how often does skill B also load?).

Both signals are required. Pure lexical similarity (Hermes' "prefix cluster") is too loose; co-load is the behavioral signal.

### 13.2 · Umbrella drafting

For each cluster $\{s_1, ..., s_n\}$, an LLM (Opus, this matters — synthesis quality is the bottleneck) is given:

- All member SKILL.md files.
- All member triggers + tags.
- Co-load stats.
- Instruction (verbatim from Hermes' curator prompt with modifications):

```
You're proposing an umbrella skill that would replace these N narrow skills.

Members:
{members_dump}

Cluster stats:
- Average pairwise trigger similarity: {sim}
- Co-load rate: {co_load_pct}%
- Combined load history: {n_loads} over {n_sessions} sessions

Goals:
1. The umbrella SKILL.md should cover the union of what these skills cover.
2. Each member's UNIQUE insight gets its own labeled subsection.
3. The umbrella's name MUST be at the class level (no PR numbers, no codenames).
4. Total length ≤ longest member × 1.5.

Do NOT propose an umbrella if:
- The members' guidance contradicts (e.g. one says "always X", another "never X")
- The members serve genuinely distinct contexts that share lexical surface but differ semantically.

In either case, output "NO_UMBRELLA" and explain.
```

### 13.3 · Umbrella validation

For the proposed umbrella $u$:
1. Compute $T_u = T_{s_1} \cup ... \cup T_{s_n}$ (union of historical tasks each member handled).
2. Sample $\leq 8$ tasks from $T_u$ (proportional to per-member representation).
3. Run Pipeline 3 with three-way comparison:
   - Fork A: original member set (each loaded by relevance).
   - Fork B: umbrella only.
   - Fork C: nothing (control).
4. Compute $V(u)$ and $\max_i V(s_i)$ per task.

Promote umbrella iff:
- $V(u) \geq \max_i V(s_i) - \text{tolerance}$ on every task (no regression).
- $V(u) > V(\text{nothing})$ across the set (still better than no skill).
- No safety regression.
- No reward-hacking flag.

### 13.4 · Member archival

On umbrella promotion:
- Move each member's directory to `.archived/` with `absorbed_into: <umbrella>` in metadata.
- Update the umbrella's frontmatter with `absorbs: [<member1>, ...]`.
- Append events for each member: `archived reason=umbrella-absorbed`.

This is the only "destructive-by-default" pipeline. Operator notification IS required (not just optional):

```
🔀 Umbrella proposed: code-review (absorbs: 4 narrow skills)
   Replay tested on 8 historical tasks:
     V(umbrella) median: +0.38
     V(best member) median: +0.41
     Regression on 1 task (trajectory_efficiency -0.03)
   
   Members to be archived:
     • pr-review-style
     • code-review-feedback-style
     • inline-comment-conventions
     • test-review-checklist
   
   [ Approve ]   [ Reject ]   [ View detail ]
```

24h auto-approve threshold: `V(u) ≥ max_i V(s_i)` (strictly, no tolerance) AND no regressions. Anything weaker requires explicit operator click.

### 13.5 · Reversibility

Members live in `.archived/`. Rollback is `freyja skill restore <name>` per member. No tar.gz, no pre-mutation snapshot — the member's whole directory is still on disk, just hidden.

---

## 14 · The value function in full

### 14.1 · Score weights

Default weight vector:

```yaml
v_weights:
  trajectory_efficiency:  0.30
  claim_grounding:        0.25
  self_correction:        0.20
  safety:                 0.20    # any negative value → hard archive
  reward_hacking:         0.05    # binary flag, not a score; non-zero → reject
```

Operator-overridable in `~/.freyja/config.yaml`. Different teams care about different things.

### 14.2 · Hard gates (any non-trivial pipeline must respect)

1. **Safety**: any candidate / patch / umbrella with $\Delta V_{\text{safety}} < 0$ is rejected. Period.
2. **Reward hacking**: any candidate that triggers the reward-hacking detector during replay is rejected and pattern-added to negative library.
3. **Skills Guard verdict**: `dangerous` → rejected; `caution` → operator-confirm only (never auto-promote).

### 14.3 · Value freshness

$V$ rollups have a freshness timestamp. Promotion uses the most recent $V$. Decay model uses rolling-30-day. The ranker (§15) prefers freshness within 30 days; older $V$ values get displayed but flagged with `~`.

### 14.4 · Cross-model concerns

If $V$ was measured on model M1 and current session is on M2, value may not transfer. Default behavior: store $V$ per-model in `.value/<name>.json`:

```json
{
  "v_by_model": {
    "claude-sonnet-4-6": {...},
    "claude-haiku-4-5": {...}
  },
  "v_default": "claude-sonnet-4-6"
}
```

Ranker uses the current session's model's $V$ if available, else `v_default`.

---

## 15 · Skill ranking in the system prompt

**File**: `bridge/knowledge/skill_store.py:build_prompt` (modify existing ~50 LOC)

The system prompt's skill listing IS the consolidation surface. The model only ever knows skills that show up here unless it explicitly calls `search_skills`.

### 15.1 · Render-time ranking

```python
def build_prompt(self, query: str = "", *, limit: int = 12) -> str:
    skills = self.list_skills()
    skills = [s for s in skills if not s.archived]
    # Filter to relevant if query is present
    if query:
        skills = self.search(query, limit=limit * 2)
    # Sort by V_scalar (per current model) descending; ties broken by:
    #   verified > experimental > unvalidated
    #   then by recency
    def rank_key(s):
        return (
            -self._v_scalar_for_current_model(s),
            -confidence_rank(s.confidence),
            -s.updated_at,
        )
    skills.sort(key=rank_key)
    return self._render(skills[:limit])
```

### 15.2 · Render format

Make the model aware of the empirical evidence:

```
## Available Skills

Call `load_skill(name)` to load full instructions.

- code-review-feedback-style [verified · V=+0.34 over 5 replays · 23 loads, 87% clean]:
  User prefers terse single-line review comments.
- debug-async-python [verified · V=+0.18 · 12 loads, 75% clean]:
  Workflow for diagnosing asyncio coroutine leaks.
- *experimental:* slack-thread-summarize [V=+0.06 (CI: -0.02 to +0.14) · 3 loads]:
  Format for summarizing long Slack threads.
- ~stale~ legacy-pylint-config [last positive use 47 days ago]:
  Pre-Ruff lint configuration. Consider verifying still applies.
```

The status tags + numbers tell the model *why* it should or shouldn't trust each skill. A skill with `87% clean` over 23 loads is a more credible recommendation than one with `60% clean` over 3.

### 15.3 · Token budget

`limit=12` keeps the system prompt skill section under ~3kb. Skills past rank 12 are not shown — they're discoverable via `search_skills`. This is the consolidation — without curator-driven archival, render-time pruning bounds the prompt.

---

## 16 · Guardrails (layered)

The guardrail set is layered with explicit responsibility per layer. Each one defends against a specific failure mode the prior layer might miss.

| Layer | Guardrail | Defends against |
|---|---|---|
| L0 Detection | Signal cooldown (1× per type per turn) | Frustrated user generating duplicate signals |
| L0 Detection | Negative-library precheck | Re-proposing already-rejected patterns |
| L1 Draft | Refusal checklist in prompt | LLM bias toward saving |
| L1 Draft | Structural name validator | "fix-pr-12345" skills |
| L1 Draft | Imperative voice heuristic | Skills that act as session-spanning directives |
| L1 Draft | Skills Guard 88-pattern scan | Malicious content from training-data leakage |
| L2 Validate | Replay V hard gates (safety, reward-hacking) | Skills that help score but hurt outcomes |
| L2 Validate | Replay statistical significance | Skills that look good on 1-2 tasks but won't generalize |
| L3 Confirm | Operator confirmation for caution-verdict | Borderline cases that benefit from human review |
| L3 Confirm | 24h timeout = auto-discard for marginal | Defaults toward "no" not "yes" |
| L4 Use | Confidence-tagged rendering in prompt | Model can weigh skill weight against its own judgment |
| L4 Use | Per-model $V$ tracking | Cross-model value claims that don't hold |
| L5 Outcome | Outcome classification post-load | Detect skills that load but don't help |
| L5 Outcome | Rolling correction-rate slope | Detect degradation BEFORE catastrophic failure |
| L6 Decay | Env-drift physical-state checks | Skills referencing dead paths/APIs |
| L6 Decay | Replacement-pressure detection | Stale skills outranked by newer alternatives |
| L7 Patch | Patch must non-regress on past clean tasks | "Fixed today, broke yesterday" |
| L7 Patch | 7-day rate limit per skill | Thrashing |
| L8 Umbrella | Umbrella must non-regress on every member task | Lossy consolidation |
| L8 Umbrella | Operator confirmation required (no auto-promote without strict gates) | Destructive cluster collapse |
| L9 Cross | Reward-hacking detector applied to EVERY replay | Gameable replay scores becoming gameable promotions |

The architecture is intentionally redundant. Skipping any one layer leaves the system surviving, but the more layers you remove the more failure modes leak through.

---

## 17 · Cost model

### 17.1 · Per-event costs (estimated)

| Pipeline | Trigger frequency | Cost per fire | Daily cost (heavy user) |
|---|---|---|---|
| Signal detector | every turn | $0 | $0 |
| Drafter (Haiku) | ~5/day | ~$0.005 | $0.025 |
| Replay validator | ~3/day | ~$1.50 | $4.50 |
| Operator confirmation | 0 cost (UI) | $0 | $0 |
| Outcome watcher | per loaded skill | ~$0.01 | $0.20 |
| Decay model | daily | filesystem + ~$0 LLM | $0 (refresh replays: ~$1/day amortized) |
| Patch proposer | ~1-2/day | ~$0.50 | $1.00 |
| Umbrella proposer | monthly | ~$5/run | $0.17/day |

**Heavy-user daily total**: ~$7. **Light user (5 turns/day)**: ~$0.50/day.

These are operational costs — paid by the human operator's API key or company billing. They're justifiable: the system measurably reduces future task cost (V is positive on average) and prevents skill-pollution that would have cost more in re-correction over time.

### 17.2 · Replay budget mechanics

Each candidate gets:
- $0.50 initial budget if originating from a regular signal.
- $2.00 if originating from `/learn-this` (operator explicitly invested).
- Lifetime $10 across all replays (validation, decay refreshes, patch validation, umbrella validation).

Exceeded budget → operator approval required to continue ("This skill has consumed $10 of replay budget. Continue spending?").

### 17.3 · Cost dashboards

Telemetry events feed a per-skill cost rollup in `.value/<name>.json`. The desktop renders a "skill economics" view: per-skill cost spent, $V$ delivered, "ROI" (this is a rough proxy at best — skills that reduce 100 future corrections-of-$0.05 each pay for themselves at $5 of validation cost).

---

## 18 · Integration with the eval harness

This loop is parasitic on `EVAL-HARNESS-DESIGN.md`. Specifically:

| Skill loop component | Eval harness component used |
|---|---|
| Replay validator scoring | §5.3 three-layer grader stack |
| Reward-hacking flag | §5.7 reward-hacking detector |
| Outcome classifier | §6 metrics catalog (claim grounding, self-correction) |
| Value vector $V$ | §6 mode-agnostic core scorers |
| Comparison-task selection | §2 normalized trace records |
| Replay fork mechanism | §5.2 live + replay primitives |

If eval harness lands ahead of skill learning (it should — it's the substrate), skill learning takes ~6 weeks to build on top. If skill learning lands first, it has to invent stubs of the eval harness — defer.

The reverse is also true: this design feeds back into eval-harness work. Specifically, the replay scoring needs to be cheaper than initially designed for skill-learning use cases to be tractable. We should target `EVAL-HARNESS-DESIGN.md §5.3 Layer 1 (deterministic grader)` to handle ~80% of replay scoring without an LLM call.

---

## 19 · Failure modes catalogue

Known failure modes for the system as designed, and how each is defended:

| Failure mode | Defense |
|---|---|
| Drafter saves narrow skills (`fix-pr-X`) | Structural name validator (L1) |
| Drafter saves negative tool claims | Refusal checklist (L1) + negative library (L0) |
| Drafter saves environment-specific gotchas | Refusal checklist (L1) + post-validation env-drift check (L6) |
| Replay shows positive V but skill harms production | Reward-hacking detector (L2) + outcome watcher (L5) catches it |
| Operator never reviews; library bloats | 24h auto-discard for marginal candidates (L3); render-time pruning (§15) |
| Skill content drifts (codebase changes) | Env-drift detector (L6.2) |
| Two skills conflict (one says X, other says ¬X) | Umbrella proposer's "NO_UMBRELLA on contradiction" rule + operator surfaces |
| Skill is loaded for the wrong task (false trigger) | Outcome `irrelevant` classification → trigger refinement signal |
| Replay budget overrun | Lifetime $10 cap + operator approval required to continue |
| Cross-model V transfer assumption fails | Per-model V tracking (§14.4) |
| Imperative-voice skill overrides current user intent | Drafter voice check (L1) + MEMORY_GUIDANCE-style rule in drafter prompt |
| Sub-agent uses parent's loaded skills inappropriately | Sub-agent's skills are filtered by sub-agent's agent_type; outcome watcher attributes outcomes per-skill (independent of parent) |
| Skill gets "patched" with operator's correction that was actually about session state | Patch proposer prompt explicitly checks for session-specific vs class-level (L7) |
| Curator-style consolidation breaks cron job skill refs | Umbrella proposer outputs `absorbs: [...]` metadata; cron job runner rewrites `skills:` field on detection |
| Adversarial user tries to teach malicious skills via long-running session | Skills Guard scan at every write (L1) catches threat patterns deterministically |

---

## 20 · Phased implementation

### Phase 0 — Foundation (assumed shipped)

- Eval harness §5 grader stack (live + replay paths).
- Eval harness §5.7 reward-hacking detector.
- Per-session transcripts persisted to disk (already exist).

### Phase 1 — Observation only (~2 weeks, ~600 LOC)

- Pipeline 1 (signal detector). Writes to `.signals.jsonl` only.
- Pipeline 5 (outcome watcher). Writes to `.events.jsonl`.
- Renderer event: surface "Signal detected" in the desktop activity panel for operator visibility.
- **No drafting yet.** This phase tells us if our signal detection is accurate by giving operators a window onto what would have been candidates.

Exit criterion: operator agrees with ≥70% of detected signals in spot-checks.

### Phase 2 — Drafting + manual promotion (~3 weeks, ~800 LOC)

- Pipeline 2 (drafter).
- Pipeline 4 (confirmation UI), but no auto-promote.
- Skills Guard scanner ported from Hermes.
- Operator must explicitly approve every promotion.

Exit criterion: ≥80% of drafts get promoted by operator (means drafter quality is high enough).

### Phase 3 — Replay validation (~4 weeks, ~1200 LOC)

- Pipeline 3 (replay validator).
- Pipeline 4 auto-promote on validated + 24h timeout.
- Per-skill $V$ tracking in `.value/`.
- System-prompt ranking by $V$ (§15).

Exit criterion: $V$ measurements correlate (Pearson > 0.5) with operator's "kept this skill / discarded this skill" decisions over a 30-day window. Loose correlation, but enough to trust the metric for auto-promote decisions.

### Phase 4 — Continuous improvement (~4 weeks, ~1000 LOC)

- Pipeline 6 (decay model).
- Pipeline 7 (patch proposer).
- Env-drift detection.
- Refresh replays.

Exit criterion: rolling-30-day average $V$ across the live skill set is non-decreasing over a 60-day window.

### Phase 5 — Consolidation (~3 weeks, ~600 LOC)

- Pipeline 8 (umbrella proposer).
- Cluster detection.
- Operator surfaces for umbrella review.

Exit criterion: skill count plateaus despite continued promotion (means umbrella absorption is keeping up).

### Phase 6 — Hardening + telemetry surface (~2 weeks)

- Per-skill cost dashboards.
- Operator-facing UI for $V$ history per skill.
- Slack flow for confirmation.
- Manual override commands (`/skill restore`, `/skill repatch`, etc.).

Total: ~18 weeks, ~4200 LOC. Larger than Hermes' 10,500 LOC system measured in "completed features delivered" but each line is doing measurable work toward making skills better. We're not trying to be smaller — we're trying to be defensible.

---

## 21 · Open questions

1. **Cross-session embedding store**. Comparison-task selection (§8.2) needs embeddings for past sessions. Build a vector store, or compute embeddings on-the-fly per candidate? On-the-fly is simpler; vector store is faster for heavy users. Defer to Phase 3 measurement.

2. **Replay determinism on Anthropic models**. Anthropic doesn't expose a seed parameter. N=3 medians is the best we have. Is that variance acceptable for our promotion threshold? Test in Phase 3.

3. **Operator-initiated `/learn-this` semantics with no clear evidence**. If the operator triggers it on a turn that contained no detected signal, what does the drafter use as evidence? Default: the entire previous assistant turn becomes the "evidence." Acceptable.

4. **Per-workspace vs global skills**. Hermes has `external_dirs` for project-local skills. We currently use `~/.freyja/skills/` only. Skill that says "use ruff for this project" might be globally wrong. Defer per-workspace scoping to Phase 5+; tag with `metadata.workspace` initially.

5. **Streaming-skill mode (sub-agents)**. When a sub-agent loads a skill, whose $V$ does it count toward? Probably both (parent + sub-agent). Need to think about whether sub-agent outcomes should be weighted differently.

6. **Privacy of replay**. Comparison tasks are pulled from the operator's session history. If a candidate originated from session A and gets replayed against session B, the model running replay sees session B's content. Operator should be able to mark sessions as "do not use for replay." Defer to Phase 4.

7. **The `agent-created` provenance carve-out**. Hermes' curator only touches `agent-created` skills, leaving user-authored ones immune. Do we want the same? Initial answer: yes — our auto-pipelines never touch skills with `created_by: operator`. Operator can opt their own skills *into* curation if they want.

8. **What about memory?** This doc focuses on skills. Memory has its own gold-standard ("user said preference X") that doesn't need replay validation — preferences are facts, not procedures. Memory loop is simpler: detect preference signals → ask operator → save. No $V$ involved. Worth a separate small doc.

9. **Detecting reward-hacking IN A SKILL** (not just in the replay). What if a skill teaches the agent how to game judge scores? The reward-hacking detector running on each replay catches this for *that replay*, but a skill that's adversarially constructed by a future-model-with-bad-prior could still slip through. Phase 4+ research direction.

10. **Multi-operator semantics**. If two operators share a Freyja install, who confirms candidates? Defaults to first-to-act. Real multi-operator support is out of scope.

---

## 22 · The summary

We are not trying to build a smaller version of Hermes. We are trying to build a system where the question "is this skill actually helping?" has a numerical answer.

The system reuses Hermes' best ideas — the load-bearing prompts, the skill-guard scanner, the tool-whitelist enforcement, the cached-prefix forking — and replaces every place where Hermes uses LLM judgment as the final arbiter with replay-based empirical measurement.

The architecture is more expensive to run per-candidate (~$1.50 per replay vs ~$0.10 per Hermes review). It's also more expensive to *build* — eight pipelines, multi-dimensional value vector, decay modeling. But every line does measurable work, and the operational guarantee at the end is something Hermes can't provide: **every active skill in the library has, at some point in its life, been empirically demonstrated to improve agent outcomes**. And if it stops improving them, the decay loop detects that and demotes.

That's what makes the difference between "self-improving" as a marketing line and "self-improving" as a defensible operational property.
