import type { MessagePart, SubagentRecord, ToolCallRecord } from '@shared/events'
import { resolveToolCall } from '../../lib/toolCall'
import { describeCall, summarizeArgs, summarizePartialJson } from '../ToolCallChip'
import { sampleStrands } from '../../lib/seam'
import type { SeamUnit, StrandState } from '../../lib/seam'

/**
 * How a tool call reads as a quiet line (direction A) and how a stretch of
 * work reads as a seam (direction B). Plain language first: the call's own
 * account of itself (bash's `summary`, a computer-use `goal`) beats any
 * argument, and a file is named by its name with its folder dimmed.
 */

export const EDIT_TOOLS = new Set(['write_file', 'write', 'edit_file', 'edit', 'edit_json'])
const READ_TOOLS = new Set(['read_file', 'read'])
const LOOK_TOOLS = new Set(['glob', 'grep', 'list_directory'])
const WEB_SEARCH = new Set(['web_search', 'twitter_search'])
const WEB_PAGE = new Set(['web_fetch', 'web_research'])
const BOARD_TOOLS = new Set(['kanban', 'tasks'])
const BROWSER_TOOLS = new Set(['browser_execute_js', 'browser_screenshot', 'browser_navigate', 'browser_click'])
const NOTE_TOOLS = new Set(['memory', 'session_memory', 'working_memory', 'publish_finding', 'read_findings'])

/** One line of words, as runs: `dim` runs are the quiet parts (a folder,
 *  a tool name), the rest is the step itself. */
export type Words = Array<{ t: string; dim?: boolean }>

export const wordsText = (w: Words): string => w.map((r) => r.t).join('')

function str(v: unknown): string {
  return typeof v === 'string' ? v.trim() : ''
}

function splitPath(p: string): { dir: string; file: string } {
  const clean = p.replace(/\/+$/, '')
  const i = clean.lastIndexOf('/')
  return i < 0 ? { dir: '', file: clean } : { dir: clean.slice(0, i + 1), file: clean.slice(i + 1) }
}

function pathWords(verb: string, path: string, tail = ''): Words {
  const { dir, file } = splitPath(path)
  const out: Words = [{ t: `${verb} ` }]
  if (dir) out.push({ t: dir.length > 48 ? `…${dir.slice(-46)}` : dir, dim: true })
  out.push({ t: file || path })
  if (tail) out.push({ t: tail, dim: true })
  return out
}

function lineRange(args: Record<string, unknown>): string {
  const a = Number(args.start_line ?? args.offset)
  const b = Number(args.end_line ?? (Number.isFinite(Number(args.limit)) && Number.isFinite(a) ? a + Number(args.limit) - 1 : NaN))
  if (Number.isFinite(a) && Number.isFinite(b) && b >= a) return `, lines ${a}–${b}`
  if (Number.isFinite(a) && a > 1) return `, from line ${a}`
  return ''
}

function host(url: string): string {
  try {
    const u = new URL(url)
    return `${u.host}${u.pathname === '/' ? '' : u.pathname}`.slice(0, 80)
  } catch {
    return url.slice(0, 80)
  }
}

/** The step a tool call takes, in plain words. */
export function stepWords(call: ToolCallRecord): Words {
  const shown = resolveToolCall(call)
  const name = shown.name
  const args = (shown.arguments ?? undefined) as Record<string, unknown> | undefined
  if (!args) {
    const partial = summarizePartialJson(name, shown.partialJson)
    return partial ? [{ t: `${name} `, dim: true }, { t: partial }] : [{ t: name, dim: true }]
  }
  const described = describeCall(name, args)
  if (described) return [{ t: described }]
  const path = str(args.path) || str(args.file_path)
  if (EDIT_TOOLS.has(name) && path) return pathWords(name.startsWith('write') ? 'Write' : 'Edit', path)
  if (READ_TOOLS.has(name) && path) return pathWords('Read', path, lineRange(args))
  if (name === 'list_directory' && path) return pathWords('List', path)
  if (name === 'grep') {
    const where = path ? ` in ${splitPath(path).file || path}` : ''
    return [{ t: 'Search files for ' }, { t: str(args.pattern) }, { t: where, dim: true }]
  }
  if (name === 'glob') return [{ t: 'Find files matching ' }, { t: str(args.pattern) }]
  if (WEB_SEARCH.has(name)) return [{ t: 'Search the web for ' }, { t: searchQuery(args) }]
  if (WEB_PAGE.has(name)) {
    const url = str(args.url)
    if (url) return [{ t: 'Open ' }, { t: host(url) }]
    const q = str(args.query) || str(args.prompt)
    if (q) return [{ t: 'Research ' }, { t: q.slice(0, 120) }]
  }
  if (name === 'bash') {
    const cmd = str(args.command).split('\n')[0]
    if (cmd) return [{ t: '$ ', dim: true }, { t: cmd.slice(0, 120) }]
  }
  const known = knownWords(name, args)
  if (known) return known
  if (name === 'kanban') return [{ t: 'Board ', dim: true }, { t: summarizeArgs(name, args) }]
  const value = telling(args)
  const label = humanTool(name)
  return value ? [{ t: `${label} `, dim: true }, { t: value }] : [{ t: label, dim: true }]
}

/** Words for tools that take no summary of their own. */
function knownWords(name: string, args: Record<string, unknown>): Words | null {
  const action = str(args.action)
  switch (name) {
    case 'tasks': {
      const title = str(args.title)
      const id = str(args.task_id)
      const status = str(args.status)
      if (action === 'create') return [{ t: 'Add task ' }, { t: title ? `“${title}”` : id }]
      if (action === 'list') return [{ t: 'List tasks' }]
      if (status) return [{ t: 'Mark ' }, { t: title ? `“${title}”` : id || 'task' }, { t: ` ${status.replace(/_/g, ' ')}`, dim: true }]
      const note = str(args.note) || str(args.progress)
      return [{ t: `${action ? action[0].toUpperCase() + action.slice(1) : 'Update'} task ` }, { t: title || id }, ...(note ? [{ t: ` · ${note.slice(0, 80)}`, dim: true }] : [])]
    }
    case 'artifacts': {
      const ref = str(args.ref) || str(args.path)
      const file = ref ? splitPath(ref).file || ref : ''
      if (action === 'list' || !file) return [{ t: action === 'list' ? 'List artifacts' : 'Artifacts ' }, ...(action && action !== 'list' ? [{ t: action, dim: true }] : [])]
      return pathWords(action === 'read' ? 'Read artifact' : action === 'write' || action === 'save' ? 'Save artifact' : `${action[0]?.toUpperCase() ?? ''}${action.slice(1)} artifact`, ref)
    }
    case 'talk': {
      const to = str(args.to)
      return [{ t: `Message ${to || 'a helper'}` }, { t: ` · ${str(args.content).slice(0, 90)}`, dim: true }]
    }
    case 'subagents':
      return [{ t: action === 'wait_all' || action === 'wait' ? 'Wait for the helpers' : `Helpers ${action.replace(/_/g, ' ')}` }]
    case 'view_image':
      return [{ t: 'Look at an image' }]
    case 'tool_search':
      return [{ t: 'Find a tool ' }, { t: humanTool(str(args.tool_name) || str(args.query)), dim: true }]
    case 'load_skill':
      return [{ t: 'Load the skill ' }, { t: str(args.name) }]
    case 'search_skills':
      return [{ t: 'Search skills for ' }, { t: str(args.query) }]
    case 'publish_finding':
      return [{ t: 'Share a finding' }, { t: ` · ${str(args.content).slice(0, 90)}`, dim: true }]
    case 'read_findings':
      return [{ t: 'Read the findings' }]
    case 'memory':
    case 'session_memory':
    case 'working_memory': {
      const text = str(args.text) || str(args.content) || str(args.query)
      const verb = action === 'search' || action === 'read' || action === 'recall' ? 'Check memory' : 'Note in memory'
      return [{ t: verb }, ...(text ? [{ t: ` · ${text.replace(/^#+\s*/, '').slice(0, 90)}`, dim: true }] : [])]
    }
    case 'browser_execute_js':
      return [{ t: 'Run a script in ' }, { t: str(args.tab_url_filter) || 'the browser' }]
    case 'browser_screenshot':
      return [{ t: 'Capture ' }, { t: str(args.tab_url_filter) || 'the browser' }]
    case 'generate_image':
    case 'generate_svg':
      return [{ t: 'Draw ' }, { t: str(args.prompt).slice(0, 100) }]
    default:
      return null
  }
}

/** The most telling value in a call's arguments, for tools we do not know. */
function telling(args: Record<string, unknown>): string {
  for (const key of ['title', 'query', 'objective', 'prompt', 'content', 'text', 'name', 'url', 'ref', 'channel', 'action']) {
    const v = str(args[key])
    if (v) return (key === 'url' ? host(v) : v).replace(/\s+/g, ' ').slice(0, 100)
  }
  return ''
}

/** `mcp__slack__slack_read_thread` → "slack read thread". */
export function humanTool(name: string): string {
  const m = name.match(/^mcp__([^_]+(?:_[^_]+)*?)__(.+)$/)
  const server = m ? m[1] : ''
  let tool = m ? m[2] : name
  if (server && tool.startsWith(`${server}_`)) tool = tool.slice(server.length + 1)
  return `${server ? `${server} ` : ''}${tool}`.replace(/_/g, ' ')
}

function searchQuery(args: Record<string, unknown>): string {
  const q = str(args.query) || str(args.q) || str(args.objective)
  if (q) return q.slice(0, 120)
  const qs = Array.isArray(args.queries) ? args.queries.filter((x): x is string => typeof x === 'string') : []
  return qs.slice(0, 3).join(' · ').slice(0, 120)
}

/** A few words for a call among many (the queries of a search crowd). */
export function shortWords(call: ToolCallRecord): string {
  const shown = resolveToolCall(call)
  const args = (shown.arguments ?? undefined) as Record<string, unknown> | undefined
  if (!args) return summarizePartialJson(shown.name, shown.partialJson) || shown.name
  const q = searchQuery(args) || str(args.pattern)
  if (q) return q.slice(0, 64)
  const path = str(args.path) || str(args.file_path)
  if (path) return splitPath(path).file || path
  const url = str(args.url)
  if (url) return host(url).slice(0, 48)
  return (describeCall(shown.name, args) || summarizeArgs(shown.name, args) || shown.name).slice(0, 64)
}

/** The last command line of a running bash call, shown faintly under it. */
export function liveCommand(call: ToolCallRecord): string {
  const shown = resolveToolCall(call)
  if (shown.name !== 'bash') return ''
  const args = shown.arguments as Record<string, unknown> | undefined
  const cmd = str(args?.command)
  if (!cmd) return ''
  const lines = cmd.split('\n').filter((l) => l.trim())
  return (lines[lines.length - 1] ?? '').slice(0, 160)
}

export const toolName = (call: ToolCallRecord): string => resolveToolCall(call).name

export function isEdit(call: ToolCallRecord): boolean {
  return EDIT_TOOLS.has(toolName(call)) || (call.fileChangeSet?.totals.files ?? 0) > 0
}

export function callMs(call: ToolCallRecord, now: number): number {
  if (call.durationMs != null) return call.durationMs
  return Math.max(0, now - call.startedAt)
}

// ── Stretches ────────────────────────────────────────────────────────

/** One item in a stretch of work between two paragraphs. */
export type StretchItem =
  | { kind: 'thought'; part: MessagePart; index: number }
  | { kind: 'call'; id: string }
  | { kind: 'calls'; ids: string[]; groupId?: string }
  | { kind: 'helpers'; ids: string[] }

export interface HelperView {
  sub: SubagentRecord
  /** done | failed | running, with a finished child session counting as done. */
  state: 'running' | 'done' | 'failed'
  /** Seen working in this view and now back: its strand is drawn in. */
  back?: boolean
}

export interface StretchData {
  calls: Record<string, ToolCallRecord | undefined>
  helpers: Record<string, HelperView | undefined>
}

const thoughtSecs = (part: MessagePart): number =>
  part.startedAt != null && part.endedAt != null
    ? Math.max(1, (part.endedAt - part.startedAt) / 1000)
    : Math.max(1, Math.min(20, (part.text?.length ?? 0) / 400))

function callUnit(call: ToolCallRecord | undefined, now: number): SeamUnit {
  if (!call) return { k: 'up', ms: 100 }
  const label = `${wordsText(stepWords(call))} · ${call.status === 'error' ? 'failed' : call.status === 'running' ? 'running' : formatShort(callMs(call, now))}`
  if (call.status === 'error') return { k: 'fail', label }
  return { k: isEdit(call) ? 'down' : 'up', ms: callMs(call, now), label }
}

/** The seam units for a stretch, one per item. */
export function stretchUnits(items: StretchItem[], data: StretchData, now: number): SeamUnit[] {
  return items.map((it): SeamUnit => {
    if (it.kind === 'thought') {
      const secs = thoughtSecs(it.part)
      return { k: 'think', secs, label: `thought for ${Math.round(secs)}s` }
    }
    if (it.kind === 'call') return callUnit(data.calls[it.id], now)
    if (it.kind === 'calls') {
      const calls = it.ids.map((id) => data.calls[id])
      const states: StrandState[] = calls.map((c) => (c?.status === 'error' ? 'err' : 'ok'))
      const wall = Math.max(0, ...calls.map((c) => (c ? callMs(c, now) : 0)))
      const web = calls.every((c) => c && WEB_SEARCH.has(toolName(c)))
      return { k: 'calls', n: it.ids.length, strands: sampleStrands(states), label: `${it.ids.length} ${web ? 'searches' : 'calls'} at once · ${formatShort(wall)}` }
    }
    const views = it.ids.map((id) => data.helpers[id])
    const states: StrandState[] = views.map((h) => (h?.state === 'failed' ? 'err' : h?.state === 'running' ? 'out' : h?.back ? 'back' : 'ok'))
    const wall = Math.max(0, ...views.map((h) => h?.sub.elapsedMs ?? 0))
    return { k: 'helpers', n: it.ids.length, strands: sampleStrands(states), label: `${it.ids.length} ${it.ids.length === 1 ? 'helper' : 'helpers'} · ${formatShort(wall)}` }
  })
}

export function formatShort(ms: number): string {
  if (ms < 1000) return `${Math.max(1, Math.round(ms))}ms`
  const s = ms / 1000
  if (s < 60) return `${s < 10 ? s.toFixed(1).replace(/\.0$/, '') : Math.round(s)}s`
  const m = Math.floor(s / 60)
  if (m < 60) return `${m}m ${Math.round(s % 60)}s`
  return `${Math.floor(m / 60)}h ${m % 60}m`
}

const listJoin = (a: string[]): string =>
  a.length > 1 ? `${a.slice(0, -1).join(', ')} and ${a[a.length - 1]}` : a[0] ?? ''

/** The words beside a folded seam: what the work did, then what went
 *  wrong (kept apart so a narrow pane never cuts it off). */
export function stretchPhrase(items: StretchItem[], data: StretchData): { what: string; trouble: string[] } {
  const order: string[] = []
  const seen = (key: string) => { if (!order.includes(key)) order.push(key) }
  const reads: string[] = []
  const edits: string[] = []
  let commands = 0, pages = 0, looks = 0, board = 0, searches = 0, fails = 0, browser = 0, notes = 0, messages = 0
  const other: string[] = []
  let helperTotal = 0, back = 0, blocked = 0, out = 0
  const take = (call: ToolCallRecord | undefined) => {
    if (!call) return
    if (call.status === 'error') fails += 1
    const name = toolName(call)
    const args = (resolveToolCall(call).arguments ?? {}) as Record<string, unknown>
    const file = splitPath(str(args.path) || str(args.file_path)).file
    if (EDIT_TOOLS.has(name)) { seen('edit'); if (file && !edits.includes(file)) edits.push(file) }
    else if (READ_TOOLS.has(name)) { seen('read'); if (file && !reads.includes(file)) reads.push(file) }
    else if (LOOK_TOOLS.has(name)) { seen('look'); looks += 1 }
    else if (WEB_SEARCH.has(name)) { seen('search'); searches += 1 }
    else if (WEB_PAGE.has(name)) { seen('page'); pages += 1 }
    else if (name === 'bash') { seen('bash'); commands += 1 }
    else if (BOARD_TOOLS.has(name)) { seen('board'); board += 1 }
    else if (BROWSER_TOOLS.has(name)) { seen('browser'); browser += 1 }
    else if (NOTE_TOOLS.has(name)) { seen('notes'); notes += 1 }
    else if (name === 'talk') { seen('talk'); messages += 1 }
    else if (name === 'artifacts') { seen('read'); const ref = splitPath(str(args.ref)).file; if (ref && !reads.includes(ref)) reads.push(ref) }
    else if (name === 'subagents' || name === 'tool_search' || name === 'load_skill' || name === 'search_skills' || name === 'call_tool') { /* bookkeeping, not work */ }
    else { seen('other'); const h = humanTool(name); if (!other.includes(h)) other.push(h) }
  }
  for (const it of items) {
    if (it.kind === 'call') take(data.calls[it.id])
    else if (it.kind === 'calls') it.ids.forEach((id) => take(data.calls[id]))
    else if (it.kind === 'helpers') {
      seen('helpers')
      for (const id of it.ids) {
        helperTotal += 1
        const h = data.helpers[id]
        if (h?.state === 'failed') blocked += 1
        else if (h?.state === 'running') out += 1
        else back += 1
      }
    }
  }
  const plural = (n: number, one: string, many: string) => (n === 1 ? one : many.replace('#', String(n)))
  const bits = order.map((key) => {
    switch (key) {
      case 'edit': return edits.length && edits.length <= 2 ? `edited ${listJoin(edits)}` : plural(edits.length || 1, 'edited a file', 'edited # files')
      case 'read': return reads.length && reads.length <= 2 ? `read ${listJoin(reads)}` : plural(reads.length || 1, 'read a file', 'read # files')
      case 'look': return plural(looks, 'looked through files', 'looked through files # times')
      case 'search': return plural(searches, 'searched the web', 'searched the web # ways')
      case 'page': return plural(pages, 'opened a page', 'opened # pages')
      case 'bash': return plural(commands, 'ran a command', 'ran # commands')
      case 'board': return plural(board, 'updated a task', 'updated tasks # times')
      case 'browser': return plural(browser, 'used the browser', 'used the browser # times')
      case 'notes': return 'took notes'
      case 'talk': return plural(messages, 'messaged a helper', 'messaged helpers # times')
      case 'helpers': return plural(helperTotal, 'sent a helper', 'sent # helpers') + (helperTotal > 1 || back ? ` · ${back} came back` : '')
      default: return other.length <= 2 ? `used ${listJoin(other)}` : `used ${other.length} tools`
    }
  })
  const trouble: string[] = []
  if (blocked) trouble.push(`${blocked} blocked`)
  if (out) trouble.push(`${out} still working`)
  if (fails) trouble.push(`${fails} failed`)
  return { what: listJoin(bits) || 'worked', trouble }
}

/** Wall time of a stretch: first start to last end. */
export function stretchMs(items: StretchItem[], data: StretchData, now: number): number {
  let start = Infinity
  let end = 0
  const span = (a: number | undefined, b: number | undefined) => {
    if (a == null || b == null) return
    start = Math.min(start, a)
    end = Math.max(end, b)
  }
  for (const it of items) {
    if (it.kind === 'thought') span(it.part.startedAt, it.part.endedAt)
    const ids = it.kind === 'call' ? [it.id] : it.kind === 'calls' ? it.ids : []
    for (const id of ids) {
      const c = data.calls[id]
      if (c) span(c.startedAt, c.startedAt + callMs(c, now))
    }
    if (it.kind === 'helpers') {
      for (const id of it.ids) {
        const h = data.helpers[id]
        if (h) span(h.sub.startedAt, h.sub.startedAt + h.sub.elapsedMs)
      }
    }
  }
  return end > start ? end - start : 0
}

export function stretchDiff(items: StretchItem[], data: StretchData): { add: number; del: number } | null {
  let add = 0
  let del = 0
  for (const it of items) {
    const ids = it.kind === 'call' ? [it.id] : it.kind === 'calls' ? it.ids : []
    for (const id of ids) {
      const t = data.calls[id]?.fileChangeSet?.totals
      if (t) { add += t.additions; del += t.deletions }
    }
  }
  return add + del > 0 ? { add, del } : null
}
