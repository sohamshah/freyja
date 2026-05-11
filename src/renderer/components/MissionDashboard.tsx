import { useEffect, useLayoutEffect, useMemo, useRef, useState, type ReactNode } from 'react'
import { useHarness, type SessionSlice, type SystemEventRecord } from '../state/store'
import type {
  ArtifactRecord,
  BusMessageRecord,
  CoordinationStrategy,
  FileChangeSet,
  SessionSnapshot,
  SubagentRecord,
  ToolCallRecord,
} from '@shared/events'
import { formatCost, formatDuration, formatTokens, relativeTime } from '../lib/format'
import { Spinner } from '../lib/spinner'
import { AgentTypeTag } from './SubagentCard'
import { KanbanAutopilotStrip } from './kanban/AutopilotStrip'
import { KanbanDispatchTicker } from './kanban/DispatchTicker'
import { MissionCoverCard } from './kanban/MissionCoverCard'
import { KanbanWatchList } from './kanban/WatchList'

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
  kanbanTaskId?: string
  taskId?: string
}

interface BusEventView extends BusMessageRecord {
  sessionId: string
}

interface TelemetryEventView extends SystemEventRecord {
  sessionId: string
  sessionTitle: string
}

interface KanbanCardView {
  id: string
  title: string
  status: string
  body?: string
  assignee?: string
  createdBy?: string
  priority?: number
  summary?: string
  result?: string
  parents?: string[]
  children?: string[]
  comments?: Array<{ author?: string; body: string; timestamp?: number }>
  events?: Array<{ kind?: string; actor?: string; message?: string; timestamp?: number; details?: Record<string, unknown> }>
  agents?: AgentView[]
  progress?: number
  createdAt?: number
  startedAt?: number
  updatedAt?: number
  completedAt?: number
  // Move F — populated by the bridge's circuit-breaker accounting.
  consecutiveFailures?: number
  // Opt-in verification (Move C). When true, the worker's `complete`
  // routes the card to `done_unverified` so the verifier picks it up.
  requiresVerification?: boolean
  // Move D — populated when the specifier has filled in structured fields.
  spec?: {
    definition_of_done?: string[]
    references?: { files?: string[]; findings?: string[]; cards?: string[] }
    verify_with?: string
    token_budget?: number
  }
  metadata?: Record<string, unknown>
}

interface GoalStateView {
  goal: string
  status: string
  turnsUsed: number
  maxTurns: number
  pauseReason?: string
  lastVerdict?: {
    done?: boolean
    reason?: string
    confidence?: number
  } | null
  events: TelemetryEventView[]
}

interface TaskCardView {
  id: string
  title: string
  status: string
  body?: string
  assignee?: string
  priority?: number
  progress?: number
  summary?: string
  result?: string
  artifacts?: string[]
  events?: Array<{ kind?: string; actor?: string; message?: string; timestamp?: number; details?: Record<string, unknown> }>
  agents?: AgentView[]
  createdAt?: number
  startedAt?: number
  updatedAt?: number
  completedAt?: number
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
  const openSessionPane = useHarness((s) => s.openSessionPane)
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
        kanbanTaskId: session.kanbanTaskId ?? parentSub?.kanbanTaskId,
        taskId: session.taskId ?? parentSub?.taskId,
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
          kanbanTaskId: sub.kanbanTaskId,
          taskId: sub.taskId,
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
        kanbanTaskId: sub.kanbanTaskId,
        taskId: sub.taskId,
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
    const systemEventViews: TelemetryEventView[] = slices
      .flatMap(({ id, slice }) =>
        slice.systemEvents.map((event) => ({
          ...event,
          sessionId: id,
          sessionTitle: sessionTitleById.get(id) ?? id,
        })),
      )
      .sort((a, b) => b.at - a.at)
    const telemetryEvents = systemEventViews
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
    const kanbanCards = collectKanbanCards(systemEventViews, agents)
    const goalState = collectGoalState(systemEventViews)
    const taskCards = collectTaskCards(systemEventViews, agents)

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
      missionSessionId: missionSession?.id ?? activeSessionId,
      missionSlice,
      objective,
      agents,
      busEvents,
      findings: busEvents.filter((event) => event.topic !== 'read'),
      readEvents: busEvents.filter((event) => event.topic === 'read'),
      telemetryEvents,
      kanbanCards,
      goalState,
      taskCards,
      fileChanges: allFileChanges,
      artifacts: allArtifacts,
      toolCalls: allToolCalls,
      cost,
      skillsCount: Object.keys(skills).length,
      memoriesCount: Object.keys(memories).length,
      rootUsage: missionSlice?.usage ?? usage,
      coordinationStrategy: missionSession?.coordinationStrategy ?? missionSlice?.coordinationStrategy ?? coordinationStrategy,
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
    coordinationStrategy,
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
  const attach = (id: string, mode: 'replace' | 'split' = 'replace') => {
    openSessionPane(id, mode)
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
    <div className="fixed inset-0 z-50 flex flex-col bg-[#080808]/90 backdrop-blur-[26px]">
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
          {TABS.map((item) => {
            const itemMeta = dashboardTabMeta(item, dashboard.coordinationStrategy)
            return (
              <button
                key={item.id}
                onClick={() => setTab(item.id)}
                className={`rounded-md px-3 py-2 text-left transition ${
                  tab === item.id
                    ? 'bg-accent/12 text-accent ring-1 ring-accent/25'
                    : 'text-fg-2 hover:bg-white/[0.045] hover:text-fg-0'
                }`}
              >
                <div className="font-mono text-[11px]">{itemMeta.label}</div>
                <div className="font-mono text-[8.5px] uppercase tracking-[0.12em] text-fg-3">
                  {itemMeta.hint}
                </div>
              </button>
            )
          })}
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
          {TABS.map((item) => {
            const itemMeta = dashboardTabMeta(item, dashboard.coordinationStrategy)
            return (
              <button
                key={item.id}
                onClick={() => setTab(item.id)}
                className={`rounded-md px-3 py-1.5 font-mono text-[10px] uppercase tracking-[0.08em] ${
                  tab === item.id
                    ? 'bg-accent/12 text-accent ring-1 ring-accent/25'
                    : 'bg-white/[0.03] text-fg-2 ring-hairline'
                }`}
              >
                {itemMeta.label}
              </button>
            )
          })}
        </nav>

        {tab === 'overview' && (
          <OverviewTab
            sessionId={dashboard.missionSessionId}
            objective={dashboard.objective}
            coordinationStrategy={dashboard.coordinationStrategy}
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
            kanbanCards={dashboard.kanbanCards}
            goalState={dashboard.goalState}
            taskCards={dashboard.taskCards}
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
            sessionId={dashboard.missionSessionId}
            coordinationStrategy={dashboard.coordinationStrategy}
            agents={dashboard.agents}
            busEvents={dashboard.busEvents}
            findings={dashboard.findings}
            kanbanCards={dashboard.kanbanCards}
            goalState={dashboard.goalState}
            taskCards={dashboard.taskCards}
            fileChanges={dashboard.fileChanges}
            telemetryEvents={dashboard.telemetryEvents}
            runningAgents={dashboard.agents.filter((agent) => agent.status === 'running').length}
            source={dashboard.missionSession}
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
  sessionId,
  objective,
  coordinationStrategy,
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
  kanbanCards,
  goalState,
  taskCards,
  skillsCount,
  memoriesCount,
  onTab,
  onAttach,
  onJumpTool,
  onCopyFinding,
}: {
  sessionId: string
  objective: string
  coordinationStrategy: CoordinationStrategy
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
  kanbanCards: KanbanCardView[]
  goalState: GoalStateView | null
  taskCards: TaskCardView[]
  skillsCount: number
  memoriesCount: number
  onTab: (tab: DashboardTab) => void
  onAttach: (id: string, mode?: 'replace' | 'split') => void
  onJumpTool: (id: string) => void
  onCopyFinding: (event: BusEventView) => void
}) {
  const compactions = telemetryEvents.filter((event) => event.subtype === 'compaction_complete').length
  const mediaPrunes = telemetryEvents.filter((event) => event.subtype === 'media_pruning').length
  if (coordinationStrategy === 'kanban') {
    return (
      <KanbanHealthView
        sessionId={sessionId}
        objective={objective}
        contextPct={contextPct}
        runningAgents={runningAgents}
        agents={agents}
        cards={kanbanCards}
        cost={cost}
        toolsCount={toolsCount}
        screenshotFrames={screenshotFrames}
        compactions={compactions}
        telemetryEvents={telemetryEvents}
        onAttach={onAttach}
        onTab={onTab}
      />
    )
  }
  if (coordinationStrategy === 'goal') {
    return (
      <GoalHealthView
        objective={objective}
        contextPct={contextPct}
        runningAgents={runningAgents}
        agents={agents}
        goalState={goalState}
        cost={cost}
        toolsCount={toolsCount}
        screenshotFrames={screenshotFrames}
        compactions={compactions}
        onAttach={onAttach}
        onTab={onTab}
      />
    )
  }
  if (coordinationStrategy === 'isolated') {
    return (
      <TaskHealthView
        objective={objective}
        contextPct={contextPct}
        runningAgents={runningAgents}
        agents={agents}
        tasks={taskCards}
        cost={cost}
        toolsCount={toolsCount}
        screenshotFrames={screenshotFrames}
        compactions={compactions}
        onAttach={onAttach}
        onTab={onTab}
      />
    )
  }
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
          <BriefStat label="strategy" value={coordinationLabel(coordinationStrategy)} />
          <BriefStat label="agents" value={String(agents.length)} />
          <BriefStat label="evidence" value={String(findings.length)} />
          <BriefStat label="cards" value={String(kanbanCards.length)} />
        </div>
        {kanbanCards.length > 0 && (
          <div className="mt-3 shrink-0 overflow-hidden rounded-lg bg-black/20 p-3 ring-hairline">
            <div className="label mb-2">board pulse</div>
            <div className="flex gap-2 overflow-x-auto pb-1">
              {kanbanCards.slice(0, 8).map((card) => (
                <div key={card.id} className="min-w-[150px] rounded-md bg-white/[0.035] px-2.5 py-2 ring-hairline">
                  <div className="flex items-center justify-between gap-2">
                    <span className="font-mono text-[10px] text-accent">{card.id}</span>
                    <span className={`font-mono text-[9px] uppercase ${kanbanStatusClass(card.status)}`}>
                      {card.status}
                    </span>
                  </div>
                  <div className="mt-1 truncate text-[11px] text-fg-0">{card.title}</div>
                  {card.assignee && (
                    <div className="mt-1 truncate font-mono text-[9px] text-fg-3">{card.assignee}</div>
                  )}
                </div>
              ))}
            </div>
          </div>
        )}
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
  sessionId,
  coordinationStrategy,
  agents,
  busEvents,
  findings,
  kanbanCards,
  goalState,
  taskCards,
  fileChanges,
  telemetryEvents,
  runningAgents,
  source,
  onAttach,
}: {
  sessionId: string
  coordinationStrategy: CoordinationStrategy
  agents: AgentView[]
  busEvents: BusEventView[]
  findings: BusEventView[]
  kanbanCards: KanbanCardView[]
  goalState: GoalStateView | null
  taskCards: TaskCardView[]
  fileChanges: FileChangeSet[]
  telemetryEvents: TelemetryEventView[]
  runningAgents: number
  source: SessionSnapshot | undefined
  onAttach: (id: string, mode?: 'replace' | 'split') => void
}) {
  if (coordinationStrategy === 'kanban') {
    return (
      <KanbanAgentsView
        sessionId={sessionId}
        cards={kanbanCards}
        agents={agents}
        source={source}
        telemetryEvents={telemetryEvents}
        runningAgents={runningAgents}
        onAttach={onAttach}
      />
    )
  }
  if (coordinationStrategy === 'goal') {
    return (
      <GoalLoopAgentsView
        goalState={goalState}
        agents={agents}
        busEvents={busEvents}
        findings={findings}
        onAttach={onAttach}
      />
    )
  }
  if (coordinationStrategy === 'isolated') {
    return (
      <TaskAgentsView
        tasks={taskCards}
        agents={agents}
        source={source}
        onAttach={onAttach}
      />
    )
  }

  return (
    <div className="grid min-h-0 flex-1 grid-cols-12 gap-3 overflow-hidden">
      <section className="col-span-12 flex min-h-0 flex-col overflow-hidden rounded-xl glass-strong p-4 xl:col-span-8">
        <PanelHeader label="hive mind" />
        <div className="mt-3 flex min-h-0 flex-1 flex-col">
          <HiveMindGraph
            agents={agents}
            events={busEvents}
            fileChanges={fileChanges}
            source={source}
            onAttach={onAttach}
          />
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
  )
}

function GoalHealthView({
  objective,
  contextPct,
  runningAgents,
  agents,
  goalState,
  cost,
  toolsCount,
  screenshotFrames,
  compactions,
  onAttach,
  onTab,
}: {
  objective: string
  contextPct: number
  runningAgents: number
  agents: AgentView[]
  goalState: GoalStateView | null
  cost: number
  toolsCount: number
  screenshotFrames: number
  compactions: number
  onAttach: (id: string, mode?: 'replace' | 'split') => void
  onTab: (tab: DashboardTab) => void
}) {
  const status = goalState?.status ?? 'idle'
  const turnsUsed = goalState?.turnsUsed ?? 0
  const maxTurns = goalState?.maxTurns ?? 20
  const turnPct = Math.min(100, Math.round((turnsUsed / Math.max(1, maxTurns)) * 100))
  const verdict = goalState?.lastVerdict
  const continuations = goalState?.events.filter((event) => event.subtype === 'goal_continue').length ?? 0
  const judges = goalState?.events.filter((event) => event.subtype === 'goal_judge').length ?? 0

  return (
    <div className="grid min-h-0 flex-1 grid-cols-12 grid-rows-[auto_minmax(0,1fr)] gap-3 overflow-hidden">
      <div className="col-span-12 grid grid-cols-2 gap-3 xl:grid-cols-8">
        <MetricCard label="goal" value={status} sub={`${turnsUsed}/${maxTurns} turns`} meter={turnPct} tone={goalStatusTone(status)} />
        <MetricCard label="judge calls" value={String(judges)} sub={`${continuations} continuations`} tone="accent" />
        <MetricCard label="active agents" value={String(runningAgents)} sub={`${agents.length} total`} tone="ok" />
        <MetricCard label="context" value={`${contextPct}%`} sub="current request" meter={contextPct} />
        <MetricCard label="tool calls" value={String(toolsCount)} sub="mission total" />
        <MetricCard label="screenshots" value={String(screenshotFrames)} sub="captured" tone="accent" />
        <MetricCard label="summaries" value={String(compactions)} sub="context history" tone={compactions > 0 ? 'ok' : 'neutral'} />
        <MetricCard label="spend" value={formatCost(cost)} sub="mission total" />
      </div>

      <section className="col-span-12 flex min-h-0 flex-col rounded-xl glass-strong p-4 xl:col-span-4">
        <PanelHeader label="active goal" action="profiles" onAction={() => onTab('profiles')} />
        <div className="mt-3 max-h-[220px] shrink-0 overflow-auto rounded-lg bg-white/[0.035] p-3 ring-hairline">
          <div className="label mb-2 text-fg-2">objective</div>
          <p className="selectable text-[13px] leading-[1.55] text-fg-0">
            {goalState?.goal || objective}
          </p>
        </div>
        <div className="mt-3 grid shrink-0 grid-cols-2 gap-2">
          <BriefStat label="strategy" value="goal" />
          <BriefStat label="status" value={status} tone={status === 'paused' ? 'danger' : undefined} />
          <BriefStat label="turns" value={`${turnsUsed}/${maxTurns}`} />
          <BriefStat label="confidence" value={verdict?.confidence !== undefined ? `${Math.round((verdict.confidence ?? 0) * 100)}%` : 'n/a'} />
        </div>
        <div className="mt-3 shrink-0 rounded-lg bg-black/20 p-3 ring-hairline">
          <div className="label mb-2">latest judge</div>
          <p className="text-[12px] leading-[1.55] text-fg-1">
            {verdict?.reason || 'No judge verdict yet. The first assistant turn will arm the loop.'}
          </p>
        </div>
        <div className="mt-3 min-h-0 flex-1 overflow-auto rounded-lg bg-black/20 p-3 ring-hairline">
          <div className="label mb-3">agent roster</div>
          <div className="space-y-2">
            {agents.slice(0, 8).map((agent) => (
              <CompactAgentRow key={agent.session.id} agent={agent} onAttach={onAttach} />
            ))}
            {agents.length === 0 && (
              <EmptyState title="Same-session loop" body="Goal mode keeps iterating in the parent session. Delegated agents appear here if the loop spawns them." />
            )}
          </div>
        </div>
      </section>

      <section className="col-span-12 flex min-h-0 flex-col overflow-hidden rounded-xl glass-strong p-4 xl:col-span-8">
        <PanelHeader label="judge timeline" action="agents" onAction={() => onTab('swarm')} />
        <GoalTimeline goalState={goalState} />
      </section>
    </div>
  )
}

function TaskHealthView({
  objective,
  contextPct,
  runningAgents,
  agents,
  tasks,
  cost,
  toolsCount,
  screenshotFrames,
  compactions,
  onAttach,
  onTab,
}: {
  objective: string
  contextPct: number
  runningAgents: number
  agents: AgentView[]
  tasks: TaskCardView[]
  cost: number
  toolsCount: number
  screenshotFrames: number
  compactions: number
  onAttach: (id: string, mode?: 'replace' | 'split') => void
  onTab: (tab: DashboardTab) => void
}) {
  const done = tasks.filter((task) => task.status === 'done').length
  const blocked = tasks.filter((task) => task.status === 'blocked').length
  const active = tasks.filter((task) => task.status === 'active').length
  const progress = taskProgress(tasks)
  return (
    <div className="grid min-h-0 flex-1 grid-cols-12 grid-rows-[auto_minmax(0,1fr)] gap-3 overflow-hidden">
      <div className="col-span-12 grid grid-cols-2 gap-3 xl:grid-cols-8">
        <MetricCard label="tasks" value={`${progress}%`} sub={`${done}/${tasks.length || 0} done`} meter={progress} tone={progress === 100 ? 'ok' : 'accent'} />
        <MetricCard label="active" value={String(active)} sub={`${blocked} blocked`} tone={blocked ? 'warn' : 'ok'} />
        <MetricCard label="active agents" value={String(runningAgents)} sub={`${agents.length} total`} tone="ok" />
        <MetricCard label="context" value={`${contextPct}%`} sub="current request" meter={contextPct} />
        <MetricCard label="tool calls" value={String(toolsCount)} sub="mission total" />
        <MetricCard label="screenshots" value={String(screenshotFrames)} sub="captured" tone="accent" />
        <MetricCard label="summaries" value={String(compactions)} sub="context history" tone={compactions > 0 ? 'ok' : 'neutral'} />
        <MetricCard label="spend" value={formatCost(cost)} sub="mission total" />
      </div>

      <section className="col-span-12 flex min-h-0 flex-col rounded-xl glass-strong p-4 xl:col-span-3">
        <PanelHeader label="solo mission" action="profiles" onAction={() => onTab('profiles')} />
        <div className="mt-3 max-h-[170px] shrink-0 overflow-auto rounded-lg bg-white/[0.035] p-3 ring-hairline">
          <div className="label mb-2 text-fg-2">current objective</div>
          <p className="selectable text-[13px] leading-[1.55] text-fg-0">{objective}</p>
        </div>
        <div className="mt-3 grid shrink-0 grid-cols-2 gap-2">
          <BriefStat label="strategy" value="tasks" />
          <BriefStat label="progress" value={`${progress}%`} />
          <BriefStat label="active" value={String(active)} />
          <BriefStat label="blocked" value={String(blocked)} tone={blocked ? 'danger' : undefined} />
        </div>
        <div className="mt-3 min-h-0 flex-1 overflow-auto rounded-lg bg-black/20 p-3 ring-hairline">
          <div className="label mb-3">agent allocation</div>
          <TaskAssignmentList agents={agents} tasks={tasks} onAttach={onAttach} />
        </div>
      </section>

      <section className="col-span-12 flex min-h-0 flex-col overflow-hidden rounded-xl glass-strong p-4 xl:col-span-9">
        <PanelHeader label="task ledger" action="agents" onAction={() => onTab('swarm')} />
        <TaskBoard tasks={tasks} agents={agents} onAttach={onAttach} compact />
      </section>
    </div>
  )
}

function TaskAgentsView({
  tasks,
  agents,
  source,
  onAttach,
}: {
  tasks: TaskCardView[]
  agents: AgentView[]
  source: SessionSnapshot | undefined
  onAttach: (id: string, mode?: 'replace' | 'split') => void
}) {
  const [selectedTaskId, setSelectedTaskId] = useState(tasks[0]?.id ?? '')
  useEffect(() => {
    if (!selectedTaskId && tasks[0]) setSelectedTaskId(tasks[0].id)
    if (selectedTaskId && !tasks.some((task) => task.id === selectedTaskId)) {
      setSelectedTaskId(tasks[0]?.id ?? '')
    }
  }, [selectedTaskId, tasks])
  const selectedTask = tasks.find((task) => task.id === selectedTaskId) ?? tasks[0]
  const selectedAgents = selectedTask ? agents.filter((agent) => agent.taskId === selectedTask.id) : []
  return (
    <div className="grid min-h-0 flex-1 grid-cols-12 gap-3 overflow-hidden">
      <section className="col-span-12 flex min-h-0 flex-col overflow-hidden rounded-xl glass-strong p-4 xl:col-span-9">
        <div className="mb-3 flex shrink-0 items-start justify-between gap-3">
          <div>
            <PanelHeader label="task workbench" />
            <div className="mt-1 font-mono text-[10px] text-fg-3">
              {source?.title ?? 'Session'} · {tasks.length} tasks · {agents.filter((agent) => agent.taskId).length} assigned agents
            </div>
          </div>
          <div className="grid grid-cols-3 gap-2">
            <MiniMetric label="progress" value={`${taskProgress(tasks)}%`} />
            <MiniMetric label="active" value={String(tasks.filter((task) => task.status === 'active').length)} />
            <MiniMetric label="blocked" value={String(tasks.filter((task) => task.status === 'blocked').length)} />
          </div>
        </div>
        <TaskBoard tasks={tasks} agents={agents} selectedTaskId={selectedTask?.id} onSelect={setSelectedTaskId} onAttach={onAttach} />
      </section>
      <section className="col-span-12 grid min-h-0 grid-rows-[minmax(0,1.1fr)_minmax(0,0.9fr)] gap-3 xl:col-span-3">
        <TaskDetailPanel task={selectedTask} agents={selectedAgents} onAttach={onAttach} />
        <div className="flex min-h-0 flex-col overflow-hidden rounded-xl glass-strong p-4">
          <PanelHeader label="agent lanes" />
          <div className="mt-3 min-h-0 flex-1 overflow-auto pr-1">
            <AgentLaneList agents={agents} onAttach={onAttach} />
          </div>
        </div>
      </section>
    </div>
  )
}

const TASK_COLUMNS = [
  { id: 'todo', label: 'queued', hint: 'awaiting triage' },
  { id: 'active', label: 'in progress', hint: 'owned by agents' },
  { id: 'blocked', label: 'blocked', hint: 'needs input' },
  { id: 'done', label: 'complete', hint: 'handoff sealed' },
  { id: 'cancelled', label: 'stopped', hint: 'no longer active' },
] as const

function TaskBoard({
  tasks,
  agents,
  selectedTaskId,
  compact = false,
  onSelect,
  onAttach,
}: {
  tasks: TaskCardView[]
  agents: AgentView[]
  selectedTaskId?: string
  compact?: boolean
  onSelect?: (id: string) => void
  onAttach: (id: string, mode?: 'replace' | 'split') => void
}) {
  return (
    <div className={`min-h-0 flex-1 overflow-auto ${compact ? 'mt-3' : ''}`}>
      <div className="grid min-h-full min-w-[920px] grid-cols-5 gap-3">
        {TASK_COLUMNS.map((column) => {
          const columnTasks = tasks.filter((task) => task.status === column.id)
          return (
            <div key={column.id} className="flex min-h-0 flex-col rounded-xl bg-black/22 p-3 ring-hairline">
              <div className="mb-3 flex shrink-0 items-start justify-between gap-2">
                <div>
                  <div className={`font-mono text-[11px] uppercase tracking-[0.16em] ${taskStatusClass(column.id)}`}>
                    {column.label}
                  </div>
                  <div className="mt-1 font-mono text-[9px] text-fg-3">{column.hint}</div>
                </div>
                <span className="rounded-md bg-white/[0.04] px-2 py-1 font-mono text-[10px] text-fg-2 ring-hairline">
                  {columnTasks.length}
                </span>
              </div>
              <div className="min-h-0 flex-1 space-y-3 overflow-auto pr-1">
                {columnTasks.map((task) => (
                  <TaskCard
                    key={task.id}
                    task={task}
                    agents={agents.filter((agent) => agent.taskId === task.id)}
                    selected={task.id === selectedTaskId}
                    onClick={() => onSelect?.(task.id)}
                    onAttach={onAttach}
                  />
                ))}
                {columnTasks.length === 0 && (
                  <div className="rounded-lg border border-dashed border-white/10 px-3 py-8 text-center font-mono text-[10px] text-fg-3">
                    empty
                  </div>
                )}
              </div>
            </div>
          )
        })}
      </div>
    </div>
  )
}

function TaskCard({
  task,
  agents,
  selected,
  onClick,
  onAttach,
}: {
  task: TaskCardView
  agents: AgentView[]
  selected?: boolean
  onClick?: () => void
  onAttach: (id: string, mode?: 'replace' | 'split') => void
}) {
  const progress = Math.max(0, Math.min(task.progress ?? statusProgress(task.status), 100))
  return (
    <button
      type="button"
      onClick={onClick}
      className={`group w-full rounded-lg p-0 text-left transition ${selected ? 'ring-1 ring-accent/55' : 'hover:ring-1 hover:ring-white/18'}`}
    >
      <div className={`task-card-skin task-card-${task.status} rounded-lg p-3`}>
        <div className="flex items-start justify-between gap-2">
          <div className="min-w-0">
            <div className="font-mono text-[10px] text-accent">{task.id}</div>
            <div className="mt-1 line-clamp-2 text-[12px] leading-[1.35] text-fg-0">{task.title}</div>
          </div>
          <span className={`font-mono text-[9px] uppercase ${taskStatusClass(task.status)}`}>
            {task.status}
          </span>
        </div>
        <div className="mt-3 h-1 overflow-hidden rounded-full bg-white/10">
          <div className="h-full rounded-full bg-accent/70" style={{ width: `${progress}%` }} />
        </div>
        {task.body && (
          <p className="mt-2 line-clamp-3 text-[10.5px] leading-[1.45] text-fg-2">{stripMarkdown(task.body)}</p>
        )}
        <div className="mt-3 flex items-center justify-between gap-2">
          <span className="truncate font-mono text-[9px] text-fg-3">
            {task.assignee || agents[0]?.session.title || 'unassigned'}
          </span>
          {agents[0] && (
            <span
              role="button"
              tabIndex={0}
              onClick={(event) => {
                event.stopPropagation()
                onAttach(agents[0].session.id, 'replace')
              }}
              onKeyDown={(event) => {
                if (event.key === 'Enter' || event.key === ' ') {
                  event.stopPropagation()
                  onAttach(agents[0].session.id, 'replace')
                }
              }}
              className="rounded bg-white/[0.055] px-2 py-1 font-mono text-[9px] uppercase text-accent ring-hairline hover:bg-accent/10"
            >
              attach
            </span>
          )}
        </div>
      </div>
    </button>
  )
}

function TaskDetailPanel({
  task,
  agents,
  onAttach,
}: {
  task?: TaskCardView
  agents: AgentView[]
  onAttach: (id: string, mode?: 'replace' | 'split') => void
}) {
  if (!task) {
    return (
      <section className="flex min-h-0 flex-col overflow-hidden rounded-xl glass-strong p-4">
        <PanelHeader label="task detail" />
        <EmptyState title="No task selected" body="Click a task card to inspect its owner, history, artifacts, and result." />
      </section>
    )
  }
  return (
    <section className="flex min-h-0 flex-col overflow-hidden rounded-xl glass-strong p-4">
      <PanelHeader label="task detail" />
      <div className="mt-3 min-h-0 flex-1 overflow-auto pr-1">
        <div className="flex items-start justify-between gap-3">
          <div>
            <div className="font-mono text-[12px] text-accent">{task.id}</div>
            <div className="mt-1 text-[16px] leading-[1.25] text-fg-0">{task.title}</div>
          </div>
          <span className={`rounded-md bg-white/[0.04] px-2 py-1 font-mono text-[9px] uppercase ring-hairline ${taskStatusClass(task.status)}`}>
            {task.status}
          </span>
        </div>
        <div className="mt-4 grid grid-cols-[94px_1fr] gap-x-3 gap-y-2 font-mono text-[10px]">
          <span className="label">agent</span>
          <span className="text-fg-1">{agents[0]?.session.title ?? task.assignee ?? 'unassigned'}</span>
          <span className="label">priority</span>
          <span className="text-fg-1">P{task.priority ?? 2}</span>
          <span className="label">progress</span>
          <span className="text-fg-1">{task.progress ?? statusProgress(task.status)}%</span>
        </div>
        <div className="mt-4 h-1.5 overflow-hidden rounded-full bg-white/10">
          <div className="h-full rounded-full bg-accent/70" style={{ width: `${task.progress ?? statusProgress(task.status)}%` }} />
        </div>
        <DetailBlock title="description" body={task.body || 'No description recorded.'} />
        {task.summary && <DetailBlock title="summary" body={task.summary} />}
        {task.result && <DetailBlock title="result" body={task.result} />}
        {task.artifacts && task.artifacts.length > 0 && (
          <div className="mt-4 rounded-lg bg-black/25 p-3 ring-hairline">
            <div className="label mb-2">artifacts</div>
            <div className="space-y-1">
              {task.artifacts.map((artifact) => (
                <div key={artifact} className="truncate font-mono text-[10px] text-fg-1">{artifact}</div>
              ))}
            </div>
          </div>
        )}
        {agents.length > 0 && (
          <div className="mt-4 space-y-2">
            <div className="label">assigned agents</div>
            {agents.map((agent) => (
              <CompactAgentRow key={agent.session.id} agent={agent} onAttach={onAttach} />
            ))}
          </div>
        )}
        {task.events && task.events.length > 0 && (
          <div className="mt-4 space-y-2">
            <div className="label">history</div>
            {task.events.slice(-6).reverse().map((event, index) => (
              <div key={`${event.timestamp}-${index}`} className="rounded-md bg-white/[0.03] px-2.5 py-2 ring-hairline">
                <div className="flex justify-between gap-2">
                  <span className="font-mono text-[10px] text-fg-1">{event.kind ?? 'update'}</span>
                  {event.timestamp && <span className="font-mono text-[9px] text-fg-3">{relativeTime(event.timestamp)}</span>}
                </div>
                <p className="mt-1 text-[10.5px] leading-[1.45] text-fg-2">{event.message}</p>
              </div>
            ))}
          </div>
        )}
      </div>
    </section>
  )
}

function DetailBlock({ title, body }: { title: string; body: string }) {
  return (
    <div className="mt-4 rounded-lg bg-black/25 p-3 ring-hairline">
      <div className="label mb-2">{title}</div>
      <p className="selectable whitespace-pre-wrap text-[11px] leading-[1.55] text-fg-1">{body}</p>
    </div>
  )
}

function TaskAssignmentList({
  agents,
  tasks,
  onAttach,
}: {
  agents: AgentView[]
  tasks: TaskCardView[]
  onAttach: (id: string, mode?: 'replace' | 'split') => void
}) {
  const assigned = agents.filter((agent) => agent.taskId)
  if (assigned.length === 0) {
    return <EmptyState title="No assigned agents" body="Tasks and task-bound agents will appear here as the parent decomposes work." />
  }
  const taskById = new Map(tasks.map((task) => [task.id, task]))
  return (
    <div className="space-y-2">
      {assigned.slice(0, 8).map((agent) => {
        const task = agent.taskId ? taskById.get(agent.taskId) : undefined
        return (
          <button
            key={agent.session.id}
            type="button"
            onClick={() => onAttach(agent.session.id, 'replace')}
            className="w-full rounded-lg bg-white/[0.03] p-2 text-left ring-hairline hover:bg-white/[0.055]"
          >
            <div className="flex items-center justify-between gap-2">
              <span className="truncate text-[11px] text-fg-0">{agent.session.title}</span>
              <span className="font-mono text-[9px] uppercase text-accent">{agent.status}</span>
            </div>
            <div className="mt-1 truncate font-mono text-[9px] text-fg-3">
              {agent.taskId} · {task?.title ?? 'task'}
            </div>
          </button>
        )
      })}
    </div>
  )
}

function GoalLoopAgentsView({
  goalState,
  agents,
  busEvents,
  findings,
  onAttach,
}: {
  goalState: GoalStateView | null
  agents: AgentView[]
  busEvents: BusEventView[]
  findings: BusEventView[]
  onAttach: (id: string, mode?: 'replace' | 'split') => void
}) {
  return (
    <div className="grid min-h-0 flex-1 grid-cols-12 gap-3 overflow-hidden">
      <section className="col-span-12 flex min-h-0 flex-col overflow-hidden rounded-xl glass-strong p-4 xl:col-span-8">
        <PanelHeader label="goal loop" />
        <div className="mt-3 grid shrink-0 grid-cols-3 gap-2">
          <BriefStat label="status" value={goalState?.status ?? 'idle'} />
          <BriefStat label="turn budget" value={`${goalState?.turnsUsed ?? 0}/${goalState?.maxTurns ?? 20}`} />
          <BriefStat label="agents" value={String(agents.length)} />
        </div>
        <div className="mt-3 min-h-0 flex-1 overflow-hidden rounded-lg bg-black/25 ring-hairline">
          <GoalTimeline goalState={goalState} dense />
        </div>
      </section>
      <section className="col-span-12 grid min-h-0 grid-rows-[minmax(0,1fr)_220px] gap-3 xl:col-span-4">
        <div className="flex min-h-0 flex-col overflow-hidden rounded-xl glass-strong p-4">
          <PanelHeader label="delegated lanes" />
          <div className="mt-3 min-h-0 flex-1 overflow-auto pr-1">
            <AgentLaneList agents={agents} onAttach={onAttach} />
          </div>
        </div>
        <div className="flex min-h-0 flex-col overflow-hidden rounded-xl glass-strong p-4">
          <PanelHeader label="side channel" />
          {busEvents.length > 0 ? (
            <BusFeed events={busEvents} />
          ) : (
            <div className="mt-3 min-h-0 flex-1 space-y-2 overflow-auto pr-1">
              {findings.slice(0, 3).map((event) => (
                <FindingCard
                  key={`${event.sessionId}-${event.index}-${event.timestamp}`}
                  event={event}
                  compact
                />
              ))}
              {findings.length === 0 && (
                <EmptyState title="No side-channel traffic" body="Goal mode is same-session by default. Bus messages appear only if delegated agents publish them." />
              )}
            </div>
          )}
        </div>
      </section>
    </div>
  )
}

function GoalTimeline({ goalState, dense = false }: { goalState: GoalStateView | null; dense?: boolean }) {
  const events = goalState?.events ?? []
  return (
    <div className={`min-h-0 flex-1 overflow-auto ${dense ? 'p-3' : 'mt-3 pr-1'}`}>
      {events.length === 0 && (
        <EmptyState
          title="No goal loop events yet"
          body="Start a goal session or run /goal <objective>. Judge decisions and continuations will appear here."
        />
      )}
      <div className="space-y-2">
        {events.map((event) => {
          const state = event.details?.goalState as Record<string, unknown> | undefined
          const verdict = (event.details?.verdict as Record<string, unknown> | undefined) ?? (state?.lastVerdict as Record<string, unknown> | undefined)
          const verdictReason = typeof verdict?.reason === 'string' ? verdict.reason : ''
          const continuationPrompt = typeof event.details?.continuationPrompt === 'string' ? event.details.continuationPrompt : ''
          return (
            <div
              key={event.id}
              className={`rounded-lg bg-white/[0.035] p-3 ring-hairline ${
                event.subtype === 'goal_done'
                  ? 'ring-ok/30'
                  : event.subtype === 'goal_paused'
                    ? 'ring-warn/30'
                    : event.subtype === 'goal_continue'
                      ? 'ring-accent/25'
                      : ''
              }`}
            >
              <div className="flex items-center justify-between gap-3">
                <div className="flex items-center gap-2">
                  <span className={`h-2 w-2 rounded-full ${goalEventDot(event.subtype)}`} />
                  <span className="font-mono text-[11px] uppercase tracking-[0.08em] text-fg-0">
                    {event.subtype.replace(/^goal_/, '').replace(/_/g, ' ')}
                  </span>
                </div>
                <span className="font-mono text-[9px] text-fg-3">{relativeTime(event.at)}</span>
              </div>
              <p className="mt-2 text-[12px] leading-[1.5] text-fg-1">{event.message}</p>
              {verdictReason && (
                <div className="mt-2 rounded-md bg-black/25 px-2.5 py-2 text-[11px] leading-[1.45] text-fg-2 ring-hairline">
                  {verdictReason}
                </div>
              )}
              {event.subtype === 'goal_continue' && continuationPrompt && (
                <div className="mt-2 max-h-[90px] overflow-auto rounded-md bg-black/25 px-2.5 py-2 font-mono text-[10px] leading-[1.45] text-fg-3 ring-hairline">
                  {continuationPrompt}
                </div>
              )}
            </div>
          )
        })}
      </div>
    </div>
  )
}

const KANBAN_COLUMNS = [
  { id: 'triage', label: 'triage', hint: 'specifying / awaiting parents' },
  { id: 'ready', label: 'ready', hint: 'unblocked work' },
  { id: 'running', label: 'working', hint: 'owned by agents' },
  { id: 'done_unverified', label: 'review', hint: 'awaiting verifier' },
  { id: 'blocked', label: 'blocked', hint: 'needs input' },
  { id: 'done', label: 'done', hint: 'sealed outputs' },
  { id: 'crashed', label: 'crashed', hint: 'worker exited unclean' },
  { id: 'timed_out', label: 'timed out', hint: 'exceeded budget' },
  { id: 'failed', label: 'failed', hint: 'circuit broken' },
  { id: 'cancelled', label: 'cancelled', hint: 'stopped by operator' },
] as const

function KanbanHealthView({
  sessionId,
  objective,
  contextPct,
  runningAgents,
  agents,
  cards,
  cost,
  toolsCount,
  screenshotFrames,
  compactions,
  telemetryEvents,
  onAttach,
  onTab,
}: {
  sessionId: string
  objective: string
  contextPct: number
  runningAgents: number
  agents: AgentView[]
  cards: KanbanCardView[]
  cost: number
  toolsCount: number
  screenshotFrames: number
  compactions: number
  telemetryEvents: TelemetryEventView[]
  onAttach: (id: string, mode?: 'replace' | 'split') => void
  onTab: (tab: DashboardTab) => void
}) {
  const progress = kanbanProgress(cards)
  const blocked = cards.filter((card) => card.status === 'blocked').length
  const done = cards.filter((card) => card.status === 'done').length
  return (
    <div className="grid min-h-0 flex-1 grid-cols-12 grid-rows-[auto_auto_auto_minmax(0,1fr)] gap-3 overflow-hidden">
      <div className="col-span-12">
        <KanbanAutopilotStrip
          sessionId={sessionId}
          systemEvents={telemetryEvents}
          runningAgents={runningAgents}
        />
      </div>
      <div className="col-span-12">
        <MissionCoverCard cards={cards} restartCount={kanbanRestartCount(telemetryEvents)} />
      </div>
      <div className="col-span-12 grid grid-cols-2 gap-3 xl:grid-cols-8">
        <MetricCard label="board" value={`${progress}%`} sub={`${done}/${cards.length || 0} done`} meter={progress} tone={progress === 100 ? 'ok' : 'accent'} />
        <MetricCard label="cards" value={String(cards.length)} sub={`${blocked} blocked`} tone={blocked ? 'warn' : 'neutral'} />
        <MetricCard label="active agents" value={String(runningAgents)} sub={`${agents.length} total`} tone="ok" />
        <MetricCard label="context" value={`${contextPct}%`} sub="current request" meter={contextPct} />
        <MetricCard label="tool calls" value={String(toolsCount)} sub="mission total" />
        <MetricCard label="screenshots" value={String(screenshotFrames)} sub="captured" tone="accent" />
        <MetricCard label="summaries" value={String(compactions)} sub="context history" tone={compactions > 0 ? 'ok' : 'neutral'} />
        <MetricCard label="spend" value={formatCost(cost)} sub="mission total" />
      </div>

      <section className="col-span-12 flex min-h-0 flex-col rounded-xl glass-strong p-4 xl:col-span-3">
        <PanelHeader label="mission brief" action="profiles" onAction={() => onTab('profiles')} />
        <div className="mt-3 max-h-[140px] shrink-0 overflow-auto rounded-lg bg-white/[0.035] p-3 ring-hairline">
          <div className="label mb-2 text-fg-2">current objective</div>
          <p className="selectable text-[13px] leading-[1.55] text-fg-0">{objective}</p>
        </div>
        <div className="mt-3 grid shrink-0 grid-cols-2 gap-2">
          <BriefStat label="strategy" value="board" />
          <BriefStat label="progress" value={`${progress}%`} />
          <BriefStat label="agents" value={String(agents.length)} />
          <BriefStat label="blocked" value={String(blocked)} tone={blocked ? 'danger' : undefined} />
        </div>
        <div className="mt-3 min-h-0 flex-1 space-y-3 overflow-hidden">
          <KanbanWatchList cards={cards} />
          <KanbanDispatchTicker systemEvents={telemetryEvents} />
        </div>
      </section>

      <section className="col-span-12 flex min-h-0 flex-col overflow-hidden rounded-xl glass-strong p-4 xl:col-span-9">
        <PanelHeader label="kanban board" action="agents" onAction={() => onTab('swarm')} />
        <KanbanBoard cards={cards} agents={agents} onAttach={onAttach} compact />
      </section>
    </div>
  )
}

function KanbanAgentsView({
  sessionId,
  cards,
  agents,
  source,
  telemetryEvents,
  runningAgents,
  onAttach,
}: {
  sessionId: string
  cards: KanbanCardView[]
  agents: AgentView[]
  source: SessionSnapshot | undefined
  telemetryEvents: TelemetryEventView[]
  runningAgents: number
  onAttach: (id: string, mode?: 'replace' | 'split') => void
}) {
  const [selectedCardId, setSelectedCardId] = useState<string>('')
  const assigned = agents.filter((agent) => agent.kanbanTaskId)
  const unassigned = agents.filter((agent) => !agent.kanbanTaskId)
  const defaultCardId = useMemo(
    () =>
      cards.find((card) => card.status === 'running')?.id ??
      cards.find((card) => card.status === 'blocked')?.id ??
      cards[0]?.id ??
      '',
    [cards],
  )
  useEffect(() => {
    if (!cards.length) {
      if (selectedCardId) setSelectedCardId('')
      return
    }
    if (!selectedCardId || !cards.some((card) => card.id === selectedCardId)) {
      setSelectedCardId(defaultCardId)
    }
  }, [cards, defaultCardId, selectedCardId])
  const selectedCard = cards.find((card) => card.id === selectedCardId) ?? cards.find((card) => card.id === defaultCardId)
  const selectedIndex = selectedCard ? cards.findIndex((card) => card.id === selectedCard.id) : -1
  const selectedAgents = selectedCard ? agents.filter((agent) => agent.kanbanTaskId === selectedCard.id) : []
  const selectRelativeCard = (offset: number) => {
    if (!cards.length || selectedIndex < 0) return
    const next = cards[(selectedIndex + offset + cards.length) % cards.length]
    if (next) setSelectedCardId(next.id)
  }
  // Keyboard nav: arrows / j-k step through cards; Esc clears selection.
  // Ignored when the user is typing in an input or textarea so the chat
  // composer doesn't fight for the same keys.
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      const target = event.target as HTMLElement | null
      if (target && (target.tagName === 'INPUT' || target.tagName === 'TEXTAREA' || target.isContentEditable)) {
        return
      }
      if (event.metaKey || event.ctrlKey || event.altKey) return
      switch (event.key) {
        case 'ArrowDown':
        case 'j':
          event.preventDefault()
          selectRelativeCard(1)
          return
        case 'ArrowUp':
        case 'k':
          event.preventDefault()
          selectRelativeCard(-1)
          return
        case 'Escape':
          if (selectedCardId) {
            event.preventDefault()
            setSelectedCardId('')
          }
          return
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [cards.length, selectedIndex, selectedCardId])
  return (
    <div className="grid min-h-0 flex-1 grid-cols-12 grid-rows-[auto_auto_minmax(0,1fr)] gap-3 overflow-hidden">
      <div className="col-span-12">
        <KanbanAutopilotStrip
          sessionId={sessionId}
          systemEvents={telemetryEvents}
          runningAgents={runningAgents}
        />
      </div>
      <div className="col-span-12">
        <MissionCoverCard cards={cards} restartCount={kanbanRestartCount(telemetryEvents)} />
      </div>
      <section className="col-span-12 flex min-h-0 flex-col overflow-hidden rounded-xl glass-strong p-4 2xl:col-span-9">
        <div className="flex shrink-0 flex-wrap items-center gap-3 hairline-b pb-3">
          <div className="min-w-0 flex-1">
            <PanelHeader label="kanban board" />
            <div className="mt-2 truncate font-mono text-[10px] text-fg-3">
              {cleanTitle(source?.title || source?.task || 'Mission')} · {cards.length} cards · {assigned.length} assigned agents
            </div>
          </div>
          <div className="grid grid-cols-3 gap-2">
            <BriefStat label="progress" value={`${kanbanProgress(cards)}%`} />
            <BriefStat label="running" value={String(cards.filter((card) => card.status === 'running').length)} />
            <BriefStat label="blocked" value={String(cards.filter((card) => card.status === 'blocked').length)} tone={cards.some((card) => card.status === 'blocked') ? 'danger' : undefined} />
          </div>
        </div>
        <KanbanBoard
          cards={cards}
          agents={agents}
          selectedCardId={selectedCard?.id}
          onSelectCard={setSelectedCardId}
          onAttach={onAttach}
        />
      </section>
      <section className="col-span-12 grid min-h-0 grid-rows-[minmax(360px,1.1fr)_minmax(160px,0.48fr)_minmax(140px,0.36fr)] gap-3 2xl:col-span-3">
        <div className="flex min-h-0 flex-col overflow-hidden rounded-xl glass-strong p-4">
          <KanbanTaskDetailPanel
            card={selectedCard}
            agents={selectedAgents}
            onAttach={onAttach}
            onPrev={() => selectRelativeCard(-1)}
            onNext={() => selectRelativeCard(1)}
          />
        </div>
        <div className="flex min-h-0 flex-col overflow-hidden rounded-xl glass-strong p-4">
          <PanelHeader label="agent lanes" />
          <div className="mt-3 min-h-0 flex-1 overflow-auto pr-1">
            <KanbanAssignmentList agents={agents} cards={cards} onAttach={onAttach} />
            {unassigned.length > 0 && (
              <div className="mt-4">
                <div className="label mb-2">unassigned</div>
                <AgentLaneList agents={unassigned} onAttach={onAttach} />
              </div>
            )}
          </div>
        </div>
        <div className="flex min-h-0 flex-col overflow-hidden rounded-xl glass-strong p-4">
          <PanelHeader label="board log" />
          <KanbanEventFeed cards={cards} />
        </div>
      </section>
    </div>
  )
}

function KanbanBoard({
  cards,
  agents,
  selectedCardId,
  onSelectCard,
  onAttach,
  compact = false,
}: {
  cards: KanbanCardView[]
  agents: AgentView[]
  selectedCardId?: string
  onSelectCard?: (id: string) => void
  onAttach: (id: string, mode?: 'replace' | 'split') => void
  compact?: boolean
}) {
  const cardsByStatus = new Map<string, KanbanCardView[]>()
  for (const column of KANBAN_COLUMNS) cardsByStatus.set(column.id, [])
  for (const card of cards) {
    // Legacy `todo` events from older bridge versions get rebucketed into
    // `triage`; anything else outside the known column set also falls
    // there so the card stays visible rather than getting silently dropped.
    const status = KANBAN_COLUMNS.some((column) => column.id === card.status)
      ? card.status
      : 'triage'
    cardsByStatus.get(status)?.push(card)
  }
  for (const columnCards of cardsByStatus.values()) {
    columnCards.sort((a, b) =>
      (a.priority ?? 2) - (b.priority ?? 2)
      || (b.updatedAt ?? 0) - (a.updatedAt ?? 0),
    )
  }
  const COMPACT_HIDDEN_COLUMNS = new Set(['cancelled', 'crashed', 'timed_out', 'failed'])
  const boardColumns = KANBAN_COLUMNS.filter((column) => !compact || !COMPACT_HIDDEN_COLUMNS.has(column.id))

  if (cards.length === 0) {
    return (
      <div className="mt-3 flex min-h-0 flex-1 items-center justify-center rounded-xl bg-white/[0.025] p-8 ring-hairline">
        <EmptyState title="No board cards yet" body="Kanban card activity will appear here as soon as the session creates board work." />
      </div>
    )
  }

  return (
    <div className="mt-3 flex min-h-0 flex-1 flex-col overflow-hidden rounded-xl bg-[radial-gradient(circle_at_50%_0%,rgba(127,184,232,0.08),transparent_36%),linear-gradient(rgba(255,255,255,0.025)_1px,transparent_1px),linear-gradient(90deg,rgba(255,255,255,0.02)_1px,transparent_1px)] bg-[length:auto,44px_44px,44px_44px] ring-1 ring-white/[0.055]">
      <div className="min-h-0 flex-1 overflow-x-auto overflow-y-hidden p-3">
        <div className="flex h-full min-w-max gap-4">
          {boardColumns.map((column) => {
            const columnCards = cardsByStatus.get(column.id) ?? []
            if (columnCards.length === 0) {
              // Quiet-lane stub: 36px-wide rotated label that keeps the
              // state machine visible without a 380px empty column. Doc
              // calls for 32px; bumped to 36 so the rotated label fits.
              return (
                <section
                  key={column.id}
                  className={`flex min-h-0 w-9 shrink-0 flex-col items-center justify-between overflow-hidden rounded-xl bg-white/[0.018] py-2 ${kanbanColumnClass(column.id)}`}
                  title={`${column.label} — ${column.hint}`}
                >
                  <span className="font-mono text-[10px] text-fg-3">0</span>
                  <span
                    className={`whitespace-nowrap font-mono text-[10px] uppercase tracking-[0.10em] ${kanbanStatusClass(column.id)}`}
                    style={{ writingMode: 'vertical-rl', transform: 'rotate(180deg)' }}
                  >
                    {column.label}
                  </span>
                  <span aria-hidden className="h-3" />
                </section>
              )
            }
            return (
              <section
                key={column.id}
                className={`flex min-h-0 shrink-0 flex-col overflow-hidden rounded-xl ${compact ? 'w-[320px]' : 'w-[380px]'} ${kanbanColumnClass(column.id)}`}
              >
                <div className="shrink-0 rounded-t-xl bg-black/18 px-3 py-3 shadow-[0_10px_24px_rgba(0,0,0,0.18)] ring-1 ring-white/[0.035] backdrop-blur-md">
                  <div className="flex items-start justify-between gap-2">
                    <div>
                      <div className={`font-mono text-[12px] uppercase tracking-[0.08em] ${kanbanStatusClass(column.id)}`}>
                        {column.label}
                      </div>
                      <div className="mt-0.5 font-mono text-[9px] text-fg-3">{column.hint}</div>
                    </div>
                    <span className="rounded-[3px] bg-black/35 px-2 py-1 font-mono text-[10px] text-fg-1 ring-1 ring-white/[0.09]">
                      {columnCards.length}
                    </span>
                  </div>
                </div>
                <div className="min-h-0 flex-1 space-y-3 overflow-y-auto overflow-x-hidden px-3 py-3 pr-2">
                  {columnCards.map((card) => (
                    <KanbanCard
                      key={card.id}
                      card={card}
                      agents={agents.filter((agent) => agent.kanbanTaskId === card.id)}
                      selected={card.id === selectedCardId}
                      onSelect={onSelectCard}
                      onAttach={onAttach}
                      compact={compact}
                    />
                  ))}
                </div>
              </section>
            )
          })}
        </div>
      </div>
    </div>
  )
}

function KanbanTaskDetailPanel({
  card,
  agents,
  onAttach,
  onPrev,
  onNext,
}: {
  card: KanbanCardView | undefined
  agents: AgentView[]
  onAttach: (id: string, mode?: 'replace' | 'split') => void
  onPrev: () => void
  onNext: () => void
}) {
  if (!card) {
    return (
      <div className="flex min-h-0 flex-1 items-center justify-center">
        <EmptyState title="Select a card" body="Click a board card to inspect the full ticket, handoff, activity, and assigned agent." compact />
      </div>
    )
  }
  const dependencies = [
    ...(card.parents ?? []).map((id) => ({ id, label: 'depends on' })),
    ...(card.children ?? []).map((id) => ({ id, label: 'unblocks' })),
  ]
  const rejection = detectVerifierRejection(card)
  const activity = [
    ...(card.comments ?? []).map((comment) => ({
      at: comment.timestamp ?? 0,
      label: comment.author || 'comment',
      text: comment.body,
      kind: 'comment',
    })),
    ...(card.events ?? []).map((event) => ({
      at: event.timestamp ?? 0,
      label: event.actor || event.kind || 'event',
      text: event.message || '',
      kind: event.kind || 'event',
    })),
  ].sort((a, b) => b.at - a.at)

  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <div className="shrink-0 hairline-b pb-3">
        <div className="flex items-start justify-between gap-3">
          <div className="min-w-0">
            <div className="label text-accent">task detail</div>
            <div className="mt-2 flex flex-wrap items-center gap-2">
              <div className="font-mono text-[24px] leading-none text-fg-0">{card.id}</div>
              <span className={`rounded bg-white/[0.04] px-2 py-1 font-mono text-[9px] uppercase ring-hairline ${kanbanStatusClass(card.status)}`}>
                {card.status}
              </span>
              {typeof card.priority === 'number' && (
                <span className={`rounded bg-white/[0.04] px-2 py-1 font-mono text-[9px] uppercase ring-hairline ${kanbanPriorityClass(card.priority)}`}>
                  p{card.priority}
                </span>
              )}
            </div>
            <h2 className="mt-2 line-clamp-3 text-[15px] leading-[1.35] text-fg-0">{card.title}</h2>
          </div>
          <div className="flex shrink-0 items-center gap-1">
            <button
              type="button"
              onClick={onPrev}
              className="rounded-md bg-white/[0.04] px-2 py-1.5 font-mono text-[10px] text-fg-2 ring-hairline hover:bg-white/[0.08] hover:text-fg-0"
              title="Previous card"
            >
              &lt;
            </button>
            <button
              type="button"
              onClick={onNext}
              className="rounded-md bg-white/[0.04] px-2 py-1.5 font-mono text-[10px] text-fg-2 ring-hairline hover:bg-white/[0.08] hover:text-fg-0"
              title="Next card"
            >
              &gt;
            </button>
          </div>
        </div>
      </div>

      <div className="mt-3 min-h-0 flex-1 space-y-4 overflow-auto pr-1">
        <div className="grid grid-cols-2 gap-2">
          <DetailStat label="agent" value={agents[0]?.session.title || card.assignee || 'unassigned'} />
          <DetailStat label="created by" value={card.createdBy || 'parent'} />
          <DetailStat label="updated" value={card.updatedAt ? relativeTime(card.updatedAt) : 'n/a'} />
          <DetailStat
            label="verification"
            value={card.requiresVerification ? 'required' : 'skipped'}
          />
        </div>

        {rejection && (
          <div className="rounded-lg bg-warn/[0.10] p-3 ring-1 ring-warn/30">
            <div className="label mb-1 text-[9px] uppercase tracking-[0.10em] text-warn">
              verifier rejected
            </div>
            <div className="whitespace-pre-wrap text-[11.5px] leading-[1.5] text-fg-0">
              {rejection.text}
            </div>
            {rejection.actor && (
              <div className="mt-2 font-mono text-[9px] text-warn/80">
                — {rejection.actor}
              </div>
            )}
          </div>
        )}

        {card.body && (
          <DetailSection title="description">
            <div className="whitespace-pre-wrap text-[12px] leading-[1.6] text-fg-1">{card.body}</div>
          </DetailSection>
        )}

        {card.spec?.definition_of_done && card.spec.definition_of_done.length > 0 && (
          <DetailSection title="definition of done">
            <ul className="space-y-1.5">
              {card.spec.definition_of_done.map((line, i) => (
                <li key={i} className="flex items-start gap-2 text-[11.5px] leading-[1.5] text-fg-1">
                  <span className="mt-1 inline-block h-3 w-3 shrink-0 rounded-sm border border-fg-3/40" />
                  <span>{line}</span>
                </li>
              ))}
            </ul>
          </DetailSection>
        )}

        {card.spec?.references &&
          (Boolean(card.spec.references.files?.length) ||
            Boolean(card.spec.references.findings?.length) ||
            Boolean(card.spec.references.cards?.length)) && (
            <DetailSection title="references">
              <div className="space-y-2 text-[11px] leading-[1.5] text-fg-1">
                {card.spec.references.files && card.spec.references.files.length > 0 && (
                  <div>
                    <div className="label mb-1 text-[8.5px] text-fg-3">files</div>
                    <div className="flex flex-wrap gap-1">
                      {card.spec.references.files.map((path) => (
                        <span
                          key={path}
                          className="rounded bg-white/[0.04] px-1.5 py-0.5 font-mono text-[10px] text-fg-1 ring-hairline"
                        >
                          {path}
                        </span>
                      ))}
                    </div>
                  </div>
                )}
                {card.spec.references.cards && card.spec.references.cards.length > 0 && (
                  <div>
                    <div className="label mb-1 text-[8.5px] text-fg-3">related cards</div>
                    <div className="flex flex-wrap gap-1">
                      {card.spec.references.cards.map((id) => (
                        <span
                          key={id}
                          className="rounded bg-accent/[0.06] px-1.5 py-0.5 font-mono text-[10px] text-accent ring-1 ring-accent/15"
                        >
                          {id}
                        </span>
                      ))}
                    </div>
                  </div>
                )}
                {card.spec.references.findings && card.spec.references.findings.length > 0 && (
                  <div>
                    <div className="label mb-1 text-[8.5px] text-fg-3">findings</div>
                    <ul className="ml-1 list-disc pl-3 text-[11px] text-fg-1">
                      {card.spec.references.findings.map((finding, i) => (
                        <li key={i}>{finding}</li>
                      ))}
                    </ul>
                  </div>
                )}
              </div>
            </DetailSection>
          )}

        {card.spec?.verify_with && (
          <DetailSection title="verify with">
            <pre className="overflow-x-auto rounded-md bg-black/40 p-2 font-mono text-[10.5px] leading-[1.5] text-fg-0">
              {card.spec.verify_with}
            </pre>
          </DetailSection>
        )}

        {typeof card.spec?.token_budget === 'number' && card.spec.token_budget > 0 && (
          <DetailSection title="token budget">
            <div className="font-mono text-[11px] text-fg-1">
              {card.spec.token_budget.toLocaleString()} tokens
              <span className="ml-2 text-fg-3">— circuit breaker treats this as the wall-clock budget</span>
            </div>
          </DetailSection>
        )}

        {dependencies.length > 0 && (
          <DetailSection title="links">
            <div className="flex flex-wrap gap-1.5">
              {dependencies.map((dep) => (
                <span key={`${dep.label}-${dep.id}`} className="rounded bg-white/[0.04] px-2 py-1 font-mono text-[9px] text-fg-2 ring-hairline">
                  {dep.label} <span className="text-accent">{dep.id}</span>
                </span>
              ))}
            </div>
          </DetailSection>
        )}

        {(card.summary || card.result) && (
          <DetailSection title={card.status === 'done' ? 'handoff' : 'notes'}>
            {card.summary && <div className="whitespace-pre-wrap text-[12px] leading-[1.55] text-fg-1">{card.summary}</div>}
            {card.result && <div className="mt-2 whitespace-pre-wrap rounded-md bg-black/25 p-2 text-[11px] leading-[1.5] text-fg-2 ring-hairline">{card.result}</div>}
          </DetailSection>
        )}

        <DetailSection title={`activity ${activity.length ? activity.length : ''}`}>
          {activity.length === 0 ? (
            <div className="font-mono text-[10px] text-fg-3">No activity captured yet.</div>
          ) : (
            <div className="space-y-2">
              {activity.slice(0, 10).map((item, index) => (
                <div key={`${item.kind}-${item.at}-${index}`} className="grid grid-cols-[14px_minmax(0,1fr)] gap-2">
                  <span className={`mt-1.5 h-2 w-2 rounded-full ${item.kind === 'comment' ? 'bg-accent/80' : 'bg-ok/70'}`} />
                  <div className="min-w-0">
                    <div className="flex items-center justify-between gap-2 font-mono text-[9px] text-fg-3">
                      <span className="truncate text-fg-2">{item.label}</span>
                      <span>{item.at ? relativeTime(item.at) : 'now'}</span>
                    </div>
                    {item.text && <div className="mt-0.5 line-clamp-3 text-[11px] leading-[1.45] text-fg-1">{item.text}</div>}
                  </div>
                </div>
              ))}
            </div>
          )}
        </DetailSection>

        <DetailSection title="agent status">
          {agents.length === 0 ? (
            <div className="font-mono text-[10px] text-fg-3">
              {card.assignee ? `${card.assignee} is planned but no live worker is attached yet.` : 'No live worker attached to this card.'}
            </div>
          ) : (
            <div className="space-y-2">
              {agents.map((agent) => (
                <div key={agent.session.id} className="rounded-lg bg-white/[0.028] p-2 ring-hairline">
                  <div className="flex items-center gap-2">
                    <span
                      className="h-2.5 w-2.5 rounded-full"
                      style={{ background: PROFILE_HEX[agent.agentType] ?? PROFILE_HEX.general }}
                    />
                    <span className="min-w-0 flex-1 truncate text-[12px] text-fg-0">{agent.session.title}</span>
                    <span className={`font-mono text-[9px] uppercase ${statusTextClass(agent.status)}`}>{agent.status}</span>
                    {agent.attachable && (
                      <button
                        type="button"
                        onClick={() => onAttach(agent.session.id, 'split')}
                        className="rounded bg-white/[0.04] px-2 py-1 font-mono text-[9px] uppercase text-fg-2 ring-hairline hover:bg-white/[0.08] hover:text-fg-0"
                      >
                        split
                      </button>
                    )}
                  </div>
                  <div className="mt-2 grid grid-cols-3 gap-2 font-mono text-[9px] text-fg-3">
                    <span>tools <span className="text-fg-1">{agent.tools.length || agent.sub?.toolsCalled || 0}</span></span>
                    <span>tokens <span className="text-fg-1">{formatTokens(agent.tokensIn + agent.tokensOut)}</span></span>
                    <span>elapsed <span className="text-fg-1">{formatDuration(agent.elapsedMs)}</span></span>
                  </div>
                  <ToolActivityStrip tools={agent.tools} />
                </div>
              ))}
            </div>
          )}
        </DetailSection>

      </div>
    </div>
  )
}

function DetailSection({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section className="rounded-lg bg-white/[0.025] p-3 ring-hairline">
      <div className="label mb-2 text-[9px] text-fg-3">{title}</div>
      {children}
    </section>
  )
}

function DetailStat({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded-lg bg-white/[0.025] p-2 ring-hairline">
      <div className="label text-[8.5px] text-fg-3">{label}</div>
      <div className="mt-1 truncate font-mono text-[11px] text-fg-0">{value}</div>
    </div>
  )
}

function KanbanCardMaterial({
  status,
  selected,
}: {
  status: string
  selected: boolean
}) {
  if (status === 'todo' || status === 'triage') {
    return (
      <>
        <div className="pointer-events-none absolute inset-y-2 left-2 w-4 rounded-[4px] border border-[#172023]/10 bg-[#172023]/[0.035]" />
        <div className="pointer-events-none absolute inset-y-4 left-6 border-l border-dashed border-[#172023]/20" />
        <div className="pointer-events-none absolute right-2 top-2 h-3 w-3 rounded-sm border-b border-l border-[#172023]/18 bg-[#e4edf1]/35 shadow-[inset_1px_-1px_0_rgba(0,0,0,0.08)]" />
      </>
    )
  }
  if (status === 'ready') {
    return (
      <>
        {/* Paper-stack ghost cards behind the main card: cream tones so it
            reads as a stapled stack of index cards, not a layered glass panel. */}
        <div className="pointer-events-none absolute -right-1.5 top-2 h-[calc(100%-4px)] w-[96%] rounded-md border border-[#9da08e]/32 bg-[#d2d5c3] shadow-[0_2px_0_rgba(0,0,0,0.08)]" />

        {/* Punch holes down the left edge: recessed dark dots with a faint
            inner highlight that suggests the punched paper edge. */}
        <div className="pointer-events-none absolute inset-y-2 left-3 z-0 w-6 rounded-l-md border-r border-[#1a1c12]/12 bg-[#e6e8da]/35" />
        <div className="pointer-events-none absolute inset-y-4 left-[15px] z-0 flex flex-col justify-around">
          {Array.from({ length: 5 }).map((_, index) => (
            <span
              key={index}
              className="h-2 w-2 rounded-full bg-[#1c1e16] shadow-[inset_0_1px_1px_rgba(0,0,0,0.65),inset_0_-1px_0_rgba(255,255,255,0.10),0_0_0_1px_rgba(0,0,0,0.14)]"
            />
          ))}
        </div>
      </>
    )
  }
  if (status === 'running') {
    return (
      <>
        <div className="pointer-events-none absolute inset-x-3 top-3 h-5 rounded-[5px] border border-white/[0.105] bg-[linear-gradient(180deg,rgba(255,255,255,0.12),rgba(8,9,9,0.24))] shadow-[inset_0_1px_0_rgba(255,255,255,0.16),inset_0_-1px_0_rgba(0,0,0,0.48)]">
          <span className="absolute left-3 top-1/2 h-1 w-14 -translate-y-1/2 rounded-full bg-white/[0.14]" />
          <span className="absolute right-3 top-1/2 h-1.5 w-7 -translate-y-1/2 rounded-full bg-ok/38 shadow-[0_0_12px_rgba(112,184,103,0.18)]" />
        </div>
        <div className="pointer-events-none absolute inset-x-3 bottom-2 h-4 rounded-[4px] border border-black/35 bg-[linear-gradient(180deg,rgba(255,255,255,0.035),rgba(0,0,0,0.42))] shadow-[inset_0_1px_0_rgba(255,255,255,0.06),0_-7px_18px_rgba(0,0,0,0.20)]" />
      </>
    )
  }
  if (status === 'blocked') {
    return (
      <>
        <span className="pointer-events-none absolute left-2 top-2 h-4 w-4 border-l border-t border-danger/45" />
        <span className="pointer-events-none absolute right-2 top-2 h-4 w-4 border-r border-t border-danger/35" />
        <span className="pointer-events-none absolute bottom-2 left-2 h-4 w-4 border-b border-l border-white/20" />
        <span className="pointer-events-none absolute bottom-2 right-2 h-4 w-4 border-b border-r border-white/20" />
        <div className="pointer-events-none absolute inset-x-2 top-3 h-px bg-gradient-to-r from-transparent via-white/30 to-transparent" />
        <div className="pointer-events-none absolute -right-8 top-0 h-full w-16 rotate-12 bg-white/[0.025]" />
      </>
    )
  }
  if (status === 'done') {
    return (
      <>
        <div className="pointer-events-none absolute inset-x-3 top-3 flex h-7 items-center justify-between overflow-hidden rounded-[3px] border border-ok/20 bg-black/25 px-2.5">
          <span className="font-mono text-[8px] uppercase tracking-[0.16em] text-ok/45">sealed</span>
          <span className="h-2 w-2 rounded-full bg-ok/55 shadow-[0_0_12px_rgba(112,184,103,0.28)]" />
        </div>
        <div className="pointer-events-none absolute inset-x-4 bottom-3 h-px bg-ok/20" />
      </>
    )
  }
  if (status === 'cancelled') {
    return (
      <>
        <div className="pointer-events-none absolute inset-0 bg-[repeating-linear-gradient(-35deg,transparent_0,transparent_8px,rgba(255,120,120,0.045)_8px,rgba(255,120,120,0.045)_10px)]" />
        <div className="pointer-events-none absolute inset-x-4 top-1/2 border-t border-dashed border-danger/25" />
      </>
    )
  }
  return (
    <>
      <div className="pointer-events-none absolute inset-x-4 top-8 border-t border-dashed border-white/12" />
      <div className="pointer-events-none absolute bottom-3 right-3 h-4 w-4 rounded-full border border-dashed border-white/18" />
      {selected && <div className="pointer-events-none absolute inset-0 bg-accent/[0.025]" />}
    </>
  )
}

function KanbanAgentBadge({
  agents,
  onAttach,
  variant,
}: {
  agents: AgentView[]
  onAttach: (id: string, mode?: 'replace' | 'split') => void
  variant: 'dark' | 'paper' | 'ticket'
}) {
  if (agents.length === 0) return null
  const firstAgent = agents[0]
  if (!firstAgent) return null
  const label = agents.length === 1
    ? `agent ${firstAgent.agentType.replace(/-/g, ' ')}`
    : `${agents.length} agents`
  const title = agents
    .map((agent) => `${agent.agentType}: ${agent.session.title || agent.session.id}`)
    .join('\n')
  const variantClass = variant === 'paper'
    ? 'bg-[#0e0f08]/[0.08] text-[#11140b] ring-[#0e0f08]/18 hover:bg-[#0e0f08]/[0.12]'
    : variant === 'ticket'
      ? 'bg-[#10191d]/[0.075] text-[#10191d] ring-[#10191d]/18 hover:bg-[#10191d]/[0.12]'
      : 'bg-black/38 text-fg-1 ring-white/[0.13] hover:bg-white/[0.065] hover:text-fg-0'

  return (
    <button
      type="button"
      onClick={(event) => {
        event.stopPropagation()
        onAttach(firstAgent.session.id, 'split')
      }}
      title={title}
      className={`group flex max-w-[142px] shrink-0 items-center gap-1.5 rounded-full px-2 py-1 font-mono text-[8.5px] uppercase leading-none ring-1 transition ${variantClass}`}
    >
      <span className="truncate">{label}</span>
      <span className="flex shrink-0 items-center gap-0.5">
        {agents.slice(0, 4).map((agent) => (
          <span
            key={agent.session.id}
            className="h-1.5 w-1.5 rounded-full shadow-[0_0_10px_rgba(255,255,255,0.10)]"
            style={{ background: PROFILE_HEX[agent.agentType] ?? PROFILE_HEX.general }}
          />
        ))}
      </span>
    </button>
  )
}

function KanbanCard({
  card,
  agents,
  selected = false,
  onSelect,
  onAttach,
  compact = false,
}: {
  card: KanbanCardView
  agents: AgentView[]
  selected?: boolean
  onSelect?: (id: string) => void
  onAttach: (id: string, mode?: 'replace' | 'split') => void
  compact?: boolean
}) {
  const progress = kanbanCardProgress(card)
  const latest = latestKanbanActivity(card)
  const blocked = card.status === 'blocked'
  const owner = agents[0]?.session.title || card.assignee || card.createdBy || ''
  const materialClass = kanbanCardMaterialClass(card.status, selected, Boolean(onSelect))

  // Paper variants use dark ink instead of the standard white-on-dark ramp.
  const ticket = card.status === 'todo' || card.status === 'triage'
  const paper = card.status === 'ready'
  const palette = paper
    ? {
        id: 'text-[#1a1c12]',
        status: 'text-[#3a3d2c]',
        priority: kanbanPriorityClass(card.priority ?? 2),
        title: 'text-[#0e0f08]',
        meta: 'text-[#3a3d2c]',
        ownerStrong: 'text-[#1a1c12]',
        body: 'text-[#1f2117]',
        latestLabel: 'text-[#1a1c12]',
        latestText: 'text-[#3a3d2c]',
        pill: 'rounded bg-[#0e0f08]/[0.07] text-[#1a1c12] px-1.5 py-0.5 ring-1 ring-[#0e0f08]/15',
        summary: 'rounded-md bg-[#0e0f08]/[0.06] px-2 py-1.5 text-[10px] leading-[1.4] text-[#1a1c12] ring-1 ring-[#0e0f08]/15',
      }
    : ticket
      ? {
          id: 'text-[#172023]',
          status: 'text-[#314047]',
          priority: kanbanPriorityClass(card.priority ?? 2),
          title: 'text-[#0d1518]',
          meta: 'text-[#314047]',
          ownerStrong: 'text-[#172023]',
          body: 'text-[#1d2a2f]',
          latestLabel: 'text-[#10191d]',
          latestText: 'text-[#334248]',
          pill: 'rounded bg-[#10191d]/[0.07] text-[#172023] px-1.5 py-0.5 ring-1 ring-[#10191d]/15',
          summary: 'rounded-md bg-[#10191d]/[0.055] px-2 py-1.5 text-[10px] leading-[1.4] text-[#172023] ring-1 ring-[#10191d]/12',
        }
    : {
        id: 'text-fg-1',
        status: kanbanStatusClass(card.status),
        priority: kanbanPriorityClass(card.priority ?? 2),
        title: 'text-fg-0',
        meta: 'text-fg-3',
        ownerStrong: 'text-fg-2',
        body: 'text-fg-2',
        latestLabel: 'text-fg-1',
        latestText: 'text-fg-3',
        pill: 'rounded bg-white/[0.04] px-1.5 py-0.5 ring-hairline',
        summary: 'rounded-md bg-white/[0.035] px-2 py-1.5 text-[10px] leading-[1.4] text-fg-2 ring-hairline',
      }
  const agentBadgeVariant = paper ? 'paper' : ticket ? 'ticket' : 'dark'

  return (
    <article
      role={onSelect ? 'button' : undefined}
      tabIndex={onSelect ? 0 : undefined}
      aria-selected={onSelect ? selected : undefined}
      onClick={() => onSelect?.(card.id)}
      onKeyDown={(event) => {
        if (!onSelect) return
        if (event.key === 'Enter' || event.key === ' ') {
          event.preventDefault()
          onSelect(card.id)
        }
      }}
      className={materialClass}
    >
      <KanbanCardMaterial status={card.status} selected={selected} />
      <div className="relative z-10 flex items-start justify-between gap-2">
        <div className="min-w-0">
          <div className="flex items-center gap-2">
            <span className={`font-mono text-[10px] ${palette.id}`}>{card.id}</span>
            <span className={`font-mono text-[9px] uppercase ${palette.status}`}>
              {card.status}
            </span>
            {typeof card.priority === 'number' && (
              <span className={`font-mono text-[8.5px] uppercase ${palette.priority}`}>
                p{card.priority}
              </span>
            )}
            <KanbanVerdictBadge card={card} />
            <KanbanRetryPill card={card} />
            <KanbanVerifyOnCompletePill card={card} />
          </div>
          <h3 className={`mt-1 line-clamp-2 text-[12px] leading-[1.35] ${palette.title}`}>
            {card.title}
          </h3>
          {owner && (
            <div className={`mt-1 truncate font-mono text-[9px] ${palette.meta}`}>
              owner <span className={palette.ownerStrong}>{owner}</span>
            </div>
          )}
        </div>
        <KanbanAgentBadge agents={agents} onAttach={onAttach} variant={agentBadgeVariant} />
      </div>
      {card.status === 'running' && (
        <KanbanLivenessStrip card={card} palette={palette} />
      )}
      <div className={`relative z-10 mt-3 h-1.5 overflow-hidden ${kanbanProgressTrackClass(card.status)}`}>
        <div
          className={`h-full ${kanbanProgressFillClass(card.status, blocked)}`}
          style={{ width: `${progress}%` }}
        />
      </div>
      <KanbanTokenBurnBar card={card} />
      {!compact && card.body && (
        <p className={`relative z-10 mt-2 line-clamp-3 text-[10.5px] leading-[1.45] ${palette.body}`}>
          {card.body}
        </p>
      )}
      {(card.parents?.length || card.children?.length) && (
        <div className={`relative z-10 mt-2 flex flex-wrap gap-1 font-mono text-[9px] ${palette.meta}`}>
          {(card.parents ?? []).slice(0, 3).map((id) => (
            <span key={`p-${id}`} className={palette.pill}>
              from {id}
            </span>
          ))}
          {(card.children ?? []).slice(0, 3).map((id) => (
            <span key={`c-${id}`} className={palette.pill}>
              to {id}
            </span>
          ))}
        </div>
      )}
      {latest && (
        <div className={`relative z-10 mt-2 line-clamp-2 font-mono text-[9.5px] leading-[1.4] ${palette.latestText}`}>
          <span className={palette.latestLabel}>{latest.label}</span>
          {latest.text ? ` · ${latest.text}` : ''}
        </div>
      )}
      {card.summary && (
        <div className={`relative z-10 mt-2 line-clamp-2 ${palette.summary}`}>
          {card.summary}
        </div>
      )}
    </article>
  )
}

function KanbanAssignmentList({
  agents,
  cards,
  onAttach,
  compact = false,
}: {
  agents: AgentView[]
  cards: KanbanCardView[]
  onAttach: (id: string, mode?: 'replace' | 'split') => void
  compact?: boolean
}) {
  const cardById = new Map(cards.map((card) => [card.id, card]))
  const assigned = agents.filter((agent) => agent.kanbanTaskId)
  const planned = cards.filter((card) => card.assignee && !assigned.some((agent) => agent.kanbanTaskId === card.id))
  if (assigned.length === 0) {
    if (planned.length > 0) {
      return (
        <div className="space-y-2">
          {planned.slice(0, compact ? 5 : 12).map((card) => (
            <PlannedKanbanAssignment key={card.id} card={card} compact={compact} />
          ))}
        </div>
      )
    }
    return (
      <EmptyState
        title="No assigned agents"
        body="Agents attached to board cards will appear here with their current lane."
        compact
      />
    )
  }
  return (
    <div className="space-y-2">
      {assigned.map((agent) => {
        const card = agent.kanbanTaskId ? cardById.get(agent.kanbanTaskId) : undefined
        const color = PROFILE_HEX[agent.agentType] ?? PROFILE_HEX.general
        return (
          <div key={agent.session.id} className="rounded-lg bg-white/[0.028] p-2.5 ring-hairline">
            <div className="flex items-start gap-2">
              <span
                className="mt-1 h-2.5 w-2.5 shrink-0 rounded-full"
                style={{ background: color, boxShadow: `0 0 16px ${color}55` }}
              />
              <div className="min-w-0 flex-1">
                <div className="flex min-w-0 items-center gap-2">
                  <span className="truncate text-[12px] text-fg-0">{agent.session.title}</span>
                  <span className={`font-mono text-[9px] uppercase ${statusTextClass(agent.status)}`}>{agent.status}</span>
                </div>
                <div className="mt-1 flex flex-wrap items-center gap-1.5 font-mono text-[9px] text-fg-3">
                  <span className="rounded bg-accent/10 px-1.5 py-0.5 text-accent ring-1 ring-accent/20">
                    {agent.kanbanTaskId}
                  </span>
                  {card && <span className={kanbanStatusClass(card.status)}>{card.status}</span>}
                  <span>{agent.tools.length || agent.sub?.toolsCalled || 0} tools</span>
                </div>
                {!compact && card && (
                  <div className="mt-1 line-clamp-2 text-[10.5px] leading-[1.4] text-fg-2">
                    {card.title}
                  </div>
                )}
              </div>
              {agent.attachable && (
                <button
                  type="button"
                  onClick={() => onAttach(agent.session.id, 'split')}
                  className="rounded bg-white/[0.04] px-2 py-1 font-mono text-[9px] uppercase text-fg-2 ring-hairline hover:bg-white/[0.08] hover:text-fg-0"
                >
                  split
                </button>
              )}
            </div>
          </div>
        )
      })}
      {planned.length > 0 && (
        <div className="space-y-2 pt-2">
          <div className="label text-[8.5px] text-fg-3">planned</div>
          {planned.slice(0, compact ? 3 : 8).map((card) => (
            <PlannedKanbanAssignment key={card.id} card={card} compact={compact} />
          ))}
        </div>
      )}
    </div>
  )
}

function PlannedKanbanAssignment({
  card,
  compact,
}: {
  card: KanbanCardView
  compact?: boolean
}) {
  return (
    <div className="rounded-lg bg-white/[0.028] p-2.5 ring-hairline">
      <div className="flex items-start justify-between gap-2">
        <div className="min-w-0">
          <div className="flex items-center gap-2 font-mono text-[9px]">
            <span className="rounded bg-accent/10 px-1.5 py-0.5 text-accent ring-1 ring-accent/20">{card.id}</span>
            <span className={kanbanStatusClass(card.status)}>{card.status}</span>
          </div>
          <div className="mt-1 truncate text-[12px] text-fg-0">{card.assignee}</div>
          {!compact && <div className="mt-1 line-clamp-2 text-[10.5px] leading-[1.4] text-fg-2">{card.title}</div>}
        </div>
        <span className={`font-mono text-[9px] uppercase ${kanbanPriorityClass(card.priority ?? 2)}`}>
          p{card.priority ?? 2}
        </span>
      </div>
    </div>
  )
}

function KanbanEventFeed({ cards }: { cards: KanbanCardView[] }) {
  const events = cards
    .flatMap((card) =>
      (card.events ?? []).map((event) => ({
        ...event,
        card,
        timestamp: event.timestamp ?? card.updatedAt ?? 0,
      })),
    )
    .sort((a, b) => (b.timestamp ?? 0) - (a.timestamp ?? 0))
    .slice(0, 20)
  if (events.length === 0) {
    return <EmptyState title="No board events" body="Card updates will collect here." compact />
  }
  return (
    <div className="mt-3 min-h-0 flex-1 space-y-2 overflow-auto pr-1">
      {events.map((event, index) => (
        <div key={`${event.card.id}-${event.timestamp}-${index}`} className="rounded-lg bg-white/[0.028] p-2.5 ring-hairline">
          <div className="flex items-center justify-between gap-2">
            <span className="font-mono text-[9px] text-accent">{event.card.id}</span>
            <span className="font-mono text-[9px] text-fg-3">
              {event.timestamp ? relativeTime(event.timestamp) : 'now'}
            </span>
          </div>
          {event.actor && (
            <div className="mt-1 truncate font-mono text-[9px] text-fg-3">
              {event.kind || 'event'} by <span className="text-fg-2">{event.actor}</span>
            </div>
          )}
          <div className="mt-1 line-clamp-2 text-[10.5px] leading-[1.4] text-fg-1">
            {event.message || event.kind || event.card.title}
          </div>
        </div>
      ))}
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
  onAttach: (id: string, mode?: 'replace' | 'split') => void
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
  onAttach: (id: string, mode?: 'replace' | 'split') => void
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
  onAttach: (id: string, mode?: 'replace' | 'split') => void
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
          <div className="flex shrink-0 items-center gap-1">
            <button
              onClick={() => onAttach(agent.session.id, 'split')}
              className="rounded-md bg-white/[0.04] px-2 py-1.5 font-mono text-[10px] uppercase tracking-[0.08em] text-fg-2 ring-hairline hover:bg-white/[0.08] hover:text-fg-0"
            >
              split
            </button>
            <button
              onClick={() => onAttach(agent.session.id, 'replace')}
              className="rounded-md bg-accent/10 px-2 py-1.5 font-mono text-[10px] uppercase tracking-[0.08em] text-accent ring-1 ring-accent/20 hover:bg-accent/18"
            >
              open
            </button>
          </div>
        )}
      </div>
    </div>
  )
}

// ── Hive Mind Timeline ────────────────────────────────────────────────────
//
// Swimlane Gantt-style view of the swarm. Each agent is a horizontal lane;
// tool calls render as small bars within the lane; a horizontal bus rail
// above the lanes acts as the message medium — every publish/read drops a
// vertical connector between an agent's lane and the rail. File artifacts
// (diffs/edits) drop into the timeline at the moment they were created
// inside the originating agent's lane.
//
// Two accent colors only. Everything else is monochrome:
//   ACCENT_BUS  — message bus + sharing connections
//   ACCENT_FILE — artifacts / diffs
const ACCENT_BUS = '#7fb8e8'
const ACCENT_FILE = '#e8a854'

// Detect a shared prefix across agent titles so lane labels can elide it.
// Many swarms ship 20+ agents with names like "Dir Structure - X" —
// stripping the boilerplate makes each lane label uniquely identifying.
function commonTitlePrefix(titles: string[]): string {
  if (titles.length < 3) return ''
  let prefix = titles[0]
  for (let i = 1; i < titles.length; i++) {
    while (prefix && !titles[i].startsWith(prefix)) {
      prefix = prefix.slice(0, -1)
    }
    if (!prefix) return ''
  }
  if (prefix.length < 6) return ''
  const m = prefix.match(/^(.+?[\s\-:_·])[^\s\-:_·]*$/)
  return m ? m[1] : prefix
}

// Strip a trailing `[bracketed-tag]` suffix and decode the most common HTML
// entities. Many subagent titles are auto-tagged with `[explore-fast]` etc.
// — that's already encoded in the agent-type pill at the top of the card,
// so we elide it from the visible title to free up line height for the
// actual semantic title.
function cleanTitle(raw: string): string {
  const stripped = raw.replace(/\s*\[[^\]\n]+\]\s*$/g, '').trim()
  return decodeHtmlEntities(stripped || raw)
}
function decodeHtmlEntities(s: string): string {
  return s
    .replace(/&amp;/g, '&')
    .replace(/&lt;/g, '<')
    .replace(/&gt;/g, '>')
    .replace(/&quot;/g, '"')
    .replace(/&#39;/g, "'")
}

// ── Profile shapes ─────────────────────────────────────────────────────
// Each agent profile family gets a distinct silhouette so you can read the
// type at a glance without parsing the pill text. Paths are normalized to
// a 160×100 viewBox; cards stretch them via `preserveAspectRatio="none"`.
//
//   rect — general / default (rounded rectangle)
//   hex  — explore family (research / discovery)
//   pill — plan / review / docs (long-running deliberation)
//   oct  — operational / computer (browser-qa, performance, computer)
//   code — code / test / verify (notched corner — "file with cut")
const SHAPE_PATHS: Record<string, string> = {
  rect:
    'M 8 0.6 L 152 0.6 Q 159.4 0.6 159.4 8 L 159.4 92 Q 159.4 99.4 152 99.4 L 8 99.4 Q 0.6 99.4 0.6 92 L 0.6 8 Q 0.6 0.6 8 0.6 Z',
  hex:
    'M 18 0.6 L 142 0.6 L 159.4 50 L 142 99.4 L 18 99.4 L 0.6 50 Z',
  pill:
    'M 50 0.6 L 110 0.6 A 49.4 49.4 0 0 1 110 99.4 L 50 99.4 A 49.4 49.4 0 0 1 50 0.6 Z',
  oct:
    'M 18 0.6 L 142 0.6 L 159.4 18 L 159.4 82 L 142 99.4 L 18 99.4 L 0.6 82 L 0.6 18 Z',
  // Smaller, tighter notch so the top-row content (CODE · ● status) clears
  // the diagonal cut. Notch goes from x=138 down to y=22 (was 132/28).
  code:
    'M 8 0.6 L 138 0.6 L 159.4 22 L 159.4 92 Q 159.4 99.4 152 99.4 L 8 99.4 Q 0.6 99.4 0.6 92 L 0.6 8 Q 0.6 0.6 8 0.6 Z',
}

const PROFILE_SHAPE: Record<string, keyof typeof SHAPE_PATHS> = {
  general: 'rect',
  explore: 'hex',
  'explore-fast': 'hex',
  code: 'code',
  test: 'code',
  verify: 'code',
  plan: 'pill',
  review: 'pill',
  docs: 'pill',
  'memory-curator': 'oct',
  'browser-qa': 'oct',
  performance: 'oct',
  computer: 'oct',
}

function shapeFor(agentType: string): keyof typeof SHAPE_PATHS {
  return PROFILE_SHAPE[agentType] ?? 'rect'
}

// Per-side inset for the HTML content overlay so text never crashes into
// a cut/curved edge. `code` gets extra right padding only — the notch is
// in the top-right corner, so we don't waste space on the left side.
const SHAPE_INSET: Record<
  keyof typeof SHAPE_PATHS,
  { left: number; right: number; top: number; bottom: number }
> = {
  rect: { left: 11, right: 11, top: 9, bottom: 9 },
  hex:  { left: 22, right: 22, top: 9, bottom: 9 },
  pill: { left: 24, right: 24, top: 9, bottom: 9 },
  oct:  { left: 18, right: 18, top: 9, bottom: 9 },
  code: { left: 11, right: 24, top: 9, bottom: 9 },
}

// ── HiveMindGraph ─────────────────────────────────────────────────────────
//
// Node-graph view of a swarm session. Orchestrator on the left, agents
// laid out in columns by round, file artifacts in a column on the right.
// Edges are the hero:
//   - Delegation:  faint dashed white arc (orchestrator → round 1 agents)
//   - Collab:      cyan curved arc bowing above the cards (publish → read)
//   - Artifact:    amber straight arc (creating agent → file change)
//
// A thin event-density strip at the bottom keeps the time context without
// the canvas itself being a wall-clock Gantt.

interface CardPos {
  x: number
  y: number
  w: number
  h: number
}

function HiveMindGraph({
  agents,
  events,
  fileChanges,
  source,
  onAttach,
}: {
  agents: AgentView[]
  events: BusEventView[]
  fileChanges: FileChangeSet[]
  source: SessionSnapshot | undefined
  onAttach: (id: string) => void
}) {
  const sortedAgents = agents.slice().sort((a, b) => agentStartedAt(a) - agentStartedAt(b))
  const rounds = groupAgentRounds(sortedAgents)
  const titlePrefix = commonTitlePrefix(
    sortedAgents.map((a) => a.session.title || a.sub?.label || a.session.id),
  )

  // Layout constants. Card sizes are tight so multiple round columns fit
  // without horizontal scroll on a typical 700–1100px panel; canvas can
  // still scroll if the swarm is unusually wide or tall.
  const orchW = 184
  const orchH = 100
  const cardW = 168
  const cardH = 100
  const fileW = 172
  const fileH = 56
  const colGap = 56 // wider gap so collab arcs read clearly
  const rowGap = 12
  const topPad = 64
  const leftPad = 28
  const rightPad = 28

  // tool → agent index lookup so artifact edges land on the right card
  const toolToAgent = new Map<string, number>()
  sortedAgents.forEach((agent, i) => {
    for (const tool of agent.tools) toolToAgent.set(tool.id, i)
  })
  const placedArtifacts = fileChanges
    .filter((fc) => toolToAgent.has(fc.toolCallId))
    .sort((a, b) => a.createdAt - b.createdAt)
  const hasArtifacts = placedArtifacts.length > 0

  // Card positions
  const orchPos: CardPos = { x: leftPad, y: topPad, w: orchW, h: orchH }
  const agentPos = new Map<string, CardPos>()
  rounds.forEach((round, ri) => {
    const x = leftPad + orchW + colGap + ri * (cardW + colGap)
    round.agents.forEach((agent, ai) => {
      agentPos.set(agent.session.id, {
        x,
        y: topPad + ai * (cardH + rowGap),
        w: cardW,
        h: cardH,
      })
    })
  })

  const artifactColX = leftPad + orchW + colGap + rounds.length * (cardW + colGap)
  const artifactPos = new Map<string, CardPos>()
  placedArtifacts.forEach((fc, fi) => {
    artifactPos.set(fc.id, {
      x: artifactColX,
      y: topPad + fi * (fileH + rowGap),
      w: fileW,
      h: fileH,
    })
  })

  // Canvas size
  const lastColRight = hasArtifacts
    ? artifactColX + fileW
    : leftPad + orchW + colGap + Math.max(0, rounds.length) * (cardW + colGap) - colGap
  const canvasW = Math.max(560, lastColRight + rightPad)
  const tallestRound = Math.max(0, ...rounds.map((r) => r.agents.length * (cardH + rowGap) - rowGap))
  const tallestArtifacts = placedArtifacts.length * (fileH + rowGap) - rowGap
  const canvasH = Math.max(
    240,
    topPad + Math.max(orchH, tallestRound, tallestArtifacts) + 24,
  )

  // Edges
  const round1 = rounds[0]?.agents ?? []
  const delegationEdges = round1.map((agent) => {
    const tgt = agentPos.get(agent.session.id)!
    return {
      from: { x: orchPos.x + orchPos.w, y: orchPos.y + orchPos.h / 2 },
      to: { x: tgt.x, y: tgt.y + tgt.h / 2 },
    }
  })

  // Reuse the cross-agent collaboration inference. Compat-shape positions
  // for the helper which expects {x, y, cx, topY, bottomY, ...}.
  const compatPositions = new Map<string, GraphAgentNode>()
  for (const [id, p] of agentPos) {
    const agent = sortedAgents.find((a) => a.session.id === id)!
    compatPositions.set(id, {
      agent,
      x: p.x + p.w / 2,
      y: p.y + p.h / 2,
      w: p.w,
      h: p.h,
      cx: p.x + p.w / 2,
      topY: p.y,
      bottomY: p.y + p.h,
      color: PROFILE_HEX[agent.agentType] ?? PROFILE_HEX.general,
      published: 0,
      reads: 0,
    })
  }
  // Cap visible collab edges so a saturated swarm doesn't draw a hairball.
  // The helper already sorts by count + recency, so the slice keeps the
  // strongest signals.
  const collabEdges = inferCollaborationEdges(events, compatPositions).slice(0, 12)

  // Artifact edges
  const artifactEdges = placedArtifacts
    .map((fc) => {
      const idx = toolToAgent.get(fc.toolCallId)!
      const agent = sortedAgents[idx]
      const from = agentPos.get(agent.session.id)
      const to = artifactPos.get(fc.id)
      if (!from || !to) return null
      return {
        from: { x: from.x + from.w, y: from.y + from.h / 2 },
        to: { x: to.x, y: to.y + to.h / 2 },
        changeSet: fc,
      }
    })
    .filter((e): e is NonNullable<typeof e> => Boolean(e))

  // Bus density bins for the bottom strip
  const eventTimes = events
    .map((e) => normalizeBusTimestamp(e.timestamp))
    .filter((t) => t > 0)
  const sourceTokens =
    (source?.totalInputTokens ?? 0) + (source?.totalOutputTokens ?? 0)
  const totalAgentTokens = sortedAgents.reduce(
    (acc, a) => acc + (a.tokensIn + a.tokensOut),
    0,
  )

  return (
    <div className="flex min-h-0 flex-1 flex-col">
      {/* Header rail */}
      <div className="flex shrink-0 flex-wrap items-center gap-x-4 gap-y-1.5 hairline-b pb-3">
        <div className="flex items-center gap-2">
          <span className="font-mono text-[10px] uppercase tracking-[0.22em] text-fg-1">
            hive mind
          </span>
          <span className="font-mono text-[10px] text-fg-3">
            {sortedAgents.length} agent{sortedAgents.length === 1 ? '' : 's'} ·{' '}
            {rounds.length} round{rounds.length === 1 ? '' : 's'} ·{' '}
            {events.length} bus event{events.length === 1 ? '' : 's'} ·{' '}
            {placedArtifacts.length} artifact{placedArtifacts.length === 1 ? '' : 's'}
          </span>
        </div>
        <div className="ml-auto flex flex-wrap items-center gap-3 font-mono text-[9.5px] text-fg-3">
          <span className="flex items-center gap-1.5">
            <svg width="22" height="6" className="block">
              <path
                d="M 1 3 Q 11 -2 21 3"
                fill="none"
                stroke={ACCENT_BUS}
                strokeWidth="1.4"
              />
            </svg>
            <span>collab</span>
            <span className="text-fg-1">{collabEdges.length}</span>
          </span>
          <span className="flex items-center gap-1.5">
            <svg width="22" height="6" className="block">
              <line x1="1" y1="3" x2="21" y2="3" stroke={ACCENT_FILE} strokeWidth="1.4" />
            </svg>
            <span>artifact</span>
            <span className="text-fg-1">{placedArtifacts.length}</span>
          </span>
          <span className="flex items-center gap-1.5">
            <svg width="22" height="6" className="block">
              <line
                x1="1"
                y1="3"
                x2="21"
                y2="3"
                stroke="white"
                strokeOpacity="0.4"
                strokeWidth="1"
                strokeDasharray="3 3"
              />
            </svg>
            <span>delegation</span>
          </span>
        </div>
      </div>

      {/* Graph canvas */}
      <div className="mt-3 min-h-0 flex-1 overflow-auto">
        {sortedAgents.length === 0 ? (
          <div className="rounded-xl bg-white/[0.025] p-8 ring-hairline">
            <EmptyState
              title="No agents yet"
              body="Spawned subagents will appear as nodes connected to the orchestrator. Cyan arcs show shared findings; amber lines lead to the artifacts each agent created."
            />
          </div>
        ) : (
          <div
            className="relative"
            style={{ width: `${canvasW}px`, height: `${canvasH}px` }}
          >
            {/* Round + artifacts column headers */}
            {rounds.map((round, ri) => (
              <div
                key={`rh-${ri}`}
                className="absolute font-mono text-[10px] uppercase tracking-[0.22em]"
                style={{
                  left: leftPad + orchW + colGap + ri * (cardW + colGap),
                  top: 18,
                  width: cardW,
                }}
              >
                <span className="block text-fg-1">round {String(ri + 1).padStart(2, '0')}</span>
                <span className="mt-0.5 block text-[9px] text-fg-3">
                  {round.agents.length} agent{round.agents.length === 1 ? '' : 's'} ·{' '}
                  {relativeTime(round.startAt)}
                </span>
              </div>
            ))}
            {hasArtifacts && (
              <div
                className="absolute font-mono text-[10px] uppercase tracking-[0.22em]"
                style={{
                  left: artifactColX,
                  top: 18,
                  width: fileW,
                  color: ACCENT_FILE,
                }}
              >
                <span className="block">artifacts</span>
                <span className="mt-0.5 block text-[9px] text-fg-3">
                  {placedArtifacts.length} change{placedArtifacts.length === 1 ? '' : 's'}
                </span>
              </div>
            )}

            {/* SVG edge layer (under cards via z-index, no pointer events) */}
            <svg
              width={canvasW}
              height={canvasH}
              className="hive-edges absolute inset-0"
              style={{ pointerEvents: 'none' }}
            >
              {/* Delegation: orchestrator → round 1 agents */}
              {delegationEdges.map((edge, i) => {
                const dx = edge.to.x - edge.from.x
                const c1x = edge.from.x + Math.max(20, dx * 0.55)
                const c2x = edge.to.x - Math.max(20, dx * 0.45)
                return (
                  <path
                    key={`del-${i}`}
                    d={`M ${edge.from.x} ${edge.from.y} C ${c1x} ${edge.from.y} ${c2x} ${edge.to.y} ${edge.to.x} ${edge.to.y}`}
                    fill="none"
                    stroke="white"
                    strokeOpacity="0.22"
                    strokeWidth="1"
                    strokeDasharray="3 4"
                  />
                )
              })}

              {/* Collab arcs (cyan) — solid lines bowing out to the right
                  past the cards. Stroke width + opacity scale with how many
                  reads the pair has shared, so heavier collaborators read
                  louder than one-off pairings. */}
              {collabEdges.map((edge) => {
                const from = agentPos.get(edge.fromId)
                const to = agentPos.get(edge.toId)
                if (!from || !to) return null
                const fx = from.x + from.w
                const fy = from.y + from.h / 2
                const tx = to.x + to.w
                const ty = to.y + to.h / 2
                const reach = 28 + Math.min(36, edge.count * 5)
                const c1x = fx + reach
                const c2x = tx + reach
                const opacity = Math.min(0.62, 0.22 + edge.count * 0.05)
                const width = Math.min(2.0, 0.9 + edge.count * 0.14)
                return (
                  <path
                    key={`coll-${edge.fromId}-${edge.toId}`}
                    d={`M ${fx} ${fy} C ${c1x} ${fy} ${c2x} ${ty} ${tx} ${ty}`}
                    fill="none"
                    stroke={ACCENT_BUS}
                    strokeOpacity={opacity}
                    strokeWidth={width}
                    strokeLinecap="round"
                    className="hive-edge-collab"
                  />
                )
              })}

              {/* Artifact edges (amber) — straight-ish from agent → file */}
              {artifactEdges.map((edge, i) => {
                const dx = edge.to.x - edge.from.x
                const c1x = edge.from.x + Math.max(16, dx * 0.5)
                const c2x = edge.to.x - Math.max(16, dx * 0.5)
                return (
                  <path
                    key={`art-${i}`}
                    d={`M ${edge.from.x} ${edge.from.y} C ${c1x} ${edge.from.y} ${c2x} ${edge.to.y} ${edge.to.x} ${edge.to.y}`}
                    fill="none"
                    stroke={ACCENT_FILE}
                    strokeOpacity="0.62"
                    strokeWidth="1.2"
                  />
                )
              })}
            </svg>

            {/* Orchestrator card */}
            <div
              className="absolute"
              style={{
                left: orchPos.x,
                top: orchPos.y,
                width: orchPos.w,
                height: orchPos.h,
              }}
            >
              <OrchestratorCard
                source={source}
                agentCount={sortedAgents.length}
                tokens={sourceTokens + totalAgentTokens}
                runningCount={sortedAgents.filter((a) => a.status === 'running').length}
              />
            </div>

            {/* Agent cards */}
            {sortedAgents.map((agent) => {
              const pos = agentPos.get(agent.session.id)
              if (!pos) return null
              return (
                <div
                  key={`ag-${agent.session.id}`}
                  className="absolute"
                  style={{
                    left: pos.x,
                    top: pos.y,
                    width: pos.w,
                    height: pos.h,
                  }}
                >
                  <HiveAgentCard
                    agent={agent}
                    titlePrefix={titlePrefix}
                    onAttach={onAttach}
                  />
                </div>
              )
            })}

            {/* Artifact cards */}
            {placedArtifacts.map((fc) => {
              const pos = artifactPos.get(fc.id)
              if (!pos) return null
              return (
                <div
                  key={`fc-${fc.id}`}
                  className="absolute"
                  style={{
                    left: pos.x,
                    top: pos.y,
                    width: pos.w,
                    height: pos.h,
                  }}
                >
                  <HiveArtifactCard changeSet={fc} />
                </div>
              )
            })}
          </div>
        )}
      </div>

      {/* Event-density strip — gives time context without dominating */}
      {eventTimes.length > 0 && <EventDensityStrip times={eventTimes} />}

      {titlePrefix && (
        <div className="shrink-0 pt-2 font-mono text-[9.5px] text-fg-3/80">
          card titles elide shared prefix · "{titlePrefix.trim()}"
        </div>
      )}
    </div>
  )
}

function OrchestratorCard({
  source,
  agentCount,
  tokens,
  runningCount,
}: {
  source: SessionSnapshot | undefined
  agentCount: number
  tokens: number
  runningCount: number
}) {
  const title = cleanTitle(source?.title || source?.task || 'Mission')
  return (
    <div className="hive-card relative h-full w-full">
      <svg
        className="hive-card-svg pointer-events-none absolute inset-0 h-full w-full"
        viewBox="0 0 160 100"
        preserveAspectRatio="none"
        aria-hidden="true"
      >
        <path d={SHAPE_PATHS.rect} className="hive-card-fill" />
        <path d={SHAPE_PATHS.rect} className="hive-card-stroke" fill="none" />
      </svg>
      <div className="relative flex h-full w-full flex-col px-3 py-2.5">
        <div className="flex items-center justify-between gap-2 font-mono text-[9px] uppercase tracking-[0.18em] text-fg-3">
          <span className="text-fg-1">orchestrator</span>
          {runningCount > 0 && (
            <span className="flex items-center gap-1 text-accent">
              <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-accent" />
              {runningCount} live
            </span>
          )}
        </div>
        <h3 className="mt-2 line-clamp-3 font-prose text-[12px] font-medium leading-[1.32] text-fg-0">
          {title}
        </h3>
        <div className="mt-auto pt-2 font-mono text-[9px] text-fg-3">
          <span className="text-fg-1">{agentCount}</span> agents
          <span className="text-fg-3/40"> · </span>
          <span className="text-fg-1">{formatTokens(tokens)}</span> tokens
          {source?.model && (
            <>
              <span className="text-fg-3/40"> · </span>
              <span className="truncate text-fg-2">{source.model}</span>
            </>
          )}
        </div>
      </div>
    </div>
  )
}

function HiveAgentCard({
  agent,
  titlePrefix,
  onAttach,
}: {
  agent: AgentView
  titlePrefix: string
  onAttach: (id: string) => void
}) {
  const accent = PROFILE_HEX[agent.agentType] ?? PROFILE_HEX.general
  const tools = agent.tools.length || agent.sub?.toolsCalled || 0
  const running = agent.status === 'running'
  const canAttach = agent.attachable
  const rawTitle = agent.session.title || agent.sub?.label || agent.session.id
  const elided = titlePrefix && rawTitle.startsWith(titlePrefix)
    ? rawTitle.slice(titlePrefix.length).trim()
    : ''
  const displayTitle = cleanTitle(elided || rawTitle)
  const totalTokens = (agent.tokensIn || 0) + (agent.tokensOut || 0)
  const shape = shapeFor(agent.agentType)
  const shapePath = SHAPE_PATHS[shape]
  const inset = SHAPE_INSET[shape]

  return (
    <button
      type="button"
      onClick={() => canAttach && onAttach(agent.session.id)}
      disabled={!canAttach}
      title={canAttach ? `Attach to ${rawTitle}` : rawTitle}
      className={`hive-card hive-card-agent group relative h-full w-full text-left ${
        running ? 'is-running' : ''
      } ${canAttach ? 'is-clickable' : ''}`}
      style={{ ['--hive-accent' as string]: accent }}
    >
      {/* Shape silhouette: SVG fill + stroke (replaces border-radius/box-shadow
          so each profile family gets its own outline. preserveAspectRatio
          "none" lets the path stretch to the card box. */}
      <svg
        className="hive-card-svg pointer-events-none absolute inset-0 h-full w-full"
        viewBox="0 0 160 100"
        preserveAspectRatio="none"
        aria-hidden="true"
      >
        <path d={shapePath} className="hive-card-fill" />
        <path d={shapePath} className="hive-card-stroke" fill="none" />
      </svg>
      {/* Content overlay — per-side inset so text never crashes into a
          cut/curved edge (e.g. `code` gets extra right padding for its
          top-right notch). */}
      <div
        className="relative flex h-full w-full flex-col"
        style={{
          paddingLeft: `${inset.left}px`,
          paddingRight: `${inset.right}px`,
          paddingTop: `${inset.top}px`,
          paddingBottom: `${inset.bottom}px`,
        }}
      >
        <div className="flex items-center justify-between gap-2 font-mono text-[9px] uppercase tracking-[0.12em] text-fg-3">
          <span className="truncate" style={{ color: accent }}>
            {agent.agentType}
          </span>
          <span className={`flex items-center gap-1 ${statusTextClass(agent.status)}`}>
            <span
              className="h-1.5 w-1.5 rounded-full"
              style={{ background: statusHex(agent.status) }}
            />
            {agent.status}
          </span>
        </div>
        <h3 className="mt-1.5 line-clamp-3 font-prose text-[11.5px] font-medium leading-[1.32] text-fg-0">
          {displayTitle}
        </h3>
        <footer className="mt-auto flex items-center justify-between gap-1.5 pt-1.5 font-mono text-[9px] text-fg-3">
          <span>
            <span className="text-fg-1">{tools}</span>t
          </span>
          <span className="text-fg-3/40">·</span>
          <span>
            <span className="text-fg-1">{formatTokens(totalTokens)}</span>tk
          </span>
          <span className="text-fg-3/40">·</span>
          <span className="text-fg-1">{formatDuration(agent.elapsedMs)}</span>
        </footer>
      </div>
    </button>
  )
}

function HiveArtifactCard({ changeSet }: { changeSet: FileChangeSet }) {
  const first = changeSet.files[0]
  const filename = first?.filename ?? 'change'
  const path = first?.path ?? ''
  const op = first?.operation ?? 'update'
  const more = changeSet.files.length - 1
  return (
    <div
      className="hive-card hive-card-artifact relative h-full w-full"
      style={{ ['--hive-accent' as string]: ACCENT_FILE }}
    >
      {/* Artifact silhouette: file shape (notched corner). The amber stroke
          is the file's accent — replaces the old left-edge color bar. */}
      <svg
        className="hive-card-svg pointer-events-none absolute inset-0 h-full w-full"
        viewBox="0 0 160 100"
        preserveAspectRatio="none"
        aria-hidden="true"
      >
        <path d={SHAPE_PATHS.code} className="hive-card-fill" />
        <path
          d={SHAPE_PATHS.code}
          className="hive-card-stroke"
          fill="none"
          style={{ stroke: ACCENT_FILE, strokeOpacity: 0.55 }}
        />
      </svg>
      <div className="relative flex h-full w-full flex-col justify-center px-3 py-2">
        <div className="flex items-center gap-2">
          <svg width="10" height="12" viewBox="0 0 10 12" className="shrink-0">
            <path
              d="M 1 1 L 6 1 L 9 4 L 9 11 L 1 11 Z"
              fill="none"
              stroke={ACCENT_FILE}
              strokeOpacity="0.85"
              strokeWidth="1"
            />
            <path
              d="M 6 1 L 6 4 L 9 4"
              fill="none"
              stroke={ACCENT_FILE}
              strokeOpacity="0.6"
              strokeWidth="1"
            />
          </svg>
          <span className="truncate font-prose text-[11px] font-medium text-fg-0">
            {filename}
            {more > 0 && <span className="text-fg-3"> +{more}</span>}
          </span>
        </div>
        {path && path !== filename && (
          <div className="mt-1 truncate font-mono text-[9px] text-fg-3">{path}</div>
        )}
        <div className="mt-1 flex items-center gap-2 font-mono text-[9px]">
          <span className="uppercase" style={{ color: ACCENT_FILE, opacity: 0.85 }}>
            {op}
          </span>
          <span className="text-fg-3/40">·</span>
          <span className="text-ok">+{changeSet.totals.additions}</span>
          <span className="text-fg-3/40">/</span>
          <span className="text-danger">-{changeSet.totals.deletions}</span>
        </div>
      </div>
    </div>
  )
}

function EventDensityStrip({ times }: { times: number[] }) {
  if (times.length === 0) return null
  const t0 = Math.min(...times)
  const t1 = Math.max(...times)
  const span = Math.max(1, t1 - t0)
  const bins = 60
  const counts = new Array<number>(bins).fill(0)
  for (const t of times) {
    const idx = Math.min(bins - 1, Math.max(0, Math.floor(((t - t0) / span) * bins)))
    counts[idx]++
  }
  const max = Math.max(1, ...counts)
  return (
    <div className="mt-3 shrink-0 hairline-t pt-2.5">
      <div className="flex items-center justify-between font-mono text-[9px] uppercase tracking-[0.18em] text-fg-3">
        <span>event density</span>
        <span>
          <span className="text-fg-1">{times.length}</span> over{' '}
          <span className="text-fg-1">{formatDurationMs(span)}</span>
        </span>
      </div>
      <div className="mt-1.5 flex h-7 items-end gap-[2px]">
        {counts.map((c, i) => (
          <div
            key={i}
            className="flex-1 rounded-[1px] bg-white"
            style={{
              height: `${Math.max(6, (c / max) * 100)}%`,
              opacity: c === 0 ? 0.04 : 0.12 + (c / max) * 0.6,
            }}
            title={`${c} events`}
          />
        ))}
      </div>
    </div>
  )
}

function formatDurationMs(ms: number): string {
  if (ms <= 0) return '0s'
  if (ms < 60_000) return `${Math.round(ms / 1000)}s`
  if (ms < 3_600_000) return `${Math.floor(ms / 60_000)}m`
  if (ms < 86_400_000) return `${Math.floor(ms / 3_600_000)}h`
  return `${Math.floor(ms / 86_400_000)}d`
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
  onAttach: (id: string, mode?: 'replace' | 'split') => void
}) {
  const color = PROFILE_HEX[agent.agentType] ?? PROFILE_HEX.general
  return (
    <button
      onClick={(event) =>
        agent.attachable &&
        onAttach(
          agent.session.id,
          event.metaKey || event.ctrlKey ? 'split' : 'replace',
        )
      }
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

function MiniMetric({ label, value }: { label: string; value: string }) {
  return (
    <div className="min-w-[82px] rounded-lg bg-white/[0.03] px-3 py-2 ring-hairline">
      <div className="label text-[8.5px]">{label}</div>
      <div className="mt-1 font-mono text-[15px] leading-none text-fg-0">{value}</div>
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

function coordinationLabel(strategy?: CoordinationStrategy): string {
  if (strategy === 'kanban') return 'board'
  if (strategy === 'isolated') return 'tasks'
  if (strategy === 'goal') return 'goal'
  return 'bus'
}

function dashboardTabMeta(
  item: { id: DashboardTab; label: string; hint: string },
  strategy?: CoordinationStrategy,
): { label: string; hint: string } {
  if (strategy === 'kanban') {
    if (item.id === 'overview') return { label: 'health', hint: 'board run' }
    if (item.id === 'swarm') return { label: 'board', hint: 'cards + lanes' }
    if (item.id === 'findings') return { label: 'evidence', hint: 'findings' }
  }
  if (strategy === 'goal') {
    if (item.id === 'overview') return { label: 'goal', hint: 'judge loop' }
    if (item.id === 'swarm') return { label: 'loop', hint: 'turns + agents' }
  }
  return item
}

function collectGoalState(events: TelemetryEventView[]): GoalStateView | null {
  const goalEvents = events
    .filter((event) => event.subtype.startsWith('goal_'))
    .sort((a, b) => b.at - a.at)
  if (goalEvents.length === 0) return null
  const stateEvent = goalEvents.find((event) => event.details?.goalState)
  const state = stateEvent?.details?.goalState as Record<string, unknown> | undefined
  const lastVerdict = state?.lastVerdict as GoalStateView['lastVerdict'] | undefined
  return {
    goal: String(state?.goal ?? ''),
    status: String(state?.status ?? 'active'),
    turnsUsed: Number(state?.turnsUsed ?? 0),
    maxTurns: Number(state?.maxTurns ?? 20),
    pauseReason: typeof state?.pauseReason === 'string' ? state.pauseReason : undefined,
    lastVerdict: lastVerdict ?? null,
    events: goalEvents,
  }
}

function collectTaskCards(events: TelemetryEventView[], agents: AgentView[]): TaskCardView[] {
  const tasks = new Map<string, TaskCardView>()
  const upsert = (task: Record<string, unknown>, fallbackAt: number) => {
    const id = String(task.id ?? '')
    if (!id) return
    const existing = tasks.get(id)
    const history = mergeTaskEvents(existing?.events, normalizeTaskEvents(task.events))
    tasks.set(id, {
      id,
      title: typeof task.title === 'string' ? task.title : existing?.title ?? id,
      status: typeof task.status === 'string' ? task.status : existing?.status ?? 'todo',
      body: typeof task.body === 'string' ? task.body : existing?.body,
      assignee: typeof task.assignee === 'string' ? task.assignee : existing?.assignee,
      priority: typeof task.priority === 'number' ? task.priority : existing?.priority,
      progress: typeof task.progress === 'number' ? task.progress : existing?.progress,
      summary: typeof task.summary === 'string' ? task.summary : existing?.summary,
      result: typeof task.result === 'string' ? task.result : existing?.result,
      artifacts: normalizeStringArray(task.artifacts).length > 0
        ? normalizeStringArray(task.artifacts)
        : existing?.artifacts ?? [],
      events: history,
      agents: [],
      createdAt: typeof task.createdAt === 'number' ? task.createdAt : existing?.createdAt,
      startedAt: typeof task.startedAt === 'number' ? task.startedAt : existing?.startedAt,
      updatedAt: typeof task.updatedAt === 'number' ? task.updatedAt : existing?.updatedAt ?? fallbackAt,
      completedAt: typeof task.completedAt === 'number' ? task.completedAt : existing?.completedAt,
    })
  }

  for (const event of [...events].reverse()) {
    if (!event.subtype.startsWith('task_')) continue
    const details = event.details ?? {}
    const task = details.task
    if (task && typeof task === 'object') upsert(task as Record<string, unknown>, event.at)
    const list = details.tasks
    if (Array.isArray(list)) {
      for (const item of list) {
        if (item && typeof item === 'object') upsert(item as Record<string, unknown>, event.at)
      }
    }
  }

  for (const task of tasks.values()) {
    task.agents = agents.filter((agent) => agent.taskId === task.id)
    if (task.progress === undefined) task.progress = statusProgress(task.status)
  }
  return [...tasks.values()].sort((a, b) => {
    const order = { todo: 0, active: 1, blocked: 2, done: 3, cancelled: 4 } as Record<string, number>
    return (order[a.status] ?? 99) - (order[b.status] ?? 99) || (a.createdAt ?? 0) - (b.createdAt ?? 0)
  })
}

function normalizeTaskEvents(value: unknown): TaskCardView['events'] {
  if (!Array.isArray(value)) return []
  const normalized: NonNullable<TaskCardView['events']> = []
  for (const event of value) {
    if (!event || typeof event !== 'object') continue
    const item = event as Record<string, unknown>
    const message = typeof item.message === 'string' ? item.message : ''
    if (!message) continue
    normalized.push({
      kind: typeof item.kind === 'string' ? item.kind : undefined,
      actor: typeof item.actor === 'string' ? item.actor : undefined,
      message,
      timestamp: typeof item.timestamp === 'number' ? item.timestamp : undefined,
      details: typeof item.details === 'object' && item.details !== null ? item.details as Record<string, unknown> : undefined,
    })
  }
  return normalized
}

function mergeTaskEvents(
  a: TaskCardView['events'] = [],
  b: TaskCardView['events'] = [],
): TaskCardView['events'] {
  const seen = new Set<string>()
  const merged: NonNullable<TaskCardView['events']> = []
  for (const event of [...a, ...b]) {
    const key = `${event.timestamp ?? 0}:${event.kind ?? ''}:${event.message}`
    if (seen.has(key)) continue
    seen.add(key)
    merged.push(event)
  }
  return merged.sort((x, y) => (x.timestamp ?? 0) - (y.timestamp ?? 0))
}

function taskProgress(tasks: TaskCardView[]): number {
  if (tasks.length === 0) return 0
  const total = tasks.reduce((acc, task) => acc + (task.progress ?? statusProgress(task.status)), 0)
  return Math.round(total / tasks.length)
}

function statusProgress(status: string): number {
  if (status === 'done') return 100
  if (status === 'active') return 55
  if (status === 'blocked') return 35
  if (status === 'cancelled') return 0
  return 8
}

function taskStatusClass(status: string): string {
  if (status === 'done') return 'text-ok'
  if (status === 'active') return 'text-accent'
  if (status === 'blocked') return 'text-warn'
  if (status === 'cancelled') return 'text-danger'
  return 'text-fg-2'
}

function stripMarkdown(text: string): string {
  return text
    .replace(/[`*_#>[\]()]/g, '')
    .replace(/\s+/g, ' ')
    .trim()
}

function goalStatusTone(status: string): 'neutral' | 'accent' | 'ok' | 'warn' {
  if (status === 'done') return 'ok'
  if (status === 'paused' || status === 'cleared') return 'warn'
  if (status === 'active') return 'accent'
  return 'neutral'
}

function goalEventDot(subtype: string): string {
  if (subtype === 'goal_done') return 'bg-ok'
  if (subtype === 'goal_paused' || subtype === 'goal_error') return 'bg-warn'
  if (subtype === 'goal_continue') return 'bg-accent'
  if (subtype === 'goal_judge') return 'bg-fg-1'
  return 'bg-fg-3'
}

function collectKanbanCards(events: TelemetryEventView[], agents: AgentView[]): KanbanCardView[] {
  const cards = new Map<string, KanbanCardView>()
  const upsert = (card: Record<string, unknown>, fallbackAt: number) => {
    const id = String(card.id ?? '')
    if (!id) return
    const existing = cards.get(id)
    const comments = mergeKanbanComments(existing?.comments, normalizeKanbanComments(card.comments))
    const history = mergeKanbanEvents(existing?.events, normalizeKanbanEvents(card.events))
    const parents = normalizeStringArray(card.parents)
    const children = normalizeStringArray(card.children)
    cards.set(id, {
      id,
      title: typeof card.title === 'string' ? card.title : existing?.title ?? id,
      status: typeof card.status === 'string' ? card.status : existing?.status ?? 'todo',
      body: typeof card.body === 'string' ? card.body : existing?.body,
      assignee: typeof card.assignee === 'string' ? card.assignee : existing?.assignee,
      createdBy: typeof card.createdBy === 'string' ? card.createdBy : existing?.createdBy,
      priority: typeof card.priority === 'number' ? card.priority : existing?.priority,
      summary: typeof card.summary === 'string' ? card.summary : existing?.summary,
      result: typeof card.result === 'string' ? card.result : existing?.result,
      parents: parents.length > 0 ? parents : existing?.parents ?? [],
      children: children.length > 0 ? children : existing?.children ?? [],
      comments,
      events: history,
      agents: [],
      progress: 0,
      createdAt: typeof card.createdAt === 'number' ? card.createdAt : existing?.createdAt,
      startedAt: typeof card.startedAt === 'number' ? card.startedAt : existing?.startedAt,
      updatedAt: typeof card.updatedAt === 'number' ? card.updatedAt : existing?.updatedAt ?? fallbackAt,
      completedAt: typeof card.completedAt === 'number' ? card.completedAt : existing?.completedAt,
      consecutiveFailures:
        typeof card.consecutiveFailures === 'number'
          ? card.consecutiveFailures
          : existing?.consecutiveFailures,
      requiresVerification:
        typeof card.requiresVerification === 'boolean'
          ? card.requiresVerification
          : existing?.requiresVerification,
      spec:
        card.spec && typeof card.spec === 'object'
          ? (card.spec as KanbanCardView['spec'])
          : existing?.spec,
      metadata:
        card.metadata && typeof card.metadata === 'object'
          ? (card.metadata as Record<string, unknown>)
          : existing?.metadata,
    })
  }

  for (const event of [...events].reverse()) {
    if (!event.subtype.startsWith('kanban_')) continue
    const details = event.details ?? {}
    const task = details.task
    if (task && typeof task === 'object') {
      upsert(task as Record<string, unknown>, event.at)
    }
    const taskList = details.tasks
    if (Array.isArray(taskList)) {
      for (const entry of taskList) {
        if (!entry || typeof entry !== 'object') continue
        upsert(entry as Record<string, unknown>, event.at)
      }
    }
  }

  for (const card of cards.values()) {
    card.agents = agents.filter((agent) => agent.kanbanTaskId === card.id)
    card.progress = kanbanCardProgress(card)
  }

  return [...cards.values()].sort((a, b) => (b.updatedAt ?? 0) - (a.updatedAt ?? 0))
}

function normalizeStringArray(value: unknown): string[] {
  return Array.isArray(value) ? value.map((item) => String(item)).filter(Boolean) : []
}

function normalizeKanbanComments(value: unknown): KanbanCardView['comments'] {
  if (!Array.isArray(value)) return []
  return value
    .filter((item): item is Record<string, unknown> => Boolean(item && typeof item === 'object'))
    .map((item) => ({
      author: typeof item.author === 'string' ? item.author : undefined,
      body: String(item.body ?? ''),
      timestamp: typeof item.timestamp === 'number' ? item.timestamp : undefined,
    }))
    .filter((item) => item.body)
}

function normalizeKanbanEvents(value: unknown): KanbanCardView['events'] {
  if (!Array.isArray(value)) return []
  return value
    .filter((item): item is Record<string, unknown> => Boolean(item && typeof item === 'object'))
    .map((item) => ({
      kind: typeof item.kind === 'string' ? item.kind : undefined,
      actor: typeof item.actor === 'string' ? item.actor : undefined,
      message: typeof item.message === 'string' ? item.message : undefined,
      timestamp: typeof item.timestamp === 'number' ? item.timestamp : undefined,
      details: item.details && typeof item.details === 'object'
        ? item.details as Record<string, unknown>
        : undefined,
    }))
}

function mergeKanbanComments(
  existing: KanbanCardView['comments'] = [],
  next: KanbanCardView['comments'] = [],
): KanbanCardView['comments'] {
  const merged = new Map<string, NonNullable<KanbanCardView['comments']>[number]>()
  for (const item of [...existing, ...next]) {
    merged.set(`${item.author ?? ''}:${item.timestamp ?? 0}:${item.body}`, item)
  }
  return [...merged.values()].sort((a, b) => (a.timestamp ?? 0) - (b.timestamp ?? 0))
}

function mergeKanbanEvents(
  existing: KanbanCardView['events'] = [],
  next: KanbanCardView['events'] = [],
): KanbanCardView['events'] {
  const merged = new Map<string, NonNullable<KanbanCardView['events']>[number]>()
  for (const item of [...existing, ...next]) {
    merged.set(`${item.kind ?? ''}:${item.actor ?? ''}:${item.timestamp ?? 0}:${item.message ?? ''}`, item)
  }
  return [...merged.values()].sort((a, b) => (a.timestamp ?? 0) - (b.timestamp ?? 0))
}

function kanbanProgress(cards: KanbanCardView[]): number {
  if (cards.length === 0) return 0
  const total = cards.reduce((acc, card) => acc + kanbanCardProgress(card), 0)
  return Math.round(total / cards.length)
}

// Matches the bridge's `KANBAN_STALE_SECONDS` constant — keep in sync.
// At STALE_SECONDS the dispatcher emits a `kanban_stale` event; at
// RECLAIM_SECONDS it reclaims the card to `crashed`. Showing the halo
// from half the stale threshold gives the user advance warning.
const KANBAN_STALE_WARN_SECONDS = 90
const KANBAN_STALE_HARD_SECONDS = 180

function KanbanVerifyOnCompletePill({ card }: { card: KanbanCardView }) {
  // Only surface the pill while the card is still pre-verification.
  // After it lands in `done_unverified`/`done` the verdict badge takes
  // over and this pill would be redundant noise.
  if (!card.requiresVerification) return null
  if (
    card.status === 'done' ||
    card.status === 'done_unverified' ||
    card.status === 'cancelled' ||
    card.status === 'failed'
  ) {
    return null
  }
  return (
    <span
      className="rounded-full bg-warn/[0.08] px-1.5 py-0.5 font-mono text-[8.5px] uppercase text-warn/85 ring-1 ring-warn/20"
      title="Parent marked this card as requiring verification — complete will route to the verifier instead of sealing directly to done."
    >
      verify on complete
    </span>
  )
}

function KanbanRetryPill({ card }: { card: KanbanCardView }) {
  const failures = card.consecutiveFailures ?? 0
  if (failures <= 0) return null
  // Bridge threshold is 3 — one more failure trips the breaker. Mark
  // the last attempt with red text so it's unmissable.
  const lastAttempt = failures >= 2
  const cls = lastAttempt
    ? 'bg-danger/[0.18] text-danger ring-1 ring-danger/40'
    : 'bg-warn/[0.12] text-warn ring-1 ring-warn/25'
  return (
    <span
      className={`rounded-full px-1.5 py-0.5 font-mono text-[8.5px] uppercase ${cls}`}
      title={`${failures}/3 failures — circuit breaker trips on next failure`}
    >
      retry {failures}/3
    </span>
  )
}

function formatLivenessAge(seconds: number): string {
  if (seconds < 60) return `${seconds}s`
  const minutes = Math.floor(seconds / 60)
  if (minutes < 60) return `${minutes}m`
  const hours = Math.floor(minutes / 60)
  return `${hours}h`
}

function KanbanLivenessStrip({
  card,
  palette,
}: {
  card: KanbanCardView
  palette: { meta: string }
}) {
  const [now, setNow] = useState(Date.now())
  useEffect(() => {
    // Re-render every 5s so the age string and dot pulse rate stay
    // current without burning cycles when nothing's changed.
    const handle = window.setInterval(() => setNow(Date.now()), 5000)
    return () => window.clearInterval(handle)
  }, [])
  const lastSignal = card.updatedAt ?? card.startedAt
  if (!lastSignal) return null
  const ageMs = Math.max(0, now - lastSignal)
  const ageSeconds = Math.floor(ageMs / 1000)
  const isWarn = ageSeconds >= KANBAN_STALE_WARN_SECONDS
  const isStale = ageSeconds >= KANBAN_STALE_HARD_SECONDS
  const dotCls = isStale
    ? 'bg-warn'
    : isWarn
      ? 'bg-warn/70'
      : 'bg-accent'
  const labelCls = isStale
    ? 'text-warn'
    : isWarn
      ? 'text-warn/85'
      : palette.meta
  return (
    <div
      className={`relative z-10 mt-2 flex items-center gap-1.5 font-mono text-[9px] ${labelCls}`}
    >
      <span className="relative inline-flex h-1.5 w-1.5">
        {!isStale && (
          <span className={`absolute inset-0 animate-ping rounded-full ${dotCls} opacity-60`} />
        )}
        <span className={`relative inline-block h-1.5 w-1.5 rounded-full ${dotCls}`} />
      </span>
      <span>heartbeat {formatLivenessAge(ageSeconds)}</span>
      {isStale && <span className="ml-1 uppercase">stale</span>}
    </div>
  )
}

function KanbanTokenBurnBar({ card }: { card: KanbanCardView }) {
  if (card.status !== 'running') return null
  const budget = card.spec?.token_budget
  if (!budget || budget <= 0) return null
  // We don't yet thread per-card token usage from the sub-agent runner
  // into the card view, so render the bar at 0% as a known-empty
  // placeholder. The bar still communicates "this card has a budget"
  // to the operator; we'll wire actual usage in a follow-up commit.
  const used = 0
  const pct = Math.min(100, Math.round((used / budget) * 100))
  const tone =
    pct >= 100 ? 'bg-danger/65' : pct >= 80 ? 'bg-warn/70' : 'bg-accent/55'
  return (
    <div className="relative z-10 mt-1 flex items-center gap-1.5">
      <div className="flex-1 h-1 overflow-hidden rounded-full bg-black/40 shadow-[inset_0_0_0_1px_rgba(255,255,255,0.04)]">
        <div className={`h-full ${tone}`} style={{ width: `${pct}%` }} />
      </div>
      <span className="font-mono text-[8.5px] uppercase tracking-[0.06em] text-fg-3">
        {Math.floor(used / 1000)}k/{Math.floor(budget / 1000)}k
      </span>
    </div>
  )
}

function kanbanRestartCount(events: TelemetryEventView[]): number {
  // Reads `kanban_replay` system events — the bridge emits one on each
  // session init whose journal had prior events. The most recent
  // event's `restartCount` is the source of truth.
  for (let i = events.length - 1; i >= 0; i--) {
    const event = events[i]
    if (event.subtype !== 'kanban_replay') continue
    const details = (event.details ?? {}) as Record<string, unknown>
    const count = details.restartCount
    if (typeof count === 'number') return count
  }
  return 0
}

function detectVerifierRejection(
  card: KanbanCardView,
): { text: string; actor?: string } | null {
  // A verifier rejection lives in the comment the verifier left when
  // it transitioned the card back to `running`. Find the most recent
  // verifier-authored comment on a card that's currently `running`
  // and has a prior `done_unverified -> running` transition in its
  // event tail.
  if (card.status !== 'running') return null
  const comments = (card.comments ?? []).slice().reverse()
  for (const comment of comments) {
    const author = (comment.author || '').toLowerCase()
    if (author.includes('verify')) {
      return { text: comment.body, actor: comment.author }
    }
  }
  return null
}

function detectVerifierVerdict(
  card: KanbanCardView,
): { kind: 'verified' | 'rejected'; actor?: string } | null {
  // Walk events newest-first. We classify the most recent verifier-driven
  // transition:
  //   • running card whose latest verifier event was an "updated -> running"
  //     is one the verifier just rejected.
  //   • done card whose latest verifier event was an "updated -> done" is
  //     one the verifier just sealed.
  const events = (card.events ?? []).slice().reverse()
  for (const event of events) {
    const actor = (event.actor || '').toLowerCase()
    const message = (event.message || '').toLowerCase()
    const detailsRaw = event.details
    const details =
      detailsRaw && typeof detailsRaw === 'object' ? (detailsRaw as Record<string, unknown>) : {}
    const nextStatus =
      typeof details.status === 'string' ? details.status.toLowerCase() : ''
    const isVerifier = actor.includes('verify') || message.includes('verify')
    if (!isVerifier) continue
    if (card.status === 'done' && nextStatus === 'done') {
      return { kind: 'verified', actor: event.actor }
    }
    if (card.status === 'running' && nextStatus === 'running') {
      return { kind: 'rejected', actor: event.actor }
    }
  }
  return null
}

function KanbanVerdictBadge({ card }: { card: KanbanCardView }) {
  const verdict = detectVerifierVerdict(card)
  if (!verdict) return null
  if (verdict.kind === 'verified') {
    return (
      <span
        className="rounded-full bg-ok/[0.12] px-1.5 py-0.5 font-mono text-[8.5px] uppercase text-ok ring-1 ring-ok/25"
        title={verdict.actor ? `verified by ${verdict.actor}` : 'verified'}
      >
        ✓ verified
      </span>
    )
  }
  return (
    <span
      className="rounded-full bg-warn/[0.12] px-1.5 py-0.5 font-mono text-[8.5px] uppercase text-warn ring-1 ring-warn/25"
      title="verifier rejected — see latest comment for feedback"
    >
      ✗ rejected
    </span>
  )
}

function kanbanCardProgress(card: KanbanCardView): number {
  if (card.status === 'done') return 100
  if (card.status === 'cancelled') return 100
  if (card.status === 'running') return 62
  if (card.status === 'blocked') return 35
  if (card.status === 'ready') return 18
  return 4
}

function latestKanbanActivity(card: KanbanCardView): { label: string; text: string } | null {
  const comments = (card.comments ?? []).map((comment) => ({
    at: comment.timestamp ?? 0,
    label: comment.author || 'comment',
    text: comment.body,
  }))
  const events = (card.events ?? []).map((event) => ({
    at: event.timestamp ?? 0,
    label: event.actor || event.kind || 'event',
    text: event.message || '',
  }))
  const latest = [...comments, ...events].sort((a, b) => b.at - a.at)[0]
  return latest ? { label: latest.label, text: latest.text } : null
}

function kanbanPriorityClass(priority: number): string {
  if (priority <= 1) return 'text-danger'
  if (priority === 2) return 'text-warn'
  return 'text-fg-3'
}

function kanbanColumnClass(status: string): string {
  // Done keeps the soft green tint as the only completed affordance.
  // Other columns stay material-led, not traffic-light colored.
  if (status === 'done') {
    return 'border border-ok/[0.14] bg-[linear-gradient(180deg,rgba(112,184,103,0.032),rgba(0,0,0,0.19))]'
  }
  if (status === 'todo' || status === 'triage') {
    return 'border border-dashed border-white/[0.11] bg-[linear-gradient(180deg,rgba(139,160,165,0.035),rgba(0,0,0,0.13))]'
  }
  if (status === 'running') {
    return 'border border-white/[0.11] bg-[linear-gradient(180deg,rgba(255,255,255,0.030),rgba(0,0,0,0.22))]'
  }
  return 'border border-white/[0.09] bg-[linear-gradient(180deg,rgba(255,255,255,0.022),rgba(0,0,0,0.17))]'
}

function kanbanCardMaterialClass(status: string, selected: boolean, selectable: boolean): string {
  // Selection ring is white instead of accent-blue — keeps the board
  // monochrome and stops every focused card from popping a coloured halo.
  const base = [
    'relative overflow-hidden p-3 transition focus:outline-none',
    selectable ? 'cursor-pointer hover:-translate-y-px' : '',
    selected
      ? 'ring-1 ring-white/55 shadow-[0_0_0_1px_rgba(255,255,255,0.18),0_18px_44px_rgba(0,0,0,0.34)]'
      : 'ring-1',
  ].join(' ')

  // Done is the one card style that stays green-tinted.
  if (status === 'done') {
    return `${base} rounded-md border bg-[linear-gradient(180deg,rgba(85,116,80,0.24),rgba(26,32,25,0.54)_42%,rgba(8,11,8,0.72))] pt-12 shadow-[inset_0_1px_0_rgba(255,255,255,0.10),inset_0_-1px_0_rgba(112,184,103,0.16),0_12px_22px_rgba(0,0,0,0.30)] ${
      selected ? 'border-white/35' : 'border-ok/25 ring-ok/18'
    }`
  }
  if (status === 'cancelled') {
    return `${base} rounded-[5px] border border-white/[0.10] bg-[linear-gradient(180deg,rgba(255,255,255,0.020),rgba(0,0,0,0.25))] opacity-65 ring-white/10`
  }
  if (status === 'todo' || status === 'triage') {
    return `${base} rounded-[7px] border border-[#6f7c82]/48 bg-[linear-gradient(180deg,#c0cbd0_0%,#b1bec4_58%,#9eabb1_100%)] pl-8 shadow-[0_1px_0_rgba(255,255,255,0.45),0_9px_20px_-8px_rgba(0,0,0,0.54),inset_0_1px_0_rgba(255,255,255,0.36),inset_0_-1px_0_rgba(0,0,0,0.12)] ${
      selected ? 'ring-2 ring-[#172023]/65' : 'ring-0'
    }`
  }
  if (status === 'ready') {
    // Punched index-card material: warm cream paper, soft inset highlight at
    // the top edge, slight darker fold at the bottom. Drop shadow gives the
    // card lift over the dark board. The selection ring darkens to a neutral
    // ink on paper so it doesn't read as a coloured halo.
    return `${base} pl-12 rounded-md border border-[#9da08e]/50 bg-[linear-gradient(180deg,#e0e3d3_0%,#d7dac9_56%,#cbcebd_100%)] shadow-[0_1px_0_rgba(255,255,255,0.55),0_2px_2px_rgba(0,0,0,0.16),0_10px_22px_-6px_rgba(0,0,0,0.40),inset_0_1px_0_rgba(255,255,255,0.46),inset_0_-1px_0_rgba(0,0,0,0.09)] ${
      selected ? 'ring-2 ring-[#1a1c12]/60' : 'ring-0'
    }`
  }
  if (status === 'running') {
    return `${base} rounded-xl border bg-[linear-gradient(145deg,rgba(116,122,119,0.28),rgba(39,42,41,0.66)_46%,rgba(8,9,9,0.82))] pt-12 shadow-[inset_0_1px_0_rgba(255,255,255,0.16),inset_0_-10px_20px_rgba(0,0,0,0.34),0_18px_34px_rgba(0,0,0,0.38)] ${
      selected ? 'border-white/38 ring-white/38' : 'border-white/18 ring-white/16'
    }`
  }
  if (status === 'done_unverified') {
    // Amber tint sits between `running` dark and `done` green — the card
    // is past worker activity but not yet sealed by the verifier.
    return `${base} rounded-xl border bg-[linear-gradient(145deg,rgba(184,135,55,0.22),rgba(54,42,22,0.62)_46%,rgba(18,14,8,0.78))] pt-12 shadow-[inset_0_1px_0_rgba(255,213,140,0.16),inset_0_-10px_20px_rgba(0,0,0,0.30),0_18px_30px_rgba(0,0,0,0.34)] ${
      selected ? 'border-warn/55 ring-warn/45' : 'border-warn/30 ring-warn/20'
    }`
  }
  if (status === 'blocked') {
    return `${base} rounded-[3px] border bg-[linear-gradient(135deg,rgba(255,255,255,0.05),rgba(255,255,255,0.02)_45%,rgba(0,0,0,0.16))] shadow-[inset_0_0_0_1px_rgba(255,255,255,0.06),0_12px_28px_rgba(0,0,0,0.30)] backdrop-blur-sm ${
      selected ? 'border-white/35 ring-white/35' : 'border-white/18 ring-white/12'
    }`
  }
  if (status === 'crashed') {
    // Recoverable amber: warmer than done_unverified, slightly cooler than
    // timed_out. Signals "the worker died, the breaker hasn't tripped yet".
    return `${base} rounded-md border bg-[linear-gradient(145deg,rgba(196,118,72,0.22),rgba(44,28,18,0.62)_46%,rgba(16,10,6,0.78))] shadow-[inset_0_1px_0_rgba(255,205,160,0.16),0_14px_28px_rgba(0,0,0,0.34)] ${
      selected ? 'border-warn/55 ring-warn/40' : 'border-warn/35 ring-warn/22'
    }`
  }
  if (status === 'timed_out') {
    // Budget-exceeded peach: cooler than crashed so the operator can tell
    // them apart at a glance. The retry pill carries the actionable detail.
    return `${base} rounded-md border bg-[linear-gradient(145deg,rgba(168,116,108,0.22),rgba(38,24,22,0.60)_46%,rgba(14,8,8,0.76))] shadow-[inset_0_1px_0_rgba(245,200,190,0.16),0_14px_28px_rgba(0,0,0,0.34)] ${
      selected ? 'border-warn/55 ring-warn/40' : 'border-warn/30 ring-warn/20'
    }`
  }
  if (status === 'failed') {
    // Circuit-broken red: visually distinct from cancelled (dimmed). The
    // ring is the warning signal — "do not auto-respawn".
    return `${base} rounded-md border-2 bg-[linear-gradient(145deg,rgba(150,52,52,0.24),rgba(48,16,16,0.58)_46%,rgba(14,6,6,0.78))] shadow-[inset_0_1px_0_rgba(255,150,150,0.14),0_14px_30px_rgba(0,0,0,0.36)] ${
      selected ? 'border-danger/75 ring-danger/55' : 'border-danger/55 ring-danger/35'
    }`
  }
  return `${base} rounded-lg bg-black/24 ring-white/10`
}

function kanbanProgressTrackClass(status: string): string {
  if (status === 'todo' || status === 'triage') return 'rounded-[2px] bg-[#10191d]/15 shadow-[inset_0_0_0_1px_rgba(0,0,0,0.10)]'
  // Ready cards are paper, so the track needs to be ink-on-cream: a dark
  // hairline well sunk into the paper instead of a white track.
  if (status === 'ready') return 'rounded-[2px] bg-[#0e0f08]/15 shadow-[inset_0_0_0_1px_rgba(0,0,0,0.10)]'
  if (status === 'running' || status === 'done_unverified') return 'rounded-[2px] bg-black/55 shadow-[inset_0_0_0_1px_rgba(255,255,255,0.10)]'
  if (status === 'done') return 'rounded-[1px] bg-black/50'
  if (status === 'blocked') return 'rounded-[1px] bg-white/10'
  if (status === 'crashed' || status === 'timed_out') return 'rounded-[1px] bg-black/40 shadow-[inset_0_0_0_1px_rgba(255,180,140,0.12)]'
  if (status === 'failed') return 'rounded-[1px] bg-black/40 shadow-[inset_0_0_0_1px_rgba(220,90,90,0.20)]'
  return 'rounded-full bg-white/10'
}

function kanbanProgressFillClass(status: string, blocked: boolean): string {
  // Done is the only status that lights up green; everything else gets
  // a neutral bar so the board stays monochrome. Ready uses dark ink so
  // the bar reads against the cream paper.
  if (status === 'done') return 'rounded-[1px] bg-ok/75 shadow-[0_0_14px_rgba(112,184,103,0.22)]'
  if (status === 'todo' || status === 'triage') return 'rounded-[1px] bg-[#10191d]/55'
  if (status === 'ready') return 'rounded-[1px] bg-[#0e0f08]/65'
  if (status === 'done_unverified') return 'rounded-[1px] bg-warn/70 shadow-[0_0_10px_rgba(217,162,73,0.18)]'
  if (status === 'crashed' || status === 'timed_out') return 'rounded-[1px] bg-warn/55'
  if (status === 'failed') return 'rounded-[1px] bg-danger/65'
  if (blocked || status === 'cancelled') return 'rounded-[1px] bg-white/40'
  if (status === 'running') return 'rounded-[1px] bg-white/75'
  return 'rounded-full bg-white/55'
}

function kanbanStatusClass(status: string): string {
  // Done is the only status that gets a colour. Everything else stays in
  // the neutral text ramp so the board reads as monochrome with a single
  // green "shipped" emphasis.
  if (status === 'done') return 'text-ok'
  if (status === 'done_unverified') return 'text-warn'
  if (status === 'failed' || status === 'crashed' || status === 'timed_out') return 'text-danger'
  if (status === 'blocked' || status === 'cancelled') return 'text-fg-2'
  if (status === 'running') return 'text-fg-1'
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
