import { useEffect, useMemo, useRef } from 'react'
import { useHarness, type SessionSlice } from '../state/store'
import { Spinner } from '../lib/spinner'
import type { CoordinationStrategy, Message } from '@shared/events'

/** macOS Cmd-Tab style overlay for jumping between sessions.
 *
 *  Opened by Ctrl+Tab in `App.tsx`. Selection cycles on each subsequent
 *  Tab (Shift+Tab to reverse), Esc cancels, and releasing Ctrl commits
 *  the highlighted session. App owns the open/selection state so the
 *  keydown handler can mutate it directly without round-tripping
 *  through the store.
 *
 *  Candidate ordering is frozen at open time (`candidateIds` prop) —
 *  the running set can change mid-cycle while the agent streams, and a
 *  shifting list under the user's cursor would be disorienting. */

const MAX_ROWS = 10
const PREVIEW_CHARS = 110

interface QuickSwitcherProps {
  candidateIds: string[]
  selectedIndex: number
}

export function QuickSwitcher({
  candidateIds,
  selectedIndex,
}: QuickSwitcherProps) {
  const sessions = useHarness((s) => s.sessions)
  const sessionArchive = useHarness((s) => s.sessionArchive)
  const activeSessionId = useHarness((s) => s.activeSessionId)
  const activeMessages = useHarness((s) => s.messages)
  const activeIsStreaming = useHarness((s) => s.isStreaming)

  const rows = useMemo(() => {
    return candidateIds
      .slice(0, MAX_ROWS)
      .map((id) => {
        const snapshot = sessions.find((sn) => sn.id === id)
        if (!snapshot) return null
        // Active session never appears in the switcher, but if it did
        // we'd want its live slice — fall through to archive read which
        // returns undefined for the active id (it lives at the top
        // level, not in the archive).
        const isActive = id === activeSessionId
        const slice: SessionSlice | undefined = isActive
          ? undefined
          : sessionArchive[id]
        const isStreaming = isActive ? activeIsStreaming : !!slice?.isStreaming
        const messages = isActive ? activeMessages : slice?.messages ?? []
        return {
          id,
          snapshot,
          isStreaming,
          preview: extractPreview(messages),
        }
      })
      .filter(Boolean) as Array<{
        id: string
        snapshot: NonNullable<ReturnType<typeof sessions.find>>
        isStreaming: boolean
        preview: string
      }>
  }, [
    candidateIds,
    sessions,
    sessionArchive,
    activeSessionId,
    activeMessages,
    activeIsStreaming,
  ])

  const scrollRef = useRef<HTMLDivElement>(null)
  // Keep the highlighted row in view as Tab walks past the visible
  // window. The overlay caps at MAX_ROWS so this only matters for
  // very dense MRU lists, but it's cheap insurance.
  useEffect(() => {
    const el = scrollRef.current
    if (!el) return
    const target = el.querySelector<HTMLElement>(
      `[data-row-index="${selectedIndex}"]`,
    )
    if (target) target.scrollIntoView({ block: 'nearest' })
  }, [selectedIndex])

  if (rows.length === 0) return null

  return (
    <div className="fixed inset-0 z-[60] flex items-start justify-center bg-black/30 pt-[18vh] backdrop-blur-[2px]">
      <div
        className="w-[520px] overflow-hidden rounded-xl border border-white/[0.08] bg-bg-1/95 shadow-2xl ring-1 ring-black/40 backdrop-blur-xl"
        // Don't bubble pointer events to the underlying app — the user
        // is in keyboard-only mode while holding Ctrl, but stray
        // clicks shouldn't pass through.
        onMouseDown={(e) => e.preventDefault()}
      >
        <div className="flex items-center justify-between border-b border-white/[0.05] px-3.5 py-2 font-mono text-[10.5px] uppercase tracking-[0.14em] text-fg-3">
          <span>switch session</span>
          <span className="text-fg-4">
            tab cycle · shift+tab back · esc cancel · release ctrl select
          </span>
        </div>
        <div ref={scrollRef} className="max-h-[60vh] overflow-y-auto py-1">
          {rows.map((row, idx) => (
            <SwitcherRow
              key={row.id}
              index={idx}
              selected={idx === selectedIndex}
              title={row.snapshot.title || 'Untitled'}
              strategy={row.snapshot.coordinationStrategy ?? 'bus'}
              model={row.snapshot.model}
              isStreaming={row.isStreaming}
              preview={row.preview}
              updatedAt={row.snapshot.updatedAt}
            />
          ))}
        </div>
      </div>
    </div>
  )
}

function SwitcherRow({
  index,
  selected,
  title,
  strategy,
  model,
  isStreaming,
  preview,
  updatedAt,
}: {
  index: number
  selected: boolean
  title: string
  strategy: CoordinationStrategy
  model: string
  isStreaming: boolean
  preview: string
  updatedAt: number
}) {
  const meta = SWITCHER_STRATEGY_META[strategy]
  return (
    <div
      data-row-index={index}
      className={`mx-1 flex items-start gap-3 rounded-md px-2.5 py-2 ${
        selected
          ? 'bg-accent/[0.12] ring-1 ring-accent/40'
          : 'hover:bg-white/[0.03]'
      }`}
    >
      <span className="mt-[3px] inline-flex h-3 w-3 shrink-0 items-center justify-center">
        {isStreaming ? (
          <Spinner name="braille" className="text-accent" />
        ) : (
          <span
            className={`inline-block h-1.5 w-1.5 rounded-full ${
              selected ? 'bg-accent' : 'bg-white/[0.20]'
            }`}
          />
        )}
      </span>
      <div className="flex min-w-0 flex-1 flex-col gap-1">
        <div className="flex min-w-0 items-center gap-2">
          <span className="truncate text-[12.5px] text-fg-0">{title}</span>
          {meta ? (
            <span
              className={`shrink-0 rounded px-1 py-px font-mono text-[9px] uppercase tracking-[0.10em] ${meta.cls}`}
              title={meta.tooltip}
            >
              {meta.label}
            </span>
          ) : null}
          <span className="shrink-0 font-mono text-[10px] text-fg-4">
            {model}
          </span>
          <span className="ml-auto shrink-0 font-mono text-[10px] tabular-nums text-fg-4">
            {formatRecency(updatedAt)}
          </span>
        </div>
        {preview ? (
          <div className="truncate font-mono text-[11px] text-fg-3">
            {preview}
          </div>
        ) : null}
      </div>
    </div>
  )
}

function extractPreview(messages: Message[]): string {
  // Walk newest-first looking for the last meaningful chunk. Skip empty
  // assistant scaffolds (model just opened a turn, no parts yet).
  for (let i = messages.length - 1; i >= 0; i--) {
    const m = messages[i]
    if (m.role !== 'assistant' && m.role !== 'user') continue
    const text = m.parts
      .filter((p) => p.type === 'text')
      .map((p) => p.text ?? '')
      .join(' ')
      .replace(/\s+/g, ' ')
      .trim()
    if (!text) continue
    const prefix = m.role === 'user' ? '› ' : ''
    return prefix + (text.length > PREVIEW_CHARS
      ? text.slice(0, PREVIEW_CHARS - 1) + '…'
      : text)
  }
  return ''
}

function formatRecency(ts: number): string {
  if (!ts) return ''
  const delta = Date.now() - ts
  if (delta < 60_000) return 'now'
  const m = Math.floor(delta / 60_000)
  if (m < 60) return `${m}m`
  const h = Math.floor(m / 60)
  if (h < 24) return `${h}h`
  const d = Math.floor(h / 24)
  return `${d}d`
}

const SWITCHER_STRATEGY_META: Record<
  CoordinationStrategy,
  { label: string; cls: string; tooltip: string }
> = {
  bus: {
    label: 'bus',
    cls: 'text-accent/90 bg-accent/[0.07] ring-1 ring-accent/[0.18]',
    tooltip: 'Message-bus',
  },
  isolated: {
    label: 'task',
    cls: 'text-fg-1 bg-white/[0.05] ring-1 ring-white/[0.10]',
    tooltip: 'Task-first solo',
  },
  kanban: {
    label: 'board',
    cls: 'text-warn bg-warn/[0.08] ring-1 ring-warn/[0.20]',
    tooltip: 'Kanban board',
  },
  goal: {
    label: 'goal',
    cls: 'text-ok bg-ok/[0.08] ring-1 ring-ok/[0.20]',
    tooltip: 'Goal loop',
  },
}
