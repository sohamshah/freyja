import { useContext } from 'react'
import { useHarness } from '../state/store'
import { StatusPip, DatelineTS } from './memory/primitives'
import { SystemEventLookupContext } from './Conversation'

/**
 * Grounded Memory — inline conversation cards.
 *
 * Two restrained cards that drop into the message stream when the runtime
 * notices the agent's self-model diverging from the durable ledger
 * (InlineForgetting) or relocates — but never loses — work during a
 * compaction (InlineCompactionReceipt). Both hydrate from the shared
 * SystemEventLookupContext exactly like InlineRefusal: they receive an
 * { eventId } pointer and read the structured payload at render time so the
 * MessagePart shape stays a thin reference.
 *
 * These are the CALMEST surfaces in the memory language. Each fades in once
 * (animate-fade-in, 220ms) and never pulses — only the single live
 * working-memory card is allowed to move. They are sized like InlineRefusal:
 * compact, hairline-separated, not full-bleed. The status trio is muted
 * toward grey (ok/sage, danger/dusty-rose); the lone accent stays reserved
 * for the live/now elsewhere, so neither card raises its voice.
 */

/** OP-glyph map cloned from ActionLedgerSection so any effect summaries read
 *  identically to the Action Ledger: + create (sage), ~ edit (accent),
 *  $ shell (tan), ★ pin (fg-1). Used only if a payload happens to carry a
 *  top_effects array — the canonical forgetting/receipt payload is just a
 *  count, so the cards never depend on it. */
const OP_META: Record<string, { glyph: string; color: string }> = {
  create: { glyph: '+', color: 'text-ok' },
  edit: { glyph: '~', color: 'text-accent' },
  shell: { glyph: '$', color: 'text-warn' },
  pin: { glyph: '★', color: 'text-fg-1' },
}

interface EffectRow {
  op?: string
  summary?: string
  path?: string
}

/** Pull a best-effort list of recent effects from the event details so the
 *  forgetting card can show a few concrete actions when the payload carries
 *  them. Tolerant of either `top_effects` or `effects`; returns [] otherwise. */
function readEffects(details: Record<string, unknown> | undefined): EffectRow[] {
  if (!details) return []
  const raw =
    (details.top_effects as unknown) ??
    (details.topEffects as unknown) ??
    (details.effects as unknown)
  if (!Array.isArray(raw)) return []
  return raw
    .filter((r): r is Record<string, unknown> => !!r && typeof r === 'object')
    .map((r) => ({
      op: typeof r.op === 'string' ? r.op : undefined,
      summary: typeof r.summary === 'string' ? r.summary : undefined,
      path: typeof r.path === 'string' ? r.path : undefined,
    }))
}

function plural(n: number): string {
  return n === 1 ? 'action' : 'actions'
}

/** The 'recall ↗' open-affordance, styled as a .kanban-card-mention so it
 *  reads as a quiet reference into the memory surfaces rather than a button.
 *  Opens the Grounded Memory recall drawer ("The Morgue") via the store; the
 *  click is stopped from bubbling so the Conversation's delegated
 *  .kanban-card-mention handler (which opens the mission dashboard) doesn't
 *  also fire. `query` seeds the drawer's search field when provided. */
function RecallAffordance({
  tone = 'accent',
  query,
}: {
  tone?: 'accent' | 'ok'
  query?: string
}) {
  const openRecallDrawer = useHarness((s) => s.openRecallDrawer)
  return (
    <span
      role="button"
      tabIndex={0}
      className={`kanban-card-mention cursor-pointer text-[9.5px] ${
        tone === 'ok' ? 'text-ok/90' : ''
      }`}
      title="Open recall"
      onClick={(e) => {
        e.preventDefault()
        e.stopPropagation()
        openRecallDrawer(query)
      }}
      onKeyDown={(e) => {
        if (e.key === 'Enter' || e.key === ' ') {
          e.preventDefault()
          e.stopPropagation()
          openRecallDrawer(query)
        }
      }}
    >
      history <span className="text-[9px]">↗</span>
    </span>
  )
}

// ============ INLINE FORGETTING ============
//
// CORRECTION / erratum. Fires when the agent's last message disowned work
// the ledger shows it did. The tone is calm, not alarmist: a struck belief
// line sits directly above the ground truth, framed as "the record disagrees"
// rather than an error. Reads details.effect_count from the
// forgetting_detected system_event; if absent, renders nothing so the default
// system chip handles it.

export function InlineForgetting({ eventId }: { eventId: string }) {
  const lookup = useContext(SystemEventLookupContext)
  const event = lookup.get(eventId)
  const details = event?.details as Record<string, unknown> | undefined
  const rawCount = details?.effect_count ?? (details as { effectCount?: number })?.effectCount
  const count = typeof rawCount === 'number' ? rawCount : undefined

  // No count → let the default chip handle it. The card has nothing
  // load-bearing to say without the ground-truth number.
  if (count == null) return null

  const effects = readEffects(details).slice(0, 3)
  const remaining = Math.max(0, count - effects.length)

  return (
    <div
      className="animate-fade-in flex flex-col gap-1.5 rounded-md bg-danger/[0.05] px-3 py-2 text-[11px] ring-1 ring-danger/15"
      style={{ borderLeft: '2px solid var(--danger)' }}
    >
      {/* kicker — calm correction framing, not an alarm */}
      <div className="flex items-baseline justify-between gap-2">
        <span className="label text-danger">correction</span>
        {event && <DatelineTS ts={event.at} />}
      </div>

      {/* the struck belief — what the message implied */}
      <div className="flex items-baseline gap-1.5">
        <span aria-hidden="true" className="shrink-0 font-mono text-[10.5px] leading-[1.5] text-fg-2">
          ⊘
        </span>
        <span className="min-w-0 flex-1 font-mono text-[10.5px] leading-[1.5] text-fg-2 line-through">
          I implied nothing was changed this session
        </span>
      </div>

      {/* the ground truth — what the ledger actually holds */}
      <div className="flex items-baseline gap-1.5">
        <StatusPip status="blocked" className="mt-[5px]" />
        <span className="min-w-0 flex-1 font-mono text-[10.5px] leading-[1.5] text-fg-0">
          but you took{' '}
          <span className="tabular-nums">{count}</span> {plural(count)} this session
          {effects.length > 0 ? ':' : '.'}
        </span>
      </div>

      {/* concrete top effects via OP glyphs, when the payload carries them */}
      {effects.length > 0 && (
        <div className="flex flex-col gap-0.5 pl-[18px]">
          {effects.map((eff, i) => {
            const meta = OP_META[eff.op ?? ''] ?? { glyph: '·', color: 'text-fg-3' }
            return (
              <div key={i} className="flex items-baseline gap-1.5">
                <span
                  aria-hidden="true"
                  className={`w-3 shrink-0 text-center font-mono text-[10px] leading-[1.5] ${meta.color}`}
                >
                  {meta.glyph}
                </span>
                <span className="min-w-0 flex-1 truncate font-mono text-[10px] leading-[1.5] text-fg-2">
                  {eff.summary || eff.path || 'action'}
                </span>
              </div>
            )
          })}
          {remaining > 0 && (
            <span className="pl-[18px] font-mono text-[9.5px] text-fg-3">
              …<span className="tabular-nums">{remaining}</span> more
            </span>
          )}
        </div>
      )}

      <div className="pt-px">
        <RecallAffordance />
      </div>
    </div>
  )
}

// ============ INLINE COMPACTION RECEIPT ============
//
// PRESS RECEIPT. Fires after a real LLM compaction: work was relocated, not
// lost — the durable ledger still holds the actions and `recall` recovers the
// older detail. Sage/ok throughout, with a "nothing lost" headline so the
// user reads reassurance, not a warning. Reads details.effect_count from the
// compaction_receipt system_event.

export function InlineCompactionReceipt({ eventId }: { eventId: string }) {
  const lookup = useContext(SystemEventLookupContext)
  const event = lookup.get(eventId)
  const details = event?.details as Record<string, unknown> | undefined
  const rawCount = details?.effect_count ?? (details as { effectCount?: number })?.effectCount
  const count = typeof rawCount === 'number' ? rawCount : undefined

  if (count == null) return null

  return (
    <div
      className="animate-fade-in flex flex-col gap-1.5 rounded-md bg-ok/[0.06] px-3 py-2 text-[11px] ring-1 ring-ok/15"
      style={{ borderLeft: '2px solid var(--ok)' }}
    >
      {/* kicker — reassuring: context was compacted, nothing lost */}
      <div className="flex items-baseline justify-between gap-2">
        <span className="label text-ok">context compacted</span>
        {event && <DatelineTS ts={event.at} />}
      </div>

      {/* the durable line — what was kept */}
      <div className="flex items-baseline gap-1.5">
        <span aria-hidden="true" className="shrink-0 font-mono text-[10.5px] leading-[1.5] text-ok">
          ✓
        </span>
        <span className="min-w-0 flex-1 font-mono text-[10.5px] leading-[1.5] text-fg-0">
          <span className="tabular-nums">{count}</span> {plural(count)} kept; older detail still
          searchable
        </span>
      </div>

      {/* recovery affordance — older detail isn't gone, just searchable */}
      <div className="flex items-baseline gap-1.5">
        <span className="min-w-0 flex-1 font-mono text-[10px] leading-[1.5] text-fg-2">
          view the full transcript — <RecallAffordance tone="ok" />
        </span>
      </div>
    </div>
  )
}
