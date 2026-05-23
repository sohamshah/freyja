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
  /** Artifact file paths produced by the worker. Used in the drawer
   *  body and as a count chip on the kanban card. */
  artifacts?: string[]
  /** Move R — default-on judge-review pipeline. `reviewIteration`
   *  counts how many review->rework cycles this card has been
   *  through. Sticky session ids preserve worker/judge continuity
   *  across reworks. `workerTerminalState` records what state the
   *  worker exited at (done / failed / cancelled / crashed /
   *  timed_out) so the UI can communicate whether a card in review
   *  is being judged on a clean delivery or a partial crash dump. */
  reviewIteration?: number
  workerSessionId?: string
  judgeSessionId?: string
  workerTerminalState?: string
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

export type JudgeProfile = 'skip' | 'quick' | 'standard' | 'deep'

export interface CalibratorMeta {
  /** The model that produced this calibration. */
  model: string
  /** Epoch ms when calibration completed. */
  ranAt: number
  /** Schema version, lets future calibrator output evolve safely. */
  version: number
  /** Calibrator's overall confidence in the configuration (0-1). */
  confidence: number
  /** One paragraph explaining the goal type, dominant failure modes, and
   * the high-level reason for the chosen profile + rigor. */
  rationaleOverall: string
  /** Per-field one-sentence rationale. Keyed by JudgeRules field name in
   * camelCase (judgeProfile, rigorScore, voice, criteria, neverDo,
   * whenToStop, judgeTools). */
  rationaleByField: Record<string, string>
  /** Field names the calibrator actually populated. The editor uses this
   * to render a "set by calibrator" affordance next to those fields. */
  calibratorSetFields: string[]
  /** Calibrator child session id — links the editor's "open calibrator"
   * button to the right session pane. */
  sessionId?: string | null
}

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
  /** Provenance from the auto-calibrator. Null when no calibration has
   * run for this goal (e.g. operator authored the rules manually). */
  calibratorMeta?: CalibratorMeta | null
  updatedAt?: number
}

/** Pending calibrator proposal — set when the operator already had
 * pre-authored JudgeRules at calibration time so we surface the proposal
 * for review instead of clobbering them. The renderer pulls this from
 * goal events on every render. */
export interface CalibrationStatus {
  /** Lifecycle: idle | running | applied | proposed | failed */
  status: 'idle' | 'running' | 'applied' | 'proposed' | 'failed'
  /** Calibrator child session id (when running or completed). */
  sessionId?: string | null
  /** Model that's running / ran the calibration. */
  model?: string
  /** Reason the calibrator fired: 'goal_set' | 'recalibrate' */
  reason?: string
  /** Epoch ms when the latest event landed. */
  at?: number
  /** Failure detail when status === 'failed'. */
  errorMessage?: string
  /** When status === 'proposed', the pending JudgeRules the operator can
   * accept or dismiss. Null otherwise. */
  proposal?: JudgeRules | null
  /** Whether the calibrator was about to auto-apply (false if operator
   * had pre-authored rules). Useful for the UI's "Apply" prompt. */
  willApplyAutomatically?: boolean
}

export interface GoalStateView {
  goal: string
  status: string
  turnsUsed: number
  pauseReason?: string
  lastVerdict?: GoalVerdict | null
  judgeRules?: JudgeRules | null
  /** Pending calibrator proposal when the operator's existing rules
   * blocked auto-apply. Editor offers an "Apply" button to adopt. */
  judgeRulesProposal?: JudgeRules | null
  /** Live calibrator lifecycle — set by goal_calibration_* events. */
  calibration?: CalibrationStatus | null
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
