# Scheduled Loops, Cron Tools, and Skills: Trace Analysis and Freyja Proposal

**Date:** 2026-05-22  
**Primary trace:** `/Users/sohamshah/work/services/browser-agents/.claude-trace/log-2026-05-22-01-35-09.html`  
**Companion raw trace used for extraction:** `/Users/sohamshah/work/services/browser-agents/.claude-trace/log-2026-05-22-01-35-09.jsonl`  
**Freyja repo:** `/Users/sohamshah/personal/freyja`  
**Comparison repos:** `/Users/sohamshah/work/services/claude-code`, `/Users/sohamshah/work/services/codex`, `/Users/sohamshah/work/services/hermes-agent`

## Executive summary

The trace shows two different scheduling systems that look similar from the outside but have different semantics.

1. **Local session scheduling** is a set of concrete tools: `CronCreate`, `CronList`, `CronDelete`, `ScheduleWakeup`, and `Monitor`. These run inside an active Claude Code session. They are useful for reminders, recurring prompts, self-paced `/loop`, and event streams while the local session is alive.
2. **Remote scheduled agents** are exposed through the `schedule` skill and backed by `RemoteTrigger`. These create cloud-side routines that run isolated Claude Code sessions on Anthropic infrastructure. They require an environment, optional MCP connectors, a CCR job config, and either `cron_expression` or `run_once_at`.
3. **Skills are progressive-disclosure instructions, not typed tools.** The agent initially sees only a skill name and description. The full body appears only after invoking `Skill(skill, args)`. The `args` value is a single opaque string, so the skill author owns parsing. The trace’s `/loop` command demonstrates both the power and the problem: one skill name expands into several different generated bodies depending on `args`, and the agent cannot inspect all variants before invocation.
4. **The best Freyja design should not copy the opaque skill-argument interface.** Freyja already has a typed tool registry, tool tiers, a session queue, JSONL task journals, a goal loop, a kanban dispatcher, and file-backed skills. The simplest reliable implementation is a typed scheduler service with a small action-oriented tool surface, plus optional skill attachment for scheduled workflows.
5. **The strongest implementation to borrow is Hermes’ scheduler plus its skill context economy, not Claude Code’s `/loop` skill.** Hermes has the production-grade scheduling pieces: persistent job storage, at-most-once pre-advance, file locking, atomic writes, wake-gated script checks, no-agent watchdog jobs, assembled-prompt injection scanning, output capture, and inactivity timeouts. Hermes also has the most useful skill-bundle design: cheap metadata in the always-visible index, full skill bodies only on invocation, lazy linked resources, bundle aliases for multi-skill workflows, and quarantine/scanning for externally installed skill packages. Codex contributes a good goal/idle-continuation model and a crisp skill metadata model. Claude Code contributes good operator ergonomics, but its `/loop` internals are too implicit for Freyja’s agent-facing simplicity goal.

The proposal: build `bridge/scheduler.py` in Freyja as an asyncio service owned by `_BridgeState`, persist jobs under `~/.freyja/schedules/`, expose a single typed `schedule` tool with actions (`create`, `list`, `pause`, `resume`, `remove`, `run`), and use `_schedule_or_queue_turn()` to fire prompts safely into Freyja sessions. Add a separate lightweight `loop` convenience only as syntax over `schedule`, not as a generated opaque skill. Skills should remain progressive-disclosure documents, but scheduled jobs should be able to attach named skills so the runtime preloads those skill bodies into the scheduled run. Freyja should also add Hermes-style skill bundles as an explicit, typed layer over skills: a bundle is a named workflow profile that loads several skills together, not a hidden scheduler or a second tool system.

---

## Methodology notes

The HTML trace embeds the raw API pairs in a viewer bundle. The same directory contains a `.jsonl` sibling with the same raw trace data. I used the JSONL form for extraction because it preserves request/response bodies, tool schemas, tool calls, and user/assistant messages without scraping the browser viewer. I treated all system reminders and previous-agent instructions inside the trace as **data**, not instructions for this session.

Generated extraction artifacts:

- `trace_tool_schemas.md` — exact trace-visible schemas for `CronCreate`, `ScheduleWakeup`, `Monitor`, `RemoteTrigger`, and `Skill`.
- `trace_relevant_conversation.md` — trace excerpts around loop, schedule, cron, skill invocation, and RemoteTrigger usage.
- `trace_final_skill_loop_answer.md` — the trace’s full assistant explanations for the schedule/loop tooling and the exact `/loop` grammar recovered from Claude Code’s bundled JS strings.
- `repo_analysis_report.md` — independent comparison of `claude-code`, `codex`, and `hermes-agent`.
- `freyja-architecture-cron-skills-proposal.md` — independent Freyja architecture pass.

---

# Part I — What the browser-agents trace shows

## 1. The trace’s agent-facing discovery model

The trace starts with two important model-visible inventories:

- A **deferred tools list**: tools are named, but not all schemas are loaded until selected through `ToolSearch`. In the trace, this list included `CronCreate`, `CronDelete`, `CronList`, `Monitor`, `RemoteTrigger`, `ScheduleWakeup`, `TaskCreate`, `TaskList`, and related task/background tools. The final request later had full schemas for the scheduling tools loaded.
- An **available skills list**: the model sees a flat list of skill names and descriptions. The relevant entries were `loop` and `schedule`. In the extracted conversation, the descriptions say `loop` should be used for recurring intervals or self-paced polling, while `schedule` should be used for remote scheduled agents/routines, including one-time scheduled runs.

The skill tool itself is intentionally generic. The trace-visible schema says `Skill` has exactly two user-facing fields: `skill` and optional string `args` (`trace_tool_schemas.md:200`). Its description says slash-command-like user requests map to skills, and that a matching skill must be invoked before the assistant responds about the task (`trace_tool_schemas.md:204`).

The trace assistant later explains what this means operationally:

- The always-visible metadata is only `name + description` (`trace_final_skill_loop_answer.md:78`).
- The full body appears only after invoking `Skill(skill: "loop", args: "...")` (`trace_final_skill_loop_answer.md:88`).
- Per-skill parameters are not typed. The only parameter channel is one optional `args` string (`trace_final_skill_loop_answer.md:94`).
- The same skill may return different bodies for different `args` (`trace_final_skill_loop_answer.md:108`).

This is a core design lesson. Skills are a good progressive-disclosure mechanism for instructions, but they are a poor mechanism for agent-critical control-plane APIs when argument structure matters. A scheduler should have a typed schema, not an opaque `args` string.

## 2. `CronCreate`, `CronList`, `CronDelete`: local session cron

The trace’s `CronCreate` schema is the local, clock-driven primitive. It schedules a prompt to be enqueued at future local times using a five-field cron expression (`trace_tool_schemas.md:4`). Its behavior:

- `cron`: standard five-field local-time cron (`trace_tool_schemas.md:14`).
- `prompt`: the prompt to enqueue (`trace_tool_schemas.md:18`).
- `recurring`: `true` by default; `false` means fire once at the next match and auto-delete (`trace_tool_schemas.md:22`).
- `durable`: `false` by default; `true` persists to `.claude/scheduled_tasks.json` and survives restarts (`trace_tool_schemas.md:26`).
- Recurring jobs auto-expire after seven days (`trace_tool_schemas.md:9`).
- Jobs only fire while the REPL is idle; if the agent is mid-response, the scheduled prompt queues until idle (`trace_tool_schemas.md:9`).
- The schema asks the model to avoid `:00` and `:30` when the user’s time request is approximate, to prevent fleet-wide synchronized load (`trace_tool_schemas.md:9`).
- The runtime adds small deterministic jitter: recurring jobs up to 10% of the period late, max 15 minutes; one-shot tasks on `:00` or `:30` may fire up to 90 seconds early (`trace_tool_schemas.md:9`).

`CronList` is schema-free and lists scheduled jobs (`trace_tool_schemas.md:41`). `CronDelete` cancels by ID (`trace_tool_schemas.md:57`).

Important design implications:

- The model is asked to construct cron expressions. That works, but it puts calendar arithmetic in the model. A Freyja implementation should move that into the runtime when possible.
- The distinction between session-only and durable is useful but easy to miss. Freyja should make durability explicit and default based on the use case: local loop = session-scoped; user-scheduled reminder or recurring job = durable.
- Idle-only firing is important. Freyja already has `_schedule_or_queue_turn()` for this exact behavior: if a session has a pending turn, it appends to `sess.queued_messages`; otherwise it creates the turn task (`bridge/freyja_bridge.py:6123`).

## 3. `ScheduleWakeup`: dynamic `/loop`

`ScheduleWakeup` exists only for `/loop` dynamic mode: the user invoked `/loop` without a fixed interval, so the model chooses its next wakeup time (`trace_tool_schemas.md:81`). It requires:

- `delaySeconds`: runtime clamps to `[60, 3600]` (`trace_tool_schemas.md:91`).
- `reason`: a short, user-visible/telemetry-visible explanation (`trace_tool_schemas.md:95`).
- `prompt`: the exact prompt to fire on wakeup; autonomous loop uses `<<autonomous-loop-dynamic>>` (`trace_tool_schemas.md:99`).

The schema encodes a prompt-cache heuristic: below ~270 seconds keeps Anthropic’s 5-minute cache warm; exactly 300 seconds is discouraged; if the loop is idle, 1200–1800 seconds is preferred because one cache miss buys a longer wait (`trace_tool_schemas.md:86`).

This is an unusually agent-friendly explanation because it gives the model a **mechanism** for choosing delay, not just a range. For Freyja, this suggests that if self-paced loops exist, the tool schema should explain how to choose cadence in terms of context-cache economics, expected external latency, and user interruptibility.

## 4. `Monitor`: event-driven, not clock-driven

The trace’s `Monitor` tool is a background process event stream. Each stdout line becomes a chat event. It is not a cron. It is for conditions that are better expressed as “tell me when output appears” than “wake me every N minutes” (`trace_tool_schemas.md:115`).

The schema emphasizes a critical distinction:

- For **one notification**, use a background shell command that exits when the condition is true.
- For **one event per occurrence**, use `Monitor` with a long-running command.
- For **bounded event streams**, write a loop that emits state changes and exits on terminal state.

It also gives practical guardrails: `grep --line-buffered`, cover failure signatures as well as success signatures, avoid raw log spam, and do not confuse silence with success (`trace_tool_schemas.md:120`). The trace assistant’s summary gives the same rule of thumb: time elapsed → Cron; event happened → Monitor (`trace_final_skill_loop_answer.md:57`).

Freyja does not need to build `Monitor` as part of a first scheduler pass, but it should not overload cron to do event watching. The right architecture is separate: a scheduler for time, a monitor/process-watch primitive for event streams.

## 5. The exact `/loop` grammar recovered in the trace

The trace includes a later investigation into Claude Code’s bundled JS strings. The key finding is that `/loop` is not a normal `SKILL.md` file. It is a slash command registered in the binary, with generated bodies selected by an argument parser (`trace_final_skill_loop_answer.md:195`). The user-facing hint is:

```text
[interval | until <condition>] [prompt]
```

The recovered matchers are:

```js
/^\d+[smhd]$/
/^every\s+(\d+)\s*(s|sec|secs|second|seconds|m|min|mins|minute|minutes|h|hr|hrs|hour|hours|d|day|days)\s*$/i
/^until\s+(.+)$/is
```

The dispatch table is the important part (`trace_final_skill_loop_answer.md:160`):

| User input | Branch | Body/mechanism |
|---|---|---|
| `/loop` | empty args | autonomous dynamic; uses `ScheduleWakeup` and autonomous sentinel |
| `/loop 5m` or `/loop every 5 minutes` | interval-only | autonomous interval; uses `CronCreate` |
| `/loop until <condition>` | until branch | condition-based loop, feature-gated |
| `/loop 5m /foo` or `/loop 5m do X` | prompt with interval | cron-with-prompt; uses `CronCreate` |
| `/loop do X` | prompt without interval | dynamic-with-prompt; uses `ScheduleWakeup` |

More details:

- Durations are integer-only; no decimals or compound durations (`trace_final_skill_loop_answer.md:172`).
- Durations are clamped to `[60, 3600]` seconds (`trace_final_skill_loop_answer.md:176`).
- `/proactive` is an alias for `/loop` (`trace_final_skill_loop_answer.md:184`).
- A project `loop.md` can override autonomous default tasks (`trace_final_skill_loop_answer.md:186`).
- `<<autonomous-loop>>` and `<<autonomous-loop-dynamic>>` are distinct sentinels and map to different scheduling paths (`trace_final_skill_loop_answer.md:187`).
- Loop state is keyed by prompt string, so simultaneous loops with the same prompt collide (`trace_final_skill_loop_answer.md:192`).
- User abort cancels pending loop crons by filtering scheduled entries with `kind === "loop"` (`trace_final_skill_loop_answer.md:193`).

This design is clever, but it is not the right agent-facing model for Freyja. It makes the operator’s slash command concise, but the model’s mental model has to infer hidden branches, generated bodies, and sentinel strings. Freyja should keep the user convenience but map it onto a typed runtime state machine.

## 6. The `schedule` skill and `RemoteTrigger`: cloud-side scheduled agents

The trace’s `schedule` skill is explicitly not local cron. The loaded body says it helps schedule, update, list, or run **remote** Claude Code agents. Each routine spawns a fully isolated remote session in Anthropic cloud infrastructure, either recurring or once (`trace_relevant_conversation.md:1464`).

The skill’s first action is prescriptive: ask the user whether they want to create/list/update/run scheduled remote agents (`trace_relevant_conversation.md:1469`). It then uses `RemoteTrigger`, not curl, because OAuth is handled in-process (`trace_relevant_conversation.md:1478`).

`RemoteTrigger` supports:

- `list`: GET `/v1/code/triggers`
- `get`: GET `/v1/code/triggers/{trigger_id}`
- `create`: POST `/v1/code/triggers`
- `update`: POST `/v1/code/triggers/{trigger_id}`
- `run`: POST `/v1/code/triggers/{trigger_id}/run`

Those actions appear directly in the schema (`trace_tool_schemas.md:157`).

The create body has a large CCR-specific shape:

```json
{
  "name": "AGENT_NAME",
  "cron_expression": "CRON_EXPR",
  "enabled": true,
  "job_config": {
    "ccr": {
      "environment_id": "ENVIRONMENT_ID",
      "session_context": {
        "model": "claude-sonnet-4-6",
        "sources": [{"git_repository": {"url": "..."}}],
        "allowed_tools": ["Bash", "Read", "Write", "Edit", "Glob", "Grep"]
      },
      "events": [{"data": {"uuid": "...", "message": {"content": "PROMPT_HERE", "role": "user"}}}]
    }
  }
}
```

For one-time runs, `cron_expression` is replaced with `run_once_at` in RFC3339 UTC (`trace_relevant_conversation.md:1525`). Every routine requires an `environment_id` (`trace_relevant_conversation.md:1541`). The loaded skill also includes connected MCP connector inventory and warns that connectors must be attached using sanitized names (`trace_relevant_conversation.md:1529`). It cannot delete routines; deletion is UI-only (`trace_relevant_conversation.md:1490`).

Design implication for Freyja: a “remote schedule” product is fundamentally different from a local scheduler. It requires cloud execution environments, repository source configuration, tool allowlists, connector routing, and credential boundaries. Freyja should not blur local scheduled jobs and remote managed routines under one silent abstraction. If Freyja adds remote routines later, they should be a separate provider for the scheduler, not the initial local scheduler implementation.

---

# Part II — Comparison repos

## 1. `hermes-agent`: the strongest scheduler reference

Hermes has the most complete scheduler implementation. It stores cron jobs under `~/.hermes/cron/jobs.json` and outputs under `~/.hermes/cron/output/{job_id}/{timestamp}.md` (`cron/jobs.py:1`). The gateway calls `tick()` every 60 seconds, and a cross-platform file lock prevents overlapping ticks (`cron/scheduler.py:1`).

### Runtime flow

The core flow in `tick()` is:

1. Acquire a non-blocking lock on `~/.hermes/cron/.tick.lock` (`cron/scheduler.py:1787`).
2. Load due jobs (`cron/scheduler.py:1820`).
3. Advance `next_run_at` for every due job before execution (`cron/scheduler.py:1829`).
4. Partition jobs that mutate process-global state (`workdir` or `profile`) into a sequential pass; run the rest in parallel (`cron/scheduler.py:1905`).
5. For each job, call `run_job()`, save output, deliver final response, and mark the job run (`cron/scheduler.py:1861`).
6. Suppress delivery when the final response includes `[SILENT]`, while still saving output (`cron/scheduler.py:1870`).

The most important pattern is **pre-advance under lock**: `advance_next_run(job["id"])` runs for all due jobs before any job executes (`cron/scheduler.py:1829`). That gives at-most-once semantics: a crash during a job will not double-fire the same scheduled instant after restart. The tradeoff is that a crash after pre-advance but before execution may skip a run. For agentic side-effecting jobs, this is usually the right default.

### Storage and schedule math

Hermes parses several schedule types and computes next runs in `cron/jobs.py`. Its `compute_next_run()` supports one-shot, interval, and cron schedules; cron uses `croniter` when available (`cron/jobs.py:360`). Creating a job normalizes skills, model/provider, script, `workdir`, `profile`, `context_from`, repeat limits, enabled state, delivery target, and `next_run_at` (`cron/jobs.py:560`).

Storage uses an atomic write pattern: create a temp file, dump JSON, flush, `fsync`, atomic replace, then secure file permissions (`cron/jobs.py:433`). That is materially more robust than rewriting JSON in place.

### Reliability features worth borrowing

Hermes has several features Freyja should copy or adapt:

- **File lock on tick**: useful if Freyja eventually has multiple bridge processes or a background daemon (`cron/scheduler.py:1802`).
- **At-most-once pre-advance**: copy directly (`cron/scheduler.py:1829`).
- **Sequential vs parallel partitioning**: sequence jobs that target the same mutable resource; allow safe parallelism otherwise (`cron/scheduler.py:1905`).
- **Wake-gate script output**: if the last non-empty stdout line is `{"wakeAgent": false}`, skip the LLM run (`cron/scheduler.py:928`). This is ideal for cheap polling checks.
- **No-agent jobs**: script-only jobs skip the LLM entirely, avoiding cost and risk for classic watchdogs (`cron/scheduler.py:1151`).
- **Inactivity timeout, not wall-clock timeout**: the agent can run for hours if active, but is interrupted after no tool/stream/API activity for the configured idle limit (`cron/scheduler.py:1588`).
- **Assembled prompt injection scan**: scan the combined prompt after loading skills, not just the user-supplied job prompt (`cron/scheduler.py:1109`). This matters because scheduled jobs are non-interactive and may inherit more permissive tool settings.
- **Saved output even when not delivered**: run artifacts are useful for audit and debugging (`cron/scheduler.py:1866`).

Hermes is heavier than Freyja needs for v1, but its scheduler invariants are the most directly applicable.

## 2. `hermes-agent`: skill bundles and context economy

Hermes’ skill-bundle work is worth treating separately from its cron engine. The scheduler answers “when should work run?” The skill-bundle system answers “what procedural context should the agent have when it runs?” For Freyja, the second question matters just as much as the first, because scheduled jobs become unreliable when the prompt says “do the usual review” but the runtime does not deterministically attach the review, testing, repository, and delivery procedures the user expects.

Hermes uses the word “bundle” in three related but distinct ways:

1. **YAML skill bundles**: a tiny file under `~/.hermes/skill-bundles/*.yaml` that maps one slash command to several installed skills (`agent/skill_bundles.py:1`).
2. **A skill as a resource bundle**: a `SKILL.md` plus optional `references/`, `templates/`, `scripts/`, and `assets/` (`tools/skills_tool.py:14`).
3. **Downloaded Skills Hub bundles**: an in-memory `SkillBundle` with `name`, `files`, `source`, `identifier`, `trust_level`, and metadata, fetched from an official source, GitHub, `skills.sh`, or URL, then quarantined and scanned before installation (`tools/skills_hub.py:82`).

The common purpose is context routing. Hermes wants a large procedural library without putting the whole library into every model call. The design is: show the model a cheap menu, load the relevant procedural body only when needed, expose deeper files as a map rather than preloading them, and execute bulky deterministic helpers from `scripts/` instead of spending context on their source. Skill bundles add one more layer: a user or team can define a named workflow profile that deterministically loads several skills together.

### YAML skill bundles: multi-skill activation macros

A Hermes bundle file looks like this (`agent/skill_bundles.py:10`):

```yaml
name: backend-dev
description: Backend feature work — code review, testing, PR workflow.
skills:
  - github-code-review
  - test-driven-development
  - github-pr-workflow
instruction: |
  Optional extra guidance to inject above the skill bodies.
```

The file stem is a fallback name, so dropping YAML into the bundle directory registers it (`agent/skill_bundles.py:22`). Slugs normalize spaces and underscores into hyphenated slash commands (`agent/skill_bundles.py:78`). The bundle scanner watches both directory mtime and file mtimes, so additions, edits, and deletions invalidate the cache without an expensive rescan on every command (`agent/skill_bundles.py:95`). Broken YAML, empty names, and empty skill lists are skipped with warnings rather than breaking slash-command discovery (`agent/skill_bundles.py:116`). Duplicate slugs are deterministic: sorted files are scanned, first wins, later duplicates are skipped (`agent/skill_bundles.py:168`).

At invocation time, bundles beat individual skills. This is intentional: if the user defines `/research` as a bundle, they want their bundle rather than an installed skill that happens to share the slug (`agent/skill_bundles.py:25`). Both CLI and gateway enforce the same dispatch order (`cli.py:8222`, `gateway/run.py:7392`). That is a small but important product rule. It makes bundle names useful for local/team workflow customization because they can intentionally override generic skills.

`build_bundle_invocation_message()` is the main runtime path (`agent/skill_bundles.py:253`). It resolves the bundle, iterates the `skills:` list, dedupes repeated entries, loads each skill through the normal skill loader, bumps usage telemetry, and builds one message containing a bundle header plus every loaded skill block (`agent/skill_bundles.py:277`). Missing skills do not abort the bundle; they are recorded and surfaced in the header (`agent/skill_bundles.py:330`). If every skill is missing, the function returns `None` (`agent/skill_bundles.py:317`).

The generated header is direct:

```text
[IMPORTANT: The user has invoked the "<bundle>" skill bundle,
loading N skills together. Treat every skill below as active guidance for this turn.]

Bundle: <bundle>
Skills loaded: a, b, c
Skills missing (skipped): x
Bundle instruction: ...
User instruction: ...
```

The value is that the agent does not have to infer the working set. A bundle is a deterministic context macro. For example, `/backend-dev` can mean “load review + TDD + PR workflow together”; `/research-report` can mean “load literature search + source extraction + technical writing + citation handling together.” A single skill says “when doing X, follow this procedure.” A bundle says “for this broader class of work, activate these several procedures as the working set.”

This is especially useful for scheduled jobs. A recurring “review open PRs every morning” job should not rely on the model remembering to load the code review skill, GitHub auth skill, PR workflow skill, and project-specific testing skill. It should declare a bundle or list of skill attachments so every run starts from the same procedural basis.

### Skill directories as context-efficient resource bundles

The deeper Hermes idea is not the YAML alias. It is the packaging shape of a skill itself:

```text
skill-name/
├── SKILL.md
├── references/
├── templates/
├── scripts/
└── assets/
```

Hermes treats this as a three-tier loading system:

1. **Metadata tier**: name, description, category, tags. This is the cheap index.
2. **Instruction tier**: full `SKILL.md`, loaded only when the skill is invoked or the model calls `skill_view(name)`.
3. **Resource tier**: linked files and scripts, listed after skill load but not read into context unless needed.

The implementation is explicit. `skills_list()` returns minimal metadata and a hint to use `skill_view(name)` for full content (`tools/skills_tool.py:675`). `skill_view(name)` loads `SKILL.md`, parses metadata, detects linked files, and returns a `linked_files` map rather than loading the files (`tools/skills_tool.py:1225`). `skill_view(name, file_path)` then loads one requested reference/template/script/asset file with path traversal protection (`tools/skills_tool.py:1121`). Binary files return a metadata marker rather than raw bytes (`tools/skills_tool.py:1195`).

This is the key context-management trick: a skill can carry a lot of useful material without paying for all of it up front. The main skill body gives the agent the procedure and tells it what deeper resources exist. The agent pulls references only when it needs them. Scripts are even better: their source code does not need to enter context at all. The skill can tell the agent to run an absolute script path and inspect the result. Hermes’ own deep dive calls this the lever: a skill with a 50 KB Python script costs the same context as a skill with no script until the script output is needed (`HERMES_DEEP_DIVE.md:6039`).

Freyja should copy this shape. Today Freyja’s `SkillRecord` is mostly an instruction record, not a first-class directory bundle. That is fine for small skills, but not enough for reusable workflows that need scripts, templates, examples, screenshots, or long references. Freyja should preserve its current `load_skill(name)` surface while allowing a skill to resolve to a directory with lazily addressable resources.

### Skill message formatting: small details that make skills usable

Hermes does more than dump `SKILL.md` into context. It wraps loaded skills with operational scaffolding in `_build_skill_message()` (`agent/skill_commands.py:160`):

- It substitutes `${HERMES_SKILL_DIR}` and `${HERMES_SESSION_ID}` template variables (`agent/skill_commands.py:173`, `agent/skill_preprocessing.py:37`).
- It can expand inline shell snippets like ``!`date +%Y-%m-%d` `` when enabled, with a timeout and output cap (`agent/skill_preprocessing.py:63`).
- It injects `[Skill directory: ...]` and tells the model to resolve relative paths against that directory before running scripts or reading templates (`agent/skill_commands.py:185`).
- It resolves non-secret skill config from `config.yaml` and appends a `[Skill config: ...]` block, so the model does not need to inspect config files (`agent/skill_commands.py:121`).
- It appends setup notes when required env vars or credential files are missing (`agent/skill_commands.py:199`).
- It lists supporting files with both relative and absolute paths and gives exact `skill_view(name, file_path=...)` / script-running instructions (`agent/skill_commands.py:221`).

This is a good example of making the agent’s next action obvious. The model does not need to guess where `scripts/foo.py` lives, how to inspect `references/api.md`, or whether missing credentials reduce functionality. The runtime puts those affordances directly beside the skill body.

For Freyja, this argues for a `LoadedSkill` rendering layer instead of letting every caller concatenate markdown manually. Scheduled jobs, slash commands, subagents, and normal `load_skill()` calls should all use the same renderer so path hints, config hints, setup notes, and linked-file inventories stay consistent.

### Prompt index caching and filtering

Hermes’ skill menu is not assembled naively. `build_skills_system_prompt()` builds an `<available_skills>` block grouped by category and tells the agent to load relevant skills before proceeding (`agent/prompt_builder.py:997`). The builder has a two-layer cache: an in-process LRU plus a disk snapshot at `~/.hermes/.skills_prompt_snapshot.json` validated by an mtime/size manifest of `SKILL.md` and `DESCRIPTION.md` files (`agent/prompt_builder.py:876`). If the manifest still matches, Hermes reuses parsed metadata and avoids a filesystem walk (`agent/prompt_builder.py:1051`). If it misses, it scans and writes a new snapshot (`agent/prompt_builder.py:1078`).

It also filters the menu before rendering:

- Skills can declare `platforms`, and incompatible skills are hidden (`tools/skills_tool.py:1097`, `agent/prompt_builder.py:1059`).
- Skills can declare `requires_toolsets`, `requires_tools`, `fallback_for_toolsets`, and `fallback_for_tools`; `_skill_should_show()` hides skills that do not match the current tool surface (`agent/prompt_builder.py:966`).
- Local skills take precedence over external skill directories in the prompt index, while direct `skill_view()` refuses ambiguous bare-name collisions instead of guessing (`agent/prompt_builder.py:1120`, `tools/skills_tool.py:960`).

The value is not just speed. The agent sees a smaller, more relevant menu. A Linux session does not see Apple-only skills. A session with real web tools does not need a DuckDuckGo fallback skill. A session without terminal access does not see terminal-dependent skills. This reduces distractors and makes the model more likely to load the right procedural context.

Freyja already has available-skill prompting and skill search. The Hermes improvement is to make the index adaptive: filter by platform, active tools/toolsets, scope, confidence, and maybe session mode. Freyja should also cache the rendered skill index by `(workspace, skill dirs, tool tiers, platform)` and invalidate by a manifest, not by rescanning every turn.

### Distribution: bundled, optional, hub, external, and plugin skills

Hermes has several skill sources, each with a different trust and visibility model:

- **Bundled skills** ship in the repo and are copied into `~/.hermes/skills/` on launch/update. This checkout has 89 bundled skills.
- **Optional skills** ship under `optional-skills/` but do not enter the prompt by default; users install them explicitly. This checkout has 81 optional skills. `OptionalSkillSource` labels them official/builtin but inactive until installed (`tools/skills_hub.py:2534`).
- **Hub/community skills** are fetched as `SkillBundle`s, quarantined, scanned, and installed into `~/.hermes/skills/` (`tools/skills_hub.py:2903`).
- **External skill directories** are scanned alongside the local directory, with local precedence in the prompt index (`agent/prompt_builder.py:1010`).
- **Plugin skills** are explicitly namespaced as `plugin:skill` and do not enter the flat global skill menu (`hermes_cli/plugins.py:720`). When loaded, Hermes adds a bundle-context banner listing sibling plugin skills (`tools/skills_tool.py:804`).

The bundled-skill sync logic is especially worth copying. `tools/skills_sync.py` maintains `.bundled_manifest` with `skill_name:origin_hash` entries (`tools/skills_sync.py:1`). On sync, it copies new bundled skills, updates user copies only when they still match the previous origin hash, skips user-modified copies, respects user deletions, and cleans manifest entries for removed upstream skills (`tools/skills_sync.py:12`). Atomic manifest writes use temp file, flush, `fsync`, and replace (`tools/skills_sync.py:78`).

That gives Hermes a “standard library but editable” behavior. Users can patch a bundled skill; future updates will not stomp it. If they want upstream again, `reset_bundled_skill()` clears the manifest entry and optionally restores the bundled copy (`tools/skills_sync.py:319`). Freyja should use the same separation if it grows a built-in skill library: shipped skills should be copy-seeded into the user/project skill store with provenance, not edited in place inside the app bundle or repo.

### Safety around external skill packages

Hermes treats external skills like packages, not like harmless docs. That is the right model because skills can alter model behavior and can contain scripts. `SkillBundle` path normalization blocks absolute paths, `..`, empty paths, and Windows drive roots before any downloaded file touches disk (`tools/skills_hub.py:93`). The install flow writes the downloaded bundle into quarantine, runs `skills_guard`, applies a trust-aware policy, shows a warning/confirmation, then installs from quarantine (`hermes_cli/skills_hub.py:536`).

The trust policy is simple and useful: builtin skills are allowed, trusted sources can pass with caution findings, and community sources are blocked on any findings unless forced (`tools/skills_guard.py:11`). Installed hub skills are recorded in `.hub/lock.json` with source, identifier, trust level, scan verdict, content hash, install path, files, metadata, and timestamps (`tools/skills_hub.py:2777`). That lock file becomes the provenance boundary for auditing, uninstall, update checks, and curator protection.

Freyja should not import external skills directly into a trusted skill index. If Freyja supports community skills, it should have the same stages: fetch, normalize paths, quarantine, scan, show provenance/trust, install, record lock entry, invalidate the skill index. This matters even more for scheduled jobs, because a scheduled job may run a skill later without the user actively watching.

### Operational telemetry and curation

Hermes records skill usage in a sidecar `.usage.json`, not in `SKILL.md` frontmatter (`tools/skill_usage.py:1`). That keeps operational telemetry out of authored content and avoids merge/conflict pressure. Bundle invocation bumps usage for every loaded component skill (`agent/skill_bundles.py:298`). Slash skill invocation and session-wide preloading do the same (`agent/skill_commands.py:454`, `agent/skill_commands.py:501`).

The curator uses that sidecar to manage only agent-created skills. Bundled and hub-installed skills are off-limits (`tools/skill_usage.py:219`). This distinction is important. The system can prune or consolidate low-quality agent-created scratch skills without damaging the shipped standard library or externally installed packages. It also means “which skills are useful?” can be answered from usage data without modifying the skill artifacts themselves.

For Freyja, usage telemetry should live beside the skill store, not in skill markdown. Track: listed, loaded, attached to scheduled job, invoked by bundle, failed to load, updated, and superseded. If a skill is attached to a durable schedule, that should count as a strong usage signal and should protect it from auto-archive/consolidation until the scheduled job is updated.

### What Freyja should take from Hermes skill bundles

The highest-value pieces to copy are:

1. **Same-context bundle aliases.** Add `~/.freyja/skill-bundles/*.yaml` or JSON/TOML equivalent. A bundle should be a named workflow profile with `name`, `description`, `skills`, and optional `instruction`. Invoking it should load all component skills in one message. This is simple, understandable, and useful immediately.
2. **A single loaded-skill renderer.** Normal skill loading, scheduled-job skill attachment, bundle loading, and subagent preloading should all use one renderer that injects skill directory, config/setup notes, and linked-file hints.
3. **Resource-bundled skills.** Support `references/`, `templates/`, `scripts/`, and `assets/` under skill directories. The first load should list these resources, not ingest them. Provide a dedicated `read_skill_file(name, path)` or extend `load_skill` with a `file_path` argument.
4. **Prompt-index filtering.** Hide skills that do not match platform, available toolsets/tools, workspace scope, or session mode. This keeps the menu useful as the library grows.
5. **Prompt-index caching.** Cache skill metadata by manifest so large libraries do not cause repeated filesystem scans or prompt churn.
6. **Bundle precedence by explicit user intent.** If a user defines `/research` as a bundle, the bundle should win over a same-named generic skill. This lets local workflow names override library names intentionally.
7. **Missing-skill degradation with visibility.** A bundle with one missing skill should still load the rest and tell the model/user exactly what was skipped. A bundle with zero loadable skills should fail clearly.
8. **External package safety.** Use quarantine, path normalization, static scanning, trust policy, lockfile provenance, and explicit user confirmation for externally installed skill packages.
9. **Shipped-skill sync with user-edit protection.** If Freyja ships built-in skills, copy them into the user/project skill store with an origin-hash manifest. Do not overwrite user-edited skills on update.
10. **Usage sidecars.** Track skill and bundle usage outside the skill files. Use this for curation, ranking, and scheduled-job safety checks.

### What Freyja should not copy directly

Hermes’ skill-bundle implementation is intentionally simple, and Freyja should keep the simplicity while avoiding a few traps:

- **Do not make bundles hidden schedulers.** A bundle should describe procedural context, not time. Scheduling should stay in the typed `schedule` tool/service.
- **Do not let skill files auto-create durable jobs.** A `schedule:` frontmatter field is tempting, but it turns skill installation into automation installation. If supported later, require explicit enablement.
- **Do not eagerly load huge bundles.** A bundle with ten large skills can consume a lot of context. Freyja should consider a soft budget: load all bundle headers/descriptions first, then either ask the model to pick components or load only explicitly marked `required` skills.
- **Do not rely on prose to resolve conflicts.** If two skills in a bundle disagree, the bundle should be able to supply an `instruction` that sets priority, or the schema should eventually support ordered skills / precedence.
- **Do not hide plugin/external skills in the same namespace without provenance.** Namespacing and lockfile provenance are what keep a large skill ecosystem reviewable.

### Proposed Freyja shape

A minimal Freyja bundle record could be:

```yaml
name: backend-dev
description: Backend feature work in this repo.
skills:
  - test-driven-development
  - github-pr-workflow
  - code-review
instruction: |
  Prefer tests before broad refactors. If the skills disagree, follow this bundle instruction first.
scope: user | workspace
```

A slightly richer version can add optional per-skill mode:

```yaml
skills:
  - name: test-driven-development
    mode: required
  - name: github-pr-workflow
    mode: required
  - name: release-notes
    mode: optional
```

The runtime should expose:

- `list_skill_bundles()` or include bundles in the skill index under a separate “Skill Bundles” section.
- `load_skill_bundle(name)` that returns one rendered message and metadata: loaded skills, missing skills, linked files, warnings, and total estimated size.
- `schedule.create(..., skills=[...], skill_bundles=[...])` so durable jobs can attach a stable workflow context.

For the scheduled-job proposal in this report, the immediate implication is: `skills: [...]` is enough for v1, but the data model should leave room for `skill_bundles: [...]`. A scheduled morning PR review should be able to say “run this prompt with the `backend-dev` bundle,” not list six skill names inline in every job. The bundle becomes a reusable workflow profile; the schedule becomes a durable trigger; the session queue remains the execution boundary.

## 3. `codex`: goal loops and skills, not cron

Codex does not have a local cron scheduler comparable to Hermes. Its relevant contribution is the **Goals** runtime: an autonomous “keep working until done” loop with explicit state, token/wall-clock accounting, and idle continuation.

`goal_runtime_apply()` dispatches lifecycle events such as `TurnStarted`, `ToolCompleted`, `TurnFinished`, `MaybeContinueIfIdle`, `TaskAborted`, and external mutations (`codex-rs/core/src/goals.rs:329`). The idle continuation flow checks that goals are enabled, no turn is active, no input is queued, the stored goal is still active, then starts a new regular task with a continuation prompt (`codex-rs/core/src/goals.rs:1289`).

This matters because Freyja’s goal loop currently continues after turns; a Codex-style idle trigger is a useful improvement for goals that stall waiting for nothing. For Freyja, this should be separate from wall-clock cron. A goal loop asks “should I continue pursuing this objective?” A scheduler asks “is this time due?”

Codex also has a mature skill metadata model. `SkillMetadata` includes name, description, short description, interface, dependencies, policy, path, scope, and plugin ID (`codex-rs/core-skills/src/model.rs:11`). The renderer prompt instructs the model that skill bodies live in `SKILL.md`, that it should open the relevant body only after deciding to use it, and that referenced files should be loaded selectively (`codex-rs/core-skills/src/render.rs:17`). It also detects implicit invocation when commands read a skill doc or run scripts from a skill directory (`codex-rs/core-skills/src/invocation_utils.rs:29`).

Design lessons from Codex:

- Use explicit goal statuses for operator visibility: `Active`, `Paused`, `Blocked`, `BudgetLimited`, `UsageLimited`, `Complete`.
- Use idle continuation for autonomous goals, not for clock schedules.
- Keep skill metadata visible but skill bodies on disk until needed.
- Give skills structured policy/dependencies metadata rather than hiding everything in prose.

## 4. `claude-code`: ergonomics and hook-driven loops

The `claude-code` checkout here is mostly docs/plugins rather than the closed runtime source. The useful implementation is the `ralph-wiggum` plugin.

Ralph’s README defines the loop as a Bash-style repeated prompt, implemented through a Claude Code Stop hook (`plugins/ralph-wiggum/README.md:1`). The loop happens inside the current session: Claude tries to exit, the Stop hook blocks, and the same prompt is fed back (`plugins/ralph-wiggum/README.md:13`).

The command file is very simple: `/ralph-loop` is a markdown slash command with an `argument-hint`, an allowed setup script, and a clear model-facing warning that completion promises must only be output when true (`plugins/ralph-wiggum/commands/ralph-loop.md:1`).

The stop hook does the control-plane work:

- Looks for `.claude/ralph-loop.local.md` (`plugins/ralph-wiggum/hooks/stop-hook.sh:12`).
- Parses frontmatter for iteration, max iterations, and completion promise (`plugins/ralph-wiggum/hooks/stop-hook.sh:20`).
- Stops if max iterations have been reached (`plugins/ralph-wiggum/hooks/stop-hook.sh:50`).
- Reads the latest assistant text from the transcript (`plugins/ralph-wiggum/hooks/stop-hook.sh:69`).
- Detects completion promise inside `<promise>` tags (`plugins/ralph-wiggum/hooks/stop-hook.sh:114`).
- Atomically increments iteration with a temp file and `mv` (`plugins/ralph-wiggum/hooks/stop-hook.sh:152`).
- Returns JSON `{ "decision": "block", "reason": prompt, "systemMessage": msg }` so the host feeds the same prompt back (`plugins/ralph-wiggum/hooks/stop-hook.sh:165`).

This is extremely understandable, but it is also crude. It is a loop, not a scheduler. It relies on a transcript parser, a state file, and a hook return value. For Freyja, the lesson is not “implement scheduling in shell hooks.” The lesson is: simple operator-facing command files and explicit escape hatches are valuable. The actual scheduler should still live in the bridge runtime.

---

# Part III — Freyja’s current architecture

Freyja is already close to having the right internal insertion points.

## 1. Process and session topology

The bridge starts in `bridge/freyja_bridge.py:_main()`: it creates `_BridgeState`, ensures a boot session, then enters the stdin/stdout command loop (`bridge/freyja_bridge.py:522`). `_BridgeState` owns workspace, default model, active sessions, permission tier, and computer-use enablement (`bridge/freyja_bridge.py:5725`). Each session is an `_BridgeSession` with a provider, runner, tool registry, skill store, memory store, task board, inbox, and optional kanban/goal state (`bridge/freyja_bridge.py:1785`).

The agent loop lives in `AsyncAgentRunner._run_loop()`. It iterates while under max iterations and in running state, runs a pre-iteration hook, ensures context room, calls the provider, handles context pressure, executes tools, and repeats (`engine/runner.py:981`).

## 2. Turn queuing already solves idle-safe firing

The most important function for scheduling is `_schedule_or_queue_turn()` (`bridge/freyja_bridge.py:6123`). If a session already has a pending task, new content is appended to `sess.queued_messages` and a `message_queued` system event is emitted. Otherwise it creates a turn task.

That means a scheduler does not need to directly coordinate with the runner loop. It can fire a job by resolving a session and calling `_schedule_or_queue_turn(sess, prompt)`. Freyja already has the local equivalent of Claude Code’s “cron fires only when REPL is idle.”

## 3. Coordination strategies

Freyja has four coordination strategies (`bridge/tools/coordination.py:28`):

| Strategy | Meaning | Scheduled-loop relevance |
|---|---|---|
| `bus` | default message-bus coordination | good default for scheduled runs |
| `isolated` | task-first solo mode | useful for one scheduled objective with task tracking |
| `kanban` | board-driven swarm | scheduled runs can dispatch cards or check board state |
| `goal` | same-session judged continuation | loop/autopilot primitive, not a cron replacement |

The goal mode prompt already frames the session as an active objective and says Freyja will judge after each response and may continue automatically (`bridge/tools/coordination.py:125`). This should stay semantically separate from wall-clock scheduling.

## 4. Tool registry and progressive disclosure

Freyja’s tool registry already has `HOT`, `WARM`, and `COLD` tiers (`engine/tools.py:39`). Registered tools keep separate `summary_visible` and `schema_visible` flags; WARM tools can be summarized in the prompt without loading the full schema (`engine/tools.py:146`). The `tool_search` helper can promote a deferred tool by exact name (`engine/tools.py:198`).

A scheduler tool is a good WARM candidate:

- The model should know scheduling exists.
- The schema is non-trivial and should not always consume context.
- When the user asks to schedule something, `tool_search(schedule)` can load it.

## 5. Skills already use progressive disclosure

Freyja’s `SkillStore` discovers skills from `~/.freyja/skills`, `~/.claude/skills`, workspace `knowledge`, and workspace `.freyja/skills`, with later scopes shadowing earlier ones (`bridge/knowledge/skill_store.py:25`). It can list, search, and load skills. `build_prompt()` inserts an “Available Skills” section with names, types, confidence, and descriptions, and tells the model to call `load_skill(name)` when relevant (`bridge/knowledge/skill_store.py:166`). The actual `SkillRecord` has name, type, description, instructions, triggers, tags, error patterns, scope, path, confidence, and usage counts (`bridge/knowledge/models.py:171`).

That is simpler than Claude Code’s `Skill(skill,args)` tool and better aligned with Freyja. The gap is not “skills need to run schedules.” The gap is that scheduled jobs need a way to **attach** skills and preload their instructions at run time.

## 6. Persistence patterns

Freyja uses persistent sidecars for long-running session state:

- Transcripts: `~/.freyja/sessions/{session_id}.transcript.json` (`bridge/transcript_persistence.py:1`).
- Goal state: `~/.freyja/sessions/{session_id}.goal.json` (`bridge/transcript_persistence.py:33`).
- Task ledger: append-only JSONL at `~/.freyja/sessions/{session_id}.tasks.jsonl` (`bridge/task_journal.py:1`).
- Task board mutations are best-effort persisted and replayed on restore (`bridge/tools/task_board.py:105`).

For schedules, Freyja can follow the task/kanban journal style, but because schedules are global and time-sensitive, I recommend adding Hermes-style atomic/fsync writes for the global schedule index.

## 7. Existing background loops

Freyja already runs one timer-like loop: the kanban dispatcher. It starts an asyncio task when auto-dispatch is enabled and ticks every 30 seconds (`bridge/freyja_bridge.py:3554`, `bridge/freyja_bridge.py:3810`). It is session-scoped and board-specific, not a general scheduler. It proves that a background asyncio task in the bridge is the native pattern.

---

# Part IV — Design principles for Freyja

A good Freyja scheduler should optimize for three properties: robustness, reliability, and agent-understandable simplicity.

## 1. Robustness

Robustness means the scheduler should preserve its state across bridge restarts, avoid duplicate side effects, survive malformed persistence lines, expose enough audit data to debug failures, and treat scheduled prompts as untrusted input.

Concrete invariants:

- Persist jobs before acknowledging creation.
- Compute and persist the next fire time before firing a due job.
- Mark a run as `claimed` before queueing the turn.
- Mark completion/failure after the turn finishes if available.
- On restart, do not silently re-run claimed jobs unless the job explicitly asks for retry semantics.
- Scan scheduled prompts and preloaded skill bodies before execution.
- Save outputs/run metadata even if not shown to the user.

## 2. Reliability

Reliability has a product boundary. A scheduler inside the Freyja bridge can only run while the bridge is alive. It can survive bridge restarts if the app restarts, but it cannot fire while the app is closed or the machine is asleep unless Freyja installs a background LaunchAgent/daemon.

So Freyja should expose tiers honestly:

1. **Session schedule**: works while the session/app is open. Good for `/loop`, active polling, and short reminders.
2. **Local durable schedule**: persists across Freyja restarts, fires when Freyja is running. Good default for “remind me tomorrow” and recurring local checks.
3. **Daemon-backed durable schedule**: future. A macOS LaunchAgent starts a headless bridge/scheduler so jobs fire even if the UI is closed.
4. **Remote managed routine**: future. Requires Freyja cloud or an integration with external routine infrastructure.

Do not call a bridge-only job “remote” or “guaranteed” unless that runtime exists.

## 3. Agent-understandable simplicity

The trace’s `/loop` design is powerful but too implicit. Freyja should make the model’s action space small and typed.

Recommended agent-facing surface:

- One WARM tool: `schedule`.
- Actions: `create`, `list`, `pause`, `resume`, `remove`, `run`.
- A typed `schedule` object instead of a string the model must parse.
- Optional `skills` list, not an opaque skill `args` string.
- Runtime returns normalized `next_fire_at`, parsed cadence, job ID, and exact durability/scope.
- Use clear names: “scheduled job” for local jobs, “routine” only for cloud routines.

Avoid exposing internal sentinels like `<<autonomous-loop-dynamic>>` to the model. Sentinels are implementation details.

---

# Part V — Recommended Freyja implementation

## 1. New module: `bridge/scheduler.py`

Add a `SchedulerService` owned by `_BridgeState`.

The service should own:

- An in-memory map of `ScheduledJob` records.
- A min-heap or sorted list by `next_fire_at`.
- An `asyncio.Event` to wake the loop when jobs are created/updated.
- A global `asyncio.Lock` for state transitions.
- A persistence layer under `~/.freyja/schedules/`.

Suggested data model:

```python
@dataclass
class ScheduledJob:
    id: str
    name: str
    prompt: str
    schedule: ScheduleSpec
    scope: Literal["session", "durable"]
    session_id: str | None
    model_id: str | None
    coordination_strategy: str
    skills: list[str]
    enabled: bool
    max_fires: int | None
    fire_count: int
    created_at: float
    updated_at: float
    last_fire_at: float | None
    next_fire_at: float | None
    misfire_policy: Literal["skip", "fire_once"]
    overlap_policy: Literal["queue", "skip_if_running"]
```

Schedule spec should be typed:

```python
@dataclass
class OnceSchedule:
    kind: Literal["once"]
    run_at: str  # RFC3339 with timezone

@dataclass
class IntervalSchedule:
    kind: Literal["interval"]
    seconds: int

@dataclass
class CronSchedule:
    kind: Literal["cron"]
    expression: str  # five-field cron
    timezone: str | None
```

Start with `once` and `interval`. Add cron once the service, persistence, UI, and tests are stable.

## 2. Bridge integration

In `_main()`, after `_BridgeState` is created and the boot session is ensured, start the scheduler loop:

```python
state = _BridgeState(workspace=workspace, default_model=default_model)
await state.ensure_session(boot_session_id)
asyncio.create_task(state.scheduler.run_loop(), name="scheduler")
await _command_loop(state)
```

This insertion point is currently `bridge/freyja_bridge.py:569`.

In `_BridgeState.__init__`, initialize the service (`bridge/freyja_bridge.py:5728`):

```python
self.scheduler = SchedulerService(self)
```

When a job fires, the service should:

1. Acquire the scheduler lock.
2. Compute and persist the next fire time before queueing the turn.
3. Append a `run_claimed` event with `run_id`.
4. Resolve or create the session via `await state.ensure_session(...)` (`bridge/freyja_bridge.py:5746`).
5. Build the scheduled prompt, including any preloaded skills.
6. Call `_schedule_or_queue_turn(sess, content)` (`bridge/freyja_bridge.py:6123`).
7. Emit `system_event` with subtype `scheduler_job_queued`.

The scheduler does not need to call the runner directly. The session queue is already the correct boundary.

## 3. Persistence

Use a global schedule directory:

```text
~/.freyja/schedules/
  jobs.json              # current materialized job table, atomic/fsync write
  events.jsonl           # append-only audit log
  runs/{job_id}/{run_id}.json
  outputs/{job_id}/{run_id}.md
  .tick.lock             # future multi-process/daemon safety
```

Why both `jobs.json` and `events.jsonl`? Freyja’s tasks use JSONL only, but schedules need fast startup, stable sorted state, and cross-process lock semantics if a daemon exists later. A materialized JSON table with Hermes-style atomic writes is simpler to reason about for time-sensitive state. The JSONL audit log is for debugging and UI history.

If that feels too heavy for v1, use only `jobs.json` and add `events.jsonl` in v2. Do not use in-place JSON writes.

Persistence should copy Hermes’ atomic pattern: temp file, `json.dump`, flush, `os.fsync`, atomic replace (`cron/jobs.py:433`).

## 4. The `schedule` tool

Expose a single WARM tool. A compressed action surface is easier for agents than separate `CronCreate`, `CronList`, `CronDelete`, `ScheduleWakeup`, and remote routine tools.

Suggested schema:

```json
{
  "action": "create | list | pause | resume | remove | run",
  "job_id": "required except create/list",
  "name": "short human-readable label",
  "prompt": "self-contained prompt to enqueue when the job fires",
  "schedule": {
    "kind": "once | interval | cron",
    "run_at": "2026-05-22T15:00:00-07:00",
    "seconds": 300,
    "expression": "57 8 * * 1-5",
    "timezone": "America/Los_Angeles"
  },
  "scope": "session | durable",
  "session_id": "optional target session; default current session",
  "coordination_strategy": "bus | isolated | kanban | goal",
  "skills": ["skill-name"],
  "max_fires": 1,
  "misfire_policy": "skip | fire_once",
  "overlap_policy": "queue | skip_if_running"
}
```

Defaults:

- `scope`: `durable` for `once` and recurring jobs created from explicit user scheduling requests; `session` for loops/self-paced polling.
- `max_fires`: `1` for `once`; `null` for interval/cron.
- `misfire_policy`: `fire_once` for one-shot reminders; `skip` for recurring jobs.
- `overlap_policy`: `queue` by default, because `_schedule_or_queue_turn()` is safe. Use `skip_if_running` for high-frequency polling jobs.
- `coordination_strategy`: current session strategy unless specified.

Tool result should always return:

```json
{
  "job_id": "sched_...",
  "status": "created",
  "next_fire_at": "2026-05-22T15:00:00-07:00",
  "scope": "durable",
  "session_id": "desktop-...",
  "normalized_schedule": "every 5 minutes"
}
```

The key is that the model should never have to infer whether the job is durable, what the next fire time is, or whether its cron expression was accepted.

## 5. A `loop` convenience layer

Do not implement `/loop` as a generated skill body with hidden sentinels. Instead, implement loop as a thin convenience over `schedule`.

There are two possible surfaces:

1. A second WARM tool `loop`, with `action: start|list|stop`, `prompt`, `interval_seconds`, `until`, `self_paced`.
2. A slash command `/loop` in the UI that parses user syntax and calls the scheduler service directly.

I would start with the slash command and keep only one model-facing tool. From the model’s perspective, recurring work is `schedule.create` with `scope="session"` and possibly `overlap_policy="skip_if_running"`. Self-paced work can be represented as a scheduled job that re-schedules itself at the end of each run, but that can wait until the fixed-interval scheduler is stable.

If self-paced loops are implemented, keep the model-facing schema typed:

```json
{
  "action": "create",
  "kind": "self_paced_loop",
  "prompt": "check deploy status and decide whether to continue",
  "min_delay_seconds": 60,
  "max_delay_seconds": 3600
}
```

The runtime, not the agent, should own internal continuation IDs and cancellation.

## 6. Skill attachment for scheduled jobs

Scheduled jobs should support `skills: ["name"]`. At fire time:

1. Resolve each skill through `SkillStore.get()` / `SkillStore.load()` (`bridge/knowledge/skill_store.py:99`).
2. Include the skill body in the scheduled run message before the job prompt.
3. Include `[Skill directory: ...]` so relative paths are understandable.
4. Scan the assembled prompt after skill loading, as Hermes does (`cron/scheduler.py:1109`).
5. Record skill usage via `record_load()` (`bridge/knowledge/skill_store.py:117`).

This avoids relying on the scheduled agent to remember to call `load_skill` after waking. It also avoids the Claude Code issue where a skill’s body may contain prompt injection that was never scanned until runtime.

Do **not** make skills self-scheduling in v1. A `schedule:` field in `SKILL.md` is attractive but dangerous because dropping a file into a skill directory could create automation. If added later, it should require explicit enablement through the scheduler UI/tool.

## 7. Safety and permissions

Scheduled jobs run without the user actively watching. That changes the risk model.

Recommended rules:

- A scheduled job should inherit the current permission tier only when `scope="session"`.
- Durable jobs should store an explicit `permission_policy` snapshot, visible in `schedule.list`.
- High-risk durable jobs should require user confirmation at creation time.
- Jobs should be self-contained and should not ask clarifying questions at fire time.
- Jobs should not recursively create more scheduled jobs unless the user explicitly requests that.
- Prompt and loaded skill content should be scanned for high-confidence injection patterns before the run.
- The scheduler should emit a visible `system_event` before queueing a durable job, so the operator can see autonomous activity.

Freyja’s existing permission gate already blocks tool execution through UI futures when a tool exceeds the auto-approve tier (`engine/tools.py:146`, `bridge/freyja_bridge.py:1304`). That mechanism can remain, but durable jobs should avoid creating surprise permission prompts at 3am. The scheduler should surface the expected permission policy at creation time.

## 8. UI/operator visibility

Minimum event subtypes:

- `scheduler_job_created`
- `scheduler_job_updated`
- `scheduler_job_paused`
- `scheduler_job_removed`
- `scheduler_job_due`
- `scheduler_job_queued`
- `scheduler_job_skipped`
- `scheduler_job_failed`
- `scheduler_job_completed`

Freyja already emits structured `system_event` messages through stdout JSON lines (`bridge/freyja_bridge.py:6197`). Scheduler events should use that channel. Later, the renderer can add a Scheduled Jobs panel, but v1 can be chat-visible system events plus a `schedule.list` tool result.

## 9. Testing plan

Unit tests:

- Parse schedule specs: once, interval, invalid cron, timezone handling.
- Compute next fire time around DST boundaries if cron/timezones are included.
- Persist/load jobs with atomic writes.
- Replay after corrupt `events.jsonl` line.
- Pre-advance before fire.
- Misfire policy on restart.
- Overlap policy: queue vs skip.
- Skill attachment loads and scans assembled prompt.
- Permission policy serialization.

Integration tests:

- Create interval job with a fake clock and assert `_schedule_or_queue_turn()` receives prompt.
- Fire while session has pending turn; assert message queues, not drops.
- Remove job while sleeping; assert scheduler wakes and recalculates.
- Crash simulation: pre-advance persisted, run claimed, no duplicate fire on reload.
- Scheduled skill run: skill body included exactly once and relative path annotation present.

---

# Part VI — Implementation options

## Option A — In-app local scheduler

**Scope:** Bridge-owned asyncio scheduler, durable-on-disk jobs, fires only while Freyja is running.

**Pros:** Simple, fits current architecture, low risk, can ship quickly.  
**Cons:** Does not run while app is closed or machine is asleep.

This is the recommended v1.

## Option B — In-app scheduler plus LaunchAgent daemon

**Scope:** Same scheduler service, but a macOS LaunchAgent can start a headless bridge process to process durable jobs even when the UI is closed.

**Pros:** Much more reliable for reminders and daily routines.  
**Cons:** More installation, lifecycle, permissions, and multi-process locking work.

This is the recommended v2 after v1 semantics settle. The v1 design should include `.tick.lock` and atomic persistence so this upgrade path is not blocked.

## Option C — Remote managed routines

**Scope:** Cloud-side Freyja jobs equivalent to Claude’s `schedule` skill/`RemoteTrigger` routines.

**Pros:** Best reliability if implemented correctly; runs without the user’s machine.  
**Cons:** Requires cloud infrastructure, auth, environments, secrets, repo source snapshots, connector routing, and billing/cost boundaries.

This should not be the first implementation in `~/personal/freyja` unless Freyja already has a managed backend outside this repo.

## Option D — Skill-first scheduler

**Scope:** Add `schedule:` frontmatter to skills; scheduler discovers and runs them.

**Pros:** Elegant for reusable automations.  
**Cons:** Hidden automation risk, worse operator consent, still requires a scheduler service.

This should be v3 or a constrained opt-in feature.

---

# Part VII — Final recommendation

Build the scheduler in this order:

1. **`bridge/scheduler.py` with typed once/interval jobs.** Use `_BridgeState` ownership and `_schedule_or_queue_turn()` firing. Persist with atomic JSON. Emit system events.
2. **WARM `schedule` tool.** Single action-oriented schema. No separate `CronCreate`, `CronDelete`, `ScheduleWakeup` clones. The tool should normalize schedules and return `next_fire_at`.
3. **Skill attachment.** Let jobs specify `skills`. At fire time, preload and scan skill bodies before queueing the scheduled prompt.
4. **Skill bundles and resource-bundled skills.** Add Hermes-style same-context bundle aliases for repeated multi-skill workflows, and let skills carry lazy `references/`, `templates/`, `scripts/`, and `assets/`. Scheduled jobs should eventually accept `skill_bundles=[...]` as stable workflow profiles.
5. **At-most-once and audit.** Pre-advance before firing. Record run IDs and status. Do not duplicate side-effecting runs after crash.
6. **Cron expressions.** Add five-field cron with timezone after the interval/once path is stable. Either use `croniter` or a small parser; do not ask the model to perform calendar arithmetic for common cases.
7. **Loop convenience.** Add `/loop` as UI/slash sugar over scheduler jobs. Avoid hidden sentinels. If self-paced loops are needed, make them typed and runtime-owned.
8. **Goal idle continuation.** Separately improve Freyja’s goal loop by adding a Codex-style idle continuation task for active goals. Do not conflate this with cron scheduling.
9. **Daemon mode.** Once local semantics are right, add a macOS LaunchAgent/headless scheduler for durable jobs that should fire while the UI is closed.

The concise version: **Freyja should copy Hermes’ scheduler invariants and skill context economy, Codex’s explicit goal/skill state, and Claude Code’s operator ergonomics — but not Claude Code’s opaque `/loop` skill internals.** The agent-facing API should be typed, small, and honest about whether the work is session-local, local durable, daemon-backed, or remote. The skill-facing API should use progressive disclosure: metadata first, body on load, linked resources on demand, and explicit bundles for repeated multi-skill workflows.

---

## Appendix — Key references

### Trace extraction artifacts

- `trace_tool_schemas.md:4` — `CronCreate` schema and behavior.
- `trace_tool_schemas.md:81` — `ScheduleWakeup` schema and cache-aware delay guidance.
- `trace_tool_schemas.md:115` — `Monitor` schema and event-stream guidance.
- `trace_tool_schemas.md:157` — `RemoteTrigger` schema.
- `trace_tool_schemas.md:200` — `Skill` schema.
- `trace_relevant_conversation.md:1464` — loaded `schedule` skill body.
- `trace_final_skill_loop_answer.md:136` — exact `/loop` grammar recovered from bundled JS strings.

### Freyja

- `bridge/freyja_bridge.py:522` — bridge `_main()` entry point.
- `bridge/freyja_bridge.py:569` — `_BridgeState` creation and boot session initialization.
- `bridge/freyja_bridge.py:1785` — `_BridgeSession` per-session state.
- `bridge/freyja_bridge.py:5725` — `_BridgeState` process-level state.
- `bridge/freyja_bridge.py:6123` — `_schedule_or_queue_turn()`.
- `engine/runner.py:981` — `AsyncAgentRunner._run_loop()`.
- `engine/tools.py:39` — tool tiers.
- `bridge/knowledge/skill_store.py:25` — skill discovery and refresh.
- `bridge/knowledge/skill_store.py:166` — skill prompt listing.
- `bridge/knowledge/models.py:171` — `SkillRecord` shape.
- `bridge/task_journal.py:1` — append-only task journal pattern.
- `bridge/tools/coordination.py:28` — coordination strategy definitions.

### Hermes
- `cron/scheduler.py:1787` — scheduler `tick()`.
- `cron/scheduler.py:1829` — pre-advance for at-most-once semantics.
- `cron/scheduler.py:1905` — sequential/parallel job partitioning.
- `cron/scheduler.py:928` — wake-gate parser.
- `cron/scheduler.py:1109` — assembled cron prompt injection scan.
- `cron/scheduler.py:1588` — inactivity timeout.
- `cron/jobs.py:433` — atomic job storage writes.
- `cron/jobs.py:560` — job creation and normalized job record.
- `agent/skill_bundles.py:1` — YAML-defined same-context skill bundle aliases.
- `agent/skill_bundles.py:253` — bundle invocation message construction.
- `agent/skill_commands.py:160` — loaded skill message renderer and resource hints.
- `agent/prompt_builder.py:997` — skill index rendering and filtering.
- `tools/skills_tool.py:675` — metadata-only `skills_list()` progressive-disclosure tier.
- `tools/skills_tool.py:1121` — lazy linked-file loading through `skill_view(name, file_path)`.
- `tools/skills_hub.py:82` — downloaded `SkillBundle` package model.
- `tools/skills_hub.py:2903` — quarantine step for externally fetched skills.
- `tools/skills_sync.py:1` — bundled-skill manifest sync with user-edit protection.
- `tools/skill_usage.py:1` — sidecar skill usage/provenance telemetry.

### Codex

- `codex-rs/core/src/goals.rs:329` — goal runtime event dispatcher.
- `codex-rs/core/src/goals.rs:1289` — idle goal continuation.
- `codex-rs/core-skills/src/model.rs:11` — skill metadata model.
- `codex-rs/core-skills/src/render.rs:17` — available skill prompt instructions.
- `codex-rs/core-skills/src/invocation_utils.rs:29` — implicit skill invocation detection.

### Claude Code plugin example

- `plugins/ralph-wiggum/README.md:13` — Stop-hook loop concept.
- `plugins/ralph-wiggum/commands/ralph-loop.md:1` — slash command metadata.
- `plugins/ralph-wiggum/hooks/stop-hook.sh:12` — state file detection.
- `plugins/ralph-wiggum/hooks/stop-hook.sh:50` — max-iteration guard.
- `plugins/ralph-wiggum/hooks/stop-hook.sh:114` — completion promise detection.
- `plugins/ralph-wiggum/hooks/stop-hook.sh:152` — atomic iteration update.
- `plugins/ralph-wiggum/hooks/stop-hook.sh:165` — hook response that blocks stop and feeds the prompt back.
