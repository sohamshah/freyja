import { useCallback, useEffect, useMemo, useState } from 'react'
import { aggregateSessionCost, useHarness, type SystemEventRecord } from '../state/store'
import { formatTokens, formatCost, relativeTime } from '../lib/format'
import { ComputerLiveView } from './ComputerLiveView'
import { LogStreamModal } from './LogStreamModal'
import { ArtifactsSection } from './ArtifactsSection'
import { ToolTimeline } from './ToolTimeline'
import { ChangesSection } from './ChangesSection'
import { TopoBackdrop } from './TopoBackdrop'

export function ActivityPanel() {
  const systemEvents = useHarness((s) => s.systemEvents)
  const usage = useHarness((s) => s.usage)
  const logs = useHarness((s) => s.logs)
  const toggleActivityPanel = useHarness((s) => s.toggleActivityPanel)
  const panelWidth = useHarness((s) => s.activityPanelWidth)
  const setPanelWidth = useHarness((s) => s.setActivityPanelWidth)
  // Session spend = this session's own cost + every descendant
  // subagent's cost. The selector reads `state.sessions`,
  // `state.sessionArchive`, and the active slice so it works whether a
  // subagent is currently loaded or not.
  const activeSessionId = useHarness((s) => s.activeSessionId)
  const subagentSpend = useHarness(
    (s) => aggregateSessionCost(s, s.activeSessionId) - (s.usage?.totalCost ?? 0),
  )
  const totalSpend = (usage?.totalCost ?? 0) + Math.max(0, subagentSpend)
  void activeSessionId

  const [logModalOpen, setLogModalOpen] = useState(false)
  const [dragging, setDragging] = useState(false)
  const [diagnosticsOpen, setDiagnosticsOpen] = useState(false)
  const mediaEvents = systemEvents.filter((event) => event.subtype === 'media_pruning')
  const compactionEvents = systemEvents.filter((event) => event.subtype === 'compaction_complete')
  const omittedImages = mediaEvents.reduce((acc, event) => acc + detailNumber(event, 'omitted_images'), 0)
  const latestCompaction = compactionEvents[compactionEvents.length - 1]
  const taskItems = useMemo(() => collectActivityTasks(systemEvents), [systemEvents])
  const warningLogs = logs.filter((log) => log.level === 'error' || log.level === 'warn')
  const attentionEvents = systemEvents.filter((event) =>
    event.subtype.includes('failed') ||
    event.subtype.includes('skipped') ||
    event.subtype.includes('error'),
  )
  const diagnosticAttention = warningLogs.length + attentionEvents.length

  // Mouse-driven resize: the drag handle is a thin column on the
  // LEFT edge of the panel. We translate pageX into a new width by
  // anchoring on window.innerWidth — pageX increasing = panel
  // shrinking. The store clamps to a sane range so we don't need to
  // worry about edge cases here.
  const onResizeMouseDown = useCallback(
    (e: React.MouseEvent) => {
      e.preventDefault()
      setDragging(true)
      const onMove = (ev: MouseEvent) => {
        const nextWidth = window.innerWidth - ev.pageX
        setPanelWidth(nextWidth)
      }
      const onUp = () => {
        setDragging(false)
        window.removeEventListener('mousemove', onMove)
        window.removeEventListener('mouseup', onUp)
        // Restore default cursor + text-selection after release.
        document.body.style.cursor = ''
        document.body.style.userSelect = ''
      }
      window.addEventListener('mousemove', onMove)
      window.addEventListener('mouseup', onUp)
      document.body.style.cursor = 'col-resize'
      document.body.style.userSelect = 'none'
    },
    [setPanelWidth],
  )

  // Keep the cursor styling in sync if the component unmounts mid-drag
  // (e.g. the user hits the focus-mode shortcut while dragging).
  useEffect(() => {
    return () => {
      document.body.style.cursor = ''
      document.body.style.userSelect = ''
    }
  }, [])

  useEffect(() => {
    if (diagnosticAttention > 0) setDiagnosticsOpen(true)
  }, [diagnosticAttention])

  const contextKnown = usage.currentContextTokens > 0 || usage.totalInputTokens <= usage.contextWindow
  const contextTokens = usage.currentContextTokens > 0
    ? usage.currentContextTokens
    : contextKnown
      ? usage.totalInputTokens
      : 0
  const ctxPct = Math.min(100, Math.round((contextTokens / usage.contextWindow) * 100))

  return (
    <aside
      className="glass glass-panel relative isolate flex shrink-0 flex-col overflow-hidden rounded-[18px]"
      style={{ width: `${panelWidth}px` }}
    >
      {/* Ambient topographic backdrop — same vocabulary as Sidebar
          (paired-peak height field, logo polar-noise), different seed
          so the two panels don't mirror each other. `-z-10` + parent
          `isolate` keep it behind every sibling without z-indexing
          them individually. */}
      <TopoBackdrop
        seed={13}
        className="pointer-events-none absolute inset-0 -z-10"
      />
      {/* Drag handle — a 6px invisible strip hugging the panel's
          left edge, with a 1px hairline to hint at the affordance.
          Widens slightly (and brightens the hairline) on hover so
          the user can find it without having to aim pixel-perfect. */}
      <div
        role="separator"
        aria-orientation="vertical"
        aria-label="Resize activity panel"
        title="Drag to resize (⌘\\ toggles focus mode)"
        onMouseDown={onResizeMouseDown}
        onDoubleClick={() => setPanelWidth(320)}
        className={`group absolute left-0 top-0 z-10 h-full w-[6px] -translate-x-[3px] cursor-col-resize select-none ${
          dragging ? 'bg-accent/20' : ''
        }`}
      >
        <div
          className={`absolute left-1/2 top-0 h-full w-px -translate-x-1/2 transition-colors ${
            dragging
              ? 'bg-accent/60'
              : 'bg-transparent group-hover:bg-accent/40'
          }`}
        />
      </div>
      <div className="flex items-center justify-between px-4 py-3 hairline-b">
        <div className="label">activity</div>
        <button
          onClick={() => toggleActivityPanel(true)}
          title="Collapse activity panel (⌘])"
          className="rounded bg-white/[0.04] px-1.5 py-[2px] font-mono text-[11px] text-fg-2 ring-hairline hover:bg-white/[0.08] hover:text-fg-0"
        >
          ›
        </button>
      </div>

      <div className="flex-1 overflow-y-auto">
        {/* ── Computer live view (only when a session is active) ── */}
        <ComputerLiveView />
        {/* ── Context meter ─────────────────────────────────── */}
        <div className="px-4 py-3 hairline-b">
          <div className="mb-2 flex items-baseline justify-between">
            <div className="label">request context</div>
            <div className="font-mono text-[11px] text-fg-0">
              {contextKnown ? formatTokens(contextTokens) : 'n/a'}
              <span className="text-fg-2">/{formatTokens(usage.contextWindow)}</span>
            </div>
          </div>
          <div className="relative h-1.5 w-full overflow-hidden rounded-full bg-white/[0.06]">
            <div
              className="absolute left-0 top-0 h-full bg-gradient-to-r from-accent/70 to-accent"
              style={{ width: `${ctxPct}%` }}
            />
          </div>
          <div className="mt-2 grid grid-cols-3 gap-2 text-[10.5px]">
            <Metric label="request" value={contextKnown ? formatTokens(contextTokens) : 'n/a'} />
            <Metric label="output" value={formatTokens(usage.totalOutputTokens)} />
            <Metric label="cache" value={formatTokens(usage.totalCacheReadTokens)} />
          </div>
          <div className="mt-2 grid grid-cols-1 gap-2 text-[10.5px]">
            <Metric label="billed input" value={formatTokens(usage.totalInputTokens)} />
          </div>
          <div className="mt-2 flex items-center justify-between border-t border-white/5 pt-2 text-[10.5px]">
            <span className="text-fg-2">session spend</span>
            <span className="font-mono text-fg-0">{formatCost(totalSpend)}</span>
          </div>
          {subagentSpend > 0.0001 && (
            <div className="mt-1 flex items-center justify-between text-[9.5px] text-fg-3">
              <span>
                self {formatCost(usage.totalCost)}
                <span className="text-fg-3/50"> · </span>
                subagents {formatCost(subagentSpend)}
              </span>
            </div>
          )}
          <div className="mt-2 grid grid-cols-2 gap-2 border-t border-white/5 pt-2 text-[10.5px]">
            <Metric label="img trims" value={String(omittedImages)} />
            <Metric label="summaries" value={String(compactionEvents.length)} />
          </div>
          {latestCompaction && (
            <div className="mt-2 rounded bg-ok/[0.06] px-2 py-1.5 ring-1 ring-ok/15">
              <div className="flex items-center justify-between font-mono text-[9px] uppercase tracking-[0.08em] text-ok">
                <span>last summary</span>
                <span>{relativeTime(latestCompaction.at)}</span>
              </div>
              <div className="mt-1 font-mono text-[10.5px] text-fg-1">
                {formatTokens(
                  detailNumber(latestCompaction, 'context_tokens_before') ||
                    detailNumber(latestCompaction, 'tokens_before'),
                )}
                <span className="text-fg-3"> → </span>
                {formatTokens(
                  detailNumber(latestCompaction, 'context_tokens_after') ||
                    detailNumber(latestCompaction, 'tokens_after'),
                )}
              </div>
            </div>
          )}
        </div>

        <TaskProgressSection tasks={taskItems} />

        {/* ── Tool calls (Gantt timeline) ─────────────────── */}
        <ToolTimeline />

        {/* ── File changes ───────────────────────────────── */}
        <ChangesSection />

        {/* ── Artifacts ─────────────────────────────────── */}
        <ArtifactsSection />

        <div className="px-4 py-3">
          <div className="mb-2 flex items-center justify-between gap-2">
            <button
              onClick={() => setDiagnosticsOpen((open) => !open)}
              className={`label flex items-center gap-2 hover:text-fg-1 ${
                diagnosticAttention > 0 ? 'text-warn' : 'text-fg-2'
              }`}
            >
              <span>{diagnosticsOpen ? '▾' : '▸'}</span>
              diagnostics
              {diagnosticAttention > 0 && (
                <span className="rounded bg-warn/10 px-1.5 py-[1px] font-mono text-[8.5px] text-warn ring-1 ring-warn/20">
                  {diagnosticAttention}
                </span>
              )}
            </button>
            <button
              onClick={() => setLogModalOpen(true)}
              title="Pop out for full details"
              className="rounded bg-white/[0.04] px-1.5 py-[2px] font-mono text-[9px] uppercase tracking-[0.08em] text-fg-2 ring-hairline hover:bg-white/[0.08] hover:text-fg-0"
            >
              expand
            </button>
          </div>
          {!diagnosticsOpen && (
            <div className="rounded bg-white/[0.02] px-2 py-1.5 font-mono text-[10px] text-fg-3 ring-hairline">
              {systemEvents.length} events · {logs.length} logs
            </div>
          )}
          {diagnosticsOpen && (
            <div className="space-y-2">
              <div className="space-y-1">
                {systemEvents.length === 0 && (
                  <div className="rounded bg-white/[0.02] px-2 py-1.5 text-[11px] italic text-fg-3 ring-hairline">
                    No system events
                  </div>
                )}
                {systemEvents.slice(-6).reverse().map((e) => (
                  <div key={e.id} className="rounded bg-white/[0.025] px-2 py-1.5 ring-hairline">
                    <div className="flex items-center gap-2 text-[10px]">
                      <span className="font-mono uppercase text-warn/80">{e.subtype}</span>
                      <span className="ml-auto text-fg-3">{relativeTime(e.at)}</span>
                    </div>
                    <div className="mt-0.5 text-[11px] leading-[1.4] text-fg-1">{e.message}</div>
                  </div>
                ))}
              </div>
              <div className="max-h-[120px] overflow-y-auto rounded bg-black/45 p-2 font-mono text-[10px] text-fg-2 ring-hairline">
                {logs.length === 0 && <div className="italic">— empty —</div>}
                {logs.slice(-40).map((l, i) => (
                  <div key={i} className="truncate">
                    <span
                      className={
                        l.level === 'error'
                          ? 'text-danger'
                          : l.level === 'warn'
                            ? 'text-warn'
                            : l.level === 'info'
                              ? 'text-ok'
                              : 'text-fg-2'
                      }
                    >
                      {l.level}
                    </span>{' '}
                    {l.message}
                  </div>
                ))}
              </div>
            </div>
          )}
        </div>
      </div>
      {logModalOpen && <LogStreamModal onClose={() => setLogModalOpen(false)} />}
    </aside>
  )
}

interface ActivityTaskItem {
  id: string
  title: string
  status: string
  progress: number
  assignee?: string
  updatedAt: number
}

function TaskProgressSection({ tasks }: { tasks: ActivityTaskItem[] }) {
  if (tasks.length === 0) return null
  const done = tasks.filter((task) => task.status === 'done').length
  const blocked = tasks.filter((task) => task.status === 'blocked').length
  const pct = Math.round(tasks.reduce((acc, task) => acc + task.progress, 0) / tasks.length)
  const visible = [...tasks].sort(activityTaskSort).slice(0, 9)
  const hidden = Math.max(0, tasks.length - visible.length)

  return (
    <div className="px-4 py-3 hairline-b">
      <div className="mb-2 flex items-start justify-between gap-3">
        <div>
          <div className="label">progress</div>
          <div className="mt-1 font-mono text-[10px] text-fg-3">
            {done}/{tasks.length} complete{blocked > 0 ? ` · ${blocked} blocked` : ''}
          </div>
        </div>
        <div className="min-w-[64px] rounded-lg bg-white/[0.035] px-2 py-1.5 text-right ring-hairline">
          <div className="font-mono text-[15px] leading-none text-fg-0">{pct}%</div>
          <div className="mt-1 h-1 overflow-hidden rounded-full bg-white/10">
            <div className="h-full rounded-full bg-accent/75" style={{ width: `${pct}%` }} />
          </div>
        </div>
      </div>
      <div className="space-y-1.5">
        {visible.map((task) => {
          const complete = task.status === 'done'
          const cancelled = task.status === 'cancelled'
          return (
            <div
              key={task.id}
              className={`group rounded-lg px-2.5 py-2 ring-hairline ${
                task.status === 'blocked'
                  ? 'bg-warn/[0.045]'
                  : task.status === 'active'
                    ? 'bg-accent/[0.05]'
                    : complete
                      ? 'bg-ok/[0.035]'
                      : 'bg-white/[0.025]'
              }`}
            >
              <div className="flex items-start gap-2.5">
                <span className={`mt-0.5 flex h-4 w-4 shrink-0 items-center justify-center rounded-full ring-1 ${activityTaskDotClass(task.status)}`}>
                  {complete ? '✓' : task.status === 'blocked' ? '!' : ''}
                </span>
                <div className="min-w-0 flex-1">
                  <div className="flex items-center gap-2">
                    <span className={`truncate text-[11.5px] leading-[1.35] text-fg-1 ${complete || cancelled ? 'line-through decoration-fg-3/70' : ''}`}>
                      {task.title}
                    </span>
                    <span className={`shrink-0 font-mono text-[8.5px] uppercase ${activityTaskStatusClass(task.status)}`}>
                      {activityTaskLabel(task.status)}
                    </span>
                  </div>
                  <div className="mt-1 flex items-center justify-between gap-2">
                    <span className="truncate font-mono text-[9px] text-fg-3">
                      {task.assignee || task.id}
                    </span>
                    <span className="shrink-0 font-mono text-[9px] text-fg-3">{relativeTime(task.updatedAt)}</span>
                  </div>
                  {!complete && !cancelled && (
                    <div className="mt-1.5 h-1 overflow-hidden rounded-full bg-white/[0.08]">
                      <div className="h-full rounded-full bg-accent/65" style={{ width: `${task.progress}%` }} />
                    </div>
                  )}
                </div>
              </div>
            </div>
          )
        })}
        {hidden > 0 && (
          <div className="rounded bg-white/[0.02] px-2 py-1.5 font-mono text-[9.5px] text-fg-3 ring-hairline">
            +{hidden} more task{hidden === 1 ? '' : 's'}
          </div>
        )}
      </div>
    </div>
  )
}

function collectActivityTasks(events: SystemEventRecord[]): ActivityTaskItem[] {
  const tasks = new Map<string, ActivityTaskItem>()
  const upsert = (raw: Record<string, unknown>, fallbackAt: number, subtype?: string) => {
    const id = typeof raw.id === 'string' ? raw.id : ''
    if (!id) return
    const existing = tasks.get(id)
    const status = typeof raw.status === 'string'
      ? raw.status
      : activityTaskStatusFromEvent(subtype) ?? existing?.status ?? 'todo'
    const progress = typeof raw.progress === 'number' && Number.isFinite(raw.progress)
      ? raw.progress
      : activityTaskProgressFromEvent(subtype) ?? existing?.progress ?? activityStatusProgress(status)
    const updatedAt = typeof raw.updatedAt === 'number' && Number.isFinite(raw.updatedAt)
      ? raw.updatedAt
      : fallbackAt
    tasks.set(id, {
      id,
      title: typeof raw.title === 'string' ? raw.title : existing?.title ?? id,
      status,
      progress: Math.max(0, Math.min(100, progress)),
      assignee: typeof raw.assignee === 'string' ? raw.assignee : existing?.assignee,
      updatedAt,
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

  return [...tasks.values()]
}

function activityTaskSort(a: ActivityTaskItem, b: ActivityTaskItem): number {
  const order: Record<string, number> = {
    active: 0,
    blocked: 1,
    todo: 2,
    done: 3,
    cancelled: 4,
  }
  return (order[a.status] ?? 9) - (order[b.status] ?? 9) || b.updatedAt - a.updatedAt
}

function activityStatusProgress(status: string): number {
  if (status === 'done') return 100
  if (status === 'active') return 55
  if (status === 'blocked') return 35
  if (status === 'cancelled') return 0
  return 8
}

function activityTaskStatusFromEvent(subtype?: string): string | undefined {
  if (!subtype) return undefined
  if (subtype === 'task_complete') return 'done'
  if (subtype === 'task_block') return 'blocked'
  if (subtype === 'task_cancel') return 'cancelled'
  if (subtype === 'task_claim' || subtype === 'task_heartbeat') return 'active'
  if (subtype === 'task_create') return 'todo'
  return undefined
}

function activityTaskProgressFromEvent(subtype?: string): number | undefined {
  if (subtype === 'task_complete') return 100
  if (subtype === 'task_cancel') return 0
  return undefined
}

function activityTaskLabel(status: string): string {
  if (status === 'active') return 'working'
  if (status === 'todo') return 'queued'
  return status
}

function activityTaskStatusClass(status: string): string {
  if (status === 'done') return 'text-ok'
  if (status === 'active') return 'text-accent'
  if (status === 'blocked') return 'text-warn'
  if (status === 'cancelled') return 'text-danger'
  return 'text-fg-3'
}

function activityTaskDotClass(status: string): string {
  if (status === 'done') return 'bg-accent text-bg-0 ring-accent/70'
  if (status === 'active') return 'bg-accent/15 text-accent ring-accent/45'
  if (status === 'blocked') return 'bg-warn/12 text-warn ring-warn/45'
  if (status === 'cancelled') return 'bg-danger/10 text-danger ring-danger/35'
  return 'bg-white/[0.035] text-fg-3 ring-white/15'
}

function Metric({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded bg-white/[0.025] px-1.5 py-1 text-center ring-hairline">
      <div className="text-[9px] uppercase tracking-[0.1em] text-fg-3">{label}</div>
      <div className="mt-0.5 font-mono text-[11px] text-fg-0">{value}</div>
    </div>
  )
}

function detailNumber(
  event: { details?: Record<string, unknown> } | undefined,
  key: string,
): number {
  const value = event?.details?.[key]
  if (typeof value === 'number' && Number.isFinite(value)) return value
  if (typeof value === 'string') {
    const parsed = Number(value)
    if (Number.isFinite(parsed)) return parsed
  }
  return 0
}
