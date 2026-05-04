# Freyja Skills and Memory Design

This document maps the simple `agent-harness` memory and skills implementation
to a Freyja-native design. The goal is to make the existing Skills and Memory
panels useful product surfaces instead of placeholder counters.

## What Agent Harness Does Today

`agent-harness` has a clear but intentionally simple split:

- User preferences are personal memory: how the user wants the agent to work.
- Platform learnings are procedural memory: what the agent learned about how to
  complete a class of tasks.
- Procedural memory is represented as skills.

The key files are:

- `/Users/sohamshah/work/services/agent-harness/agent_harness/cli/tools/memory_tools.py`
- `/Users/sohamshah/work/services/agent-harness/agent_harness/preferences.py`
- `/Users/sohamshah/work/services/agent-harness/agent_harness/skills/models.py`
- `/Users/sohamshah/work/services/agent-harness/agent_harness/skills/db.py`
- `/Users/sohamshah/work/services/agent-harness/agent_harness/skills/seed.py`
- `/Users/sohamshah/work/services/agent-harness/agent_harness/knowledge/tools.py`
- `/Users/sohamshah/work/services/agent-harness/agent_harness/knowledge/integration.py`

### User Preferences

The current implementation stores user preferences in PostgreSQL rows. The
tool exposes CRUD operations: add, list, update, delete. Each row carries
category, user id, session id, session name, and creation time. The prompt
builder formats these rows into a bounded `<START: MEMORY>` section.

Older `MEMORY.md` usage still exists in the repo and shows the prior shape:
timestamped markdown entries plus some larger scenario notes. That format is
portable and easy to inspect, but it is weak for dedupe, provenance, edit/delete,
and selective retrieval.

### Skills

Skills are database rows with markdown compatibility. The model is:

- `name`
- `skill_type`: `build` or `guard`
- `description`
- `instructions`
- `triggers`
- `tags`
- `error_patterns`
- `severity`
- `source`
- `version`
- usage counters: retrievals, success signals, failure signals
- `confidence`: `unvalidated`, `experimental`, `verified`, `deprecated`

Seed skills are read from `knowledge/{build,guard}/*/SKILL.md` and upserted into
the database. A skill can also be rendered back into `SKILL.md` with frontmatter.

The active retrieval design is progressive disclosure:

1. Inject a compact skill index into the system prompt.
2. Let the model call `load_skill(name)` when it knows the relevant skill.
3. Fall back to `search_skills_from_db(query)` when the index is insufficient.
4. Track whether loaded/injected skills correlated with successful turns.

That separation is worth keeping. Metadata is cheap enough to show early; full
instructions are expensive and should be loaded only when useful.

### Learning Loop

`record_ema_platform_learning` writes a skill when the agent discovers a
reusable pattern or a non-obvious failure. It strongly nudges the agent to:

- name skills by structural pattern, not by one-off use case
- update an existing skill instead of creating duplicates
- use build skills for "how to do this"
- use guard skills for "what breaks and how to recover"
- include wrong and correct versions, complete examples, error text, and why
  the platform behaves that way

Outcome tracking is simple but valuable: after turns, skill retrieval counts and
success/failure signals drive confidence changes. This gives the UI something
real to show and gives retrieval a reason to prefer proven skills.

### Skill Pruning

The most reusable part of `agent-harness` is the skill pruning loop:

- Loaded skills are tracked per session by name, skill type, load turn, and
  estimated token count.
- Maintenance only triggers under real pressure: at least 5 loaded skills, at
  least 5,000 loaded skill tokens, at least 50,000 session input tokens, and no
  maintenance run since the last new skill was loaded.
- The system asks the working agent, not a separate judge, to review currently
  loaded skills. This matters because the working agent has the task context.
- The maintenance call uses a structured `review_skills` schema with one
  decision per loaded skill.
- Valid actions are `keep` or `prune`.
- Valid reasons are `actively_using`, `needed_soon`, `task_completed`,
  `never_relevant`, `superseded`, `low_value`, and `causing_confusion`.
- Pruned skill tool results are replaced with compact reloadable stubs instead
  of deleting the tool call from history.
- The maintenance exchange itself is removed from the conversation transcript.

The reason enum is the important bit. A pruned skill is not automatically a bad
skill. `task_completed` is positive: the skill helped and is no longer needed.
`never_relevant` is a strong routing failure. `superseded` is a mild negative.
`low_value` is neutral because it may only mean the skill was not worth the
current token cost. `causing_confusion` is a strong quality failure.

Freyja should copy this pattern almost directly, with file-backed counters
instead of a required database.

## What Freyja Does Today

Freyja already has the visible shell:

- `src/shared/events.ts` defines `Skill`, `skill_retrieved`, and `skill_updated`.
- `src/renderer/components/Sidebar.tsx` renders Skills and Memory sections.
- `src/renderer/components/CommandPalette.tsx` includes skill commands.
- `src/renderer/state/store.ts` persists retrieved/updated skills in renderer
  state.

The live bridge implementation is still thin:

- `bridge/tools/memory_tools.py` only appends `record_user_preference` calls to
  `MEMORY.md`.
- `bridge/freyja_bridge.py` handles `list_skills` by reading
  `<workspace>/knowledge/index.jsonl` if it exists.
- The bridge system prompt does not currently load `MEMORY.md` or a skills
  index.
- There is no live `load_skill` tool.
- There is no live `search_skills` tool.
- There is no durable memory list, memory event, memory retrieval reason, or
  memory edit/delete flow.
- The Memory sidebar count is hardcoded to `0`.

So the UI suggests a real knowledge layer, but the runtime does not yet have
one.

## Product Goal

Freyja should make memory and skills observable, editable, and useful during
work. The user should be able to answer:

- What did the agent remember about me?
- What did the agent remember about this project?
- Which memories affected the current turn?
- Which skills were available?
- Which skills were actually loaded?
- Why did the agent load them?
- Did a skill help or hurt?
- Can I inspect, edit, forget, pin, or promote this knowledge?

The agent should benefit without bloating context:

- Inject only relevant memories.
- Show skill metadata early, not full skill bodies.
- Load full skill content only on demand.
- Record learning candidates only when there is reusable value.
- Keep provenance so bad memories and skills can be audited.

## Proposed Architecture

Use a local-first knowledge layer with structured files and tiny in-memory
indexes. Freyja is a desktop app; we do not need to get fancy with Postgres or
SQLite unless the file-backed path becomes a proven bottleneck. Simplicity and
predictable performance are more important than query-engine sophistication.

The practical design is:

- Markdown skill bodies under project/user skill directories for portability.
- JSONL append logs for memory, usage counters, pruning decisions, and audit.
- Cached `index.jsonl` files generated from `SKILL.md` frontmatter and refreshed
  by mtime/hash.
- In-memory maps for hot lookup: `name -> skill`, `trigger -> skill ids`,
  `tool -> skill ids`, `file glob -> skill ids`, `error pattern -> guard ids`.
- Plain file search fallback (`rg`/glob/read) for unusual queries.
- Compatibility importers for existing `MEMORY.md`, `knowledge/index.jsonl`,
  `.codex/skills`, `.agents/skills`, `.claude/skills`, and project `.freyja`.

Only add SQLite later if there is a measured problem that flat files cannot
solve. At the expected scale, JSONL plus a cached in-memory index is faster to
build, easier to debug, and easier to recover.

### Storage Scopes

Memory scopes:

- `user`: preferences that apply across workspaces.
- `project`: repo-specific facts, commands, conventions, architecture notes.
- `session`: temporary notes for the current long-running session.
- `subagent`: child-agent findings that may be promoted to session/project.

Skill scopes:

- `project`: `<workspace>/.freyja/skills/<name>/SKILL.md`
- `user`: `~/.freyja/skills/<name>/SKILL.md`
- `compat`: read-only imported skills from known external locations
- `plugin`: future plugin-provided skills

### Memory Item Model

```text
memory_id
scope: user | project | session | subagent
kind: preference | project_fact | workflow | decision | warning | entity | note
text
summary
tags[]
applies_to[]       # workspace paths, tools, commands, file globs, agent types
confidence
status: active | draft | archived | rejected
source_session_id
source_message_id
source_tool_call_id
created_at
updated_at
last_retrieved_at
retrieval_count
evidence[]         # short source snippets or event ids
```

This avoids the main weakness of `MEMORY.md`: Freyja can list, search, edit,
delete, dedupe, and explain memory items individually.

### Skill Model

```text
skill_id
scope
name
skill_type: build | guard | reference | workflow | tool
description
body_path
triggers[]
tags[]
error_patterns[]
file_globs[]
tools[]
agent_types[]
depends_on[]
composes_with[]
version
source
created_by: seed | user | agent | imported | plugin
confidence: unvalidated | experimental | verified | deprecated
retrieval_count
load_count
success_signals
failure_signals
last_loaded_at
```

The first implementation can keep `build` and `guard` as the only active types,
while accepting the richer enum in the UI and schema.

## Runtime Flow

### Turn Start Context Builder

Before each model call, Freyja should compile an ephemeral context bundle:

1. Collect signals:
   - user message
   - active workspace
   - active files or mentioned paths
   - recent tool errors
   - current agent type
   - available tools
   - previous turn outcome
2. Retrieve memory candidates:
   - exact scope match first
   - FTS/keyword search over memory text and tags
   - path/tool/agent-type filters
   - recency and confidence as tie breakers
3. Retrieve skill candidates:
   - trigger match
   - tags and tool names
   - file globs
   - error pattern match for guard skills
   - optional light reranker later
4. Compile a bounded context section:
   - user/project memories that are relevant now
   - compact skill index with names, descriptions, confidence, and why matched
   - instruction to call `load_skill(name)` for full content
5. Emit UI events:
   - `memory_retrieved`
   - `skill_retrieved`
   - `knowledge_context_built`

Do not append this bundle permanently to the transcript. It should be a dynamic
system-context layer for the current provider call. That keeps long sessions from
accumulating stale repeated memory blocks.

### Skills Progressive Disclosure

Freyja should expose three skill tools:

- `list_skills`: list skill metadata with filters.
- `search_skills`: search metadata and optionally content.
- `load_skill`: load the full markdown body by exact name.

The base prompt should include a small relevant skill index, not every full
skill. `load_skill` should emit `skill_loaded` so the UI can distinguish
"available" from "actually used".

### Skill Context Pruning

Every `load_skill` result is useful context at first, but it becomes stale
context tax in long sessions. Freyja should implement pressure-driven skill
maintenance from the beginning.

Track every loaded skill in the session:

```text
name
skill_type
loaded_turn
token_count
tool_call_id
message_id
status: loaded | pruned
last_decision_reason
```

Run maintenance only when all thresholds are met:

```text
loaded_skill_count >= 5
loaded_skill_tokens >= 5_000
session_input_tokens >= 50_000
new_skill_loaded_since_last_maintenance == true
```

When triggered:

1. Build a loaded-skill inventory with name, type, loaded turn, and token count.
2. Make a synthetic structured call asking the active agent to review all loaded
   skills.
3. Force the `review_skills` schema for that call only.
4. Replace pruned `load_skill` tool-result bodies with compact stubs:
   `[Skill: name - PRUNED (reason). Call load_skill('name') to reload if needed.]`
5. Remove pruned skills from the loaded-skill tracker.
6. Append a pruning decision event to the skill usage JSONL.
7. Delete or avoid persisting the synthetic maintenance exchange.
8. Emit a UI activity event with pruned count and tokens freed.

Reason-to-signal mapping:

```text
actively_using     -> success +1.0
needed_soon        -> no update
task_completed     -> success +1.0
never_relevant     -> failure +1.0
superseded         -> failure +0.3
low_value          -> no update
causing_confusion  -> failure +1.5
```

This gives Freyja both performance wins and learning signals. It bounds stale
skill context without reducing what the agent can do, because every pruned skill
can be reloaded by name.

### Memory Tools

Replace the one append-only memory tool with CRUD tools:

- `record_memory`: create a user/project/session memory.
- `search_memory`: search memory items and explain matches.
- `list_memory`: list by scope/kind/status.
- `update_memory`: edit a memory item.
- `forget_memory`: archive/delete a memory item.
- `promote_memory`: session/subagent memory -> project/user memory.

Keep `record_user_preference` as an alias for `record_memory(kind=preference,
scope=user)` so existing model behavior still works.

### Learning Flow

Freyja should not write durable knowledge for every turn. Most turns should
produce zero memory writes.

Good capture triggers:

- explicit user instruction: "remember this", "always", "never"
- repeated user correction
- a tool or workflow failure that required trial and error
- a project-specific command, convention, or architecture fact
- a solved bug pattern likely to recur
- a subagent finding that the parent reused successfully

Learning candidate states:

- `draft`: captured but not trusted
- `active`: available for retrieval
- `verified`: enough successful use
- `deprecated`: low value or superseded
- `rejected`: user or maintainer rejected it

For generated skills, use the `agent-harness` discipline:

- name by structural pattern
- search before creating
- update related skills instead of fragmenting
- include wrong/correct examples
- include exact error text for guard skills
- include validation steps

## UI Design

### Sidebar Skills

The Skills section should become a compact live status panel:

- Active: full skills loaded in the current turn/session.
- Suggested: relevant skills in the current context but not loaded.
- Library: searchable global/project skill list.
- Each row shows type, confidence, retrieval/load counts, and match reason.
- Actions: open, load, pin, hide, mark helpful, mark harmful.

The current row design can be retained, but rows need a detail popover or modal.
The important addition is causality: "loaded because error matched X" or
"suggested because path matched `*.tsx` and trigger matched React".

### Sidebar Memory

The Memory section should not be a generic `MEMORY.md` note. It should show:

- Applied this turn: memories that affected the current response.
- Captured this session: new draft memories.
- User preferences: stable user-level preferences.
- Project memory: repo-specific facts and workflows.

Each memory row should show scope, kind, short text, source, confidence, and
last used time. Actions should include edit, forget, pin, promote, and open
source.

### Activity Panel

Knowledge should be visible in the timeline:

- memory retrieved
- memory captured
- skill suggested
- skill loaded
- skill pruned
- skill updated
- skill confidence changed

This makes knowledge behavior debuggable. If the agent gives a weird answer,
the user can see whether a bad memory or stale skill caused it.

### Artifact Workspace

Add a Knowledge workspace view with tabs:

- Memories
- Skills
- Drafts
- Outcomes

Useful views:

- Skill body preview with frontmatter and usage stats.
- Skill diff when a learning tool updates a skill.
- Memory editor with provenance and retrieval history.
- "Used in this session" filter.

## Subagent Behavior

Subagents should not inherit the entire parent memory and skill catalog. They
should inherit a compiled, task-specific context bundle:

- memories relevant to the delegated task
- skills relevant to the delegated task and agent type
- parent instructions that constrain output expectations

Subagents may propose memory or skill candidates, but durable user/project writes
should be mediated by the parent session. This keeps parallel subagents from
creating contradictory durable knowledge while still letting them contribute
findings.

For shared memory coherence:

- session memories are append-only during a run
- project/user memories update through a single parent-owned writer
- UI receives invalidation/update events when memory or skills change
- subagents can read new active knowledge on their next turn

## Performance Constraints

This design should improve behavior without sacrificing responsiveness:

- Keep only metadata in the hot path.
- Load skill bodies lazily.
- Use small in-memory indexes for metadata retrieval.
- Use plain file search as the slow path for unusual queries.
- Add SQLite only after measured flat-file/index limits.
- Debounce filesystem scans.
- Cache parsed frontmatter and invalidates by mtime/hash.
- Cap dynamic context tokens per call.
- Render memory/skill lists with lazy detail loading.
- Never stream large skill or memory bodies into React lists by default.

The goal is not to restrict agent capability. The goal is to stop carrying
irrelevant knowledge in prompt or renderer state.

## Implementation Plan

### Slice 1: File-Backed Knowledge Indexes

Add a bridge knowledge package:

- `bridge/knowledge/models.py`
- `bridge/knowledge/memory_store.py`
- `bridge/knowledge/skill_store.py`
- `bridge/knowledge/prompt.py`

Implement:

- parse existing `<workspace>/MEMORY.md`
- create/read `~/.freyja/knowledge/memory.jsonl`
- create/read `~/.freyja/knowledge/skill_usage.jsonl`
- discover `.freyja/skills`, `~/.freyja/skills`, and `knowledge/{build,guard}`
- parse `SKILL.md` frontmatter
- import `knowledge/index.jsonl` compatibility entries
- build small in-memory maps from those files
- build a compact memory prompt section
- build a compact relevant-skill metadata section
- inject relevant memory and skill metadata at session start
- emit `memory_updated` and `memory_retrieved`
- emit `skill_retrieved` and `skill_updated`
- render real memory and skill rows in the sidebar

This immediately makes the Memory and Skills cards truthful without introducing
a database.

### Slice 2: `load_skill` and Sidebar States

Add:

- `bridge/tools/skill_tools.py`

Implement:

- expose `list_skills`, `search_skills`, `load_skill`
- track loaded skills by session
- emit `skill_loaded`, `skill_pruned`, and `skill_updated`
- show Active/Suggested/Library states in the sidebar

This turns Skills from a passive display into a usable progressive-disclosure
system.

### Slice 3: Skill Pruning Maintenance

Implement the `agent-harness` pruning loop in Freyja:

- loaded-skill tracker in bridge session state
- pressure threshold check after turns
- synthetic structured `review_skills` call
- reason enum and file-backed usage-signal update
- replacement of pruned skill results with compact stubs
- Activity Panel event with tokens freed
- Sidebar state transition from loaded to pruned/reloadable

This is a direct performance improvement for long sessions and gives the skill
library better quality signals.

### Slice 4: Dynamic Context Builder

Add a runner/bridge hook that compiles per-turn knowledge without adding it to
the persisted transcript.

Implement:

- turn signal collection
- memory retrieval
- skill retrieval
- guard-skill retrieval on tool errors
- bounded dynamic context
- `knowledge_context_built` activity event

This is where Freyja starts using knowledge automatically instead of just
displaying it.

### Slice 5: Learning Tools

Upgrade memory and skill writes:

- make `record_user_preference` CRUD-compatible
- add `record_project_memory`
- add `record_skill_learning`
- add draft/update flow for generated skills
- add search-before-create guidance in tool descriptions
- show generated memory/skill candidates in the UI

This gives the agent a way to improve itself across runs.

### Slice 6: Outcomes and Confidence

Track loaded skills and memory use through turn completion:

- record retrieval and load counts
- correlate skill use with success/failure
- detect tool errors as negative signals
- allow user correction: mark helpful/harmful
- update confidence lifecycle
- expose outcome stats in UI

This makes Freyja's skill library compound instead of just grow.

## Recommended First Build

Start with Slice 1 and Slice 2 together:

1. Local memory store with real sidebar rendering.
2. Skill discovery from project/user directories.
3. `load_skill` tool.
4. Skill sidebar states: suggested vs loaded.
5. File-backed usage counters.
6. Basic prompt injection of relevant memory and skill metadata.

That creates immediate product value without touching subagent limits, screenshot
limits, or autonomy settings. It also lays the data model needed for the later
learning loop. The next immediate slice after that should be skill pruning,
because it directly protects long-session performance while preserving full
capability through reloadable skill stubs.
