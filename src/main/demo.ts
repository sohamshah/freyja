import type { BridgeCommand, BridgeEvent, Skill, SubagentRecord } from '../shared/events.js'

interface DemoOptions {
  onEvent: (event: BridgeEvent) => void
}

/**
 * DemoBridge streams a realistic, scripted session that shows off every
 * surface of the UI: text streaming, tool calls, subagent spawns, skill
 * retrievals, compaction notices, and usage updates. Runs when the real
 * Python bridge can't be started.
 *
 * Turns are serialized through a promise chain so streamed text never
 * interleaves.
 */
export class DemoBridge {
  private readonly onEvent: (event: BridgeEvent) => void
  private running = true
  private turnCounter = 0
  private sessionId = 'demo-' + Date.now().toString(36)
  private currentChain: Promise<void> = Promise.resolve()
  private timers = new Set<NodeJS.Timeout>()

  constructor(opts: DemoOptions) {
    this.onEvent = opts.onEvent
  }

  start() {
    this.emit({
      type: 'ready',
      sessionId: this.sessionId,
      mode: 'demo',
      capabilities: {
        model: 'claude-sonnet-4-6',
        subagents: true,
        skills: true,
        tools: 42,
        thinking: 'adaptive',
      },
    })
    for (const skill of DEMO_SKILLS) this.emit({ type: 'skill_updated', skill })
    this.enqueue(() => this.playIntro())
  }

  stop() {
    this.running = false
    for (const t of this.timers) clearTimeout(t)
    this.timers.clear()
  }

  handleCommand(cmd: BridgeCommand) {
    if (!this.running) return
    if (cmd.type === 'send_message') {
      this.enqueue(() => this.playUserTurn(cmd.content))
    } else if (cmd.type === 'cancel') {
      this.emit({
        type: 'system_event',
        subtype: 'turn_cancelled',
        message: 'Turn cancelled by user',
      })
      this.emit({ type: 'turn_complete', turnId: `turn-${this.turnCounter}`, success: false })
    } else if (cmd.type === 'set_model') {
      this.emit({
        type: 'system_event',
        subtype: 'model_changed',
        message: `Model switched to ${cmd.model}`,
        details: { model: cmd.model },
      })
    }
  }

  /** Trigger a burst of activity for screenshots */
  burst() {
    this.enqueue(() => this.playUserTurn('Map the architecture and show me how subagents interact with the skills system'))
  }

  // --- Serialization helper ---

  private enqueue(fn: () => Promise<void>) {
    const prev = this.currentChain
    this.currentChain = prev
      .then(() => (this.running ? fn() : Promise.resolve()))
      .catch((err) => {
        this.emit({
          type: 'log',
          level: 'error',
          message: `demo turn failed: ${String(err)}`,
        })
      })
  }

  private sleep(ms: number): Promise<void> {
    return new Promise<void>((resolve, reject) => {
      const t = setTimeout(() => {
        this.timers.delete(t)
        if (!this.running) reject(new Error('stopped'))
        else resolve()
      }, ms)
      this.timers.add(t)
    })
  }

  private async streamText(text: string, perCharMs: number): Promise<void> {
    const words = text.split(/(\s+)/)
    for (const w of words) {
      if (!this.running) return
      this.emit({ type: 'text_delta', text: w })
      await this.sleep(perCharMs * Math.max(1, w.length))
    }
  }

  private async streamThinking(text: string, perCharMs: number): Promise<void> {
    const words = text.split(/(\s+)/)
    for (const w of words) {
      if (!this.running) return
      this.emit({ type: 'thinking_delta', thinking: w })
      await this.sleep(perCharMs * Math.max(1, w.length))
    }
  }

  // --- Scripted content ---

  private async playIntro(): Promise<void> {
    const turnId = `turn-${++this.turnCounter}`
    this.emit({ type: 'turn_start', turnId })
    await this.streamText(
      `Welcome to Freyja. I'm running in **demo mode** because the Python bridge could not be reached, but the full UI is live.

Try: **"Map the architecture and show me how subagents interact with the skills system"** -- or press \`Cmd+B\` to trigger a scripted burst.`,
      10,
    )
    this.emit({ type: 'message_stop', stopReason: 'end_turn' })
    this.emit({ type: 'turn_complete', turnId, success: true })
    this.emit({
      type: 'usage',
      inputTokens: 820,
      outputTokens: 72,
      cacheReadTokens: 2048,
      cacheWriteTokens: 0,
      cost: 0.0031,
    })
  }

  private async playUserTurn(_content: string): Promise<void> {
    const turnId = `turn-${++this.turnCounter}`
    this.emit({ type: 'turn_start', turnId })

    void this.streamThinking(
      "The user is asking about the architecture. I should begin by mapping the runner, then look at the subagent registry, and finally the knowledge layer.\nLet me start with a broad read.",
      2,
    ).catch(() => {})

    await this.sleep(900)
    const skill = DEMO_SKILLS[0]
    this.emit({
      type: 'skill_retrieved',
      skill,
      reason: 'keyword match: "architecture", "subagents"',
    })
    this.emit({
      type: 'system_event',
      subtype: 'knowledge_retrieval_complete',
      message: `Loaded skill: ${skill.name} (${skill.confidence})`,
    })

    await this.sleep(300)
    this.emit({ type: 'tool_use_start', id: 'call_001', name: 'read_file' })
    this.emit({
      type: 'tool_input_delta',
      id: 'call_001',
      partialJson: '{"path": "engine/runner.py"',
    })
    await this.sleep(300)
    this.emit({
      type: 'tool_input_end',
      id: 'call_001',
      arguments: { path: 'engine/runner.py' },
    })
    await this.sleep(500)
    this.emit({
      type: 'tool_result',
      id: 'call_001',
      preview:
        'class AsyncAgentRunner:\n  def __init__(self, provider, config=None, *, on_stream=None, on_tool_call=None, ...)\n  # ... 2300 lines',
      isError: false,
      durationMs: 420,
    })

    await this.sleep(400)
    await this.streamText(
      `I've mapped the entry points. The core runner is at \`engine/runner.py:907\` (\`AsyncAgentRunner\`) -- it accepts \`on_stream\`, \`on_tool_call\`, \`on_system_event\` callbacks that the CLI wires into Rich, and that the HTTP server wires into SSE via \`EventBridge\`.

Let me spawn a background subagent to look at the subagent registry in parallel.`,
      14,
    )

    const sub: SubagentRecord = {
      id: 'sub_01',
      label: 'research subagent-registry',
      mode: 'background',
      state: 'running',
      task: 'Read cli/tools/sub_agent_registry.py and summarize the SubAgentRegistry API.',
      startedAt: Date.now(),
      elapsedMs: 0,
      tokensIn: 0,
      tokensOut: 0,
      toolsCalled: 0,
    }
    this.emit({ type: 'subagent_spawn', record: sub })

    // Subagent progress in background
    void (async () => {
      for (let i = 1; i <= 8; i++) {
        try {
          await this.sleep(180)
        } catch {
          return
        }
        this.emit({
          type: 'subagent_update',
          id: 'sub_01',
          patch: {
            tokensIn: 120 * i + Math.floor(Math.random() * 80),
            tokensOut: 30 * i + Math.floor(Math.random() * 20),
            toolsCalled: Math.min(3, Math.floor(i / 2)),
            elapsedMs: i * 180,
          },
        })
      }
    })()

    await this.sleep(400)
    this.emit({ type: 'tool_use_start', id: 'call_002', name: 'grep' })
    await this.sleep(250)
    this.emit({
      type: 'tool_input_end',
      id: 'call_002',
      arguments: {
        pattern: 'class SubAgentRegistry|def record|def execute',
        glob: 'engine/cli/tools/*.py',
      },
    })
    await this.sleep(350)
    this.emit({
      type: 'tool_result',
      id: 'call_002',
      preview:
        'sub_agent_registry.py:12: class SubAgentRegistry:\nsub_agent_registry.py:45:   def record(self, record) -> None:\nsub_agent_tool.py:88:   async def execute(self, call_id, args): ...',
      isError: false,
      durationMs: 38,
    })

    await this.sleep(500)
    await this.streamText(
      `

Here's what the topology looks like:

\`\`\`
AsyncAgentRunner (engine/runner.py:907)
  |-- on_stream --> TextDeltaEvent / ThinkingDeltaEvent / ToolUse*
  |-- on_tool_call --> ToolRegistry.execute() --> ToolResult
  |-- on_system_event --> compaction / context_pruning / skill_maintenance
  \`-- knowledge_integration --> SubAgentRegistry (engine/cli/tools/sub_agent_registry.py)
                                |-- foreground: blocks turn, inline result
                                \`-- background: ThreadPoolExecutor, auto-inject
\`\`\`

The skills subsystem hangs off \`KnowledgeIntegration\` -- keyword > semantic > subagent cascade. Skills carry a \`confidence\` field that graduates on \`retrieval_count\` + success/failure signals.`,
      14,
    )

    this.emit({
      type: 'subagent_update',
      id: 'sub_01',
      patch: { state: 'done', tokensIn: 1840, tokensOut: 410, toolsCalled: 3, elapsedMs: 3200 },
    })
    this.emit({
      type: 'subagent_done',
      id: 'sub_01',
      elapsedMs: 3200,
      result:
        'SubAgentRegistry exposes: record(), get(id), list_active(), wait(id), kill(id), wait_all(). Records track state, tokens, elapsed, tool count. Background mode uses ThreadPoolExecutor.',
    })
    this.emit({
      type: 'system_event',
      subtype: 'tool_truncation',
      message: 'Tool result from read_file truncated to 4096 tokens (original: 18432)',
      details: { tool: 'read_file', budget: 4096 },
    })

    await this.sleep(400)
    await this.streamText(
      `

Bottom line: every surface you'd want in the desktop UI already has a hook. The bridge just needs to tap the runner callbacks and forward JSONL lines -- which is exactly what this demo is simulating.`,
      12,
    )

    this.emit({ type: 'message_stop', stopReason: 'end_turn' })
    this.emit({ type: 'turn_complete', turnId, success: true })
    this.emit({
      type: 'usage',
      inputTokens: 4120,
      outputTokens: 612,
      cacheReadTokens: 6144,
      cacheWriteTokens: 1024,
      cost: 0.0382,
    })
    const bumped = {
      ...DEMO_SKILLS[0],
      retrievalCount: DEMO_SKILLS[0].retrievalCount + 1,
      successSignals: DEMO_SKILLS[0].successSignals + 1,
    }
    this.emit({ type: 'skill_updated', skill: bumped })
  }

  private emit(event: BridgeEvent) {
    if (!this.running && event.type !== 'log') return
    this.onEvent(event)
  }
}

// --- Demo data ---

const DEMO_SKILLS: Skill[] = [
  {
    id: 'skill_search-and-respond',
    name: 'search-and-respond',
    skillType: 'build',
    description: 'RAG pattern: search KB then respond with sources',
    triggers: ['search', 'RAG', 'knowledge base', 'answer with sources'],
    tags: ['chat', 'core-pattern'],
    confidence: 'verified',
    retrievalCount: 42,
    successSignals: 36,
    failureSignals: 2,
    path: 'knowledge/build/search-and-respond/SKILL.md',
  },
  {
    id: 'skill_add-categorizer-to-workflow',
    name: 'add-categorizer-to-workflow',
    skillType: 'build',
    description: 'Wire a classifier node into an existing workflow graph.',
    triggers: ['add categorizer', 'route', 'branch on category'],
    tags: ['workflow', 'routing'],
    confidence: 'experimental',
    retrievalCount: 7,
    successSignals: 5,
    failureSignals: 1,
    path: 'knowledge/build/add-categorizer-to-workflow/SKILL.md',
  },
  {
    id: 'skill_guard_query-type-mismatch',
    name: 'call-llm-v2-query-type-mismatch',
    skillType: 'guard',
    description: 'Recover from query-type mismatches in call_llm_v2 calls.',
    triggers: ['query type mismatch', 'call_llm_v2 error'],
    tags: ['error-recovery', 'llm'],
    confidence: 'verified',
    retrievalCount: 28,
    successSignals: 24,
    failureSignals: 3,
    path: 'knowledge/guard/call-llm-v2-query-type-mismatch/SKILL.md',
  },
  {
    id: 'skill_workflow-search-and-respond',
    name: 'workflow-search-and-respond',
    skillType: 'build',
    description: 'End-to-end retrieve-augment-generate for a workflow node.',
    triggers: ['workflow', 'retrieve', 'augment', 'generate'],
    tags: ['workflow', 'rag'],
    confidence: 'unvalidated',
    retrievalCount: 1,
    successSignals: 0,
    failureSignals: 0,
    path: 'knowledge/build/workflow-search-and-respond/SKILL.md',
  },
  {
    id: 'skill_dashboard-error-triage',
    name: 'dashboard-error-triage',
    skillType: 'reference',
    description: 'Reference for triaging dashboard/alerting errors.',
    triggers: ['dashboard error', 'alert triage', 'SigNoz'],
    tags: ['observability', 'ops'],
    confidence: 'experimental',
    retrievalCount: 4,
    successSignals: 3,
    failureSignals: 0,
    path: 'knowledge/reference/dashboard-error-triage/SKILL.md',
  },
]
