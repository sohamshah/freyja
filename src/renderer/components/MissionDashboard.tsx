import { useEffect, useMemo, useState } from 'react'
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
import { SessionActivityTab } from './views/SessionReceiptView'
import { ScopedErrorBoundary } from './ScopedErrorBoundary'

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
  status: 'pending' | 'running' | 'done' | 'failed' | 'stopped' | 'cancelled'
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
  // Hint that the card's definition_of_done is concretely checkable
  // (Move C). Informational since Move R: `complete` sends every card
  // to `review` whatever the flag, and only a passing judge verdict
  // seals it. update/comment/heartbeat cannot set review or done states.
  requiresVerification?: boolean
  // Move R — default-on judge-review pipeline. `reviewIteration`
  // counts entries to review, one per judge review (worker → review →
  // judge fail → worker); the third failed verdict blocks the card.
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
  blockedBy?: string[]
  blocks?: string[]
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
  // Scheduler lives as its own modal now (⌘⇧S / /schedule / palette
  // entry / TitleBar SchedulerPill). Mounting it inside a dashboard
  // tab double-stacked a fixed overlay on top of the dashboard chrome,
  // and the dashboard's session-scoped frame implied scheduler state
  // belonged to one session — it doesn't.
]

function visibleDashboardTabs(
  strategy?: CoordinationStrategy,
): Array<{ id: DashboardTab; label: string; hint: string }> {
  // Hide the dedicated tasks tab in kanban mode — the tasks tool is
  // intentionally not registered there (the kanban board is the only
  // planning surface). The tab would be permanently empty.
  if (strategy === 'kanban') {
    return TABS.filter((t) => t.id !== 'tasks')
  }
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
    model: 'claude-sonnet-5-5, fallback gpt-6.1-sol / glm-5.3-fireworks / kimi-k3',
    thinking: 'medium',
    tools: 'Web research + read-only file tools',
    maxIterations: 160,
    note: 'Best for open-ended research where coverage matters.',
  },
  {
    id: 'explore-fast',
    bestFor: 'Cheap factual lookups and parallel fanout searches.',
    model: 'Random pick of gpt-6-luna / glm-5.3-flash-fireworks / gemini-3.5-flash-lite',
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
    model: 'gpt-5.5, fallback gpt-6.1-sol / claude-sonnet-5-5 / deepseek-v4p1-flash',
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
    model: 'Prefers the parent, fallback claude-sonnet-5-5 / gpt-6.1-sol / deepseek-v4p1-flash',
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
  general: 'rgb(var(--fg-1))',
  explore: 'rgb(var(--fg-1))',
  'explore-fast': 'rgb(var(--fg-1))',
  code: 'rgb(var(--fg-1))',
  verify: 'rgb(var(--fg-1))',
  plan: 'rgb(var(--fg-1))',
  review: 'rgb(var(--fg-1))',
  test: 'rgb(var(--fg-1))',
  'browser-qa': 'rgb(var(--fg-1))',
  performance: 'rgb(var(--fg-1))',
  docs: 'rgb(var(--fg-1))',
  'memory-curator': 'rgb(var(--fg-1))',
  computer: 'rgb(var(--fg-1))',
}

export function MissionDashboard() {
  const tab = useHarness((s) => s.missionDashboardTab)
  const toggleDashboard = useHarness((s) => s.toggleMissionDashboard)
  const focusTaskId = useHarness((s) => s.missionDashboardFocusTaskId)
  const clearTaskFocus = useHarness((s) => s.clearDashboardTaskFocus)
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
          'compaction_failed',
          'context_pruning',
          'tool_truncation',
          'output_truncation',
          'iteration_limit',
        ].includes(event.subtype),
      )
    // The kanban board view consumes the *full* system-event stream
    // (kanban_tick / kanban_stale / kanban_judge_verdict / dispatch
    // events), NOT the compaction-only `telemetryEvents` whitelist above
    // — that whitelist strips every kanban_* event, which would blank the
    // tick countdown, stale halos, judge meters and the activity feed.
    // Ascending chronological order: the board's scanners treat the tail
    // of the array as the newest event (findLatestTickInfo walks from the
    // end; the activity feed does slice(-30).reverse()).
    const kanbanEvents = [...systemEventViews].sort((a, b) => a.at - b.at)
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
    const latestAsk = lastUserMessage(missionSlice?.messages ?? messages)
    const objective = latestAsk?.text ??
      missionSession?.task ??
      missionSession?.title ??
      'No objective yet'

    return {
      activeSession,
      missionSession,
      missionSessionId: missionSession?.id ?? activeSessionId,
      missionSlice,
      objective,
      latestAsk,
      agents,
      sessions,
      busEvents,
      findings: busEvents.filter((event) => event.topic !== 'read'),
      readEvents: busEvents.filter((event) => event.topic === 'read'),
      inboxEvents: inboxEventsAggregated,
      telemetryEvents,
      kanbanEvents,
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
      <header
        className="relative flex shrink-0 items-center gap-5 border-b border-white/[0.06] pr-5"
        style={{
          // zoom-compensated: clears the native traffic lights at any zoom
          paddingLeft: 'calc(var(--titlebar-inset, 82px) + 6px)',
          minHeight: 'var(--titlebar-height, 46px)',
        }}
      >
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

        {/* Contain a crash in one tab's content to that tab — keeps the
            dashboard chrome + other tabs alive instead of the whole app
            dropping to the root recovery screen. Resets on tab switch. */}
        <ScopedErrorBoundary label="dashboard" resetKey={tab}>
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
            kanbanEvents={dashboard.kanbanEvents}
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
        {/* The activity tab leads with the session receipt (thread braid +
            metrics); the classic event log stays behind a toggle. The
            findings/telemetry aliases keep jumping straight to the log
            view, since they exist to deep-link specific event rows. */}
        {tab === 'activity' && (
          <SessionActivityTab
            renderLog={() => (
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
          />
        )}
        {(tab === 'findings' || tab === 'telemetry') && (
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
            title={dashboard.missionSession?.title ?? ''}
            goal={dashboard.goalState?.goal}
            latestAsk={dashboard.latestAsk}
            tasks={dashboard.taskCards}
            agents={dashboard.agents}
            contextPct={contextPct}
            cost={dashboard.cost}
            onAttach={attach}
            focusTaskId={focusTaskId}
            onFocusTaskHandled={clearTaskFocus}
          />
        )}
        {tab === 'profiles' && <ProfilesTab />}
        </ScopedErrorBoundary>
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
  kanbanEvents,
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
  /** Full system-event stream (ascending) for the kanban board view —
   *  carries the kanban_* + dispatch events that `telemetryEvents`
   *  deliberately strips. */
  kanbanEvents: TelemetryEventView[]
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
          telemetryEvents={kanbanEvents}
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
              border: `1px solid color-mix(in srgb, ${color} 40%, transparent)`,
              boxShadow: agent.status === 'running' ? `0 0 24px color-mix(in srgb, ${color} 20%, transparent)` : undefined,
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

function normalizeBusTimestamp(timestamp: number): number {
  if (!Number.isFinite(timestamp) || timestamp <= 0) return Date.now()
  return timestamp < 1_000_000_000_000 ? timestamp * 1000 : timestamp
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
          style={{ boxShadow: `inset 0 0 0 1px color-mix(in srgb, ${color} 33%, transparent)` }}
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
  // A jev run that ended blocked or waiting for confirmation stopped; it did not fail.
  if (session.completed) {
    if (session.success !== false) return 'done'
    return session.id.startsWith('jev_') ? 'stopped' : 'failed'
  }
  if (sub?.state) return sub.state
  return 'running'
}

function lastUserMessage(
  messages: Array<{ role: string; createdAt?: number; parts: Array<{ type: string; text?: string }> }>,
): { text: string; at?: number } | null {
  for (let i = messages.length - 1; i >= 0; i -= 1) {
    const message = messages[i]
    if (message.role !== 'user') continue
    const text = message.parts
      .filter((part) => part.type === 'text' && part.text)
      .map((part) => part.text)
      .join(' ')
      .trim()
    if (text) return { text, at: message.createdAt }
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

function topicBadgeClass(topic: BusMessageRecord['topic']): string {
  if (topic === 'errors') return 'bg-danger/12 text-danger ring-1 ring-danger/25'
  if (topic === 'findings') return 'bg-accent/12 text-accent ring-1 ring-accent/25'
  if (topic === 'progress') return 'bg-ok/10 text-ok ring-1 ring-ok/20'
  return 'bg-white/[0.05] text-fg-2 ring-1 ring-white/[0.08]'
}

function statusTextClass(status: AgentView['status']): string {
  if (status === 'failed' || status === 'cancelled') return 'text-danger'
  if (status === 'stopped') return 'text-warn'
  if (status === 'done') return 'text-ok'
  if (status === 'running') return 'text-accent'
  return 'text-fg-2'
}

function coordinationLabel(strategy?: CoordinationStrategy): string {
  if (strategy === 'kanban') return 'board'
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
      blockedBy: Array.isArray(task.blockedBy) ? normalizeStringArray(task.blockedBy) : existing?.blockedBy ?? [],
      blocks: Array.isArray(task.blocks) ? normalizeStringArray(task.blocks) : existing?.blocks ?? [],
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
