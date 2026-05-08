import { useEffect, useMemo } from 'react'
import { useHarness, type SessionSlice, type SystemEventRecord } from '../state/store'
import type {
  ArtifactRecord,
  BusMessageRecord,
  FileChangeSet,
  SessionSnapshot,
  SubagentRecord,
  ToolCallRecord,
} from '@shared/events'
import { formatCost, formatDuration, formatTokens, relativeTime } from '../lib/format'
import { Spinner } from '../lib/spinner'
import { AgentTypeTag } from './SubagentCard'

type DashboardTab = 'overview' | 'swarm' | 'findings' | 'telemetry' | 'profiles'

interface AgentView {
  session: SessionSnapshot
  sub?: SubagentRecord
  slice?: SessionSlice
  attachable: boolean
  status: 'pending' | 'running' | 'done' | 'failed' | 'cancelled'
  agentType: string
  tools: ToolCallRecord[]
  tokensIn: number
  tokensOut: number
  elapsedMs: number
}

interface BusEventView extends BusMessageRecord {
  sessionId: string
}

interface TelemetryEventView extends SystemEventRecord {
  sessionId: string
  sessionTitle: string
}

interface ProfileDefinition {
  id: string
  bestFor: string
  model: string
  thinking: string
  tools: string
  maxIterations: number | string
  note: string
}

const TABS: Array<{ id: DashboardTab; label: string; hint: string }> = [
  { id: 'overview', label: 'health', hint: 'current run' },
  { id: 'swarm', label: 'agents', hint: 'lanes + bus' },
  { id: 'findings', label: 'evidence', hint: 'findings' },
  { id: 'telemetry', label: 'history', hint: 'context + media' },
  { id: 'profiles', label: 'profiles', hint: 'subagents' },
]

const AGENT_PROFILES: ProfileDefinition[] = [
  {
    id: 'general',
    bestFor: 'Default delegation when no tighter role fits.',
    model: 'Inherits parent model',
    thinking: 'auto',
    tools: 'Safe parent tools',
    maxIterations: 100,
    note: 'Keeps broad tasks ergonomic without forcing a profile choice.',
  },
  {
    id: 'explore',
    bestFor: 'Deep web, docs, file, and codebase reconnaissance.',
    model: 'claude-sonnet-4-6, fallback gpt-5.5 / kimi-k2.6 / deepseek-v4-pro',
    thinking: 'medium',
    tools: 'Web research + read-only file tools',
    maxIterations: 160,
    note: 'Best for open-ended research where coverage matters.',
  },
  {
    id: 'explore-fast',
    bestFor: 'Cheap factual lookups and parallel fanout searches.',
    model: 'Random fast fallback',
    thinking: 'off',
    tools: 'Basic web/file lookup',
    maxIterations: 60,
    note: 'Designed to answer one narrow question quickly.',
  },
  {
    id: 'code',
    bestFor: 'Isolated code changes, migrations, and refactors.',
    model: 'Inherits parent model',
    thinking: 'high',
    tools: 'File/code editing tools',
    maxIterations: 120,
    note: 'Owns bounded write scopes and reports changed paths.',
  },
  {
    id: 'verify',
    bestFor: 'Independent validation after a change.',
    model: 'gpt-5.5 fallback chain; DeepSeek V4 Pro / GLM 5.1 when available',
    thinking: 'high',
    tools: 'Read-only test/file tools',
    maxIterations: 100,
    note: 'Useful when the parent needs a second pass without edits.',
  },
  {
    id: 'plan',
    bestFor: 'Read-only implementation planning.',
    model: 'Inherits parent model',
    thinking: 'medium',
    tools: 'Read-only code/doc exploration',
    maxIterations: 80,
    note: 'Turns ambiguity into an executable plan before writes begin.',
  },
  {
    id: 'review',
    bestFor: 'Code review after implementation.',
    model: 'Independent fallback chain; DeepSeek V4 Pro / GLM 5.1 when available',
    thinking: 'high',
    tools: 'Read-only code/file tools',
    maxIterations: 100,
    note: 'Findings-first review stance for regressions and missing tests.',
  },
  {
    id: 'test',
    bestFor: 'Running builds/tests and diagnosing failures.',
    model: 'Inherits parent model',
    thinking: 'medium',
    tools: 'Test/build/file tools',
    maxIterations: 100,
    note: 'Runs verification loops without mixing in unrelated edits.',
  },
  {
    id: 'browser-qa',
    bestFor: 'Frontend behavior, layout, and browser state checks.',
    model: 'Inherits parent model',
    thinking: 'medium',
    tools: 'Browser CDP + file tools',
    maxIterations: 100,
    note: 'Pairs screenshots with DOM inspection for UI work.',
  },
  {
    id: 'performance',
    bestFor: 'Profiling renderer, bridge, IO, and hot loops.',
    model: 'Inherits parent model',
    thinking: 'high',
    tools: 'Profiling + read-only code/file tools',
    maxIterations: 140,
    note: 'Focused on bottlenecks that improve speed without reducing capability.',
  },
  {
    id: 'docs',
    bestFor: 'API, framework, and product documentation research.',
    model: 'Inherits parent model',
    thinking: 'medium',
    tools: 'Docs/web/file tools',
    maxIterations: 100,
    note: 'Keeps implementation work grounded in current primary sources.',
  },
  {
    id: 'memory-curator',
    bestFor: 'Summarizing reusable facts, skills, and project memory.',
    model: 'Inherits parent model',
    thinking: 'medium',
    tools: 'Memory/skill/file tools',
    maxIterations: 80,
    note: 'Keeps long-running sessions useful without stuffing the prompt.',
  },
  {
    id: 'computer',
    bestFor: 'Mac UI automation with screenshots, clicks, typing, and navigation.',
    model: 'Inherits active session policy',
    thinking: 'task dependent',
    tools: 'Computer control + visual observation',
    maxIterations: 'session policy',
    note: 'Shown here as a first-class actor when live computer sessions exist.',
  },
]

const PROFILE_HEX: Record<string, string> = {
  general: '#a8d4fc',
  explore: '#7ab8a3',
  'explore-fast': '#7bd3ec',
  code: '#ffcc66',
  verify: '#88d67f',
  plan: '#b8a7ff',
  review: '#f0a6ca',
  test: '#f5b45d',
  'browser-qa': '#79b3fa',
  performance: '#f07878',
  docs: '#72d0b2',
  'memory-curator': '#c8d67f',
  computer: '#d99bbe',
}

export function MissionDashboard() {
  const tab = useHarness((s) => s.missionDashboardTab)
  const toggleDashboard = useHarness((s) => s.toggleMissionDashboard)
  const activeSessionId = useHarness((s) => s.activeSessionId)
  const sessions = useHarness((s) => s.sessions)
  const sessionArchive = useHarness((s) => s.sessionArchive)
  const messages = useHarness((s) => s.messages)
  const toolCalls = useHarness((s) => s.toolCalls)
  const toolCallOrder = useHarness((s) => s.toolCallOrder)
  const fileChanges = useHarness((s) => s.fileChanges)
  const subagents = useHarness((s) => s.subagents)
  const subagentOrder = useHarness((s) => s.subagentOrder)
  const usage = useHarness((s) => s.usage)
  const systemEvents = useHarness((s) => s.systemEvents)
  const busMessages = useHarness((s) => s.busMessages)
  const artifacts = useHarness((s) => s.artifacts)
  const model = useHarness((s) => s.model)
  const reasoningLevel = useHarness((s) => s.reasoningLevel)
  const coordinationStrategy = useHarness((s) => s.coordinationStrategy)
  const isStreaming = useHarness((s) => s.isStreaming)
  const computerSessions = useHarness((s) => s.computerSessions)
  const skills = useHarness((s) => s.skills)
  const memories = useHarness((s) => s.memories)
  const switchSession = useHarness((s) => s.switchSession)
  const focusToolCall = useHarness((s) => s.focusToolCall)
  const showToast = useHarness((s) => s.showToast)

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        event.stopPropagation()
        toggleDashboard(false)
      }
    }
    window.addEventListener('keydown', onKey, { capture: true })
    return () => window.removeEventListener('keydown', onKey, { capture: true })
  }, [toggleDashboard])

  const dashboard = useMemo(() => {
    const activeSession =
      sessions.find((session) => session.id === activeSessionId) ??
      sessions[0] ??
      null
    const missionSession =
      activeSession?.parentSessionId
        ? sessions.find((session) => session.id === activeSession.parentSessionId) ?? activeSession
        : activeSession

    const liveSlice: SessionSlice = {
      messages,
      currentStreamingMessageId: null,
      currentTurnId: null,
      thinking: '',
      isStreaming,
      toolCalls,
      toolCallOrder,
      fileChanges,
      subagents,
      subagentOrder,
      usage,
      systemEvents,
      busMessages,
      artifacts,
      model,
      reasoningLevel,
      coordinationStrategy,
    }

    const sliceFor = (sessionId?: string): SessionSlice | undefined => {
      if (!sessionId) return undefined
      if (sessionId === activeSessionId) return liveSlice
      return sessionArchive[sessionId]
    }

    const missionSlice = sliceFor(missionSession?.id)
    const childSessions = missionSession
      ? sessions
          .filter((session) => session.parentSessionId === missionSession.id)
          .sort((a, b) => a.createdAt - b.createdAt)
      : []
    const childSessionIds = new Set(childSessions.map((session) => session.id))

    const childAgents = childSessions.map((session): AgentView => {
      const slice = sliceFor(session.id)
      const parentSub =
        missionSlice?.subagents[session.id] ??
        subagents[session.id] ??
        (subagentOrder.includes(session.id) ? subagents[session.id] : undefined)
      const status = resolveAgentStatus(session, parentSub)
      const calls = slice
        ? slice.toolCallOrder.map((id) => slice.toolCalls[id]).filter(Boolean)
        : []
      return {
        session,
        sub: parentSub,
        slice,
        attachable: true,
        status,
        agentType: session.agentType ?? parentSub?.agentType ?? 'general',
        tools: calls,
        tokensIn: session.totalInputTokens || parentSub?.tokensIn || slice?.usage.totalInputTokens || 0,
        tokensOut: session.totalOutputTokens || parentSub?.tokensOut || slice?.usage.totalOutputTokens || 0,
        elapsedMs:
          parentSub?.elapsedMs ??
          ((session.completedAt ?? Date.now()) - session.createdAt),
      }
    })

    const orphanSubagents = (missionSlice?.subagentOrder ?? subagentOrder)
      .map((id) => missionSlice?.subagents[id] ?? subagents[id])
      .filter((sub): sub is SubagentRecord => Boolean(sub))
      .filter((sub) => !childSessionIds.has(sub.id))
      .map((sub): AgentView => ({
        session: {
          id: sub.id,
          title: sub.label,
          workspace: missionSession?.workspace ?? '',
          model,
          createdAt: sub.startedAt,
          updatedAt: sub.startedAt + sub.elapsedMs,
          messageCount: 0,
          totalInputTokens: sub.tokensIn,
          totalOutputTokens: sub.tokensOut,
          cacheReadTokens: 0,
          parentSessionId: missionSession?.id,
          task: sub.task,
          agentType: sub.agentType,
          completed: sub.state === 'done' || sub.state === 'failed' || sub.state === 'cancelled',
          success: sub.state === 'done',
        },
        sub,
        attachable: false,
        status: sub.state,
        agentType: sub.agentType ?? 'general',
        tools: [],
        tokensIn: sub.tokensIn,
        tokensOut: sub.tokensOut,
        elapsedMs: sub.elapsedMs,
      }))

    const computerAgents: AgentView[] = Object.values(computerSessions)
      .filter((computer) => !missionSession || computer.parentSessionId === missionSession.id)
      .map((computer) => ({
        session: {
          id: computer.sessionId,
          title: computer.goal || 'Computer control',
          workspace: missionSession?.workspace ?? '',
          model,
          createdAt: computer.latestFrame?.takenAt ?? Date.now(),
          updatedAt: computer.latestFrame?.takenAt ?? Date.now(),
          messageCount: computer.history.length,
          totalInputTokens: 0,
          totalOutputTokens: 0,
          cacheReadTokens: 0,
          parentSessionId: computer.parentSessionId,
          task: computer.goal,
          agentType: 'computer',
          completed: computer.status !== 'running' && computer.status !== 'idle',
          success: computer.status === 'done',
        },
        attachable: false,
        status: computer.status === 'idle' ? 'pending' : computer.status,
        agentType: 'computer',
        tools: [],
        tokensIn: 0,
        tokensOut: 0,
        elapsedMs: 0,
      }))

    const agents = [...childAgents, ...orphanSubagents, ...computerAgents]
    const slices = [
      ...(missionSlice ? [{ id: missionSession?.id ?? activeSessionId, slice: missionSlice }] : []),
      ...childSessions
        .map((session) => ({ id: session.id, slice: sliceFor(session.id) }))
        .filter((item): item is { id: string; slice: SessionSlice } => Boolean(item.slice)),
    ]

    const busEvents = dedupeBy(
      slices.flatMap(({ id, slice }) =>
        slice.busMessages.map((message) => ({
          ...message,
          timestamp: normalizeBusTimestamp(message.timestamp),
          sessionId: id,
        })),
      ),
      (message) => `${message.sessionId}:${message.index}:${message.timestamp}:${message.topic}`,
    ).sort((a, b) => b.timestamp - a.timestamp)

    const sessionTitleById = new Map(sessions.map((session) => [session.id, session.title]))
    const telemetryEvents: TelemetryEventView[] = slices
      .flatMap(({ id, slice }) =>
        slice.systemEvents.map((event) => ({
          ...event,
          sessionId: id,
          sessionTitle: sessionTitleById.get(id) ?? id,
        })),
      )
      .filter((event) =>
        [
          'media_pruning',
          'compaction_start',
          'compaction_complete',
          'compaction_skipped',
          'context_pruning',
          'tool_truncation',
          'output_truncation',
        ].includes(event.subtype),
      )
      .sort((a, b) => b.at - a.at)

    const allFileChanges = dedupeBy(
      slices.flatMap(({ slice }) => slice.fileChanges),
      (change) => change.id,
    ).sort((a, b) => b.createdAt - a.createdAt)

    const allArtifacts = dedupeBy(
      slices.flatMap(({ slice }) => slice.artifacts),
      (artifact) => artifact.id,
    ).sort((a, b) => b.createdAt - a.createdAt)

    const allToolCalls = slices.flatMap(({ slice }) =>
      slice.toolCallOrder.map((id) => slice.toolCalls[id]).filter(Boolean),
    )
    const cost = slices.reduce((acc, { slice }) => acc + slice.usage.totalCost, 0)
    const objective = lastUserText(missionSlice?.messages ?? messages) ??
      missionSession?.task ??
      missionSession?.title ??
      'No objective yet'

    return {
      activeSession,
      missionSession,
      missionSlice,
      objective,
      agents,
      busEvents,
      findings: busEvents.filter((event) => event.topic !== 'read'),
      readEvents: busEvents.filter((event) => event.topic === 'read'),
      telemetryEvents,
      fileChanges: allFileChanges,
      artifacts: allArtifacts,
      toolCalls: allToolCalls,
      cost,
      skillsCount: Object.keys(skills).length,
      memoriesCount: Object.keys(memories).length,
      rootUsage: missionSlice?.usage ?? usage,
      screenshotFrames: Object.values(computerSessions)
        .filter((computer) => !missionSession || computer.parentSessionId === missionSession.id || computer.sessionId === missionSession.id)
        .reduce((acc, computer) => acc + computer.frameCount, 0),
    }
  }, [
    activeSessionId,
    artifacts,
    busMessages,
    computerSessions,
    fileChanges,
    isStreaming,
    memories,
    messages,
    model,
    sessions,
    sessionArchive,
    skills,
    subagentOrder,
    subagents,
    systemEvents,
    toolCallOrder,
    toolCalls,
    usage,
  ])

  const setTab = (next: DashboardTab) => toggleDashboard(true, next)
  const closeAndJumpToTool = (id: string) => {
    toggleDashboard(false)
    requestAnimationFrame(() => focusToolCall(id))
  }
  const attach = (id: string) => {
    switchSession(id)
      .then(() => toggleDashboard(false))
      .catch(() => showToast(`Could not attach ${id}`, 'danger'))
  }

  const runningAgents = dashboard.agents.filter((agent) => agent.status === 'running').length
  const rootContextKnown =
    dashboard.rootUsage.currentContextTokens > 0 ||
    dashboard.rootUsage.totalInputTokens <= dashboard.rootUsage.contextWindow
  const rootContextTokens = dashboard.rootUsage.currentContextTokens > 0
    ? dashboard.rootUsage.currentContextTokens
    : rootContextKnown
      ? dashboard.rootUsage.totalInputTokens
      : 0
  const contextPct = Math.min(
    100,
    Math.round((rootContextTokens / dashboard.rootUsage.contextWindow) * 100),
  )
  const changeTotals = dashboard.fileChanges.reduce(
    (acc, change) => ({
      files: acc.files + change.totals.files,
      additions: acc.additions + change.totals.additions,
      deletions: acc.deletions + change.totals.deletions,
    }),
    { files: 0, additions: 0, deletions: 0 },
  )
  const missionStatus =
    runningAgents > 0 || dashboard.activeSession?.id === activeSessionId && isStreaming
      ? 'running'
      : dashboard.findings.some((event) => event.topic === 'errors')
        ? 'attention'
        : 'idle'

  return (
    <div className="fixed inset-0 z-50 flex flex-col bg-[#050807]/90 backdrop-blur-[26px]">
      <div className="pointer-events-none absolute inset-0 opacity-70">
        <div className="absolute left-[7%] top-[10%] h-[360px] w-[360px] rounded-full bg-accent/[0.045] blur-[110px]" />
        <div className="absolute right-[13%] top-[26%] h-[300px] w-[300px] rounded-full bg-ok/[0.035] blur-[90px]" />
        <div className="absolute bottom-[8%] left-[35%] h-[260px] w-[420px] rounded-full bg-warn/[0.025] blur-[120px]" />
      </div>

      <header className="relative flex shrink-0 items-center gap-4 py-4 pl-[88px] pr-6 hairline-b">
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-3">
            <span className="label text-accent">mission dashboard</span>
            <StatusPill status={missionStatus} />
            {dashboard.missionSession && (
              <span className="truncate font-mono text-[10px] text-fg-3">
                {dashboard.missionSession.id}
              </span>
            )}
          </div>
          <div className="mt-2 truncate text-[18px] leading-none text-fg-0">
            {dashboard.objective}
          </div>
        </div>
        <nav className="hidden items-center gap-1 rounded-lg bg-white/[0.03] p-1 ring-hairline lg:flex">
          {TABS.map((item) => (
            <button
              key={item.id}
              onClick={() => setTab(item.id)}
              className={`rounded-md px-3 py-2 text-left transition ${
                tab === item.id
                  ? 'bg-accent/12 text-accent ring-1 ring-accent/25'
                  : 'text-fg-2 hover:bg-white/[0.045] hover:text-fg-0'
              }`}
            >
              <div className="font-mono text-[11px]">{item.label}</div>
              <div className="font-mono text-[8.5px] uppercase tracking-[0.12em] text-fg-3">
                {item.hint}
              </div>
            </button>
          ))}
        </nav>
        <button
          onClick={() => toggleDashboard(false)}
          className="rounded-md bg-white/[0.04] px-3 py-2 font-mono text-[10px] uppercase tracking-[0.12em] text-fg-2 ring-hairline hover:bg-white/[0.08] hover:text-fg-0"
        >
          esc close
        </button>
      </header>

      <div className="relative flex min-h-0 flex-1 flex-col overflow-hidden px-5 pb-5">
        <nav className="flex gap-1 py-3 lg:hidden">
          {TABS.map((item) => (
            <button
              key={item.id}
              onClick={() => setTab(item.id)}
              className={`rounded-md px-3 py-1.5 font-mono text-[10px] uppercase tracking-[0.08em] ${
                tab === item.id
                  ? 'bg-accent/12 text-accent ring-1 ring-accent/25'
                  : 'bg-white/[0.03] text-fg-2 ring-hairline'
              }`}
            >
              {item.label}
            </button>
          ))}
        </nav>

        {tab === 'overview' && (
          <OverviewTab
            objective={dashboard.objective}
            contextPct={contextPct}
            runningAgents={runningAgents}
            agents={dashboard.agents}
            findings={dashboard.findings}
            fileChanges={dashboard.fileChanges}
            artifacts={dashboard.artifacts}
            cost={dashboard.cost}
            changeTotals={changeTotals}
            toolsCount={dashboard.toolCalls.length}
            screenshotFrames={dashboard.screenshotFrames}
            telemetryEvents={dashboard.telemetryEvents}
            skillsCount={dashboard.skillsCount}
            memoriesCount={dashboard.memoriesCount}
            onTab={setTab}
            onAttach={attach}
            onJumpTool={closeAndJumpToTool}
            onCopyFinding={(event) => copyFinding(event, showToast)}
          />
        )}
        {tab === 'swarm' && (
          <SwarmTab
            agents={dashboard.agents}
            busEvents={dashboard.busEvents}
            findings={dashboard.findings}
            onAttach={attach}
          />
        )}
        {tab === 'findings' && (
          <FindingsTab
            findings={dashboard.findings}
            readEvents={dashboard.readEvents}
            agents={dashboard.agents}
            onAttach={attach}
            onCopy={(event) => copyFinding(event, showToast)}
          />
        )}
        {tab === 'telemetry' && (
          <TelemetryTab
            events={dashboard.telemetryEvents}
            screenshotFrames={dashboard.screenshotFrames}
            agents={dashboard.agents}
            rootUsage={dashboard.rootUsage}
          />
        )}
        {tab === 'profiles' && <ProfilesTab />}
      </div>
    </div>
  )
}

function OverviewTab({
  objective,
  contextPct,
  runningAgents,
  agents,
  findings,
  fileChanges,
  artifacts,
  cost,
  changeTotals,
  toolsCount,
  screenshotFrames,
  telemetryEvents,
  skillsCount,
  memoriesCount,
  onTab,
  onAttach,
  onJumpTool,
  onCopyFinding,
}: {
  objective: string
  contextPct: number
  runningAgents: number
  agents: AgentView[]
  findings: BusEventView[]
  fileChanges: FileChangeSet[]
  artifacts: ArtifactRecord[]
  cost: number
  changeTotals: { files: number; additions: number; deletions: number }
  toolsCount: number
  screenshotFrames: number
  telemetryEvents: TelemetryEventView[]
  skillsCount: number
  memoriesCount: number
  onTab: (tab: DashboardTab) => void
  onAttach: (id: string) => void
  onJumpTool: (id: string) => void
  onCopyFinding: (event: BusEventView) => void
}) {
  const compactions = telemetryEvents.filter((event) => event.subtype === 'compaction_complete').length
  const mediaPrunes = telemetryEvents.filter((event) => event.subtype === 'media_pruning').length
  return (
    <div className="grid min-h-0 flex-1 grid-cols-12 grid-rows-[auto_minmax(0,1fr)] gap-3 overflow-hidden">
      <div className="col-span-12 grid grid-cols-2 gap-3 xl:grid-cols-8">
        <MetricCard label="context" value={`${contextPct}%`} sub="current request" meter={contextPct} />
        <MetricCard label="active agents" value={String(runningAgents)} sub={`${agents.length} total`} tone="ok" />
        <MetricCard label="findings" value={String(findings.length)} sub="bus messages" tone="accent" />
        <MetricCard label="changes" value={String(changeTotals.files)} sub={`+${changeTotals.additions} -${changeTotals.deletions}`} tone="warn" />
        <MetricCard label="tool calls" value={String(toolsCount)} sub="mission total" />
        <MetricCard label="screenshots" value={String(screenshotFrames)} sub={`${mediaPrunes} trims`} tone="accent" />
        <MetricCard label="summaries" value={String(compactions)} sub="context history" tone={compactions > 0 ? 'ok' : 'neutral'} />
        <MetricCard label="spend" value={formatCost(cost)} sub={`${skillsCount} skills / ${memoriesCount} memories`} />
      </div>

      <section className="col-span-12 flex min-h-0 flex-col rounded-xl glass-strong p-4 lg:col-span-4">
        <PanelHeader label="current session" action="profiles" onAction={() => onTab('profiles')} />
        <div className="mt-3 max-h-[150px] shrink-0 overflow-auto rounded-lg bg-white/[0.035] p-3 ring-hairline">
          <div className="label mb-2 text-fg-2">current objective</div>
          <p className="selectable text-[13px] leading-[1.55] text-fg-0">{objective}</p>
        </div>
        <div className="mt-3 grid shrink-0 grid-cols-2 gap-2">
          <BriefStat label="agents" value={String(agents.length)} />
          <BriefStat label="evidence" value={String(findings.length)} />
          <BriefStat label="work products" value={String(artifacts.length)} />
          <BriefStat label="change sets" value={String(fileChanges.length)} />
        </div>
        <div className="mt-3 flex min-h-0 flex-1 flex-col overflow-hidden rounded-lg bg-black/20 p-3 ring-hairline">
          <div className="label mb-3 shrink-0">agent roster</div>
          <div className="min-h-0 flex-1 space-y-2 overflow-auto pr-1">
            {agents.slice(0, 7).map((agent) => (
              <CompactAgentRow key={agent.session.id} agent={agent} onAttach={onAttach} />
            ))}
            {agents.length === 0 && (
              <EmptyState title="No sub-agents yet" body="Spawned agents and computer sessions will appear here." />
            )}
          </div>
        </div>
      </section>

      <section className="col-span-12 flex min-h-0 flex-col overflow-hidden rounded-xl glass-strong p-4 lg:col-span-5">
        <PanelHeader label="agent lanes" action="open" onAction={() => onTab('swarm')} />
        <div className="mt-3 min-h-0 flex-1 overflow-auto pr-1">
          <AgentLaneList agents={agents} onAttach={onAttach} />
        </div>
      </section>

      <section className="col-span-12 grid min-h-0 grid-rows-[minmax(0,1fr)_minmax(0,1fr)] gap-3 lg:col-span-3">
        <div className="flex min-h-0 flex-col overflow-hidden rounded-xl glass-strong p-4">
          <PanelHeader label="findings board" action="open" onAction={() => onTab('findings')} />
          <div className="mt-3 min-h-0 flex-1 space-y-2 overflow-auto pr-1">
            {findings.slice(0, 4).map((event) => (
              <FindingCard
                key={`${event.sessionId}-${event.index}-${event.timestamp}`}
                event={event}
                compact
                onCopy={() => onCopyFinding(event)}
              />
            ))}
            {findings.length === 0 && (
              <EmptyState title="No findings yet" body="Progress, findings, and errors posted to the message bus will collect here." />
            )}
          </div>
        </div>
        <div className="flex min-h-0 flex-col overflow-hidden rounded-xl glass-strong p-4">
          <PanelHeader label="work products" action="changes" onAction={() => onTab('findings')} />
          <WorkProducts
            fileChanges={fileChanges}
            artifacts={artifacts}
            onJumpTool={onJumpTool}
          />
        </div>
      </section>
    </div>
  )
}

function SwarmTab({
  agents,
  busEvents,
  findings,
  onAttach,
}: {
  agents: AgentView[]
  busEvents: BusEventView[]
  findings: BusEventView[]
  onAttach: (id: string) => void
}) {
  return (
    <div className="flex min-h-0 flex-1 flex-col gap-3 overflow-hidden">
      <section className="flex shrink-0 flex-col overflow-hidden rounded-xl glass-strong p-4">
        <PanelHeader label="collaboration map" />
        <div className="mt-3 h-[min(520px,52vh)] min-h-[360px] shrink-0 rounded-xl bg-black/20 p-3 ring-hairline">
          <CollaborationGraph agents={agents} events={busEvents} onAttach={onAttach} />
        </div>
      </section>
      <div className="grid min-h-0 flex-1 grid-cols-12 gap-3 overflow-hidden">
        <section className="col-span-12 flex min-h-0 flex-col overflow-hidden rounded-xl glass-strong p-4 xl:col-span-8">
          <PanelHeader label="agent lanes" />
          <div className="mt-3 min-h-0 flex-1 overflow-auto pr-1">
            <AgentLaneList agents={agents} onAttach={onAttach} large />
          </div>
        </section>
        <section className="col-span-12 grid min-h-0 grid-rows-[minmax(0,1fr)_220px] gap-3 xl:col-span-4">
          <div className="flex min-h-0 flex-col overflow-hidden rounded-xl glass-strong p-4">
            <PanelHeader label="message bus" />
            <BusFeed events={busEvents} />
          </div>
          <div className="flex min-h-0 flex-col overflow-hidden rounded-xl glass-strong p-4">
            <PanelHeader label="latest evidence" />
            <div className="mt-3 min-h-0 flex-1 space-y-2 overflow-auto pr-1">
              {findings.slice(0, 3).map((event) => (
                <FindingCard
                  key={`${event.sessionId}-${event.index}-${event.timestamp}`}
                  event={event}
                  compact
                />
              ))}
              {findings.length === 0 && (
                <EmptyState title="No evidence" body="Agents can publish findings, progress, and errors to the bus." />
              )}
            </div>
          </div>
        </section>
      </div>
    </div>
  )
}

function FindingsTab({
  findings,
  readEvents,
  agents,
  onAttach,
  onCopy,
}: {
  findings: BusEventView[]
  readEvents: BusEventView[]
  agents: AgentView[]
  onAttach: (id: string) => void
  onCopy: (event: BusEventView) => void
}) {
  const topicCounts = countBy(findings, (event) => event.topic)
  return (
    <div className="grid min-h-0 flex-1 grid-cols-12 gap-3 overflow-hidden">
      <section className="col-span-12 flex min-h-0 flex-col overflow-hidden rounded-xl glass-strong p-4 xl:col-span-8">
        <PanelHeader label="evidence board" />
        <div className="mt-3 grid shrink-0 grid-cols-3 gap-2">
          <BriefStat label="findings" value={String(topicCounts.findings ?? 0)} />
          <BriefStat label="progress" value={String(topicCounts.progress ?? 0)} />
          <BriefStat label="errors" value={String(topicCounts.errors ?? 0)} tone="danger" />
        </div>
        <div className="mt-3 grid min-h-0 flex-1 grid-cols-1 gap-2 overflow-auto pr-1 2xl:grid-cols-2">
          {findings.map((event) => (
            <FindingCard
              key={`${event.sessionId}-${event.index}-${event.timestamp}`}
              event={event}
              onCopy={() => onCopy(event)}
            />
          ))}
          {findings.length === 0 && (
            <EmptyState title="Nothing on the board yet" body="The board becomes useful as agents publish progress and findings." />
          )}
        </div>
      </section>
      <section className="col-span-12 grid min-h-0 grid-rows-[minmax(0,1fr)_220px] gap-3 xl:col-span-4">
        <div className="flex min-h-0 flex-col overflow-hidden rounded-xl glass-strong p-4">
          <PanelHeader label="sources" />
          <div className="mt-3 min-h-0 flex-1 space-y-2 overflow-auto pr-1">
            {agents.map((agent) => (
              <CompactAgentRow
                key={agent.session.id}
                agent={agent}
                onAttach={onAttach}
              />
            ))}
            {agents.length === 0 && (
              <EmptyState title="No sources" body="Sub-agent sessions will appear as evidence sources." />
            )}
          </div>
        </div>
        <div className="flex min-h-0 flex-col overflow-hidden rounded-xl glass-strong p-4">
          <PanelHeader label="read receipts" />
          <div className="mt-3 min-h-0 flex-1 space-y-1.5 overflow-auto pr-1">
            {readEvents.slice(0, 10).map((event) => (
              <div
                key={`${event.sessionId}-${event.index}-${event.timestamp}`}
                className="rounded-md bg-white/[0.025] px-2 py-1.5 font-mono text-[10px] text-fg-2 ring-hairline"
              >
                <span className="text-fg-1">{event.senderLabel}</span>
                <span className="text-fg-3"> read bus </span>
                <span className="text-fg-3">{relativeTime(event.timestamp)}</span>
              </div>
            ))}
            {readEvents.length === 0 && (
              <EmptyState title="No reads yet" body="Bus read activity will show up here." compact />
            )}
          </div>
        </div>
      </section>
    </div>
  )
}

function TelemetryTab({
  events,
  screenshotFrames,
  agents,
  rootUsage,
}: {
  events: TelemetryEventView[]
  screenshotFrames: number
  agents: AgentView[]
  rootUsage: SessionSlice['usage']
}) {
  const compactions = events
    .filter((event) => event.subtype === 'compaction_complete')
    .sort((a, b) => b.at - a.at)
  const mediaPrunes = events
    .filter((event) => event.subtype === 'media_pruning')
    .sort((a, b) => b.at - a.at)
  const contextPrunes = events.filter((event) => event.subtype === 'context_pruning')
  const truncations = events.filter((event) => event.subtype === 'tool_truncation' || event.subtype === 'output_truncation')
  const omittedImages = mediaPrunes.reduce((acc, event) => acc + detailNumber(event, 'omitted_images'), 0)
  const rootContextKnown =
    rootUsage.currentContextTokens > 0 ||
    rootUsage.totalInputTokens <= rootUsage.contextWindow
  const rootContextTokens = rootUsage.currentContextTokens > 0
    ? rootUsage.currentContextTokens
    : rootContextKnown
      ? rootUsage.totalInputTokens
      : 0
  const tokensSaved = compactions.reduce((acc, event) => {
    const before = detailNumber(event, 'context_tokens_before') || detailNumber(event, 'tokens_before')
    const after = detailNumber(event, 'context_tokens_after') || detailNumber(event, 'tokens_after')
    return acc + Math.max(0, before - after)
  }, 0)
  const latestMedia = mediaPrunes[0]
  return (
    <div className="grid min-h-0 flex-1 grid-cols-12 gap-3 overflow-hidden">
      <section className="col-span-12 grid shrink-0 grid-cols-2 gap-3 xl:col-span-12 xl:grid-cols-6">
        <MetricCard label="screenshots" value={String(screenshotFrames)} sub="captured" tone="accent" />
        <MetricCard label="image trims" value={String(mediaPrunes.length)} sub={`${omittedImages} omitted`} tone={mediaPrunes.length ? 'ok' : 'neutral'} />
        <MetricCard label="summaries" value={String(compactions.length)} sub={`${formatTokens(tokensSaved)} saved`} tone={compactions.length ? 'ok' : 'neutral'} />
        <MetricCard label="tool trims" value={String(contextPrunes.length)} sub="old results" tone="warn" />
        <MetricCard label="output cuts" value={String(truncations.length)} sub="hard limits" tone={truncations.length ? 'warn' : 'neutral'} />
        <MetricCard
          label="live context"
          value={rootContextKnown ? formatTokens(rootContextTokens) : 'n/a'}
          sub={`${formatTokens(rootUsage.totalInputTokens)} billed in`}
        />
      </section>

      <section className="col-span-12 flex min-h-0 flex-col overflow-hidden rounded-xl glass-strong p-4 xl:col-span-7">
        <PanelHeader label="context summaries" />
        <div className="mt-3 min-h-0 flex-1 overflow-auto pr-1">
          {compactions.length === 0 ? (
            <EmptyState
              title="No LLM compactions yet"
              body="When history is summarized, the before/after token, message, and image deltas will appear here."
            />
          ) : (
            <div className="space-y-3">
              {compactions.map((event) => (
                <CompactionCard key={event.id} event={event} />
              ))}
            </div>
          )}
        </div>
      </section>

      <section className="col-span-12 grid min-h-0 grid-rows-[minmax(0,1fr)_220px] gap-3 xl:col-span-5">
        <div className="flex min-h-0 flex-col overflow-hidden rounded-xl glass-strong p-4">
          <PanelHeader label="screenshot history" />
          <div className="mt-3 grid shrink-0 grid-cols-3 gap-2">
            <BriefStat label="kept recent" value={String(detailNumber(latestMedia, 'kept_recent') || 4)} />
            <BriefStat label="safety cap" value={String(detailNumber(latestMedia, 'hard_limit') || 80)} />
            <BriefStat label="omitted" value={String(omittedImages)} />
          </div>
          <div className="mt-3 min-h-0 flex-1 overflow-auto pr-1">
            {mediaPrunes.length === 0 ? (
              <EmptyState
                title="No media pruning needed"
                body="The session has not accumulated enough screenshot media to trim request history."
                compact
              />
            ) : (
              <div className="space-y-2">
                {mediaPrunes.map((event) => (
                  <MediaPruneRow key={event.id} event={event} />
                ))}
              </div>
            )}
          </div>
        </div>

        <div className="flex min-h-0 flex-col overflow-hidden rounded-xl glass-strong p-4">
          <PanelHeader label="affected sessions" />
          <div className="mt-3 min-h-0 flex-1 space-y-2 overflow-auto pr-1">
            {agents.map((agent) => (
              <TelemetryAgentRow key={agent.session.id} agent={agent} events={events} />
            ))}
            {agents.length === 0 && (
              <EmptyState title="No lanes yet" body="Sub-agents and computer sessions will be grouped here as they run." compact />
            )}
          </div>
        </div>
      </section>
    </div>
  )
}

function ProfilesTab() {
  return (
    <div className="min-h-0 flex-1 overflow-auto rounded-xl glass-strong p-4">
      <div className="mb-4 flex items-end justify-between gap-4">
        <div>
          <div className="label text-accent">sub-agent profiles</div>
          <div className="mt-2 max-w-[760px] text-[13px] leading-[1.55] text-fg-1">
            Profiles are working contracts: model policy, tool access, thinking depth, and iteration budget.
            They make delegation explicit without taking capability away from the parent session.
          </div>
        </div>
        <div className="hidden rounded-lg bg-white/[0.035] px-3 py-2 ring-hairline md:block">
          <div className="label">profiles</div>
          <div className="mt-1 font-mono text-[18px] text-fg-0">{AGENT_PROFILES.length}</div>
        </div>
      </div>
      <div className="grid grid-cols-1 gap-3 xl:grid-cols-2 2xl:grid-cols-3">
        {AGENT_PROFILES.map((profile) => (
          <ProfileCard key={profile.id} profile={profile} />
        ))}
      </div>
    </div>
  )
}

function AgentLaneList({
  agents,
  onAttach,
  large = false,
}: {
  agents: AgentView[]
  onAttach: (id: string) => void
  large?: boolean
}) {
  if (agents.length === 0) {
    return (
      <div className="rounded-xl bg-white/[0.025] p-6 ring-hairline">
        <EmptyState title="No active lanes" body="Spawned agents will appear as independent lanes with their tool activity and status." />
      </div>
    )
  }
  return (
    <div className="space-y-2">
      {agents.map((agent) => (
        <AgentLane
          key={agent.session.id}
          agent={agent}
          onAttach={onAttach}
          large={large}
        />
      ))}
    </div>
  )
}

function AgentLane({
  agent,
  onAttach,
  large,
}: {
  agent: AgentView
  onAttach: (id: string) => void
  large?: boolean
}) {
  const latestTool = agent.tools[agent.tools.length - 1]
  const color = PROFILE_HEX[agent.agentType] ?? PROFILE_HEX.general
  const canAttach = agent.attachable
  return (
    <div className="rounded-xl bg-white/[0.028] p-3 ring-hairline transition hover:bg-white/[0.045]">
      <div className="flex items-start gap-3">
        <div className="relative mt-1 h-10 w-10 shrink-0 rounded-lg bg-black/25 ring-hairline">
          <div
            className="absolute inset-2 rounded-md"
            style={{
              border: `1px solid ${color}66`,
              boxShadow: agent.status === 'running' ? `0 0 24px ${color}33` : undefined,
            }}
          />
          <span
            className="absolute left-1/2 top-1/2 h-2 w-2 -translate-x-1/2 -translate-y-1/2 rounded-full"
            style={{ background: color }}
          />
        </div>
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-2">
            <span className="truncate text-[13px] text-fg-0">
              {agent.session.title || agent.sub?.label || agent.session.id}
            </span>
            <AgentTypeTag type={agent.agentType} />
            <span className={`font-mono text-[10px] uppercase ${statusTextClass(agent.status)}`}>
              {agent.status}
            </span>
            {agent.status === 'running' && <Spinner name="scan" className="text-accent" />}
          </div>
          <div className="mt-1 line-clamp-2 text-[11px] leading-[1.5] text-fg-2">
            {agent.session.task ?? agent.sub?.task ?? 'No task description'}
          </div>
          <div className="mt-3 flex flex-wrap items-center gap-3 font-mono text-[10px] text-fg-3">
            <span>model <span className="text-fg-1">{agent.session.model}</span></span>
            <span>in <span className="text-fg-1">{formatTokens(agent.tokensIn)}</span></span>
            <span>out <span className="text-fg-1">{formatTokens(agent.tokensOut)}</span></span>
            <span>tools <span className="text-fg-1">{agent.tools.length || agent.sub?.toolsCalled || 0}</span></span>
            <span>elapsed <span className="text-fg-1">{formatDuration(agent.elapsedMs)}</span></span>
          </div>
          <ToolActivityStrip tools={agent.tools} large={large} />
          {latestTool && (
            <div className="mt-2 truncate font-mono text-[10px] text-fg-2">
              latest: <span className="text-fg-1">{latestTool.name}</span>
              <span className="text-fg-3"> / {latestTool.status}</span>
            </div>
          )}
          {agent.sub?.result && (
            <div className="mt-2 line-clamp-2 rounded-md bg-black/20 px-2 py-1.5 text-[10.5px] leading-[1.45] text-fg-2 ring-hairline">
              {agent.sub.result}
            </div>
          )}
        </div>
        {canAttach && (
          <button
            onClick={() => onAttach(agent.session.id)}
            className="shrink-0 rounded-md bg-accent/10 px-2 py-1.5 font-mono text-[10px] uppercase tracking-[0.08em] text-accent ring-1 ring-accent/20 hover:bg-accent/18"
          >
            attach
          </button>
        )}
      </div>
    </div>
  )
}

function CollaborationGraph({
  agents,
  events,
  onAttach,
}: {
  agents: AgentView[]
  events: BusEventView[]
  onAttach: (id: string) => void
}) {
  const nodes = agents
    .slice()
    .sort((a, b) => agentStartedAt(a) - agentStartedAt(b))
    .slice(0, 18)
  const width = 1280
  const height = 560
  const busX = width / 2
  const busY = 68
  const busW = 286
  const busH = 78
  const rounds = groupAgentRounds(nodes)
  const roundGap = 18
  const leftPad = 42
  const graphTop = 148
  const panelH = height - graphTop - 26
  const roundW = rounds.length
    ? (width - leftPad * 2 - roundGap * (rounds.length - 1)) / rounds.length
    : 0
  const eventsBySender = new Map<string, BusEventView[]>()
  for (const event of events) {
    const list = eventsBySender.get(event.senderId) ?? []
    list.push(event)
    eventsBySender.set(event.senderId, list)
  }
  for (const list of eventsBySender.values()) {
    list.sort((a, b) => b.timestamp - a.timestamp)
  }
  const topicTotals = {
    findings: events.filter((event) => event.topic === 'findings').length,
    progress: events.filter((event) => event.topic === 'progress').length,
    errors: events.filter((event) => event.topic === 'errors').length,
    read: events.filter((event) => event.topic === 'read').length,
  }
  const maxTopicTotal = Math.max(1, ...Object.values(topicTotals))
  const positions = new Map<string, GraphAgentNode>()
  const roundViews = rounds.map((round, roundIndex) => {
    const x = leftPad + roundIndex * (roundW + roundGap)
    const innerCols = round.agents.length > 5 && roundW > 430 ? 2 : 1
    const cardGap = 10
    const cardW = Math.min(292, (roundW - 28 - cardGap * (innerCols - 1)) / innerCols)
    const cardH = 52
    const maxRows = Math.max(1, Math.floor((panelH - 62) / (cardH + 8)))
    const maxAgents = innerCols * maxRows
    const visible = round.agents.slice(0, maxAgents)
    const hidden = round.agents.length - visible.length
    const rows = Math.max(1, Math.ceil(visible.length / innerCols))
    const firstY = graphTop + 52
    const rowGap = rows > 1 ? Math.min(10, Math.max(5, (panelH - 72 - rows * cardH) / (rows - 1))) : 0
    const cards = visible.map((agent, index) => {
      const col = index % innerCols
      const row = Math.floor(index / innerCols)
      const cardX = x + 14 + col * (cardW + cardGap)
      const cardY = firstY + row * (cardH + rowGap)
      const senderEvents = eventsBySender.get(agent.session.id) ?? []
      const published = senderEvents.filter((event) => event.topic !== 'read')
      const reads = senderEvents.filter((event) => event.topic === 'read')
      const latest = senderEvents[0]
      const color = PROFILE_HEX[agent.agentType] ?? PROFILE_HEX.general
      const node: GraphAgentNode = {
        agent,
        x: cardX,
        y: cardY,
        w: cardW,
        h: cardH,
        cx: cardX + cardW / 2,
        topY: cardY,
        bottomY: cardY + cardH,
        color,
        published: published.length,
        reads: reads.length,
        latest,
      }
      positions.set(agent.session.id, node)
      return node
    })
    return { round, x, y: graphTop, w: roundW, h: panelH, cards, hidden }
  })
  const publishEdges = Array.from(positions.values()).filter((node) => node.published > 0)
  const readEdges = Array.from(positions.values()).filter((node) => node.reads > 0)
  const collaborationEdges = inferCollaborationEdges(events, positions)
  const hasEvents = events.length > 0
  return (
    <svg viewBox={`0 0 ${width} ${height}`} className="h-full w-full" role="img" aria-label="Collaboration map">
      <defs>
        <radialGradient id="busGlow" cx="50%" cy="50%" r="50%">
          <stop offset="0%" stopColor="#a8d4fc" stopOpacity="0.55" />
          <stop offset="100%" stopColor="#a8d4fc" stopOpacity="0" />
        </radialGradient>
        <linearGradient id="busSpine" x1="0%" x2="100%" y1="0%" y2="0%">
          <stop offset="0%" stopColor="#7ab8a3" stopOpacity="0" />
          <stop offset="48%" stopColor="#a8d4fc" stopOpacity="0.44" />
          <stop offset="100%" stopColor="#7ab8a3" stopOpacity="0" />
        </linearGradient>
        <pattern id="collabGrid" width="32" height="32" patternUnits="userSpaceOnUse">
          <path d="M 32 0 L 0 0 0 32" fill="none" stroke="rgba(255,255,255,0.045)" strokeWidth="1" />
        </pattern>
        <marker id="collabArrow" markerHeight="8" markerWidth="8" orient="auto" refX="7" refY="4">
          <path d="M 0 0 L 8 4 L 0 8 z" fill="#a8d4fc" fillOpacity="0.55" />
        </marker>
        <marker id="collabReadArrow" markerHeight="8" markerWidth="8" orient="auto" refX="7" refY="4">
          <path d="M 0 0 L 8 4 L 0 8 z" fill="#8a9491" fillOpacity="0.52" />
        </marker>
      </defs>
      <rect x="0" y="0" width={width} height={height} fill="url(#collabGrid)" opacity="0.45" />
      <circle cx={busX} cy={busY + 16} r="170" fill="url(#busGlow)" opacity={hasEvents ? '0.16' : '0.08'} />
      <rect x="54" y={busY + 68} width={width - 108} height="2" rx="1" fill="url(#busSpine)" opacity={hasEvents ? '0.72' : '0.28'} />

      {collaborationEdges.map((edge) => {
        const from = positions.get(edge.fromId)
        const to = positions.get(edge.toId)
        if (!from || !to) return null
        const lift = Math.max(18, Math.min(76, Math.abs(from.cx - to.cx) * 0.16))
        const midY = Math.min(height - 18, Math.max(from.bottomY, to.bottomY) + lift)
        return (
          <path
            key={`${edge.fromId}-${edge.toId}`}
            d={`M ${from.cx} ${from.bottomY} C ${from.cx} ${midY} ${to.cx} ${midY} ${to.cx} ${to.bottomY}`}
            fill="none"
            stroke="#d0a040"
            strokeDasharray="4 7"
            strokeLinecap="round"
            strokeOpacity={Math.min(0.44, 0.1 + edge.count * 0.08)}
            strokeWidth={Math.min(2.4, 0.9 + edge.count * 0.25)}
          />
        )
      })}

      {publishEdges.map((node) => {
        const anchorX = busX + (node.cx - busX) * 0.16
        const anchorY = busY + busH / 2 + 22
        const active = node.agent.status === 'running'
        return (
          <g key={`${node.agent.session.id}-publish`}>
            <path
              d={`M ${node.cx} ${node.topY} C ${node.cx} ${node.topY - 52} ${anchorX} ${anchorY + 34} ${anchorX} ${anchorY}`}
              fill="none"
              markerEnd="url(#collabArrow)"
              stroke={topicHex(node.latest?.topic === 'read' ? 'findings' : (node.latest?.topic ?? 'findings'))}
              strokeLinecap="round"
              strokeOpacity={active ? 0.62 : 0.34}
              strokeWidth={Math.min(4, 1.2 + node.published * 0.26)}
            />
          </g>
        )
      })}

      {readEdges.map((node) => {
        const anchorX = busX + (node.cx - busX) * 0.16
        const anchorY = busY + busH / 2 + 22
        return (
          <path
            key={`${node.agent.session.id}-read`}
            d={`M ${anchorX} ${anchorY} C ${anchorX} ${anchorY + 36} ${node.cx} ${node.topY - 44} ${node.cx} ${node.topY}`}
            fill="none"
            markerEnd="url(#collabReadArrow)"
            stroke="#8a9491"
            strokeDasharray="6 7"
            strokeLinecap="round"
            strokeOpacity={node.agent.status === 'running' ? 0.56 : 0.3}
            strokeWidth={Math.min(3.2, 1 + node.reads * 0.34)}
          />
        )
      })}

      <rect
        x={busX - busW / 2}
        y={busY - busH / 2}
        width={busW}
        height={busH}
        rx="16"
        fill="rgba(10,14,13,0.82)"
        stroke="rgba(168,212,252,0.42)"
      />
      <rect
        x={busX - busW / 2 + 12}
        y={busY - busH / 2 + 12}
        width="3"
        height={busH - 24}
        rx="1.5"
        fill="#a8d4fc"
        opacity={hasEvents ? '0.72' : '0.28'}
      />
      <text x={busX} y={busY - 16} textAnchor="middle" fill="#e8e8e8" fontSize="16" fontFamily="Departure Mono, monospace">
        message bus
      </text>
      <text x={busX} y={busY + 5} textAnchor="middle" fill="#8e9a96" fontSize="10" fontFamily="Departure Mono, monospace">
        {events.length} events / {nodes.length} agents / {rounds.length} rounds
      </text>
      {hasEvents ? (
        (['findings', 'progress', 'errors', 'read'] as BusMessageRecord['topic'][]).map((topic, index) => {
          const barW = 20 + (topicTotals[topic] / maxTopicTotal) * 82
          return (
            <g key={topic}>
              <rect
                x={busX - 92}
                y={busY + 21 + index * 9}
                width={barW}
                height="4"
                rx="2"
                fill={topicHex(topic)}
                opacity={topicTotals[topic] > 0 ? 0.72 : 0.18}
              />
              <text x={busX + 18} y={busY + 25 + index * 9} fill="#7a8582" fontSize="8" fontFamily="Departure Mono, monospace">
                {topic} {topicTotals[topic]}
              </text>
            </g>
          )
        })
      ) : (
        <text x={busX} y={busY + 30} textAnchor="middle" fill="#687370" fontSize="9" fontFamily="Departure Mono, monospace">
          no bus traffic yet
        </text>
      )}

      {roundViews.map((view, roundIndex) => (
        <g key={view.round.id}>
          <rect
            x={view.x}
            y={view.y}
            width={view.w}
            height={view.h}
            rx="18"
            fill="rgba(255,255,255,0.018)"
            stroke="rgba(168,212,252,0.12)"
          />
          <text x={view.x + 16} y={view.y + 25} fill="#a8d4fc" fontSize="10" fontFamily="Departure Mono, monospace">
            ROUND {roundIndex + 1}
          </text>
          <text x={view.x + 86} y={view.y + 25} fill="#687370" fontSize="9" fontFamily="Departure Mono, monospace">
            {view.round.agents.length} agents / {relativeTime(view.round.startAt)}
          </text>
          {view.cards.map((node) => {
            const cardActive = node.agent.status === 'running'
            const title = truncateText(node.agent.session.title || node.agent.sub?.label || node.agent.session.id, node.w > 220 ? 31 : 22)
            const task = truncateText(node.agent.session.task ?? node.agent.sub?.task ?? 'No task description', node.w > 220 ? 34 : 24)
            const tools = node.agent.tools.length || node.agent.sub?.toolsCalled || 0
            return (
              <g
                key={node.agent.session.id}
                onClick={() => node.agent.attachable && onAttach(node.agent.session.id)}
                style={{ cursor: node.agent.attachable ? 'pointer' : 'default' }}
              >
                <rect
                  x={node.x}
                  y={node.y}
                  width={node.w}
                  height={node.h}
                  rx="10"
                  fill={cardActive ? 'rgba(255,255,255,0.055)' : 'rgba(255,255,255,0.032)'}
                  stroke={node.color}
                  strokeOpacity={cardActive ? 0.56 : 0.24}
                />
                <rect x={node.x + 9} y={node.y + 9} width="3" height={node.h - 18} rx="1.5" fill={node.color} opacity={cardActive ? 0.88 : 0.62} />
                <circle cx={node.x + node.w - 16} cy={node.y + 15} r="4" fill={statusHex(node.agent.status)} opacity="0.92" />
                <text x={node.x + 22} y={node.y + 18} fill="#e8e8e8" fontSize="10.5" fontFamily="Departure Mono, monospace">
                  {title}
                </text>
                <text x={node.x + 22} y={node.y + 33} fill="#7f8b87" fontSize="8.5" fontFamily="Departure Mono, monospace">
                  {node.agent.agentType} / {node.agent.status.toUpperCase()} / tools {tools}
                </text>
                <text x={node.x + 22} y={node.y + 46} fill="#6e7774" fontSize="8" fontFamily="Departure Mono, monospace">
                  pub {node.published} / read {node.reads} / {task}
                </text>
              </g>
            )
          })}
          {view.hidden > 0 && (
            <text x={view.x + view.w / 2} y={view.y + view.h - 14} textAnchor="middle" fill="#7a8582" fontSize="9" fontFamily="Departure Mono, monospace">
              +{view.hidden} more agents in lanes below
            </text>
          )}
        </g>
      ))}

      {agents.length > nodes.length && (
        <text x={busX} y={height - 16} textAnchor="middle" fill="#7a8582" fontSize="10" fontFamily="Departure Mono, monospace">
          +{agents.length - nodes.length} more agents in lanes below
        </text>
      )}
      {nodes.length === 0 && (
        <g>
          <rect
            x={busX - 240}
            y={graphTop + 72}
            width="480"
            height="86"
            rx="18"
            fill="rgba(255,255,255,0.022)"
            stroke="rgba(255,255,255,0.08)"
            strokeDasharray="5 7"
          />
          <text x={busX} y={graphTop + 112} textAnchor="middle" fill="#8e9a96" fontSize="12" fontFamily="Departure Mono, monospace">
            spawn agents to form collaboration rounds
          </text>
          <text x={busX} y={graphTop + 136} textAnchor="middle" fill="#687370" fontSize="9" fontFamily="Departure Mono, monospace">
            publish, read, and cross-agent reuse edges will appear here
          </text>
        </g>
      )}
    </svg>
  )
}

interface AgentRoundView {
  id: string
  agents: AgentView[]
  startAt: number
  endAt: number
}

interface GraphAgentNode {
  agent: AgentView
  x: number
  y: number
  w: number
  h: number
  cx: number
  topY: number
  bottomY: number
  color: string
  published: number
  reads: number
  latest?: BusEventView
}

function agentStartedAt(agent: AgentView): number {
  return agent.sub?.startedAt ?? agent.session.createdAt ?? 0
}

function normalizeBusTimestamp(timestamp: number): number {
  if (!Number.isFinite(timestamp) || timestamp <= 0) return Date.now()
  return timestamp < 1_000_000_000_000 ? timestamp * 1000 : timestamp
}

function groupAgentRounds(agents: AgentView[]): AgentRoundView[] {
  const sorted = agents.slice().sort((a, b) => agentStartedAt(a) - agentStartedAt(b))
  const rounds: AgentRoundView[] = []
  const roundGapMs = 90_000
  for (const agent of sorted) {
    const startedAt = agentStartedAt(agent)
    const current = rounds[rounds.length - 1]
    if (!current || startedAt - current.endAt > roundGapMs) {
      rounds.push({
        id: `round-${rounds.length + 1}`,
        agents: [agent],
        startAt: startedAt,
        endAt: startedAt,
      })
      continue
    }
    current.agents.push(agent)
    current.endAt = Math.max(current.endAt, startedAt)
  }
  return rounds
}

function inferCollaborationEdges(
  events: BusEventView[],
  positions: Map<string, GraphAgentNode>,
): Array<{ fromId: string; toId: string; count: number; lastAt: number }> {
  const pairs = new Map<string, { fromId: string; toId: string; count: number; lastAt: number }>()
  const latestPublishBySender = new Map<string, BusEventView>()
  const chronological = events.slice().sort((a, b) => a.timestamp - b.timestamp)

  for (const event of chronological) {
    if (!positions.has(event.senderId)) continue
    if (event.topic !== 'read') {
      latestPublishBySender.set(event.senderId, event)
      continue
    }

    const readers = Array.from(latestPublishBySender.values())
      .filter((published) => published.senderId !== event.senderId)
      .filter((published) => positions.has(published.senderId))
      .sort((a, b) => b.timestamp - a.timestamp)
      .slice(0, 4)

    for (const published of readers) {
      const key = `${published.senderId}->${event.senderId}`
      const existing = pairs.get(key)
      if (existing) {
        existing.count += 1
        existing.lastAt = Math.max(existing.lastAt, event.timestamp)
      } else {
        pairs.set(key, {
          fromId: published.senderId,
          toId: event.senderId,
          count: 1,
          lastAt: event.timestamp,
        })
      }
    }
  }

  return Array.from(pairs.values())
    .sort((a, b) => b.count - a.count || b.lastAt - a.lastAt)
    .slice(0, 24)
}

function BusFeed({ events }: { events: BusEventView[] }) {
  return (
    <div className="mt-3 min-h-0 flex-1 space-y-1.5 overflow-auto pr-1">
      {events.slice(0, 24).map((event) => (
        <div
          key={`${event.sessionId}-${event.index}-${event.timestamp}`}
          className="grid grid-cols-[62px_70px_minmax(0,1fr)] gap-2 rounded-md bg-white/[0.025] px-2 py-1.5 font-mono text-[9.5px] ring-hairline"
        >
          <span className="text-fg-3">{relativeTime(event.timestamp)}</span>
          <span className={topicClass(event.topic)}>{event.topic}</span>
          <span className="truncate text-fg-1">{event.senderLabel}: {event.content}</span>
        </div>
      ))}
      {events.length === 0 && (
        <EmptyState title="No bus traffic" body="Message bus activity will appear here as agents collaborate." />
      )}
    </div>
  )
}

function WorkProducts({
  fileChanges,
  artifacts,
  onJumpTool,
}: {
  fileChanges: FileChangeSet[]
  artifacts: ArtifactRecord[]
  onJumpTool: (id: string) => void
}) {
  const latestChange = fileChanges[0]
  return (
    <div className="mt-3 min-h-0 flex-1 space-y-2 overflow-auto pr-1">
      {latestChange && (
        <div className="rounded-lg bg-white/[0.025] p-2 ring-hairline">
          <div className="mb-2 flex items-center justify-between gap-2">
            <div className="label">latest change</div>
            <button
              onClick={() => onJumpTool(latestChange.toolCallId)}
              className="rounded bg-white/[0.05] px-1.5 py-[2px] font-mono text-[9px] uppercase tracking-[0.08em] text-fg-2 ring-hairline hover:bg-accent/10 hover:text-accent"
            >
              jump
            </button>
          </div>
          <div className="truncate font-mono text-[11px] text-fg-0">
            {latestChange.files[0]?.filename ?? latestChange.toolName}
          </div>
          <div className="mt-1 font-mono text-[9px] text-fg-3">
            {latestChange.totals.files} files / <span className="text-ok">+{latestChange.totals.additions}</span>{' '}
            <span className="text-danger">-{latestChange.totals.deletions}</span>
          </div>
        </div>
      )}
      {artifacts.slice(0, 4).map((artifact) => (
        <button
          key={artifact.id}
          onClick={() => openExternal(artifact.path)}
          className="block w-full rounded-lg bg-white/[0.025] px-2 py-2 text-left ring-hairline hover:bg-white/[0.05]"
        >
          <div className="truncate font-mono text-[11px] text-fg-0">{artifact.filename}</div>
          <div className="mt-1 flex items-center gap-2 font-mono text-[9px] text-fg-3">
            <span>{artifact.fileType}</span>
            <span>/</span>
            <span>{artifact.creatorLabel}</span>
            <span>/</span>
            <span>{relativeTime(artifact.createdAt)}</span>
          </div>
        </button>
      ))}
      {!latestChange && artifacts.length === 0 && (
        <EmptyState title="No work products" body="File edits and created artifacts will collect here." />
      )}
    </div>
  )
}

function FindingCard({
  event,
  compact,
  onCopy,
}: {
  event: BusEventView
  compact?: boolean
  onCopy?: () => void
}) {
  return (
    <div className="rounded-lg bg-white/[0.028] p-3 ring-hairline">
      <div className="mb-2 flex items-center gap-2">
        <span className={`rounded px-1.5 py-[1px] font-mono text-[9px] uppercase tracking-[0.08em] ${topicBadgeClass(event.topic)}`}>
          {event.topic}
        </span>
        <span className="truncate font-mono text-[10px] text-fg-2">{event.senderLabel}</span>
        <span className="ml-auto font-mono text-[9px] text-fg-3">{relativeTime(event.timestamp)}</span>
      </div>
      <div className={`selectable text-[11px] leading-[1.55] text-fg-1 ${compact ? 'line-clamp-3' : ''}`}>
        {event.content}
      </div>
      {onCopy && (
        <button
          onClick={onCopy}
          className="mt-2 rounded bg-white/[0.04] px-1.5 py-[2px] font-mono text-[9px] uppercase tracking-[0.08em] text-fg-3 ring-hairline hover:bg-white/[0.08] hover:text-fg-0"
        >
          copy
        </button>
      )}
    </div>
  )
}

function CompactionCard({ event }: { event: TelemetryEventView }) {
  const beforeTokens = detailNumber(event, 'context_tokens_before') || detailNumber(event, 'tokens_before')
  const afterTokens = detailNumber(event, 'context_tokens_after') || detailNumber(event, 'tokens_after')
  const requestBefore = detailNumber(event, 'request_tokens_before')
  const requestAfter = detailNumber(event, 'request_tokens_after')
  const providerBefore = detailNumber(event, 'last_provider_context_tokens')
  const transcriptBefore = detailNumber(event, 'transcript_tokens_before')
  const transcriptAfter = detailNumber(event, 'transcript_tokens_after')
  const entriesRemoved = detailNumber(event, 'entries_removed')
  const beforeMessages = detailNumber(event, 'messages_before')
  const afterMessages = detailNumber(event, 'messages_after')
  const beforeImages = detailNumber(event, 'images_before')
  const afterImages = detailNumber(event, 'images_after')
  const summary = detailString(event, 'summary_preview')
  const beforePreview = detailString(event, 'before_preview')
  const beforeSnapshotPath = detailString(event, 'before_snapshot_path')
  const beforeSnapshotJsonPath = detailString(event, 'before_snapshot_json_path')
  const afterSnapshotPath = detailString(event, 'after_snapshot_path')
  const saved = Math.max(0, beforeTokens - afterTokens)
  const pct = beforeTokens > 0 ? Math.max(4, Math.min(100, Math.round((afterTokens / beforeTokens) * 100))) : 100
  return (
    <div className="rounded-xl bg-white/[0.028] p-4 ring-hairline">
      <div className="mb-3 flex items-start gap-3">
        <div className="mt-1 grid h-10 w-10 shrink-0 place-items-center rounded-lg bg-ok/10 ring-1 ring-ok/25">
          <span className="font-mono text-[13px] text-ok">Σ</span>
        </div>
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-2">
            <span className="font-mono text-[11px] uppercase tracking-[0.08em] text-ok">llm summary</span>
            <span className="rounded bg-white/[0.04] px-1.5 py-[1px] font-mono text-[9px] text-fg-3 ring-hairline">
              {detailString(event, 'trigger') || 'context'}
            </span>
            <span className="ml-auto font-mono text-[9px] text-fg-3">{relativeTime(event.at)}</span>
          </div>
          <div className="mt-1 truncate font-mono text-[10px] text-fg-2">{event.sessionTitle}</div>
        </div>
      </div>
      <div className="grid gap-2 md:grid-cols-[1fr_auto_1fr]">
        <CompactionPoint label="before" tokens={beforeTokens} messages={beforeMessages} images={beforeImages} />
        <div className="hidden items-center px-2 text-fg-3 md:flex">→</div>
        <CompactionPoint label="after" tokens={afterTokens} messages={afterMessages} images={afterImages} tone="ok" />
      </div>
      <div className="mt-3 grid gap-2 sm:grid-cols-2 xl:grid-cols-4">
        <BriefStat label="request estimate" value={`${formatTokens(requestBefore || beforeTokens)} -> ${formatTokens(requestAfter || afterTokens)}`} />
        <BriefStat label="text payload" value={`${formatTokens(transcriptBefore)} -> ${formatTokens(transcriptAfter)}`} />
        <BriefStat label="entries summarized" value={String(entriesRemoved || beforeMessages)} />
        <BriefStat label="last api context" value={providerBefore ? formatTokens(providerBefore) : 'n/a'} />
      </div>
      <div className="mt-3">
        <div className="mb-1 flex items-center justify-between font-mono text-[9px] text-fg-3">
          <span>context retained</span>
          <span>{formatTokens(saved)} saved</span>
        </div>
        <div className="h-2 overflow-hidden rounded-full bg-white/[0.07]">
          <div className="h-full rounded-full bg-ok/65" style={{ width: `${pct}%` }} />
        </div>
      </div>
      {(beforeSnapshotPath || afterSnapshotPath || beforeSnapshotJsonPath) && (
        <div className="mt-3 flex flex-wrap gap-2">
          {beforeSnapshotPath && (
            <button
              type="button"
              onClick={() => openExternal(beforeSnapshotPath)}
              className="rounded bg-white/[0.045] px-2 py-1 font-mono text-[9px] uppercase tracking-[0.08em] text-accent ring-hairline hover:bg-white/[0.08] hover:text-fg-0"
            >
              before snapshot
            </button>
          )}
          {beforeSnapshotJsonPath && (
            <button
              type="button"
              onClick={() => openExternal(beforeSnapshotJsonPath)}
              className="rounded bg-white/[0.035] px-2 py-1 font-mono text-[9px] uppercase tracking-[0.08em] text-fg-2 ring-hairline hover:bg-white/[0.08] hover:text-fg-0"
            >
              raw transcript
            </button>
          )}
          {afterSnapshotPath && (
            <button
              type="button"
              onClick={() => openExternal(afterSnapshotPath)}
              className="rounded bg-ok/10 px-2 py-1 font-mono text-[9px] uppercase tracking-[0.08em] text-ok ring-1 ring-ok/20 hover:bg-ok/16 hover:text-fg-0"
            >
              after snapshot
            </button>
          )}
        </div>
      )}
      <div className="mt-3 grid gap-3 xl:grid-cols-2">
        <SnapshotPreview
          title="original before compaction"
          text={beforePreview || 'No before snapshot preview was captured for this event.'}
        />
        <SnapshotPreview
          title="llm summary after compaction"
          text={summary || 'No summary preview was captured for this event.'}
        />
      </div>
    </div>
  )
}

function CompactionPoint({
  label,
  tokens,
  messages,
  images,
  tone,
}: {
  label: string
  tokens: number
  messages: number
  images: number
  tone?: 'ok'
}) {
  return (
    <div className="rounded-lg bg-white/[0.025] p-2 ring-hairline">
      <div className={`label text-[9px] ${tone === 'ok' ? 'text-ok' : 'text-fg-3'}`}>{label}</div>
      <div className="mt-1 font-mono text-[17px] leading-none text-fg-0">{formatTokens(tokens)}</div>
      <div className="mt-2 flex items-center gap-3 font-mono text-[9px] text-fg-3">
        <span>{messages} entries</span>
        <span>{images} imgs</span>
      </div>
    </div>
  )
}

function SnapshotPreview({ title, text }: { title: string; text: string }) {
  return (
    <div className="rounded-lg bg-black/25 p-3 ring-hairline">
      <div className="label mb-2 text-[9px]">{title}</div>
      <div className="max-h-[260px] overflow-auto whitespace-pre-wrap pr-1 font-mono text-[10.5px] leading-[1.55] text-fg-2">
        {text}
      </div>
    </div>
  )
}

function MediaPruneRow({ event }: { event: TelemetryEventView }) {
  const before = detailNumber(event, 'images_before')
  const after = detailNumber(event, 'images_after')
  const omitted = detailNumber(event, 'omitted_images')
  return (
    <div className="rounded-lg bg-white/[0.025] p-2 ring-hairline">
      <div className="flex items-center gap-2">
        <span className="rounded bg-accent/10 px-1.5 py-[1px] font-mono text-[9px] uppercase tracking-[0.08em] text-accent ring-1 ring-accent/20">
          images
        </span>
        <span className="truncate font-mono text-[10px] text-fg-2">{event.sessionTitle}</span>
        <span className="ml-auto font-mono text-[9px] text-fg-3">{relativeTime(event.at)}</span>
      </div>
      <div className="mt-2 flex items-center justify-between gap-3 font-mono text-[11px]">
        <span className="text-fg-1">{before} → {after}</span>
        <span className="text-ok">{omitted} omitted</span>
      </div>
      <div className="mt-2 h-1.5 overflow-hidden rounded-full bg-white/[0.07]">
        <div
          className="h-full rounded-full bg-accent/70"
          style={{ width: `${before > 0 ? Math.max(5, Math.min(100, (after / before) * 100)) : 100}%` }}
        />
      </div>
    </div>
  )
}

function TelemetryAgentRow({
  agent,
  events,
}: {
  agent: AgentView
  events: TelemetryEventView[]
}) {
  const mine = events.filter((event) => event.sessionId === agent.session.id)
  const compactions = mine.filter((event) => event.subtype === 'compaction_complete').length
  const mediaPrunes = mine.filter((event) => event.subtype === 'media_pruning').length
  const color = PROFILE_HEX[agent.agentType] ?? PROFILE_HEX.general
  return (
    <div className="rounded-lg bg-white/[0.025] px-2 py-2 ring-hairline">
      <div className="flex items-center gap-2">
        <span className="h-2 w-2 shrink-0 rounded-full" style={{ background: color }} />
        <span className="min-w-0 flex-1 truncate font-mono text-[10.5px] text-fg-0">
          {agent.session.title || agent.session.id}
        </span>
        <span className={`font-mono text-[9px] uppercase ${statusTextClass(agent.status)}`}>{agent.status}</span>
      </div>
      <div className="mt-1 flex items-center gap-3 font-mono text-[9px] text-fg-3">
        <span>{agent.agentType}</span>
        <span>{compactions} summaries</span>
        <span>{mediaPrunes} image trims</span>
      </div>
    </div>
  )
}

function ProfileCard({ profile }: { profile: ProfileDefinition }) {
  const color = PROFILE_HEX[profile.id] ?? PROFILE_HEX.general
  return (
    <div className="rounded-xl bg-white/[0.028] p-4 ring-hairline transition hover:bg-white/[0.045]">
      <div className="mb-3 flex items-start gap-3">
        <div
          className="mt-1 h-7 w-7 shrink-0 rounded-lg bg-black/25 ring-hairline"
          style={{ boxShadow: `inset 0 0 0 1px ${color}55` }}
        >
          <div
            className="mx-auto mt-[11px] h-1.5 w-1.5 rounded-full"
            style={{ background: color }}
          />
        </div>
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2">
            <AgentTypeTag type={profile.id} />
            <span className="font-mono text-[10px] text-fg-3">
              max {profile.maxIterations}
            </span>
          </div>
          <p className="mt-2 text-[12px] leading-[1.5] text-fg-0">{profile.bestFor}</p>
        </div>
      </div>
      <div className="grid grid-cols-2 gap-2 font-mono text-[10px]">
        <ProfileField label="model" value={profile.model} />
        <ProfileField label="thinking" value={profile.thinking} />
        <ProfileField label="tools" value={profile.tools} wide />
      </div>
      <div className="mt-3 rounded-lg bg-black/20 p-2 text-[10.5px] leading-[1.45] text-fg-2 ring-hairline">
        {profile.note}
      </div>
    </div>
  )
}

function ProfileField({
  label,
  value,
  wide,
}: {
  label: string
  value: string
  wide?: boolean
}) {
  return (
    <div className={`rounded-md bg-white/[0.025] px-2 py-1.5 ring-hairline ${wide ? 'col-span-2' : ''}`}>
      <div className="mb-1 uppercase tracking-[0.1em] text-fg-3">{label}</div>
      <div className="leading-[1.4] text-fg-1">{value}</div>
    </div>
  )
}

function CompactAgentRow({
  agent,
  onAttach,
}: {
  agent: AgentView
  onAttach: (id: string) => void
}) {
  const color = PROFILE_HEX[agent.agentType] ?? PROFILE_HEX.general
  return (
    <button
      onClick={() => agent.attachable && onAttach(agent.session.id)}
      className="flex w-full items-center gap-2 rounded-md bg-white/[0.025] px-2 py-1.5 text-left ring-hairline hover:bg-white/[0.05]"
    >
      <span className="h-2 w-2 shrink-0 rounded-full" style={{ background: color }} />
      <span className="min-w-0 flex-1">
        <span className="block truncate font-mono text-[10.5px] text-fg-0">
          {agent.session.title || agent.session.id}
        </span>
        <span className="block truncate font-mono text-[9px] text-fg-3">
          {agent.agentType} / {agent.status}
        </span>
      </span>
      {agent.status === 'running' && <Spinner name="scan" className="text-accent" />}
    </button>
  )
}

function ToolActivityStrip({
  tools,
  large,
}: {
  tools: ToolCallRecord[]
  large?: boolean
}) {
  const recent = tools.slice(-(large ? 18 : 12))
  return (
    <div className="mt-3 flex h-6 items-center gap-1 overflow-hidden">
      {recent.length === 0 ? (
        <div className="h-1 w-full rounded-full bg-white/[0.04]" />
      ) : (
        recent.map((tool) => (
          <div
            key={tool.id}
            title={`${tool.name} / ${tool.status}`}
            className={`h-2 min-w-[18px] flex-1 rounded-full ${toolStatusClass(tool.status)}`}
          />
        ))
      )}
    </div>
  )
}

function MetricCard({
  label,
  value,
  sub,
  tone = 'neutral',
  meter,
}: {
  label: string
  value: string
  sub: string
  tone?: 'neutral' | 'accent' | 'ok' | 'warn'
  meter?: number
}) {
  const toneClass =
    tone === 'accent'
      ? 'text-accent'
      : tone === 'ok'
        ? 'text-ok'
        : tone === 'warn'
          ? 'text-warn'
          : 'text-fg-0'
  return (
    <div className="rounded-xl glass-strong px-3 py-3">
      <div className="label">{label}</div>
      <div className={`mt-2 font-mono text-[20px] leading-none ${toneClass}`}>{value}</div>
      <div className="mt-1 truncate font-mono text-[10px] text-fg-3">{sub}</div>
      {typeof meter === 'number' && (
        <div className="mt-3 h-1 overflow-hidden rounded-full bg-white/10">
          <div className="h-full rounded-full bg-accent/70" style={{ width: `${meter}%` }} />
        </div>
      )}
    </div>
  )
}

function BriefStat({
  label,
  value,
  tone,
}: {
  label: string
  value: string
  tone?: 'danger'
}) {
  return (
    <div className="rounded-lg bg-white/[0.03] p-2 ring-hairline">
      <div className="label text-[9px]">{label}</div>
      <div className={`mt-1 font-mono text-[16px] leading-none ${tone === 'danger' ? 'text-danger' : 'text-fg-0'}`}>
        {value}
      </div>
    </div>
  )
}

function PanelHeader({
  label,
  action,
  onAction,
}: {
  label: string
  action?: string
  onAction?: () => void
}) {
  return (
    <div className="flex items-center justify-between gap-2">
      <div className="label flex items-center gap-2 text-fg-2">
        <span className="h-1.5 w-1.5 rounded-full bg-accent" />
        {label}
      </div>
      {action && onAction && (
        <button
          onClick={onAction}
          className="rounded bg-white/[0.04] px-1.5 py-[2px] font-mono text-[9px] uppercase tracking-[0.08em] text-fg-3 ring-hairline hover:bg-white/[0.08] hover:text-fg-0"
        >
          {action}
        </button>
      )}
    </div>
  )
}

function StatusPill({ status }: { status: 'running' | 'attention' | 'idle' }) {
  const cls =
    status === 'running'
      ? 'text-accent ring-accent/25 bg-accent/10'
      : status === 'attention'
        ? 'text-warn ring-warn/25 bg-warn/10'
        : 'text-fg-2 ring-white/[0.08] bg-white/[0.035]'
  return (
    <span className={`rounded px-2 py-[2px] font-mono text-[9px] uppercase tracking-[0.1em] ring-1 ${cls}`}>
      {status}
    </span>
  )
}

function EmptyState({
  title,
  body,
  compact,
}: {
  title: string
  body: string
  compact?: boolean
}) {
  return (
    <div className={`rounded-lg border border-dashed border-white/[0.08] bg-white/[0.015] ${compact ? 'p-2' : 'p-4'}`}>
      <div className="font-mono text-[11px] text-fg-1">{title}</div>
      <div className="mt-1 text-[10.5px] leading-[1.45] text-fg-3">{body}</div>
    </div>
  )
}

function resolveAgentStatus(
  session: SessionSnapshot,
  sub?: SubagentRecord,
): AgentView['status'] {
  if (session.completed) return session.success === false ? 'failed' : 'done'
  if (sub?.state) return sub.state
  return 'running'
}

function lastUserText(messages: Array<{ role: string; parts: Array<{ type: string; text?: string }> }>): string | null {
  for (let i = messages.length - 1; i >= 0; i -= 1) {
    const message = messages[i]
    if (message.role !== 'user') continue
    const text = message.parts
      .filter((part) => part.type === 'text' && part.text)
      .map((part) => part.text)
      .join(' ')
      .trim()
    if (text) return text
  }
  return null
}

function dedupeBy<T>(items: T[], key: (item: T) => string): T[] {
  const seen = new Set<string>()
  const out: T[] = []
  for (const item of items) {
    const k = key(item)
    if (seen.has(k)) continue
    seen.add(k)
    out.push(item)
  }
  return out
}

function countBy<T>(items: T[], key: (item: T) => string): Record<string, number> {
  const counts: Record<string, number> = {}
  for (const item of items) {
    const k = key(item)
    counts[k] = (counts[k] ?? 0) + 1
  }
  return counts
}

function truncateText(text: string, max: number): string {
  return text.length > max ? `${text.slice(0, Math.max(0, max - 3))}...` : text
}

function detailNumber(event: TelemetryEventView | undefined, key: string): number {
  const value = event?.details?.[key]
  if (typeof value === 'number' && Number.isFinite(value)) return value
  if (typeof value === 'string') {
    const parsed = Number(value)
    if (Number.isFinite(parsed)) return parsed
  }
  return 0
}

function detailString(event: TelemetryEventView | undefined, key: string): string {
  const value = event?.details?.[key]
  return typeof value === 'string' ? value : ''
}

function topicHex(topic: BusMessageRecord['topic']): string {
  if (topic === 'errors') return '#f07878'
  if (topic === 'findings') return '#a8d4fc'
  if (topic === 'progress') return '#88d67f'
  return '#8a9491'
}

function statusHex(status: AgentView['status']): string {
  if (status === 'failed' || status === 'cancelled') return '#f07878'
  if (status === 'done') return '#88d67f'
  if (status === 'running') return '#a8d4fc'
  return '#8a9491'
}

function topicBadgeClass(topic: BusMessageRecord['topic']): string {
  if (topic === 'errors') return 'bg-danger/12 text-danger ring-1 ring-danger/25'
  if (topic === 'findings') return 'bg-accent/12 text-accent ring-1 ring-accent/25'
  if (topic === 'progress') return 'bg-ok/10 text-ok ring-1 ring-ok/20'
  return 'bg-white/[0.05] text-fg-2 ring-1 ring-white/[0.08]'
}

function topicClass(topic: BusMessageRecord['topic']): string {
  if (topic === 'errors') return 'text-danger'
  if (topic === 'findings') return 'text-accent'
  if (topic === 'progress') return 'text-ok'
  return 'text-fg-3'
}

function statusTextClass(status: AgentView['status']): string {
  if (status === 'failed' || status === 'cancelled') return 'text-danger'
  if (status === 'done') return 'text-ok'
  if (status === 'running') return 'text-accent'
  return 'text-fg-2'
}

function toolStatusClass(status: ToolCallRecord['status']): string {
  if (status === 'error') return 'bg-danger/70'
  if (status === 'success') return 'bg-ok/60'
  return 'bg-accent/70 animate-pulse-soft'
}

function openExternal(path: string) {
  const api = (window as any).harness
  if (api?.openExternal) api.openExternal(`file://${path}`)
}

function copyFinding(
  event: BusEventView,
  showToast: (message: string, tone?: 'info' | 'ok' | 'warn' | 'danger') => void,
) {
  const text = `[${event.topic}] ${event.senderLabel}: ${event.content}`
  navigator.clipboard
    ?.writeText(text)
    .then(() => showToast('Finding copied', 'ok'))
    .catch(() => showToast('Could not copy finding', 'warn'))
}
