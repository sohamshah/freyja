import { useState } from 'react'
import { relativeTime } from '../../lib/format'
import { StickyHeader } from '../StickyHeader'

/**
 * Grounded Memory — shared design primitives.
 *
 * One coherent "memory language" assembled from the app's shipped idioms
 * (StickyHeader, .label, .glass-raised/.glass-chip, the ArtifactsSection
 * +N/−M diff badge, .kanban-card-mention id chips, the council-tile pulse).
 * Every memory surface — the working-memory cards panel, the history drawer,
 * the inline correction/receipt cards, the diff-aware artifact — imports these
 * so the whole feature reads as one crafted, restrained system.
 *
 * Accent (#a8d4fc) is the ONLY saturated color and is reserved for the
 * live/active/now. The status trio is muted toward grey: ok/sage, warn/tan,
 * danger/dusty-rose. Type is Geist Mono everywhere (8.5–11px) with tabular-nums
 * for numerals — no serif; legibility comes from size, weight, and contrast.
 */

/* ──────────────────────────────────────────────────────────────────────
   StatusPip — the 5–6px state dot. Only the live pip glows; it never
   animates (the single moving thing is the active card's ring pulse).
   ────────────────────────────────────────────────────────────────────── */

export type PipStatus =
  | 'active'
  | 'running'
  | 'paused'
  | 'open'
  | 'done'
  | 'resolved'
  | 'blocked'
  | 'diverged'
  | 'idle'
  | 'noted'

const PIP_COLOR: Record<PipStatus, string> = {
  active: 'bg-accent',
  running: 'bg-accent',
  paused: 'bg-warn',
  open: 'bg-warn',
  done: 'bg-ok',
  resolved: 'bg-ok',
  blocked: 'bg-danger',
  diverged: 'bg-danger',
  idle: 'bg-fg-3',
  noted: 'bg-fg-3',
}

export function StatusPip({
  status,
  className = '',
}: {
  status: PipStatus | string
  className?: string
}) {
  const key = (status in PIP_COLOR ? status : 'idle') as PipStatus
  const isLive = key === 'active' || key === 'running'
  return (
    <span
      aria-hidden="true"
      className={`inline-block h-[5px] w-[5px] shrink-0 rounded-full ${PIP_COLOR[key]} ${
        isLive ? 'memory-pip--live' : ''
      } ${className}`}
    />
  )
}


/* ──────────────────────────────────────────────────────────────────────
   MarginMark — the +N/−M glass-chip badge (cloned from ArtifactsSection),
   with an optional '▸ pull proof' toggle that unfolds a collapsed unified
   diff peek. Collapsed by default; the diff is the proof, not the headline.
   ────────────────────────────────────────────────────────────────────── */

export function MarginMark({
  additions,
  deletions,
  diff,
  diffTruncated = false,
  className = '',
}: {
  additions?: number | null
  deletions?: number | null
  diff?: string | null
  diffTruncated?: boolean
  className?: string
}) {
  const [open, setOpen] = useState(false)
  const hasCounts = additions != null || deletions != null
  const hasDiff = typeof diff === 'string' && diff.trim().length > 0

  if (!hasCounts && !hasDiff) return null

  return (
    <div className={`flex flex-col items-end gap-1 ${className}`}>
      <div className="flex items-center gap-1.5">
        {hasCounts && (
          <span className="glass-chip rounded-full px-1.5 py-[1px] font-mono text-[7.5px] uppercase tabular-nums">
            <span className="text-ok">+{additions ?? 0}</span>{' '}
            <span className="text-danger">−{deletions ?? 0}</span>
          </span>
        )}
        {hasDiff && (
          <button
            onClick={() => setOpen((v) => !v)}
            className="shrink-0 font-mono text-[8.5px] uppercase tracking-[0.08em] text-fg-3 transition-colors hover:text-fg-1"
          >
            {open ? '▾ proof' : '▸ pull proof'}
          </button>
        )}
      </div>
      {open && hasDiff && <DiffPeek diff={diff!} truncated={diffTruncated} />}
    </div>
  )
}

/** Collapsed unified-diff peek: +lines on a faint ok wash, −lines on a faint
 *  danger wash, @@ hunk headers in fg-3. Deliberately monochrome-leaning —
 *  the wash carries the sign, the text stays quiet. */
function DiffPeek({ diff, truncated }: { diff: string; truncated: boolean }) {
  const lines = diff.replace(/\n$/, '').split('\n')
  return (
    <div className="memory-dash-rule mt-1 w-full overflow-hidden rounded-md bg-white/[0.02] pt-1">
      <pre className="m-0 max-h-48 overflow-auto whitespace-pre px-2 pb-1 font-mono text-[9px] leading-[1.55] tabular-nums">
        {lines.map((ln, i) => {
          const isAdd = ln.startsWith('+') && !ln.startsWith('+++')
          const isDel = ln.startsWith('-') && !ln.startsWith('---')
          const isHunk = ln.startsWith('@@')
          const isMeta =
            ln.startsWith('+++') || ln.startsWith('---') || ln.startsWith('diff ')
          const cls = isAdd
            ? 'bg-ok/[0.04] text-fg-1'
            : isDel
              ? 'bg-danger/[0.04] text-fg-2'
              : isHunk
                ? 'text-fg-3'
                : isMeta
                  ? 'text-fg-3'
                  : 'text-fg-2'
          return (
            <div key={i} className={`-mx-2 px-2 ${cls}`}>
              {ln === '' ? ' ' : ln}
            </div>
          )
        })}
      </pre>
      {truncated && (
        <div className="px-2 pb-1 font-mono text-[8.5px] italic text-fg-3">…truncated</div>
      )}
    </div>
  )
}

/* ──────────────────────────────────────────────────────────────────────
   DatelineTS — right-aligned tabular relative timestamp on each card head and
   history row. Reuses the shared relativeTime() helper so wording matches the
   rest of the app.
   ────────────────────────────────────────────────────────────────────── */

export function DatelineTS({
  ts,
  className = '',
}: {
  ts: number
  className?: string
}) {
  return (
    <span
      title={new Date(ts).toLocaleString()}
      className={`shrink-0 text-right font-mono text-[10px] tabular-nums text-fg-3 ${className}`}
    >
      {relativeTime(ts)}
    </span>
  )
}

/* ──────────────────────────────────────────────────────────────────────
   SectionSlug — the StickyHeader-wrapped section header shared by every
   memory surface. A Fraunces-300-italic title (the ONLY serif), a .label
   kicker, a tabular count, and a ▾/▸ caret; a dashed rule sits beneath.
   ────────────────────────────────────────────────────────────────────── */

export function SectionSlug({
  title,
  kicker,
  count,
  expanded = true,
  onToggle,
  children,
  topOffset = 0,
}: {
  title?: string
  kicker?: string
  count?: number
  expanded?: boolean
  onToggle?: () => void
  children?: React.ReactNode
  topOffset?: number
}) {
  const toggleable = typeof onToggle === 'function'
  // Plain mono `.label` header to match ActionLedgerSection / ArtifactsSection
  // — no serif, no editorial masthead.
  const label = kicker ?? title ?? ''
  return (
    <StickyHeader topOffset={topOffset}>
      <div className="flex w-full items-baseline justify-between gap-2 px-4 py-2">
        <button
          type="button"
          onClick={onToggle}
          disabled={!toggleable}
          className={`flex min-w-0 items-baseline gap-2 text-left ${
            toggleable ? '' : 'cursor-default'
          }`}
        >
          <span className="label shrink-0">{label}</span>
          {count != null && (
            <span className="shrink-0 font-mono text-[10px] tabular-nums text-fg-3">{count}</span>
          )}
          {toggleable && (
            <span className="shrink-0 text-[9px] text-fg-3">{expanded ? '▾' : '▸'}</span>
          )}
        </button>
        {children && <div className="flex shrink-0 items-center gap-1.5">{children}</div>}
      </div>
    </StickyHeader>
  )
}

/* ──────────────────────────────────────────────────────────────────────
   ChildLabel — a small fixed-width type label ("decided" / "found" / "open"
   / "file") in a left column so each entry says what it IS. Replaces the
   cryptic ◆ › ¶ glyph gutter; the fixed width keeps content left-aligned.
   ────────────────────────────────────────────────────────────────────── */

export function ChildLabel({
  text,
  color = 'text-fg-2',
}: {
  text: string
  color?: string
}) {
  return (
    <span
      className={`mt-[1px] inline-block w-[42px] shrink-0 select-none font-mono text-[8.5px] uppercase tracking-[0.1em] ${color}`}
    >
      {text}
    </span>
  )
}

/* ──────────────────────────────────────────────────────────────────────
   GalleyArtifact — the diff-aware artifact row (the "galley vernier").
   The agent's intent note ('✎ intent:') rides directly ABOVE the MarginMark;
   the MarginMark carries the +N/−M and the '▸ pull proof' diff peek. Never a
   diff without an intent, never an intent without a diff.
   ────────────────────────────────────────────────────────────────────── */

export function GalleyArtifact({
  path,
  note,
  additions,
  deletions,
  diff,
  diffTruncated = false,
  onOpen,
}: {
  path: string
  note?: string
  additions?: number | null
  deletions?: number | null
  diff?: string | null
  diffTruncated?: boolean
  onOpen?: () => void
}) {
  const filename = path.split('/').pop() ?? path
  return (
    <div className="flex items-baseline gap-2">
      <ChildLabel text="file" />
      <div className="min-w-0 flex-1">
        <div className="flex items-baseline justify-between gap-2">
          <button
            type="button"
            onClick={onOpen}
            disabled={!onOpen}
            title={onOpen ? `Open ${path}` : path}
            className={`group min-w-0 flex-1 truncate text-left font-mono text-[10.5px] leading-[1.5] text-fg-1 ${
              onOpen ? 'hover:text-accent' : 'cursor-default'
            }`}
          >
            {filename}
            {onOpen && (
              <span className="ml-1 text-[9px] text-fg-3 opacity-0 group-hover:opacity-100">↗</span>
            )}
          </button>
          <MarginMark additions={additions} deletions={deletions} />
        </div>
        {note && (
          <div className="mt-0.5 font-mono text-[10px] leading-[1.5] text-fg-2">{note}</div>
        )}
        {typeof diff === 'string' && diff.trim().length > 0 && (
          <MarginMark diff={diff} diffTruncated={diffTruncated} className="mt-1 items-start" />
        )}
      </div>
    </div>
  )
}
