/**
 * Scheduled Jobs — the standalone modal that owns schedules + past runs.
 *
 * Reach paths: ⌘⇧S, /schedule (+ /jobs /cron aliases), Command Palette
 * "Scheduled Jobs", title-bar SchedulerPill, and (legacy) the
 * MissionDashboard scheduler tab.
 *
 * Layout (operator-workshop vibe):
 *   ┌─ header strip ─────────────────────────────────────────────────┐
 *   │  Fraunces h1 · live count chip · next-fire countdown · daemon  │
 *   │  pill · [+ new schedule]                                       │
 *   ├─ schedule cards (left, scrollable) ─┬─ run feed (right) ───────┤
 *   │  · 14-day timeline strip            │  filter chips            │
 *   │  · sinks chips w/ destination       │  click row → drawer      │
 *   │  · hover-revealed actions row       │                          │
 *   └─────────────────────────────────────┴──────────────────────────┘
 *
 * Data flows through `scheduler-store.ts`. The store subscribes to push
 * events from the bridge (scheduler_run_* / scheduler_job_*), so no
 * polling is needed — this component only hydrates once on mount.
 */

import React, { useCallback, useEffect, useMemo, useState } from 'react'
import {
  schedulerApi,
  useSchedulerStore,
  type DaemonStatus,
  type SchedulerJob,
  type SchedulerRun,
} from '../state/scheduler-store'
import { DetailDrawer, DrawerSection } from './shared/DetailDrawer'
import { useHarness } from '../state/store'

type RunFilter = 'all' | 'succeeded' | 'failed' | 'running'

export function ScheduledJobsDashboard() {
  const close = useSchedulerStore((s) => s.closeDashboard)
  const jobs = useSchedulerStore((s) => s.jobs)
  const recentRuns = useSchedulerStore((s) => s.recentRuns)
  const metrics = useSchedulerStore((s) => s.metrics)
  const daemon = useSchedulerStore((s) => s.daemonStatus)
  const runsByJob = useSchedulerStore((s) => s.runsByJob)
  const selectedJobId = useSchedulerStore((s) => s.selectedJobId)
  const selectJob = useSchedulerStore((s) => s.selectJob)
  const selectedRunId = useSchedulerStore((s) => s.selectedRunId)
  const selectRun = useSchedulerStore((s) => s.selectRun)

  const [runFilter, setRunFilter] = useState<RunFilter>('all')
  const [creating, setCreating] = useState(false)
  const [daemonExpanded, setDaemonExpanded] = useState(false)

  // One-time hydration on mount; the store handles push events from
  // there. We still re-fire runs/metrics if the user has been away for
  // a while — cheap, idempotent.
  useEffect(() => {
    schedulerApi.listJobs().catch(() => {})
    schedulerApi.recentRuns(100).catch(() => {})
    schedulerApi.metrics().catch(() => {})
    schedulerApi.daemonStatus().catch(() => {})
  }, [])

  // When a schedule is focused we pull its full run history so the
  // timeline strip + run feed reflect actual fire history (not just
  // the global recent-runs cache).
  useEffect(() => {
    if (selectedJobId) {
      schedulerApi.getRuns(selectedJobId, 100).catch(() => {})
    }
  }, [selectedJobId])

  const filteredRuns = useMemo(() => {
    const base = selectedJobId
      ? runsByJob[selectedJobId] || []
      : recentRuns
    return base.filter((r) => {
      if (runFilter === 'all') return true
      if (runFilter === 'running') return r.status === 'running'
      if (runFilter === 'succeeded') return r.status === 'succeeded'
      if (runFilter === 'failed') {
        return r.status === 'failed'
            || r.status === 'timed_out'
            || r.status === 'partial_failure'
      }
      return true
    })
  }, [runFilter, selectedJobId, runsByJob, recentRuns])

  const selectedRun = useMemo(() => {
    if (!selectedRunId) return null
    if (selectedJobId) {
      const r = (runsByJob[selectedJobId] || []).find((x) => x.run_id === selectedRunId)
      if (r) return r
    }
    return recentRuns.find((r) => r.run_id === selectedRunId) || null
  }, [selectedRunId, selectedJobId, runsByJob, recentRuns])

  // Live tick so the next-fire countdown re-renders every second.
  const [now, setNow] = useState(() => Math.floor(Date.now() / 1000))
  useEffect(() => {
    const t = setInterval(() => setNow(Math.floor(Date.now() / 1000)), 1000)
    return () => clearInterval(t)
  }, [])

  const enabledCount = jobs.filter((j) => j.enabled).length
  const focusedJob = jobs.find((j) => j.id === selectedJobId) || null

  return (
    <div className="fixed inset-0 z-50 flex flex-col bg-bg-0/[0.96] backdrop-blur-[24px]">
      {/* Header strip */}
      <header className="relative flex shrink-0 items-center gap-4 border-b border-white/[0.06] py-4 pl-[88px] pr-5">
        <div className="flex min-w-0 flex-col gap-0.5">
          <span className="font-mono text-[10px] uppercase tracking-[0.18em] text-fg-3">
            schedules · past runs · daemon
          </span>
          <h1 className="m-0 font-serif text-[26px] font-light leading-[1.1] tracking-[-0.01em] text-fg-0">
            {jobs.length === 0 ? 'No schedules yet'
              : `${enabledCount} active · ${jobs.length} total`}
          </h1>
        </div>

        <div className="ml-4 flex items-center gap-2 font-mono text-[10.5px] uppercase tracking-[0.12em] text-fg-2">
          {metrics?.next_fire_at && metrics.next_fire_at > now ? (
            <NextFireChip
              label={metrics.next_fire_job_name || ''}
              countdown={humanCountdown(metrics.next_fire_at - now)}
              jobId={metrics.next_fire_job_id || null}
              onClick={(id) => selectJob(id)}
            />
          ) : jobs.length > 0 ? (
            <span className="rounded border border-white/[0.08] bg-white/[0.03] px-2 py-1 text-fg-3">
              no upcoming fires
            </span>
          ) : null}
          <DaemonChip status={daemon} onClick={() => setDaemonExpanded((v) => !v)} />
        </div>

        <span className="flex-1" />

        <button
          type="button"
          onClick={() => setCreating(true)}
          className="rounded-md border border-accent/[0.30] bg-accent/[0.10] px-3 py-1.5 font-mono text-[10.5px] uppercase tracking-[0.14em] text-accent transition hover:bg-accent/[0.18]"
        >
          + new schedule
        </button>
        <button
          type="button"
          onClick={close}
          className="rounded-md border border-white/[0.06] bg-white/[0.03] px-3 py-1.5 font-mono text-[10px] uppercase tracking-[0.18em] text-fg-2 transition hover:bg-white/[0.07] hover:text-fg-0"
        >
          esc · close
        </button>
      </header>

      {/* Daemon strip — collapsible */}
      {daemonExpanded && (
        <DaemonStrip status={daemon} onClose={() => setDaemonExpanded(false)} />
      )}

      {/* Body */}
      <div className="relative flex min-h-0 flex-1 overflow-hidden">
        {jobs.length === 0 ? (
          <EmptyState onCreate={() => setCreating(true)} />
        ) : (
          <>
            {/* Schedule cards (left column) */}
            <div className="flex min-w-0 flex-1 flex-col gap-3 overflow-y-auto px-5 py-5">
              {selectedJobId && (
                <button
                  type="button"
                  onClick={() => selectJob(null)}
                  className="self-start font-mono text-[10px] uppercase tracking-[0.14em] text-fg-3 transition hover:text-fg-0"
                >
                  ← back to all schedules
                </button>
              )}
              {(selectedJobId ? jobs.filter((j) => j.id === selectedJobId) : jobs).map((j) => (
                <ScheduleCard
                  key={j.id}
                  job={j}
                  runs={runsByJob[j.id] || recentRuns.filter((r) => r.job_id === j.id)}
                  focused={selectedJobId === j.id}
                  now={now}
                  onClick={() => selectJob(selectedJobId === j.id ? null : j.id)}
                />
              ))}
            </div>

            {/* Run feed (right column) */}
            <div className="flex w-[440px] shrink-0 flex-col gap-3 border-l border-white/[0.06] px-5 py-5">
              <div className="flex items-center justify-between">
                <span className="font-mono text-[10.5px] uppercase tracking-[0.14em] text-fg-3">
                  {focusedJob ? `runs · ${focusedJob.name}` : 'recent runs · all schedules'}
                </span>
                <span className="font-mono text-[10px] text-fg-3">
                  {filteredRuns.length}
                </span>
              </div>
              <div className="flex gap-1.5">
                {(['all', 'succeeded', 'failed', 'running'] as RunFilter[]).map((f) => (
                  <button
                    key={f}
                    type="button"
                    onClick={() => setRunFilter(f)}
                    className={`rounded px-2 py-1 font-mono text-[10px] uppercase tracking-[0.12em] transition ${
                      runFilter === f
                        ? 'bg-white/[0.10] text-fg-0 ring-1 ring-white/[0.18]'
                        : 'bg-white/[0.02] text-fg-2 hover:bg-white/[0.05] hover:text-fg-0'
                    }`}
                  >
                    {f}
                  </button>
                ))}
              </div>
              <div className="flex min-h-0 flex-1 flex-col gap-1 overflow-y-auto">
                {filteredRuns.length === 0 ? (
                  <div className="rounded border border-dashed border-white/[0.08] p-4 text-center font-mono text-[11px] text-fg-3">
                    {focusedJob ? 'this schedule has no runs yet'
                      : 'no runs to show — try a different filter'}
                  </div>
                ) : (
                  filteredRuns.map((r) => (
                    <RunRow
                      key={r.run_id}
                      run={r}
                      showJobName={!selectedJobId}
                      selected={r.run_id === selectedRunId}
                      onClick={() => selectRun(r.run_id === selectedRunId ? null : r.run_id)}
                    />
                  ))
                )}
              </div>
            </div>

            {/* Run detail drawer (right-most column when a run is selected) */}
            {selectedRun && (
              <RunDetailDrawer
                run={selectedRun}
                job={jobs.find((j) => j.id === selectedRun.job_id) || null}
                onClose={() => selectRun(null)}
              />
            )}
          </>
        )}
      </div>

      {/* New-schedule modal */}
      {creating && (
        <NewScheduleModal
          onClose={() => setCreating(false)}
          onCreated={() => {
            setCreating(false)
            schedulerApi.listJobs().catch(() => {})
          }}
        />
      )}
    </div>
  )
}

// ─── Header chips ───────────────────────────────────────────────────

function NextFireChip(props: {
  label: string
  countdown: string
  jobId: string | null
  onClick: (jobId: string | null) => void
}) {
  return (
    <button
      type="button"
      onClick={() => props.onClick(props.jobId)}
      title={props.label ? `next fire: ${props.label}` : 'next scheduled fire'}
      className="inline-flex items-center gap-1.5 rounded border border-accent/[0.18] bg-accent/[0.06] px-2 py-1 font-mono text-fg-1 transition hover:bg-accent/[0.10] hover:text-fg-0"
    >
      <span className="h-1.5 w-1.5 animate-pulse-soft rounded-full bg-accent" />
      <span>next</span>
      <span className="text-fg-0">{props.countdown}</span>
      {props.label && (
        <>
          <span className="text-fg-3">·</span>
          <span className="max-w-[120px] truncate normal-case text-fg-1">
            {props.label}
          </span>
        </>
      )}
    </button>
  )
}

function DaemonChip(props: { status: DaemonStatus | null; onClick: () => void }) {
  const supported = props.status?.supported ?? false
  const installed = props.status?.installed ?? false
  const running = props.status?.running ?? false
  let dotClass = 'bg-fg-2'
  let label = 'daemon ?'
  if (!supported) {
    dotClass = 'bg-fg-2'
    label = 'daemon n/a'
  } else if (installed && running) {
    dotClass = 'bg-ok'
    label = 'daemon live'
  } else if (installed && !running) {
    dotClass = 'bg-warn'
    label = 'daemon stopped'
  } else {
    dotClass = 'bg-warn'
    label = 'daemon off'
  }
  return (
    <button
      type="button"
      onClick={props.onClick}
      className="inline-flex items-center gap-1.5 rounded border border-white/[0.08] bg-white/[0.03] px-2 py-1 font-mono text-fg-1 transition hover:bg-white/[0.06] hover:text-fg-0"
    >
      <span className={`h-1.5 w-1.5 rounded-full ${dotClass}`} />
      <span>{label}</span>
    </button>
  )
}

// ─── Daemon strip (collapsible) ─────────────────────────────────────

function DaemonStrip(props: { status: DaemonStatus | null; onClose: () => void }) {
  const s = props.status
  const [busy, setBusy] = useState(false)
  const install = async () => {
    setBusy(true)
    try {
      await schedulerApi.daemonInstall('renderer_button')
      await schedulerApi.daemonStatus()
    } finally { setBusy(false) }
  }
  const uninstall = async () => {
    setBusy(true)
    try {
      await schedulerApi.daemonUninstall()
      await schedulerApi.daemonStatus()
    } finally { setBusy(false) }
  }
  return (
    <div className="flex shrink-0 items-center justify-between gap-6 border-b border-white/[0.06] bg-white/[0.02] px-6 py-3 font-mono text-[11px] text-fg-1">
      <div className="flex flex-col gap-1">
        <span className="text-fg-2">
          Background daemon — schedules fire even when Freyja is closed.
        </span>
        {s ? (
          <span className="text-[10.5px] text-fg-3">
            supported: {String(s.supported)} ·{' '}
            installed: {s.installed ? 'yes' : 'no'} ·{' '}
            running: {s.running ? `yes (pid ${s.pid ?? '—'})` : 'no'}
            {s.plist ? <> · plist <span className="text-fg-2">{s.plist}</span></> : null}
          </span>
        ) : (
          <span className="text-fg-3">loading…</span>
        )}
      </div>
      <div className="flex gap-2">
        {s?.installed
          ? <button disabled={busy} onClick={uninstall}
              className="rounded border border-white/[0.10] bg-white/[0.04] px-3 py-1 uppercase tracking-[0.12em] text-fg-1 transition hover:bg-white/[0.08] disabled:opacity-40">
              uninstall
            </button>
          : <button disabled={busy || !s?.supported} onClick={install}
              className="rounded border border-accent/[0.30] bg-accent/[0.10] px-3 py-1 uppercase tracking-[0.12em] text-accent transition hover:bg-accent/[0.18] disabled:opacity-40">
              install daemon
            </button>}
        <button type="button" onClick={props.onClose}
          className="rounded border border-white/[0.06] bg-white/[0.02] px-3 py-1 uppercase tracking-[0.18em] text-fg-3 transition hover:text-fg-0">
          collapse
        </button>
      </div>
    </div>
  )
}

// ─── Empty state ─────────────────────────────────────────────────────

function EmptyState(props: { onCreate: () => void }) {
  return (
    <div className="m-auto flex max-w-[680px] flex-col items-start gap-6 px-8 py-12">
      <h2 className="m-0 font-serif text-[34px] font-light leading-[1.1] text-fg-0">
        Freyja can keep working without you.
      </h2>
      <p className="m-0 font-mono text-[12px] leading-[1.6] text-fg-2">
        Schedules run prompts at the times you choose — once, on an interval,
        on a cron, or self-paced — and deliver the output to Slack, your
        desktop, a file, or a webhook.
      </p>
      <div className="flex flex-col gap-3 font-mono text-[11.5px] text-fg-1">
        <Path label="from the chat" body={'“remind me in 30 min to check the deploy”'} />
        <Path label="from Slack" body={'/freyja remind every weekday at 9am — review yesterday’s metrics'} />
        <Path label="from this modal" body="click + new schedule above" />
      </div>
      <button type="button" onClick={props.onCreate}
        className="mt-2 rounded-md border border-accent/[0.30] bg-accent/[0.10] px-4 py-2 font-mono text-[11px] uppercase tracking-[0.14em] text-accent transition hover:bg-accent/[0.18]">
        create your first schedule
      </button>
    </div>
  )
}

function Path(props: { label: string; body: string }) {
  return (
    <div className="flex flex-col gap-1 rounded border border-white/[0.06] bg-white/[0.02] px-3 py-2">
      <span className="text-[10px] uppercase tracking-[0.14em] text-fg-3">{props.label}</span>
      <span className="text-fg-1">{props.body}</span>
    </div>
  )
}

// ─── Schedule card ───────────────────────────────────────────────────

function ScheduleCard(props: {
  job: SchedulerJob
  runs: SchedulerRun[]
  focused: boolean
  now: number
  onClick: () => void
}) {
  const { job, runs, focused, now } = props
  const [busy, setBusy] = useState<string | null>(null)

  const act = async (verb: string, fn: () => Promise<unknown>) => {
    setBusy(verb)
    try {
      await fn()
      schedulerApi.listJobs().catch(() => {})
    } finally { setBusy(null) }
  }

  const cadence = formatSchedule(job.schedule)
  const lastRun = runs[0]
  const lastStatus = lastRun?.status

  return (
    <div
      onClick={props.onClick}
      className={`group cursor-pointer rounded-lg border ${
        focused ? 'border-accent/[0.30] bg-accent/[0.04]'
                : 'border-white/[0.06] bg-white/[0.02] hover:bg-white/[0.04] hover:border-white/[0.12]'
      } px-4 py-3.5 transition`}
    >
      <div className="flex items-start gap-3">
        <StatusDot status={job.status} enabled={job.enabled} />
        <div className="flex min-w-0 flex-1 flex-col gap-1">
          <div className="flex items-center gap-2">
            <h3 className="m-0 truncate font-serif text-[17px] font-normal leading-tight text-fg-0">
              {job.name}
            </h3>
            <span className={`rounded px-1.5 py-0.5 font-mono text-[9.5px] uppercase tracking-[0.12em] ${statusPillClass(job.status)}`}>
              {job.enabled ? job.status : 'paused'}
            </span>
          </div>
          <div className="flex flex-wrap items-center gap-x-3 gap-y-1 font-mono text-[10.5px] text-fg-2">
            <span>{cadence}</span>
            <span className="text-fg-3">·</span>
            <span>
              next <span className="text-fg-1">{formatRelative(job.next_fire_at, now)}</span>
            </span>
            {job.last_fire_at && (
              <>
                <span className="text-fg-3">·</span>
                <span>
                  last <span className="text-fg-1">{formatRelative(job.last_fire_at, now)}</span>
                </span>
              </>
            )}
            <span className="text-fg-3">·</span>
            <span>
              {job.fire_count}
              {job.max_fires ? `/${job.max_fires}` : ''} fires
            </span>
          </div>
        </div>
      </div>

      {/* 14-day timeline strip */}
      <div className="mt-3 flex items-center gap-3">
        <TimelineStrip runs={runs} now={now} nextFireAt={job.next_fire_at ?? null} />
      </div>

      {/* Sinks */}
      <div className="mt-3 flex flex-wrap gap-1.5">
        {(job.sinks || []).map((s, i) => (
          <SinkChip key={i} sink={s as Record<string, unknown>} />
        ))}
        {job.execution && (
          <span className="ml-auto inline-flex items-center gap-1 rounded bg-white/[0.04] px-2 py-1 font-mono text-[10px] uppercase tracking-[0.10em] text-fg-3">
            <span>exec</span>
            <span className="text-fg-1">{(job.execution as any)?.kind || '?'}</span>
          </span>
        )}
      </div>

      {/* Action row — revealed on focus or hover */}
      <div
        onClick={(e) => e.stopPropagation()}
        className={`mt-3 flex gap-2 transition-opacity ${
          focused ? 'opacity-100' : 'opacity-0 group-hover:opacity-100'
        }`}
      >
        {job.enabled ? (
          <CardButton onClick={() => act('pause', () => schedulerApi.pauseJob(job.id))}
            disabled={busy !== null} label="pause" />
        ) : (
          <CardButton onClick={() => act('resume', () => schedulerApi.resumeJob(job.id))}
            disabled={busy !== null} label="resume" accent />
        )}
        <CardButton onClick={() => act('run', () => schedulerApi.runJobNow(job.id))}
          disabled={busy !== null} label="run now" />
        <CardButton onClick={() => act('remove', async () => {
          if (!window.confirm(`Delete schedule “${job.name}”? Past runs are kept.`)) return
          await schedulerApi.removeJob(job.id)
        })}
          disabled={busy !== null} label="delete" danger />
        {lastStatus && (
          <span className="ml-auto self-center font-mono text-[10px] text-fg-3">
            last run: <span className={runStatusTextClass(lastStatus)}>{lastStatus}</span>
          </span>
        )}
      </div>
    </div>
  )
}

function CardButton(props: {
  label: string
  onClick: () => void
  disabled?: boolean
  accent?: boolean
  danger?: boolean
}) {
  const tone = props.danger
    ? 'border-danger/[0.30] text-danger hover:bg-danger/[0.10]'
    : props.accent
      ? 'border-accent/[0.30] text-accent hover:bg-accent/[0.10]'
      : 'border-white/[0.10] text-fg-1 hover:bg-white/[0.06] hover:text-fg-0'
  return (
    <button
      type="button"
      onClick={props.onClick}
      disabled={props.disabled}
      className={`rounded border bg-white/[0.02] px-2.5 py-1 font-mono text-[10px] uppercase tracking-[0.14em] transition disabled:opacity-40 ${tone}`}
    >
      {props.label}
    </button>
  )
}

function StatusDot(props: { status: string; enabled: boolean }) {
  let cls = 'bg-fg-2'
  if (!props.enabled) cls = 'bg-warn'
  else if (props.status === 'active') cls = 'bg-ok'
  else if (props.status === 'paused') cls = 'bg-warn'
  else if (props.status === 'running') cls = 'bg-accent animate-pulse-soft'
  else if (props.status === 'disabled') cls = 'bg-fg-3'
  return (
    <span className={`mt-1.5 h-2 w-2 shrink-0 rounded-full ${cls}`} aria-hidden />
  )
}

function statusPillClass(status: string): string {
  if (status === 'active') return 'bg-ok/[0.10] text-ok ring-1 ring-ok/[0.18]'
  if (status === 'paused') return 'bg-warn/[0.10] text-warn ring-1 ring-warn/[0.18]'
  if (status === 'running') return 'bg-accent/[0.10] text-accent ring-1 ring-accent/[0.18]'
  if (status === 'disabled') return 'bg-white/[0.04] text-fg-3 ring-1 ring-white/[0.06]'
  return 'bg-white/[0.04] text-fg-2 ring-1 ring-white/[0.06]'
}

function runStatusTextClass(status: string): string {
  if (status === 'succeeded') return 'text-ok'
  if (status === 'failed' || status === 'timed_out') return 'text-danger'
  if (status === 'partial_failure') return 'text-warn'
  if (status === 'running') return 'text-accent'
  if (status === 'cancelled') return 'text-fg-3'
  return 'text-fg-1'
}

// ─── 14-day timeline strip ───────────────────────────────────────────

function TimelineStrip(props: {
  runs: SchedulerRun[]
  now: number
  nextFireAt: number | null
}) {
  // Build 14 day-buckets. Each cell shows: filled = had ≥1 run;
  // colored by worst status in the bucket; outlined ring = today;
  // accent rim = next fire lands in this bucket.
  const DAYS = 14
  const todayStart = startOfDay(props.now)
  const cells: { day: number; runs: SchedulerRun[]; isToday: boolean; isNext: boolean }[] = []
  const nextFireDay = props.nextFireAt
    ? Math.floor((startOfDay(props.nextFireAt) - todayStart) / 86400)
    : null
  for (let i = -DAYS + 1; i <= 0; i++) {
    const dayStart = todayStart + i * 86400
    const dayEnd = dayStart + 86400
    const bucket = props.runs.filter(
      (r) => r.started_at >= dayStart && r.started_at < dayEnd,
    )
    cells.push({ day: i, runs: bucket, isToday: i === 0, isNext: false })
  }
  // Append future days up to where the next fire lands (max 7 ahead).
  const futureSpan = nextFireDay !== null ? Math.min(nextFireDay, 7) : 0
  for (let i = 1; i <= futureSpan; i++) {
    cells.push({ day: i, runs: [], isToday: false, isNext: i === nextFireDay })
  }
  return (
    <div className="flex h-7 flex-1 items-stretch gap-[3px]" title="14-day fire history">
      {cells.map((c, i) => {
        const worst = worstStatus(c.runs)
        const color =
          worst === 'failed' ? 'bg-danger/70'
            : worst === 'partial_failure' ? 'bg-warn/70'
              : worst === 'succeeded' ? 'bg-ok/60'
                : worst === 'running' ? 'bg-accent/70'
                  : 'bg-white/[0.04]'
        return (
          <div
            key={i}
            title={timelineTitle(c)}
            className={`relative flex-1 rounded-[2px] ${color} ${
              c.isToday ? 'outline outline-1 outline-white/[0.18]' : ''
            } ${
              c.isNext ? 'ring-1 ring-accent/[0.50] bg-accent/[0.10]' : ''
            }`}
          >
            {c.runs.length > 1 && (
              <span className="absolute inset-0 flex items-center justify-center font-mono text-[8.5px] text-fg-0/90">
                {c.runs.length}
              </span>
            )}
          </div>
        )
      })}
    </div>
  )
}

function startOfDay(ts: number): number {
  const d = new Date(ts * 1000)
  d.setHours(0, 0, 0, 0)
  return Math.floor(d.getTime() / 1000)
}

function worstStatus(runs: SchedulerRun[]): string {
  if (runs.length === 0) return ''
  const order = ['failed', 'timed_out', 'partial_failure', 'running', 'succeeded']
  for (const s of order) {
    if (runs.some((r) => r.status === s)) return s === 'timed_out' ? 'failed' : s
  }
  return runs[0].status
}

function timelineTitle(cell: { day: number; runs: SchedulerRun[]; isNext: boolean }): string {
  if (cell.day === 0) return `today — ${cell.runs.length} run(s)`
  if (cell.day < 0) return `${-cell.day}d ago — ${cell.runs.length} run(s)`
  if (cell.isNext) return `next fire — in ${cell.day}d`
  return `+${cell.day}d`
}

// ─── Sink chip ───────────────────────────────────────────────────────

function SinkChip(props: { sink: Record<string, unknown> }) {
  const k = String(props.sink.kind || '?')
  let dest = ''
  if (k === 'slack') dest = String(props.sink.chat_id || '#?')
  else if (k === 'desktop') dest = String(props.sink.session_id || '').slice(0, 12)
  else if (k === 'filesystem') dest = String(props.sink.path_template || '?')
  else if (k === 'webhook') dest = shortUrl(String(props.sink.url || '?'))
  return (
    <span
      title={`${k} → ${dest}`}
      className="inline-flex items-center gap-1.5 rounded border border-white/[0.08] bg-white/[0.04] px-2 py-1 font-mono text-[10px] text-fg-1"
    >
      <span className="uppercase tracking-[0.10em] text-fg-3">{k}</span>
      {dest && (
        <>
          <span className="text-fg-3">→</span>
          <span className="max-w-[160px] truncate text-fg-1">{dest}</span>
        </>
      )}
    </span>
  )
}

function shortUrl(u: string): string {
  try {
    const url = new URL(u)
    return url.host + url.pathname.slice(0, 24)
  } catch {
    return u.slice(0, 30)
  }
}

// ─── Run row + drawer ───────────────────────────────────────────────

function RunRow(props: {
  run: SchedulerRun
  showJobName: boolean
  selected: boolean
  onClick: () => void
}) {
  const r = props.run
  return (
    <button
      type="button"
      onClick={props.onClick}
      className={`flex flex-col gap-1 rounded border px-3 py-2 text-left transition ${
        props.selected
          ? 'border-accent/[0.30] bg-accent/[0.06]'
          : 'border-white/[0.06] bg-white/[0.02] hover:bg-white/[0.05]'
      }`}
    >
      <div className="flex items-center gap-2 font-mono text-[10.5px]">
        <span className={`h-1.5 w-1.5 shrink-0 rounded-full ${
          r.status === 'succeeded' ? 'bg-ok'
            : r.status === 'failed' || r.status === 'timed_out' ? 'bg-danger'
              : r.status === 'partial_failure' ? 'bg-warn'
                : r.status === 'running' ? 'bg-accent animate-pulse-soft'
                  : 'bg-fg-3'
        }`} />
        <span className={`uppercase tracking-[0.10em] ${runStatusTextClass(r.status)}`}>
          {r.status}
        </span>
        {props.showJobName && (
          <span className="max-w-[160px] truncate text-fg-1">{r.job_name}</span>
        )}
        <span className="ml-auto tabular-nums text-fg-3">
          {formatRelative(r.started_at, Math.floor(Date.now() / 1000))}
        </span>
      </div>
      <div className="flex items-center gap-3 font-mono text-[10px] text-fg-2">
        <span>{(r.duration_seconds ?? 0).toFixed(1)}s</span>
        {typeof r.cost_usd === 'number' && (
          <>
            <span className="text-fg-3">·</span>
            <span>${(r.cost_usd || 0).toFixed(4)}</span>
          </>
        )}
        {(r.delivery_reports || []).length > 0 && (
          <>
            <span className="text-fg-3">·</span>
            <span className="flex gap-1">
              {(r.delivery_reports || []).map((d, i) => (
                <span
                  key={i}
                  title={d.error || d.artifact_ref || `${d.sink_kind} ${d.success ? 'ok' : 'failed'}`}
                  className={`h-1.5 w-1.5 rounded-full ${d.success ? 'bg-ok' : 'bg-danger'}`}
                />
              ))}
            </span>
          </>
        )}
      </div>
      {r.output_text && (
        <span className="truncate font-mono text-[10.5px] italic text-fg-3">
          {r.output_text.slice(0, 120)}
        </span>
      )}
    </button>
  )
}

function RunDetailDrawer(props: {
  run: SchedulerRun
  job: SchedulerJob | null
  onClose: () => void
}) {
  const { run, job } = props
  const openSessionPane = useHarness((s) => s.openSessionPane)
  const sessionId = (run as any).session_id as string | undefined

  const openSession = useCallback(async () => {
    if (!sessionId) return
    await openSessionPane(sessionId, 'replace').catch(() => {})
    // Close the scheduler modal — user has been handed off to the
    // live session.
    useSchedulerStore.getState().closeDashboard()
  }, [sessionId, openSessionPane])

  const statusLabel = run.status === 'running' ? 'in flight'
    : run.status === 'succeeded' ? 'completed'
      : run.status === 'failed' ? 'failed'
        : run.status === 'timed_out' ? 'timed out'
          : run.status === 'partial_failure' ? 'partial delivery failure'
            : run.status

  return (
    <DetailDrawer
      open
      onClose={props.onClose}
      width={560}
      title={job?.name || run.job_name || 'Scheduled run'}
      statusLabel={statusLabel}
      footer={
        <div className="flex items-center gap-2">
          {sessionId && (
            <button
              type="button"
              onClick={openSession}
              className="rounded border border-accent/[0.30] bg-accent/[0.10] px-3 py-1.5 font-mono text-[10.5px] uppercase tracking-[0.14em] text-accent transition hover:bg-accent/[0.18]"
            >
              open session →
            </button>
          )}
          {run.status === 'running' && (
            <button
              type="button"
              onClick={() => schedulerApi.cancelRun(run.run_id).catch(() => {})}
              className="rounded border border-danger/[0.30] bg-danger/[0.06] px-3 py-1.5 font-mono text-[10.5px] uppercase tracking-[0.14em] text-danger transition hover:bg-danger/[0.10]"
            >
              cancel run
            </button>
          )}
          <span className="ml-auto font-mono text-[10px] text-fg-3">{run.run_id}</span>
        </div>
      }
    >
      <DrawerSection label="run">
        <dl className="grid grid-cols-[max-content_1fr] gap-x-4 gap-y-1.5 font-mono text-[11.5px]">
          <dt className="text-fg-3">started</dt>
          <dd className="text-fg-1">{formatAbsolute(run.started_at)}</dd>
          <dt className="text-fg-3">finished</dt>
          <dd className="text-fg-1">{run.finished_at ? formatAbsolute(run.finished_at) : '—'}</dd>
          <dt className="text-fg-3">duration</dt>
          <dd className="text-fg-1">{(run.duration_seconds ?? 0).toFixed(2)}s</dd>
          <dt className="text-fg-3">fire #</dt>
          <dd className="text-fg-1">{run.fire_number}{run.iteration > 1 ? ` (iter ${run.iteration})` : ''}</dd>
          {(typeof run.input_tokens === 'number' || typeof run.output_tokens === 'number') && (
            <>
              <dt className="text-fg-3">tokens</dt>
              <dd className="text-fg-1">
                in {run.input_tokens ?? '—'} · out {run.output_tokens ?? '—'}
              </dd>
            </>
          )}
          {typeof run.cost_usd === 'number' && (
            <>
              <dt className="text-fg-3">cost</dt>
              <dd className="text-fg-1">${run.cost_usd.toFixed(4)}</dd>
            </>
          )}
        </dl>
      </DrawerSection>

      {run.error && (
        <DrawerSection label="error">
          <pre className="m-0 whitespace-pre-wrap rounded border border-danger/[0.18] bg-danger/[0.06] p-3 font-mono text-[11px] text-danger">
            {run.error}
          </pre>
        </DrawerSection>
      )}

      <DrawerSection label="output">
        {run.output_text ? (
          <pre className="m-0 max-h-[420px] overflow-auto whitespace-pre-wrap rounded border border-white/[0.06] bg-black/[0.30] p-3 font-mono text-[11.5px] leading-[1.55] text-fg-0">
            {run.output_text}
          </pre>
        ) : (
          <span className="font-mono text-[11px] italic text-fg-3">
            (no output captured)
          </span>
        )}
      </DrawerSection>

      <DrawerSection label="delivery">
        {(run.delivery_reports || []).length === 0 ? (
          <span className="font-mono text-[11px] italic text-fg-3">no sinks reported</span>
        ) : (
          <ul className="m-0 flex flex-col gap-1.5 p-0">
            {(run.delivery_reports || []).map((d, i) => (
              <li key={i} className="flex items-start gap-2 rounded border border-white/[0.06] bg-white/[0.02] px-3 py-2 font-mono text-[11px]">
                <span className={`mt-1 h-1.5 w-1.5 shrink-0 rounded-full ${d.success ? 'bg-ok' : 'bg-danger'}`} />
                <span className="flex min-w-0 flex-1 flex-col">
                  <span className="text-fg-0">{d.sink_kind}</span>
                  <span className="text-fg-3">
                    {formatAbsolute(d.delivered_at)}
                    {d.artifact_ref ? <> · <span className="text-fg-2">{d.artifact_ref}</span></> : null}
                  </span>
                  {d.error && (
                    <span className="mt-1 whitespace-pre-wrap text-danger">{d.error}</span>
                  )}
                </span>
              </li>
            ))}
          </ul>
        )}
      </DrawerSection>

      {job?.prompt && (
        <DrawerSection label="prompt (as fired)">
          <pre className="m-0 max-h-[280px] overflow-auto whitespace-pre-wrap rounded border border-white/[0.06] bg-black/[0.20] p-3 font-mono text-[11px] leading-[1.55] text-fg-1">
            {job.prompt}
          </pre>
        </DrawerSection>
      )}
    </DetailDrawer>
  )
}

// ─── New schedule modal ──────────────────────────────────────────────

const SUGGESTIONS = [
  'in 30 minutes',
  'tomorrow at 9am',
  'every weekday at 9am',
  'every Monday at 10am',
  'every 6 hours',
  'every 15 minutes',
]

function NewScheduleModal(props: { onClose: () => void; onCreated: () => void }) {
  const [name, setName] = useState('')
  const [prompt, setPrompt] = useState('')
  const [when, setWhen] = useState('')
  const [sinks, setSinks] = useState('here')
  const [execution, setExecution] = useState<'new_session' | 'persistent_job_session' | 'here'>('new_session')
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [preview, setPreview] = useState<number[] | null>(null)
  const [previewError, setPreviewError] = useState<string | null>(null)

  // Debounced live preview of the next 5 fires.
  useEffect(() => {
    const trimmed = when.trim()
    if (!trimmed) {
      setPreview(null)
      setPreviewError(null)
      return
    }
    setPreviewError(null)
    const handle = setTimeout(() => {
      schedulerApi
        .previewNextFires({ when: trimmed, n: 5 })
        .then((resp) => {
          if (resp.error) {
            setPreview(null)
            setPreviewError(resp.error)
          } else {
            setPreview(resp.fires || [])
          }
        })
        .catch((e) => {
          setPreview(null)
          setPreviewError(String(e))
        })
    }, 300)
    return () => clearTimeout(handle)
  }, [when])

  const submit = async (ev: React.FormEvent) => {
    ev.preventDefault()
    setSubmitting(true)
    setError(null)
    try {
      const sinkList = sinks.split(',').map((s) => s.trim()).filter(Boolean)
      const resp: any = await schedulerApi.createJob({
        name: name.trim() || undefined,
        prompt: prompt.trim(),
        when: when.trim(),
        execution,
        sinks: sinkList,
      })
      if (resp?.error) {
        setError(resp.error)
        return
      }
      props.onCreated()
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <div
      className="fixed inset-0 z-[60] flex items-center justify-center bg-black/40 backdrop-blur-[6px]"
      onClick={props.onClose}
    >
      <form
        onSubmit={submit}
        onClick={(e) => e.stopPropagation()}
        className="modal-opaque flex max-h-[88vh] w-[680px] flex-col overflow-hidden rounded-xl"
      >
        <header className="flex items-center justify-between border-b border-white/[0.06] px-6 py-4">
          <h2 className="m-0 font-serif text-[20px] font-light leading-tight text-fg-0">
            New schedule
          </h2>
          <button type="button" onClick={props.onClose}
            className="font-mono text-[10.5px] uppercase tracking-[0.18em] text-fg-3 transition hover:text-fg-0">
            esc · close
          </button>
        </header>

        <div className="flex min-h-0 flex-1 flex-col gap-4 overflow-y-auto px-6 py-5 font-mono text-[12px]">
          <Field label="name" optional>
            <input value={name} onChange={(e) => setName(e.target.value)}
              placeholder="auto-derived from prompt if blank"
              className="w-full rounded border border-white/[0.10] bg-black/[0.30] px-3 py-2 text-fg-0 placeholder:text-fg-3 focus:border-accent/[0.40] focus:outline-none" />
          </Field>

          <Field label="when">
            <div className="flex flex-col gap-2">
              <input value={when} onChange={(e) => setWhen(e.target.value)}
                placeholder='"every weekday at 9am", "in 30 minutes", "tomorrow at 5pm"'
                required
                className="w-full rounded border border-white/[0.10] bg-black/[0.30] px-3 py-2 text-fg-0 placeholder:text-fg-3 focus:border-accent/[0.40] focus:outline-none" />
              <div className="flex flex-wrap gap-1.5">
                {SUGGESTIONS.map((s) => (
                  <button
                    key={s}
                    type="button"
                    onClick={() => setWhen(s)}
                    className="rounded border border-white/[0.08] bg-white/[0.02] px-2 py-1 text-[10.5px] text-fg-2 transition hover:bg-white/[0.06] hover:text-fg-0"
                  >
                    {s}
                  </button>
                ))}
              </div>
              <PreviewStrip preview={preview} error={previewError} hasInput={when.trim().length > 0} />
            </div>
          </Field>

          <Field label="prompt">
            <textarea value={prompt} onChange={(e) => setPrompt(e.target.value)}
              rows={6} required
              placeholder="Self-contained prompt the agent will run at fire time."
              className="w-full resize-y rounded border border-white/[0.10] bg-black/[0.30] px-3 py-2 font-mono text-[11.5px] leading-[1.55] text-fg-0 placeholder:text-fg-3 focus:border-accent/[0.40] focus:outline-none" />
          </Field>

          <Field label="sinks">
            <input value={sinks} onChange={(e) => setSinks(e.target.value)}
              placeholder="here · slack · desktop · session · laptop:/path · https://hook"
              className="w-full rounded border border-white/[0.10] bg-black/[0.30] px-3 py-2 text-fg-0 placeholder:text-fg-3 focus:border-accent/[0.40] focus:outline-none" />
          </Field>

          <Field label="execution">
            <div className="flex flex-wrap gap-2">
              {([
                ['new_session', 'fresh ephemeral session per fire'],
                ['persistent_job_session', 'memory carries across fires'],
                ['here', 'fire into the current desktop session'],
              ] as const).map(([k, desc]) => (
                <button
                  key={k}
                  type="button"
                  onClick={() => setExecution(k)}
                  className={`flex flex-col items-start gap-0.5 rounded border px-3 py-2 text-left transition ${
                    execution === k
                      ? 'border-accent/[0.30] bg-accent/[0.08] text-fg-0'
                      : 'border-white/[0.08] bg-white/[0.02] text-fg-2 hover:bg-white/[0.05]'
                  }`}
                >
                  <span className="text-[10.5px] uppercase tracking-[0.12em]">{k}</span>
                  <span className="text-[10px] text-fg-3">{desc}</span>
                </button>
              ))}
            </div>
          </Field>

          {error && (
            <div className="rounded border border-danger/[0.30] bg-danger/[0.06] px-3 py-2 text-[11px] text-danger">
              {error}
            </div>
          )}
        </div>

        <footer className="flex items-center justify-end gap-2 border-t border-white/[0.06] bg-black/[0.30] px-6 py-3">
          <button type="button" onClick={props.onClose}
            className="rounded border border-white/[0.10] bg-white/[0.02] px-3 py-1.5 font-mono text-[10.5px] uppercase tracking-[0.14em] text-fg-2 transition hover:bg-white/[0.06] hover:text-fg-0">
            cancel
          </button>
          <button type="submit" disabled={submitting || !prompt.trim() || !when.trim()}
            className="rounded border border-accent/[0.30] bg-accent/[0.10] px-4 py-1.5 font-mono text-[10.5px] uppercase tracking-[0.14em] text-accent transition hover:bg-accent/[0.18] disabled:opacity-40">
            {submitting ? 'creating…' : 'create schedule'}
          </button>
        </footer>
      </form>
    </div>
  )
}

function Field(props: { label: string; optional?: boolean; children: React.ReactNode }) {
  return (
    <label className="flex flex-col gap-1.5">
      <span className="flex items-center gap-2 text-[10.5px] uppercase tracking-[0.14em] text-fg-3">
        {props.label}
        {props.optional && <span className="text-fg-3/70">· optional</span>}
      </span>
      {props.children}
    </label>
  )
}

function PreviewStrip(props: {
  preview: number[] | null
  error: string | null
  hasInput: boolean
}) {
  if (!props.hasInput) {
    return (
      <div className="rounded border border-dashed border-white/[0.06] px-3 py-2 text-[10.5px] text-fg-3">
        next 5 fires preview appears here once you describe the schedule
      </div>
    )
  }
  if (props.error) {
    return (
      <div className="rounded border border-warn/[0.18] bg-warn/[0.06] px-3 py-2 text-[10.5px] text-warn">
        couldn’t parse: {props.error}
      </div>
    )
  }
  if (!props.preview) {
    return (
      <div className="rounded border border-white/[0.06] px-3 py-2 text-[10.5px] text-fg-3">
        computing…
      </div>
    )
  }
  if (props.preview.length === 0) {
    return (
      <div className="rounded border border-white/[0.08] bg-white/[0.02] px-3 py-2 text-[10.5px] text-fg-2">
        this schedule has no future fires
      </div>
    )
  }
  const now = Math.floor(Date.now() / 1000)
  return (
    <div className="rounded border border-accent/[0.18] bg-accent/[0.04] px-3 py-2">
      <div className="mb-1 text-[10px] uppercase tracking-[0.14em] text-accent">
        next {props.preview.length} fires
      </div>
      <ol className="m-0 flex flex-col gap-0.5 p-0">
        {props.preview.map((ts, i) => (
          <li key={i} className="flex items-baseline justify-between text-[11px]">
            <span className="text-fg-0">{formatAbsolute(ts)}</span>
            <span className="text-fg-3">in {humanCountdown(ts - now)}</span>
          </li>
        ))}
      </ol>
    </div>
  )
}

// ─── Helpers ─────────────────────────────────────────────────────────

function formatSchedule(s: Record<string, unknown> | undefined | null): string {
  if (!s) return '—'
  const k = String((s as any).kind || '')
  if (k === 'once') return `once · ${formatAbsolute(toEpoch((s as any).at_iso))}`
  if (k === 'interval') return `every ${humanizeSeconds((s as any).seconds)}`
  if (k === 'cron') return `cron ${(s as any).expression} · ${(s as any).timezone || 'UTC'}`
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

function toEpoch(iso: string | undefined | null): number {
  if (!iso) return 0
  const t = Date.parse(iso)
  return isNaN(t) ? 0 : Math.floor(t / 1000)
}

function formatAbsolute(ts: number | undefined | null): string {
  if (!ts) return '—'
  return new Date(ts * 1000).toLocaleString(undefined, {
    month: 'short', day: 'numeric',
    hour: 'numeric', minute: '2-digit',
  })
}

function formatRelative(ts: number | undefined | null, now: number): string {
  if (!ts) return '—'
  const dt = ts - now
  const abs = Math.abs(dt)
  let rel: string
  if (abs < 60) rel = `${Math.round(abs)}s`
  else if (abs < 3600) rel = `${Math.round(abs / 60)}m`
  else if (abs < 86400) rel = `${Math.round(abs / 3600)}h`
  else rel = `${Math.round(abs / 86400)}d`
  return dt >= 0 ? `in ${rel}` : `${rel} ago`
}

function humanCountdown(seconds: number): string {
  const s = Math.max(0, Math.floor(seconds))
  if (s < 60) return `${s}s`
  if (s < 3600) return `${Math.floor(s / 60)}m`
  if (s < 86400) return `${Math.floor(s / 3600)}h ${Math.floor((s % 3600) / 60)}m`
  return `${Math.floor(s / 86400)}d ${Math.floor((s % 86400) / 3600)}h`
}
