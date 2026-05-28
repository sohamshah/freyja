# Phase 3 plan: judge as a real subagent

Working draft. Promotes the `deep` judge profile from a one-shot LLM call
into a real subagent that can use tools to verify the agent's claims.
Quick and standard profiles stay inline one-shot for cost control.

The plan is grounded in the existing subagent infrastructure — survey of
`bridge/tools/sub_agent_tool.py`, `bridge/tools/agent_types.py`, and the
renderer's subagent rendering surfaces. No new infrastructure to build;
we're plugging into rails that already carry `verify`, `explore`, etc.

---

## What we're doing

  · Define `judge-deep` as an AgentType (or three, see open question #1
    below) with model, thinking budget, tool allowlist, system prompt,
    and max_iterations.
  · Replace the `deep`-branch of `_judge_goal` with a `sub_agent` spawn
    call so the judge runs through the same runner as `verify` /
    `explore` / `code`. Same event scoping (tool calls tagged with the
    child sessionId), same telemetry (cost / tokens / tools_called), same
    persistence (child transcript saved separately).
  · Extend `GoalBrief` with `judge_tools` (allowlist) and
    `judge_max_iterations`. Surface in the JudgeBrief modal as capability
    toggles + a small slider.
  · Plumb judge tool calls into the per-turn TurnCard's reserved "Judge
    inspected" slot. They render inline alongside the verdict prose.
  · Operator can click the "Judge inspected" header to open the judge's
    full subagent session (same UX as clicking any subagent record).

## What we're explicitly not doing

  · **Not** promoting `quick` or `standard` to subagents. They stay
    inline one-shot calls. Cost control — most turns get the cheap path.
  · **Not** adding multi-judge mode (gate + critic). That's Phase 4, only
    after Phase 3 has been observed in practice.
  · **Not** giving the judge mutating tools (`bash`, `write_file`,
    `run_tests`, anything that touches state). Read-only by default; see
    open question #2.

---

## Architecture (grounded in survey findings)

### Spawn path

The judge becomes a `sub_agent` invocation. In `_judge_goal`, the `deep`
branch dispatches to:

```python
record = await sub_agent_tool.execute({
    "label": f"judge · deep · turn {goal.turns_used}",
    "task": prompt,                  # the existing GOAL_JUDGE_USER_TEMPLATE
    "agent_type": "judge-deep",       # NEW — defined in agent_types.py
    "mode": "foreground",             # block on completion; we need the verdict
})
verdict_text = record.result
verdict = parse_goal_verdict(verdict_text)
```

Foreground mode blocks the parent (the bridge) until the judge finishes,
which matches the current semantics — `_maybe_continue_goal` already
awaits `_judge_goal` synchronously.

### AgentType definition

Add to `bridge/tools/agent_types.py` (alongside `verify`):

```python
JUDGE_DEEP = AgentType(
    name="judge-deep",
    description="Skeptical structured judge for goal-mode verification.",
    usage_hint="Goal mode auto-invokes this each turn when profile=deep.",
    model=ModelPolicy.PREFER_PARENT,  # same as the agent
    thinking_effort="high",
    tool_include=frozenset({"read_file", "grep", "glob", "fetch_url"}),
    tool_exclude=frozenset(),
    system_prompt=GOAL_JUDGE_SYSTEM_PROMPT,  # already exists
    max_iterations=3,                  # 3 turns of judge reasoning + tool use
    source="builtin",
)
```

The `verify` profile is the closest existing analog and tells us this
shape works.

### Tool allowlist

Per profile, default allowlist:

| Profile  | model        | thinking | tools                                  | iters |
|----------|--------------|----------|----------------------------------------|-------|
| quick    | Haiku 4.5    | none     | none (one-shot)                        | 1     |
| standard | parent       | none     | none (one-shot)                        | 1     |
| deep     | parent       | high     | read_file · grep · glob · fetch_url    | 3     |

`quick` and `standard` keep the current ad-hoc Session.create path; only
`deep` goes through `sub_agent_tool`.

The brief's `judge_tools` field (new) lets the operator narrow the
allowlist below the default. Format: `string[]`. Empty array means "no
tools — make deep behave like standard but with thinking on." Missing
field means "use profile default."

### Event tagging

Already handled by the existing subagent infrastructure. Tool calls fire
during the judge's run, get tagged with the judge's child sessionId via
`wrap_registry`, and stream out as `tool_use_start` / `tool_input_delta` /
tool results. The renderer already routes these to a child session view.

For the goal-mode timeline (where we want to show "Judge inspected: read
3 files, ran 2 web fetches" inline in the verdict card), we read from the
child session's `toolsCalled` aggregate or scan the child's tool_call
events. Either works.

### Persistence

Already correct. The judge's child transcript saves to
`~/.freyja/sessions/{judge_session_id}.transcript.json`. Resumable like
any subagent. Telemetry rows (`llm_call_metric`, `tool_call_metric`)
already include session_id and agent_type.

The goal-mode sidecar (`.goal.json`) doesn't need to change — it still
holds goal_state / goal_brief / verdict_history. The judge's transcript
is found via the verdict event's child sessionId (we should add that to
the verdict payload — see open question #6).

### Verdict contract — unchanged

The judge subagent still returns the same JSON: `{done, confidence,
reason, criteria, openQuestions}`. No renderer changes for the verdict
shape. The criteria-delta visualization keeps working as-is.

### UX: where the judge shows up

  · **In the goal-mode timeline**: every verdict card already has a
    reserved "Judge inspected" section. For deep verdicts, it populates
    with the judge's tool calls (compact list: `read_file · 3`,
    `fetch_url · 2`, etc.). Click the header to expand into a full tool
    transcript or jump to the child session.
  · **In the agents list / swarm grid**: yes, the judge appears as a
    peer subagent while it's running, like `verify` does today. Tagged
    with `agent_type = "judge-deep"`.
  · **Cost / token attribution**: the judge's tokens roll up into the
    parent session's totals (consistent with how subagents are accounted
    today). We surface judge-specific cost in the verdict card so the
    operator can see "this verdict cost $0.18."

---

## Brief schema additions

`GoalBrief` (Python) and matching TS type:

```python
judge_tools: list[str] = field(default_factory=lambda: [
    "read_file", "grep", "glob", "fetch_url"
])
# Hard ceiling on iterations per judge call. Bounded [1, 10].
judge_max_iterations: int = 3
# Soft USD ceiling per judge call. None = no cap; operator picks profile
# and lives with the cost. See open question #5.
judge_cost_cap_usd: float | None = None
```

Default tools chosen so the judge can verify file contents, hunt for
references, and confirm cited URLs — covers most goal types (code
verification, research) without giving it the power to mutate state.

UI surface in JudgeBrief modal, new section `§ 0.5 Judge capabilities`:
  · 4 capability toggles (Read files, Grep / Glob, Fetch URLs, Search)
    — disabled when profile != deep
  · A small slider for max iterations (1–10, default 3)
  · Cost cap textbox (optional)

---

## Open design questions

These are the forks I want your call on before I touch code.

### Q1. AgentType-as-registry vs. inline AgentType at spawn time

**Option A** — register `judge-deep` (and maybe `judge-quick`,
`judge-standard`) in `AGENT_TYPES` alongside `verify`/`code`/etc.

**Option B** — keep `judge_profile` as the operator-facing field and
build an AgentType *inline* at spawn time, not in the global registry.

I lean **Option B**. The judge isn't user-spawnable via the `sub_agent`
tool — it's only invoked automatically by the goal loop. Registering it
globally pollutes the agent-types list with internal-only entries.
Building inline keeps the profile concept colocated with `goal_loop.py`.

### Q2. Tool ceiling — read-only forever, or can it execute things?

I'm proposing `read_file / grep / glob / fetch_url` only. **Should we ever
allow `bash`, `run_tests`, or `write_file`?**

  · Allowing `run_tests` would let the judge actually run the test suite
    when the agent claims "44 of 47 tests pass" — massive signal lift for
    code-verification goals.
  · But it introduces risk: a judge that can run bash is one prompt away
    from doing damage.

My lean: ship read-only first. After we observe judge behavior in
production for a few weeks, add an opt-in `dangerous_tools` flag on the
brief that the operator must explicitly enable. Default off.

### Q3. Brief sets allowlist, or profile sets allowlist?

The brief currently has `judge_profile`. I'm adding `judge_tools`. Should
the tools allowlist be:

  · per-profile (no operator override; `deep` always gets the same
    tools) — simpler
  · per-brief (operator picks tools independently of profile) — more
    flexible

My lean: brief overrides profile defaults. If `judge_tools` is empty in
the brief, use the profile's default; if it's specified, use that. Same
shape as how brief criteria add to the judge's criteria.

### Q4. Cost cap

Add `judge_cost_cap_usd` to the brief? If exceeded mid-run, the judge
gets a forced "wrap up now" instruction and produces a partial verdict.

My lean: skip this in v1. Operator picks the profile and pays for it.
If we need a cap later, add it as a separate Phase. Premature
optimization.

### Q5. What happens if the judge subagent crashes?

Three modes:

  a. **Hard fail** — the goal loop pauses with `goal_paused` and a
     pause_reason like "judge subagent failed". Operator must resume.
  b. **Fallback to standard** — automatically retry with a one-shot
     standard call so the loop keeps moving. Note the fallback in the
     verdict's `reason` so the operator sees what happened.
  c. **Skip this turn's verdict** — emit a `goal_judge_failed` event,
     don't update goal_state, treat the turn as if no judge ran.

My lean: **(b) fallback to standard**. The loop continues, the verdict
flags the fallback, the operator notices and can investigate. (a) is too
disruptive; (c) leaves the loop in an undefined state.

### Q6. Surface the judge's child sessionId

Currently the verdict payload has `{done, confidence, reason, criteria,
openQuestions}`. To let the renderer link "Judge inspected" → full
session, we need to add the judge's child sessionId to the verdict.

My lean: add `judgeSessionId: str | None` to the GoalVerdict dataclass.
Backfill from goal_judge event details.

### Q7. Concurrency

Only one judge per turn. The goal loop awaits `_judge_goal` synchronously
before continuing. Already the case; no change needed. Just stating it
explicitly so we don't accidentally allow concurrent judges later.

---

## Failure modes (beyond Q5)

  · **Judge runs out of iterations without producing valid JSON** — the
    parser already returns a "Judge response was not valid JSON" verdict
    that fails the rubber-stamp guard. Same handling applies whether the
    judge is inline or subagent.
  · **Judge tool call fails** (read_file on a missing path, fetch_url on
    a 404) — tool errors flow back to the judge as part of its
    conversation; it can decide how to handle them. We don't need to
    intervene.
  · **Judge subagent timeout** — set a hard wall-clock limit (e.g.,
    120s) in the spawn config. On timeout, fall back per Q5.
  · **Operator cancels mid-judge** — add a `cancel_judge` action that
    kills the running subagent. The goal loop treats it like a failed
    judge and follows Q5's fallback.
  · **Cost runaway from cascading tool calls** — bounded by
    `judge_max_iterations` (3 by default). The judge has at most 3 turns
    to verify; if it can't, it ships a partial verdict.

---

## Backward compatibility

  · `quick` and `standard` profiles unchanged. Operators on those
    profiles see no behavioral difference.
  · `deep` profile previously ran inline with thinking; now runs as a
    subagent with thinking + tools. The verdict contract is identical;
    the rendering is enriched.
  · Briefs without `judge_tools` / `judge_max_iterations` use defaults
    on rehydration (from_dict). Existing sidecar files keep working.
  · The Phase 1 inline `deep` path can be left in place behind a feature
    flag for a release or two so we can A/B compare.

---

## Migration plan

Roughly four PRs, each independently shippable.

  **3a/i — agent type + inline spawn**. Define `judge-deep` as an inline
  AgentType. Switch `_judge_goal` deep branch to call the subagent
  runner. No tools yet (allowlist = empty frozenset). Verify the runner
  works, child session appears in the agents tab, verdict JSON still
  parses. **Smallest change; biggest blast radius mitigation.**

  **3a/ii — tools on**. Set the default allowlist to
  `{read_file, grep, glob, fetch_url}`. max_iterations = 3. Add a
  feature flag so we can disable if it misbehaves.

  **3b — render judge tool calls in TurnCard**. Wire the per-verdict
  card's "Judge inspected" slot to read the judge child session's tool
  calls. Click-through to the child session view.

  **3c — brief schema + UI**. Add `judge_tools` and
  `judge_max_iterations` to GoalBrief. JudgeBrief modal gets the
  capability toggles + iter slider.

Each phase is ~half-day of work assuming nothing surprises us. After 3a
ships we can run a real goal session and observe how the judge behaves
with tools before locking in 3b/3c.

---

## What you need to approve before I write code

  · **Q1** — Option A (registry) or Option B (inline)
  · **Q2** — Read-only forever, or open the door for `run_tests` later
  · **Q3** — Profile sets allowlist, or brief overrides
  · **Q4** — Cost cap in v1 or skip
  · **Q5** — Crash fallback policy
  · **Q6** — Add `judgeSessionId` to GoalVerdict

Defaults I'd ship if you say "go" without picking:
  · B / read-only / brief overrides / skip cost cap / fallback to
    standard / yes add judgeSessionId.
