/**
 * Scheduled Jobs dashboard.
 *
 * Surfaces the SchedulerService state — jobs table, run history,
 * per-job detail, metrics, daemon controls. Standalone component so it
 * can be mounted from MissionDashboard, a separate route, or the
 * settings panel.
 *
 * Data flows through `scheduler-store.ts`. The store is hydrated at
 * app boot (see App.tsx); this component re-fetches on mount for
 * freshness and subscribes to background updates via the bridge's
 * `scheduler_response` event.
 */

import React, { useCallback, useEffect, useMemo, useState } from 'react'
import {
  schedulerApi,
  useSchedulerStore,
  type DaemonStatus,
  type SchedulerJob,
  type SchedulerRun,
} from '../state/scheduler-store'


type Tab = 'jobs' | 'runs' | 'create' | 'daemon' | 'metrics'


export function ScheduledJobsDashboard() {
  const [tab, setTab] = useState<Tab>('jobs')
  const [selected, setSelected] = useState<string | null>(null)

  const jobs = useSchedulerStore((s) => s.jobs)
  const recentRuns = useSchedulerStore((s) => s.recentRuns)
  const metrics = useSchedulerStore((s) => s.metrics)
  const daemon = useSchedulerStore((s) => s.daemonStatus)
  const runsByJob = useSchedulerStore((s) => s.runsByJob)

  const refresh = useCallback(() => {
    schedulerApi.listJobs().catch(() => {})
    schedulerApi.recentRuns(100).catch(() => {})
    schedulerApi.metrics().catch(() => {})
    schedulerApi.daemonStatus().catch(() => {})
  }, [])

  useEffect(() => {
    refresh()
    const t = setInterval(refresh, 30_000)
    return () => clearInterval(t)
  }, [refresh])

  useEffect(() => {
    if (selected) {
      schedulerApi.getRuns(selected, 20).catch(() => {})
    }
  }, [selected])

  return (
    <div className="scheduled-jobs-dashboard">
      <header className="sjd-header">
        <h2>Scheduled Jobs</h2>
        <nav className="sjd-tabs" role="tablist">
          {(['jobs', 'runs', 'create', 'daemon', 'metrics'] as Tab[]).map((t) => (
            <button
              key={t}
              role="tab"
              aria-selected={tab === t}
              className={tab === t ? 'sjd-tab sjd-tab-active' : 'sjd-tab'}
              onClick={() => setTab(t)}
            >
              {t === 'jobs' ? `Jobs (${jobs.length})` :
               t === 'runs' ? 'Runs' :
               t === 'create' ? 'New' :
               t === 'daemon' ? 'Daemon' :
               'Metrics'}
            </button>
          ))}
          <button className="sjd-tab sjd-refresh" onClick={refresh}>↻</button>
        </nav>
      </header>

      <div className="sjd-body">
        {tab === 'jobs' && (
          <JobsView
            jobs={jobs}
            selected={selected}
            onSelect={setSelected}
            runsForSelected={selected ? runsByJob[selected] : undefined}
          />
        )}
        {tab === 'runs' && <RunsView runs={recentRuns} />}
        {tab === 'create' && <CreateView onCreated={refresh} />}
        {tab === 'daemon' && <DaemonView status={daemon} onRefresh={refresh} />}
        {tab === 'metrics' && <MetricsView metrics={metrics} jobs={jobs} runs={recentRuns} />}
      </div>

      <style>{STYLES}</style>
    </div>
  )
}


// ── Jobs view ────────────────────────────────────────────────────────


function JobsView(props: {
  jobs: SchedulerJob[]
  selected: string | null
  onSelect: (id: string | null) => void
  runsForSelected?: SchedulerRun[]
}) {
  const { jobs, selected, onSelect, runsForSelected } = props
  if (jobs.length === 0) {
    return (
      <div className="sjd-empty">
        <p>No scheduled jobs yet.</p>
        <p>
          Create one from the <em>New</em> tab, ask Freyja in chat
          (“remind me in 30 min to check the deploy”), or use
          <code> /freyja remind …</code> on Slack.
        </p>
      </div>
    )
  }
  const job = jobs.find((j) => j.id === selected) ?? null
  return (
    <div className="sjd-grid">
      <div className="sjd-list">
        {jobs.map((j) => (
          <JobRow
            key={j.id}
            job={j}
            selected={j.id === selected}
            onSelect={() => onSelect(j.id === selected ? null : j.id)}
          />
        ))}
      </div>
      {job && (
        <JobDetail
          job={job}
          runs={runsForSelected}
          onClose={() => onSelect(null)}
        />
      )}
    </div>
  )
}


function JobRow(props: { job: SchedulerJob; selected: boolean; onSelect: () => void }) {
  const { job, selected, onSelect } = props
  const cadence = formatSchedule(job.schedule)
  const sinks = (job.sinks || []).map((s) => (s as any).kind).join(', ')
  return (
    <div
      className={selected ? 'sjd-row sjd-row-selected' : 'sjd-row'}
      onClick={onSelect}
      role="button"
    >
      <div className="sjd-row-main">
        <div className="sjd-row-name">{job.name}</div>
        <div className="sjd-row-id">{job.id}</div>
      </div>
      <div className="sjd-row-meta">
        <span className={`sjd-status sjd-status-${job.status}`}>{job.status}</span>
        <span>{cadence}</span>
        <span>next: {formatTs(job.next_fire_at)}</span>
        <span>sinks: {sinks || '—'}</span>
        <span>fires: {job.fire_count}{job.max_fires ? `/${job.max_fires}` : ''}</span>
      </div>
    </div>
  )
}


function JobDetail(props: { job: SchedulerJob; runs?: SchedulerRun[]; onClose: () => void }) {
  const { job, runs, onClose } = props
  const [busy, setBusy] = useState<string | null>(null)
  const act = async (verb: string, fn: () => Promise<unknown>) => {
    setBusy(verb)
    try {
      await fn()
      await schedulerApi.listJobs()
    } finally {
      setBusy(null)
    }
  }
  return (
    <aside className="sjd-detail">
      <div className="sjd-detail-head">
        <div>
          <h3>{job.name}</h3>
          <div className="sjd-row-id">{job.id}</div>
        </div>
        <button className="sjd-close" onClick={onClose} aria-label="Close">×</button>
      </div>
      <div className="sjd-actions">
        {job.enabled
          ? <button disabled={busy !== null} onClick={() => act('pause', () => schedulerApi.pauseJob(job.id))}>
              Pause
            </button>
          : <button disabled={busy !== null} onClick={() => act('resume', () => schedulerApi.resumeJob(job.id))}>
              Resume
            </button>
        }
        <button disabled={busy !== null} onClick={() => act('run', () => schedulerApi.runJobNow(job.id))}>
          Run now
        </button>
        <button disabled={busy !== null}
                onClick={() => act('remove', () => schedulerApi.removeJob(job.id))}>
          Delete
        </button>
        <button onClick={() => schedulerApi.getRuns(job.id, 20).catch(() => {})}>
          Reload runs
        </button>
      </div>
      <dl className="sjd-meta">
        <dt>Description</dt><dd>{job.description || <em>(none)</em>}</dd>
        <dt>Status</dt><dd>{job.status} · {job.enabled ? 'enabled' : 'disabled'}</dd>
        <dt>Schedule</dt><dd>{formatSchedule(job.schedule)}</dd>
        <dt>Next fire</dt><dd>{formatTs(job.next_fire_at)}</dd>
        <dt>Last fire</dt><dd>{formatTs(job.last_fire_at)}</dd>
        <dt>Fires</dt><dd>{job.fire_count}{job.max_fires ? ` / ${job.max_fires}` : ''}</dd>
        <dt>Execution</dt><dd>{(job.execution as any)?.kind || '?'}</dd>
        <dt>Sinks</dt>
        <dd>
          {(job.sinks || []).length === 0
            ? <em>(none)</em>
            : (job.sinks as any[]).map((s, i) => (
              <div key={i}>{s.kind}{s.kind === 'slack' ? ` → ${s.chat_id || '?'}` : ''}
                {s.kind === 'desktop' ? ` → ${s.session_id || '?'}` : ''}
                {s.kind === 'filesystem' ? ` → ${s.path_template || '?'}` : ''}
                {s.kind === 'webhook' ? ` → ${s.url || '?'}` : ''}
              </div>
            ))}
        </dd>
        <dt>Tags</dt><dd>{(job.tags || []).join(', ') || <em>(none)</em>}</dd>
        <dt>Created by</dt>
        <dd>
          {job.creator?.surface || '?'}
          {job.creator?.user_name ? ` · ${job.creator.user_name}` : ''}
        </dd>
      </dl>
      <details>
        <summary>Prompt</summary>
        <pre className="sjd-prompt">{job.prompt}</pre>
      </details>

      <h4>Recent runs</h4>
      {!runs || runs.length === 0
        ? <p className="sjd-empty-inline">No runs yet.</p>
        : (
          <table className="sjd-runs">
            <thead>
              <tr>
                <th>Run</th>
                <th>Status</th>
                <th>Started</th>
                <th>Duration</th>
                <th>Sinks</th>
              </tr>
            </thead>
            <tbody>
              {runs.map((r) => (
                <tr key={r.run_id}>
                  <td>{r.run_id.slice(0, 14)}…</td>
                  <td className={`sjd-status sjd-status-${r.status}`}>{r.status}</td>
                  <td>{formatTs(r.started_at)}</td>
                  <td>{r.duration_seconds?.toFixed(1)}s</td>
                  <td>
                    {(r.delivery_reports || []).map((d) => (
                      <span key={d.sink_index}
                            className={d.success ? 'sjd-ok' : 'sjd-fail'}
                            title={d.error || d.artifact_ref || ''}>
                        {d.sink_kind}
                      </span>
                    ))}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
    </aside>
  )
}


// ── Recent runs (global) ──────────────────────────────────────────────


function RunsView(props: { runs: SchedulerRun[] }) {
  const { runs } = props
  if (runs.length === 0) {
    return <div className="sjd-empty">No scheduled runs yet.</div>
  }
  return (
    <table className="sjd-runs sjd-runs-wide">
      <thead>
        <tr>
          <th>Run</th>
          <th>Job</th>
          <th>Status</th>
          <th>Started</th>
          <th>Duration</th>
          <th>Cost</th>
          <th>Preview</th>
        </tr>
      </thead>
      <tbody>
        {runs.map((r) => (
          <tr key={r.run_id}>
            <td>{r.run_id.slice(0, 14)}…</td>
            <td>{r.job_name}</td>
            <td className={`sjd-status sjd-status-${r.status}`}>{r.status}</td>
            <td>{formatTs(r.started_at)}</td>
            <td>{r.duration_seconds?.toFixed(1)}s</td>
            <td>{(r.cost_usd ?? 0).toFixed(4)}</td>
            <td className="sjd-preview">{(r.output_text || '').slice(0, 80)}</td>
          </tr>
        ))}
      </tbody>
    </table>
  )
}


// ── Create form ──────────────────────────────────────────────────────


function CreateView(props: { onCreated: () => void }) {
  const [name, setName] = useState('')
  const [prompt, setPrompt] = useState('')
  const [when, setWhen] = useState('')
  const [sinks, setSinks] = useState('here')
  const [execution, setExecution] = useState('new_session')
  const [submitting, setSubmitting] = useState(false)
  const [msg, setMsg] = useState<string | null>(null)

  const submit = async (ev: React.FormEvent) => {
    ev.preventDefault()
    setSubmitting(true)
    setMsg(null)
    try {
      const sinkList = sinks.split(',').map((s) => s.trim()).filter(Boolean)
      const resp = await schedulerApi.createJob({
        name: name.trim() || undefined,
        prompt: prompt.trim(),
        when: when.trim(),
        execution: execution.trim() || 'new_session',
        sinks: sinkList,
      })
      if ((resp as any).error) {
        setMsg(`Error: ${(resp as any).error}`)
      } else {
        const job = (resp as any).job
        setMsg(`Created ${job?.name || job?.id}`)
        setPrompt('')
        setWhen('')
        props.onCreated()
      }
    } catch (e) {
      setMsg(`Error: ${(e as Error).message}`)
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <form className="sjd-create" onSubmit={submit}>
      <label>
        <span>Name (optional)</span>
        <input value={name} onChange={(e) => setName(e.target.value)}
               placeholder="auto-derived from prompt if blank"/>
      </label>
      <label>
        <span>When</span>
        <input value={when} onChange={(e) => setWhen(e.target.value)}
               placeholder='e.g. "every weekday at 9am", "in 30 minutes", "tomorrow at 5pm"'
               required/>
      </label>
      <label>
        <span>Prompt</span>
        <textarea value={prompt} onChange={(e) => setPrompt(e.target.value)} rows={6}
                  placeholder="Self-contained prompt the agent will run at fire time."
                  required/>
      </label>
      <label>
        <span>Sinks</span>
        <input value={sinks} onChange={(e) => setSinks(e.target.value)}
               placeholder="here · slack · desktop · session · laptop:/path · https://hook"/>
      </label>
      <label>
        <span>Execution</span>
        <select value={execution} onChange={(e) => setExecution(e.target.value)}>
          <option value="new_session">new_session — fresh ephemeral session per fire</option>
          <option value="persistent_job_session">persistent_job_session — memory across fires</option>
          <option value="here">here — fire into the current desktop session</option>
        </select>
      </label>
      <div className="sjd-create-actions">
        <button type="submit" disabled={submitting || !prompt || !when}>
          {submitting ? 'Creating…' : 'Create'}
        </button>
        {msg && <span className="sjd-msg">{msg}</span>}
      </div>
    </form>
  )
}


// ── Daemon view ──────────────────────────────────────────────────────


function DaemonView(props: { status: DaemonStatus | null; onRefresh: () => void }) {
  const { status, onRefresh } = props
  const [busy, setBusy] = useState(false)
  const install = async () => {
    setBusy(true)
    try {
      await schedulerApi.daemonInstall('renderer_button')
      await schedulerApi.daemonStatus()
      onRefresh()
    } finally { setBusy(false) }
  }
  const uninstall = async () => {
    setBusy(true)
    try {
      await schedulerApi.daemonUninstall()
      await schedulerApi.daemonStatus()
      onRefresh()
    } finally { setBusy(false) }
  }
  if (!status) return <div className="sjd-empty">Loading daemon status…</div>
  return (
    <div className="sjd-daemon">
      <p>
        The scheduler daemon runs in the background as a macOS
        LaunchAgent. With it installed, scheduled jobs fire even when
        the Freyja app is closed — for example, a recurring Slack
        post at 9am still works.
      </p>
      <dl className="sjd-meta">
        <dt>Supported on this platform</dt><dd>{String(status.supported)}</dd>
        <dt>Installed</dt><dd>{status.installed ? 'yes' : 'no'}</dd>
        <dt>Running</dt><dd>{status.running ? `yes (pid ${status.pid ?? '?'})` : 'no'}</dd>
        <dt>Plist</dt><dd><code>{status.plist || '—'}</code></dd>
        <dt>Log</dt><dd><code>{status.log || '—'}</code></dd>
      </dl>
      <div className="sjd-actions">
        {status.installed
          ? <button disabled={busy} onClick={uninstall}>Uninstall daemon</button>
          : <button disabled={busy || !status.supported} onClick={install}>Install daemon</button>
        }
      </div>
    </div>
  )
}


// ── Metrics view ─────────────────────────────────────────────────────


function MetricsView(props: {
  metrics: any
  jobs: SchedulerJob[]
  runs: SchedulerRun[]
}) {
  const { metrics, jobs, runs } = props
  const histogram = useMemo(() => bucketRunsByHour(runs), [runs])
  if (!metrics) return <div className="sjd-empty">Loading metrics…</div>
  return (
    <div className="sjd-metrics">
      <div className="sjd-kpis">
        <Kpi label="Jobs (active)" value={`${metrics.enabled_jobs} / ${metrics.total_jobs}`} />
        <Kpi label="Paused" value={String(metrics.paused_jobs)} />
        <Kpi label="Disabled" value={String(metrics.disabled_jobs)} />
        <Kpi label="Runs (24h)" value={String(metrics.runs_24h)} />
        <Kpi label="Success (24h)" value={`${metrics.succeeded_24h}/${metrics.runs_24h}`} />
        <Kpi label="Failed (24h)" value={String(metrics.failed_24h)} />
        <Kpi label="Avg duration" value={`${metrics.avg_run_duration_seconds?.toFixed(1) || 0}s`} />
        <Kpi label="Cost (24h)" value={`$${(metrics.total_cost_usd_24h || 0).toFixed(4)}`} />
        <Kpi label="Next fire" value={formatTs(metrics.next_fire_at) +
          (metrics.next_fire_job_name ? ` · ${metrics.next_fire_job_name}` : '')} />
      </div>
      <h4>Runs by hour of day (last {runs.length})</h4>
      <Sparkline data={histogram} />
      <h4>Top jobs by fire count</h4>
      <table className="sjd-runs">
        <thead>
          <tr><th>Job</th><th>Fires</th><th>Last fire</th><th>Status</th></tr>
        </thead>
        <tbody>
          {jobs.slice().sort((a, b) => b.fire_count - a.fire_count).slice(0, 10).map((j) => (
            <tr key={j.id}>
              <td>{j.name}</td>
              <td>{j.fire_count}</td>
              <td>{formatTs(j.last_fire_at)}</td>
              <td>{j.status}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}


function Kpi(props: { label: string; value: string }) {
  return (
    <div className="sjd-kpi">
      <div className="sjd-kpi-label">{props.label}</div>
      <div className="sjd-kpi-value">{props.value}</div>
    </div>
  )
}


function Sparkline(props: { data: number[] }) {
  const max = Math.max(1, ...props.data)
  return (
    <div className="sjd-spark">
      {props.data.map((v, i) => (
        <div key={i} className="sjd-spark-bar"
             style={{ height: `${(v / max) * 100}%` }}
             title={`${i}:00 — ${v} runs`} />
      ))}
    </div>
  )
}


// ── Helpers ──────────────────────────────────────────────────────────


function formatSchedule(s: Record<string, unknown> | undefined | null): string {
  if (!s) return '—'
  const k = String((s as any).kind || '')
  if (k === 'once') return `once at ${(s as any).at_iso}`
  if (k === 'interval') return `every ${humanizeSeconds((s as any).seconds)}`
  if (k === 'cron') return `cron(${(s as any).expression}) [${(s as any).timezone || 'UTC'}]`
  if (k === 'self_paced') {
    const mn = humanizeSeconds((s as any).min_delay_seconds)
    const mx = humanizeSeconds((s as any).max_delay_seconds)
    return `self-paced ${mn}–${mx}`
  }
  return k || '?'
}


function humanizeSeconds(n: number | undefined | null): string {
  if (!n) return '0s'
  if (n % 86400 === 0) return `${n / 86400}d`
  if (n % 3600 === 0) return `${n / 3600}h`
  if (n % 60 === 0) return `${n / 60}m`
  return `${n}s`
}


function formatTs(t: number | undefined | null): string {
  if (!t) return '—'
  const d = new Date(t * 1000)
  const now = Date.now()
  const delta = (d.getTime() - now) / 1000
  const abs = Math.abs(delta)
  let rel = ''
  if (abs < 60) rel = `${Math.round(delta)}s`
  else if (abs < 3600) rel = `${Math.round(delta / 60)}m`
  else if (abs < 86400) rel = `${Math.round(delta / 3600)}h`
  else rel = `${Math.round(delta / 86400)}d`
  return `${d.toLocaleString()} (${rel})`
}


function bucketRunsByHour(runs: SchedulerRun[]): number[] {
  const out = new Array(24).fill(0)
  for (const r of runs) {
    if (!r.started_at) continue
    const h = new Date(r.started_at * 1000).getHours()
    out[h] += 1
  }
  return out
}


// ── Styles ───────────────────────────────────────────────────────────


const STYLES = `
.scheduled-jobs-dashboard {
  display: flex; flex-direction: column; height: 100%;
  color: var(--text-primary, #e8e8e8);
  font-family: var(--font-sans, -apple-system, BlinkMacSystemFont, sans-serif);
}
.sjd-header { display: flex; justify-content: space-between; align-items: center;
  padding: 12px 16px; border-bottom: 1px solid rgba(255,255,255,0.08); }
.sjd-header h2 { margin: 0; font-size: 16px; font-weight: 600; letter-spacing: 0.02em; }
.sjd-tabs { display: flex; gap: 4px; }
.sjd-tab { background: transparent; border: 1px solid rgba(255,255,255,0.08);
  color: var(--text-secondary, #aaa); padding: 4px 10px; border-radius: 4px;
  font-size: 12px; cursor: pointer; }
.sjd-tab:hover { background: rgba(255,255,255,0.05); }
.sjd-tab-active { background: rgba(255,255,255,0.08); color: var(--text-primary, #e8e8e8); }
.sjd-refresh { padding: 4px 8px; }
.sjd-body { flex: 1; overflow: auto; padding: 16px; }
.sjd-empty { padding: 24px; opacity: 0.7; }
.sjd-empty-inline { opacity: 0.7; font-size: 13px; }
.sjd-grid { display: grid; grid-template-columns: 1fr 380px; gap: 16px; }
.sjd-list { display: flex; flex-direction: column; gap: 8px; }
.sjd-row { padding: 12px; background: rgba(255,255,255,0.03); border-radius: 6px;
  cursor: pointer; border: 1px solid transparent; }
.sjd-row:hover { background: rgba(255,255,255,0.06); }
.sjd-row-selected { border-color: rgba(255,255,255,0.2); background: rgba(255,255,255,0.08); }
.sjd-row-main { display: flex; justify-content: space-between; align-items: baseline; }
.sjd-row-name { font-weight: 600; font-size: 13px; }
.sjd-row-id { font-size: 11px; opacity: 0.5; font-family: var(--font-mono, monospace); }
.sjd-row-meta { display: flex; gap: 12px; margin-top: 6px; font-size: 11px; opacity: 0.75; flex-wrap: wrap; }
.sjd-status { padding: 2px 6px; border-radius: 3px; font-size: 10px;
  text-transform: uppercase; letter-spacing: 0.05em; }
.sjd-status-active { background: rgba(80,200,120,0.15); color: #6acc88; }
.sjd-status-paused { background: rgba(200,180,80,0.15); color: #d4b542; }
.sjd-status-running { background: rgba(80,140,220,0.15); color: #6aa2dd; }
.sjd-status-succeeded { background: rgba(80,200,120,0.15); color: #6acc88; }
.sjd-status-failed, .sjd-status-timed_out { background: rgba(220,80,80,0.15); color: #dc6a6a; }
.sjd-status-partial_failure { background: rgba(220,180,80,0.15); color: #dcb46a; }
.sjd-detail { background: rgba(255,255,255,0.04); padding: 16px; border-radius: 6px;
  overflow: auto; max-height: 100%; }
.sjd-detail-head { display: flex; justify-content: space-between; align-items: flex-start; }
.sjd-detail h3 { margin: 0 0 4px; font-size: 14px; }
.sjd-close { background: transparent; border: none; color: var(--text-secondary, #aaa);
  font-size: 18px; cursor: pointer; }
.sjd-actions { display: flex; gap: 6px; margin: 12px 0; flex-wrap: wrap; }
.sjd-actions button { background: rgba(255,255,255,0.08); border: none;
  color: var(--text-primary, #e8e8e8); padding: 4px 10px; border-radius: 3px;
  font-size: 12px; cursor: pointer; }
.sjd-actions button:hover { background: rgba(255,255,255,0.12); }
.sjd-actions button:disabled { opacity: 0.4; cursor: not-allowed; }
.sjd-meta { display: grid; grid-template-columns: max-content 1fr; gap: 4px 12px;
  font-size: 12px; margin: 12px 0; }
.sjd-meta dt { opacity: 0.6; }
.sjd-prompt { background: rgba(0,0,0,0.3); padding: 8px; border-radius: 3px;
  font-size: 11px; white-space: pre-wrap; overflow: auto; max-height: 200px; }
.sjd-runs { width: 100%; border-collapse: collapse; font-size: 12px; margin-top: 6px; }
.sjd-runs th, .sjd-runs td { padding: 4px 8px; text-align: left;
  border-bottom: 1px solid rgba(255,255,255,0.05); }
.sjd-runs-wide td.sjd-preview { opacity: 0.7; max-width: 400px;
  text-overflow: ellipsis; overflow: hidden; white-space: nowrap; }
.sjd-ok { color: #6acc88; margin-right: 6px; }
.sjd-fail { color: #dc6a6a; margin-right: 6px; }
.sjd-create { display: flex; flex-direction: column; gap: 10px; max-width: 720px; }
.sjd-create label { display: flex; flex-direction: column; gap: 4px; font-size: 12px; }
.sjd-create label span { opacity: 0.7; }
.sjd-create input, .sjd-create textarea, .sjd-create select {
  background: rgba(255,255,255,0.06); border: 1px solid rgba(255,255,255,0.1);
  color: var(--text-primary, #e8e8e8); border-radius: 3px; padding: 6px 8px;
  font-family: inherit; font-size: 12px; }
.sjd-create textarea { resize: vertical; font-family: var(--font-mono, monospace); }
.sjd-create-actions { display: flex; gap: 8px; align-items: center; }
.sjd-create-actions button { background: rgba(80,140,220,0.3);
  color: var(--text-primary, #e8e8e8); padding: 6px 14px; border: none;
  border-radius: 3px; cursor: pointer; }
.sjd-create-actions button:disabled { opacity: 0.4; cursor: not-allowed; }
.sjd-msg { font-size: 12px; opacity: 0.7; }
.sjd-daemon p { font-size: 13px; opacity: 0.85; max-width: 640px; }
.sjd-daemon code { font-family: var(--font-mono, monospace); font-size: 11px;
  background: rgba(0,0,0,0.3); padding: 2px 4px; border-radius: 2px; }
.sjd-kpis { display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr));
  gap: 8px; margin-bottom: 16px; }
.sjd-kpi { background: rgba(255,255,255,0.04); padding: 10px 12px; border-radius: 4px; }
.sjd-kpi-label { font-size: 10px; opacity: 0.6; text-transform: uppercase;
  letter-spacing: 0.05em; }
.sjd-kpi-value { font-size: 16px; font-weight: 600; margin-top: 2px; }
.sjd-spark { display: flex; align-items: flex-end; height: 80px; gap: 2px;
  border-bottom: 1px solid rgba(255,255,255,0.1); }
.sjd-spark-bar { flex: 1; background: rgba(140,180,220,0.5); border-radius: 2px 2px 0 0;
  min-height: 2px; }
`
