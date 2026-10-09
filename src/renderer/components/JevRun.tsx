// How a `jev_computer_use` run looks in the UI.
//
// `JevRunView` replaces the markdown paragraph in a run's own pane with a
// step timeline and a result card. `JevResultSummary` is the compact form
// the parent shows on the sub-agent card and the inbox memo. Both read
// the text the run wrote (see lib/jevLog.ts), so runs saved before this
// view existed render the same way.

import { useMemo, useState, type ReactNode } from 'react'
import {
  parseJevLog,
  parseJevResult,
  shortUrl,
  type JevEntry,
  type JevResult,
  type JevStep,
} from '../lib/jevLog'
import { renderMarkdown } from '../lib/markdown'
import { highlightHtml } from '../lib/searchHighlight'
import { useHarness } from '../state/store'

/** The last line a jev run has written to its pane, wherever its slice lives
 *  (the active session's state, or the archive while another is open). */
export function useJevLastLine(sessionId: string | undefined): string | undefined {
  return useHarness((s) => {
    if (!sessionId?.startsWith('jev_')) return undefined
    const msgs = s.activeSessionId === sessionId ? s.messages : s.sessionArchive[sessionId]?.messages
    const last = msgs?.[msgs.length - 1]
    if (last?.role !== 'assistant') return undefined
    return last.parts?.find((p) => p.type === 'text')?.text?.trimEnd().split('\n').pop()
  })
}

/** "step 7 · click Close": what a running jev run is doing, from its last log line. */
export function jevActivity(line: string | undefined): string | null {
  if (!line) return null
  const e = parseJevLog(line).entries[0]
  if (!e) return null
  switch (e.type) {
    case 'step': {
      const what = e.op === 'type' ? `type into ${e.label}` : `${e.op.replace(/_/g, ' ')} ${e.label ?? e.arg ?? ''}`
      return `step ${e.n} · ${what.trim()}`
    }
    case 'llm':
      return e.what === 'replan' ? 'replanning' : e.what === 'verify' ? 'checking the result' : 'writing field text'
    case 'subgoal':
      return `sub-goal: ${e.text}`
    case 'item':
      return `item ${e.n}/${e.total} · ${e.text}`
    case 'open':
      return `opening ${shortUrl(e.url)}`
    case 'target':
      return `working in ${e.app}`
    default:
      return null
  }
}

/** One line for a finished run: its summary, or how many items were done.
 *  Takes a result that may be cut short (the activity card keeps 400 chars). */
export function jevOutcomeLine(text: string | undefined): string | null {
  if (!text) return null
  const table = text.startsWith('# | item | status')
  if (!table) {
    const r = parseJevResult(text)
    return (r ? r.summary : text.split('\n\n')[0]).split('\n')[0] || null
  }
  const rows = text.split('\n\n')[0].split('\n').slice(1)
  const done = rows.filter((r) => r.split(' | ')[2] === 'done').length
  return `${done} of ${rows.length}${text.includes('\n\n') ? '' : '+'} items done`
}

const STATUS: Record<string, { word: string; text: string; dot: string }> = {
  done: { word: 'done', text: 'text-ok', dot: 'bg-ok' },
  partial: { word: 'partly done', text: 'text-warn', dot: 'bg-warn' },
  blocked: { word: 'blocked', text: 'text-warn', dot: 'bg-warn' },
  needs_confirmation: { word: 'needs confirmation', text: 'text-accent', dot: 'bg-accent' },
  budget_exhausted: { word: 'out of budget', text: 'text-warn', dot: 'bg-warn' },
  skipped: { word: 'skipped', text: 'text-fg-2', dot: 'bg-fg-3' },
  cancelled: { word: 'cancelled', text: 'text-fg-2', dot: 'bg-fg-2' },
  error: { word: 'error', text: 'text-danger', dot: 'bg-danger' },
  dry_run: { word: 'dry run', text: 'text-fg-1', dot: 'bg-fg-2' },
  running: { word: 'running', text: 'text-accent', dot: 'bg-accent' },
}

/** Word and colours for a run or item status. */
export function jevStatus(status?: string): { word: string; text: string; dot: string } {
  return STATUS[status ?? ''] ?? { word: (status ?? 'finished').replace(/_/g, ' '), text: 'text-fg-1', dot: 'bg-fg-2' }
}

const OP_WORDS: Record<string, string> = {
  click: 'click',
  double_click: 'dbl-click',
  type: 'type',
  key: 'key',
  wait: 'wait',
  done: 'done',
  need_help: 'unsure',
  scroll_down: 'scroll ↓',
  scroll_up: 'scroll ↑',
  launch_app: 'launch',
  open_url: 'open',
}

/** "AXPopUpButton" → "pop up button". */
function roleWord(role?: string): string {
  if (!role) return ''
  return role.replace(/^AX/, '').replace(/([a-z])([A-Z])/g, '$1 $2').toLowerCase()
}

/** URLs inside a diff or reason, shortened; the full text goes in the tooltip. */
function withShortUrls(text: string): string {
  return text.replace(/https?:\/\/[^\s)'"]+/g, (u) => shortUrl(u, 48))
}

function prettyPart(part: string): string {
  return withShortUrls(part)
    .replace(/ -> /g, ' → ')
    .replace(/^elements \+(\d+)\/-(\d+)$/, 'elements +$1 −$2')
}

function sameAction(a: JevStep, b: JevStep): boolean {
  return a.op === b.op && a.index === b.index && a.label === b.label && a.arg === b.arg && a.text === b.text
}

/** Confidence that matters for the step: the operation, and the target when there is one. */
function stepConfidence(s: JevStep): number | undefined {
  if (!s.conf) return undefined
  return ['click', 'double_click', 'type'].includes(s.op) ? Math.min(s.conf.op, s.conf.target) : s.conf.op
}

function metricsLine(s: JevStep): string {
  const bits: string[] = []
  if (s.conf) bits.push(`op ${s.conf.op.toFixed(2)}`, `target ${s.conf.target.toFixed(2)}`)
  if (s.ms) bits.push(`jev ${s.ms.jev} ms`, `read ${s.ms.read} ms`)
  if (s.rows != null) bits.push(`${s.rows} rows`)
  return bits.join(' · ')
}

function StepTarget({ step, again }: { step: JevStep; again: boolean }) {
  const dim = again ? 'text-fg-2' : 'text-fg-0'
  if (step.op === 'type') {
    return (
      <span className={dim}>
        {step.text === null ? (
          <span className="italic text-fg-2">text from LLM</span>
        ) : (
          <span className="rounded bg-white/[0.05] px-1 text-fg-0">“{step.text}”</span>
        )}
        <span className="text-fg-3"> into </span>
        {step.label}
        {step.index != null && <span className="text-fg-3"> #{step.index}</span>}
      </span>
    )
  }
  if (step.op === 'key' && step.arg) {
    return <kbd className="kbd">{step.arg}</kbd>
  }
  if (step.label != null) {
    return (
      <span className={dim}>
        {again && <span className="mr-1 text-fg-3">again ·</span>}
        {step.label || <span className="italic text-fg-3">unlabelled</span>}
        <span className="text-fg-3">
          {' '}
          {roleWord(step.role)}
          {step.index != null ? ` #${step.index}` : ''}
        </span>
      </span>
    )
  }
  if (step.arg) return <span className={dim}>{step.op === 'open_url' ? shortUrl(step.arg) : step.arg}</span>
  if (step.op === 'need_help') return <span className="text-warn">{step.reasons ?? 'no confident choice'}</span>
  if (step.op === 'done') return <span className="text-fg-2">reports the goal done</span>
  return null
}

function Outcome({ step }: { step: JevStep }) {
  const o = step.outcome
  if (!o) return null
  if (o.kind === 'refused') return <span className="text-danger">refused</span>
  if (o.kind === 'changed') {
    return (
      <span className="flex items-center gap-1 text-ok">
        <span className="h-1.5 w-1.5 rounded-full bg-ok" />
        changed
      </span>
    )
  }
  return (
    <span className="flex items-center gap-1 text-fg-3">
      <span className="h-1.5 w-1.5 rounded-full ring-1 ring-fg-3" />
      no change
    </span>
  )
}

function StepRow({
  step,
  again,
  live,
  showMetrics,
}: {
  step: JevStep
  again: boolean
  live: boolean
  showMetrics: boolean
}) {
  const conf = stepConfidence(step)
  const low = conf != null && conf < 0.6
  const o = step.outcome
  const details =
    o?.kind === 'refused'
      ? [o.text]
      : o && !(o.kind === 'unchanged' && o.parts.length === 1 && o.parts[0] === 'no visible change')
        ? o.parts
        : []
  return (
    <div className="grid grid-cols-[2.25rem_5rem_minmax(0,1fr)_auto] items-baseline gap-x-2 py-[3px]">
      <span
        className={`text-right tabular-nums ${low ? 'text-warn' : 'text-fg-3'}`}
        title={`${metricsLine(step)}${low ? '\nlow confidence' : ''}`}
      >
        {String(step.n).padStart(2, '0')}
      </span>
      <span className={`uppercase tracking-[0.08em] text-[10px] ${step.op === 'done' ? 'text-ok' : step.op === 'need_help' ? 'text-warn' : 'text-fg-2'}`}>
        {OP_WORDS[step.op] ?? step.op.replace(/_/g, ' ')}
      </span>
      <span className="min-w-0 break-words">
        <StepTarget step={step} again={again} />
      </span>
      <span className="justify-self-end whitespace-nowrap text-[10.5px]">
        {live && !o && step.op !== 'need_help' && step.op !== 'done' ? (
          <span className="flex items-center gap-1 text-accent">
            <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-accent" />
            acting
          </span>
        ) : (
          <Outcome step={step} />
        )}
      </span>
      {(details.length > 0 || showMetrics) && (
        <div className="col-start-3 col-end-5 space-y-px pb-[2px] text-[10.5px] leading-[1.5]">
          {details.map((d, i) => (
            <div key={i} className={`break-words ${o?.kind === 'refused' ? 'text-danger/80' : 'text-fg-2'}`} title={d}>
              {prettyPart(d)}
            </div>
          ))}
          {showMetrics && <div className="text-fg-3">{metricsLine(step)}</div>}
        </div>
      )}
    </div>
  )
}

const LLM_WORDS = { replan: 'replan', compose: 'write text', verify: 'check result' } as const

function Aside({ tone, label, children }: { tone: string; label: string; children?: ReactNode }) {
  return (
    <div className={`my-1 ml-[2.75rem] border-l-2 py-[2px] pl-2.5 ${tone}`}>
      <div className="flex flex-wrap items-baseline gap-x-2">
        <span className="text-[10px] uppercase tracking-[0.08em]">{label}</span>
        <span className="min-w-0 break-words text-fg-1">{children}</span>
      </div>
    </div>
  )
}

function EntryRow({ entry, live, showMetrics, prevStep }: {
  entry: JevEntry
  live: boolean
  showMetrics: boolean
  prevStep?: JevStep
}) {
  switch (entry.type) {
    case 'step':
      return (
        <StepRow
          step={entry}
          again={!!prevStep && sameAction(prevStep, entry)}
          live={live}
          showMetrics={showMetrics}
        />
      )
    case 'llm':
      return (
        <Aside tone="border-accent/50 text-accent/90" label={LLM_WORDS[entry.what]}>
          {entry.reason && <span title={entry.reason}>{withShortUrls(entry.reason)}</span>}
          {entry.screenshot && <span className="ml-1.5 text-fg-3">· with screenshot</span>}
        </Aside>
      )
    case 'subgoal':
      return (
        <Aside tone="border-accent/50 text-accent/70" label="sub-goal">
          <span className="text-fg-0">{entry.text}</span>
        </Aside>
      )
    case 'subgoal_done':
      return (
        <div className="ml-[2.75rem] py-[2px] text-[10.5px] text-ok/90">
          ✓ sub-goal reached <span className="text-fg-3">· p {entry.p.toFixed(2)}</span>
        </div>
      )
    case 'disagree':
      return (
        <Aside tone="border-warn/60 text-warn" label="check disagrees">
          {entry.text}
          {entry.subgoal && <span className="block text-fg-0">→ {entry.subgoal}</span>}
        </Aside>
      )
    case 'stop':
      return (
        <Aside tone="border-danger/60 text-danger" label="stop">
          {entry.text}
        </Aside>
      )
    case 'target':
      return (
        <div className="flex items-baseline gap-2 py-[3px] pl-[2.75rem] text-[10.5px]">
          <span className="uppercase tracking-[0.08em] text-fg-3">app</span>
          <span className="text-fg-0">{entry.app}</span>
          {entry.bundle && <span className="text-fg-3">{entry.bundle}</span>}
        </div>
      )
    case 'launch':
      return (
        <div className="flex items-baseline gap-2 py-[3px] pl-[2.75rem] text-[10.5px]">
          <span className="uppercase tracking-[0.08em] text-fg-3">launch</span>
          <span className="text-fg-1">{entry.app}</span>
        </div>
      )
    case 'open':
      return (
        <div className="flex items-baseline gap-2 py-[3px] pl-[2.75rem] text-[10.5px]" title={entry.url}>
          <span className="uppercase tracking-[0.08em] text-fg-3">new tab</span>
          <span className="break-all text-fg-1">{shortUrl(entry.url, 80)}</span>
        </div>
      )
    case 'item':
      return (
        <div className="mb-1 mt-3 flex items-baseline gap-2 border-t border-white/[0.06] pt-2 first:mt-0 first:border-t-0 first:pt-0">
          <span className="text-[10px] uppercase tracking-[0.12em] text-fg-2">
            item {entry.n}/{entry.total}
          </span>
          <span className="text-[10px] text-fg-3">#{entry.index}</span>
          <span className="min-w-0 break-words text-fg-0">{entry.text}</span>
        </div>
      )
    case 'note':
      return <div className="py-[2px] pl-[2.75rem] text-[10.5px] text-fg-2 break-words">{entry.text}</div>
  }
}

function Stat({ children }: { children: ReactNode }) {
  return <span className="whitespace-nowrap">{children}</span>
}

/** The footer as words. Its `steps` counts actions taken; the timeline
 *  numbers Jev's decisions, which include `done` and `unsure`. */
function footerStats(r: JevResult): string[] {
  const f = r.footer
  const llmCalls = Number(f.llm_calls ?? 0)
  return [
    f.steps != null ? `${f.steps} action${f.steps === '1' ? '' : 's'}` : '',
    f.jev_median_ms ? `jev ${f.jev_median_ms} ms median` : '',
    llmCalls ? `${llmCalls} LLM call${llmCalls === 1 ? '' : 's'}${f.llm_ms ? ` (${(Number(f.llm_ms) / 1000).toFixed(1)} s)` : ''}` : '',
    f.elapsed ? f.elapsed.replace(/s$/, ' s') : '',
    f.surface ? (f.surface === 'dom' ? 'page DOM' : f.surface === 'ax' ? 'accessibility' : f.surface) : '',
  ].filter(Boolean)
}

function ItemsTable({ table, max }: { table: NonNullable<JevResult['table']>; max?: number }) {
  const rows = max ? table.rows.slice(0, max) : table.rows
  return (
    <div className="overflow-x-auto">
      <table className="w-full border-collapse text-[11px]">
        <thead>
          <tr className="text-left text-[9.5px] uppercase tracking-[0.1em] text-fg-3">
            <th className="py-1 pr-2 font-normal">#</th>
            <th className="py-1 pr-2 font-normal">item</th>
            <th className="py-1 pr-2 font-normal">status</th>
            <th className="py-1 pr-2 text-right font-normal">steps</th>
            <th className="py-1 pr-2 text-right font-normal">secs</th>
            {!max && <th className="py-1 font-normal">evidence</th>}
          </tr>
        </thead>
        <tbody>
          {rows.map((r, i) => {
            const st = jevStatus(r[2])
            return (
              <tr key={i} className="border-t border-white/[0.05] align-baseline">
                <td className="py-1 pr-2 tabular-nums text-fg-3">{r[0]}</td>
                <td className="whitespace-nowrap py-1 pr-2 text-fg-0">{r[1]}</td>
                <td className={`whitespace-nowrap py-1 pr-2 ${st.text}`}>{st.word}</td>
                <td className="py-1 pr-2 text-right tabular-nums text-fg-2">{r[3]}</td>
                <td className="py-1 pr-2 text-right tabular-nums text-fg-2">{r[4]}</td>
                {!max && <td className="py-1 text-fg-1 break-words" title={r[5]}>{withShortUrls(r[5] ?? '')}</td>}
              </tr>
            )
          })}
        </tbody>
      </table>
      {max && table.rows.length > max && (
        <div className="pt-1 text-[10px] text-fg-3">+{table.rows.length - max} more</div>
      )}
    </div>
  )
}

/** The result card at the end of a run's pane. */
function ResultCard({ result }: { result: JevResult }) {
  const st = jevStatus(result.status)
  const summaryHtml = useMemo(() => (result.summary ? renderMarkdown(result.summary) : ''), [result.summary])
  return (
    <div className="mt-3 rounded-lg border border-white/[0.08] bg-white/[0.02] px-3 py-2.5">
      <div className="mb-1.5 flex items-center gap-2">
        <span className="label text-fg-3">result</span>
        <span className={`h-1.5 w-1.5 rounded-full ${st.dot}`} />
        <span className={`text-[10.5px] uppercase tracking-[0.1em] ${st.text}`}>{st.word}</span>
      </div>
      {summaryHtml && (
        // eslint-disable-next-line react/no-danger
        <div className="md selectable text-[12.5px]" dangerouslySetInnerHTML={{ __html: summaryHtml }} />
      )}
      {result.table && (
        <div className="mt-1">
          <ItemsTable table={result.table} />
        </div>
      )}
      {result.pending && (
        <div className="mt-2 rounded-md bg-accent/[0.06] px-2 py-1 text-[11px] text-fg-0 ring-1 ring-accent/20">
          <span className="mr-2 text-[10px] uppercase tracking-[0.08em] text-accent">waiting on</span>
          {result.pending}
        </div>
      )}
      {result.page && (
        <div className="mt-2 flex items-baseline gap-2 text-[11px]" title={result.page}>
          <span className="text-[10px] uppercase tracking-[0.08em] text-fg-3">page</span>
          <span className="min-w-0 break-words text-fg-1">{withShortUrls(result.page)}</span>
        </div>
      )}
      <div className="mt-2 flex flex-wrap gap-x-3 gap-y-0.5 text-[10.5px] text-fg-2">
        {footerStats(result).map((s) => (
          <Stat key={s}>{s}</Stat>
        ))}
      </div>
      {result.next && <div className="mt-1.5 text-[11px] text-fg-2">next: {result.next}</div>}
      {result.handoff && (
        <details className="mt-1.5 text-[11px]">
          <summary className="cursor-pointer select-none text-[10px] uppercase tracking-[0.08em] text-fg-3 hover:text-fg-1">
            handoff
          </summary>
          <pre className="selectable mt-1 max-h-[280px] overflow-auto whitespace-pre-wrap break-words rounded bg-black/30 p-2 text-[10.5px] leading-[1.5] text-fg-1">
            {result.handoff}
          </pre>
        </details>
      )}
      {result.footer.log && (
        <div className="selectable mt-1.5 truncate text-[10px] text-fg-3" title={result.footer.log}>
          log {result.footer.log}
        </div>
      )}
    </div>
  )
}

function escapeHtml(s: string): string {
  return s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
}

/** A run's own pane: the step log as a timeline, then the result. With a
 *  search query the raw log shows instead, one event per line, so matches
 *  can be highlighted in place. `fallbackResult` stands in for runs saved
 *  before the log carried its result. */
export function JevRunView({
  text,
  live,
  searchQuery,
  fallbackResult,
}: {
  text: string
  live: boolean
  searchQuery?: string
  fallbackResult?: JevResult | null
}) {
  const log = useMemo(() => parseJevLog(text), [text])
  const [showMetrics, setShowMetrics] = useState(false)
  if (searchQuery) {
    const html = highlightHtml(escapeHtml(text), searchQuery)
    return (
      <pre
        className="selectable whitespace-pre-wrap break-words font-mono text-[11.5px] leading-[1.6] text-fg-1"
        // eslint-disable-next-line react/no-danger
        dangerouslySetInnerHTML={{ __html: html }}
      />
    )
  }
  const result = log.result ?? fallbackResult ?? undefined
  const steps = log.entries.filter((e): e is JevStep => e.type === 'step')
  const status = result?.status ?? (live ? 'running' : undefined)
  const st = status ? jevStatus(status) : null
  const app = log.entries.find((e) => e.type === 'target')
  // The last step still waiting for its outcome shows "acting" on its row.
  const tail = log.entries[log.entries.length - 1]
  const actingStep = tail?.type === 'step' && !tail.outcome && tail.op !== 'need_help' && tail.op !== 'done'
  let prevStep: JevStep | undefined
  return (
    <div className="font-mono text-[11.5px] leading-[1.55]">
      <div className="mb-2 flex flex-wrap items-center gap-x-2 gap-y-1 border-b border-white/[0.06] pb-2">
        <span className="label text-fg-2">jev run</span>
        {app && app.type === 'target' && <span className="text-fg-0">{app.app}</span>}
        <span className="text-fg-3">
          {steps.length} step{steps.length === 1 ? '' : 's'}
        </span>
        {st && (
          <span className={`flex items-center gap-1.5 text-[10.5px] uppercase tracking-[0.1em] ${st.text}`}>
            <span className={`h-1.5 w-1.5 rounded-full ${st.dot} ${status === 'running' ? 'animate-pulse' : ''}`} />
            {st.word}
          </span>
        )}
        <button
          type="button"
          onClick={() => setShowMetrics((v) => !v)}
          className={`ml-auto rounded px-1.5 py-[1px] text-[9.5px] uppercase tracking-[0.08em] ring-hairline hover:bg-white/[0.06] ${showMetrics ? 'text-fg-0' : 'text-fg-3'}`}
          title="Confidence, Jev latency, read time and element count for each step"
        >
          timings
        </button>
      </div>
      <div className="selectable">
        {log.entries.map((entry, i) => {
          const row = (
            <EntryRow
              key={i}
              entry={entry}
              live={live && i === log.entries.length - 1}
              showMetrics={showMetrics}
              prevStep={prevStep}
            />
          )
          if (entry.type === 'step') prevStep = entry
          else if (entry.type === 'item' || entry.type === 'llm') prevStep = undefined
          return row
        })}
        {live && !result && !actingStep && (
          <div className="flex items-center gap-1.5 py-1 pl-[2.75rem] text-[10.5px] text-fg-3">
            <span className="h-1 w-1 animate-pulse rounded-full bg-accent" />
            reading the screen…
          </div>
        )}
      </div>
      {result && <ResultCard result={result} />}
    </div>
  )
}

/** The compact result the parent shows on a sub-agent card or an inbox
 *  memo. `showStatus` off when the surrounding header already names it. */
export function JevResultSummary({
  result,
  clamp = true,
  showStatus = true,
  showStats = true,
}: {
  result: JevResult
  clamp?: boolean
  showStatus?: boolean
  showStats?: boolean
}) {
  const st = jevStatus(result.status)
  const stats = showStats ? footerStats(result) : []
  return (
    <div className="font-mono text-[11px] leading-[1.55]">
      <div className="flex flex-wrap items-center gap-x-2 gap-y-0.5">
        {showStatus && (
          <span className={`flex items-center gap-1.5 text-[10px] uppercase tracking-[0.1em] ${st.text}`}>
            <span className={`h-1.5 w-1.5 rounded-full ${st.dot}`} />
            {st.word}
          </span>
        )}
        {stats.map((s) => (
          <span key={s} className="whitespace-nowrap text-[10px] text-fg-3">
            {s}
          </span>
        ))}
      </div>
      {result.summary && (
        <div className={`selectable mt-1 whitespace-pre-wrap break-words text-fg-1 ${clamp ? 'line-clamp-3' : ''}`}>
          {result.summary}
        </div>
      )}
      {result.table && (
        <div className="mt-1">
          <ItemsTable table={result.table} max={clamp ? 5 : undefined} />
        </div>
      )}
      {result.pending && (
        <div className="mt-1 break-words text-accent">
          <span className="text-[10px] uppercase tracking-[0.08em]">waiting on </span>
          {result.pending}
        </div>
      )}
      {result.page && (
        <div className="mt-1 truncate text-[10.5px] text-fg-2" title={result.page}>
          page {withShortUrls(result.page)}
        </div>
      )}
    </div>
  )
}
