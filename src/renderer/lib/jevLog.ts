// Parser for what a `jev_computer_use` run writes.
//
// A run streams one line per event into its own pane (bridge/tools/
// jev_operator/loop.py `_say`), then a `[result]` line and the result text
// the parent receives (render_result / render_items_result in
// bridge/tools/jev_computer_use_tool.py). As markdown that log is one
// unreadable paragraph: single newlines collapse, `need_help` turns italic,
// and long URLs wrap mid-token. These functions turn it back into events so
// the pane can draw a timeline and the parent a result card.
//
// The text is the record (it is what the session saves), so the parser
// reads every format the operator has written: older runs label the read
// time `ax` and have no `[result]` block. A line it does not know becomes a
// plain note, never an error.

export type JevOutcome =
  | { kind: 'changed' | 'unchanged'; parts: string[] }
  | { kind: 'refused'; text: string }

export interface JevStep {
  type: 'step'
  n: number
  /** click, double_click, type, key, wait, done, need_help, scroll_down, … */
  op: string
  /** Element number in the table Jev saw. */
  index?: number
  role?: string
  label?: string
  /** Typed text; null when the LLM composed it. */
  text?: string | null
  /** Key name, app name or URL. */
  arg?: string
  conf?: { op: number; target: number }
  ms?: { jev: number; read: number }
  rows?: number
  reasons?: string
  outcome?: JevOutcome
}

export type JevEntry =
  | JevStep
  | { type: 'llm'; what: 'replan' | 'compose' | 'verify'; reason?: string; screenshot?: boolean }
  | { type: 'subgoal'; text: string }
  | { type: 'subgoal_done'; p: number }
  | { type: 'disagree'; text: string; subgoal?: string }
  | { type: 'stop'; text: string }
  | { type: 'target'; app: string; bundle?: string; pid?: number }
  | { type: 'launch'; app: string }
  | { type: 'open'; url: string }
  | { type: 'item'; n: number; total: number; index: number; text: string }
  | { type: 'note'; text: string }

export interface JevResult {
  status?: string
  /** What the run found or did, in the operator's words. */
  summary: string
  /** `key=value` pairs of the `[jev_computer_use]` line. */
  footer: Record<string, string>
  page?: string
  pending?: string
  /** The `[handoff]` block, for runs that did not finish cleanly. */
  handoff?: string
  next?: string
  /** The per-item table of an items run. */
  table?: { header: string[]; rows: string[][] }
}

export interface JevLog {
  entries: JevEntry[]
  result?: JevResult
}

export const RESULT_MARK = '[result]'
const FOOTER = '[jev_computer_use]'

const STEP =
  /^step (\d+): (.*) \(op ([\d.]+), target ([\d.]+), jev (\d+) ms, (?:ax|dom|read) (\d+) ms, (\d+) rows\)(?: \[(.*)\])?$/
const OUTCOME = /^→ (changed|no change): (.*)$/
const REPLAN = /^→ LLM: replanning \((.*)\)( with screenshot)?$/

/** A Python `repr()` of a str back to the str. */
export function unrepr(s: string): string {
  const q = s[0]
  if (s.length < 2 || (q !== "'" && q !== '"') || s[s.length - 1] !== q) return s
  return s.slice(1, -1).replace(/\\(x[0-9a-fA-F]{2}|u[0-9a-fA-F]{4}|U[0-9a-fA-F]{8}|.)/g, (_, e: string) => {
    if (e.length > 1) return String.fromCodePoint(parseInt(e.slice(1), 16))
    return ({ n: '\n', t: '\t', r: '\r' } as Record<string, string>)[e] ?? e
  })
}

/** The action half of a step line, as `describe()` in decide.py writes it. */
function parseAction(s: string): Pick<JevStep, 'op' | 'index' | 'role' | 'label' | 'text' | 'arg'> {
  let m = /^(click|double_click) \[(\d+)\] (\S+) (.+)$/.exec(s)
  if (m) return { op: m[1], index: Number(m[2]), role: m[3], label: unrepr(m[4]) }
  m = /^type (.+) into \[(\d+)\] (.+)$/.exec(s)
  if (m) {
    return {
      op: 'type',
      text: m[1] === '(text from LLM)' ? null : unrepr(m[1]),
      index: Number(m[2]),
      label: unrepr(m[3]),
    }
  }
  m = /^(key|launch_app|open_url) (.+)$/.exec(s)
  if (m) return { op: m[1], arg: m[2] }
  return { op: s.trim() }
}

function lastOpenStep(entries: JevEntry[]): JevStep | undefined {
  for (let i = entries.length - 1; i >= 0; i--) {
    const e = entries[i]
    if (e.type === 'step') return e.outcome ? undefined : e
  }
  return undefined
}

function parseLine(line: string, entries: JevEntry[]): void {
  const t = line.trim()
  if (!t) return
  let m = STEP.exec(t)
  if (m) {
    entries.push({
      type: 'step',
      n: Number(m[1]),
      ...parseAction(m[2]),
      conf: { op: Number(m[3]), target: Number(m[4]) },
      ms: { jev: Number(m[5]), read: Number(m[6]) },
      rows: Number(m[7]),
      ...(m[8] ? { reasons: m[8] } : {}),
    })
    return
  }
  if ((m = OUTCOME.exec(t))) {
    const outcome: JevOutcome = {
      kind: m[1] === 'changed' ? 'changed' : 'unchanged',
      parts: m[2].split('; ').filter(Boolean),
    }
    const step = lastOpenStep(entries)
    if (step) step.outcome = outcome
    else entries.push({ type: 'note', text: t.slice(2) })
    return
  }
  if ((m = /^→ refused: (.*)$/.exec(t))) {
    const step = lastOpenStep(entries)
    if (step) step.outcome = { kind: 'refused', text: m[1] }
    else entries.push({ type: 'stop', text: `refused: ${m[1]}` })
    return
  }
  if ((m = REPLAN.exec(t))) {
    entries.push({ type: 'llm', what: 'replan', reason: m[1], screenshot: !!m[2] })
    return
  }
  if (t === '→ LLM: composing field text') {
    entries.push({ type: 'llm', what: 'compose' })
    return
  }
  if (t === '→ LLM: verifying end state') {
    entries.push({ type: 'llm', what: 'verify' })
    return
  }
  if ((m = /^sub-goal complete \(p=([\d.]+)\); back to the main goal$/.exec(t))) {
    entries.push({ type: 'subgoal_done', p: Number(m[1]) })
    return
  }
  if ((m = /^sub-goal: (.*)$/.exec(t))) {
    entries.push({ type: 'subgoal', text: m[1] })
    return
  }
  if ((m = /^verifier disagrees: (.*); sub-goal: (.*)$/.exec(t))) {
    entries.push({ type: 'disagree', text: m[1], subgoal: m[2] })
    return
  }
  if ((m = /^planner said done, end-state check disagrees: (.*)$/.exec(t))) {
    entries.push({ type: 'disagree', text: m[1] })
    return
  }
  if (
    /; stopping for confirmation$/.test(t) ||
    /; answered Cancel$/.test(t) ||
    /^not touching window /.test(t)
  ) {
    entries.push({ type: 'stop', text: t })
    return
  }
  if ((m = /^target: (.+) \(([^(),]+), pid (\d+)\)$/.exec(t))) {
    entries.push({ type: 'target', app: m[1], bundle: m[2], pid: Number(m[3]) })
    return
  }
  if ((m = /^target is now (.+)$/.exec(t))) {
    entries.push({ type: 'target', app: m[1] })
    return
  }
  if ((m = /^launching (.+)$/.exec(t))) {
    entries.push({ type: 'launch', app: m[1] })
    return
  }
  if ((m = /^opening (\S+) in a new tab$/.exec(t))) {
    entries.push({ type: 'open', url: m[1] })
    return
  }
  if ((m = /^item (\d+) of (\d+) \(#(\d+)\): (.*)$/.exec(t))) {
    entries.push({ type: 'item', n: Number(m[1]), total: Number(m[2]), index: Number(m[3]), text: m[4] })
    return
  }
  entries.push({ type: 'note', text: t })
}

/** True when `text` reads as a jev run's log (its first line is one the operator writes). */
export function looksLikeJevLog(text: string): boolean {
  const first = text.trimStart().split('\n', 1)[0]
  return /^(target: |launching |opening \S+ in a new tab|item \d+ of \d+ |step \d+: )/.test(first) || first === RESULT_MARK
}

export function parseJevLog(text: string): JevLog {
  const cut = text.search(/^\[result\]$/m)
  const log = cut < 0 ? text : text.slice(0, cut)
  const entries: JevEntry[] = []
  for (const line of log.split('\n')) parseLine(line, entries)
  const out: JevLog = { entries }
  if (cut >= 0) {
    const result = parseJevResult(text.slice(cut + RESULT_MARK.length).replace(/^\n/, ''))
    if (result) out.result = result
  }
  return out
}

function parseFooter(line: string): Record<string, string> {
  const out: Record<string, string> = {}
  const body = line.slice(FOOTER.length).trim()
  // `log=` is last and its path may hold spaces.
  const logAt = body.search(/(^| )log=/)
  const head = logAt < 0 ? body : body.slice(0, logAt)
  for (const pair of head.split(' ')) {
    const eq = pair.indexOf('=')
    if (eq > 0) out[pair.slice(0, eq)] = pair.slice(eq + 1)
  }
  if (logAt >= 0) out.log = body.slice(logAt).trim().slice(4)
  return out
}

/** The result text a run returns (tool result, inbox memo, or the part of
 *  the pane after `[result]`); null when it carries no `[jev_computer_use]`
 *  line and is not a bare cancel or failure. */
export function parseJevResult(text: string): JevResult | null {
  const lines = text.replace(/\s+$/, '').split('\n')
  const f = lines.findIndex((l) => l.startsWith(FOOTER))
  if (f < 0) return null
  const footer = parseFooter(lines[f])
  const result: JevResult = { status: footer.status, summary: '', footer }

  let head = lines.slice(0, f)
  if (head[0]?.startsWith('# | item | status')) {
    const end = head.findIndex((l) => !l.trim())
    const rows = (end < 0 ? head : head.slice(0, end)).map((l) => l.split(' | '))
    const width = rows[0].length
    result.table = {
      header: rows[0],
      rows: rows.slice(1).map((r) => [...r.slice(0, width - 1), r.slice(width - 1).join(' | ')]),
    }
    head = end < 0 ? [] : head.slice(end)
  }
  result.summary = head.join('\n').trim()

  let i = f + 1
  for (; i < lines.length && lines[i].trim(); i++) {
    const l = lines[i]
    if (l.startsWith('page: ')) result.page = l.slice(6)
    else if (l.startsWith('pending_action: ')) result.pending = l.slice(16)
    else if (l.startsWith('next: ')) result.next = l.slice(6)
  }
  const rest = lines.slice(i).join('\n').trim()
  if (rest) {
    const block = rest.split('\n')
    const nextAt = block.findIndex((l) => l.startsWith('next: '))
    if (nextAt >= 0) {
      result.next = block[nextAt].slice(6)
      block.splice(nextAt, 1)
    }
    if (block[0]?.startsWith('[handoff]')) result.handoff = block.join('\n')
    else if (block.length) result.summary = [result.summary, block.join('\n')].filter(Boolean).join('\n\n')
  }
  return result
}

/** "https://host/very/long/path?query" → "host/very/long/path…", for display. */
export function shortUrl(url: string, max = 60): string {
  const bare = url.replace(/^https?:\/\//, '').replace(/^www\./, '')
  const noQuery = bare.split(/[?#]/, 1)[0]
  const s = noQuery.length < bare.length ? `${noQuery}?…` : noQuery
  return s.length <= max ? s : `${s.slice(0, max - 1)}…`
}
