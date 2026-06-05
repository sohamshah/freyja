import { useState } from 'react'
import { relativeTime } from '../../lib/format'
import { StickyHeader } from '../StickyHeader'

/**
 * Grounded Memory — shared design primitives.
 *
 * One coherent "memory language" assembled from the app's shipped idioms
 * (StickyHeader, .label, .glass-raised/.glass-chip, the ArtifactsSection
 * TYPE_META map + +N/−M diff badge, the ActionLedgerSection OP glyphs,
 * .kanban-card-mention id chips, the council-tile pulse). Every memory
 * surface — the working-memory Ledger-Cards panel, the recall drawer, the
 * inline correction/receipt cards, the diff-aware artifact — imports these
 * so the whole feature reads as one crafted, restrained system.
 *
 * Accent (#a8d4fc) is the ONLY saturated color and is reserved for the
 * live/active/now. The status trio is muted toward grey: ok/sage, warn/tan,
 * danger/dusty-rose. Type is Geist Mono 8.5–11px with tabular-nums for every
 * numeral; Fraunces serif (font-serif) appears ONLY in SectionSlug titles.
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
   LeafMark — the typed-child glyph that rides in a ~16px left gutter so
   every row in a ledger card hangs off the same vertical seam.
   ────────────────────────────────────────────────────────────────────── */

export type LeafKind = 'decision' | 'finding' | 'open_thread' | 'artifact_note'

/** File-ext glyph map, cloned from ArtifactsSection's TYPE_META so the
 *  artifact_note leaf reads identically to the Artifacts list. */
const TYPE_META: Record<string, { icon: string; color: string }> = {
  md: { icon: '◆', color: 'text-fg-1' },
  markdown: { icon: '◆', color: 'text-fg-1' },
  ts: { icon: 'τ', color: 'text-accent' },
  tsx: { icon: 'τ', color: 'text-accent' },
  js: { icon: 'ƒ', color: 'text-warn' },
  jsx: { icon: 'ƒ', color: 'text-warn' },
  py: { icon: 'λ', color: 'text-ok' },
  json: { icon: '{}', color: 'text-warn' },
  css: { icon: '#', color: 'text-ok' },
  html: { icon: '◇', color: 'text-accent' },
  svg: { icon: '◎', color: 'text-accent' },
  sh: { icon: '$', color: 'text-ok' },
  yaml: { icon: '⊞', color: 'text-warn' },
  yml: { icon: '⊞', color: 'text-warn' },
  toml: { icon: '⊞', color: 'text-warn' },
  txt: { icon: '≡', color: 'text-fg-2' },
}

/** Resolve a path/filename to its TYPE_META glyph. Exported so artifact
 *  rows (GalleyArtifact, the WM panel) share one ext→glyph source. */
export function extMeta(pathOrExt?: string): { icon: string; color: string } {
  if (!pathOrExt) return { icon: '·', color: 'text-fg-3' }
  const tail = pathOrExt.split('/').pop() ?? pathOrExt
  const ext = tail.includes('.') ? tail.split('.').pop()! : tail
  return TYPE_META[ext.toLowerCase()] ?? { icon: '·', color: 'text-fg-3' }
}

export function LeafMark({
  kind,
  path,
  resolved = false,
}: {
  kind: LeafKind
  /** For artifact_note: the file path/ext used to pick the glyph. */
  path?: string
  /** For open_thread: a resolved thread is struck out and dimmed. */
  resolved?: boolean
}) {
  let glyph = '·'
  let color = 'text-fg-3'
  let extra = ''
  switch (kind) {
    case 'decision':
      glyph = '◆'
      color = 'text-fg-1'
      break
    case 'finding':
      glyph = '›'
      color = 'text-fg-2'
      break
    case 'open_thread':
      glyph = '¶'
      color = resolved ? 'text-fg-3' : 'text-warn'
      extra = resolved ? 'line-through' : ''
      break
    case 'artifact_note': {
      const m = extMeta(path)
      glyph = m.icon
      color = 'text-fg-2'
      break
    }
  }
  return (
    <span
      aria-hidden="true"
      className={`inline-flex w-4 shrink-0 select-none justify-center font-mono text-[10.5px] leading-[1.5] ${color} ${extra}`}
    >
      {glyph}
    </span>
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
   DatelineTS — right-aligned tabular relative timestamp. The quiet dateline
   in the morgue spine and on every card head. Reuses the shared
   relativeTime() helper so wording matches the rest of the app.
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
  title: string
  kicker?: string
  count?: number
  expanded?: boolean
  onToggle?: () => void
  children?: React.ReactNode
  topOffset?: number
}) {
  const toggleable = typeof onToggle === 'function'
  return (
    <StickyHeader topOffset={topOffset}>
      <div className="memory-dash-rule px-4 py-2">
        <div className="flex w-full items-baseline justify-between gap-2">
          <button
            type="button"
            onClick={onToggle}
            disabled={!toggleable}
            className={`flex min-w-0 items-baseline gap-2 text-left ${
              toggleable ? '' : 'cursor-default'
            }`}
          >
            <span className="truncate font-serif text-[14px] font-light italic leading-none text-fg-0">
              {title}
            </span>
            {kicker && <span className="label shrink-0">{kicker}</span>}
            {count != null && (
              <span className="shrink-0 font-mono text-[10px] tabular-nums text-fg-3">
                {count}
              </span>
            )}
            {toggleable && (
              <span className="shrink-0 text-[9px] text-fg-3">{expanded ? '▾' : '▸'}</span>
            )}
          </button>
          {children && <div className="flex shrink-0 items-center gap-1.5">{children}</div>}
        </div>
      </div>
    </StickyHeader>
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
  const meta = extMeta(path)
  const filename = path.split('/').pop() ?? path
  return (
    <div className="flex flex-col gap-1 rounded-md bg-white/[0.02] px-2 py-1.5 ring-hairline">
      <div className="flex items-baseline gap-2">
        <span className={`shrink-0 font-mono text-[10px] font-bold leading-[1.5] ${meta.color}`}>
          {meta.icon}
        </span>
        <button
          type="button"
          onClick={onOpen}
          disabled={!onOpen}
          title={onOpen ? `Open ${path}` : path}
          className={`group min-w-0 flex-1 truncate text-left font-mono text-[10.5px] text-fg-0 ${
            onOpen ? 'hover:text-accent' : 'cursor-default'
          }`}
        >
          {filename}
          {onOpen && (
            <span className="ml-1 text-[9px] text-fg-3 opacity-0 group-hover:opacity-100">↗</span>
          )}
        </button>
      </div>
      {note && (
        <div className="flex items-baseline gap-1.5 pl-6">
          <span className="shrink-0 font-mono text-[9px] uppercase tracking-[0.08em] text-fg-3">
            ✎ intent:
          </span>
          <span className="min-w-0 flex-1 font-mono text-[10px] leading-[1.5] text-fg-1">
            {note}
          </span>
        </div>
      )}
      <div className="pl-6">
        <MarginMark
          additions={additions}
          deletions={deletions}
          diff={diff}
          diffTruncated={diffTruncated}
          className="items-start"
        />
      </div>
    </div>
  )
}
