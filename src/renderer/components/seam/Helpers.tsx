import { memo, useLayoutEffect, useMemo, useRef, useState } from 'react'
import { useShallow } from 'zustand/react/shallow'
import { useHarness } from '../../state/store'
import { renderMarkdown } from '../../lib/markdown'
import { strandIndex } from '../../lib/seam'
import { Fold } from '../Fold'
import { Seam } from './Seam'
import { Caret, FanMark } from './Steps'
import { formatShort } from './stepWords'
import { helperDigest, parseDigest, replyLine } from './helperDigest'
import { prefersReducedMotion, useNow } from './hooks'
import type { SubagentRecord } from '@shared/events'

type HelperState = 'running' | 'done' | 'failed'

/** A helper's state, trusting its finished session over its record. */
export function helperState(sub: SubagentRecord | undefined, snap: { completed?: boolean; success?: boolean } | undefined): HelperState {
  if (snap) {
    if (!snap.completed) return 'running'
    return snap.success === false ? 'failed' : 'done'
  }
  if (!sub) return 'running'
  if (sub.state === 'failed' || sub.state === 'cancelled') return 'failed'
  return sub.state === 'done' ? 'done' : 'running'
}

/**
 * Helpers (sub-agents) sent out together.
 *
 * Each hangs from the line on its own strand in a ribbon of fine parallel
 * threads. While it works the strand is basted, a light dashed thread, and
 * the helper grows its own small seam beside the step it is on. When it
 * comes back, a solid thread is sewn up from its line to the top, its
 * working line folds away with its seam reeled in, and its reply takes the
 * place of its task. A helper that failed is never sewn in: its strand
 * stays basted, in red, with a small cut across its branch.
 */
export const Helpers = memo(function Helpers({ ids, u }: { ids: string[]; u: number }) {
  const subs = useHarness(useShallow((s) => ids.map((id) => s.subagents[id])))
  const statesKey = useHarness((s) => ids.map((id) => helperState(s.subagents[id], s.sessions.find((x) => x.id === id))).join(','))
  const states = statesKey.split(',') as HelperState[]
  const n = ids.length
  const dense = n > 6
  const running = states.filter((x) => x === 'running').length
  const failed = states.filter((x) => x === 'failed').length
  const back = n - running - failed
  const now = useNow(running > 0)
  const wall = Math.max(0, ...subs.map((sub, j) => (sub ? (states[j] === 'running' ? Math.max(sub.elapsedMs, now - sub.startedAt) : sub.elapsedMs) : 0)))
  const rowsRef = useRef<HTMLDivElement>(null)
  const svgRef = useRef<SVGSVGElement>(null)
  const headRef = useRef<HTMLSpanElement>(null)

  // The ribbon is drawn from the rows' real positions after every layout,
  // and for a moment after a helper comes back, while its line folds away.
  useLayoutEffect(() => {
    const changed = drawRibbon(rowsRef.current, svgRef.current, headRef.current)
    if (changed) follow(rowsRef, svgRef, headRef)
  })

  return (
    <div className="wk-helpers" data-dense={dense ? 'true' : 'false'} data-state={running ? 'live' : 'done'}>
      <div className="wk-step-head wk-helpers-head">
        <span ref={headRef} className="wk-fanmark" data-u={u} aria-hidden>
          <FanMark states={states.map((x) => (x === 'running' ? 'run' : x === 'failed' ? 'err' : 'ok'))} />
        </span>
        <span className="wk-step-text">{n === 1 ? 'Send a helper' : `Send ${n} helpers`}</span>
        <span className="wk-step-meta">
          {running && back + failed < n ? (
            <span>
              {back + failed} of {n} back
            </span>
          ) : (
            <span>{back} back</span>
          )}
          {failed > 0 && <span className="wk-fail">{failed} blocked</span>}
          <span className="wk-time">{formatShort(wall)}</span>
        </span>
      </div>
      <div ref={rowsRef} className="wk-helpers-rows">
        <svg ref={svgRef} className="wk-ribbon" width="24" height="10" aria-hidden />
        {ids.map((id, j) => (
          <HelperRow key={id} id={id} sub={subs[j]} state={states[j]} u={u} s={strandIndex(j, n)} dense={dense} now={now} />
        ))}
      </div>
    </div>
  )
})

const HelperRow = memo(function HelperRow({
  id,
  sub,
  state,
  u,
  s,
  dense,
  now,
}: {
  id: string
  sub: SubagentRecord | undefined
  state: HelperState
  u: number
  s: number
  dense: boolean
  now: number
}) {
  const digest = useHarness((st) => helperDigest(st, id))
  const steps = useMemo(() => parseDigest(digest), [digest])
  const openSessionPane = useHarness((st) => st.openSessionPane)
  const [open, setOpen] = useState(false)
  if (!sub) return null
  const running = state === 'running'
  const failed = state === 'failed'
  const ms = running ? Math.max(sub.elapsedMs, now - sub.startedAt) : sub.elapsedMs
  const say = replyLine(sub.result, failed)
  const seam = (
    <span className="wk-helper-seam">
      <Seam units={steps.units} maxWidth={dense ? 84 : 170} fresh={running} />
    </span>
  )
  const step = (
    <span key={`s${steps.now}`} className="wk-helper-step">
      {steps.now || steps.steps[steps.steps.length - 1]?.text || (running ? 'Starting…' : '')}
    </span>
  )
  const reply = (
    <span key="say" className={`wk-helper-say${failed ? ' wk-bad' : ''}`}>
      {say}
    </span>
  )
  return (
    <div className="wk-helper" data-helper={id} data-state={state} data-open={open ? 'true' : 'false'}>
      <button type="button" className="wk-helper-head" aria-expanded={open} title={sub.task} onClick={() => setOpen(!open)}>
        <span className="wk-helper-mark" data-u={u} data-s={s} aria-hidden />
        <span className="wk-helper-name">{sub.label}</span>
        {dense ? (
          <>
            {running ? step : reply}
            {seam}
          </>
        ) : running ? (
          <span key="task" className="wk-helper-task">
            {sub.task}
          </span>
        ) : (
          reply
        )}
        <span className="wk-step-meta">
          <span className="wk-time">{formatShort(ms)}</span>
          <Caret />
        </span>
      </button>
      {!dense && (
        <div className="wk-helper-line2">
          <div>
            <div className="wk-helper-now">
              {seam}
              {step}
            </div>
          </div>
        </div>
      )}
      <Fold open={open} stagger={false}>
        <div className="wk-rule wk-reply">
          <div className="wk-reply-meta">
            {running ? `working · ${steps.steps.length} steps so far` : `${failed ? 'stopped' : 'sent back'} · ${formatShort(sub.elapsedMs)} · ${sub.toolsCalled} tools`}
          </div>
          {!running && sub.result && <Reply text={sub.result} />}
          {steps.steps.length > 0 && (
            <div className="wk-reply-steps">
              {steps.steps.slice(-12).map((st, i) => (
                <div key={i} className="wk-reply-step">
                  <span className={`wk-reply-mark wk-reply-mark--${st.kind}`} aria-hidden />
                  <span>{st.text}</span>
                </div>
              ))}
            </div>
          )}
          <button type="button" className="wk-reply-open" onClick={() => openSessionPane(id, 'split')}>
            open its session
          </button>
        </div>
      </Fold>
    </div>
  )
})

function Reply({ text }: { text: string }) {
  const html = useMemo(() => renderMarkdown(text.replace(/<\/?subagent-report[^>]*>/g, '')), [text])
  return (
    <div
      className="md selectable wk-reply-body"
      // eslint-disable-next-line react/no-danger
      dangerouslySetInnerHTML={{ __html: html }}
    />
  )
}

/* ── The ribbon ─────────────────────────────────────────────────────── */

const NS = 'http://www.w3.org/2000/svg'
const r2 = (n: number) => Math.round(n * 100) / 100

/** One strand: down the ribbon from the line's mark, one rounded turn,
 *  across to its helper. The strand for the top helper sits nearest the
 *  rows, so no two ever cross. */
function strandD(tx: number, hy: number, x1: number, y1: number, j: number, n: number): string {
  const g = n > 1 ? Math.min(1.7, 10 / (n - 1)) : 0
  const xj = tx + ((n - 1) / 2 - j) * g
  const r = 5
  const turn = Math.max(hy + 9, y1 - r)
  return `M${r2(tx)} ${r2(hy)}C${r2(tx)} ${r2(hy + 4)} ${r2(xj)} ${r2(hy + 4)} ${r2(xj)} ${r2(hy + 9)}V${r2(turn)}Q${r2(xj)} ${r2(y1)} ${r2(xj + r)} ${r2(y1)}H${r2(x1)}`
}

function strandEl(svg: SVGSVGElement, id: string, kind: string, cls: string): { el: SVGPathElement; fresh: boolean } {
  let el = svg.querySelector<SVGPathElement>(`[data-h="${id}"][data-k="${kind}"]`)
  if (el) return { el, fresh: false }
  el = document.createElementNS(NS, 'path') as SVGPathElement
  el.setAttribute('data-h', id)
  el.setAttribute('data-k', kind)
  el.setAttribute('class', cls)
  svg.append(el)
  return { el, fresh: true }
}

/** Draw or move every strand. Returns true when a helper changed state. */
function drawRibbon(rows: HTMLDivElement | null, svg: SVGSVGElement | null, head: HTMLElement | null): boolean {
  if (!rows || !svg || !head) return false
  const rr = rows.getBoundingClientRect()
  if (!rr.height) return false
  const still = prefersReducedMotion()
  const hr = head.getBoundingClientRect()
  const tx = hr.left + hr.width / 2 - rr.left
  const hy = hr.bottom - rr.top + 1
  svg.setAttribute('height', String(Math.ceil(rr.height)))
  const list = Array.from(rows.querySelectorAll<HTMLElement>(':scope > .wk-helper'))
  let changed = false
  list.forEach((row, j) => {
    const mark = row.querySelector('.wk-helper-mark')
    if (!mark) return
    const r = mark.getBoundingClientRect()
    const x1 = r.left - rr.left - 2
    const y1 = r.top + r.height / 2 - rr.top
    const d = strandD(tx, hy, x1, y1, j, list.length)
    const id = row.dataset.helper ?? String(j)
    const st = row.dataset.state ?? 'running'
    const baste = strandEl(svg, id, 'baste', 'wk-strand wk-strand--baste')
    const prev = baste.el.getAttribute('data-st')
    baste.el.setAttribute('d', d)
    if (prev !== st) baste.el.setAttribute('data-st', st)
    if (baste.fresh && !still && st === 'running' && typeof baste.el.animate === 'function') {
      // Basted down from the line when the helpers start.
      baste.el.animate([{ clipPath: 'inset(0 0 100% 0)' }, { clipPath: 'inset(0 0 0 0)' }], { duration: 520 + j * 110, easing: 'cubic-bezier(.3,.6,.25,1)' })
    }
    if (st === 'done') {
      const sewn = strandEl(svg, id, 'sewn', 'wk-strand wk-strand--sewn')
      sewn.el.setAttribute('d', d)
      if (sewn.fresh && !still && prev === 'running' && typeof sewn.el.animate === 'function') {
        // Sewn solid, from the helper's line up to the top.
        const L = sewn.el.getTotalLength()
        sewn.el.animate([{ strokeDasharray: `${L} ${L}`, strokeDashoffset: -L }, { strokeDasharray: `${L} ${L}`, strokeDashoffset: 0 }], { duration: 780, easing: 'cubic-bezier(.5,0,.2,1)' })
      }
    }
    if (st === 'failed') {
      const cut = strandEl(svg, id, 'cut', 'wk-strand-cut')
      cut.el.setAttribute('d', `M${r2(x1 - 6.4)} ${r2(y1 + 2.6)}L${r2(x1 - 3.8)} ${r2(y1 - 2.6)}`)
    }
    if (prev && prev !== st) changed = true
  })
  return changed
}

/** Keep the strands on their rows for a moment while a line folds away. */
function follow(rows: { current: HTMLDivElement | null }, svg: { current: SVGSVGElement | null }, head: { current: HTMLElement | null }) {
  const until = performance.now() + 1000
  const tick = (t: number) => {
    drawRibbon(rows.current, svg.current, head.current)
    if (t < until) requestAnimationFrame(tick)
  }
  requestAnimationFrame(tick)
}
