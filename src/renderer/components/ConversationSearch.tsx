import { useEffect, useRef } from 'react'

interface Props {
  query: string
  onQueryChange: (q: string) => void
  onClose: () => void
  onNext: () => void
  onPrev: () => void
  total: number
  activeIdx: number // 0-based; -1 when no matches
}

/**
 * Floating search bar pinned to the top of the conversation scroller.
 * Opens on ⌘F, closes on Esc. Enter / ⇧Enter navigate between matches.
 */
export function ConversationSearch({
  query,
  onQueryChange,
  onClose,
  onNext,
  onPrev,
  total,
  activeIdx,
}: Props) {
  const inputRef = useRef<HTMLInputElement>(null)

  // Focus + select the input whenever this component mounts.
  useEffect(() => {
    const el = inputRef.current
    if (!el) return
    el.focus()
    el.select()
  }, [])

  const onKeyDown = (e: React.KeyboardEvent<HTMLInputElement>) => {
    if (e.key === 'Escape') {
      e.preventDefault()
      onClose()
      return
    }
    if (e.key === 'Enter') {
      e.preventDefault()
      if (e.shiftKey) onPrev()
      else onNext()
      return
    }
  }

  const display =
    total === 0
      ? query.trim()
        ? 'no matches'
        : '—'
      : `${activeIdx + 1} / ${total}`

  return (
    <div
      className="pointer-events-none absolute left-0 right-0 top-0 z-10 flex justify-center px-4 pt-3"
      // Let clicks on the actual bar work, but ignore the outer spacer.
    >
      <div className="pointer-events-auto flex w-full max-w-[520px] items-center gap-2 rounded-lg glass-strong px-3 py-2 ring-hairline-strong">
        <span className="label text-fg-2">find</span>
        <input
          ref={inputRef}
          type="text"
          value={query}
          onChange={(e) => onQueryChange(e.target.value)}
          onKeyDown={onKeyDown}
          placeholder="search this session…"
          className="min-w-0 flex-1 bg-transparent font-mono text-[12px] text-fg-0 placeholder:text-fg-3 focus:outline-none"
        />
        <span
          className={`font-mono text-[10.5px] ${
            total === 0 && query.trim() ? 'text-danger/80' : 'text-fg-2'
          }`}
        >
          {display}
        </span>
        <div className="flex items-center gap-1">
          <button
            onClick={onPrev}
            disabled={total === 0}
            title="Previous match (⇧⏎)"
            className="rounded bg-white/[0.04] px-1.5 py-[2px] font-mono text-[11px] text-fg-1 ring-hairline hover:bg-white/[0.08] hover:text-fg-0 disabled:opacity-40 disabled:hover:bg-white/[0.04]"
          >
            ↑
          </button>
          <button
            onClick={onNext}
            disabled={total === 0}
            title="Next match (⏎)"
            className="rounded bg-white/[0.04] px-1.5 py-[2px] font-mono text-[11px] text-fg-1 ring-hairline hover:bg-white/[0.08] hover:text-fg-0 disabled:opacity-40 disabled:hover:bg-white/[0.04]"
          >
            ↓
          </button>
          <button
            onClick={onClose}
            title="Close (esc)"
            className="ml-1 rounded bg-white/[0.04] px-1.5 py-[2px] font-mono text-[10px] uppercase tracking-[0.08em] text-fg-2 ring-hairline hover:bg-white/[0.08] hover:text-fg-0"
          >
            esc
          </button>
        </div>
      </div>
    </div>
  )
}
