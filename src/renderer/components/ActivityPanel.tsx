import { useCallback, useEffect, useState } from 'react'
import { useHarness } from '../state/store'
import { formatTokens, formatCost, relativeTime } from '../lib/format'
import { ComputerLiveView } from './ComputerLiveView'
import { LogStreamModal } from './LogStreamModal'
import { ArtifactsSection } from './ArtifactsSection'
import { ToolTimeline } from './ToolTimeline'
import { ChangesSection } from './ChangesSection'

export function ActivityPanel() {
  const systemEvents = useHarness((s) => s.systemEvents)
  const usage = useHarness((s) => s.usage)
  const logs = useHarness((s) => s.logs)
  const toggleActivityPanel = useHarness((s) => s.toggleActivityPanel)
  const panelWidth = useHarness((s) => s.activityPanelWidth)
  const setPanelWidth = useHarness((s) => s.setActivityPanelWidth)

  const [logModalOpen, setLogModalOpen] = useState(false)
  const [dragging, setDragging] = useState(false)

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

  const ctxPct = Math.min(100, Math.round((usage.totalInputTokens / usage.contextWindow) * 100))

  return (
    <aside
      className="glass relative flex shrink-0 flex-col rounded-[18px]"
      style={{ width: `${panelWidth}px` }}
    >
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
            <div className="label">
              context
            </div>
            <div className="font-mono text-[11px] text-fg-0">
              {formatTokens(usage.totalInputTokens)}
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
            <Metric label="in" value={formatTokens(usage.totalInputTokens)} />
            <Metric label="out" value={formatTokens(usage.totalOutputTokens)} />
            <Metric label="cached" value={formatTokens(usage.totalCacheReadTokens)} />
          </div>
          <div className="mt-2 flex items-center justify-between border-t border-white/5 pt-2 text-[10.5px]">
            <span className="text-fg-2">session spend</span>
            <span className="font-mono text-fg-0">{formatCost(usage.totalCost)}</span>
          </div>
        </div>

        {/* ── Tool calls (Gantt timeline) ─────────────────── */}
        <ToolTimeline />

        {/* ── File changes ───────────────────────────────── */}
        <ChangesSection />

        {/* ── Artifacts ─────────────────────────────────── */}
        <ArtifactsSection />

        {/* ── System events ───────────────────────────────── */}
        <div className="px-4 py-3 hairline-b">
          <div className="mb-2 flex items-baseline justify-between">
            <div className="label">
              system events
            </div>
            <div className="font-mono text-[10px] text-fg-3">{systemEvents.length}</div>
          </div>
          {systemEvents.length === 0 && (
            <div className="py-2 text-[11px] italic text-fg-3">Quiet</div>
          )}
          <div className="space-y-1">
            {systemEvents.slice(-8).reverse().map((e) => (
              <div key={e.id} className="rounded bg-white/[0.025] px-2 py-1.5 ring-hairline">
                <div className="flex items-center gap-2 text-[10px]">
                  <span className="font-mono uppercase text-warn/80">{e.subtype}</span>
                  <span className="ml-auto text-fg-3">{relativeTime(e.at)}</span>
                </div>
                <div className="mt-0.5 text-[11px] leading-[1.4] text-fg-1">{e.message}</div>
              </div>
            ))}
          </div>
        </div>

        {/* ── Log stream ───────────────────────────────────── */}
        <div className="px-4 py-3">
          <div className="mb-2 flex items-baseline justify-between">
            <div className="label">log stream</div>
            <button
              onClick={() => setLogModalOpen(true)}
              title="Pop out for full details (errors, stderr, etc.)"
              className="rounded bg-white/[0.04] px-1.5 py-[2px] font-mono text-[9px] uppercase tracking-[0.08em] text-fg-2 ring-hairline hover:bg-white/[0.08] hover:text-fg-0"
            >
              expand ↗
            </button>
          </div>
          <div className="max-h-[160px] overflow-y-auto rounded bg-black/45 p-2 font-mono text-[10px] text-fg-2 ring-hairline">
            {logs.length === 0 && <div className="italic">— empty —</div>}
            {logs.slice(-60).map((l, i) => (
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
      </div>
      {logModalOpen && <LogStreamModal onClose={() => setLogModalOpen(false)} />}
    </aside>
  )
}

function Metric({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded bg-white/[0.025] px-1.5 py-1 text-center ring-hairline">
      <div className="text-[9px] uppercase tracking-[0.1em] text-fg-3">{label}</div>
      <div className="mt-0.5 font-mono text-[11px] text-fg-0">{value}</div>
    </div>
  )
}
