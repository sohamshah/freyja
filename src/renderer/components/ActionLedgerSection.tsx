import { useEffect, useMemo, useState } from 'react'
import { useHarness } from '../state/store'
import { StickyHeader } from './StickyHeader'

/**
 * One row of the per-session action ledger (what the agent did). Mirrors the
 * Python `SessionLedger` row shape (bridge/session_ledger.py).
 */
type LedgerRow = {
  kind: string
  class: 'effect' | 'observation'
  operation?: string
  summary?: string
  path?: string | null
  dir?: string | null
  repo?: string | null
  additions?: number | null
  deletions?: number | null
  createdAt?: number
}

/** A file effect aggregated across every edit/write to that path — so a file
 *  the agent touched 23 times reads as one row with "×23" and the summed
 *  diff, instead of collapsing to the last edit's misleading "(1 lines)". */
type FileAgg = {
  type: 'file'
  path: string
  filename: string
  operation: string
  count: number
  additions: number
  deletions: number
  hasDiff: boolean
  createdAt: number
  dir?: string | null
  repo?: string | null
}
type OtherItem = { type: 'other'; row: LedgerRow; createdAt: number }
type DisplayItem = FileAgg | OtherItem

const OP_META: Record<string, { glyph: string; color: string }> = {
  create: { glyph: '+', color: 'text-ok' },
  edit: { glyph: '~', color: 'text-accent' },
  shell: { glyph: '$', color: 'text-warn' },
  pin: { glyph: '★', color: 'text-fg-1' },
}

function opMeta(op?: string) {
  return OP_META[op ?? ''] ?? { glyph: '·', color: 'text-fg-3' }
}

/**
 * "Actions this session" — the durable, runtime-authored ledger of what the
 * agent created/edited/ran, surfaced read-only via IPC (session:actionLedger).
 * This is the UI face of the Grounded Memory ledger.
 */
export function ActionLedgerSection() {
  const activeSessionId = useHarness((s) => s.activeSessionId)
  // Refetch when new activity lands (cheap, avoids polling).
  const activityTick = useHarness((s) => s.systemEvents.length)
  const [rows, setRows] = useState<LedgerRow[]>([])
  const [expanded, setExpanded] = useState(true)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let cancelled = false
    const run = async () => {
      const api = (window as any).harness
      if (!api?.getActionLedger || !activeSessionId) {
        setRows([])
        return
      }
      try {
        const res = await api.getActionLedger(activeSessionId)
        if (cancelled) return
        if (!res?.ok) {
          setError(res?.error ?? 'failed to load')
          setRows([])
          return
        }
        setError(null)
        setRows((res.rows ?? []) as LedgerRow[])
      } catch (err) {
        if (!cancelled) setError(String(err))
      }
    }
    void run()
    return () => {
      cancelled = true
    }
  }, [activeSessionId, activityTick])

  const { items, pinned, actionCount } = useMemo(() => {
    // Aggregate file effects by path (count edits + sum diff); keep non-file
    // effects (shell commands, images) as individual rows.
    const byPath = new Map<string, FileAgg>()
    const other: OtherItem[] = []
    const pins: LedgerRow[] = []
    let count = 0
    for (const r of rows) {
      if (r.kind === 'pinned_fact') {
        pins.push(r)
        continue
      }
      if (r.class !== 'effect') continue
      count += 1
      if (r.path) {
        const add = r.additions ?? 0
        const del = r.deletions ?? 0
        const hasDiff = r.additions != null || r.deletions != null
        const cur = byPath.get(r.path)
        if (!cur) {
          byPath.set(r.path, {
            type: 'file',
            path: r.path,
            filename: r.path.split('/').pop() ?? r.path,
            operation: r.operation ?? 'edit',
            count: 1,
            additions: add,
            deletions: del,
            hasDiff,
            createdAt: r.createdAt ?? 0,
            dir: r.dir,
            repo: r.repo,
          })
        } else {
          cur.count += 1
          cur.additions += add
          cur.deletions += del
          cur.hasDiff = cur.hasDiff || hasDiff
          // A file that was ever created keeps the "create" verb.
          if (r.operation === 'create') cur.operation = 'create'
          cur.createdAt = Math.max(cur.createdAt, r.createdAt ?? 0)
        }
      } else {
        other.push({ type: 'other', row: r, createdAt: r.createdAt ?? 0 })
      }
    }
    const merged: DisplayItem[] = [...byPath.values(), ...other].sort(
      (a, b) => b.createdAt - a.createdAt,
    )
    return { items: merged, pinned: pins, actionCount: count }
  }, [rows])

  const total = items.length

  return (
    <div className="hairline-b">
      <StickyHeader>
        <div className="flex w-full items-baseline justify-between gap-2 px-4 py-2">
          <button
            onClick={() => setExpanded((v) => !v)}
            className="flex items-baseline gap-2 text-left"
          >
            <div className="label">actions this session</div>
            <span className="font-mono text-[10px] tabular-nums text-fg-3">{actionCount}</span>
            <span className="text-[9px] text-fg-3">{expanded ? '▾' : '▸'}</span>
          </button>
        </div>
      </StickyHeader>

      {!expanded ? null : total === 0 && pinned.length === 0 ? (
        <div className="px-4 pb-3 pt-1 text-[11px] italic text-fg-3">
          {error ? `ledger unavailable: ${error}` : 'No recorded actions yet'}
        </div>
      ) : (
        <div className="space-y-1 px-4 pb-3 pt-1">
          {items.map((item, i) => {
            if (item.type === 'file') {
              const meta = opMeta(item.operation)
              const verb = item.operation === 'create' ? 'created' : 'edited'
              return (
                <div
                  key={`${item.path}-${i}`}
                  title={item.path}
                  className="flex w-full items-center gap-2 rounded-md bg-white/[0.02] px-2 py-1.5 ring-hairline"
                >
                  <span className={`shrink-0 font-mono text-[11px] font-bold ${meta.color}`}>
                    {meta.glyph}
                  </span>
                  <div className="min-w-0 flex-1">
                    <div className="flex items-baseline gap-1.5">
                      <span className="truncate font-mono text-[10.5px] text-fg-0">
                        {verb} {item.filename}
                      </span>
                      {item.count > 1 && (
                        <span className="shrink-0 font-mono text-[9px] tabular-nums text-fg-3">
                          ×{item.count}
                        </span>
                      )}
                      {item.hasDiff && (item.additions > 0 || item.deletions > 0) && (
                        <span className="shrink-0 font-mono text-[9px] tabular-nums">
                          <span className="text-ok">+{item.additions}</span>{' '}
                          <span className="text-danger">−{item.deletions}</span>
                        </span>
                      )}
                    </div>
                    {(item.repo || item.dir) && (
                      <div className="truncate font-mono text-[8.5px] text-fg-3">
                        {item.repo || item.dir}
                      </div>
                    )}
                  </div>
                </div>
              )
            }
            const row = item.row
            const meta = opMeta(row.operation)
            return (
              <div
                key={`${row.summary ?? 'r'}-${i}`}
                title={row.summary}
                className="flex w-full items-center gap-2 rounded-md bg-white/[0.02] px-2 py-1.5 ring-hairline"
              >
                <span className={`shrink-0 font-mono text-[11px] font-bold ${meta.color}`}>
                  {meta.glyph}
                </span>
                <div className="min-w-0 flex-1">
                  <div className="truncate font-mono text-[10.5px] text-fg-0">{row.summary}</div>
                  {(row.repo || row.dir) && (
                    <div className="truncate font-mono text-[8.5px] text-fg-3">
                      {row.repo || row.dir}
                    </div>
                  )}
                </div>
              </div>
            )
          })}
          {pinned.length > 0 && (
            <div className="pt-1">
              <div className="mb-1 font-mono text-[9px] uppercase tracking-[0.08em] text-fg-3">
                pinned notes
              </div>
              {pinned.map((p, i) => (
                <div
                  key={`pin-${i}`}
                  className="rounded-md bg-white/[0.02] px-2 py-1 font-mono text-[10px] text-fg-1 ring-hairline"
                >
                  ★ {p.summary}
                </div>
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  )
}
