# Goal-mode judge: directional notes

Working draft. Captures the architectural options we've discussed for the
judge in goal mode plus what the real-trace investigation surfaced. Not
intended to be canonical — this is a scratchpad for the conversation, to
be condensed into the actual code/docs once decisions land.

---

## What the trace investigation surfaced

Source: `~/Downloads/goal-session-trace.json` (real session, 17 messages,
174 tool calls, 67 systemEvents, 9 judge verdicts over 9 turns of deep
research).

**The data is fine in events.** Every `goal_judge` event in the trace
carries the full structured verdict in `details.verdict` (done, confidence,
reason prose, criteria, openQuestions). The latest event also carries a
`verdictHistory` array with all 9 prior verdicts. Confidence climbs
through the run — 0.42 → 0.52 → 0.62 → 0.58 (regression) → 0.60 → 0.65
→ 0.70 → 0.72 → 0.86 (done). The judge's reason text on each turn is
~2k chars of detailed prose. There is *enormous* signal here that we
are throwing away in the UI.

**But the data dies on app restart.** The bridge persists the engine
transcript (`~/.freyja/sessions/{id}.transcript.json`) only. `goal_state`,
`goal_brief`, and `goal_verdict_history` live in `FreyjaBridge` instance
memory. The renderer holds the last ~100 `systemEvents` in its rolling
buffer. On reload the bridge rehydrates messages but reinits goal state
to defaults (`goal_state = None`, `goal_brief = GoalBrief()`,
`goal_verdict_history = []`). So if you close and reopen a goal session,
all judge history is lost. The user's report that "verdicts aren't
recorded" is *this bug*, not a rendering issue.

**The UI is hostile to the signal.** Real complaints from the screenshot
review:
  · the mission objective takes the top third of the page and the
    operator already read it on turn 1
  · the verdict rail is too narrow to hold the judge's 2k-char reason
    prose, so it scrolls a tiny column with truncated lines and the
    operator can't copy anything for inspection
  · text isn't selectable (probably `user-select: none` inherited from
    chrome surfaces)
  · there's no way to correlate "verdict on turn 4" with "what the
    agent actually did on turn 4" — those are in different tabs
  · when the judge becomes tool-using, there is nowhere to render what
    it inspected (which files, which fetches, which test runs)

## The architectural shape we want, eventually

A judge that is a real subagent with a profile, not a one-shot call.
Specifically:

  · pulled from a **small fleet of profiles** (rigor / capability), not
    one judge slot
  · model, thinking budget, and tool allowlist are **parameterizable**
    from the operator brief
  · each judge call is its own session that can persist briefly and
    expose its tool calls in the UI
  · verdicts are **persisted to disk** alongside the transcript so they
    survive reload
  · the renderer **correlates** turn N's agent activity with turn N's
    verdict, side by side, in one place

We're explicitly not going domain-first ("code judge / research judge").
The domain is already in the work; the brief carries the voice/criteria.
What varies architecturally is **rigor and tool access**.

## Options under discussion

### Option A — Two profiles (simple)

Three judge profiles to start: `quick`, `standard`, `deep`.

  · `quick` — fast cheap model (Haiku, eventually GLM / Kimi when wired),
    no tools, no thinking. For high-frequency sanity gating. ~$0.001/call.
  · `standard` — same model as the agent, no tools, no thinking. Today's
    behavior. Default.
  · `deep` — frontier model with thinking on, no tools yet. Multi-turn
    allowed (`max_iterations` > 1). Used when the agent claims done or
    every N turns. ~$0.10/call.

Brief field: `judge_profile: 'quick' | 'standard' | 'deep'`. Operator
picks from a dropdown in the rules editor.

Pro: smallest change. Doesn't require subagent runner integration. Adds
a meaningful rigor dial.

Con: no tools. The judge can only reason from text the prompt hands it.
Big claims like "44/47 tests pass" are taken on faith.

### Option B — Judge as a real subagent profile

The judge becomes one (or several) entries in the existing subagent
profile registry. Like the `verify` or `review` profiles that already
exist. The current ad-hoc `_judge_goal` is replaced with the same
subagent runner the agent uses.

  · operator picks **model + thinking + tool allowlist** independently
    of the agent
  · judge can be **tool-using**: `read_file`, `run_tests`, `fetch_url`,
    `search`, etc., scoped per profile
  · judge can be **multi-turn**: ask follow-up questions of itself,
    chain tool calls
  · judge calls show up in the agents list (or in a "judges" sublist) —
    operator can attach to them and see their work like any other
    subagent

Pro: real verification. The judge can run the tests, fetch the source,
diff the files. Biggest single signal lift available. Plus the same
infra (events, UI, persistence) we already have for subagents.

Con: cost (each judge call is a billable subagent run; could go
3-5x current). Latency (multi-turn + tools add seconds-to-minutes).
Architectural surgery — touching the runner, the event system,
profile registry, brief schema, persistence.

### Option C — Multiple judges per turn

Run two judges per turn instead of one. Examples:

  · **Gate** (every turn, cheap): "Is the agent making forward progress?
    Has it regressed? Continue or pause?" Returns a 2-3 sentence verdict.
    Used for high-frequency sanity.
  · **Critic** (every turn or every N): the rigorous structured verdict
    we already built. Skeptical, criteria-tracking.

OR, two critics with different *temperaments*:

  · **Generous** — "if I squint, has the agent done this?"
  · **Adversarial** — "what could the agent be wrong about?"

Disagreement between the two surfaces operator-relevant bias the
averaging-into-one approach hides.

Pro: huge signal lift via diversity. The disagreement *is* a signal.
Easier to spot rubber-stamping when one judge keeps voting done and the
other keeps voting continue.

Con: doubles per-turn cost. UI complication — now we have two streams
of verdicts to render side by side. Operator may not want to read two
verdicts per turn even if they should.

### Option D — Tool-augmented deep profile only

Subset of Option B: only the `deep` profile gets tools; `quick` and
`standard` remain one-shot calls. Lets us limit the cost/complexity blast
radius — most turns get a cheap text judge, occasional milestones get the
expensive tool-using judge.

Pro: cost-controlled. Architecturally smaller than full-Option-B.

Con: still requires subagent runner integration for `deep`. The benefit
only kicks in occasionally.

## Recommended phasing

**Phase 0 — fix the persistence bug** *(must ship before anything else)*
  · persist `goal_state`, `goal_brief`, and `goal_verdict_history` to
    disk alongside the transcript
  · rehydrate on session load
  · include them in session export
  · without this, every other improvement is moot — the operator will
    keep losing data

**Phase 1 — Option A: rigor profiles, no tools**
  · add `judge_profile: 'quick' | 'standard' | 'deep'` to the brief
  · `quick` uses a cheap model + no thinking
  · `standard` is current behavior (default)
  · `deep` uses frontier + thinking on
  · still single-call, still no tools — but operator can dial rigor

**Phase 2 — UI redesign as turn-correlated timeline**
  · main view becomes a vertical list of turns
  · each turn = side-by-side row: agent activity + judge verdict
  · clicking a turn expands to full prose + tool calls
  · selectable text everywhere
  · reserve a "judge inspected" slot in each row for Phase 3's tool calls
  · mission objective collapses to a one-line header

**Phase 3 — Option B/D: tool-augmented deep judge**
  · promote `deep` to a real subagent profile
  · operator-configurable tool allowlist on the brief
  · judge tool calls render in the per-turn row alongside agent tool calls
  · `quick` and `standard` stay one-shot for cost control

**Phase 4 (maybe) — Option C: multiple judges per turn**
  · only after Phase 3 lands and we have appetite for cost
  · two-judge mode: gate + critic, or generous + adversarial
  · UI shows both verdicts side by side with disagreement highlighted

## Open questions

  1. **Persistence format.** Do we cram goal state into the existing
     transcript file or use a sidecar `.goal.json`? Sidecar is cleaner
     for partial loads but adds a file.

  2. **Tool budget for `deep`.** What's the cost ceiling per judge call?
     5 tool calls? 20? Configurable on the brief?

  3. **Where do judge tool calls live in the event stream?** Currently
     the agent's tool calls go through `tool_call_*` events. A judge
     subagent would do the same — but tagged so the UI can separate them.

  4. **Brief schema for tool allowlist.** Free-form list? Or a fixed
     enum of capability bundles (`code`, `web`, `eval`)?

  5. **`quick` profile model.** We don't currently have GLM/Kimi
     providers wired. Haiku 4.5 is our cheapest available. Worth shimming
     in OpenRouter or similar to get cheaper options, or start with
     Haiku and add others later?

  6. **Auto-promotion of `quick` to `deep`.** Should a `quick` verdict
     that says "this might be done" automatically trigger a `deep`
     verdict for confirmation? Or always operator-driven?
