import { useState } from 'react'
import type { Receipt } from '@shared/events'
import { useVoiceStore } from '../../state/voice-store'
import { StickyHeader } from '../StickyHeader'

/**
 * VoiceReceiptsSection — the paper trail of the Galdr voice lane inside
 * the ActivityPanel (contract §7.3). One row per verb execution,
 * refusals included: time · what was heard · what actually happened ·
 * undo. Live — voice_receipt events land here as they happen.
 */

const VISIBLE = 12

export function VoiceReceiptsSection() {
  const receipts = useVoiceStore((s) => s.receipts)
  const undo = useVoiceStore((s) => s.undo)
  const [expanded, setExpanded] = useState(true)

  const recent = receipts.slice(0, VISIBLE)

  return (
    <div className="hairline-b">
      <StickyHeader>
        <div className="flex w-full items-baseline justify-between gap-2 px-4 py-2">
          <button
            onClick={() => setExpanded((v) => !v)}
            className="flex items-baseline gap-2 text-left"
          >
            <div className="label">voice</div>
            <span className="font-mono text-[10px] text-fg-3">{receipts.length}</span>
            <span className="text-[9px] text-fg-3">{expanded ? '▾' : '▸'}</span>
          </button>
        </div>
      </StickyHeader>

      {!expanded ? null : recent.length === 0 ? (
        <div className="px-4 pb-3 pt-1 text-[11px] italic text-fg-3">
          No voice activity yet · ⌥space to speak
        </div>
      ) : (
        <div className="space-y-1 px-4 pb-3 pt-1">
          {recent.map((receipt) => (
            <ReceiptRow key={receipt.id} receipt={receipt} onUndo={() => undo(receipt.id)} />
          ))}
        </div>
      )}
    </div>
  )
}

function ReceiptRow({ receipt, onUndo }: { receipt: Receipt; onUndo: () => void }) {
  return (
    <div
      className={`rounded-md px-2 py-1.5 ring-hairline ${
        receipt.ok ? 'bg-white/[0.02]' : 'bg-danger/[0.05]'
      }`}
    >
      <div className="flex items-center gap-2">
        <span
          className={`h-1 w-1 shrink-0 rounded-full ${laneDotClass(receipt.lane)}`}
          title={`${receipt.lane} lane · ${receipt.verb}`}
        />
        <span className="shrink-0 font-mono text-[10px] tabular-nums text-fg-3">
          {hhmm(receipt.ts)}
        </span>
        {receipt.heard ? (
          <span className="min-w-0 flex-1 truncate text-[10.5px] italic text-fg-2">
            “{receipt.heard}”
          </span>
        ) : (
          <span className="min-w-0 flex-1 truncate text-[10.5px] italic text-fg-3">
            (typed)
          </span>
        )}
        {receipt.undoable && !receipt.undone && (
          <button
            onClick={onUndo}
            className="shrink-0 border-b border-dotted border-accent/60 font-mono text-[9.5px] text-accent hover:border-accent hover:text-accent-hi"
          >
            undo
          </button>
        )}
        {receipt.undone && (
          <span className="shrink-0 font-mono text-[8.5px] uppercase tracking-[0.1em] text-fg-3">
            undone
          </span>
        )}
      </div>
      <div
        className={`mt-0.5 truncate pl-3 text-[11px] leading-[1.4] ${
          receipt.ok ? 'text-fg-1' : 'text-danger/90'
        } ${receipt.undone ? 'line-through decoration-fg-3/70 opacity-70' : ''}`}
        title={receipt.summary}
      >
        {receipt.summary}
      </div>
    </div>
  )
}

function laneDotClass(lane: Receipt['lane']): string {
  switch (lane) {
    case 'floor':
      return 'bg-ok'
    case 'mission':
      return 'bg-warn'
    case 'undo':
      return 'bg-fg-3'
    default:
      return 'bg-accent'
  }
}

function hhmm(ts: number): string {
  return new Date(ts).toLocaleTimeString([], {
    hour: '2-digit',
    minute: '2-digit',
    hour12: false,
  })
}
