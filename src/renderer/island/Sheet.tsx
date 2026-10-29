import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react'
import type { JevPaletteDecision } from '../../shared/events'
import type {
  IslandAction,
  IslandAttention,
  IslandDigest,
  IslandDone,
  IslandGeometry,
  IslandLive,
  IslandPaletteIndex,
  IslandPaletteRow,
  IslandSessionRow,
  IslandTaskRow,
} from '../../shared/island'
import type { IslandApi } from './bridge'
import { stepSelection } from '../lib/listNavigation'
import { chooseWidget, parseSlots, widgetForDomain, type PaletteWidgetType } from '../lib/paletteIntent'
import { Intent, intentPlan, type IntentCandidate, type IntentProps, type IntentType } from './Intent'
import { Check, Chevron, Elapsed, ago, clock, elapsed, groupLive, liveDetail, liveWord, type LiveGroup } from './parts'

/**
 * The sheet that drops below the strip: ⌘K in the notch. One field at the
 * top replies to a session, searches the palette's rows, or asks Jev what a
 * sentence means; under it, what is happening now (or the matches), and the
 * keys at the foot. Keyboard: ↑↓ move through the rows, ↵ acts on the
 * selected one, ⌘↵ does it in Freyja (or cuts a reply in), Esc clears the
 * field and then folds the island.
 */
export interface SheetProps {
  api: IslandApi
  digest: IslandDigest
  geometry: IslandGeometry
  palette: IslandPaletteIndex | null
  now: number
  done: IslandDone | null
  justDone: { id: string; title: string; at: number } | null
  /** The session the strip follows, if any. */
  follow: string | null
  onFollow: (id: string | null) => void
  act: (action: IslandAction) => void
  onComposing: (on: boolean) => void
  /** Fold the island and hand the keyboard back (after an action). */
  onFold: () => void
  wantKeyboard: number
  onKeyboardTaken: () => void
  replyTo: string | null
  droppedText: string
  onDroppedTextTaken: () => void
  onFocus: () => void
}

/** Rows per palette group the island lists; it is a small pane. */
const ROWS_PER_GROUP: Record<IslandPaletteRow['group'], number> = { Command: 8, Session: 8, Subagent: 6, Skill: 6 }
const GROUP_ORDER: IslandPaletteRow['group'][] = ['Command', 'Session', 'Subagent', 'Skill']
const JEV_DEBOUNCE_MS = 160
const JEV_MIN_CHARS = 3

/** Which Jev sub-targets belong to which domain (a sub-target is only shown with its domain). */
export function Sheet(p: SheetProps) {
  const { api, digest, geometry, palette, now, done, justDone, follow, onFollow, act, onComposing, onFold, wantKeyboard, onKeyboardTaken, replyTo, droppedText, onDroppedTextTaken, onFocus } = p
  const { attention, live, tasks, voice, sessions, active, appshotLabel, schedule, costToday } = digest
  const [query, setQuery] = useState('')
  const [chosen, setChosen] = useState<string | null>(replyTo)
  /** The keyboard's row: a key the rows carry as data-nav-key, or null for the field alone. */
  const [sel, setSel] = useState<string | null>(null)
  const [openRows, setOpenRows] = useState<Set<string>>(() => new Set())
  const [foldedGroups, setFoldedGroups] = useState<Set<string>>(() => new Set())
  const inputRef = useRef<HTMLInputElement>(null)
  const scrollRef = useRef<HTMLDivElement>(null)

  // ── The field's target ───────────────────────────────────────────────
  useEffect(() => {
    if (replyTo) setChosen(replyTo)
  }, [replyTo])
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

  // ── The keyboard ─────────────────────────────────────────────────────
  // The hotkey (or a reply key) opened the sheet: take the keyboard and put
  // the caret in the field. The counter is reset once taken, which re-runs
  // this effect, so the timer must not be cleared on that re-run: the
  // second focus lands once the panel has become the key window.
  useEffect(() => {
    if (wantKeyboard === 0) return
    onFocus()
    inputRef.current?.focus()
    window.setTimeout(() => inputRef.current?.focus(), 60)
    onKeyboardTaken()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [wantKeyboard])
  useEffect(() => {
    if (!droppedText) return
    setQuery((cur) => (cur ? `${cur} ${droppedText}` : droppedText))
    onComposing(true)
    onDroppedTextTaken()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [droppedText])

  // ── The palette's rows ───────────────────────────────────────────────
  // Asked for once per open; the rows are titles only, the actions stay in Freyja.
  useEffect(() => {
    act({ type: 'palette-index' })
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])
  const trimmed = query.trim()
  const q = trimmed.toLowerCase()
  const matches = useMemo(() => {
    if (!q) return [] as Array<{ group: IslandPaletteRow['group']; rows: IslandPaletteRow[] }>
    const rows = (palette?.rows ?? []).filter(
      (r) => r.title.toLowerCase().includes(q) || (r.subtitle ?? '').toLowerCase().includes(q) || r.group.toLowerCase().includes(q),
    )
    return GROUP_ORDER.map((group) => ({ group, rows: rows.filter((r) => r.group === group).slice(0, ROWS_PER_GROUP[group]) })).filter((g) => g.rows.length > 0)
  }, [palette, q])
  /** A row whose title starts with the text is what the person is reaching for: it wins, no Jev, no card. */
  const reached = useMemo(() => {
    if (!q || q.length > 28) return null
    for (const g of matches) for (const r of g.rows) if (r.title.toLowerCase().startsWith(q)) return r
    return null
  }, [matches, q])

  // ── Jev: what a sentence means ───────────────────────────────────────
  // Slots are parsed on every keystroke; Jev names the domain, debounced,
  // and a stale answer never overrides a newer query. The card already
  // showing stays while the sentence grows (hysteresis), as in ⌘K.
  const slots = useMemo(() => parseSlots(trimmed), [trimmed])
  const [decision, setDecision] = useState<JevPaletteDecision | null>(null)
  const [inferring, setInferring] = useState(false)
  const [pinned, setPinned] = useState<string | null>(null)
  const jevSeq = useRef(0)
  const lastWidget = useRef<{ type: PaletteWidgetType; query: string }>({ type: null, query: '' })
  useEffect(() => {
    if (trimmed.length < JEV_MIN_CHARS || trimmed.startsWith('/') || reached) {
      jevSeq.current += 1
      setDecision(null)
      setInferring(false)
      return
    }
    const seq = ++jevSeq.current
    setInferring(true)
    const timer = window.setTimeout(() => {
      api
        .jevClassify(trimmed)
        .then((d) => {
          if (seq !== jevSeq.current) return
          setDecision(d.ok ? d : null)
          setInferring(false)
        })
        .catch(() => {
          if (seq === jevSeq.current) setInferring(false)
        })
    }, JEV_DEBOUNCE_MS)
    return () => window.clearTimeout(timer)
  }, [api, trimmed, reached])
  useEffect(() => {
    if (trimmed.length < JEV_MIN_CHARS) setPinned(null)
  }, [trimmed])
  const widgetType: PaletteWidgetType = useMemo(() => {
    if (pinned && trimmed.length >= JEV_MIN_CHARS) return widgetForDomain(pinned, trimmed, slots)
    if (trimmed.length < JEV_MIN_CHARS || reached) return null
    const keep = lastWidget.current.type && trimmed.startsWith(lastWidget.current.query) ? lastWidget.current.type : null
    if (decision) {
      const chosenType = chooseWidget(decision.domain, decision.subTarget, decision.confidence, decision.readiness, trimmed, slots)
      if (chosenType) return chosenType
      const stale = decision.query !== trimmed
      if (stale) return keep
      if (decision.confidence >= 0.6) return null
      return keep
    }
    return keep
  }, [decision, trimmed, slots, reached, pinned])
  useEffect(() => {
    if (widgetType) lastWidget.current = { type: widgetType, query: decision?.query ?? trimmed }
    else if (trimmed.length < JEV_MIN_CHARS) lastWidget.current = { type: null, query: '' }
  }, [widgetType, decision, trimmed])
  const shownDomain = useMemo(() => {
    if (pinned) return pinned
    if (!widgetType) return null
    const probs = decision?.probabilities
    if (probs) {
      const d = Object.keys(probs).find((domain) => widgetForDomain(domain, trimmed, slots) === widgetType)
      if (d) return d
    }
    return decision?.domain ?? 'palette_command'
  }, [pinned, widgetType, decision, trimmed, slots])
  const also: IntentCandidate[] = useMemo(() => {
    if (!decision?.probabilities || !widgetType || !shownDomain) return []
    const probs = decision.probabilities
    const leadP = probs[shownDomain] ?? decision.confidence
    return Object.entries(probs)
      .filter(([d]) => d !== 'palette_command' && d !== shownDomain)
      .map(([domain, p]) => ({ domain, p }))
      .filter((c) => !!widgetForDomain(c.domain, trimmed, slots) && c.p >= 0.12 && leadP - c.p < 0.55)
      .sort((a, b) => b.p - a.p)
      .slice(0, 3)
  }, [decision, widgetType, shownDomain, trimmed, slots])
  const confidence = decision ? ((shownDomain ? decision.probabilities?.[shownDomain] : undefined) ?? decision.confidence) : 0

  // ── Selection ────────────────────────────────────────────────────────
  const navKeys = useCallback((): string[] => {
    const root = scrollRef.current
    if (!root) return []
    return Array.from(root.querySelectorAll<HTMLElement>('[data-nav-key]')).map((el) => el.dataset.navKey!)
  }, [])
  // A new query starts over at what the person is most likely reaching for;
  // an empty field selects nothing, so ↵ sends and ↓ enters the list.
  useLayoutEffect(() => {
    if (!q) {
      setSel(null)
      return
    }
    setSel(reached ? `row:${reached.id}` : widgetType ? 'card' : 'send')
  }, [q, reached, widgetType])
  useLayoutEffect(() => {
    if (!sel) return
    const el = scrollRef.current?.querySelector<HTMLElement>(`[data-nav-key="${CSS.escape(sel)}"]`)
    el?.scrollIntoView({ block: 'nearest' })
  }, [sel, matches, widgetType])

  // ── Acting ───────────────────────────────────────────────────────────
  const send = (cutIn: boolean) => {
    const body = query.trim()
    if (!body || !target) return
    act({ type: 'send', sessionId: target.id, text: body, cutIn })
    setQuery('')
    onComposing(false)
    onFold()
  }
  const runRow = (id: string) => {
    act({ type: 'palette-run', id })
    setQuery('')
    onFold()
  }
  const open = (sessionId: string) => act({ type: 'open', sessionId })
  const toggleRow = (id: string, want?: boolean) =>
    setOpenRows((cur) => {
      const next = new Set(cur)
      const on = want ?? !next.has(id)
      if (on) next.add(id)
      else next.delete(id)
      return next
    })
  const toggleGroup = (id: string) =>
    setFoldedGroups((cur) => {
      const next = new Set(cur)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })
  const reply = (sessionId: string) => {
    setChosen(sessionId)
    setQuery('')
    onFocus()
    inputRef.current?.focus()
  }
  const groups = useMemo(() => groupLive(live), [live])
  const stopAll = () => {
    for (const g of groups) {
      if (g.root) act({ type: 'stop', sessionId: g.root.id, scope: 'turn' })
      if (g.children.length > 0) act({ type: 'stop', sessionId: g.id, scope: 'subagents' })
    }
  }
  const activate = (inFreyja: boolean) => {
    const key = sel
    if (!key) {
      if (trimmed) send(inFreyja)
      return
    }
    if (key === 'send') return send(inFreyja)
    if (key === 'card') {
      if (inFreyja) {
        act({ type: 'palette-open', query: trimmed })
        onFold()
        return
      }
      scrollRef.current?.querySelector<HTMLButtonElement>('.island-intent [data-primary]:not(:disabled)')?.click()
      return
    }
    const [kind, id] = [key.slice(0, key.indexOf(':')), key.slice(key.indexOf(':') + 1)]
    switch (kind) {
      case 'row':
        return runRow(id)
      case 'live':
        if (inFreyja) return open(id)
        return toggleRow(id)
      case 'group':
        return toggleGroup(id)
      case 'done':
      case 'recent':
        return open(id)
      case 'task':
        return act({ type: 'dashboard', tab: 'tasks' })
    }
  }
  const onKeyDown = (e: React.KeyboardEvent<HTMLInputElement>) => {
    const keys = navKeys()
    const idx = sel ? keys.indexOf(sel) : -1
    if (e.key === 'ArrowUp' && idx === 0 && !q) {
      e.preventDefault()
      setSel(null)
      return
    }
    const next = stepSelection(e, { selected: idx, count: keys.length, wrap: false })
    if (next != null) {
      e.preventDefault()
      setSel(keys[next] ?? null)
      return
    }
    if (e.key === 'Enter') {
      e.preventDefault()
      activate(e.metaKey || e.ctrlKey)
      return
    }
    if (e.key === 'Escape' && query) {
      // The first Esc clears the field; the island's own listener folds on the next.
      e.preventDefault()
      e.stopPropagation()
      setQuery('')
      setPinned(null)
      onComposing(false)
      return
    }
    if (!q && sel?.startsWith('live:') && (e.key === 'ArrowRight' || e.key === 'ArrowLeft')) {
      e.preventDefault()
      toggleRow(sel.slice(5), e.key === 'ArrowRight')
    }
  }

  // ── Scroll fades ─────────────────────────────────────────────────────
  useEffect(() => {
    const el = scrollRef.current
    if (!el) return
    const update = () => {
      el.dataset.top = el.scrollTop > 2 ? 'true' : 'false'
      el.dataset.bottom = el.scrollHeight - el.scrollTop - el.clientHeight > 2 ? 'true' : 'false'
    }
    update()
    el.addEventListener('scroll', update, { passive: true })
    const sizes = new ResizeObserver(update)
    sizes.observe(el)
    const content = el.firstElementChild
    if (content) sizes.observe(content)
    return () => {
      el.removeEventListener('scroll', update)
      sizes.disconnect()
    }
  }, [])

  const quiet = live.length === 0 && attention.length === 0 && !voice.active
  const placeholder = !target ? 'search, or ask…' : `reply to ${target.title} · search · ask`
  const intentProps: IntentProps | null =
    widgetType && shownDomain
      ? {
          type: widgetType as IntentType,
          domain: shownDomain,
          query: trimmed,
          slots,
          confidence,
          inferring,
          also,
          onPin: (d) => {
            setPinned(d)
            inputRef.current?.focus()
          },
          target,
          selected: sel === 'card',
          act,
          onDone: () => {
            setQuery('')
            onFold()
          },
        }
      : null
  const cardVerb = intentProps ? (intentPlan(intentProps).primary?.label ?? 'open in Freyja') : ''
  const verb = !sel
    ? trimmed
      ? 'send'
      : ''
    : sel === 'send'
      ? 'send'
      : sel === 'card'
        ? cardVerb
        : sel.startsWith('row:')
          ? 'run'
          : sel.startsWith('live:')
            ? openRows.has(sel.slice(5))
              ? 'fold'
              : 'more'
            : sel.startsWith('group:')
              ? 'fold'
              : 'open'

  return (
    <div className="island-sheet">
      <form
        className={`island-field ${options.length > 1 ? '' : 'island-field-single'}`}
        onSubmit={(e) => {
          e.preventDefault()
          activate(false)
        }}
      >
        {options.length > 1 && (
          <select className="island-target" value={target?.id ?? ''} onChange={(e) => setChosen(e.target.value)} title="which session the reply goes to">
            {options.map((row) => (
              <option key={row.id} value={row.id}>
                {row.streaming ? '● ' : ''}
                {row.title}
              </option>
            ))}
          </select>
        )}
        <input
          ref={inputRef}
          className="island-input"
          value={query}
          placeholder={placeholder}
          spellCheck={false}
          autoComplete="off"
          onChange={(e) => {
            setQuery(e.target.value)
            onComposing(true)
          }}
          onFocus={() => {
            onComposing(true)
            onFocus()
          }}
          onBlur={() => onComposing(query.trim().length > 0)}
          onKeyDown={onKeyDown}
        />
        {verb && (
          <span className="island-field-hint">
            <kbd>↵</kbd> {verb}
          </span>
        )}
      </form>
      <div className="island-scroll" ref={scrollRef}>
        <div className="island-scroll-in">
          {q ? (
            <PaletteView
              query={trimmed}
              target={target}
              matches={matches}
              reached={reached}
              sel={sel}
              onSel={setSel}
              onRun={runRow}
              onSend={send}
              card={intentProps ? <Intent {...intentProps} /> : null}
              inferring={inferring && !widgetType}
            />
          ) : (
            <NowView
              attention={attention}
              groups={groups}
              liveCount={live.length}
              done={done}
              tasks={tasks}
              justDone={justDone}
              recent={quiet ? sessions : []}
              now={now}
              sel={sel}
              onSel={setSel}
              openRows={openRows}
              foldedGroups={foldedGroups}
              follow={follow}
              onFollow={onFollow}
              onToggleRow={toggleRow}
              onToggleGroup={toggleGroup}
              onReply={reply}
              onOpen={open}
              onStopAll={stopAll}
              act={act}
            />
          )}
        </div>
      </div>
      <div className="island-foot">
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
          <button type="button" className="island-key" title="the task board" onClick={() => act({ type: 'dashboard', tab: 'tasks' })}>
            board
          </button>
          <button type="button" className="island-key island-key-end" onClick={() => act({ type: 'focus-app' })}>
            open Freyja
          </button>
        </div>
        <div className="island-readout">
          {costToday > 0 && <span>${costToday.toFixed(2)} today</span>}
          {schedule && (
            <span className="island-readout-next" title={schedule.name}>
              next · {schedule.name} · {clock(schedule.at)}
            </span>
          )}
          <span className="island-legend">
            {geometry.hotkey && <kbd>{geometry.hotkey}</kbd>}
            <kbd>↑↓</kbd>
            <kbd>↵</kbd>
            <kbd>esc</kbd>
          </span>
        </div>
      </div>
    </div>
  )
}

// ── The matches ─────────────────────────────────────────────────────────

function PaletteView({
  query,
  target,
  matches,
  reached,
  sel,
  onSel,
  onRun,
  onSend,
  card,
  inferring,
}: {
  query: string
  target: IslandSessionRow | null
  matches: Array<{ group: IslandPaletteRow['group']; rows: IslandPaletteRow[] }>
  reached: IslandPaletteRow | null
  sel: string | null
  onSel: (key: string) => void
  onRun: (id: string) => void
  onSend: (cutIn: boolean) => void
  card: React.ReactNode
  inferring: boolean
}) {
  const sendRow = (
    <button
      type="button"
      className="island-row island-row-send"
      data-nav-key="send"
      data-selected={sel === 'send' ? 'true' : 'false'}
      onMouseEnter={() => onSel('send')}
      onClick={() => onSend(false)}
      disabled={!target}
      title="⌘↵ cuts into the running turn"
    >
      <span className="island-row-kind">›</span>
      <span className="island-row-title">
        {target ? (
          <>
            send to <b>{target.title}</b>
          </>
        ) : (
          'no session to send to'
        )}
      </span>
      <span className="island-row-status">{target?.streaming ? 'after the turn · ⌘↵ cuts in' : ''}</span>
    </button>
  )
  return (
    <>
      {card}
      {!card && inferring && <div className="island-inferring">jev is reading it…</div>}
      {!reached && sendRow}
      {matches.map((g) => (
        <section className="island-section" key={g.group}>
          <h3 className="island-h">{g.group === 'Subagent' ? 'helpers' : g.group.toLowerCase() + 's'}</h3>
          {g.rows.map((row) => {
            const key = `row:${row.id}`
            return (
              <button
                key={row.id}
                type="button"
                className="island-row"
                data-nav-key={key}
                data-selected={sel === key ? 'true' : 'false'}
                onMouseEnter={() => onSel(key)}
                onClick={() => onRun(row.id)}
              >
                <span className="island-row-kind">{row.group === 'Session' ? '●' : row.group === 'Subagent' ? '↳' : row.group === 'Skill' ? '◇' : '›'}</span>
                <span className="island-row-title">{row.title}</span>
                {row.subtitle && <span className="island-row-status island-row-sub">{row.subtitle}</span>}
              </button>
            )
          })}
        </section>
      ))}
      {reached && sendRow}
      {matches.length === 0 && !card && !inferring && query.length >= 2 && <div className="island-empty">no command, session or skill matches</div>}
    </>
  )
}

// ── What is happening now ───────────────────────────────────────────────

function NowView({
  attention,
  groups,
  liveCount,
  done,
  tasks,
  justDone,
  recent,
  now,
  sel,
  onSel,
  openRows,
  foldedGroups,
  follow,
  onFollow,
  onToggleRow,
  onToggleGroup,
  onReply,
  onOpen,
  onStopAll,
  act,
}: {
  attention: IslandAttention[]
  groups: LiveGroup[]
  liveCount: number
  done: IslandDone | null
  tasks: IslandDigest['tasks']
  justDone: { id: string; title: string; at: number } | null
  recent: IslandSessionRow[]
  now: number
  sel: string | null
  onSel: (key: string) => void
  openRows: Set<string>
  foldedGroups: Set<string>
  follow: string | null
  onFollow: (id: string | null) => void
  onToggleRow: (id: string, want?: boolean) => void
  onToggleGroup: (id: string) => void
  onReply: (sessionId: string) => void
  onOpen: (sessionId: string) => void
  onStopAll: () => void
  act: (action: IslandAction) => void
}) {
  const rowProps = (row: IslandLive, depth: number) => ({
    row,
    depth,
    now,
    open: openRows.has(row.id),
    selected: sel === `live:${row.id}`,
    following: follow === row.id,
    onSel,
    onToggle: onToggleRow,
    onFollow,
    onReply,
    onOpen,
    act,
  })
  const nothing = attention.length === 0 && liveCount === 0 && !done && tasks.latest.length === 0 && recent.length === 0
  return (
    <>
      {attention.map((item) => (
        <AttentionCard key={item.id} item={item} act={act} />
      ))}
      {liveCount > 0 && (
        <section className="island-section">
          <h3 className="island-h">
            live
            <span className="island-h-count">{liveCount}</span>
            {liveCount > 1 && (
              <button type="button" className="island-h-key island-h-key-stop" onClick={onStopAll} title="stop every turn and helper listed here">
                stop all
              </button>
            )}
          </h3>
          {groups.map((g) => {
            const folded = foldedGroups.has(g.id)
            return (
              <div className="island-group" key={g.id}>
                {g.root ? (
                  <LiveRow {...rowProps(g.root, 0)} />
                ) : (
                  <GroupRow group={g} folded={folded} selected={sel === `group:${g.id}`} onSel={onSel} onToggle={() => onToggleGroup(g.id)} now={now} act={act} />
                )}
                {!folded && g.children.map((row) => <LiveRow key={row.id} {...rowProps(row, 1)} />)}
              </div>
            )
          })}
        </section>
      )}
      {done && (
        <section className="island-section">
          <h3 className="island-h">just finished</h3>
          <button
            type="button"
            className="island-row island-row-done"
            data-nav-key="done"
            data-selected={sel === 'done' ? 'true' : 'false'}
            onMouseEnter={() => onSel('done')}
            onClick={() => onOpen(done.sessionId)}
            title="open in Freyja"
          >
            <span className="island-row-kind">
              <Check ok={done.success} />
            </span>
            <span className="island-row-title">{done.title}</span>
            <span className="island-row-status">{done.success ? 'done' : 'stopped'}</span>
            <span className="island-clock">{elapsed(done.durationMs)}</span>
            {done.text && <span className="island-row-peek island-row-peek-wrap">{done.text}</span>}
          </button>
        </section>
      )}
      {tasks.latest.length > 0 && (
        <section className="island-section">
          <h3 className="island-h">
            tasks
            <span className="island-counts">
              <span className="island-glyph-now">◐</span> {tasks.now} · <span className="island-glyph-next">○</span> {tasks.next} ·{' '}
              <span className="island-glyph-blocked">⊘</span> {tasks.blocked} · <span className="island-glyph-done">✓</span> {tasks.done}
            </span>
          </h3>
          {tasks.latest.map((task) => (
            <TaskRow key={task.id} task={task} stamped={justDone?.id === task.id} selected={sel === `task:${task.id}`} onSel={onSel} act={act} />
          ))}
        </section>
      )}
      {recent.length > 0 && (
        <section className="island-section">
          <h3 className="island-h">recent</h3>
          {recent.map((row) => {
            const key = `recent:${row.id}`
            return (
              <button
                key={row.id}
                type="button"
                className="island-row"
                data-nav-key={key}
                data-selected={sel === key ? 'true' : 'false'}
                onMouseEnter={() => onSel(key)}
                onClick={() => onOpen(row.id)}
                title="open in Freyja"
              >
                <span className="island-row-kind island-row-kind-root">●</span>
                <span className="island-row-title">{row.title}</span>
                <span className="island-row-status">{ago(row.updatedAt, now)}</span>
              </button>
            )
          })}
        </section>
      )}
      {nothing && <div className="island-empty island-empty-quiet">nothing running · type to reply, search or ask</div>}
    </>
  )
}

function GroupRow({
  group,
  folded,
  selected,
  onSel,
  onToggle,
  now,
  act,
}: {
  group: LiveGroup
  folded: boolean
  selected: boolean
  onSel: (key: string) => void
  onToggle: () => void
  now: number
  act: (action: IslandAction) => void
}) {
  const oldest = group.children.reduce((a, b) => (a.startedAt < b.startedAt ? a : b))
  const key = `group:${group.id}`
  return (
    <div className="island-row island-row-group" data-selected={selected ? 'true' : 'false'}>
      <button type="button" className="island-row-main" data-nav-key={key} onMouseEnter={() => onSel(key)} onClick={onToggle} title={folded ? 'show the helpers' : 'fold the helpers'}>
        <span className="island-row-kind">
          <Chevron open={!folded} />
        </span>
        <span className="island-row-title">{group.title}</span>
        <span className="island-row-status">
          {group.children.length} helper{group.children.length === 1 ? '' : 's'}
        </span>
        <Elapsed since={oldest.startedAt} now={now} />
      </button>
      <button type="button" className="island-key island-key-xs island-key-stop" onClick={() => act({ type: 'stop', sessionId: group.id, scope: 'subagents' })} title="stop every helper under this session">
        stop
      </button>
    </div>
  )
}

function LiveRow({
  row,
  depth,
  now,
  open,
  selected,
  following,
  onSel,
  onToggle,
  onFollow,
  onReply,
  onOpen,
  act,
}: {
  row: IslandLive
  depth: number
  now: number
  open: boolean
  selected: boolean
  following: boolean
  onSel: (key: string) => void
  onToggle: (id: string, want?: boolean) => void
  onFollow: (id: string | null) => void
  onReply: (sessionId: string) => void
  onOpen: (sessionId: string) => void
  act: (action: IslandAction) => void
}) {
  const key = `live:${row.id}`
  const word = liveWord(row)
  const detail = liveDetail(row)
  return (
    <div className={`island-row island-row-live ${open ? 'island-row-open' : ''}`} data-depth={depth} data-selected={selected ? 'true' : 'false'} data-following={following ? 'true' : 'false'}>
      <button
        type="button"
        className="island-row-main"
        data-nav-key={key}
        onMouseEnter={() => onSel(key)}
        onClick={() => onToggle(row.id)}
        onDoubleClick={() => onOpen(row.id)}
        title={open ? 'fold · double-click opens in Freyja' : 'more · double-click opens in Freyja'}
      >
        <span className={`island-row-kind island-row-kind-${row.kind}`}>{row.kind === 'sub' ? '↳' : row.kind === 'voice' ? '◉' : '●'}</span>
        <span className="island-row-title">
          {row.title}
          {following && <span className="island-tag island-tag-dim">following</span>}
        </span>
        <span className="island-row-status">
          <b>{word}</b>
          {detail && !open ? <span className="island-row-detail"> {detail}</span> : null}
        </span>
        <Elapsed since={row.startedAt} now={now} />
        <Chevron open={open} />
      </button>
      {open && (
        <div className="island-row-more">
          {row.tool?.summary && (
            <div className="island-row-arg">
              <span className="island-row-arg-tool">{word}</span> {row.tool.summary}
            </div>
          )}
          {row.text ? <div className="island-row-text">{row.text}</div> : !row.tool && <div className="island-row-text island-dim">{row.thinking ? 'thinking, nothing written yet' : 'nothing written yet'}</div>}
          <div className="island-row-keys">
            <button type="button" className="island-key island-key-sm" onClick={() => onReply(row.id)} title="aim the field at this session">
              reply
            </button>
            <button type="button" className="island-key island-key-sm" onClick={() => onFollow(following ? null : row.id)} title="the strip shows this session's progress">
              {following ? 'unfollow' : 'follow'}
            </button>
            <button type="button" className="island-key island-key-sm island-key-stop" onClick={() => act({ type: 'stop', sessionId: row.id, scope: 'turn' })} title="stop this turn">
              stop
            </button>
            <button type="button" className="island-key island-key-sm island-key-end" onClick={() => onOpen(row.id)}>
              open in Freyja
            </button>
          </div>
        </div>
      )}
    </div>
  )
}

function TaskRow({ task, stamped, selected, onSel, act }: { task: IslandTaskRow; stamped: boolean; selected: boolean; onSel: (key: string) => void; act: (action: IslandAction) => void }) {
  const glyph = task.bucket === 'now' ? '◐' : task.bucket === 'next' ? '○' : task.bucket === 'blocked' ? '⊘' : '✓'
  const key = `task:${task.id}`
  return (
    <button
      type="button"
      className="island-row island-row-task"
      data-nav-key={key}
      data-selected={selected ? 'true' : 'false'}
      onMouseEnter={() => onSel(key)}
      onClick={() => act({ type: 'dashboard', tab: 'tasks' })}
      title="open the board"
    >
      <span className={`island-row-kind island-glyph-${task.bucket}`}>{glyph}</span>
      <span className={`island-row-title ${task.bucket === 'done' ? 'island-dim' : ''}`}>{task.title}</span>
      <span className="island-row-status">{task.bucket === 'next' ? 'up next' : task.bucket}</span>
      {stamped && (
        <span className="island-stamp-slot">
          <span className="ticket-stamp ticket-stamp-ok island-stamp">done</span>
        </span>
      )}
    </button>
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
