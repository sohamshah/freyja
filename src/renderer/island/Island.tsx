import { useCallback, useEffect, useMemo, useRef, useState, type CSSProperties, type ReactNode } from 'react'
import type { IslandAction, IslandAttention, IslandDigest, IslandDone, IslandGeometry, IslandLive, IslandPaletteIndex, IslandVoice } from '../../shared/island'
import { islandApi } from './bridge'
import { Sheet } from './Sheet'
import { Check, Elapsed, Meter, elapsed, liveDetail, liveWord, useNow } from './parts'
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
  height: 600,
  scale: 2,
  hotkey: null,
}
/** How long a finished turn hangs from the notch before the island settles. */
const DONE_BANNER_MS = 8000
/** How long the "done" stamp stays on a task that just closed. */
const STAMP_MS = 6000
/** A finished turn older than this is not worth announcing (the island opened late). */
const DONE_STALE_MS = 30000
const HOVER_OPEN_MS = 150
const HOVER_CLOSE_MS = 420
/** A new needs-you item opens the sheet by itself for this long. */
const ATTENTION_PEEK_MS = 6000
/** Files over the island: the shelf state. */
const DROP_MS = 1200
const FOLLOW_KEY = 'freyja.island.follow'

type Mode = 'quiet' | 'live' | 'attention' | 'voice'

export function Island() {
  const api = useMemo(islandApi, [])
  const [digest, setDigest] = useState<IslandDigest | null>(null)
  const [geometry, setGeometry] = useState<IslandGeometry>(FALLBACK_GEOMETRY)
  const [palette, setPalette] = useState<IslandPaletteIndex | null>(null)
  const [expanded, setExpanded] = useState(false)
  /** The field holds focus or text: the island must not fold away under it. */
  const [composing, setComposing] = useState(false)
  /** The hotkey opened the island: the field takes the keyboard. */
  const [wantKeyboard, setWantKeyboard] = useState(0)
  /** Something is being dragged over the island. */
  const [dropping, setDropping] = useState(false)
  /** Text dropped on the island lands in the field. */
  const [droppedText, setDroppedText] = useState('')
  /** Bumps when a new needs-you item arrives: the shape nudges once. */
  const [nudge, setNudge] = useState(0)
  /** The banner's reply key (or a row's) aims the field at that session. */
  const [replyTo, setReplyTo] = useState<string | null>(null)
  /** The session the strip follows while several are live. */
  const [follow, setFollowState] = useState<string | null>(() => {
    try {
      return localStorage.getItem(FOLLOW_KEY)
    } catch {
      return null
    }
  })
  const now = useNow(1000)
  const openTimer = useRef<number | null>(null)
  const closeTimer = useRef<number | null>(null)
  const dropTimer = useRef<number | null>(null)
  const hovering = useRef(false)
  const seenAttention = useRef<Set<string> | null>(null)
  const islandRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    const offs = [
      api.onState(setDigest),
      api.onGeometry(setGeometry),
      api.onPalette(setPalette),
      api.onToggle(() => {
        setExpanded((open) => {
          if (!open) setWantKeyboard((n) => n + 1)
          return !open
        })
      }),
    ]
    void api.control({ type: 'ready' })
    return () => offs.forEach((off) => off())
  }, [api])

  const setFollow = useCallback((id: string | null) => {
    setFollowState(id)
    try {
      if (id) localStorage.setItem(FOLLOW_KEY, id)
      else localStorage.removeItem(FOLLOW_KEY)
    } catch {
      /* no storage: the follow lasts the session */
    }
  }, [])

  const release = useCallback(() => {
    setExpanded(false)
    setComposing(false)
    const el = document.activeElement as HTMLElement | null
    el?.blur?.()
    void api.control({ type: 'release' })
  }, [api])

  // Esc folds the island and hands the keyboard back. So does the keyboard
  // leaving on its own (a click into another app while the caret sat in
  // the field), unless the pointer is still over the island.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') release()
    }
    const onBlur = () => {
      setComposing(false)
      if (!hovering.current) setExpanded(false)
    }
    window.addEventListener('keydown', onKey)
    window.addEventListener('blur', onBlur)
    return () => {
      window.removeEventListener('keydown', onKey)
      window.removeEventListener('blur', onBlur)
    }
  }, [release])

  const clearTimer = (ref: typeof openTimer) => {
    if (ref.current != null) {
      window.clearTimeout(ref.current)
      ref.current = null
    }
  }
  const onEnter = () => {
    hovering.current = true
    void api.control({ type: 'hover', over: true })
    clearTimer(closeTimer)
    if (!expanded) openTimer.current = window.setTimeout(() => setExpanded(true), HOVER_OPEN_MS)
  }
  const onLeave = () => {
    hovering.current = false
    clearTimer(openTimer)
    if (!composing) closeTimer.current = window.setTimeout(() => setExpanded(false), HOVER_CLOSE_MS)
    // While the sheet is open the window keeps the mouse (a key may be
    // about to be clicked); it goes back to click-through when it folds.
    if (!expanded) void api.control({ type: 'hover', over: false })
  }
  useEffect(() => {
    if (!expanded && !hovering.current) void api.control({ type: 'hover', over: false })
    if (expanded) void api.control({ type: 'hover', over: true })
    // The field unmounts with the sheet, and an unmount fires no blur.
    if (!expanded) setComposing(false)
  }, [expanded, api])
  useEffect(() => () => {
    clearTimer(openTimer)
    clearTimer(closeTimer)
    clearTimer(dropTimer)
  }, [])

  // A new needs-you item: the shape nudges once and the sheet opens for a
  // moment, so allow or deny is one click away.
  useEffect(() => {
    const ids = (digest?.attention ?? []).map((item) => item.id)
    if (seenAttention.current === null) {
      seenAttention.current = new Set(ids)
      return
    }
    const fresh = ids.filter((id) => !seenAttention.current!.has(id))
    seenAttention.current = new Set(ids)
    if (fresh.length === 0) return
    setNudge((n) => n + 1)
    setExpanded(true)
    clearTimer(closeTimer)
    closeTimer.current = window.setTimeout(() => {
      if (!hovering.current && !composing) setExpanded(false)
    }, ATTENTION_PEEK_MS)
  }, [digest?.attention, composing])
  useEffect(() => {
    const el = islandRef.current
    if (!el || nudge === 0) return
    el.classList.remove('island-nudge')
    void el.offsetWidth
    el.classList.add('island-nudge')
  }, [nudge])

  // The shelf: files dropped on the island go to the composer's tray, text
  // goes into the field.
  const onDragOver = (e: React.DragEvent) => {
    e.preventDefault()
    e.dataTransfer.dropEffect = 'copy'
    setDropping(true)
    clearTimer(dropTimer)
    dropTimer.current = window.setTimeout(() => setDropping(false), DROP_MS)
  }
  const onDrop = async (e: React.DragEvent) => {
    e.preventDefault()
    clearTimer(dropTimer)
    setDropping(false)
    const files = Array.from(e.dataTransfer.files ?? [])
    const text = e.dataTransfer.getData('text/plain')
    if (files.length > 0) {
      const encoded = await Promise.all(
        files.map(
          (file) =>
            new Promise<{ name: string; mimeType: string; dataBase64: string }>((resolve, reject) => {
              const reader = new FileReader()
              reader.onload = () => resolve({ name: file.name, mimeType: file.type, dataBase64: String(reader.result).split(',')[1] ?? '' })
              reader.onerror = () => reject(reader.error)
              reader.readAsDataURL(file)
            }),
        ),
      ).catch(() => [])
      if (encoded.length > 0) act({ type: 'attach', files: encoded })
    } else if (text.trim()) {
      setDroppedText(text.trim())
      setExpanded(true)
      setWantKeyboard((n) => n + 1)
    }
  }

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
  // The strip's one live row: the followed session while it runs, else the only one.
  const followed = follow ? (live.find((row) => row.id === follow) ?? null) : null
  const focusRow = followed ?? (live.length === 1 ? live[0] : null)

  const style = {
    '--notch-w': `${geometry.notched ? geometry.notchWidth : 0}px`,
    '--strip-h': `${geometry.stripHeight}px`,
  } as CSSProperties

  return (
    <div className="island-root">
      <div
        ref={islandRef}
        className="island"
        data-mode={mode}
        data-expanded={expanded ? 'true' : 'false'}
        data-banner={banner ? 'true' : 'false'}
        data-notched={geometry.notched ? 'true' : 'false'}
        data-drop={dropping ? 'true' : 'false'}
        style={style}
        onMouseEnter={onEnter}
        onMouseLeave={onLeave}
        onDragOver={onDragOver}
        onDragLeave={() => setDropping(false)}
        onDrop={onDrop}
      >
        <div className="island-pane">
          <div
            className="island-strip"
            onClick={() => {
              if (expanded) release()
              else setExpanded(true)
            }}
            title={expanded ? 'fold' : undefined}
          >
            <div className="island-ear island-ear-l">
              <Leading mode={mode} voice={voice} banner={banner} bloomKey={done?.at ?? 0} live={live} focusRow={focusRow} />
            </div>
            <div className="island-notch" aria-hidden="true" />
            <div className="island-ear island-ear-r" aria-live="polite">
              <Trailing mode={mode} digest={digest} voice={voice} live={live} focusRow={focusRow} attention={attention} banner={banner} done={done} now={now} dropping={dropping} />
            </div>
          </div>
          <div className="island-body">
            {expanded && digest ? (
              <Sheet
                api={api}
                digest={digest}
                geometry={geometry}
                palette={palette}
                now={now}
                done={done}
                justDone={justDone}
                follow={follow}
                onFollow={setFollow}
                act={act}
                onComposing={setComposing}
                onFold={release}
                wantKeyboard={wantKeyboard}
                onKeyboardTaken={() => setWantKeyboard(0)}
                replyTo={replyTo}
                droppedText={droppedText}
                onDroppedTextTaken={() => setDroppedText('')}
                onFocus={() => void api.control({ type: 'focus' })}
              />
            ) : banner && done ? (
              <Banner
                done={done}
                onOpen={() => act({ type: 'open', sessionId: done.sessionId })}
                onReply={() => {
                  setReplyTo(done.sessionId)
                  setExpanded(true)
                  setWantKeyboard((n) => n + 1)
                }}
              />
            ) : null}
          </div>
        </div>
      </div>
    </div>
  )
}

// ── The strip ──────────────────────────────────────────────────────────
// Both ears have a fixed width per mode, so the shape never moves while the
// words inside it change: the detail runs in a fixed box that fades at its
// leading edge, like tape, with the newest characters at the right.

function Leading({ mode, voice, banner, bloomKey, live, focusRow }: { mode: Mode; voice: IslandVoice | null; banner: boolean; bloomKey: number; live: IslandLive[]; focusRow: IslandLive | null }) {
  if (banner) {
    return (
      <span className="island-mark island-swap" key={`bloom-${bloomKey}`}>
        <BloomMark key={bloomKey} />
      </span>
    )
  }
  if (mode === 'voice' && voice) {
    return (
      <span className="island-swap island-ear-text" key={`voice-${voice.state}`}>
        <VoiceSigil size={18} state={voice.state} level={voice.level} className="island-sigil" />
        <span className="island-ear-clip island-dim">{voiceWord(voice)}</span>
      </span>
    )
  }
  if (mode === 'attention') {
    return (
      <span className="island-swap island-ear-text" key="attention">
        <span className="island-mark">
          <TopographicMark size={18} />
        </span>
        <span className="island-tag island-tag-warn">needs you</span>
      </span>
    )
  }
  if (mode === 'live') {
    const others = focusRow ? live.length - 1 : 0
    return (
      <span className="island-swap island-ear-text" key={`live-${focusRow?.id ?? 'many'}-${live.length}`}>
        <span className="island-mark">
          <AnimatedTopographicMark size={18} intensity={0.55} className="island-mark-svg" />
        </span>
        {focusRow ? (
          <>
            <span className="island-ear-clip">{focusRow.title}</span>
            {others > 0 && <span className="island-tag island-tag-dim">+{others}</span>}
          </>
        ) : (
          <span className="island-ear-clip">{live.length} live</span>
        )}
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
  focusRow,
  attention,
  banner,
  done,
  now,
  dropping,
}: {
  mode: Mode
  digest: IslandDigest | null
  voice: IslandVoice | null
  live: IslandLive[]
  focusRow: IslandLive | null
  attention: IslandAttention[]
  banner: boolean
  done: IslandDone | null
  now: number
  dropping: boolean
}) {
  if (dropping) {
    return (
      <span className="island-swap island-ear-text" key="drop">
        <span className="island-tag island-tag-info">drop</span>
        <span className="island-ear-clip">to attach</span>
      </span>
    )
  }
  if (banner && done) {
    return (
      <span className="island-swap island-ear-text" key={`done-${done.at}`}>
        <Check ok={done.success} /> {done.success ? 'done' : 'stopped'}
        <span className="island-clock">{elapsed(done.durationMs)}</span>
      </span>
    )
  }
  if (mode === 'attention') {
    const first = attention[0]
    return (
      <span className="island-swap island-ear-text" key={`att-${first.id}`}>
        <Tape text={attentionLine(first)} />
        {attention.length > 1 && <span className="island-tag island-tag-dim">+{attention.length - 1}</span>}
      </span>
    )
  }
  if (mode === 'voice' && voice) {
    const line = voice.state === 'listening' ? voice.userLine : voice.state === 'speaking' ? voice.assistantLine : ''
    return (
      <span className="island-swap island-ear-text" key={`voice-${voice.state}`}>
        <Tape text={line} />
        <Meter level={voice.level} live={voice.state === 'listening'} />
      </span>
    )
  }
  if (mode === 'live') {
    if (focusRow) {
      const word = liveWord(focusRow)
      return (
        <span className="island-swap island-ear-text" key={`live-${focusRow.id}-${word}`}>
          <b className="island-word">{word}</b>
          <Tape text={liveDetail(focusRow)} />
          <Elapsed since={focusRow.startedAt} now={now} />
        </span>
      )
    }
    // Several live and none followed: the activity most of them share, and the oldest clock.
    const oldest = live.reduce((a, b) => (a.startedAt < b.startedAt ? a : b))
    const tools = live.filter((row) => row.tool && !row.thinking).length
    const thinking = live.filter((row) => row.thinking).length
    const writing = live.length - tools - thinking
    const top = [
      [writing, 'writing'],
      [tools, 'on tools'],
      [thinking, 'thinking'],
    ].sort((a, b) => (b[0] as number) - (a[0] as number))[0]
    return (
      <span className="island-swap island-ear-text" key={`multi-${top[1]}`}>
        <span className="island-ear-clip island-dim">
          {top[0] === live.length ? 'all' : top[0]} {top[1]}
        </span>
        <Elapsed since={oldest.startedAt} now={now} />
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

/** Text in a fixed box, newest characters at the right, the start fading out: the words change, the shape does not. */
function Tape({ text }: { text: string }) {
  return (
    <span className="island-tape" data-empty={text ? 'false' : 'true'}>
      <span>{text}</span>
    </span>
  )
}

function Banner({ done, onOpen, onReply }: { done: IslandDone; onOpen: () => void; onReply: () => void }) {
  return (
    <div
      className="island-banner"
      role="button"
      tabIndex={0}
      onClick={onOpen}
      onKeyDown={(e) => {
        if (e.key === 'Enter') onOpen()
      }}
      title="open in Freyja"
    >
      <span className="island-banner-title">{done.title}</span>
      <button
        type="button"
        className="island-key island-key-sm"
        onClick={(e) => {
          e.stopPropagation()
          onReply()
        }}
      >
        reply
      </button>
      {done.text && <span className="island-banner-text">{done.text}</span>}
    </div>
  )
}

/** The mark blooming out of nothing: the ear's moment when a turn ends. */
function BloomMark() {
  const progress = useRef(0)
  useEffect(() => {
    let raf = 0
    const start = performance.now()
    const tick = (t: number) => {
      const x = Math.min(1, (t - start) / 700)
      progress.current = 1 - Math.pow(1 - x, 3)
      if (x < 1) raf = requestAnimationFrame(tick)
    }
    raf = requestAnimationFrame(tick)
    return () => cancelAnimationFrame(raf)
  }, [])
  return <AnimatedTopographicMark size={18} intensity={0.25} expandProgressRef={progress} className="island-mark-svg" />
}

function voiceWord(voice: IslandVoice): ReactNode {
  switch (voice.state) {
    case 'listening':
      return 'listening'
    case 'thinking':
      return 'thinking'
    case 'acting':
      return 'doing it'
    case 'speaking':
      return 'speaking'
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
