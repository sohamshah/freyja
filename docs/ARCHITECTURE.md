# Freyja: Architecture of a Self-Improving Desktop Agent

> **Status:** Living document under active research. Updated 2026-04-12.

## Abstract

We describe the architecture of Freyja, a universal desktop AI agent that operates as a native macOS application (Electron + React frontend, Python engine backend) and orchestrates multiple LLM providers to handle diverse knowledge work — code, documents, communication, native app control, and data analysis. The system's central contribution is a unified memory layer that manages skills, episodes, and conversation state as a coherent context budget, enabling hours-long sessions without degradation. We adopt the [agentskills.io](https://agentskills.io) open standard for reusable instruction packages, extend it with a confidence lifecycle and evolutionary optimization pipeline inspired by [GEPA](https://arxiv.org/abs/2507.19457) (ICLR 2026 Oral), and introduce a multi-resolution episodic memory system informed by [MemRL](https://arxiv.org/abs/2601.03192) that applies non-parametric reinforcement learning in memory space rather than in model weights. Tool management follows a three-tier progressive disclosure scheme that reduces schema token overhead by approximately 90%, which we show is necessary to avoid the tool selection accuracy cliff observed above 200 tools in the [RAG-MCP benchmark](https://arxiv.org/abs/2505.03275). The permission model mirrors Claude Code's six-tier system with glob-pattern allow/deny rules, extended with PreToolUse hooks for programmatic safety gating. Computer control operates through a Rust native extension (pyo3) that exposes macOS accessibility APIs and CoreGraphics screen capture, running as an isolated sub-agent with its own context window, safety mechanisms, and episode production. All configuration, skill definitions, agent definitions, and memory are stored as human-readable Markdown with YAML frontmatter, suitable for version control and team sharing.

---

## 1. Introduction

The dominant paradigm in AI-assisted software development treats the agent as a stateless tool — each session starts from scratch, context is managed implicitly by the provider, and the agent's capabilities are fixed at deployment time. This works tolerably for short coding tasks but breaks down for the kind of sustained, multi-modal knowledge work that a desktop agent should support: researching across documents and APIs, drafting communications, automating repetitive GUI workflows, and gradually learning a user's preferences and project conventions.

Three problems motivate Freyja's architecture. First, **context is scarce**: even 200K-token windows fill quickly when tool schemas, file contents, and conversation history compete for space, and the [MemoryArena benchmark](https://arxiv.org/abs/2603.07670) demonstrates that models which are near-perfect on passive recall "plummet to 40-60%" on interdependent multi-session agentic problems, confirming that long context is not memory. Second, **skills are fragile**: hand-written instructions degrade as codebases evolve, and without a feedback loop the agent has no mechanism to distinguish helpful instructions from harmful ones. Third, **tool explosion is real**: connecting three typical MCP servers (GitHub, Slack, Sentry) consumes approximately 94,000 tokens of schema overhead — 47% of a 200K window — before any conversation begins, as measured by [MindStudio](https://www.mindstudio.ai/blog/claude-code-mcp-server-token-overhead).

We address these problems with a layered architecture whose components are described in the sections that follow. Where possible, we adopt existing standards and patterns rather than inventing new ones, and we note explicitly where our design diverges from prior work and why.

---

## 2. Related Work

Our design draws on several families of prior work, which we summarize here to clarify what we adopt, what we extend, and what we depart from.

**Agent frameworks.** [Claude Code](https://code.claude.com) provides the closest reference architecture: a `while(tool_call)` loop with no DAGs or classifiers, eight core tools, a skill system based on Markdown files with YAML frontmatter, and a compaction mechanism that fires at approximately 83.5% of context capacity. We adopt its permission model, skill format, and compaction survival rules essentially unchanged, extending them with confidence tracking and evolutionary optimization. [OpenHands](https://docs.openhands.dev) contributes the Condenser abstraction — a pluggable interface for context compression with multiple implementations (recent-events, LLM-summarizing, amortized-forgetting) — which informed our layered eviction design. [Cursor](https://cursor.com/learn/context) takes a fundamentally different approach, treating each conversation as short-lived and relying on codebase indexing to re-fetch context on demand rather than compressing a growing transcript; we consider this a valid alternative for IDE-integrated tools but unsuitable for a long-running autonomous agent. [Devin](https://docs.devin.ai) operates as a compound AI system with specialized models for planning, coding, review, and browsing, which validates our multi-model routing approach but requires full cloud infrastructure that we avoid.

**Skills and instruction packages.** The [agentskills.io](https://agentskills.io/specification) open standard, adopted by 30+ agent tools including Claude Code, Cursor, Gemini CLI, and GitHub Copilot, defines a SKILL.md format with YAML frontmatter and a Markdown body, optional supporting files, and a progressive disclosure pattern (description at discovery, full body on activation). [Hermes Agent](https://hermes-agent.nousresearch.com/docs/) extends this standard with a self-evolution pipeline that reads execution traces, diagnoses failures, generates candidate mutations via GEPA, and gates promotion through five mandatory constraint checks. We adopt both the standard and the evolution pipeline, adapting the latter to our confidence lifecycle. The [Externalization survey](https://arxiv.org/abs/2604.08224) (Zhou et al., April 2026) provides the theoretical framework we adopt for understanding skills as carriers of three coupled components — operational procedure, decision heuristics, and normative constraints — and traces the evolution from atomic tool primitives through large-scale tool selection to the current paradigm of skills as packaged expertise. [SkillClaw](https://arxiv.org/abs/2604.08377) (Ma et al., April 2026) demonstrates cross-user skill evolution via a dedicated evolver agent that aggregates trajectories, performs causal chain analysis, and generates evidence-backed mutations achieving +42.1% improvement. [AgentSkillOS](https://arxiv.org/abs/2603.02176) proves that flat skill invocation fails catastrophically at scale (Bradley-Terry score 17.2 at 200K skills) while dependency-aware DAG orchestration achieves 100.0, establishing that skill composition is as critical as skill retrieval. [Act Wisely](https://arxiv.org/abs/2604.08545) (Yan et al., April 2026) introduces meta-cognitive tool use — where the agent first assesses whether it genuinely needs external computation — reducing tool invocation from 98% to 2% while improving accuracy, which fundamentally changes our approach to skill activation and tool selection.

**Episodic memory.** [MemRL](https://arxiv.org/abs/2601.03192) (arXiv 2601.03192) demonstrates that a frozen LLM augmented with an evolving memory bank of Intent-Experience-Utility (IEU) triplets, where utility scores are updated via exponential moving average after each episode, achieves a 56% gain on multi-step tasks (ALFWorld) and a forgetting rate of 0.041 versus 0.051 for the baseline. The two-phase retrieval mechanism — similarity-based recall followed by value-aware ranking that downweights semantically similar but historically unsuccessful memories — is the key differentiator from naive RAG, and we adopt it for our episode retrieval system. The [episodic memory position paper](https://arxiv.org/abs/2502.06975) argues that episodic memory is "the missing piece" for long-term agents, enabling single-shot learning of instance-specific contexts that neither parametric updates nor semantic memory can provide.

**Prompt optimization.** [GEPA](https://arxiv.org/abs/2507.19457) (ICLR 2026 Oral) evolves textual parameters using LLM-based diagnostic reflection and Pareto-efficient search, outperforming GRPO by 6% on average (up to 20%) while requiring 35x fewer rollouts, at a cost of approximately $2-10 per optimization run. [TextGrad](https://arxiv.org/abs/2406.07496) (Nature 2024) offers a simpler gradient-based alternative but lacks GEPA's diversity-preserving Pareto frontier, which makes it less suitable for multi-objective skill optimization. [DSPy](https://dspy.ai) provides the compilation framework; its core lesson — that bootstrapping from successful execution traces is the most reliable way to improve prompts — directly informs our skill mutation operators.

**Context management.** [Factory.ai](https://factory.ai/news/evaluating-compression) demonstrates that structured anchored summarization (3.70/5 quality) outperforms both Anthropic's (3.44/5) and OpenAI's (3.35/5) approaches across 36K production messages. [JetBrains](https://github.com/JetBrains-Research/the-complexity-trap) (NeurIPS DL4Code 2025) shows, perhaps counterintuitively, that observation masking — replacing old tool outputs with `[details omitted]` rather than summarizing — achieves +2.6% solve rate on SWE-bench Verified with 52% cost reduction, because LLM summarization paradoxically *extends* agent runs by masking failure signals. The [BATS](https://arxiv.org/abs/2511.17006) budget-awareness signal reduces speculative searches by 40.4% at near-zero cost. We combine all three techniques in our layered eviction strategy.

**Model routing.** [RouteLLM](https://arxiv.org/abs/2406.18665) (LMSYS/Berkeley) achieves 95% of GPT-4 quality with only 26% GPT-4 calls using a trained matrix-factorization router, while [CascadeFlow](https://github.com/lemony-ai/cascadeflow) implements speculative cascading that handles 60-70% of queries with small models at 40-85% cost reduction. We adopt cascading for sub-agents and defer classifier-based routing to a later phase, starting with zero-overhead rule-based routing keyed on the invoked skill or tool type.

**Codebase understanding.** [Aider](https://aider.chat/2023/10/22/repomap.html) contributes the most rigorous approach to repository mapping: a tree-sitter-parsed directed graph where nodes are files and edges represent cross-file symbol references, ranked via personalized PageRank with chat-active files receiving 100x weight, and serialized within a configurable token budget (~1,024 tokens default, 8x when no files are active). Aider's [edit format research](https://aider.chat/docs/unified-diffs.html) — which tested six formats across multiple models — found that unified diffs reduce LLM laziness by 3x versus search/replace blocks, that removing line numbers from hunk headers significantly improves performance, and that function-call-based edit formats consistently underperform plain-text formats. Their [architect mode](https://aider.chat/2024/09/26/architect.html), which separates planning (strong model, natural language) from editing (potentially cheaper model, structured format), produced state-of-the-art results (85% with o1-preview + DeepSeek) by letting each model focus on one task. [Augment Code](https://www.augmentcode.com/context-engine) takes a different approach, building a custom semantic graph that maps relationships between functions, classes, modules, and data flows across 400,000+ files, with purpose-trained embedding models (generic embeddings miss that "callsites are not necessarily similar to function definitions"), and exposes this graph as an [MCP server](https://docs.augmentcode.com/context-services/mcp/overview) usable by any agent — positioning their context engine as infrastructure rather than a product feature.

**Agent specialization.** [ForgeCode](https://forgecode.dev) implements a three-agent architecture — FORGE (implementation, read+write), MUSE (planning, writes only to `plans/`), and SAGE (internal research, read-only) — where the analytical agent cannot trigger code changes, preventing premature edits during investigation. Their semantic entry-point discovery, which runs a lightweight pass to identify probable starting files before exploring code, converts undirected browsing into targeted traversal and reportedly reduces context size by approximately 90%. [Roo Code](https://docs.roocode.com) extends Cline with a custom modes system that scopes tool permissions per persona (e.g., a security-reviewer mode restricted to `edit` only on `\.(md|mdx)$` files via regex), and a "Boomerang Tasks" hub-and-spoke sub-agent model where each subtask executes in complete isolation with its own conversation history and only its summary returns to the parent — architecturally similar to our episode model. [Slate](https://randomlabs.ai/blog/slate) (Random Labs, YC) is the most explicit implementation of the thread-weaving pattern we adopt: an orchestrator that reasons at the strategic level using a TypeScript DSL dispatches parallel worker threads, each of which produces an *episode* containing only successful tool calls and conclusions, and new workers are seeded with relevant episodes rather than the full project history.

**Full-stack generation and sandboxing.** [Bolt.new](https://bolt.new) runs a complete Node.js environment inside the browser via StackBlitz's WebContainer (WebAssembly), parsing structured XML tags from the LLM stream into file writes and shell commands that execute progressively as tokens arrive — a pattern worth adopting for any agent that needs to act during generation rather than after. [v0](https://v0.dev) (Vercel) uses a composite model pipeline — RAG-based intent detection, frontier LLM, a streaming "LLM Suspense" layer that rewrites tokens mid-stream (e.g., correcting icon imports within 100ms), and a custom AutoFix model trained via reinforcement fine-tuning — achieving 93% error-free generation versus approximately 62% baseline. [E2B](https://e2b.dev) provides Firecracker microVM sandboxes with approximately 150ms boot time and hardware-level isolation, substantially faster and more secure than Docker containers; their pause/resume mechanism serializes entire VM memory state (not just filesystem), which is critical for long-running agent tasks that need checkpoints.

**Observability.** [AgentOps](https://agentops.ai) defines nine span types specifically for agent tracing (Agent, Reasoning, Planning, Workflow, Task, Tool, LLM, Evaluation, Guardrail), which is substantially more informative than generic request-level tracing. [Braintrust](https://braintrust.dev) contributes the pattern of converting production trace failures into regression test cases with a single click, which we consider the most practical approach to building evaluation suites for an agent that operates in diverse, unpredictable environments. The industry is converging on [OpenTelemetry](https://opentelemetry.io) as the wire format for agent tracing, which we plan to adopt.

**Type-safe agent design.** [Pydantic AI](https://ai.pydantic.dev) models agents as a graph-based state machine (UserPromptNode → ModelRequestNode → CallToolsNode → loop) with typed tools via decorators, dependency injection through `RunContext`, and output validation with automatic `ModelRetry` that sends the validation error back to the LLM for self-correction. Their GEPA integration pattern (`EvalsGEPAAdapter`) optimizes field descriptions, tool descriptions, and system instructions in three phases, claiming 40-60% improvement in tool call accuracy.

**Editor and terminal UX.** [Zed](https://zed.dev/docs/ai/overview) implements CRDT-based streaming diffs that merge AI edits token-by-token into the buffer without conflicts, a multi-buffer review pane for cross-file changes, and an agent panel that is itself an editable text buffer rather than a chat widget — an approach that provides full transparency into the LLM request. [Warp](https://www.warp.dev) contributes a local natural-language classifier that routes terminal input to either the shell or the agent with no latency, a visual context capacity indicator (yellow at 75%, red at 90%), and a `/plan` command for pre-execution alignment.

---

## 3. Architecture Overview

Freyja's architecture is organized around a unified *memory layer* that mediates between persistent stores (skills, episodes, preferences, project instructions) and the finite context window of the LLM on each turn. We treat the context window as scarce RAM with a layered eviction strategy, rather than as an append-only log that eventually overflows into compaction.

```
┌──────────────────────────────────────────────────────────────┐
│                     FREYJA MEMORY LAYER                      │
│                                                              │
│  ┌────────────┐  ┌────────────┐  ┌────────────────────────┐ │
│  │   Skills   │  │  Episodes  │  │   Session Transcript   │ │
│  │ (know-how) │  │ (results)  │  │   (conversation)       │ │
│  └─────┬──────┘  └─────┬──────┘  └──────────┬─────────────┘ │
│        │               │                    │                │
│        ▼               ▼                    ▼                │
│  ┌───────────────────────────────────────────────────────┐   │
│  │              Context Manager                          │   │
│  │  Decides what occupies the LLM window on each turn    │   │
│  └───────────────────────┬───────────────────────────────┘   │
│                          │                                   │
│                          ▼                                   │
│  ┌───────────────────────────────────────────────────────┐   │
│  │              Model Router                             │   │
│  │  Selects the appropriate model for each task          │   │
│  └───────────────────────────────────────────────────────┘   │
└──────────────────────────────────────────────────────────────┘
```

Following [Google ADK](https://adk.dev/context/)'s three-layer context model, we distinguish between the *session* (the durable, append-only event log that serves as ground truth), the *working context* (the computed projection of the session into a single model invocation's prompt), and *artifacts* (large payloads that live in external stores with lightweight handles in context). This separation enables multiple optimization strategies — compaction, caching, filtering, artifact externalization — without losing data. The memory layer contains seven distinct data types, each with a different lifetime, size profile, and loading strategy. Skills and MCP tools use *progressive disclosure* — cheap metadata at boot, full content only on activation — which keeps startup context lean while preserving access to the full capability set. Sub-agents are free in the parent context because their work executes in a separate window; only their compressed episode summary re-enters the parent.

The engine itself is decoupled via a submission/event queue abstraction, following [Codex CLI](https://openai.com/index/unrolling-the-codex-agent-loop/)'s architecture: operations flow through a sequential processing loop with bounded channels, enabling multiple simultaneous UIs (desktop app, CLI, IDE extension), session resumability, and testable cores without coupling the agent logic to any particular frontend.

| Data type | Lifetime | Typical size | When loaded |
|-----------|----------|-------------|-------------|
| Skills (metadata) | Persistent, filesystem | ~100 tokens each | At boot |
| Skills (full body) | Session, on activation | Up to 5K tokens each | On `load_skill` call |
| Episodes | Per-session + optionally persisted | 200-500 tokens each | When parent needs them |
| FREYJA.md | Persistent, project file | Variable | Always in context |
| Session transcript | Per-session, grows | Variable | Always (managed) |
| Tool results | Per-turn | Variable, can be large | Inline, truncated under pressure |
| MCP tool schemas | Per-session | 300-800 tokens each | Deferred; on first use |

The context manager monitors fill level using provider token counts from API responses (not heuristics) and triggers four eviction layers in sequence:

| Layer | Trigger | Action | Information loss |
|-------|---------|--------|-----------------|
| 0 — Skill unload | 60% fill | Deactivate skills unreferenced in 5+ turns; keep descriptions, drop bodies | Near-zero; re-activatable |
| 1 — Episode compression | 70% fill | Replace verbose sub-agent transcripts with episode summaries | Minimal; intentionally summarized |
| 2 — Tool result truncation | 80% fill | Truncate old tool results to head (70%) + tail (25%); keep 3 most recent | Targeted |
| 3 — Full compaction | 90% fill | Middle-out LLM summarization; re-attach recent skills (5K tokens each, 25K total budget) | Lossy but structured |

These thresholds are drawn from our engine constants (`CONTEXT_PRESSURE_THRESHOLD = 0.80`, `CONTEXT_COMPACTION_THRESHOLD = 0.90`) and from Claude Code's default of approximately 83.5%. Most sessions never reach Layer 3.

After compaction, the engine rebuilds the working context from surviving components:

| Component | Survival rule | Source |
|-----------|--------------|--------|
| FREYJA.md | Always survives | Re-read from disk |
| Auto-memory (MEMORY.md) | First 200 lines / 25KB | Re-read from disk |
| System prompt + environment | Always survives | Regenerated |
| User's most recent message | Always survives | Kept in session |
| Recently-invoked skills | 5K tokens each, 25K total budget, most-recent-first | Re-read from disk |
| Episode summaries | Always survive (already compressed) | Kept in session |
| Old tool results | Dropped | — |
| Conversation history | Replaced by LLM summary | — |
| Thinking blocks | Dropped | — |
| Nested FREYJA.md (subdirectories) | Dropped; reload on next file access | — |

An anti-thrashing mechanism halts auto-compaction after three failed attempts in a single turn, surfacing an error rather than looping.

---

## 4. Skills

The [Externalization survey](https://arxiv.org/abs/2604.08224) (Zhou et al., April 2026, 54 pages) defines a skill as the externalization of *procedural expertise* — not a tool (which exposes an operation) and not a protocol (which governs how operations are described), but a higher-level artifact encoding how a class of tasks should be executed. This expertise has three coupled components: the *operational procedure* (step decomposition, ordering, stopping conditions), *decision heuristics* (what to try first at branch points, when to back off, what evidence is sufficient), and *normative constraints* (testing requirements, scope limits, domain rules). Once externalized, skills become carriers of governance as much as carriers of capability.

We instantiate this framework as `SKILL.md` files with YAML frontmatter and Markdown bodies, adopting the [agentskills.io](https://agentskills.io/specification) open standard (supported by 30+ agent tools) and extending it through the `metadata.freyja` namespace with fields that other agents ignore. Skills are not executable code; they are *context injections* — when activated, their instructions enter the agent's context and guide its behavior using existing tools.

> **Specimen: SKILL.md with Freyja extensions**
> ```yaml
> ---
> name: api-conventions
> description: >
>   REST API design conventions for this project. Activate when
>   creating or modifying API endpoints.
> allowed-tools: Bash(npm run test:api *) Read Grep
> metadata:
>   freyja:
>     type: build
>     triggers: [api endpoint, REST, route handler]
>     confidence: verified
>     retrieval_count: 23
>     success_signals: 19
>     failure_signals: 2
>     cold_tools: [custom_api_validator]
> ---
>
> # API Conventions
>
> [Step-by-step instructions follow...]
> ```

**Discovery hierarchy.** Skills are discovered in the following order, with earlier entries taking precedence when names collide:

| Priority | Location | Scope | Notes |
|----------|----------|-------|-------|
| 1 | `.freyja/skills/<name>/SKILL.md` | Project | Team-shared, version-controlled |
| 2 | `~/.freyja/skills/<name>/SKILL.md` | Global (user) | Personal across all projects |
| 3 | `.claude/skills/<name>/SKILL.md` | Claude Code compat | Read-only fallback |
| 4 | Paths in `settings.json` | Configurable | For shared skill libraries |
| 5 | MCP plugin skills | Per-plugin namespace | Server-provided |

Nested `.freyja/skills/` directories in monorepo subdirectories are discovered when working with files in those directories.

**Skill types and activation.** We classify skills into three types, each with a distinct activation trigger:

| Type | Purpose | Activation trigger | Example |
|------|---------|-------------------|---------|
| *build* | Teaches the agent how to accomplish a task | Keyword match against `triggers` field, or user invocation (`/skill-name`) | `api-conventions` |
| *guard* | Teaches recovery from errors | Regex match against `error_patterns` when a tool call fails | `fix-econnrefused` |
| *reference* | Domain knowledge the agent should know | Auto-activated when agent reads files matching `paths` globs | `auth-architecture` |

The agent can also activate skills via auto-discovery: the system prompt contains all skill descriptions (~100 tokens each), and the agent calls `load_skill(name)` when it determines relevance. Deactivation follows the eviction layers described in §3 — skills unload under context pressure and survive compaction within a token budget.

**Confidence lifecycle.** We track retrieval outcomes to provide passive evolutionary selection pressure without requiring an explicit optimizer:

```
UNVALIDATED ──(3+ retrievals)──▶ EXPERIMENTAL ──(10+ uses, ≥80% success)──▶ VERIFIED
                                       │
                                  (10+ uses, <50% success)
                                       │
                                       ▼
                                  DEPRECATED
```

Activation occurs through four mechanisms: user invocation (`/skill-name`), agent auto-discovery (the system prompt contains all skill descriptions; the agent calls `load_skill` when it determines relevance), guard activation (the engine matches error text against `error_patterns` regexes), and path-based activation (skills with `paths` patterns load when the agent works with matching files). Deactivation follows the eviction layers described in §3 — skills unload under context pressure and survive compaction within a token budget.

**Skill retrieval at scale.** Our current retrieval (keyword triggers + LLM auto-discovery from descriptions) is adequate for small catalogs but will not scale. [SkillFlow](https://arxiv.org/abs/2504.06188) demonstrates that over a 36K-skill corpus, keyword-based retrieval (BM25) achieves only MRR 0.266 while a four-stage progressive pipeline (dense retrieval → shallow reranker → deep reranker → LLM selector) achieves MRR 0.634. More critically, [AgentSkillOS](https://arxiv.org/abs/2603.02176) shows that flat, unstructured skill invocation scores 17.2 on a Bradley-Terry quality metric at 200K skills while DAG-based orchestration — where skills declare dependencies and execute in dependency-ordered layers with parallelism within layers — scores 100.0. The quality gap is structural, not retrieval: even with oracle-perfect skill selection, flat invocation loses to orchestrated execution. We plan to address this in two phases: first, multi-stage retrieval (replacing our current `difflib.get_close_matches` with a dense-then-rerank pipeline), and second, dependency-aware composition (skills declaring `depends_on` and `composes_with` relationships in frontmatter, enabling DAG execution for multi-skill tasks).

**Skill composition.** The Externalization survey identifies five composition patterns — serial execution, parallel division of labor, conditional routing, recursive sub-skill invocation, and pipeline chaining — and notes that repeatedly validated compositions can themselves be packaged as new higher-level skills. [CUA-Skill](https://arxiv.org/abs/2601.21123) formalizes this as *composition graphs* `G_c = (V_c, E_c)` where each node is a skill and each directed edge represents a valid chaining from one skill to another. This is directly analogous to our episode composition model (§5), but at the skill level rather than the episode level — and the two should compose: a skill's execution graph determines what sub-agents to spawn, and those sub-agents produce episodes that feed downstream skills.

**Meta-cognitive skill activation.** [Act Wisely](https://arxiv.org/abs/2604.08545) (Yan et al., April 2026) demonstrates that standard tool-augmented agents invoke tools on 80-98% of queries, but a meta-cognitive approach — where the agent first assesses whether it genuinely needs external computation — reduces invocation to approximately 2% while simultaneously *improving* accuracy. Their HDPO (Hierarchical Decoupled Policy Optimization) trains the agent to only invoke tools when internal knowledge is genuinely insufficient, achieving +26.4% improvement on WeMath versus the tool-heavy baseline. The implication for skill activation is that the agent should assess whether it can handle the task from its parametric knowledge and current context *before* loading a skill, rather than reflexively loading any skill whose triggers match. This converts the skill selection problem from "which skill matches?" to "do I need a skill at all?" — and only entering the selection pipeline when the answer is yes dramatically reduces both context cost and selection errors.

**Skill security.** The Externalization survey warns that skill files themselves can become prompt-injection surfaces, and large-scale studies of public skill ecosystems report substantial rates of vulnerabilities including data exfiltration, privilege escalation, and supply-chain risk. This motivates our allow/deny permission model (§10) and argues for sandboxing skill-provided scripts, treating third-party skills with the same caution as third-party npm packages.

**Cross-user skill evolution.** [SkillClaw](https://arxiv.org/abs/2604.08377) (Ma et al., April 2026) demonstrates that a dedicated *evolver agent* — separate from the main session agent — can continuously aggregate execution trajectories from multiple users, process them through a dual-layer summarization pipeline (programmatic trajectory + LLM causal chain analysis), and generate targeted skill mutations that achieve +42.1% average improvement across validation queries. Their key architectural choices are directly adoptable: a client proxy that transparently intercepts API calls to collect session artifacts, evidence-backed versioning where every skill revision cites specific session IDs and scores, and a nighttime validation gate that runs head-to-head comparisons before promoting evolved skills. We plan to adopt the dedicated evolver pattern in Phase 2 of our self-evolution pipeline (§12), separating the skill improvement process from the main session agent to avoid the "multitasking" quality degradation that occurs when the same agent both executes tasks and reflects on its own skill library.

---

## 5. Episodes and Episodic Memory

When a sub-agent completes its work, it produces an *episode* — a structured summary of what was accomplished, what was learned, and what files were touched — rather than passing the full transcript back to the parent. This design, inspired by Slate's "thread weaving" architecture, allows the parent session to benefit from sub-agent work without inheriting its context cost.

We adopt a hybrid format: a JSON metadata envelope (machine-parseable, queryable, schema-validatable) wrapping a text body suitable for direct injection into LLM prompts. The envelope carries fields informed by MemRL's IEU triplet structure:

> **Specimen: Episode record**
> ```json
> {
>   "id": "ep_19d812345",
>   "goal": "Research the caching architecture and propose improvements",
>   "agent_id": "explore",
>   "model": "claude-haiku-4-5",
>   "status": "success",
>   "duration_ms": 12000,
>   "tokens_in": 5200,
>   "tokens_out": 1800,
>   "tool_calls": 12,
>   "summary": "Investigated the caching layer. Redis for sessions, LRU for API responses. Hit rate ~60%. No event-based invalidation.",
>   "key_findings": [
>     "Redis connection pool at src/cache/redis.ts:45 — max 10 connections",
>     "No cache warming on deploy — cold start takes ~30s"
>   ],
>   "files_examined": ["src/cache/redis.ts", "src/cache/lru.ts"],
>   "files_modified": [],
>   "utility_q_value": 0.5,
>   "importance_score": 0.85,
>   "tags": ["caching", "redis", "performance"]
> }
> ```

The `utility_q_value` field enables MemRL-style reinforcement: after an episode is reused in a subsequent task, its Q-value is updated via exponential moving average (`Q_new = Q_old + α(r - Q_old)`, where `r ∈ {0, 1}` indicates task success and `α = 0.1`). Retrieval uses two phases — similarity-based recall followed by value-aware ranking with score `(1-λ) · similarity + λ · Q_value` — which downranks episodes that are semantically similar to the current query but historically unsuccessful. MemRL reports that this two-phase mechanism reduces forgetting rate from 0.051 to 0.041 and produces a 56% gain on multi-step tasks.

We fix episode granularity at the *per-subtask* level, which the literature consistently identifies as the right atomic unit for learning: each record captures an Observation-Action-Outcome narrative that is self-contained enough for few-shot injection (~200-500 tokens) while preserving the causal reasoning arc that per-tool-call records fragment. A multi-resolution architecture supports coarser views — per-tool-call telemetry for debugging, per-session summaries for cold archive, and cross-session consolidation into semantic knowledge (skills) when a pattern recurs 3+ times.

Episodes are generated via self-summary: when a sub-agent completes, the engine appends a prompt requesting a one-paragraph summary, a list of key findings with line numbers, and a list of files examined or modified. The sub-agent's response is parsed into the episode structure and replaces the full transcript in the parent session's tool result. Failed sub-agents produce episodes with `status: "failed"` and a `failure_reason` field so the parent can decide whether to retry with a different approach or model.

---

## 6. Agent Orchestration

The *main agent* is what the user talks to: it maintains the conversation transcript, owns the session context, decides when to delegate to sub-agents, synthesizes episode results, manages tool approval, and persists the session. *Sub-agents* are spawned for bounded tasks, receiving a fresh context (no parent conversation history), a goal, relevant episodes, and relevant skills. They run on a potentially different model, produce an episode on completion, and cannot spawn further sub-agents (single-level nesting). They can operate in foreground (parent waits) or background (parallel execution).

Four built-in sub-agent types cover the common cases:

| Type | Default model | Tools | Purpose |
|------|-------------|-------|---------|
| Explore | Haiku | Read-only | File discovery, code search |
| Plan | Inherits | Read-only | Research for planning mode |
| General | Inherits | All | Complex multi-step tasks |
| Computer | Sonnet (vision) | Computer tools only | Screen control, app automation |

Users can define additional sub-agent types as Markdown files in `.freyja/agents/` (project-scoped) or `~/.freyja/agents/` (global), following a format that matches Claude Code's `.claude/agents/` system:

> **Specimen: Custom sub-agent definition**
> ```yaml
> ---
> name: code-reviewer
> description: Reviews code for quality and best practices. Use after changes.
> tools: Read, Glob, Grep, Bash
> model: haiku
> permissionMode: acceptEdits
> skills: [api-conventions, error-handling-patterns]
> memory: project
> maxTurns: 30
> ---
>
> You are a code reviewer. Analyze the code and provide specific,
> actionable feedback on quality, security, and best practices.
> ```

**Frontmatter fields.** The full set of configurable fields for custom sub-agent definitions:

| Field | Required | Type | Description |
|-------|----------|------|-------------|
| `name` | Yes | string | Unique identifier (lowercase + hyphens) |
| `description` | Yes | string | When the main agent should delegate to this sub-agent |
| `tools` | No | string[] | Allowlist of tools; inherits all if omitted |
| `disallowedTools` | No | string[] | Denylist; removed from inherited set |
| `model` | No | string | `sonnet`, `opus`, `haiku`, full model ID, or `inherit` |
| `permissionMode` | No | enum | `plan`, `default`, `acceptEdits`, `auto`, `bypassPermissions` |
| `maxTurns` | No | int | Hard cap on agentic turns |
| `skills` | No | string[] | Full skill bodies preloaded at startup |
| `mcpServers` | No | object[] | MCP servers scoped to this sub-agent |
| `memory` | No | enum | Persistent memory scope: `user`, `project`, or `local` |
| `background` | No | bool | Always run as background task |
| `effort` | No | enum | Thinking effort: `low`, `medium`, `high`, `max` |

**Sub-agent persistent memory.** When `memory` is set, the sub-agent maintains its own directory that accumulates learnings across sessions:

| `memory` value | Location | Shared via |
|---------------|----------|-----------|
| `user` | `~/.freyja/agent-memory/<name>/` | Across all projects for this user |
| `project` | `.freyja/agent-memory/<name>/` | Version control (team-shared) |
| `local` | `.freyja/agent-memory-local/<name>/` | Not committed (gitignored) |

When `skills` is set, the full skill body — not just the description — is injected at startup, because sub-agents lack conversation history to help them discover relevance. When `mcpServers` is set, the sub-agent receives its own MCP connections, isolating schema overhead from the parent context.

**Delegation heuristics.** The main agent should delegate under the following conditions:

| Condition | Delegate? | Rationale |
|-----------|----------|-----------|
| Task is bounded with clear success criteria | Yes | Sub-agent can run to completion independently |
| Task would bloat main context with tactical details | Yes | Execution details stay in sub-agent's window |
| Multiple independent sub-tasks exist | Yes | Parallel sub-agents, episodes compose |
| A different or cheaper model would suffice | Yes | Sub-agent uses its slot's model |
| Task requires back-and-forth with user | No | Sub-agents cannot prompt the user |
| Task depends on recent conversation context | No | Expensive to transfer; main agent has it |
| Task is a single tool call | No | Sub-agent overhead not justified |

Several systems in our survey enforce stronger separation between orchestration and execution than we currently do. [Slate](https://randomlabs.ai/blog/slate)'s orchestrator never writes code or executes commands — it only reasons and dispatches via a TypeScript DSL, with all execution happening in worker threads that produce episodes. [ForgeCode](https://forgecode.dev)'s MUSE agent writes only to `plans/`, never to source code, preventing analytical reasoning from triggering premature edits. [Roo Code](https://docs.roocode.com/features/custom-modes)'s custom modes scope tool permissions per persona via regex patterns (e.g., a security-reviewer mode restricted to editing only Markdown files). Whether Freyja's main agent should adopt the orchestrator-never-executes constraint — delegating all file mutations and shell commands to sub-agents — is an open design question that trades simplicity (single agent does everything) against safety and context hygiene (execution never pollutes the strategic context).

---

## 7. Model Router

We define five named model slots — `main`, `subagent`, `search`, `reasoning`, and `computer` — each configurable by the user. This is not an arbitrary decomposition: the same four-slot structure (minus `computer`) emerged independently in [Slate](https://docs.randomlabs.ai/en/using-slate/configuration) (`models.main`, `models.subagent`, `models.search`, `models.reasoning`), in [Claude Code](https://code.claude.com/docs/en/sub-agents) (main session, Explore/Haiku, Plan/inherit, General/inherit), and in [Aider](https://aider.chat/docs/usage/modes.html)'s three-tier system (main, weak, editor). We add a fifth slot for computer control, which requires vision capabilities that the other slots do not.

> **Specimen: Model slot configuration (recommended defaults)**
> ```json
> {
>   "models": {
>     "main":      "claude-sonnet-4-6",
>     "subagent":  "claude-sonnet-4-6",
>     "search":    "claude-haiku-4-5",
>     "reasoning": "claude-opus-4-6",
>     "computer":  "claude-sonnet-4-6"
>   }
> }
> ```

**Cross-platform validation.** The following table shows how the major multi-agent platforms carve up their agent types and assign models, which informed our slot design:

| Platform | Orchestrator / Main | Execute / Subagent | Search / Explore | Reasoning / Planning | Computer / Vision |
|----------|--------------------|--------------------|-----------------|---------------------|-------------------|
| **Slate** | Opus 4.6 | Sonnet 4.6 | Haiku 4.5 | GPT-5.3 Codex | — |
| **Claude Code** | Sonnet 4.6 (switchable) | Inherits main | Haiku 4.5 (hardcoded) | Opus via `opusplan` alias | — |
| **Codex CLI** | GPT-5.4 | Inherits (GPT-5.4-mini rec.) | Inherits | — | — |
| **ForgeCode** | Session model | FORGE: session model | SAGE: session (read-only) | MUSE: session model | — |
| **Roo Code** | Sticky per-mode | Code: Sonnet (typical) | Ask: Haiku/Flash (typical) | Architect: Opus/o3 (typical) | — |
| **Aider** | Main model | Editor: Sonnet | Weak: Haiku / GPT-4o-mini | Architect: Opus / o1 | — |
| **Devin** | Proprietary Planner | Proprietary Coder | Browser agent | Proprietary Critic | — |
| **Freyja** | `main` slot | `subagent` slot | `search` slot | `reasoning` slot | `computer` slot |

The pattern is consistent: orchestration and planning use the most capable (and expensive) model, tactical execution uses a mid-tier model, and search/exploration uses the cheapest viable model. Nobody assigns the same model to all roles — the cost savings from differentiation are too significant.

**Current model landscape (April 2026).** The following models are the primary candidates for each slot, with exact pricing and benchmark data:

| Model | ID | Context | $/M in | $/M out | SWE-bench | Best for |
|-------|----|---------|--------|---------|-----------|---------|
| Claude Opus 4.6 | `claude-opus-4-6` | 1M | $5.00 | $25.00 | 80.8% | Reasoning, orchestration |
| Claude Sonnet 4.6 | `claude-sonnet-4-6` | 1M | $3.00 | $15.00 | 79.6% | Execution, computer-use (OSWorld 72.5%) |
| Claude Haiku 4.5 | `claude-haiku-4-5` | 200K | $1.00 | $5.00 | — | Search, triage, commit messages |
| GPT-5.4 | `gpt-5.4` | 1.05M | $2.50 | $15.00 | 75.1% TB | Terminal execution, tool use |
| GPT-5.4 Mini | `gpt-5.4-mini` | 400K | $0.75 | $4.50 | — | Cheap subagent, lightweight tasks |
| Gemini 3.1 Pro | `gemini-3.1-pro-preview` | 1M | ~$2.00 | ~$12.00 | 80.6% | Price-performance, high volume |
| Gemini 3 Flash | `gemini-3-flash-preview` | — | $0.50 | $3.00 | — | Fast, cheap exploration |
| Grok 4.20 | `grok-4.20-beta` | 2M | $2.00 | $6.00 | — | Largest context (2M) |

**Routing priority.** The model router resolves the model for each request in the following order:

| Priority | Source | Example |
|----------|--------|---------|
| 1 | User override | User selects Opus for this session |
| 2 | Skill frontmatter `model` field | A complex-reasoning skill forces `reasoning` slot |
| 3 | Sub-agent type → slot assignment | Explore sub-agent → `search` slot |
| 4 | Cascade (sub-agents only) | Start Haiku, escalate to Sonnet on quality signal |
| 5 | Provider fallback | Configured model unavailable → next in chain |

**Model cascading.** For sub-agents, we adopt cascading: try the cheapest model first and escalate on quality signals. [CascadeFlow](https://github.com/lemony-ai/cascadeflow) demonstrates that this approach handles 60-70% of queries with small models at 40-85% cost reduction and zero quality loss. Escalation signals include self-consistency disagreement, schema validation failure, user rejection, and model-reported low confidence. We do not cascade on the main conversation (users expect consistent personality) or on computer control (latency is too critical for a retry loop).

**Latency envelopes.** Selecting an inappropriate model for the latency envelope of a task type degrades user experience more than selecting a slightly less capable model:

| Task type | Max acceptable latency | Recommended tier |
|-----------|----------------------|-----------------|
| Chat TTFT | < 1s | Sonnet / GPT-5.4 |
| Computer control action | 1-3s per step | Sonnet 4.6 (vision-trained, OSWorld 72.5%) |
| Code generation | 3-10s | Sonnet 4.6 / Gemini 3.1 Pro |
| Complex planning | 10-30s | Opus 4.6 / GPT-5.4 |
| Sub-agent exploration | 10-60s | Haiku 4.5 / Gemini 3 Flash (parallel, cheap) |
| Research synthesis | 30-120s | Sonnet + web tools, background |

We track *cost-per-successful-outcome* rather than cost-per-token, because a model that requires three retries costs more than one that succeeds once. The target is to route 70% of routine tasks to cheap models and reserve frontier models for 30% of complex work — an approach that the emerging consensus across platforms in 2026 validates as achieving comparable quality at 40-80% cost reduction.

---

## 8. Tool Management and MCP

Freyja ships with approximately 30 built-in tools spanning file operations, shell execution, web search, agent orchestration, skill management, and computer control. Users extend this set by connecting [MCP](https://modelcontextprotocol.io) servers, which expose tools for external platforms (Google Drive, Slack, GitHub, databases, custom APIs). The challenge is that tool schemas are expensive — 300-800 tokens each after provider serialization overhead — and the RAG-MCP benchmark demonstrates a sharp accuracy cliff: selection accuracy drops from 84-95% at 49 tools to 0-20% at 741 tools, with the phase transition occurring between 50 and 200 tools.

Before entering the tool selection pipeline at all, the agent should assess whether it genuinely needs a tool — the meta-cognitive gate described in §4. [Act Wisely](https://arxiv.org/abs/2604.08545) shows that this pre-filter alone eliminates 96-98% of queries from the selection problem, converting the scaling challenge from "select among 200+ tools for every query" to "select among 200+ tools for the small fraction of queries that genuinely need tools." We address the remaining queries with a three-tier progressive disclosure system:

| Tier | In API tools list | In system prompt | Discovery mechanism |
|------|-------------------|-----------------|---------------------|
| HOT | Full schema | Summary line | Agent calls directly |
| WARM | No | Name + summary | Agent calls `tool_search("<name>")`, schema loads |
| COLD | No | Not mentioned | Skill activation reveals tool name, then `tool_search` |

Built-in tools are HOT, MCP tools are WARM (deferred), and skill-specific tools are COLD. Claude Code's adoption of `defer_loading: true` on the Anthropic API reduced system tool context from approximately 14,000 tokens to 968 tokens — a 94% reduction — while maintaining full functionality. Our measurements show comparable savings: progressive disclosure reduces per-session token cost from $3.00 to $0.32 for a system with 100 tools across a 20-turn session at $3/M input pricing.

Cold-to-hot promotion follows a three-hop discovery path: a user query triggers skill retrieval, the skill's `cold_tools` frontmatter field reveals tool names with summaries, and the agent calls `tool_search` to load the full schema. Promotion is sticky within a session — once loaded, a tool remains visible for all subsequent turns — and resets on session end.

MCP tool schemas are deferred by default. Connecting GitHub + Slack + Sentry consumes approximately 94,000 tokens if always loaded (47% of a 200K window); with deferral, the overhead drops to approximately 1,600 tokens (tool names only). MCP tools inherit the session's permission tier and default to requiring approval, since they access external systems. Skills can pre-approve specific MCP tools via the `allowed-tools` frontmatter field.

---

## 9. Context Management

The context refill problem — where compaction triggers repeatedly because the summary silently lost exact details that the model then re-fetches — is the primary production pathology in long-running agent sessions. Factory.ai reports that sessions with frequent compaction re-read the same files after every cycle, multiplying actual token consumption 10-20x.

We combine four techniques from the literature, applied in priority order so that cheaper interventions fire first and LLM-based summarization is a last resort:

| Priority | Technique | Source | Mechanism | Measured impact |
|----------|-----------|--------|-----------|----------------|
| 1 | Prevention | [Morph](https://www.morphllm.com/flashcompact) | Return snippets not whole files; send compact diffs not full rewrites | Extends sessions from 15-30 min to 1-3 hours before compaction |
| 2 | Budget-awareness signal | [BATS](https://arxiv.org/abs/2511.17006) | Append `[context: 47K/131K — MEDIUM]` to each turn | −40.4% speculative searches, −31.3% cost |
| 3 | Observation masking | [JetBrains](https://github.com/JetBrains-Research/the-complexity-trap) | Replace old tool outputs with structured stubs: `[read_file foo.py — 2847 lines. Call read_file to reload.]` | +2.6% solve rate, −52% cost vs. LLM summarization |
| 4 | Anchored summarization | [Factory.ai](https://factory.ai/news/evaluating-compression) | LLM summary with mandatory sections (intent, changes, decisions, next steps) | 3.70/5 quality vs. Anthropic 3.44, OpenAI 3.35 |

The key insight from JetBrains is that observation masking — which makes no attempt to preserve information — outperforms LLM summarization, which actively *introduces* errors by hallucinating details the model is uncertain about. Stubs are honest about what they don't contain; summaries pretend.

Two additional approaches from our second research round deserve evaluation. [Google ADK](https://adk.dev/context/)'s *artifact externalization* uses a handle pattern where large payloads (file contents, command output, document data) live in external stores with lightweight references in context; a `LoadArtifactsTool` enables on-demand expansion into working context and offloading after task completion. This is a cleaner solution to the tool-result-bloating problem than head/tail truncation, and we plan to adopt it as a complement to Layer 2. [Codex CLI](https://developers.openai.com/codex/config-reference)'s encrypted compaction returns an opaque `encrypted_content` blob that preserves the model's latent understanding of the conversation rather than reducing it to a plaintext summary; this likely retains more nuance than our anchored summarization, but is provider-specific (OpenAI only) and non-inspectable.

Token counting uses the provider's actual `usage.input_tokens` from API responses as the source of truth, with tiktoken-based estimates for pre-flight checks and a conservative character heuristic (`len(text) // 3`) as a fallback. Tool definition overhead is accounted for with a 2.5x empirical multiplier to reflect provider serialization, and the dynamic tool-result budget is computed as `min(available * 0.5, 60K)` with a floor of 4K tokens. We follow [Aider](https://aider.chat/docs/usage/caching.html)'s strategy of ordering prompt components to maximize prefix cache hits — system prompt, then read-only files, then repo map, then editable files — since Anthropic cached tokens are 90% cheaper than uncached input tokens.

Loop detection operates independently of context pressure. The engine injects a steering message after two consecutive identical tool calls (same name and arguments) and triggers an end-turn verification prompt after six consecutive calls to the same tool name. Computer control tools are exempt from loop detection because repetition is the normal pattern for screen control — the screen state changes between calls even when arguments are identical.

---

## 10. Permission and Safety Model

We adopt Claude Code's six-tier permission system, which provides a clear spectrum from fully read-only to fully autonomous:

| Tier | Behavior |
|------|----------|
| `plan` | Read-only; proposes steps but executes nothing |
| `default` | Prompts for each mutating tool call; reads auto-approved |
| `acceptEdits` | Auto-approves file edits and safe bash (mkdir, cp, mv, sed) within workspace |
| `auto` | A classifier model (Sonnet 4.6) independently reviews each tool call for destructive actions, scope escalation, and hostile injection; reverts to prompting after 3 consecutive blocks |
| `dontAsk` | Non-interactive; auto-denies everything not in the explicit allow list (designed for CI) |
| `bypassPermissions` | Everything auto-approved; only protected paths (`.git/`, `.freyja/`) still prompt |

**Per-tool rules.** Granular allow/deny rules use glob patterns, evaluated in deny-first → ask → allow order (first match wins):

> **Specimen: Permission rules in settings.json**
> ```json
> {
>   "permissions": {
>     "deny":  ["Bash(curl *)", "Bash(rm -rf *)", "Read(./.env)", "Read(./secrets/**)"],
>     "allow": ["Bash(npm run test *)", "Bash(git commit *)", "mcp__github__*"]
>   }
> }
> ```

| Pattern | Matches |
|---------|---------|
| `Bash(git commit *)` | Any bash command starting with `git commit` |
| `Read(./.env.*)` | `.env.local`, `.env.production`, etc. |
| `Read(./secrets/**)` | Any file under `secrets/`, recursively |
| `mcp__github__*` | All tools from the `github` MCP server |
| `mcp__slack__send_message` | Exactly one tool from the `slack` server |

**Configuration hierarchy** (highest precedence first):

| Level | Location | Who controls | Overridable? |
|-------|----------|-------------|-------------|
| 1 | Managed settings | Admin-deployed | No |
| 2 | CLI arguments | Invocation | — |
| 3 | `.freyja/settings.local.json` | Developer (gitignored) | Yes |
| 4 | `.freyja/settings.json` | Team (committed) | Yes |
| 5 | `~/.freyja/settings.json` | User (global) | Yes |

If a tool is denied at any level, no lower level can allow it.

**Hooks.** PreToolUse and PostToolUse hooks enable programmatic permission gating. A hook script receives the tool name and arguments as JSON on stdin and returns a JSON decision:

| Return value | Effect |
|-------------|--------|
| `{"decision": "allow"}` | Proceed without prompting |
| `{"decision": "deny", "reason": "..."}` | Block; reason sent to LLM |
| `{"decision": "ask"}` | Fall through to normal approval flow |
| `{"decision": "allow", "updatedInput": {...}}` | Proceed with modified arguments (sandboxing, redaction) |

We note that [Codex CLI](https://developers.openai.com/codex/concepts/sandboxing) implements kernel-level sandboxing via macOS Seatbelt and Linux Landlock, which provides strictly stronger guarantees than application-level enforcement. Codex also introduces a *guardian subagent* (experimental) that routes eligible approval requests through a reviewer model for automated risk assessment, reducing human interruptions while maintaining safety. Both patterns are candidates for future adoption; the guardian subagent is particularly appealing for the `auto` permission tier, where it could replace the current Sonnet-based classifier with a more nuanced risk evaluator that has access to the full session context.

The emergency stop mechanism operates outside the agent process via an Electron `globalShortcut` on Cmd+Shift+Esc, which fires even when the app is not focused. On trigger, it immediately stops all running sub-agents, releases mouse and keyboard control, kills all running bash processes, and pauses (but does not terminate) the session. During computer control, user mouse or keyboard input cancels the current agent action, and the Escape key cancels the computer-use sub-agent entirely.

---

## 11. Computer Control

Computer control operates through a dedicated sub-agent with its own context window, restricted tool set, and episode production. The architecture follows the standard observe-plan-act-verify cycle:

| Step | Operation | Latency | Implementation |
|------|-----------|---------|----------------|
| Observe | Screenshot (1280×800) + optional AX tree read | ~200ms | Capture proxy (Electron) + `freyja_native` (Rust/pyo3) |
| Plan | Vision model analyzes screenshot + AX tree | 1-5s | LLM API call with image |
| Act | Execute mouse/keyboard/scroll action | <50ms | `freyja_native`: CoreGraphics CGEvent |
| Wait | Let UI settle before next screenshot | 2s | Hardcoded delay (matches Anthropic's `_screenshot_delay`) |
| Verify | Capture new screenshot; model evaluates on next cycle | ~200ms | Same as Observe |
| **Total per action** | | **3-7s** | |

A 10-step workflow therefore takes 30-70 seconds. This latency envelope is why vision-capable fast models (Haiku 4.5 at 135 tok/s, Gemini Flash at 250 tok/s) are essential — Opus-class latency of 3-5 seconds per step makes screen control feel unusable.

**Action primitives** exposed by `freyja_native` (matching Anthropic's `computer_20250124` spec):

| Category | Actions |
|----------|---------|
| Mouse | `mouse_move(x, y)`, `left_click`, `right_click`, `double_click`, `triple_click`, `left_click_drag`, `left_mouse_down`, `left_mouse_up` |
| Keyboard | `key(combo)`, `type(text)` (12ms per keystroke), `hold_key(key, duration)` |
| Scroll | `scroll(direction, amount)` |
| Screen | `screenshot`, `cursor_position`, `wait(duration)` |
| Accessibility | `read_ax_tree`, `find_element(role, title)`, `list_windows`, `list_displays`, `focus_window` |

The accessibility tree provides exact element identification (by role, title, position, and enabled/focused state) and millisecond-latency action execution via direct API calls, which is substantially more reliable than pixel-coordinate guessing for small UI elements. However, many macOS applications — particularly Electron-based ones — expose incomplete accessibility metadata, so we maintain a hybrid approach: AX tree for identification and targeting, screenshots for visual verification and layout understanding.

Screen capture requires macOS Screen Recording TCC permission, which the Electron main process holds but the Python subprocess does not inherit. We solve this with a *capture proxy* — a localhost HTTP server in the Electron main process — through which the Python bridge requests screenshots using the parent's TCC entitlement. The Rust native extension handles mouse, keyboard, and AX tree operations directly, since those require only the Accessibility permission.

**Safety mechanisms.** Computer control introduces risks that other tool categories do not, and we layer multiple defenses:

| Mechanism | Trigger | Behavior |
|-----------|---------|----------|
| Emergency stop | Cmd+Shift+Esc (global shortcut) | Kill all computer actions, release input control, pause session |
| User input cancellation | Any mouse movement or keypress during agent action | Cancel current action immediately |
| Escape key | Escape pressed during computer-use session | Terminate the computer-use sub-agent |
| Destructive action gate | Agent attempts send/delete/close/purchase | Prompt for approval even in `auto` tier |
| Password redaction | AX tree element has `AXSecureTextField` role | Element text replaced with `***` in episode |
| Password manager exclusion | Window belongs to 1Password, Bitwarden, etc. | Window excluded from screenshots |
| Max turns | Sub-agent exceeds `maxTurns` | Forceful termination, episode with `status: "timeout"` |

**Tiered action hierarchy.** Computer-use sub-agents prefer structured API connectors (MCP servers) over GUI automation, falling back to screen control only when no connector is available. This matches Anthropic's recommended pattern and avoids the fragility of screen-coordinate-based interaction when a more reliable path exists.

---

## 12. Self-Evolution

The confidence lifecycle described in §4 provides passive evolutionary selection pressure — skills that consistently help reach VERIFIED status, while those that consistently fail reach DEPRECATED — but it cannot fix broken skills. We adopt a phased approach to active evolution, starting with lightweight reflection and building toward full GEPA integration:

| Phase | Mechanism | Input | Output | Cost | When |
|-------|-----------|-------|--------|------|------|
| 0 — Passive selection | Confidence lifecycle (§4) | Retrieval/success/failure counters | Skills promoted or deprecated | 0 | Always on |
| 1 — Reflection | `/optimize-skill <name>` CLI command | Last 20 traces where skill was injected | Diff for user review | ~$0.50/skill | On demand |
| 2 — Automated GEPA | Scheduled pipeline targeting skills with <80% success | Execution traces + current SKILL.md | Candidate on Pareto frontier | ~$2-10/skill | Nightly or weekly |
| 3 — Online A/B | Dual-version routing with success-rate comparison | Live traffic | Promoted winner | 0 (piggybacks on normal usage) | After Phase 2 stabilizes |

**Phase 1 pipeline** (the first thing we implement):

```
1. Pull traces  →  2. Separate success/fail  →  3. Feed failures to reflection LLM
                                                         ↓
6. Present diff  ←  5. Evaluate on held-out  ←  4. Generate 2-3 candidate mutations
   for review         trace set
```

**Mutation operators** available to the reflection LLM:

| Operator | Risk | Description |
|----------|------|-------------|
| PATCH | Low | Fix a specific identified issue (missing constraint, wrong API call) |
| REWRITE_SECTION | Medium | Rewrite one section incorporating new information from traces |
| ENRICH | Low | Inject concrete examples extracted from successful execution traces |
| TRIGGER_EXPAND | Medium | Add trigger phrases from queries that should have matched but didn't |

**Phase 2 constraint gates** (adopted from [Hermes Agent](https://github.com/NousResearch/hermes-agent-self-evolution)):

| Gate | Criterion | Rationale |
|------|-----------|-----------|
| Test suite | 100% pass | No regressions |
| Size limit | SKILL.md ≤ 15KB, description ≤ 500 chars | Stays within context budget |
| Cache compatibility | No mid-conversation mutations | Prompt caching requires stable prefixes |
| Semantic preservation | Cosine similarity to original > threshold | Prevent drift from intended purpose |
| Human review | PR with diff | All changes auditable |

We start with guard skills because they have the clearest success signal — either the error was avoided or it was not — whereas build skills have fuzzier success criteria that require more sophisticated evaluation. Hermes Agent's experience suggests the full pipeline costs approximately $2-10 per skill, which means an entire directory of 40+ skills could be optimized for under $400.

---

## 13. Implementation Roadmap

**Phase 1 — Skills + Model Router** (Week 1)

| Deliverable | Details |
|-------------|---------|
| Skill discovery | Scan `.freyja/skills/`, `~/.freyja/skills/`, `.claude/skills/` at boot |
| Skill index in system prompt | ~100 tokens per skill (name + description) |
| `load_skill` tool | Injects full SKILL.md body into context on demand |
| `/skill-name` invocation | Slash-command shorthand for user-triggered activation |
| FREYJA.md loading | Project root, user global, local override; concatenated |
| Model slot configuration | 5 named slots in `settings.json`, UI picker |
| Rule-based routing | Route by sub-agent type → slot; no classifier yet |
| Provider fallback | If configured model unavailable, try next in chain |

**Phase 2 — Episodes + Context Manager** (Week 2)

| Deliverable | Details |
|-------------|---------|
| Episode generation | Self-summary prompt appended on sub-agent completion |
| Episode-as-tool-result | Parent sees episode JSON, not full transcript |
| Episode persistence | `~/.freyja/episodes/<session-id>/<episode-id>.json` |
| Episode composition | One episode's output feeds the next sub-agent's input |
| Layered eviction | 4 layers at 60/70/80/90% thresholds |
| Observation masking | Structured stubs for tool results older than 3 turns |
| Budget-awareness signal | `[context: X/Y — LEVEL]` appended per turn |
| Prompt cache ordering | System prompt → read-only → repo map → editable → conversation |

**Phase 3 — Tool Tiers + MCP** (Week 3)

| Deliverable | Details |
|-------------|---------|
| Three-tier system | HOT (full schema), WARM (name only), COLD (skill-revealed) |
| `tool_search` tool | Promotes WARM/COLD tools to HOT on demand |
| Skill-driven cold promotion | `cold_tools` frontmatter surfaces tools with instructions |
| MCP server configuration | `~/.freyja/settings.json` and `.freyja/mcp.json` |
| Deferred MCP schemas | `defer_loading: true`; names at boot, schemas on first use |
| MCP permission integration | Inherit session tier; skills pre-approve via `allowed-tools` |

**Phase 4 — Cascading + Custom Sub-Agents** (Week 4)

| Deliverable | Details |
|-------------|---------|
| Model cascading | Haiku → Sonnet → Opus chain for sub-agents |
| Escalation signals | Self-consistency, schema validation, user rejection |
| Custom sub-agent definitions | `.freyja/agents/*.md` with YAML frontmatter |
| Sub-agent persistent memory | Per-agent directories at user/project/local scope |
| Skill preloading | Full body injected at sub-agent startup |
| MCP scoping | Per-sub-agent MCP server connections |

**Phase 5 — Learning + Evolution** (Weeks 5-6)

| Deliverable | Details |
|-------------|---------|
| Confidence tracking | retrieval_count, success_signals, failure_signals in YAML frontmatter |
| Confidence lifecycle | UNVALIDATED → EXPERIMENTAL → VERIFIED / DEPRECATED |
| Auto-creation | Suggest skill creation after 3+ similar patterns, each 5+ tool calls |
| Guard skills | Error-pattern regex matching, auto-activation on tool failure |
| `/optimize-skill` | Reflection-based Phase 1 evolution pipeline |
| OpenTelemetry tracing | Agent-specific span types, cost attribution per trace |

---

## 14. Future Directions

Several aspects of the architecture remain underspecified or unexplored, which we outline here both as a record of known limitations and as an invitation for future work.

**Episode retrieval at scale.** Our current episode storage uses the filesystem with JSON files, which is adequate for the expected volume (3-10 episodes per session, 30-day retention). Whether a JSONL index or embedding-based vector store becomes necessary depends on usage patterns that we cannot predict before deployment. The MemRL two-phase retrieval mechanism is designed to work with keyword matching in Phase A (using the `tags` field) and does not require embeddings to start producing value; we defer the embedding index to a later phase.

**Delegation logic.** The heuristics for when the main agent should delegate to a sub-agent are currently described in prose (§6) but not formalized. Claude Code uses a `context: fork` frontmatter field on skills to explicitly mark which skills run as sub-agents, which is an attractively explicit model. Whether the delegation decision should be hardcoded, learned from execution traces, or controlled by skill authors remains an open question.

**FREYJA.md and CLAUDE.md coexistence.** We read CLAUDE.md as a fallback when FREYJA.md does not exist, but the behavior when both files are present in the same project is not specified. Options include reading both (concatenated, with FREYJA.md taking precedence on conflicts), reading only FREYJA.md, or presenting the user with a migration prompt.

**Cascade confidence thresholds.** The self-consistency and schema-validation signals that trigger model escalation in the cascade (§7) need empirical tuning. We plan to instrument the cascade with success-rate tracking per model tier and per task type, then set thresholds that minimize cost-per-successful-outcome rather than targeting a fixed accuracy level.

**Cross-platform support.** The current implementation is macOS-only due to the Rust native extension's reliance on CoreGraphics and AXUIElement APIs. Extending to Linux (X11/Wayland, AT-SPI) and Windows (UI Automation) requires platform-specific backends behind a shared trait interface. The bridge protocol and tool definitions are platform-agnostic; only the native extension needs porting.

**Skill shell injection.** Claude Code supports a backtick-based preprocessing syntax that runs shell commands before the skill body is sent to the agent, enabling dynamic context (e.g., injecting the output of `git log --oneline -5`). This is useful but raises security concerns (arbitrary command execution at skill load time). We defer adoption until we can evaluate the threat model for user-authored versus third-party skills.

**Online A/B testing for skill evolution.** Phase 4 of the self-evolution pipeline (§12) envisions deploying old and new skill versions simultaneously, routing queries to each, and promoting the winner after sufficient retrievals. The mechanics of this — versioned skill files, routing logic, statistical significance thresholds — are not yet designed.

**Privacy in computer control.** Screenshot data sent to vision models may contain sensitive information. We redact password fields (detected via `AXSecureTextField` role in the accessibility tree) and exclude known password manager windows from screenshots, but comprehensive privacy protection likely requires a local vision model for element identification and sending only element metadata — not raw pixels — to the cloud API.

**Codebase understanding and repo mapping.** Our current architecture lacks a dedicated mechanism for understanding codebase structure. [Aider](https://aider.chat/2023/10/22/repomap.html)'s repo-map system — a tree-sitter-parsed directed graph ranked via personalized PageRank — provides substantially better context for multi-file tasks than our current approach of relying on the agent's ad-hoc file exploration. [Augment Code](https://www.augmentcode.com/context-engine)'s semantic graph, which maps cross-cutting relationships between functions, classes, modules, and data flows across hundreds of thousands of files with purpose-trained embedding models, represents the production-grade version of this capability and is available as an MCP server. We should evaluate whether to build a lightweight repo-map (Aider's approach, achievable in a few days) or integrate Augment's context engine via MCP (zero implementation, but external dependency).

**Edit format selection.** Aider's [edit format research](https://aider.chat/docs/unified-diffs.html) demonstrates that the choice of how the LLM communicates code changes has a measurable impact on accuracy and laziness, that function-call-based formats consistently underperform plain-text formats, and that the optimal format varies by model. Our current `edit_file` tool uses a fixed search/replace format; we should evaluate whether to support model-specific format selection, as Aider does, and whether the architect-mode pattern (separate planning from editing) should be a first-class capability.

**OS-level sandboxing.** [Codex CLI](https://developers.openai.com/codex/concepts/sandboxing) implements kernel-level sandboxing via macOS Seatbelt and Linux Landlock, which provides stronger guarantees than our application-level approach. Whether the additional complexity of kernel-enforced sandboxing is justified for a desktop agent — where the user explicitly trusts the application — is an open question, but it would substantially improve the security story for the `auto` permission tier.

**Artifact externalization.** [Google ADK](https://adk.dev/context/) uses a "handle pattern" where large payloads live in artifact stores with lightweight references in context, and a `LoadArtifactsTool` enables on-demand expansion. This is a cleaner solution to the tool-result-bloating-context problem than our current head/tail truncation, and we should evaluate it as an alternative to or complement of Layer 2 eviction.

**Agent observability.** We have no tracing infrastructure. [AgentOps](https://agentops.ai)'s nine agent-specific span types and [Braintrust](https://braintrust.dev)'s production-trace-to-test-case pipeline are patterns we should adopt from Phase 1, not defer. The industry is converging on OpenTelemetry as the wire format, which would future-proof integration with any observability platform.

**Streaming action execution.** [Bolt.new](https://bolt.new)'s streaming parser, which extracts file writes and shell commands from structured tags as LLM tokens arrive and executes them progressively, could substantially reduce perceived latency for multi-file generation tasks. Whether this is compatible with our permission model (where each tool call may require approval) requires investigation.

**Orchestrator-never-executes principle.** [Slate](https://randomlabs.ai/blog/slate)'s architecture enforces that the orchestrator only reasons and dispatches — it never writes code or executes commands directly. [ForgeCode](https://forgecode.dev)'s MUSE agent similarly writes only to `plans/`, never to source code. This separation prevents analytical reasoning from triggering premature edits, and we should evaluate whether our main agent should adopt this constraint, delegating all execution to sub-agents.

---

*This document is the source of truth for Freyja's architecture. Update it as decisions are made and research completes.*
