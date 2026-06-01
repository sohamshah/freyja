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
    if (event.type !== 'scheduler_response') return
    const ev = event as Extract<BridgeEvent, { type: 'scheduler_response' }>
    if (ev.requestId) {
      const handled = get().resolvePending(ev.requestId, ev)
      if (handled) return
    }
    // Unsolicited / broadcast — common when other surfaces (Slack) mutate
    // state. Refresh whatever slice matches.
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
    }
  },
}))


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
}
