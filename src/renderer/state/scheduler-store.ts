import { create } from 'zustand'
import type { BridgeEvent } from '../../shared/events'

// Mirrors bridge.scheduler.models.JobRecord. We accept any extra fields
// the bridge sends so adding new ones doesn't require a renderer ship.
export interface SchedulerJob {
  id: string
  name: string
  description?: string
  enabled: boolean
  status: string
  schedule: Record<string, unknown>
  next_fire_at?: number | null
  last_fire_at?: number | null
  fire_count: number
  max_fires?: number | null
  prompt: string
  execution: Record<string, unknown>
  permission_snapshot?: string
  model_id?: string | null
  coordination_strategy?: string
  skills_to_load?: string[]
  sinks: Record<string, unknown>[]
  misfire_policy?: string
  overlap_policy?: string
  timeout_seconds?: number | null
  tags?: string[]
  creator?: { surface?: string; user_id?: string | null; user_name?: string | null }
  created_at?: number
  updated_at?: number
  // Add anything else the bridge serializes.
  [extra: string]: unknown
}

export interface SchedulerRun {
  run_id: string
  job_id: string
  // The ephemeral session the fire executed in (e.g.
  // "scheduler:<job>.ephemeral"). Already present in the bridge's
  // RunRecord; typed here so the sidebar can open the run's transcript.
  execution_session_id?: string | null
  job_name: string
  status: string
  started_at: number
  finished_at?: number | null
  duration_seconds: number
  fire_number: number
  iteration: number
  output_text: string
  error?: string | null
  delivery_reports: {
    sink_index: number
    sink_kind: string
    success: boolean
    delivered_at: number
    artifact_ref?: string | null
    error?: string | null
  }[]
  input_tokens?: number
  output_tokens?: number
  cost_usd?: number
  [extra: string]: unknown
}

// A run's status in words, wherever a run is shown: the dashboard's run
// feed, a schedule's last run, the run's drawer and the sidebar.
const RUN_STATUS_LABELS: Record<string, string> = {
  claimed: 'starting',
  running: 'in flight',
  delivering: 'delivering',
  succeeded: 'completed',
  partial_failure: 'partial delivery failure',
  failed: 'failed',
  cancelled: 'cancelled',
  timed_out: 'timed out',
  budget_exceeded: 'over budget',
  queued: 'queued',
  interrupted: 'interrupted',
  expired: 'expired',
  not_started: 'not started',
}

export function runStatusLabel(status: string): string {
  return RUN_STATUS_LABELS[status] ?? status.replace(/_/g, ' ')
}

export interface SchedulerMetrics {
  total_jobs: number
  enabled_jobs: number
  paused_jobs: number
  disabled_jobs: number
  runs_24h: number
  succeeded_24h: number
  failed_24h: number
  avg_run_duration_seconds: number
  total_cost_usd_24h: number
  next_fire_at?: number | null
  next_fire_job_id?: string | null
  next_fire_job_name?: string | null
}

export interface DaemonStatus {
  supported: boolean
  installed?: boolean
  running?: boolean
  pid?: number | null
  plist?: string | null
  log?: string | null
}

interface PendingRequest {
  subtype: string
  resolve: (event: any) => void
  reject: (err: Error) => void
  ts: number
}

interface SchedulerStore {
  jobs: SchedulerJob[]
  runsByJob: Record<string, SchedulerRun[]>
  recentRuns: SchedulerRun[]
  metrics: SchedulerMetrics | null
  daemonStatus: DaemonStatus | null
  pendingRequests: Map<string, PendingRequest>

  // ── UI modal state ─────────────────────────────────────────────
  // The dashboard mounts as a top-level modal (⌘⇧S, /schedule, palette,
  // daemon pill, or the legacy MissionDashboard tab can all open it).
  // Selected job/run drive the focused schedule card + run drawer.
  showDashboard: boolean
  selectedJobId: string | null
  selectedRunId: string | null
  openDashboard(): void
  closeDashboard(): void
  selectJob(jobId: string | null): void
  selectRun(runId: string | null): void

  // ── Mutators (used by the bridge event bus) ─────────────────────
  setJobs(jobs: SchedulerJob[]): void
  upsertJob(job: SchedulerJob): void
  removeJob(jobId: string): void
  setRunsForJob(jobId: string, runs: SchedulerRun[]): void
  setRecentRuns(runs: SchedulerRun[]): void
  setMetrics(m: SchedulerMetrics): void
  setDaemonStatus(s: DaemonStatus): void

  registerPending(id: string, subtype: string,
                  resolve: (e: any) => void, reject: (e: Error) => void): void
  resolvePending(id: string, event: any): boolean
  handleEvent(event: BridgeEvent): void
}

export const useSchedulerStore = create<SchedulerStore>((set, get) => ({
  jobs: [],
  runsByJob: {},
  recentRuns: [],
  metrics: null,
  daemonStatus: null,
  pendingRequests: new Map(),

  showDashboard: false,
  selectedJobId: null,
  selectedRunId: null,
  openDashboard() { set({ showDashboard: true }) },
  closeDashboard() {
    set({ showDashboard: false, selectedRunId: null })
  },
  selectJob(jobId) { set({ selectedJobId: jobId }) },
  selectRun(runId) { set({ selectedRunId: runId }) },

  setJobs(jobs) { set({ jobs }) },
  upsertJob(job) {
    set((s) => {
      const i = s.jobs.findIndex((j) => j.id === job.id)
      const next = i >= 0 ? [...s.jobs] : [...s.jobs, job]
      if (i >= 0) next[i] = job
      return { jobs: next }
    })
  },
  removeJob(jobId) {
    set((s) => ({ jobs: s.jobs.filter((j) => j.id !== jobId) }))
  },
  setRunsForJob(jobId, runs) {
    set((s) => ({ runsByJob: { ...s.runsByJob, [jobId]: runs } }))
  },
  setRecentRuns(runs) { set({ recentRuns: runs }) },
  setMetrics(m) { set({ metrics: m }) },
  setDaemonStatus(s) { set({ daemonStatus: s }) },

  registerPending(id, subtype, resolve, reject) {
    const map = new Map(get().pendingRequests)
    map.set(id, { subtype, resolve, reject, ts: Date.now() })
    set({ pendingRequests: map })
    // Time out after 30s so stuck requests don't leak.
    setTimeout(() => {
      const cur = get().pendingRequests.get(id)
      if (cur) {
        cur.reject(new Error(`scheduler request timed out: ${subtype}`))
        const m = new Map(get().pendingRequests)
        m.delete(id)
        set({ pendingRequests: m })
      }
    }, 30_000)
  },
  resolvePending(id, event) {
    const cur = get().pendingRequests.get(id)
    if (!cur) return false
    cur.resolve(event)
    const m = new Map(get().pendingRequests)
    m.delete(id)
    set({ pendingRequests: m })
    return true
  },

  handleEvent(event) {
    // Push events from the scheduler runtime keep the dashboard live
    // without polling. We mutate the local state and let the renderer
    // re-render off zustand. Each branch is no-op-safe if the bridge
    // hasn't surfaced the full payload yet.
    const t = (event as { type?: string }).type
    if (t === 'system_event') {
      // These come from the app's bridge, and from the gateway's and the
      // scheduler daemon's logs, which main follows.
      const { subtype, details } = event as Extract<BridgeEvent, { type: 'system_event' }>
      if (subtype.startsWith('scheduler_job_')) {
        // A job made, changed, paused, resumed, stopped or removed: read
        // the list back, so next_fire_at and the status reconcile.
        schedulerApi.listJobs().catch(() => {})
        return
      }
      // A run's events (scheduler_run_claimed, then how it ended) name its
      // job: read its runs back. Run now answers as the run starts, so its
      // outcome reaches the dashboard only this way.
      const jobId = details?.jobId
      if (typeof jobId === 'string') {
        schedulerApi.getRuns(jobId, 100).catch(() => {})
        schedulerApi.recentRuns(100).catch(() => {})
        schedulerApi.getJob(jobId).catch(() => {})
      }
      return
    }
    if (event.type !== 'scheduler_response') return
    const ev = event as Extract<BridgeEvent, { type: 'scheduler_response' }>
    // Always mirror the payload into the store FIRST, then resolve any
    // pending promise. The previous order skipped store updates when a
    // requestId matched — so promise-based callers got the data but
    // the dashboard (which only watches store state) saw nothing.
    switch (ev.subtype) {
      case 'list_jobs':
        if (Array.isArray(ev.jobs)) set({ jobs: ev.jobs as unknown as SchedulerJob[] })
        break
      case 'get_job':
        if (ev.job) get().upsertJob(ev.job as unknown as SchedulerJob)
        break
      case 'get_runs':
        if (Array.isArray(ev.runs) && ev.jobId) {
          get().setRunsForJob(ev.jobId, ev.runs as unknown as SchedulerRun[])
        }
        break
      case 'recent_runs':
        if (Array.isArray(ev.runs)) get().setRecentRuns(ev.runs as unknown as SchedulerRun[])
        break
      case 'metrics':
        if (ev.metrics) get().setMetrics(ev.metrics as unknown as SchedulerMetrics)
        break
      case 'daemon_status':
        if (ev.status) get().setDaemonStatus(ev.status as unknown as DaemonStatus)
        break
      case 'create_job':
      case 'update_job':
        if (ev.job) get().upsertJob(ev.job as unknown as SchedulerJob)
        break
      case 'pause_job':
      case 'resume_job':
      case 'remove_job':
        // The mutation already happened on the bridge; ask for a fresh
        // list so any side effects (status flip, next_fire_at recompute)
        // land in the store.
        break
      case 'run_job_now':
        // Append the run to recentRuns so the user sees it immediately
        // instead of having to refresh.
        if (ev.run) {
          const r = ev.run as unknown as SchedulerRun
          set((s) => ({ recentRuns: [r, ...s.recentRuns].slice(0, 100) }))
        }
        break
    }
    if (ev.requestId) {
      get().resolvePending(ev.requestId, ev)
    }
  },
}))


/** True for the bridge events the scheduler store takes: its answers,
 *  scheduler_* events, and the system events of a scheduler run or job. */
export function isSchedulerEvent(event: any): boolean {
  const t = event?.type
  if (t === 'system_event') {
    return typeof event.subtype === 'string' && /^scheduler_(run|job)_/.test(event.subtype)
  }
  return typeof t === 'string' && t.startsWith('scheduler_')
}


// ── Bridge call helpers (renderer → bridge) ────────────────────────

let _bridge: { send: (cmd: any) => void } | null = null
let _reqCounter = 0

export function bindSchedulerBridge(bridge: { send: (cmd: any) => void }): void {
  _bridge = bridge
}

function _send<T = any>(type: string, payload: Record<string, unknown> = {}): Promise<T> {
  if (_bridge === null) return Promise.reject(new Error('scheduler bridge not bound'))
  const id = `sched-${++_reqCounter}-${Date.now()}`
  return new Promise<T>((resolve, reject) => {
    useSchedulerStore
      .getState()
      .registerPending(id, type, (e) => resolve(e as T), reject)
    _bridge!.send({ type, id, ...payload })
  })
}

export const schedulerApi = {
  listJobs(filter: Record<string, unknown> = {}) {
    return _send('scheduler.list_jobs', filter)
  },
  getJob(jobId: string) {
    return _send('scheduler.get_job', { jobId })
  },
  getRuns(jobId: string, limit = 50) {
    return _send('scheduler.get_runs', { jobId, limit })
  },
  recentRuns(limit = 100) {
    return _send('scheduler.recent_runs', { limit })
  },
  metrics() {
    return _send('scheduler.metrics')
  },
  createJob(payload: Record<string, unknown>) {
    return _send('scheduler.create_job', { payload })
  },
  updateJob(jobId: string, payload: Record<string, unknown>) {
    return _send<{ job?: SchedulerJob; error?: string }>(
      'scheduler.update_job', { jobId, payload },
    )
  },
  pauseJob(jobId: string) {
    return _send('scheduler.pause_job', { jobId })
  },
  resumeJob(jobId: string) {
    return _send('scheduler.resume_job', { jobId })
  },
  removeJob(jobId: string) {
    return _send('scheduler.remove_job', { jobId })
  },
  runJobNow(jobId: string) {
    return _send('scheduler.run_job_now', { jobId })
  },
  cancelRun(runId: string) {
    return _send('scheduler.cancel_run', { runId })
  },
  daemonStatus() {
    return _send('scheduler.daemon_status')
  },
  daemonInstall(reason = 'renderer_button') {
    return _send('scheduler.daemon_install', { reason })
  },
  daemonUninstall() {
    return _send('scheduler.daemon_uninstall')
  },
  previewNextFires(
    args: { jobId?: string; when?: string; schedule?: Record<string, unknown>;
            timezone?: string; n?: number },
  ) {
    return _send<{ fires: number[]; error?: string }>(
      'scheduler.preview_next_fires',
      {
        jobId: args.jobId,
        when: args.when,
        schedule: args.schedule,
        timezone: args.timezone || 'UTC',
        n: args.n ?? 5,
      },
    )
  },
}
