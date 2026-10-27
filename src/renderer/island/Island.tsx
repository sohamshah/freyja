import { useCallback, useEffect, useMemo, useRef, useState, type CSSProperties, type ReactNode } from 'react'
import type {
  IslandAction,
  IslandAttention,
  IslandDigest,
  IslandDone,
  IslandGeometry,
  IslandLive,
  IslandSessionRow,
  IslandTaskRow,
  IslandVoice,
} from '../../shared/island'
import { islandApi } from './bridge'
import { TopographicMark } from '../components/TopographicMark'
import { AnimatedTopographicMark } from '../components/AnimatedTopographicMark'
import { VoiceSigil } from '../components/voice/VoiceSigil'

/** Until main says otherwise: a 16" MacBook Pro notch. */
const FALLBACK_GEOMETRY: IslandGeometry = {
  displayId: 0,
  notched: true,
  notchWidth: 185,
  stripHeight: 33,
  width: 760,
  height: 480,
  scale: 2,
}
/** How long a finished turn hangs from the notch before the island settles. */
const DONE_BANNER_MS = 8000
/** How long the "done" stamp stays on a task that just closed. */
const STAMP_MS = 6000
/** A finished turn older than this is not worth announcing (the island opened late). */
const DONE_STALE_MS = 30000
const HOVER_OPEN_MS = 150
const HOVER_CLOSE_MS = 420

type Mode = 'quiet' | 'live' | 'attention' | 'voice'

export function Island() {
  const api = useMemo(islandApi, [])
  const [digest, setDigest] = useState<IslandDigest | null>(null)
  const [geometry, setGeometry] = useState<IslandGeometry>(FALLBACK_GEOMETRY)
  const [expanded, setExpanded] = useState(false)
  /** The composer holds focus or text: the island must not fold away under it. */
  const [composing, setComposing] = useState(false)
  const now = useNow(1000)
  const openTimer = useRef<number | null>(null)
  const closeTimer = useRef<number | null>(null)

  useEffect(() => {
    const offs = [
      api.onState(setDigest),
      api.onGeometry(setGeometry),
      api.onToggle(() => setExpanded((open) => !open)),
    ]
    void api.control({ type: 'ready' })
    return () => offs.forEach((off) => off())
  }, [api])

  const release = useCallback(() => {
    setExpanded(false)
    setComposing(false)
    const el = document.activeElement as HTMLElement | null
    el?.blur?.()
    void api.control({ type: 'release' })
  }, [api])

  // Esc folds the island and hands the keyboard back.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') release()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [release])

  const clearTimer = (ref: typeof openTimer) => {
    if (ref.current != null) {
      window.clearTimeout(ref.current)
      ref.current = null
    }
  }
  const onEnter = () => {
    void api.control({ type: 'hover', over: true })
    clearTimer(closeTimer)
    if (!expanded) openTimer.current = window.setTimeout(() => setExpanded(true), HOVER_OPEN_MS)
  }
  const onLeave = () => {
    void api.control({ type: 'hover', over: false })
    clearTimer(openTimer)
    if (!composing) closeTimer.current = window.setTimeout(() => setExpanded(false), HOVER_CLOSE_MS)
  }
  useEffect(() => () => {
    clearTimer(openTimer)
    clearTimer(closeTimer)
  }, [])

  const act = useCallback(
    (action: IslandAction) => {
      void api.action(action)
    },
    [api],
  )

  const attention = digest?.attention ?? []
  const live = digest?.live ?? []
  const voice = digest?.voice ?? null
  const mode: Mode = attention.length > 0 ? 'attention' : voice?.active ? 'voice' : live.length > 0 ? 'live' : 'quiet'
  const done = digest?.done && now - digest.done.at < DONE_STALE_MS ? digest.done : null
  const banner = !!done && now - done.at < DONE_BANNER_MS && !expanded
  const justDone = digest?.tasks.justDone && now - digest.tasks.justDone.at < STAMP_MS ? digest.tasks.justDone : null

  const style = {
    '--notch-w': `${geometry.notched ? geometry.notchWidth : 0}px`,
    '--strip-h': `${geometry.stripHeight}px`,
  } as CSSProperties

  return (
    <div className="island-root">
      <div
        className="island"
        data-mode={mode}
        data-expanded={expanded ? 'true' : 'false'}
        data-banner={banner ? 'true' : 'false'}
        data-notched={geometry.notched ? 'true' : 'false'}
        style={style}
        onMouseEnter={onEnter}
        onMouseLeave={onLeave}
        onClick={() => {
          if (!expanded) setExpanded(true)
        }}
      >
        <div className="island-strip">
          <div className="island-ear island-ear-l">
            <Leading mode={mode} voice={voice} banner={banner} />
          </div>
          <div className="island-notch" aria-hidden="true" />
          <div className="island-ear island-ear-r">
            <Trailing mode={mode} digest={digest} voice={voice} live={live} attention={attention} banner={banner} done={done} now={now} expanded={expanded} />
          </div>
        </div>
        <div className="island-body">
          {expanded && digest ? (
            <Sheet
              digest={digest}
              now={now}
              done={done}
              justDone={justDone}
              act={act}
              onComposing={setComposing}
              onSent={release}
            />
          ) : banner && done ? (
            <Banner done={done} onOpen={() => act({ type: 'open', sessionId: done.sessionId })} />
          ) : null}
        </div>
      </div>
    </div>
  )
}

// ── The strip ──────────────────────────────────────────────────────────

function Leading({ mode, voice, banner }: { mode: Mode; voice: IslandVoice | null; banner: boolean }) {
  if (mode === 'voice' && voice) {
    return (
      <span className="island-swap" key="voice">
        <VoiceSigil size={18} state={voice.state} level={voice.level} className="island-sigil" />
      </span>
    )
  }
  if (mode === 'live' && !banner) {
    return (
      <span className="island-mark island-swap" key="live">
        <AnimatedTopographicMark size={18} intensity={0.55} className="island-mark-svg" />
      </span>
    )
  }
  return (
    <span className="island-mark island-swap" key="quiet">
      <TopographicMark size={18} />
    </span>
  )
}

function Trailing({
  mode,
  digest,
  voice,
  live,
  attention,
  banner,
  done,
  now,
  expanded,
}: {
  mode: Mode
  digest: IslandDigest | null
  voice: IslandVoice | null
  live: IslandLive[]
  attention: IslandAttention[]
  banner: boolean
  done: IslandDone | null
  now: number
  expanded: boolean
}) {
  if (banner && done) {
    return (
      <span className="island-swap island-ear-text" key={`done-${done.at}`}>
        <Check ok={done.success} /> {done.success ? 'done' : 'stopped'}
      </span>
    )
  }
  if (mode === 'attention') {
    const first = attention[0]
    return (
      <span className="island-swap island-ear-text" key={`att-${first.id}-${expanded}`}>
        <span className="island-tag island-tag-warn">needs you</span>
        {!expanded && <span className="island-ear-clip">{attentionLine(first)}</span>}
      </span>
    )
  }
  if (mode === 'voice' && voice) {
    return (
      <span className="island-swap island-ear-text" key={`voice-${voice.state}`}>
        <span className="island-ear-clip">{voiceWord(voice)}</span>
        <Meter level={voice.level} live={voice.state === 'listening'} />
      </span>
    )
  }
  if (mode === 'live') {
    if (live.length > 1) {
      const oldest = live.reduce((a, b) => (a.startedAt < b.startedAt ? a : b))
      return (
        <span className="island-swap island-ear-text" key={`multi-${live.length}`}>
          <span className="island-ear-clip">{live.length} live</span>
          <Elapsed since={oldest.startedAt} now={now} />
        </span>
      )
    }
    const row = live[0]
    const word = row.thinking ? 'thinking' : row.tool ? toolWord(row.tool.name) : 'writing'
    const detail = row.tool && !row.thinking && !expanded ? row.tool.summary : ''
    return (
      <span className="island-swap island-ear-text" key={`live-${row.id}-${word}-${expanded}`}>
        <span className="island-ear-clip">
          <b>{word}</b>
          {detail ? ` ${detail}` : ''}
        </span>
        <Elapsed since={row.startedAt} now={now} />
      </span>
    )
  }
  const open = (digest?.tasks.now ?? 0) + (digest?.tasks.next ?? 0)
  if (open > 0) {
    return (
      <span className="island-swap island-ear-text island-dim" key={`open-${open}`}>
        {open} open
      </span>
    )
  }
  return null
}

// ── The sheet ──────────────────────────────────────────────────────────

function Sheet({
  digest,
  now,
  done,
  justDone,
  act,
  onComposing,
  onSent,
}: {
  digest: IslandDigest
  now: number
  done: IslandDone | null
  justDone: { id: string; title: string; at: number } | null
  act: (action: IslandAction) => void
  onComposing: (on: boolean) => void
  onSent: () => void
}) {
  const { attention, live, tasks, voice, sessions, active, appshotLabel } = digest
  return (
    <div className="island-sheet">
      {attention.map((item) => (
        <AttentionCard key={item.id} item={item} act={act} />
      ))}
      {live.length > 0 && (
        <section className="island-section">
          <h3 className="island-h">live</h3>
          {live.map((row) => (
            <LiveRow key={row.id} row={row} now={now} act={act} />
          ))}
        </section>
      )}
      {done && (
        <section className="island-section">
          <h3 className="island-h">just finished</h3>
          <DoneRow done={done} act={act} />
        </section>
      )}
      <section className="island-section">
        <h3 className="island-h">
          tasks
          <span className="island-counts">
            <span className="island-glyph-now">◐</span> {tasks.now} · <span className="island-glyph-next">○</span> {tasks.next} ·{' '}
            <span className="island-glyph-blocked">⊘</span> {tasks.blocked} · <span className="island-glyph-done">✓</span> {tasks.done}
          </span>
        </h3>
        {tasks.latest.length === 0 ? (
          <div className="island-empty">no tasks on the board</div>
        ) : (
          tasks.latest.map((task) => <TaskRow key={task.id} task={task} stamped={justDone?.id === task.id} act={act} />)
        )}
      </section>
      <Composer sessions={sessions} active={active} act={act} onComposing={onComposing} onSent={onSent} />
      <div className="island-keys">
        <button
          type="button"
          className={`island-key ${voice.active ? 'island-key-live' : ''}`}
          title={voice.available ? 'start a voice conversation' : 'voice is off or has no key (settings)'}
          onClick={() => act({ type: 'voice', on: !voice.active })}
        >
          {voice.active ? 'hang up' : 'talk'}
          {voice.hotkeyLabel && <kbd>{voice.hotkeyLabel}</kbd>}
        </button>
        <button type="button" className="island-key" title="capture the frontmost window into the composer" onClick={() => act({ type: 'capture' })}>
          capture
          {appshotLabel && <kbd>{appshotLabel}</kbd>}
        </button>
        <button type="button" className="island-key" onClick={() => act({ type: 'dashboard', tab: 'tasks' })}>
          board
        </button>
        <button type="button" className="island-key island-key-end" onClick={() => act({ type: 'focus-app' })}>
          open Freyja
        </button>
      </div>
    </div>
  )
}

function AttentionCard({ item, act }: { item: IslandAttention; act: (action: IslandAction) => void }) {
  if (item.kind === 'permission') {
    const tone = item.level === 'dangerous' ? 'danger' : item.level === 'high' || item.level === 'medium' ? 'warn' : 'info'
    return (
      <div className={`island-card island-card-${tone}`}>
        <div className="island-card-head">
          <span className={`island-tag island-tag-${tone}`}>{item.level}</span>
          <span className="island-card-session">{item.sessionTitle}</span>
        </div>
        <div className="island-card-text">{item.prompt}</div>
        {item.reason && <div className="island-card-sub">{item.reason}</div>}
        <div className="island-card-keys">
          <button type="button" className="island-key" onClick={() => act({ type: 'permission', requestId: item.id, approved: false })}>
            deny
          </button>
          <button
            type="button"
            className={`island-key island-key-primary ${tone === 'danger' ? 'island-key-danger' : ''}`}
            onClick={() => act({ type: 'permission', requestId: item.id, approved: true })}
          >
            allow
          </button>
        </div>
      </div>
    )
  }
  if (item.kind === 'elicitation') {
    return (
      <div className="island-card island-card-info">
        <div className="island-card-head">
          <span className="island-tag island-tag-info">{item.server}</span>
          <span className="island-card-session">asks</span>
        </div>
        <div className="island-card-text">{item.message}</div>
        <div className="island-card-keys">
          <button type="button" className="island-key" onClick={() => act({ type: 'elicitation', requestId: item.id, action: 'decline' })}>
            decline
          </button>
          <button type="button" className="island-key island-key-primary" onClick={() => act({ type: 'focus-app' })}>
            answer in Freyja
          </button>
        </div>
      </div>
    )
  }
  return (
    <div className="island-card island-card-warn">
      <div className="island-card-head">
        <span className="island-tag island-tag-warn">voice</span>
        <span className="island-card-session">confirm</span>
      </div>
      <div className="island-card-text">{item.summary}</div>
      <div className="island-card-keys">
        <button type="button" className="island-key" onClick={() => act({ type: 'voice-confirm', approve: false })}>
          no
        </button>
        <button type="button" className="island-key island-key-primary" onClick={() => act({ type: 'voice-confirm', approve: true })}>
          yes
        </button>
      </div>
    </div>
  )
}

function LiveRow({ row, now, act }: { row: IslandLive; now: number; act: (action: IslandAction) => void }) {
  const word = row.thinking ? 'thinking' : row.tool ? toolWord(row.tool.name) : 'writing'
  return (
    <button type="button" className="island-row" onClick={() => act({ type: 'open', sessionId: row.id })} title="open in Freyja">
      <span className={`island-row-kind island-row-kind-${row.kind}`}>{row.kind === 'sub' ? '↳' : row.kind === 'voice' ? '◉' : '●'}</span>
      <span className="island-row-title">{row.title}</span>
      <span className="island-row-tool">
        {word}
        {row.tool && !row.thinking && row.tool.summary ? <span className="island-row-detail"> {row.tool.summary}</span> : null}
      </span>
      <Elapsed since={row.startedAt} now={now} />
      {row.text && <span className="island-row-peek">{row.text}</span>}
    </button>
  )
}

function DoneRow({ done, act }: { done: IslandDone; act: (action: IslandAction) => void }) {
  return (
    <button type="button" className="island-row island-row-done" onClick={() => act({ type: 'open', sessionId: done.sessionId })} title="open in Freyja">
      <span className="island-row-kind">
        <Check ok={done.success} />
      </span>
      <span className="island-row-title">{done.title}</span>
      <span className="island-row-tool">{done.success ? 'done' : 'stopped'}</span>
      <span className="island-elapsed">{elapsed(done.durationMs)}</span>
      {done.text && <span className="island-row-peek island-row-peek-wrap">{done.text}</span>}
    </button>
  )
}

function TaskRow({ task, stamped, act }: { task: IslandTaskRow; stamped: boolean; act: (action: IslandAction) => void }) {
  const glyph = task.bucket === 'now' ? '◐' : task.bucket === 'next' ? '○' : task.bucket === 'blocked' ? '⊘' : '✓'
  return (
    <button type="button" className="island-row island-row-task" onClick={() => act({ type: 'dashboard', tab: 'tasks' })} title="open the board">
      <span className={`island-row-kind island-glyph-${task.bucket}`}>{glyph}</span>
      <span className={`island-row-title ${task.bucket === 'done' ? 'island-dim' : ''}`}>{task.title}</span>
      <span className="island-row-tool island-dim">{task.bucket === 'next' ? 'up next' : task.bucket}</span>
      {stamped && (
        <span className="island-stamp-slot">
          <span className="ticket-stamp ticket-stamp-ok island-stamp">done</span>
        </span>
      )}
    </button>
  )
}

function Composer({
  sessions,
  active,
  act,
  onComposing,
  onSent,
}: {
  sessions: IslandSessionRow[]
  active: IslandDigest['active']
  act: (action: IslandAction) => void
  onComposing: (on: boolean) => void
  onSent: () => void
}) {
  const [text, setText] = useState('')
  const [chosen, setChosen] = useState<string | null>(null)
  const options = useMemo(() => {
    const rows: IslandSessionRow[] = []
    const seen = new Set<string>()
    if (active) {
      rows.push({ id: active.id, title: active.title, updatedAt: Date.now(), streaming: active.streaming })
      seen.add(active.id)
    }
    for (const row of sessions) {
      if (!seen.has(row.id)) {
        rows.push(row)
        seen.add(row.id)
      }
    }
    return rows
  }, [sessions, active])
  const target = options.find((row) => row.id === chosen) ?? options[0] ?? null
  const send = (cutIn: boolean) => {
    const body = text.trim()
    if (!body || !target) return
    act({ type: 'send', sessionId: target.id, text: body, cutIn })
    setText('')
    onComposing(false)
    onSent()
  }
  const hint = !target ? 'no session yet' : target.streaming ? `reply to ${target.title} · queues after the turn, ⌘↵ cuts in` : `reply to ${target.title}`
  return (
    <form
      className={`island-composer ${options.length > 1 ? '' : 'island-composer-single'}`}
      onSubmit={(e) => {
        e.preventDefault()
        send(false)
      }}
    >
      {options.length > 1 && (
        <select className="island-select" value={target?.id ?? ''} onChange={(e) => setChosen(e.target.value)} title="which session">
          {options.map((row) => (
            <option key={row.id} value={row.id}>
              {row.streaming ? '● ' : ''}
              {row.title}
            </option>
          ))}
        </select>
      )}
      <input
        className="island-input"
        value={text}
        placeholder={hint}
        disabled={!target}
        onChange={(e) => {
          setText(e.target.value)
          onComposing(true)
        }}
        onFocus={() => onComposing(true)}
        onBlur={() => onComposing(text.trim().length > 0)}
        onKeyDown={(e) => {
          if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) {
            e.preventDefault()
            send(true)
          }
        }}
      />
      <button type="submit" className="island-key island-key-primary" disabled={!text.trim() || !target}>
        send
      </button>
    </form>
  )
}

function Banner({ done, onOpen }: { done: IslandDone; onOpen: () => void }) {
  return (
    <button type="button" className="island-banner" onClick={onOpen} title="open in Freyja">
      <span className="island-banner-title">{done.title}</span>
      <span className="island-banner-meta">{elapsed(done.durationMs)}</span>
      {done.text && <span className="island-banner-text">{done.text}</span>}
    </button>
  )
}

// ── Small parts ────────────────────────────────────────────────────────

function Elapsed({ since, now }: { since: number; now: number }) {
  return <span className="island-elapsed">{elapsed(now - since)}</span>
}

function Check({ ok }: { ok: boolean }) {
  return (
    <svg className={`island-check ${ok ? '' : 'island-check-bad'}`} viewBox="0 0 14 14" width="12" height="12" aria-hidden="true">
      {ok ? (
        <path d="M2.5 7.5 L5.8 10.6 L11.5 3.8" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" />
      ) : (
        <path d="M3.5 3.5 L10.5 10.5 M10.5 3.5 L3.5 10.5" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" />
      )}
    </svg>
  )
}

/** Three bars that follow the mic. Driven by the level, so it is a meter, not a decoration. */
function Meter({ level, live }: { level: number; live: boolean }) {
  const l = Math.max(0, Math.min(1, level))
  const heights = [0.35 + l * 0.65, 0.2 + l * 0.8, 0.45 + l * 0.55]
  return (
    <span className={`island-meter ${live ? 'island-meter-live' : ''}`} aria-hidden="true">
      {heights.map((h, i) => (
        <span key={i} style={{ transform: `scaleY(${h.toFixed(2)})` }} />
      ))}
    </span>
  )
}

function useNow(everyMs: number): number {
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => {
    const id = window.setInterval(() => setNow(Date.now()), everyMs)
    return () => window.clearInterval(id)
  }, [everyMs])
  return now
}

function pad(n: number): string {
  return String(n).padStart(2, '0')
}

export function elapsed(ms: number): string {
  const s = Math.max(0, Math.floor(ms / 1000))
  const m = Math.floor(s / 60)
  const h = Math.floor(m / 60)
  if (h > 0) return `${h}:${pad(m % 60)}:${pad(s % 60)}`
  return `${m}:${pad(s % 60)}`
}

function toolWord(name: string): string {
  return name.replace(/^mcp__/, '').replace(/[_-]+/g, ' ')
}

function voiceWord(voice: IslandVoice): ReactNode {
  switch (voice.state) {
    case 'listening':
      return voice.userLine ? voice.userLine : 'listening'
    case 'thinking':
      return 'thinking'
    case 'acting':
      return 'doing it'
    case 'speaking':
      return voice.assistantLine ? voice.assistantLine : 'speaking'
    case 'minting':
    case 'connecting':
      return 'connecting'
    case 'error':
      return 'voice error'
    case 'closing':
      return 'closing'
    default:
      return 'talk'
  }
}

function attentionLine(item: IslandAttention): string {
  if (item.kind === 'permission') return item.prompt
  if (item.kind === 'elicitation') return `${item.server} asks`
  return item.summary
}
