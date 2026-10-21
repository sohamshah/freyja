import { memo, useContext, useEffect, useState } from 'react'
import type { CSSProperties, ReactNode } from 'react'
import { useHarness } from '../../state/store'
import { resolveToolCall } from '../../lib/toolCall'
import { useFrameObjectUrl } from '../../lib/frameMedia'
import { Fold } from '../Fold'
import { FileChangeCard } from '../FileChangeCard'
import { ToolResultImages } from '../ToolCallChip'
import { HighlightText, SearchQueryContext } from '../searchContext'
import { callMs, formatShort, liveCommand, shortWords, stepWords, toolName } from './stepWords'
import type { Words } from './stepWords'
import { useNow } from './hooks'
import { strandIndex } from '../../lib/seam'
import type { ToolCallRecord } from '@shared/events'
import { callsOf, useWorkView } from './source'

/* ── A: one tool call, one quiet line ──────────────────────────────────
   A small ring stands where thinking has a diamond, then the step in plain
   words. No box, no tool name, no colour. While it runs the ring turns and
   the words shimmer; a running command shows faintly underneath. Click to
   open the command and what came back under a hairline rule. */

/** A call's record: the active session's store, or a pane's own slice. */
export function useCall(id: string, record?: ToolCallRecord): ToolCallRecord | undefined {
  const { sessionId } = useWorkView()
  const stored = useHarness((s) => (record ? undefined : callsOf(s, sessionId)[id]))
  return record ?? stored
}

function WordsView({ words, query }: { words: Words; query: string }) {
  return (
    <>
      {words.map((r, i) =>
        r.dim ? (
          <span key={i} className="wk-dim">
            <HighlightText text={r.t} query={query} />
          </span>
        ) : (
          <HighlightText key={i} text={r.t} query={query} />
        ),
      )}
    </>
  )
}

export function DiffTag({ add, del }: { add: number; del: number }) {
  return (
    <span className="wk-diff">
      <b>+{add}</b> <i>−{del}</i>
    </span>
  )
}

function useFocusSerial(id: string): number {
  return useHarness((s) => (s.focusedToolCallId === id ? s.focusedToolCallSerial : 0))
}

export const StepLine = memo(function StepLine({
  id,
  record,
  u,
  s,
  together,
}: {
  id: string
  record?: ToolCallRecord
  /** The seam unit this line folds into, and its strand in a spindle. */
  u?: number
  s?: number
  /** The first line of a group carries the group's "together" note. */
  together?: string
}) {
  const call = useCall(id, record)
  const query = useContext(SearchQueryContext)
  const focus = useFocusSerial(id)
  const [userOpen, setUserOpen] = useState<boolean | null>(null)
  useEffect(() => {
    if (focus > 0) setUserOpen(true)
  }, [focus])
  const live = call?.status === 'running'
  const now = useNow(!!live)
  if (!call) return null
  const err = call.status === 'error'
  const words = stepWords(call)
  const q = query.trim().toLowerCase()
  const hitInBody = !!q && ((call.result ?? '').toLowerCase().includes(q) || JSON.stringify(call.arguments ?? '').toLowerCase().includes(q))
  const open = (userOpen ?? false) || hitInBody
  const ms = callMs(call, now)
  const totals = call.fileChangeSet?.totals
  const cmd = live ? liveCommand(call) : ''
  const plain = words.map((w) => w.t).join('')
  return (
    <div
      className="wk-step"
      data-tool-call-id={id}
      data-state={live ? 'live' : err ? 'error' : 'done'}
      data-open={open ? 'true' : 'false'}
      data-focus={focus > 0 ? 'true' : undefined}
    >
      <button type="button" className="wk-step-head" aria-expanded={open} title={plain} onClick={() => setUserOpen(!open)}>
        <span className="wk-step-mark" data-u={u} data-s={s} aria-hidden />
        <span className="wk-step-text">
          <WordsView words={words} query={query} />
        </span>
        <span className="wk-step-meta">
          {together && <span className="wk-together">{together}</span>}
          {totals && totals.additions + totals.deletions > 0 && !live && <DiffTag add={totals.additions} del={totals.deletions} />}
          {err && <span className="wk-fail">failed</span>}
          {live ? (
            ms >= 1000 && <span className="wk-time">{Math.floor(ms / 1000)}s</span>
          ) : (
            !together && <span className={`wk-time${ms < 1000 ? ' wk-time--short' : ''}`}>{formatShort(ms)}</span>
          )}
          <Caret />
        </span>
      </button>
      {cmd && (
        <div className="wk-step-cmd">
          <span>$</span> {cmd}
        </div>
      )}
      <StepMedia call={call} id={id} />
      <Fold open={open} stagger={false}>
        <div className="wk-rule">
          <StepDetail call={call} query={query} />
        </div>
      </Fold>
    </div>
  )
})

export function Caret() {
  return (
    <svg className="wk-caret" viewBox="0 0 10 10" width="8" height="8" aria-hidden>
      <path d="M3 1.5 L7 5 L3 8.5" fill="none" stroke="currentColor" strokeWidth="1.3" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  )
}

/** Screens and images a call produced stay in view under its line. */
function StepMedia({ call, id }: { call: ToolCallRecord; id: string }) {
  const frameUrl = useFrameObjectUrl(call.frame)
  const [big, setBig] = useState(false)
  const images = call.resultImages ?? []
  if (!frameUrl && images.length === 0) return null
  return (
    <div className="wk-step-media">
      {frameUrl && (
        <button type="button" className="wk-frame" onClick={() => setBig((v) => !v)} title="Click to toggle full size">
          <img src={frameUrl} alt="captured screen" className={big ? 'wk-frame--big' : ''} loading="lazy" decoding="async" draggable={false} />
        </button>
      )}
      {images.length > 0 && <ToolResultImages images={images} toolCallId={id} className="wk-images" />}
    </div>
  )
}

const CLIP_LINES = 7

function Output({ text, bad, query }: { text: string; bad?: boolean; query: string }) {
  const [all, setAll] = useState(false)
  const lines = text.split('\n').length
  const clip = lines > CLIP_LINES && !all && !query
  return (
    <>
      <pre className={`wk-out${bad ? ' wk-bad' : ''}`} data-clip={clip ? 'true' : 'false'}>
        <HighlightText text={text} query={query} />
      </pre>
      {clip && (
        <button type="button" className="wk-more" onClick={() => setAll(true)}>
          show all {lines} lines
        </button>
      )}
    </>
  )
}

/** What a call was asked to do and what came back. */
export function StepDetail({ call, query }: { call: ToolCallRecord; query: string }) {
  const shown = resolveToolCall(call)
  const name = shown.name
  const args = (shown.arguments ?? {}) as Record<string, unknown>
  const live = call.status === 'running'
  const err = call.status === 'error'
  const command = typeof args.command === 'string' ? args.command : ''
  const path = typeof args.path === 'string' ? args.path : typeof args.file_path === 'string' ? args.file_path : ''
  let ask: ReactNode = null
  if (command) {
    ask = (
      <div className="wk-cmd">
        {command.split('\n').map((l, i) => (
          <div key={i}>
            <span className="wk-ps">$</span>
            <HighlightText text={l} query={query} />
          </div>
        ))}
      </div>
    )
  } else if (path && !call.fileChangeSet) {
    ask = (
      <div className="wk-cmd">
        <span className="wk-ps">↳</span>
        <HighlightText text={path} query={query} />
      </div>
    )
  } else if (!call.fileChangeSet) {
    const text = shown.arguments !== undefined ? JSON.stringify(shown.arguments, null, 2) : shown.partialJson ?? ''
    if (text && text !== '{}') ask = <Output text={text} query={query} />
  }
  return (
    <div className="wk-detail selectable">
      {ask}
      {call.fileChangeSet && <FileChangeCard changeSet={call.fileChangeSet} />}
      {!live && call.result && !(call.fileChangeSet && !err) && <Output text={call.result} bad={err} query={query} />}
      {live && <div className="wk-wait">running…</div>}
      <div className="wk-foot">
        <span>{name}</span>
        {!live && call.durationMs != null && <span>{formatShort(call.durationMs)}</span>}
        {err && <span className="wk-bad">failed</span>}
      </div>
    </div>
  )
}

/* ── A: a few calls at once ────────────────────────────────────────────
   Two to four calls that ran together: their lines, joined on the left,
   the first carrying "together". */
export const StepGroup = memo(function StepGroup({ ids, u, records }: { ids: string[]; u: number; records?: Record<string, ToolCallRecord> }) {
  const recs = useGroupRecords(ids, records)
  const done = recs.every((c) => c && c.status !== 'running')
  const wall = Math.max(0, ...recs.map((c) => c?.durationMs ?? 0))
  return (
    <div className="wk-group">
      {ids.map((id, j) => (
        <StepLine
          key={id}
          id={id}
          record={records?.[id]}
          u={u}
          s={strandIndex(j, ids.length)}
          together={j === 0 ? (done ? `together · ${formatShort(wall)}` : 'together') : undefined}
        />
      ))}
    </div>
  )
})

function useGroupRecords(ids: string[], records?: Record<string, ToolCallRecord>): Array<ToolCallRecord | undefined> {
  const { sessionId } = useWorkView()
  const key = useHarness((s) => {
    if (records) return ''
    const calls = callsOf(s, sessionId)
    return ids.map((id) => `${id}:${calls[id]?.status ?? ''}:${calls[id]?.durationMs ?? ''}`).join('|')
  })
  void key
  const calls = callsOf(useHarness.getState(), sessionId)
  return ids.map((id) => records?.[id] ?? calls[id])
}

/* ── A: many calls at once (five or more) ──────────────────────────────
   One line with a small fan of strands for its mark: open while calls are
   out, closing into a spindle as each comes back. The calls run as words
   underneath; each shimmers until it lands and opens its own result. */
export const Crowd = memo(function Crowd({ ids, u, records }: { ids: string[]; u: number; records?: Record<string, ToolCallRecord> }) {
  const query = useContext(SearchQueryContext)
  const recs = useGroupRecords(ids, records)
  const running = recs.some((c) => c?.status === 'running')
  const now = useNow(running)
  const [openId, setOpenId] = useState<string | null>(null)
  const [all, setAll] = useState(false)
  const n = ids.length
  const settled = recs.filter((c) => c && c.status !== 'running').length
  const errs = recs.filter((c) => c?.status === 'error').length
  const wall = Math.max(0, ...recs.map((c) => (c ? callMs(c, now) : 0)))
  const web = recs.every((c) => c && ['web_search', 'twitter_search'].includes(toolName(c)))
  const head = web ? 'Search the web' : `Run ${n} calls at once`
  const fan = recs.map((c) => (c?.status === 'running' ? 'run' : c?.status === 'error' ? 'err' : 'ok'))
  const openCall = openId ? recs[ids.indexOf(openId)] : undefined
  return (
    <div className="wk-crowd" data-state={running ? 'live' : 'done'}>
      <div className="wk-step-head wk-crowd-head">
        <span className="wk-fanmark" data-u={u} aria-hidden>
          <FanMark states={fan} />
        </span>
        <span className="wk-step-text">{head}</span>
        <span className="wk-step-meta">
          {running ? (
            <>
              <span>
                {settled} of {n} back
              </span>
              <span className="wk-time">{Math.floor(wall / 1000)}s</span>
            </>
          ) : (
            <>
              <span>
                {n} {web ? 'searches' : 'calls'}
              </span>
              {errs > 0 && <span className="wk-fail">{errs} failed</span>}
              <span className="wk-time">{formatShort(wall)}</span>
            </>
          )}
        </span>
      </div>
      <div className="wk-crowd-words" data-clamp={all ? 'false' : 'true'}>
        {ids.map((id, j) => {
          const c = recs[j]
          return (
            <span key={id} className="wk-crowd-item">
              {j > 0 && <span className="wk-crowd-sep" aria-hidden>·</span>}
              <button
                type="button"
                className="wk-crowd-word"
                data-state={c?.status ?? 'running'}
                aria-expanded={openId === id}
                onClick={() => setOpenId(openId === id ? null : id)}
              >
                <HighlightText text={c ? shortWords(c) : '…'} query={query} />
              </button>
            </span>
          )
        })}
      </div>
      {n > 12 && !all && (
        <button type="button" className="wk-more wk-crowd-more" onClick={() => setAll(true)}>
          show all {n}
        </button>
      )}
      <Fold open={!!openCall} stagger={false}>
        {openCall && (
          <div className="wk-rule">
            <StepDetail call={openCall} query={query} />
          </div>
        )}
      </Fold>
    </div>
  )
})

/** A 15×13 fan: strands open while their calls are out, closing into a
 *  spindle as each returns; a failed call's strand stops short. */
export function FanMark({ states }: { states: string[] }) {
  const n = states.length
  const m = Math.min(n, 5)
  const sampled: string[] = []
  for (let j = 0; j < m; j++) {
    const mine = states.filter((_, i) => Math.floor((i * m) / n) === j)
    sampled.push(mine.includes('run') ? 'run' : mine.includes('err') ? 'err' : 'ok')
  }
  return (
    <svg className="wk-fan" width="15" height="13" viewBox="0 0 15 13" aria-hidden>
      {sampled.map((st, i) => {
        const y = m === 1 ? 6.5 : 1.5 + (10 * i) / (m - 1)
        const yc = Math.round((6.5 + (y - 6.5) * 0.95) * 100) / 100
        const d =
          st === 'err' ? `M1 6.5C5 6.5 7 ${y} 9.5 ${y}` : st === 'run' ? `M1 6.5C5 6.5 9 ${y} 14 ${y}` : `M1 6.5C5.2 ${yc} 9.8 ${yc} 14 6.5`
        return <path key={i} className={`wk-fan-${st}`} d={d} style={{ d: `path('${d}')` } as CSSProperties} />
      })}
    </svg>
  )
}
