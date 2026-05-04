import { useEffect, useMemo, useRef, useState } from 'react'
import { useHarness } from '../state/store'

type LogLevel = 'error' | 'warn' | 'info' | 'debug' | 'other'

function classifyLevel(level: string): LogLevel {
  const l = level.toLowerCase()
  if (l === 'error' || l === 'err') return 'error'
  if (l === 'warn' || l === 'warning') return 'warn'
  if (l === 'info') return 'info'
  if (l === 'debug' || l === 'trace') return 'debug'
  return 'other'
}

const LEVEL_COLOR: Record<LogLevel, string> = {
  error: 'text-danger',
  warn: 'text-warn',
  info: 'text-ok',
  debug: 'text-fg-2',
  other: 'text-fg-2',
}

const LEVEL_ORDER: LogLevel[] = ['error', 'warn', 'info', 'debug']

function formatTimestamp(at: number): string {
  const d = new Date(at)
  const hh = String(d.getHours()).padStart(2, '0')
  const mm = String(d.getMinutes()).padStart(2, '0')
  const ss = String(d.getSeconds()).padStart(2, '0')
  const ms = String(d.getMilliseconds()).padStart(3, '0')
  return `${hh}:${mm}:${ss}.${ms}`
}

/**
 * Expanded log stream viewer. Shows every buffered log line without the
 * right-panel's truncation so errors, stderr dumps, and provider failures
 * are readable end-to-end. Supports level filtering, plain-text search,
 * copy-to-clipboard, and auto-scroll with a "pause follow" when the user
 * scrolls up to read backwards.
 */
export function LogStreamModal({ onClose }: { onClose: () => void }) {
  const logs = useHarness((s) => s.logs)

  const [query, setQuery] = useState('')
  const [levels, setLevels] = useState<Record<LogLevel, boolean>>({
    error: true,
    warn: true,
    info: true,
    debug: true,
    other: true,
  })
  const [follow, setFollow] = useState(true)

  const scrollerRef = useRef<HTMLDivElement>(null)

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase()
    return logs.filter((l) => {
      const lvl = classifyLevel(l.level)
      if (!levels[lvl]) return false
      if (q && !l.message.toLowerCase().includes(q)) return false
      return true
    })
  }, [logs, query, levels])

  // Auto-scroll to bottom when new entries arrive, unless the user has
  // scrolled up (i.e. follow mode is off).
  useEffect(() => {
    const el = scrollerRef.current
    if (!el || !follow) return
    el.scrollTop = el.scrollHeight
  }, [filtered.length, follow])

  // Dismiss on Escape.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        e.preventDefault()
        onClose()
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose])

  const onScroll = () => {
    const el = scrollerRef.current
    if (!el) return
    const distFromBottom = el.scrollHeight - el.scrollTop - el.clientHeight
    // If we're within 40px of the bottom, keep following. Otherwise pause.
    setFollow(distFromBottom < 40)
  }

  const copyAll = async () => {
    const text = filtered
      .map(
        (l) =>
          `${formatTimestamp(l.at)} ${l.level.padEnd(5)} ${l.message}`,
      )
      .join('\n')
    try {
      await navigator.clipboard.writeText(text)
    } catch {
      // ignore — clipboard may be blocked in dev
    }
  }

  const counts = useMemo(() => {
    const c: Record<LogLevel, number> = {
      error: 0,
      warn: 0,
      info: 0,
      debug: 0,
      other: 0,
    }
    for (const l of logs) c[classifyLevel(l.level)]++
    return c
  }, [logs])

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center p-6">
      <div
        className="absolute inset-0 bg-black/55 backdrop-blur-[3px]"
        onClick={onClose}
      />
      <div className="relative flex h-[82vh] w-[min(1080px,94vw)] flex-col overflow-hidden rounded-2xl glass-strong shadow-2xl ring-hairline-strong">
        {/* Header */}
        <div className="flex items-center gap-3 px-5 py-3 hairline-b">
          <span className="block h-2 w-2 rounded-full bg-accent" />
          <span className="label">log stream</span>
          <span className="font-mono text-[10.5px] text-fg-3">
            {filtered.length}/{logs.length} entries
          </span>
          <div className="ml-auto flex items-center gap-2">
            <button
              onClick={copyAll}
              title="Copy visible entries to clipboard"
              className="rounded bg-white/[0.05] px-2.5 py-[4px] font-mono text-[10px] uppercase tracking-[0.08em] text-fg-1 ring-hairline hover:bg-white/[0.08] hover:text-fg-0"
            >
              copy
            </button>
            <button
              onClick={onClose}
              className="rounded bg-white/[0.05] px-2.5 py-[4px] font-mono text-[10px] uppercase tracking-[0.08em] text-fg-1 ring-hairline hover:bg-white/[0.08] hover:text-fg-0"
            >
              esc close
            </button>
          </div>
        </div>

        {/* Toolbar — filters + search */}
        <div className="flex items-center gap-2 px-5 py-2.5 hairline-b">
          {LEVEL_ORDER.map((lvl) => {
            const active = levels[lvl]
            return (
              <button
                key={lvl}
                onClick={() =>
                  setLevels((p) => ({ ...p, [lvl]: !p[lvl] }))
                }
                className={`rounded px-2 py-[3px] font-mono text-[10px] uppercase tracking-[0.08em] ring-hairline transition-colors ${
                  active
                    ? `${LEVEL_COLOR[lvl]} bg-white/[0.06]`
                    : 'text-fg-3 hover:bg-white/[0.04]'
                }`}
                title={`Toggle ${lvl} entries`}
              >
                {lvl} <span className="text-fg-3">({counts[lvl]})</span>
              </button>
            )
          })}
          <div className="ml-auto flex items-center gap-2">
            <input
              type="text"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder="filter…"
              className="w-[220px] rounded bg-white/[0.04] px-2.5 py-[4px] font-mono text-[11px] text-fg-0 ring-hairline placeholder:text-fg-3 focus:outline-none focus:ring-1 focus:ring-accent/40"
            />
            {!follow && (
              <button
                onClick={() => {
                  const el = scrollerRef.current
                  if (el) el.scrollTop = el.scrollHeight
                  setFollow(true)
                }}
                className="rounded bg-accent/15 px-2 py-[3px] font-mono text-[10px] uppercase tracking-[0.08em] text-accent ring-1 ring-accent/30 hover:bg-accent/25"
              >
                jump to tail ↓
              </button>
            )}
          </div>
        </div>

        {/* Log body */}
        <div
          ref={scrollerRef}
          onScroll={onScroll}
          className="flex-1 overflow-y-auto bg-black/35 px-5 py-3 font-mono text-[11.5px] leading-[1.55]"
        >
          {filtered.length === 0 ? (
            <div className="py-6 text-center italic text-fg-3">
              {logs.length === 0
                ? '— no log entries yet —'
                : '— no entries match the current filter —'}
            </div>
          ) : (
            filtered.map((l, i) => {
              const lvl = classifyLevel(l.level)
              return (
                <div
                  key={`${l.at}-${i}`}
                  className="selectable flex items-start gap-3 py-[2px] hover:bg-white/[0.02]"
                >
                  <span className="w-[96px] shrink-0 text-fg-3">
                    {formatTimestamp(l.at)}
                  </span>
                  <span
                    className={`w-[52px] shrink-0 uppercase ${LEVEL_COLOR[lvl]}`}
                  >
                    {l.level}
                  </span>
                  <span className="min-w-0 flex-1 whitespace-pre-wrap break-words text-fg-1">
                    {l.message}
                  </span>
                </div>
              )
            })
          )}
        </div>

        {/* Footer hint */}
        <div className="hairline-t px-5 py-2 text-[10px] text-fg-3">
          <span className="font-mono">esc</span> close ·{' '}
          <span className="font-mono">↑↓</span> scroll ·{' '}
          {follow ? (
            <span className="text-accent/80">following tail</span>
          ) : (
            <span>paused — scroll to bottom to resume</span>
          )}
        </div>
      </div>
    </div>
  )
}
