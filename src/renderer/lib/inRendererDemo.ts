// In-renderer demo driver. Only used when window.harness is missing (i.e. the
// renderer is loaded outside Electron for UI review). Produces the same
// sequence of events as the Node-side DemoBridge so the UI fills up.

import type { BridgeEvent, Skill, SubagentRecord } from '@shared/events'

type Emit = (ev: BridgeEvent) => void

const DEMO_SKILLS: Skill[] = [
  {
    id: 'skill_search-and-respond',
    name: 'search-and-respond',
    skillType: 'build',
    description: 'RAG pattern: search KB then respond with sources',
    triggers: ['search', 'RAG', 'knowledge base'],
    tags: ['chat', 'core-pattern'],
    confidence: 'verified',
    retrievalCount: 42,
    successSignals: 36,
    failureSignals: 2,
    path: 'knowledge/build/search-and-respond/SKILL.md',
  },
  {
    id: 'skill_add-categorizer',
    name: 'add-categorizer-to-workflow',
    skillType: 'build',
    description: 'Wire a classifier node into a workflow graph.',
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
    description: 'Recover from query-type mismatches in call_llm_v2.',
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
    description: 'Triage dashboard and alerting errors (SigNoz/Prometheus).',
    triggers: ['dashboard error', 'alert triage', 'SigNoz'],
    tags: ['observability', 'ops'],
    confidence: 'experimental',
    retrievalCount: 4,
    successSignals: 3,
    failureSignals: 0,
    path: 'knowledge/reference/dashboard-error-triage/SKILL.md',
  },
]

export function startInRendererDemo(emit: Emit): { burst(): void; send(content: string): void; stop(): void } {
  const stoppedRef = { v: false }
  // Per-instance sleepers so stopping one driver never affects another.
  const sleepers: Array<() => void> = []
  const sleep = (ms: number): Promise<void> =>
    new Promise<void>((resolve, reject) => {
      const t = window.setTimeout(() => {
        if (stoppedRef.v) reject(new Error('stopped'))
        else resolve()
      }, ms)
      sleepers.push(() => clearTimeout(t))
    })
  let turnCounter = 0
  let currentTurn: Promise<void> | null = null

  emit({
    type: 'ready',
    sessionId: 'renderer-demo',
    mode: 'demo',
    capabilities: { model: 'claude-sonnet-4-6', subagents: true, skills: true, tools: 42 },
  })
  for (const skill of DEMO_SKILLS) emit({ type: 'skill_updated', skill })

  // Intro (not awaited externally — fire-and-forget)
  currentTurn = (async () => {
    await sleep(400)
    const turnId = `turn-${++turnCounter}`
    emit({ type: 'turn_start', turnId })
    await streamText(
      emit,
      stoppedRef,
      `Welcome to Freyja. Running in **demo mode** -- the real Electron bridge isn't available, but every surface is live.

Try pressing **⌘B** for a scripted burst, **⌘K** for the command palette, or pick a prompt from above.`,
      10,
    )
    emit({ type: 'message_stop', stopReason: 'end_turn' })
    emit({ type: 'turn_complete', turnId, success: true })
    emit({
      type: 'usage',
      inputTokens: 820,
      outputTokens: 74,
      cacheReadTokens: 2048,
      cacheWriteTokens: 0,
      cost: 0.0031,
    })
  })().catch(() => {})

  return {
    burst() {
      enqueue(() => playBurst())
    },
    send(content: string) {
      enqueue(() => {
        if (content.trim() === '/burst') return playBurst()
        return playUserTurn(content)
      })
    },
    stop() {
      stoppedRef.v = true
      for (const s of sleepers) s()
    },
  }

  // Serialize turns so we never interleave streams.
  function enqueue(fn: () => Promise<void>) {
    const prev = currentTurn ?? Promise.resolve()
    currentTurn = prev.then(() => (stoppedRef.v ? Promise.resolve() : fn())).catch(() => {})
  }

  function playBurst() {
    return playUserTurn('Map the architecture and show me how subagents interact with the skills system')
  }

  async function playUserTurn(_content: string): Promise<void> {
    const turnId = `turn-${++turnCounter}`
    emit({ type: 'turn_start', turnId })

    // Thinking runs concurrently (it's not text so it doesn't interleave with assistant text)
    void streamThinking(
      emit,
      stoppedRef,
      `The user is asking about the architecture. I'll map the runner, then look at the subagent registry, then the knowledge layer. I'll spawn a background subagent in parallel.`,
      2,
    ).catch(() => {})

    await sleep(900)
    emit({
      type: 'skill_retrieved',
      skill: DEMO_SKILLS[0],
      reason: 'keyword match: architecture, subagents',
    })
    emit({
      type: 'system_event',
      subtype: 'knowledge_retrieval_complete',
      message: `Loaded skill: ${DEMO_SKILLS[0].name} (${DEMO_SKILLS[0].confidence})`,
    })

    await sleep(300)
    emit({ type: 'tool_use_start', id: 'call_001', name: 'read_file' })
    emit({
      type: 'tool_input_delta',
      id: 'call_001',
      partialJson: '{"path": "engine/runner.py"',
    })
    await sleep(300)
    emit({
      type: 'tool_input_end',
      id: 'call_001',
      arguments: { path: 'engine/runner.py' },
    })
    await sleep(500)
    emit({
      type: 'tool_result',
      id: 'call_001',
      preview:
        'class AsyncAgentRunner:\n  def __init__(self, provider, config=None, *, on_stream=None, on_tool_call=None, …)\n  # … 2300 lines',
      isError: false,
      durationMs: 420,
    })

    await sleep(400)
    await streamText(
      emit,
      stoppedRef,
      `I've mapped the entry points. The core runner is at \`engine/runner.py:907\` (\`AsyncAgentRunner\`) — it accepts \`on_stream\`, \`on_tool_call\`, and \`on_system_event\` callbacks that the CLI wires into Rich and that the HTTP server wires into SSE.

Spawning a background subagent to read the registry in parallel…`,
      12,
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
    emit({ type: 'subagent_spawn', record: sub })

    // Fire subagent progress ticks in the background while main turn continues
    void (async () => {
      for (let i = 1; i <= 8; i++) {
        try {
          await sleep(180)
        } catch {
          return
        }
        emit({
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

    await sleep(400)
    emit({ type: 'tool_use_start', id: 'call_002', name: 'grep' })
    await sleep(250)
    emit({
      type: 'tool_input_end',
      id: 'call_002',
      arguments: {
        pattern: 'class SubAgentRegistry|def record|def execute',
        glob: 'engine/cli/tools/*.py',
      },
    })
    await sleep(350)
    emit({
      type: 'tool_result',
      id: 'call_002',
      preview:
        'sub_agent_registry.py:12: class SubAgentRegistry:\nsub_agent_registry.py:45:   def record(self, record) -> None:\nsub_agent_tool.py:88:   async def execute(self, call_id, args): …',
      isError: false,
      durationMs: 38,
    })

    await sleep(500)
    await streamText(
      emit,
      stoppedRef,
      `

Here's the topology I'm working against:

\`\`\`
AsyncAgentRunner (runner.py:907)
  ├─ on_stream ─► TextDeltaEvent / ThinkingDeltaEvent / ToolUse*
  ├─ on_tool_call ─► ToolRegistry.execute() ─► ToolResult
  ├─ on_system_event ─► compaction / context_pruning / skill_maintenance
  └─ knowledge_integration ─► SubAgentRegistry
                                ├─ foreground: blocks, inline result
                                └─ background: ThreadPoolExecutor
\`\`\`

The skills subsystem hangs off \`KnowledgeIntegration\` — a keyword → semantic → subagent cascade. Skills carry a \`confidence\` field that graduates on \`retrieval_count\` plus success/failure signals.`,
      14,
    )

    emit({
      type: 'subagent_update',
      id: 'sub_01',
      patch: {
        state: 'done',
        tokensIn: 1840,
        tokensOut: 410,
        toolsCalled: 3,
        elapsedMs: 3200,
      },
    })
    emit({
      type: 'subagent_done',
      id: 'sub_01',
      elapsedMs: 3200,
      result:
        'SubAgentRegistry exposes: record(), get(id), list_active(), wait(id), kill(id), wait_all(). Records track state, tokens, elapsed, tool count. Background mode uses ThreadPoolExecutor.',
    })
    emit({
      type: 'system_event',
      subtype: 'tool_truncation',
      message: 'read_file result truncated to 4096 tokens (original: 18432)',
      details: { tool: 'read_file', budget: 4096 },
    })

    await sleep(400)
    await streamText(
      emit,
      stoppedRef,
      `

Bottom line: every surface you'd want in the desktop UI has a hook in the runner. The bridge just taps these callbacks and forwards JSONL lines — exactly what this demo is simulating.`,
      12,
    )

    emit({ type: 'message_stop', stopReason: 'end_turn' })
    emit({ type: 'turn_complete', turnId, success: true })
    emit({
      type: 'usage',
      inputTokens: 4120,
      outputTokens: 612,
      cacheReadTokens: 6144,
      cacheWriteTokens: 1024,
      cost: 0.0382,
    })
    emit({
      type: 'skill_updated',
      skill: {
        ...DEMO_SKILLS[0],
        retrievalCount: DEMO_SKILLS[0].retrievalCount + 1,
        successSignals: DEMO_SKILLS[0].successSignals + 1,
      },
    })
  }
}

async function streamText(
  emit: Emit,
  stoppedRef: { v: boolean },
  text: string,
  perCharMs: number,
): Promise<void> {
  const words = text.split(/(\s+)/)
  for (const w of words) {
    if (stoppedRef.v) return
    emit({ type: 'text_delta', text: w })
    await new Promise((r) => window.setTimeout(r, perCharMs * Math.max(1, w.length)))
  }
}

async function streamThinking(
  emit: Emit,
  stoppedRef: { v: boolean },
  text: string,
  perCharMs: number,
): Promise<void> {
  const words = text.split(/(\s+)/)
  for (const w of words) {
    if (stoppedRef.v) return
    emit({ type: 'thinking_delta', thinking: w })
    await new Promise((r) => window.setTimeout(r, perCharMs * Math.max(1, w.length)))
  }
}
