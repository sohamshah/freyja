import { memo, useContext, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react'
import type { CSSProperties, MouseEvent } from 'react'
import { useShallow } from 'zustand/react/shallow'
import { useHarness } from '../../state/store'
import { SearchQueryContext } from '../searchContext'
import { ThinkingStream } from '../ThinkingStream'
import { Helpers, helperState } from './Helpers'
import { Seam } from './Seam'
import { Caret, Crowd, DiffTag, StepGroup, StepLine } from './Steps'
import { formatShort, stretchDiff, stretchMs, stretchPhrase, stretchUnits } from './stepWords'
import type { HelperView, StretchData, StretchItem } from './stepWords'
import { captureMarks, gather, unravel } from './choreo'
import { prefersReducedMotion, useWidth } from './hooks'
import type { MessagePart, ToolCallRecord } from '@shared/events'

/**
 * A stretch of work: the thinking and tool calls between two paragraphs.
 *
 * While the agent works (`closed` false) its steps show as quiet lines.
 * When the agent starts its next paragraph, or the turn ends, the lines
 * gather into a seam: one drawn line beside a short summary. Click the
 * seam to unfold the lines under it; click again to fold them back.
 *
 * A stretch of nothing but thinking has no seam: the thought folds itself.
 */
export const WorkStretch = memo(function WorkStretch({
  items,
  closed,
  liveIndex,
}: {
  items: StretchItem[]
  /** A later paragraph exists, or the turn is over. */
  closed: boolean
  /** The message part index that is still being written, or -1. */
  liveIndex: number
}) {
  const query = useContext(SearchQueryContext)
  const callIds = useMemo(
    () => items.flatMap((it) => (it.kind === 'call' ? [it.id] : it.kind === 'calls' ? it.ids : [])),
    [items],
  )
  const helperIds = useMemo(() => items.flatMap((it) => (it.kind === 'helpers' ? it.ids : [])), [items])
  const plain = items.every((it) => it.kind === 'thought')
  const calls = useHarness(useShallow((s) => callIds.map((id) => s.toolCalls[id])))
  const subs = useHarness(useShallow((s) => helperIds.map((id) => s.subagents[id])))
  const helperStates = useHarness((s) => helperIds.map((id) => helperState(s.subagents[id], s.sessions.find((x) => x.id === id))).join(','))
  const focus = useHarness((s) => (s.focusedToolCallId && callIds.includes(s.focusedToolCallId) ? s.focusedToolCallSerial : 0))

  const [userOpen, setUserOpen] = useState(false)
  useEffect(() => {
    if (focus > 0) setUserOpen(true)
  }, [focus])
  const searching = query.trim().length > 0
  const mode: Mode = plain ? 'plain' : !closed ? 'live' : userOpen || searching ? 'open' : 'folded'

  // Helpers seen working in this view: when one comes back, its strand in
  // the seam is drawn in again (a helper that was already back when the
  // transcript opened just shows as back).
  const seenOut = useRef(new Set<string>())
  const data: StretchData = useMemo(() => {
    const out: StretchData = { calls: {}, helpers: {} }
    callIds.forEach((id, i) => { out.calls[id] = calls[i] })
    const states = helperStates.split(',')
    helperIds.forEach((id, i) => {
      const sub = subs[i]
      const state = (states[i] as HelperView['state']) || 'running'
      if (state === 'running') seenOut.current.add(id)
      if (sub) out.helpers[id] = { sub, state, back: state === 'done' && seenOut.current.has(id) }
    })
    return out
  }, [callIds, calls, helperIds, subs, helperStates])

  const rootRef = useRef<HTMLDivElement>(null)
  const layerRef = useRef<HTMLDivElement>(null)
  const tipRef = useRef<HTMLSpanElement>(null)
  const width = useWidth(rootRef)
  const maxWidth = width === 0 ? 260 : width < 420 ? 120 : width < 560 ? 160 : 260

  // The fold's motion. Runs after the DOM switched mode but before paint,
  // while the lines still sit where they were (their collapse is a CSS
  // transition that has not started moving yet).
  const prevMode = useRef(mode)
  const busy = useRef<(() => void) | null>(null)
  useLayoutEffect(() => {
    const prev = prevMode.current
    prevMode.current = mode
    const root = rootRef.current
    const layer = layerRef.current
    if (prev === mode || !root || !layer || prefersReducedMotion() || !inView(root)) return
    if (mode === 'folded' && (prev === 'live' || prev === 'open')) {
      busy.current?.()
      const marks = captureMarks(root)
      busy.current = gather(root, layer, marks, () => { busy.current = null })
    } else if (mode === 'open' && prev === 'folded') {
      busy.current?.()
      busy.current = unravel(root, layer, () => { busy.current = null })
    }
  }, [mode])
  useEffect(() => () => busy.current?.(), [])

  if (plain) {
    return (
      <div className="wk-stretch" data-mode="plain">
        <div className="wk-stretch-list">
          {items.map((it, i) => (
            <div key={itemKey(it, i)} className="wk-item">
              {it.kind === 'thought' && <Thought part={it.part} live={it.index === liveIndex} query={query} />}
            </div>
          ))}
        </div>
      </div>
    )
  }

  const now = Date.now()
  const units = stretchUnits(items, data, now)
  const phrase = stretchPhrase(items, data)
  const diff = stretchDiff(items, data)
  const ms = stretchMs(items, data, now)

  const toggle = () => {
    if (busy.current || mode === 'live' || searching) return
    hideTip()
    setUserOpen((v) => !v)
  }
  const hideTip = () => {
    const tip = tipRef.current
    if (tip) tip.dataset.show = 'false'
  }
  const clearHl = () => rootRef.current?.querySelectorAll('.wk-hl').forEach((el) => el.classList.remove('wk-hl'))
  const onOver = (e: MouseEvent) => {
    const root = rootRef.current
    if (!root) return
    clearHl()
    const target = e.target as Element
    const hit = target.closest?.('.wk-seam-hit') as SVGRectElement | null
    if (hit) {
      const i = hit.dataset.hit
      root.querySelectorAll(`.wk-seam-row g[data-u="${i}"]`).forEach((g) => g.classList.add('wk-hl'))
      if (mode === 'open') root.querySelector(`.wk-stretch-list > .wk-item[data-ui="${i}"]`)?.classList.add('wk-hl')
      const tip = tipRef.current
      const svg = hit.ownerSVGElement
      if (tip && svg) {
        tip.textContent = hit.dataset.label ?? ''
        const left = svg.getBoundingClientRect().left - root.getBoundingClientRect().left + Number(hit.dataset.cx)
        tip.style.left = `${Math.max(80, left)}px`
        tip.dataset.show = 'true'
      }
      return
    }
    hideTip()
    if (mode !== 'open') return
    const item = target.closest?.('.wk-stretch-list > .wk-item') as HTMLElement | null
    if (item) root.querySelectorAll(`.wk-seam-row g[data-u="${item.dataset.ui}"]`).forEach((g) => g.classList.add('wk-hl'))
  }

  return (
    <div
      ref={rootRef}
      className="wk-stretch"
      data-mode={mode}
      onMouseOver={onOver}
      onMouseLeave={() => {
        clearHl()
        hideTip()
      }}
    >
      <div ref={layerRef} className="wk-ghosts" aria-hidden />
      <button
        type="button"
        className="wk-seam-row"
        aria-expanded={mode === 'open'}
        aria-label={`${phrase.what}. Show the steps.`}
        tabIndex={mode === 'live' ? -1 : 0}
        onClick={toggle}
      >
        <Seam units={units} maxWidth={maxWidth} hits />
        <span className="wk-seam-txt">
          <span className="wk-seam-what">{phrase.what}</span>
          {phrase.trouble.length > 0 && (
            <span className="wk-seam-trouble">
              {phrase.trouble.map((t, i) => (
                <span key={t} className={t.endsWith('still working') ? '' : 'wk-bad'}>
                  {i > 0 && <span className="wk-sep">·</span>}
                  {t}
                </span>
              ))}
            </span>
          )}
          {ms > 0 && <span className="wk-seam-time">{formatShort(ms)}</span>}
        </span>
        {diff && (
          <span className="wk-seam-diff">
            <DiffTag add={diff.add} del={diff.del} />
          </span>
        )}
        <span className="wk-seam-caret">
          <Caret />
        </span>
        <span ref={tipRef} className="wk-seam-tip" role="tooltip" />
      </button>
      <div className="wk-stretch-fold">
        <div className="wk-stretch-fold-in">
          <div className="wk-stretch-list">
            {items.map((it, i) => (
              <div key={itemKey(it, i)} className="wk-item" data-ui={i} style={{ '--i': i } as CSSProperties}>
                <ItemView item={it} u={i} liveIndex={liveIndex} query={query} />
              </div>
            ))}
          </div>
        </div>
      </div>
    </div>
  )
})

type Mode = 'plain' | 'live' | 'folded' | 'open'

function ItemView({ item, u, liveIndex, query }: { item: StretchItem; u: number; liveIndex: number; query: string }) {
  if (item.kind === 'thought') return <Thought part={item.part} live={item.index === liveIndex} query={query} />
  if (item.kind === 'call') return <StepLine id={item.id} u={u} />
  if (item.kind === 'calls') return item.ids.length >= 5 ? <Crowd ids={item.ids} u={u} /> : <StepGroup ids={item.ids} u={u} />
  return <Helpers ids={item.ids} u={u} />
}

function Thought({ part, live, query }: { part: MessagePart; live: boolean; query: string }) {
  if (!part.text) return null
  return <ThinkingStream text={part.text} live={live} startedAt={part.startedAt} endedAt={part.endedAt} searchQuery={query} />
}

function itemKey(it: StretchItem, i: number): string {
  if (it.kind === 'thought') return `t${it.index}`
  if (it.kind === 'call') return it.id
  return `${it.kind}-${it.ids[0] ?? i}`
}

function inView(el: HTMLElement): boolean {
  const r = el.getBoundingClientRect()
  return r.bottom > -60 && r.top < window.innerHeight + 60
}

export type { ToolCallRecord }
