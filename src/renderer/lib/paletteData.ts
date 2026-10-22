import { useHarness } from '../state/store'
import type { PaletteClipboard, PalettePrResult } from '../../shared/events'

/**
 * Data the ⌘K cards read directly. MCP tools are called through the
 * bridge's `mcp_command call` with the panel surface, so replies land in
 * `mcp.lastResults` instead of the transcript and no agent turn runs.
 */

export interface McpCallResult {
  ok: boolean
  content: string
  message: string
}

export const CAL_SERVER = 'google_calendar_hosted'
export const SLACK_SERVER = 'slack'

export function mcpServerState(server: string): 'active' | 'missing' | string {
  const row = useHarness.getState().mcp.servers.find((r) => r.server === server)
  return row ? String(row.state ?? 'unknown') : 'missing'
}

export function mcpCall(server: string, tool: string, args: Record<string, unknown>, timeoutMs = 20_000): Promise<McpCallResult> {
  const store = useHarness.getState()
  const requestId = store.sendMcpCommand('call', { server, tool, arguments: args, surface: 'panel' })
  return new Promise((resolve) => {
    const finish = (r: McpCallResult) => {
      unsub()
      clearTimeout(timer)
      resolve(r)
    }
    const check = () => {
      const rec = useHarness.getState().mcp.lastResults[requestId]
      if (rec) finish({ ok: rec.ok, content: rec.message ?? '', message: rec.message ?? '' })
    }
    const unsub = useHarness.subscribe(check)
    const timer = setTimeout(() => finish({ ok: false, content: '', message: `${server}/${tool} timed out` }), timeoutMs)
    check()
  })
}

// ─── calendar ────────────────────────────────────────────────────────────

export interface CalEvent {
  id: string
  title: string
  start: Date
  end: Date
  allDay: boolean
  attendees: Array<{ email: string; name?: string; status?: string; self?: boolean }>
  link?: string
  joinUrl?: string
  location?: string
  organizer?: string
}

function parseEventTime(v: any): { d: Date; allDay: boolean } {
  if (!v) return { d: new Date(NaN), allDay: false }
  if (typeof v === 'string') return { d: new Date(v), allDay: !v.includes('T') }
  if (v.dateTime) return { d: new Date(v.dateTime), allDay: false }
  if (v.date) return { d: new Date(`${v.date}T00:00:00`), allDay: true }
  return { d: new Date(NaN), allDay: false }
}

function parseEvents(content: string): CalEvent[] {
  let raw: any
  try {
    raw = JSON.parse(content)
  } catch {
    return []
  }
  // Tool results arrive as MCP content blocks or as the bare payload.
  if (Array.isArray(raw) && raw.length && raw[0]?.type === 'text') {
    try {
      raw = JSON.parse(raw.map((b: any) => b.text ?? '').join(''))
    } catch {
      return []
    }
  }
  const list: any[] = Array.isArray(raw) ? raw : Array.isArray(raw?.events) ? raw.events : Array.isArray(raw?.items) ? raw.items : []
  return list
    .map((e): CalEvent => {
      const s = parseEventTime(e.start)
      const en = parseEventTime(e.end)
      const conf = e.conferenceUrl || e.hangoutLink || e.conferenceData?.entryPoints?.find((p: any) => p.entryPointType === 'video')?.uri
      return {
        id: String(e.id ?? e.eventId ?? Math.random()),
        title: String(e.summary ?? e.title ?? '(no title)'),
        start: s.d,
        end: en.d,
        allDay: s.allDay,
        attendees: Array.isArray(e.attendees)
          ? e.attendees.map((a: any) => ({ email: String(a.email ?? ''), name: a.displayName, status: a.responseStatus, self: !!a.self }))
          : [],
        link: e.htmlLink,
        joinUrl: conf,
        location: e.location,
        organizer: e.organizer?.email ?? e.organizer?.displayName,
      }
    })
    .filter((e) => !isNaN(e.start.getTime()))
    .sort((a, b) => a.start.getTime() - b.start.getTime())
}

export async function listEvents(start: Date, end: Date, calendarId = 'primary'): Promise<{ events: CalEvent[]; error?: string }> {
  const r = await mcpCall(CAL_SERVER, 'list_events', {
    calendarId,
    startTime: start.toISOString(),
    endTime: end.toISOString(),
    pageSize: 50,
    orderBy: 'startTime',
    timeZone: Intl.DateTimeFormat().resolvedOptions().timeZone,
  })
  if (!r.ok) return { events: [], error: r.message || 'calendar call failed' }
  return { events: parseEvents(r.content) }
}

/** Free gaps inside working hours for one day, given that day's events. */
export function freeSlots(day: Date, events: CalEvent[], durationMin: number, workStart = 9, workEnd = 18): Array<{ start: Date; end: Date }> {
  const dayStart = new Date(day)
  dayStart.setHours(workStart, 0, 0, 0)
  const dayEnd = new Date(day)
  dayEnd.setHours(workEnd, 0, 0, 0)
  const busy = events
    .filter((e) => !e.allDay)
    .map((e) => [Math.max(e.start.getTime(), dayStart.getTime()), Math.min(e.end.getTime(), dayEnd.getTime())] as const)
    .filter(([s, e]) => e > s)
    .sort((a, b) => a[0] - b[0])
  const out: Array<{ start: Date; end: Date }> = []
  let cursor = Math.max(dayStart.getTime(), Date.now() + 5 * 60_000)
  cursor = Math.ceil(cursor / (15 * 60_000)) * (15 * 60_000)
  const need = durationMin * 60_000
  for (const [s, e] of busy) {
    if (s - cursor >= need) out.push({ start: new Date(cursor), end: new Date(s) })
    cursor = Math.max(cursor, e)
  }
  if (dayEnd.getTime() - cursor >= need) out.push({ start: new Date(cursor), end: dayEnd })
  return out
}

// ─── slack ───────────────────────────────────────────────────────────────

export interface SlackChannel {
  id: string
  name: string
  purpose?: string
}

export interface SlackMessage {
  author: string
  email?: string
  text: string
  at?: string
  channel?: string
}

export async function slackFindChannel(name: string): Promise<{ channels: SlackChannel[]; error?: string }> {
  const r = await mcpCall(SLACK_SERVER, 'slack_search_channels', { query: name, limit: 5 })
  if (!r.ok) return { channels: [], error: r.message }
  const text = unwrapText(r.content, 'results')
  const channels: SlackChannel[] = []
  for (const block of text.split(/\n---\n/)) {
    const n = block.match(/Name:\s*#?([^\n]+)/)
    const id = block.match(/archives\/([A-Z0-9]+)/)
    if (n && id) channels.push({ id: id[1], name: n[1].trim(), purpose: block.match(/Purpose:\s*([^\n]+)/)?.[1]?.trim() })
  }
  const exact = channels.find((c) => c.name.toLowerCase() === name.toLowerCase())
  return { channels: exact ? [exact, ...channels.filter((c) => c !== exact)] : channels }
}

export async function slackReadChannel(channelId: string, limit = 20, oldestUnix?: number): Promise<{ messages: SlackMessage[]; channel?: string; error?: string }> {
  const args: Record<string, unknown> = { channel_id: channelId, limit, response_format: 'concise' }
  if (oldestUnix) args.oldest = String(oldestUnix)
  const r = await mcpCall(SLACK_SERVER, 'slack_read_channel', args)
  if (!r.ok) return { messages: [], error: r.message }
  const text = unwrapText(r.content, 'messages')
  const header = text.match(/^Channel:\s*#?([^\s(]+)/)
  const body = text.replace(/^Channel:[^\n]*\n+/, '')
  const parts = body.split(/\n\n(?=[^\n<]{1,80} <[^>\n]+>: )/)
  const messages: SlackMessage[] = []
  for (const p of parts) {
    const m = p.match(/^([^\n<]{1,80}) <([^>\n]+)>: ([\s\S]*?)(?:\s\[([0-9-]+ [0-9:]+ [A-Z]+)\])?\s*$/)
    if (m) messages.push({ author: m[1].trim(), email: m[2], text: m[3].trim(), at: m[4] })
  }
  return { messages, channel: header?.[1] }
}

export async function slackSearch(query: string, limit = 10): Promise<{ messages: SlackMessage[]; error?: string }> {
  const r = await mcpCall(SLACK_SERVER, 'slack_search_public_and_private', {
    query,
    limit,
    sort: 'timestamp',
    response_format: 'concise',
    include_context: false,
  })
  if (!r.ok) return { messages: [], error: r.message }
  const text = unwrapText(r.content, 'results')
  const messages: SlackMessage[] = []
  for (const line of text.split('\n')) {
    const m = line.match(/^\d+\.\s+(#[^\s]+|[^-]+?)\s+-\s+([^:]+):\s+([\s\S]*?)\s+(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} [A-Z]+)\s*$/)
    if (m) messages.push({ channel: m[1].trim(), author: m[2].trim(), text: m[3].trim(), at: m[4] })
  }
  return { messages }
}

function unwrapText(content: string, key: string): string {
  try {
    const j = JSON.parse(content)
    if (Array.isArray(j) && j[0]?.type === 'text') {
      const inner = j.map((b: any) => b.text ?? '').join('')
      try {
        const jj = JSON.parse(inner)
        return String(jj?.[key] ?? inner)
      } catch {
        return inner
      }
    }
    if (j && typeof j === 'object' && key in j) return String(j[key] ?? '')
  } catch {
    // plain text
  }
  return content
}

/** Slack's <@U123|Name> and <url|label> markup → readable text. */
export function slackPlain(text: string): string {
  return text
    .replace(/<@[A-Z0-9]+\|([^>]+)>/g, '@$1')
    .replace(/<@([A-Z0-9]+)>/g, '@$1')
    .replace(/<!subteam\^[A-Z0-9]+(?:\|([^>]+))?>/g, (_m, n) => `@${n ?? 'group'}`)
    .replace(/<(https?:[^|>]+)\|([^>]+)>/g, '$2')
    .replace(/<(https?:[^>]+)>/g, '$1')
}

// ─── main-process sources ────────────────────────────────────────────────

export async function fetchPrs(scope: string, repo?: string): Promise<PalettePrResult> {
  const api = (window as any).harness
  if (!api?.palettePrs) return { ok: false, scope, prs: [], error: 'not available outside the app', latencyMs: 0 }
  return api.palettePrs(scope, repo)
}

export async function readClipboard(): Promise<PaletteClipboard | null> {
  const api = (window as any).harness
  if (!api?.paletteClipboard) return null
  try {
    return await api.paletteClipboard()
  } catch {
    return null
  }
}

export function openExternal(url: string) {
  const api = (window as any).harness
  if (api?.paletteOpenExternal) void api.paletteOpenExternal(url)
  else window.open(url, '_blank', 'noopener')
}

export function revealInFinder(filePath: string) {
  const api = (window as any).harness
  if (api?.paletteReveal) void api.paletteReveal(filePath)
}
