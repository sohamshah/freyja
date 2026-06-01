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
import { TasksListRailView } from './views/TasksListRailView'
import { GoalStudioView } from './views/GoalStudioView'
import { KanbanBridgeView } from './views/KanbanBridgeView'
import { BusFlowView } from './views/BusFlowView'
import { JudgeBrief } from './views/JudgeBrief'
import { DispatcherBrief } from './views/DispatcherBrief'
import { ActivityView } from './views/ActivityView'
import { ScheduledJobsDashboard } from './ScheduledJobsDashboard'

// 'swarm' / 'findings' / 'telemetry' kept for legacy callers; they all
// redirect to the corresponding live tab below.
type DashboardTab =
  | 'overview'
  | 'tasks'
  | 'activity'
  | 'profiles'
  | 'swarm'
  | 'findings'
  | 'telemetry'
  | 'scheduler'

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
  // Move R — default-on judge-review pipeline. `reviewIteration`
  // counts rework cycles (worker → review → judge fail → worker).
  // Sticky session ids for the worker and judge stay pinned across
  // cycles so continuity is preserved. `workerTerminalState` records
  // what state the worker exited at (done / failed / cancelled /
  // crashed / timed_out) so the UI can show whether a card in review
  // is being judged on a clean delivery or a partial crash dump.
  reviewIteration?: number
  workerSessionId?: string
  judgeSessionId?: string
  workerTerminalState?: string
  /** Artifact file paths produced by the worker. Reducer mirrors this
   *  from each kanban_* event's `details.task.artifacts` so the card UI
   *  can show a count chip + the drawer can list the file paths. */
  artifacts?: string[]
  // Move D — populated when the specifier has filled in structured fields.
  spec?: {
    definition_of_done?: string[]
    references?: { files?: string[]; findings?: string[]; cards?: string[] }
    verify_with?: string
    token_budget?: number
  }
  metadata?: Record<string, unknown>
}

interface GoalVerdictPayload {
  done?: boolean
  reason?: string
  confidence?: number
  criteria?: Array<{
    id: string
    text: string
    priority: 'must' | 'should' | 'may'
    status: 'met' | 'partial' | 'missing'
    note?: string
  }>
  openQuestions?: string[]
  judgeSessionId?: string | null
  fallbackFrom?: string | null
}

interface CalibratorMetaPayload {
  model: string
  ranAt: number
  version: number
  confidence: number
  rationaleOverall: string
  rationaleByField: Record<string, string>
  calibratorSetFields: string[]
  sessionId?: string | null
}

interface JudgeRulesPayload {
  voice: string
  rigorScore: number
  judgeProfile: 'skip' | 'quick' | 'standard' | 'deep'
  criteria: Array<{ id: string; text: string; priority: 'must' | 'should' | 'may' }>
  neverDo: string[]
  whenToStop: string
  judgeTools?: string[]
  calibratorMeta?: CalibratorMetaPayload | null
  updatedAt?: number
}

interface CalibrationStatusView {
  status: 'idle' | 'running' | 'applied' | 'proposed' | 'failed'
  sessionId?: string | null
  model?: string
  reason?: string
  at?: number
  errorMessage?: string
  proposal?: JudgeRulesPayload | null
  willApplyAutomatically?: boolean
}

interface GoalStateView {
  goal: string
  status: string
  turnsUsed: number
  pauseReason?: string
  lastVerdict?: GoalVerdictPayload | null
  judgeRules?: JudgeRulesPayload | null
  judgeRulesProposal?: JudgeRulesPayload | null
  calibration?: CalibrationStatusView | null
  verdictHistory?: GoalVerdictPayload[]
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
  { id: 'tasks', label: 'tasks', hint: 'planning ledger' },
  { id: 'activity', label: 'activity', hint: 'session timeline' },
  { id: 'profiles', label: 'profiles', hint: 'subagents' },
  { id: 'scheduler', label: 'scheduler', hint: 'jobs & loops' },
]

function visibleDashboardTabs(
  strategy?: CoordinationStrategy,
): Array<{ id: DashboardTab; label: string; hint: string }> {
  // In isolated mode the `overview` tab IS the tasks rail (OverviewTab
  // short-circuits to TasksListRailView when the strategy matches). A
  // standalone `tasks` tab would render the exact same component, so we
  // hide it here. Every other mode keeps the dedicated tasks tab as the
  // home for the parent's private planning ledger.
  if (strategy === 'isolated') return TABS.filter((t) => t.id !== 'tasks')
  return TABS
}

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

// Monochrome profile palette: agents are identified by name, not color.
// Steel-blue accent is reserved for the active/focused state.
const PROFILE_HEX: Record<string, string> = {
  general: '#a8a8a8',
  explore: '#a8a8a8',
  'explore-fast': '#a8a8a8',
  code: '#a8a8a8',
  verify: '#a8a8a8',
  plan: '#a8a8a8',
  review: '#a8a8a8',
  test: '#a8a8a8',
  'browser-qa': '#a8a8a8',
  performance: '#a8a8a8',
  docs: '#a8a8a8',
  'memory-curator': '#a8a8a8',
  computer: '#a8a8a8',
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
  const kanbanCardsSnapshot = useHarness((s) => s.kanbanCards)
  const busMessages = useHarness((s) => s.busMessages)
  const inboxEvents = useHarness((s) => s.inboxEvents)
  const artifacts = useHarness((s) => s.artifacts)
  const widgets = useHarness((s) => s.widgets)
  const autoDispatchEnabled = useHarness((s) => s.autoDispatchEnabled)
  const model = useHarness((s) => s.model)
  const reasoningLevel = useHarness((s) => s.reasoningLevel)
  const coordinationStrategy = useHarness((s) => s.coordinationStrategy)
  const runtime = useHarness((s) => s.runtime)
  const harnessSessionId = useHarness((s) => s.harnessSessionId)
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
      kanbanCards: kanbanCardsSnapshot,
      busMessages,
      inboxEvents,
      artifacts,
      widgets,
      autoDispatchEnabled,
      model,
      reasoningLevel,
      coordinationStrategy,
      runtime,
      harnessSessionId,
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

    // Inbox events from every slice in the swarm. Each event carries
    // the RECIPIENT's session id; the FROM session is on .fromSession.
    // Dedupe by message id (each enqueue is unique by uuid); time-sort
    // ascending so the activity view can render them chronologically
    // for the swim-row visualization. We don't filter by action here —
    // the comm visual cares about enqueued events, but the activity
    // tab may want others.
    const inboxEventsAggregated = dedupeBy(
      slices.flatMap(({ slice }) => slice.inboxEvents),
      (event) => `${event.id}:${event.action}`,
    ).sort((a, b) => a.timestamp - b.timestamp)

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
    // Durable per-card snapshots from every slice we're aggregating.
    // Pre-seed the collector with these so cards from earlier in a
    // long session don't vanish once their original kanban_* event
    // rolls out of the 100-entry systemEvents buffer.
    const kanbanSnapshotSeed: Record<string, Record<string, unknown>> = {}
    for (const { slice } of slices) {
      const slicSnap = slice.kanbanCards
      if (!slicSnap) continue
      for (const id in slicSnap) {
        kanbanSnapshotSeed[id] = slicSnap[id]
      }
    }
    const kanbanCards = collectKanbanCards(systemEventViews, agents, kanbanSnapshotSeed)
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
      sessions,
      busEvents,
      findings: busEvents.filter((event) => event.topic !== 'read'),
      readEvents: busEvents.filter((event) => event.topic === 'read'),
      inboxEvents: inboxEventsAggregated,
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
      // Mirror the active session's autopilot flag here too so consumers
      // downstream (KanbanBridgeView's toggle) read the same value the
      // store reducer updates on `kanban_autopilot_enabled/_disabled`.
      autoDispatchEnabled,
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
    autoDispatchEnabled,
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
    <div className="fixed inset-0 z-50 flex flex-col bg-bg-0">
      <header className="relative flex shrink-0 items-center gap-5 border-b border-white/[0.06] py-3 pl-[88px] pr-5">
        <div className="flex min-w-0 items-center gap-3 font-mono text-[10.5px] uppercase tracking-[0.14em] text-fg-3">
          <span className="text-fg-1">mission dashboard</span>
          <span className="h-3 w-px bg-white/[0.10]" />
          <StatusPill status={missionStatus} />
        </div>
        <nav className="ml-2 hidden items-center gap-0.5 lg:flex">
          {visibleDashboardTabs(dashboard.coordinationStrategy).map((item) => {
            const itemMeta = dashboardTabMeta(item, dashboard.coordinationStrategy)
            const active = tab === item.id
            return (
              <button
                key={item.id}
                onClick={() => setTab(item.id)}
                className={`rounded-md px-3 py-1.5 font-mono text-[11px] tracking-[0.04em] transition ${
                  active
                    ? 'bg-accent/[0.08] text-accent ring-1 ring-accent/[0.22]'
                    : 'text-fg-2 hover:bg-white/[0.04] hover:text-fg-0'
                }`}
              >
                {itemMeta.label}
              </button>
            )
          })}
        </nav>
        <span className="flex-1" />
        {dashboard.missionSession ? (
          <span className="hidden truncate font-mono text-[10px] text-fg-3 lg:inline">
            {dashboard.missionSession.id.slice(0, 12)}
          </span>
        ) : null}
        <button
          onClick={() => toggleDashboard(false)}
          className="rounded-md border border-white/[0.06] bg-white/[0.03] px-3 py-1.5 font-mono text-[10px] uppercase tracking-[0.18em] text-fg-2 transition hover:bg-white/[0.07] hover:text-fg-0"
        >
          esc · close
        </button>
      </header>

      <div className="relative flex min-h-0 flex-1 flex-col overflow-hidden px-5 pb-5">
        <nav className="flex gap-1 py-3 lg:hidden">
          {visibleDashboardTabs(dashboard.coordinationStrategy).map((item) => {
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
            busEvents={dashboard.busEvents}
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
            autoDispatchEnabled={dashboard.autoDispatchEnabled}
            onTab={setTab}
            onAttach={attach}
            onJumpTool={closeAndJumpToTool}
            onCopyFinding={(event) => copyFinding(event, showToast)}
          />
        )}
        {(tab === 'activity' || tab === 'findings' || tab === 'telemetry') && (
          <ActivityView
            findings={dashboard.findings}
            readEvents={dashboard.readEvents}
            telemetryEvents={dashboard.telemetryEvents}
            agents={dashboard.agents}
            sessions={dashboard.sessions}
            inboxEvents={dashboard.inboxEvents}
            onCopyFinding={(event) => copyFinding(event, showToast)}
          />
        )}
        {tab === 'tasks' && (
          <TasksListRailView
            objective={dashboard.objective}
            tasks={dashboard.taskCards}
            agents={dashboard.agents}
            events={dashboard.telemetryEvents}
            contextPct={contextPct}
            cost={dashboard.cost}
            onAttach={attach}
          />
        )}
        {tab === 'profiles' && <ProfilesTab />}
        {tab === 'scheduler' && <ScheduledJobsDashboard />}
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
  busEvents,
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
  autoDispatchEnabled,
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
  /** Full bus event stream (findings + reads). BusFlowView needs reads
   *  too — `findings` alone is pre-filtered to topic !== 'read' and
   *  would silently strip the read overlay. */
  busEvents: BusEventView[]
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
  autoDispatchEnabled: boolean
  onTab: (tab: DashboardTab) => void
  onAttach: (id: string, mode?: 'replace' | 'split') => void
  onJumpTool: (id: string) => void
  onCopyFinding: (event: BusEventView) => void
}) {
  const compactions = telemetryEvents.filter((event) => event.subtype === 'compaction_complete').length
  const mediaPrunes = telemetryEvents.filter((event) => event.subtype === 'media_pruning').length
  const [judgeBriefOpen, setJudgeBriefOpen] = useState(false)
  const [dispatcherBriefOpen, setDispatcherBriefOpen] = useState(false)

  if (coordinationStrategy === 'kanban') {
    return (
      <>
        <KanbanBridgeView
          sessionId={sessionId}
          objective={objective}
          cards={kanbanCards}
          agents={agents}
          telemetryEvents={telemetryEvents}
          contextPct={contextPct}
          cost={cost}
          autoDispatchEnabled={autoDispatchEnabled}
          onAttach={onAttach}
          onOpenDispatcherBrief={() => setDispatcherBriefOpen(true)}
        />
        <DispatcherBrief
          open={dispatcherBriefOpen}
          onClose={() => setDispatcherBriefOpen(false)}
          cards={kanbanCards}
          agents={agents}
          objective={objective}
        />
      </>
    )
  }
  if (coordinationStrategy === 'goal') {
    return (
      <>
        <GoalStudioView
          goalState={goalState}
          agents={agents}
          contextPct={contextPct}
          cost={cost}
          onOpenJudgeBrief={() => setJudgeBriefOpen(true)}
        />
        <JudgeBrief open={judgeBriefOpen} onClose={() => setJudgeBriefOpen(false)} goalState={goalState} />
      </>
    )
  }
  if (coordinationStrategy === 'isolated') {
    return (
      <TasksListRailView
        objective={objective}
        tasks={taskCards}
        agents={agents}
        events={telemetryEvents}
        contextPct={contextPct}
        cost={cost}
        onAttach={onAttach}
      />
    )
  }
  if (coordinationStrategy === 'bus') {
    // Pass the *unfiltered* bus event stream — BusFlowView splits it
    // internally into published findings (chips on the timeline) and
    // read events (arcs back to the source). The `findings` collection
    // above is pre-filtered to `topic !== 'read'`, which silently
    // erased the read overlay and made the lane count "0 reads".
    return (
      <BusFlowView
        objective={objective}
        agents={agents}
        findings={busEvents}
        contextPct={contextPct}
        cost={cost}
        onAttach={onAttach}
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
        <PanelHeader label="agent lanes" />
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
          <span className="font-mono text-[10px] uppercase tracking-[0.14em] text-fg-1">
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
                className="absolute font-mono text-[10px] uppercase tracking-[0.14em]"
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
                className="absolute font-mono text-[10px] uppercase tracking-[0.14em]"
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
  if (topic === 'errors') return '#b48282'
  if (topic === 'findings') return '#a8d4fc'
  if (topic === 'progress') return '#a8b0a8'
  return '#8a9491'
}

function statusHex(status: AgentView['status']): string {
  if (status === 'failed' || status === 'cancelled') return '#b48282'
  if (status === 'done') return '#a8b0a8'
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
    if (item.id === 'overview') return { label: 'board', hint: 'cards · lanes · dispatch' }
    if (item.id === 'findings') return { label: 'evidence', hint: 'findings' }
  }
  if (strategy === 'goal') {
    if (item.id === 'overview') return { label: 'studio', hint: 'judge · draft · loop' }
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
  // Pull the most recent judge rules + verdict history from any event that
  // carries them. _emit_goal_event includes both in every payload so any
  // goal_* event will do; the sort puts the newest first.
  const rulesEvent = goalEvents.find((event) => event.details?.judgeRules)
  const judgeRules = (rulesEvent?.details?.judgeRules as GoalStateView['judgeRules']) ?? null
  const proposalEvent = goalEvents.find((event) => event.details?.judgeRulesProposal)
  const judgeRulesProposal =
    (proposalEvent?.details?.judgeRulesProposal as GoalStateView['judgeRulesProposal']) ?? null
  const historyEvent = goalEvents.find((event) => Array.isArray(event.details?.verdictHistory))
  const verdictHistory =
    (historyEvent?.details?.verdictHistory as GoalStateView['verdictHistory']) ?? []
  // Calibration lifecycle: take the newest calibration event and
  // collapse it into a single status. Goal events come back newest-first.
  const calibrationEvent = goalEvents.find((e) =>
    e.subtype === 'goal_calibration_started' ||
    e.subtype === 'goal_calibration_complete' ||
    e.subtype === 'goal_calibration_failed',
  )
  const calibration = calibrationEvent
    ? deriveCalibrationStatus(calibrationEvent, judgeRulesProposal)
    : null
  return {
    goal: String(state?.goal ?? ''),
    status: String(state?.status ?? 'active'),
    turnsUsed: Number(state?.turnsUsed ?? 0),
    pauseReason: typeof state?.pauseReason === 'string' ? state.pauseReason : undefined,
    lastVerdict: lastVerdict ?? null,
    judgeRules,
    judgeRulesProposal,
    calibration,
    verdictHistory,
    events: goalEvents,
  }
}

function deriveCalibrationStatus(
  event: TelemetryEventView,
  proposal: JudgeRulesPayload | null | undefined,
): CalibrationStatusView {
  const details = (event.details ?? {}) as Record<string, unknown>
  const sessionId = (details.calibratorSessionId as string | null | undefined) ?? null
  const reason = typeof details.reason === 'string' ? details.reason : undefined
  const model = typeof details.model === 'string' ? details.model : undefined
  if (event.subtype === 'goal_calibration_started') {
    return {
      status: 'running',
      sessionId,
      model,
      reason,
      at: event.at,
      willApplyAutomatically: details.willApplyAutomatically === true,
    }
  }
  if (event.subtype === 'goal_calibration_failed') {
    return {
      status: 'failed',
      sessionId,
      model,
      reason,
      at: event.at,
      errorMessage: typeof details.error === 'string' ? details.error : undefined,
    }
  }
  // goal_calibration_complete
  const applied = details.applied === true
  return {
    status: applied ? 'applied' : 'proposed',
    sessionId,
    model,
    reason,
    at: event.at,
    proposal: applied ? null : proposal ?? null,
  }
}

function collectTaskCards(events: TelemetryEventView[], agents: AgentView[]): TaskCardView[] {
  const tasks = new Map<string, TaskCardView>()
  const upsert = (task: Record<string, unknown>, fallbackAt: number, subtype?: string) => {
    const id = String(task.id ?? '')
    if (!id) return
    const existing = tasks.get(id)
    const status = typeof task.status === 'string'
      ? task.status
      : taskStatusFromEvent(subtype) ?? existing?.status ?? 'todo'
    const progress = typeof task.progress === 'number'
      ? task.progress
      : taskProgressFromEvent(subtype) ?? existing?.progress
    const history = mergeTaskEvents(existing?.events, normalizeTaskEvents(task.events))
    tasks.set(id, {
      id,
      title: typeof task.title === 'string' ? task.title : existing?.title ?? id,
      status,
      body: typeof task.body === 'string' ? task.body : existing?.body,
      assignee: typeof task.assignee === 'string' ? task.assignee : existing?.assignee,
      priority: typeof task.priority === 'number' ? task.priority : existing?.priority,
      progress,
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

  for (const event of [...events].sort((a, b) => a.at - b.at)) {
    if (!event.subtype.startsWith('task_')) continue
    const details = event.details ?? {}
    const task = details.task
    if (task && typeof task === 'object') upsert(task as Record<string, unknown>, event.at, event.subtype)
    const list = details.tasks
    if (Array.isArray(list)) {
      for (const item of list) {
        if (item && typeof item === 'object') upsert(item as Record<string, unknown>, event.at, event.subtype)
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

function taskStatusFromEvent(subtype?: string): string | undefined {
  if (!subtype) return undefined
  if (subtype === 'task_complete') return 'done'
  if (subtype === 'task_block') return 'blocked'
  if (subtype === 'task_cancel') return 'cancelled'
  if (subtype === 'task_claim' || subtype === 'task_heartbeat') return 'active'
  if (subtype === 'task_create') return 'todo'
  return undefined
}

function taskProgressFromEvent(subtype?: string): number | undefined {
  if (subtype === 'task_complete') return 100
  if (subtype === 'task_cancel') return 0
  return undefined
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

function collectKanbanCards(
  events: TelemetryEventView[],
  agents: AgentView[],
  seed: Record<string, Record<string, unknown>> = {},
): KanbanCardView[] {
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
      reviewIteration:
        typeof card.reviewIteration === 'number'
          ? card.reviewIteration
          : existing?.reviewIteration,
      workerSessionId:
        typeof card.workerSessionId === 'string'
          ? card.workerSessionId
          : existing?.workerSessionId,
      judgeSessionId:
        typeof card.judgeSessionId === 'string'
          ? card.judgeSessionId
          : existing?.judgeSessionId,
      workerTerminalState:
        typeof card.workerTerminalState === 'string'
          ? card.workerTerminalState
          : existing?.workerTerminalState,
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

  // Seed from the durable per-card snapshot first. These are the
  // most recent payloads the store has for each card; the events
  // below will overlay anything fresher that's still in the ring.
  for (const id in seed) {
    const task = seed[id]
    if (!task || typeof task !== 'object') continue
    const ts = typeof task.updatedAt === 'number' ? task.updatedAt : Date.now()
    upsert(task, ts)
  }

  for (const event of [...events].sort((a, b) => a.at - b.at)) {
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
