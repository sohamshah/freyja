import { useHarness, type SessionSlice } from '../state/store'
import { useVoiceStore } from '../state/voice-store'
import { useSchedulerStore } from '../state/scheduler-store'
import { summarizeArgs } from '../components/ToolCallChip'
import { bucketFor } from '../components/views/taskBuckets'
import { buildPaletteItems, runPaletteItem } from './paletteItems'
import type { Message } from '../../shared/events'
import type {
  IslandAction,
  IslandAttention,
  IslandDigest,
  IslandDone,
  IslandLive,
  IslandPaletteIndex,
  IslandPaletteRow,
  IslandTaskRow,
} from '../../shared/island'

/**
 * The island's feed. Reduces this store (and the voice store) into a small
 * digest after every change, pushes it to main when it differs from the
 * last one, and performs the actions the island sends back.
 * See src/shared/island.ts for the shapes and docs/ISLAND.md for the design.
 */
const PUSH_DELAY_MS = 120
const TEXT_TAIL = 320
const TOOL_SUMMARY = 120
const DONE_TEXT = 220
const DONE_KEEP_MS = 30000
const STAMP_KEEP_MS = 10000
/** Rows the island's field searches: every command and skill, the helpers, and this many recent sessions. */
const PALETTE_SESSIONS = 80

interface IslandHarnessApi {
  islandDigest(digest: IslandDigest): Promise<{ ok: boolean }>
  islandPalette?(index: IslandPaletteIndex): Promise<{ ok: boolean }>
  onIslandPerform?(listener: (action: IslandAction) => void): () => void
  sendCommand?(command: Record<string, unknown>): Promise<unknown>
}

export function startIslandFeed(api: Partial<IslandHarnessApi> | null | undefined): () => void {
  if (!api || typeof api.islandDigest !== 'function') return () => {}
  const push = api.islandDigest.bind(api)
  const wasStreaming = new Map<string, boolean>()
  const knownDone = new Set<string>()
  let seededDone = false
  let done: IslandDone | null = null
  let justDone: IslandDigest['tasks']['justDone'] = null
  let lastKey = ''
  let timer: number | null = null

  const build = (): IslandDigest => {
    const s = useHarness.getState()
    const v = useVoiceStore.getState()
    const nowMs = Date.now()
    const sessionById = new Map(s.sessions.map((row) => [row.id, row]))
    const titleOf = (id: string) => sessionById.get(id)?.title || id.slice(0, 8)
    const slices: Array<[string, SessionSlice]> = [[s.activeSessionId, s]]
    for (const [id, slice] of Object.entries(s.sessionArchive)) {
      if (id !== s.activeSessionId && slice) slices.push([id, slice])
    }

    // Live: every slice with a turn in flight, plus helpers that have no slice yet.
    const live: IslandLive[] = []
    const seen = new Set<string>()
    for (const [id, slice] of slices) {
      const was = wasStreaming.get(id) ?? false
      wasStreaming.set(id, slice.isStreaming)
      if (slice.isStreaming) {
        seen.add(id)
        const msg = slice.messages.find((m) => m.id === slice.currentStreamingMessageId) ?? null
        const last = msg?.parts[msg.parts.length - 1] as { type: string; endedAt?: number } | undefined
        const running = [...slice.toolCallOrder]
          .reverse()
          .map((cid) => slice.toolCalls[cid])
          .find((rec) => rec && rec.status === 'running')
        const row = sessionById.get(id)
        const toolName = running ? running.invokes || running.name : ''
        const parentId = row?.parentSessionId
        live.push({
          id,
          title: titleOf(id),
          kind: row?.agentType === 'voice' ? 'voice' : parentId ? 'sub' : 'root',
          parentId,
          parentTitle: parentId ? titleOf(parentId) : undefined,
          startedAt: msg?.createdAt ?? nowMs,
          tool: running
            ? { name: toolName, summary: summarizeArgs(toolName, running.arguments).slice(0, TOOL_SUMMARY), startedAt: running.startedAt }
            : null,
          thinking: last?.type === 'thinking' && !last.endedAt,
          text: tail(plain(textOf(msg)), TEXT_TAIL),
        })
      } else if (was) {
        // The turn just ended: remember it for the banner.
        const reply = [...slice.messages].reverse().find((m) => m.role === 'assistant') ?? null
        const failed = !!reply?.parts.some(
          (p) => p.type === 'system' && (p as { systemSubtype?: string }).systemSubtype === 'error',
        )
        done = {
          sessionId: id,
          title: titleOf(id),
          text: head(plain(textOf(reply)), DONE_TEXT),
          at: nowMs,
          success: !failed,
          durationMs: reply ? Math.max(0, (reply.completedAt ?? nowMs) - reply.createdAt) : 0,
        }
      }
      for (const sub of Object.values(slice.subagents)) {
        if ((sub.state === 'running' || sub.state === 'pending') && !seen.has(sub.id)) {
          seen.add(sub.id)
          const parentId = sub.parentId ?? id
          live.push({
            id: sub.id,
            title: sub.label || titleOf(sub.id),
            kind: 'sub',
            parentId,
            parentTitle: titleOf(parentId),
            startedAt: sub.startedAt || nowMs,
            tool: null,
            thinking: false,
            text: '',
          })
        }
      }
    }
    live.sort((a, b) => a.startedAt - b.startedAt)
    if (done && nowMs - done.at > DONE_KEEP_MS) done = null

    // Attention: permission prompts, MCP questions, a voice action waiting on a yes.
    const attention: IslandAttention[] = s.permissionQueue.map((req) => ({
      kind: 'permission' as const,
      id: req.requestId,
      sessionId: req.sessionId,
      sessionTitle: titleOf(req.sessionId ?? s.activeSessionId),
      level: req.level,
      prompt: req.prompt,
      reason: req.reason,
    }))
    const elicitations = (s as unknown as { mcp?: { elicitations?: Array<{ requestId: string; server: string; message: string; mode: 'form' | 'url' }> } }).mcp?.elicitations ?? []
    for (const e of elicitations) attention.push({ kind: 'elicitation', id: e.requestId, server: e.server, message: e.message, mode: e.mode })
    if (v.activity?.status === 'confirm') attention.push({ kind: 'voice-confirm', id: 'voice-confirm', summary: v.activity.summary })

    // Tasks: the board, reduced to buckets, from the task events of every slice.
    const tasks = new Map<string, { id: string; title: string; status: string; at: number }>()
    for (const [, slice] of slices) {
      for (const ev of slice.systemEvents) {
        if (!ev.subtype.startsWith('task_')) continue
        const details = ev.details ?? {}
        const items: unknown[] = [details.task, ...(Array.isArray(details.tasks) ? details.tasks : [])]
        for (const item of items) {
          if (!item || typeof item !== 'object') continue
          const task = item as Record<string, unknown>
          const id = String(task.id ?? '')
          if (!id) continue
          const prev = tasks.get(id)
          const at = typeof task.updatedAt === 'number' ? task.updatedAt : ev.at
          if (prev && prev.at > at) continue
          tasks.set(id, {
            id,
            title: typeof task.title === 'string' ? task.title : prev?.title ?? id,
            status: typeof task.status === 'string' ? task.status : statusFromSubtype(ev.subtype) ?? prev?.status ?? 'todo',
            at,
          })
        }
      }
    }
    const counts = { now: 0, next: 0, blocked: 0, done: 0 }
    const rows: IslandTaskRow[] = []
    for (const task of tasks.values()) {
      const bucket = bucketFor(task.status)
      counts[bucket] += 1
      rows.push({ id: task.id, title: task.title, bucket, at: task.at })
      if (bucket === 'done' && !knownDone.has(task.id)) {
        knownDone.add(task.id)
        if (seededDone) justDone = { id: task.id, title: task.title, at: nowMs }
      }
    }
    seededDone = true
    if (justDone && nowMs - justDone.at > STAMP_KEEP_MS) justDone = null
    rows.sort((a, b) => b.at - a.at)

    const streamingOf = (id: string) => (id === s.activeSessionId ? s.isStreaming : !!s.sessionArchive[id]?.isStreaming)
    const sessions = s.sessions
      .filter((row) => !row.parentSessionId && row.agentType !== 'voice')
      .sort((a, b) => b.updatedAt - a.updatedAt)
      .slice(0, 6)
      .map((row) => ({ id: row.id, title: row.title, updatedAt: row.updatedAt, streaming: streamingOf(row.id) }))
    const metrics = useSchedulerStore.getState().metrics
    const schedule =
      metrics?.next_fire_at && metrics.next_fire_job_name
        ? { name: metrics.next_fire_job_name, at: metrics.next_fire_at * 1000 }
        : null
    const activeRow = sessionById.get(s.activeSessionId)
    const dayStart = new Date(nowMs).setHours(0, 0, 0, 0)
    const costToday = s.sessions.reduce((acc, row) => (row.updatedAt >= dayStart ? acc + (row.totalCost ?? 0) : acc), 0)

    return {
      at: nowMs,
      active: activeRow ? { id: activeRow.id, title: activeRow.title, streaming: s.isStreaming } : null,
      live,
      attention,
      done,
      voice: {
        state: v.engineState,
        active: v.active,
        level: v.micLevel,
        userLine: v.userLine,
        assistantLine: v.assistantLine,
        hotkeyLabel: v.hotkeyLabel,
        available: !v.config || (!!v.config.enabled && !!v.config.hasApiKey),
      },
      tasks: { ...counts, latest: rows.slice(0, 4), justDone },
      sessions,
      appshotLabel: s.appshotHotkeyLabel,
      costToday: Math.round(costToday * 100) / 100,
      schedule,
      veil: s.computerActive,
    }
  }

  const flush = () => {
    timer = null
    const digest = build()
    const { at: _at, ...rest } = digest
    const key = JSON.stringify(rest)
    if (key === lastKey) return
    lastKey = key
    push(digest).catch(() => {})
  }
  const schedule = () => {
    if (timer == null) timer = window.setTimeout(flush, PUSH_DELAY_MS)
  }

  /** The ⌘K rows, minus the clipboard suggestions (the island is not where the clipboard is read). */
  const paletteIndex = (): IslandPaletteIndex => {
    const rows: IslandPaletteRow[] = []
    let sessionsKept = 0
    for (const item of buildPaletteItems(() => {})) {
      if (item.group === 'Clipboard') continue
      if (item.group === 'Session' && sessionsKept++ >= PALETTE_SESSIONS) continue
      rows.push({ id: item.id, title: item.title, subtitle: item.subtitle, group: item.group })
    }
    return { at: Date.now(), rows }
  }

  const perform = (action: IslandAction) => {
    const st = useHarness.getState()
    switch (action.type) {
      case 'open':
        void st.openSessionPane(action.sessionId, action.mode ?? 'replace')
        break
      case 'send':
        if (action.sessionId === st.activeSessionId) void st.sendMessage(action.text, { force: !!action.cutIn })
        else void st.operatorTalk(action.sessionId, action.text, !!action.cutIn)
        break
      case 'permission':
        void st.answerPermission(action.requestId, action.approved)
        break
      case 'elicitation':
        void st.answerMcpElicitation(action.requestId, action.action)
        break
      case 'voice': {
        const voice = useVoiceStore.getState()
        if (action.on && !voice.active) voice.toggleVoice()
        else if (!action.on && voice.active) voice.endVoice('island')
        break
      }
      case 'voice-confirm':
        useVoiceStore.getState().confirmPending(action.approve)
        break
      case 'capture':
        void st.captureAppshot('button')
        break
      case 'dashboard':
        st.toggleMissionDashboard(true, action.tab)
        break
      case 'stop':
        // The active session's turn goes through the store, which also
        // stops its spinners; any other turn, and a session's helpers, go
        // straight to the bridge.
        if (action.scope === 'turn' && action.sessionId === st.activeSessionId) void st.cancelTurn()
        else if (action.scope === 'subagents' && action.sessionId === st.activeSessionId) void st.stopSubagents()
        else void api.sendCommand?.({ type: 'force_cancel', sessionId: action.sessionId, scope: action.scope })
        break
      case 'attach': {
        // Dropped on the island: into the composer's tray, as a paste would.
        let skipped = 0
        for (const file of action.files) {
          const bytes = Uint8Array.from(atob(file.dataBase64), (c) => c.charCodeAt(0))
          if (file.mimeType.startsWith('image/')) {
            void st.attachImage(new File([bytes], file.name, { type: file.mimeType }))
          } else if (file.mimeType.startsWith('video/')) {
            void st.attachVideo(new File([bytes], file.name, { type: file.mimeType }))
          } else {
            skipped += 1
          }
        }
        if (skipped > 0) st.showToast(skipped === 1 ? 'Only images and videos attach' : `${skipped} files skipped: only images and videos attach`, 'warn')
        break
      }
      case 'palette-index':
        void api.islandPalette?.(paletteIndex()).catch(() => {})
        break
      case 'palette-run':
        if (!runPaletteItem(action.id)) st.showToast('That row is gone', 'warn')
        break
      case 'palette-open':
        st.toggleCommandPalette(true, action.query)
        break
      case 'appearance':
        void st.setAppearanceTheme(action.theme)
        break
      case 'view':
        switch (action.view) {
          case 'morning-room':
            st.toggleMorningRoom(true)
            break
          case 'artifacts':
            st.toggleArtifactsBrowser(true)
            break
          case 'scheduler':
            useSchedulerStore.getState().openDashboard()
            break
          default:
            st.toggleMissionDashboard(true, action.view.slice('dashboard:'.length) as 'overview' | 'tasks' | 'activity' | 'scheduler')
        }
        break
      case 'focus-app':
        break
    }
  }

  const offStore = useHarness.subscribe(schedule)
  const offVoice = useVoiceStore.subscribe(schedule)
  const offScheduler = useSchedulerStore.subscribe(schedule)
  const offPerform = typeof api.onIslandPerform === 'function' ? api.onIslandPerform(perform) : () => {}
  // The banner and the stamp expire on the island's clock, but a digest
  // pushed while they are fresh must eventually be followed by one without
  // them, for an island that opens late.
  const sweep = window.setInterval(() => {
    if (done || justDone) schedule()
  }, 5000)
  flush()
  return () => {
    offStore()
    offVoice()
    offScheduler()
    offPerform()
    window.clearInterval(sweep)
    if (timer != null) window.clearTimeout(timer)
  }
}

function textOf(msg: Message | null): string {
  if (!msg) return ''
  return msg.parts
    .filter((p) => p.type === 'text')
    .map((p) => (p as { text?: string }).text ?? '')
    .join('\n')
}

/** Markdown, roughly flattened to prose for a one-line peek. */
function plain(md: string): string {
  return md
    .replace(/```[\s\S]*?```/g, ' ')
    .replace(/`([^`]*)`/g, '$1')
    .replace(/!\[[^\]]*\]\([^)]*\)/g, '')
    .replace(/\[([^\]]*)\]\([^)]*\)/g, '$1')
    .replace(/^#{1,6}\s+/gm, '')
    .replace(/^\s*[-*+]\s+/gm, '')
    .replace(/^\s*\d+\.\s+/gm, '')
    .replace(/[*_~]{1,3}/g, '')
    .replace(/\s+/g, ' ')
    .trim()
}

function head(text: string, n: number): string {
  return text.length > n ? `${text.slice(0, n - 1).trimEnd()}…` : text
}

function tail(text: string, n: number): string {
  return text.length > n ? `…${text.slice(text.length - n + 1).trimStart()}` : text
}

function statusFromSubtype(subtype: string): string | null {
  const s = subtype.toLowerCase()
  if (s.includes('complet') || s.includes('done') || s.includes('finish')) return 'done'
  if (s.includes('cancel')) return 'cancelled'
  if (s.includes('block')) return 'blocked'
  if (s.includes('start') || s.includes('activ') || s.includes('claim')) return 'active'
  return null
}
