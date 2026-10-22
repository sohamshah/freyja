/**
 * Slot parsing for the ⌘K generative palette.
 *
 * Jev (System One) answers *which* widget a query wants; this file answers
 * *with what*: the attendee, the day, the time, the PR number, the model
 * name. It is pure and synchronous so it can run on every keystroke, which
 * is what makes a widget fill in as the sentence grows:
 *
 *   create a meeting                      → event card, every slot empty
 *   create a meeting with nari            → attendee filled
 *   create a meeting with nari tomorrow   → day filled, availability strip appears
 *   … tomorrow at 1pm                     → time filled, "Create event" enables
 *
 * Jev is debounced and only re-runs to change the domain; slots never wait
 * on the network.
 */

export interface DaySlot {
  label: string
  /** ISO date, local time. */
  iso: string
  weekday: string
}

export interface TimeSlot {
  label: string
  hour: number
  minute: number
}

export interface PaletteSlots {
  attendees: string[]
  day?: DaySlot
  /** 'week' when the query asks about a range rather than one day. */
  range?: 'today' | 'week' | 'next_week'
  time?: TimeSlot
  durationMin?: number
  topic?: string
  person?: string
  prNumber?: string
  repo?: string
  profile?: string
  focus?: string
  modelQuery?: string
  reasoning?: string
  periodDays?: number
  periodLabel?: string
  fileTypes: string[]
  keyword?: string
  theme?: 'dark' | 'light' | 'system'
  mute?: boolean
  toolName?: string
  logLevel?: 'error' | 'warn'
  subsystem?: string
  swarmStatus?: 'running' | 'done' | 'failed'
  category?: string
}

const WEEKDAYS = ['sunday', 'monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'saturday']
const WEEKDAY_ABBR: Record<string, number> = {
  sun: 0, mon: 1, tue: 2, tues: 2, wed: 3, weds: 3, thu: 4, thur: 4, thurs: 4, fri: 5, sat: 6,
}
const MONTHS = ['jan', 'feb', 'mar', 'apr', 'may', 'jun', 'jul', 'aug', 'sep', 'oct', 'nov', 'dec']

/** Words that end a free-text capture such as the attendee list. */
const STOP = /\b(?:today|tomorrow|tmrw|tmr|tonight|next|this|on|at|for|about|re|regarding|to|in|from|by|the|a|an|and|or|every|around|after|before|via|using|with|high|xhigh|medium|low|max|minimal|none|thinking|reasoning|effort|errors?|warnings?)\b/i

function cap(s: string): string {
  return s.replace(/\b[a-z]/g, (c) => c.toUpperCase())
}

function toIso(d: Date): string {
  const y = d.getFullYear()
  const m = String(d.getMonth() + 1).padStart(2, '0')
  const day = String(d.getDate()).padStart(2, '0')
  return `${y}-${m}-${day}`
}

function daySlot(d: Date, label: string): DaySlot {
  return { label, iso: toIso(d), weekday: cap(WEEKDAYS[d.getDay()]) }
}

function addDays(base: Date, n: number): Date {
  const d = new Date(base)
  d.setDate(d.getDate() + n)
  return d
}

function nextWeekday(base: Date, target: number, skipThisWeek: boolean): Date {
  let delta = (target - base.getDay() + 7) % 7
  if (delta === 0) delta = 7
  if (skipThisWeek && delta < 7 - base.getDay()) delta += 7
  return addDays(base, delta)
}

export function parseDay(text: string, now = new Date()): { day?: DaySlot; range?: PaletteSlots['range'] } {
  const q = text.toLowerCase()
  if (/\bday after tomorrow\b/.test(q)) return { day: daySlot(addDays(now, 2), 'Day after tomorrow') }
  if (/\b(?:tomorrow|tmrw|tmr)\b/.test(q)) return { day: daySlot(addDays(now, 1), 'Tomorrow') }
  if (/\b(?:today|tonight)\b/.test(q)) return { day: daySlot(now, 'Today'), range: 'today' }
  if (/\bnext week\b/.test(q)) return { range: 'next_week' }
  if (/\bthis week\b/.test(q)) return { range: 'week' }

  const wd = q.match(/\b(next|this|on)?\s*(sunday|monday|tuesday|wednesday|thursday|friday|saturday|sun|mon|tue|tues|wed|weds|thu|thur|thurs|fri|sat)\b/)
  if (wd) {
    const name = wd[2]
    const idx = WEEKDAYS.indexOf(name) >= 0 ? WEEKDAYS.indexOf(name) : WEEKDAY_ABBR[name]
    const d = nextWeekday(now, idx, wd[1] === 'next')
    const prefix = wd[1] === 'next' ? 'Next ' : ''
    return { day: daySlot(d, `${prefix}${cap(WEEKDAYS[idx])}`) }
  }

  const md = q.match(/\b(jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?\s+(\d{1,2})(?:st|nd|rd|th)?\b/)
  if (md) {
    const month = MONTHS.indexOf(md[1].slice(0, 3))
    const d = new Date(now.getFullYear(), month, parseInt(md[2], 10))
    if (d < now) d.setFullYear(d.getFullYear() + 1)
    return { day: daySlot(d, `${cap(MONTHS[month])} ${md[2]}`) }
  }
  const dm = q.match(/\b(\d{1,2})\/(\d{1,2})\b/)
  if (dm) {
    const d = new Date(now.getFullYear(), parseInt(dm[1], 10) - 1, parseInt(dm[2], 10))
    if (d < now) d.setFullYear(d.getFullYear() + 1)
    return { day: daySlot(d, `${dm[1]}/${dm[2]}`) }
  }
  return {}
}

export function parseTime(text: string): TimeSlot | undefined {
  const q = text.toLowerCase()
  if (/\bnoon\b/.test(q)) return { label: '12:00 PM', hour: 12, minute: 0 }
  if (/\bmidnight\b/.test(q)) return { label: '12:00 AM', hour: 0, minute: 0 }
  const soft = q.match(/\b(morning|afternoon|evening|eod|end of day|lunch)\b/)
  const explicit = /\d/.test(q)
  if (soft && !explicit) {
    const table: Record<string, TimeSlot> = {
      morning: { label: 'Morning · 9:00 AM', hour: 9, minute: 0 },
      lunch: { label: 'Lunch · 12:00 PM', hour: 12, minute: 0 },
      afternoon: { label: 'Afternoon · 2:00 PM', hour: 14, minute: 0 },
      evening: { label: 'Evening · 6:00 PM', hour: 18, minute: 0 },
      eod: { label: 'End of day · 5:00 PM', hour: 17, minute: 0 },
      'end of day': { label: 'End of day · 5:00 PM', hour: 17, minute: 0 },
    }
    return table[soft[1]]
  }
  // "at 1pm", "1:30 pm", "at 13:00", "1pm"
  const m = q.match(/(?:\bat\s+)?\b(\d{1,2})(?::(\d{2}))?\s*(am|pm|a\.m\.|p\.m\.)\b/) || q.match(/\bat\s+(\d{1,2})(?::(\d{2}))?\b/) || q.match(/\b(\d{1,2}):(\d{2})\b/)
  if (!m) return undefined
  let hour = parseInt(m[1], 10)
  const minute = m[2] ? parseInt(m[2], 10) : 0
  const mer = m[3]?.replace(/\./g, '')
  if (hour > 23 || minute > 59) return undefined
  if (mer === 'pm' && hour < 12) hour += 12
  if (mer === 'am' && hour === 12) hour = 0
  // "at 1" with no meridian: business hours assumption
  if (!mer && m[0].startsWith('at') && hour >= 1 && hour <= 6) hour += 12
  const h12 = hour % 12 === 0 ? 12 : hour % 12
  const label = `${h12}:${String(minute).padStart(2, '0')} ${hour >= 12 ? 'PM' : 'AM'}`
  return { label, hour, minute }
}

export function parseDuration(text: string): number | undefined {
  const q = text.toLowerCase()
  if (/\bhalf an hour\b|\b30\s*min/.test(q)) return 30
  const h = q.match(/\b(\d+(?:\.\d+)?)\s*(?:h|hr|hrs|hour|hours)\b/)
  if (h) return Math.round(parseFloat(h[1]) * 60)
  const m = q.match(/\b(\d+)\s*(?:m|min|mins|minute|minutes)\b/)
  if (m) return parseInt(m[1], 10)
  if (/\ban hour\b/.test(q)) return 60
  return undefined
}

function parseAttendees(text: string): string[] {
  // "with nari", "with nari and sam", "w/ nari", "1:1 with nari"
  const m =
    text.match(/\b(?:with|w\/)\s+([a-z][a-z'.-]*(?:\s*(?:,|and|&)\s*[a-z][a-z'.-]*)*)/i) ||
    text.match(/\b(?:meet|meeting|sync|call|chat|1:1|coffee)\s+([a-z][a-z'.-]*(?:\s*(?:,|and|&)\s*[a-z][a-z'.-]*)*)/i)
  if (!m) return []
  return m[1]
    .split(/\s*(?:,|\band\b|&)\s*/i)
    .map((s) => s.trim())
    .filter((s) => s && !STOP.test(s) && !/^\d/.test(s))
    .map(cap)
}

function parseTopic(text: string): string | undefined {
  const m = text.match(/\b(?:about|regarding|re:?|to discuss|on the topic of)\s+(.+?)(?=\s+\b(?:with|tomorrow|today|next|at|for|on)\b|$)/i)
  if (!m) return undefined
  const t = m[1].trim().replace(/[.?!]+$/, '')
  return t ? t : undefined
}

function parsePeriod(text: string): { days?: number; label?: string } {
  const q = text.toLowerCase()
  if (/\btoday\b/.test(q)) return { days: 1, label: 'Today' }
  if (/\byesterday\b/.test(q)) return { days: 2, label: 'Since yesterday' }
  const n = q.match(/\b(?:last|past)\s+(\d+)\s*(day|days|week|weeks|month|months)\b/)
  if (n) {
    const k = parseInt(n[1], 10)
    const unit = n[2].startsWith('week') ? 7 : n[2].startsWith('month') ? 30 : 1
    return { days: k * unit, label: `Last ${k} ${n[2]}` }
  }
  if (/\b(?:this|last|past)\s+week\b/.test(q)) return { days: 7, label: 'Last 7 days' }
  if (/\b(?:this|last|past)\s+month\b/.test(q)) return { days: 30, label: 'Last 30 days' }
  if (/\ball time\b|\bever\b/.test(q)) return { days: 3650, label: 'All time' }
  return {}
}

const FILE_TYPES: Record<string, string[]> = {
  html: ['html', 'htm'], report: ['html'], markdown: ['md'], md: ['md'], png: ['png'], image: ['png', 'jpg', 'jpeg', 'gif', 'webp'],
  pdf: ['pdf'], csv: ['csv'], json: ['json'], python: ['py'], script: ['py', 'sh', 'mjs', 'ts'], typescript: ['ts', 'tsx'],
  svg: ['svg'], video: ['mp4', 'mov'], audio: ['mp3', 'wav'], notebook: ['ipynb'],
}

export const TOOL_NAMES = [
  'bash', 'read_file', 'write_file', 'edit_file', 'edit_json', 'glob', 'grep', 'list_directory', 'web_search', 'web_fetch',
  'twitter_search', 'sub_agent', 'subagents', 'jev_computer_use', 'computer_use', 'browser_execute_js', 'browser_screenshot',
  'show_widget', 'tasks', 'memory', 'session_memory', 'schedule', 'talk', 'monitor', 'generate_image', 'generate_svg',
  'view_image', 'appshot', 'screenshot', 'click', 'type_text', 'press_key', 'read_ax_tree', 'find_element', 'artifacts',
  'load_skill', 'search_skills', 'recall', 'summarize_context',
]

export function parseSlots(text: string, now = new Date()): PaletteSlots {
  const q = text.toLowerCase()
  const slots: PaletteSlots = { attendees: [], fileTypes: [] }

  slots.attendees = parseAttendees(text)
  const { day, range } = parseDay(text, now)
  slots.day = day
  slots.range = range
  slots.time = parseTime(text)
  slots.durationMin = parseDuration(text)
  slots.topic = parseTopic(text)

  // memory: "what did nari say", "from nari", "nari's recommendation"
  const person =
    text.match(/\bwhat (?:did|does|has)\s+([a-z][a-z'.-]*)\b/i) ||
    text.match(/\b(?:from|by)\s+([a-z][a-z'.-]*)\b/i) ||
    text.match(/\b([a-z][a-z'.-]*)'s\b/i)
  if (person && !STOP.test(person[1])) slots.person = cap(person[1])
  else if (slots.attendees.length) slots.person = slots.attendees[0]

  const pr = text.match(/\b(?:pr|pull request|#)\s*#?(\d{2,6})\b/i)
  if (pr) slots.prNumber = pr[1]
  const repo = q.match(/\b(agent-harness|ema-next|freyja|vercel-blog|infra|evals)\b/)
  if (repo) slots.repo = repo[1]
  if (/\breview\b/.test(q)) slots.profile = 'review'
  else if (/\b(?:explore|research|investigate|look into)\b/.test(q)) slots.profile = 'explore'
  else if (/\b(?:test|run the tests|verify)\b/.test(q)) slots.profile = 'test'
  else if (/\b(?:implement|build|fix|refactor|write code)\b/.test(q)) slots.profile = 'code'
  const focus = q.match(/\b(security|performance|perf|correctness|style|tests?|migrations?|schema|prompt)\b/)
  if (focus) slots.focus = focus[1]

  const model = q.match(/\b(opus|sonnet|haiku|gpt[- ]?[\d.]*\s*(?:sol|luna)?|luna|sol|gemini|glm|kimi|deepseek|fable|o[1-9](?:-mini)?)\b/)
  if (model) slots.modelQuery = model[1].replace(/\s+/g, ' ').trim()
  const LEVEL = 'x-?high|high|medium|med|low|max|minimal|min|no|none|off'
  const reasoning =
    q.match(new RegExp(`\\b(${LEVEL})\\s*(?:thinking|reasoning|effort)\\b`)) ??
    q.match(new RegExp(`\\b(?:thinking|reasoning|effort)(?:\\s+level)?\\s*(?:to|at|=|:|on)?\\s*(${LEVEL})\\b`))
  if (reasoning) {
    const raw = reasoning[1].replace('-', '')
    slots.reasoning = raw === 'med' ? 'medium' : raw === 'min' ? 'minimal' : raw === 'no' || raw === 'off' ? 'none' : raw
  }

  const period = parsePeriod(text)
  slots.periodDays = period.days
  slots.periodLabel = period.label

  for (const [word, exts] of Object.entries(FILE_TYPES)) {
    if (new RegExp(`\\b${word}s?\\b`).test(q)) {
      for (const e of exts) if (!slots.fileTypes.includes(e)) slots.fileTypes.push(e)
    }
  }
  const kw = text.match(/\b(?:about|named|called|for|on|matching)\s+["']?([a-z0-9][a-z0-9 _.-]{1,40})["']?/i)
  if (kw && !STOP.test(kw[1].split(' ')[0])) slots.keyword = kw[1].trim()

  if (/\bdark\b/.test(q)) slots.theme = 'dark'
  else if (/\blight\b|\bpaper\b/.test(q)) slots.theme = 'light'
  else if (/\bsystem\b|\bauto\b/.test(q)) slots.theme = 'system'
  if (/\bunmute\b/.test(q)) slots.mute = false
  else if (/\bmute\b/.test(q)) slots.mute = true

  for (const t of TOOL_NAMES) {
    if (q.includes(t)) {
      slots.toolName = t
      break
    }
  }

  if (/\berrors?\b|\bfail(?:ed|ures?)?\b/.test(q)) slots.logLevel = 'error'
  else if (/\bwarn(?:ings?)?\b/.test(q)) slots.logLevel = 'warn'
  const sub = q.match(/\b(bridge|gateway|slack|mcp|scheduler|voice|jev|renderer|daemon)\b/)
  if (sub) slots.subsystem = sub[1]

  if (/\brunning\b|\bactive\b|\bin progress\b/.test(q)) slots.swarmStatus = 'running'
  else if (/\bdone\b|\bfinished\b|\bcompleted\b/.test(q)) slots.swarmStatus = 'done'
  else if (/\bfailed\b|\bcrashed\b/.test(q)) slots.swarmStatus = 'failed'

  const cat = q.match(/\b(travel|food|dining|flights?|hotels?|rideshare|uber|software|saas|hardware|groceries|coffee)\b/)
  if (cat) slots.category = cap(cat[1])

  return slots
}

export type PaletteWidgetType =
  | 'calendar_create'
  | 'calendar_view'
  | 'memory'
  | 'agent_task'
  | 'model_picker'
  | 'token_usage'
  | 'artifact_explorer'
  | 'briefing'
  | 'swarm'
  | 'tool_inspect'
  | 'diagnostics'
  | 'voice'
  | 'appearance'
  | 'mic'
  | 'quick_controls'
  | 'finance'
  | null

/** Pick the widget from Jev's decision, with local verb cues breaking
 *  ties inside a domain. Returns null for a plain palette search. */
export function chooseWidget(
  domain: string,
  subTarget: string,
  confidence: number,
  readiness: number,
  query: string,
  slots: PaletteSlots,
): PaletteWidgetType {
  const q = query.toLowerCase()
  if (domain === 'palette_command') return null
  // A bare keyword that Jev was unsure about stays a plain search.
  if (confidence < 0.45 && readiness < 0.8) return null

  switch (domain) {
    case 'calendar': {
      const creating = /\b(?:create|schedule|set ?up|book|add|new|put|block|invite|plan)\b/.test(q)
      const viewing = /\b(?:what|which|show|list|do i have|am i|any|my)\b/.test(q)
      if (subTarget === 'calendar_create' && !viewing) return 'calendar_create'
      if (subTarget === 'calendar_view' && !creating) return 'calendar_view'
      return creating ? 'calendar_create' : 'calendar_view'
    }
    case 'memory':
      return 'memory'
    case 'agent_task':
      return 'agent_task'
    case 'model_picker':
      return 'model_picker'
    case 'token_usage':
      return 'token_usage'
    case 'artifact_explorer':
      return 'artifact_explorer'
    case 'briefing':
      return 'briefing'
    case 'swarm':
      return 'swarm'
    case 'tool_inspect':
      return 'tool_inspect'
    case 'diagnostics':
      return 'diagnostics'
    case 'voice_session':
      return 'voice'
    case 'finance':
      return 'finance'
    case 'quick_control': {
      if (subTarget === 'appearance_toggle' || slots.theme || /\b(?:appearance|theme|mode)\b/.test(q)) return 'appearance'
      if (subTarget === 'mic_toggle' || slots.mute !== undefined || /\bmic\b|\bmicrophone\b/.test(q)) return 'mic'
      return 'quick_controls'
    }
    default:
      return null
  }
}

/** Which slots a widget needs before its primary action makes sense. */
export function requiredSlots(type: PaletteWidgetType): Array<keyof PaletteSlots> {
  switch (type) {
    case 'calendar_create':
      return ['attendees', 'day', 'time']
    case 'agent_task':
      return ['profile']
    default:
      return []
  }
}

export function slotFilled(slots: PaletteSlots, key: keyof PaletteSlots): boolean {
  const v = slots[key]
  if (Array.isArray(v)) return v.length > 0
  return v !== undefined && v !== null && v !== ''
}
