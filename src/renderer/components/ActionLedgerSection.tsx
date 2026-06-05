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
  createdAt?: number
}

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

  const { effects, pinned } = useMemo(() => {
    // Dedup effects by path (newest), matching SessionLedger.effects().
    const byPath = new Map<string, LedgerRow>()
    const nonFile: LedgerRow[] = []
    const pins: LedgerRow[] = []
    for (const r of rows) {
      if (r.kind === 'pinned_fact') {
        pins.push(r)
        continue
      }
      if (r.class !== 'effect') continue
      if (r.path) {
        const prev = byPath.get(r.path)
        if (!prev || (r.createdAt ?? 0) >= (prev.createdAt ?? 0)) byPath.set(r.path, r)
      } else {
        nonFile.push(r)
      }
    }
    const merged = [...byPath.values(), ...nonFile].sort(
      (a, b) => (b.createdAt ?? 0) - (a.createdAt ?? 0),
    )
    return { effects: merged, pinned: pins }
  }, [rows])

  const total = effects.length

  return (
    <div className="hairline-b">
      <StickyHeader>
        <div className="flex w-full items-baseline justify-between gap-2 px-4 py-2">
          <button
            onClick={() => setExpanded((v) => !v)}
            className="flex items-baseline gap-2 text-left"
          >
            <div className="label">actions this session</div>
            <span className="font-mono text-[10px] text-fg-3">{total}</span>
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
          {effects.map((row, i) => {
            const meta = opMeta(row.operation)
            return (
              <div
                key={`${row.path ?? row.summary ?? 'r'}-${i}`}
                title={row.path ?? row.summary}
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
