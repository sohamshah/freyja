/**
 * View-layer DTOs derived from store + bridge state.
 *
 * These mirror the local interfaces inside MissionDashboard.tsx so that
 * sibling view components (TasksListRailView, GoalStudioView, KanbanBridgeView)
 * can be authored standalone and the dashboard simply passes data through.
 */
import type {
  BusMessageRecord,
  SessionSnapshot,
  SubagentRecord,
  ToolCallRecord,
} from '../../../shared/events'
import type { SessionSlice, SystemEventRecord } from '../../state/store'

export interface AgentView {
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

export interface BusEventView extends BusMessageRecord {
  sessionId: string
}

export interface TelemetryEventView extends SystemEventRecord {
  sessionId: string
  sessionTitle: string
}

export interface KanbanCardView {
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
  events?: Array<{
    kind?: string
    actor?: string
    message?: string
    timestamp?: number
    details?: Record<string, unknown>
  }>
  agents?: AgentView[]
  progress?: number
  createdAt?: number
  startedAt?: number
  updatedAt?: number
  completedAt?: number
  consecutiveFailures?: number
  requiresVerification?: boolean
  spec?: {
    definition_of_done?: string[]
    references?: { files?: string[]; findings?: string[]; cards?: string[] }
    verify_with?: string
    token_budget?: number
  }
  metadata?: Record<string, unknown>
}

export type CriterionStatus = 'met' | 'partial' | 'missing'
export type CriterionPriority = 'must' | 'should' | 'may'

export interface VerdictCriterion {
  id: string
  text: string
  priority: CriterionPriority
  status: CriterionStatus
  note?: string
}

export interface GoalVerdict {
  done?: boolean
  reason?: string
  confidence?: number
  criteria?: VerdictCriterion[]
  openQuestions?: string[]
  /** Child session id when this verdict came from the `deep` profile
   * (judge-deep subagent). Lets the timeline link to the judge's own
   * session pane. */
  judgeSessionId?: string | null
  /** Set when the deep judge crashed and we fell back to an inline
   * standard call. Reason prefixed with `[judge-fallback]` in `reason`. */
  fallbackFrom?: string | null
}

export interface BriefCriterion {
  id: string
  text: string
  priority: CriterionPriority
}

export type JudgeProfile = 'quick' | 'standard' | 'deep'

export interface JudgeRules {
  voice: string
  rigorScore: number
  judgeProfile: JudgeProfile
  criteria: BriefCriterion[]
  neverDo: string[]
  whenToStop: string
  /** Optional allowlist of tool names for the deep judge. Empty means use
   * the profile default (read_file/list_directory/grep/glob/bash/fetch_url).
   * Only meaningful for the `deep` profile. */
  judgeTools?: string[]
  /** Max number of tool/think iterations the deep judge may take per
   * verdict. Bounded [1, 10]. Only meaningful for the `deep` profile. */
  judgeMaxIterations?: number
  updatedAt?: number
}

export interface GoalStateView {
  goal: string
  status: string
  turnsUsed: number
  maxTurns: number
  pauseReason?: string
  lastVerdict?: GoalVerdict | null
  judgeRules?: JudgeRules | null
  verdictHistory?: GoalVerdict[]
  events: TelemetryEventView[]
}

export interface TaskCardView {
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
  events?: Array<{
    kind?: string
    actor?: string
    message?: string
    timestamp?: number
    details?: Record<string, unknown>
  }>
  agents?: AgentView[]
  createdAt?: number
  startedAt?: number
  updatedAt?: number
  completedAt?: number
}
