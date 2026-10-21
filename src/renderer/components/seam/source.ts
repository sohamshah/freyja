import { createContext, useContext } from 'react'
import type { MessagePart, SubagentRecord, ToolCallRecord } from '@shared/events'
import type { StretchItem } from './stepWords'

/**
 * Which session a stretch is drawn for. The main conversation shows the
 * active session, whose records live in the store's top-level fields; a
 * split pane shows another session, whose records live in its archived
 * slice. Components read through these helpers so the same stretch draws
 * in either place.
 */
export interface WorkView {
  /** The session drawn, or null for the active one. */
  sessionId: string | null
  /** Smaller type for split panes. */
  compact: boolean
}

export const WorkViewContext = createContext<WorkView>({ sessionId: null, compact: false })
export const useWorkView = (): WorkView => useContext(WorkViewContext)

interface StoreLike {
  activeSessionId: string
  toolCalls: Record<string, ToolCallRecord>
  subagents: Record<string, SubagentRecord>
  widgets: Record<string, unknown>
  sessionArchive: Record<string, { toolCalls?: Record<string, ToolCallRecord>; subagents?: Record<string, SubagentRecord>; widgets?: Record<string, unknown> } | undefined>
}

const EMPTY = {}

const archived = (s: StoreLike, sid: string | null) => (sid && sid !== s.activeSessionId ? s.sessionArchive[sid] : undefined)

export function callsOf(s: StoreLike, sid: string | null): Record<string, ToolCallRecord> {
  const a = archived(s, sid)
  return sid && sid !== s.activeSessionId ? a?.toolCalls ?? EMPTY : s.toolCalls
}

export function subagentsOf(s: StoreLike, sid: string | null): Record<string, SubagentRecord> {
  const a = archived(s, sid)
  return sid && sid !== s.activeSessionId ? a?.subagents ?? EMPTY : s.subagents
}

const SPAWN_TOOLS = new Set(['sub_agent', 'subagent', 'spawn_subagent', 'subagent_spawn'])

/** Calls issued in one response start streaming within a moment of each
 *  other; a later response comes after a model round trip. */
const TOGETHER_MS = 2500

export type Block =
  | { kind: 'text' | 'other'; part: MessagePart; index: number }
  | { kind: 'stretch'; key: string; items: StretchItem[] }
  | { kind: 'widget'; toolCallId: string; index: number }

/** Split a message into paragraphs and the stretches of work between them.
 *  Thinking, tool calls and helpers join the open stretch; a paragraph, a
 *  widget or a system note closes it. Calls that ran together (one
 *  `groupId`, started within moments) become one item. Spawn calls are
 *  dropped: the helper they started stands for them. */
export function buildBlocks(parts: MessagePart[], toolCalls: Record<string, ToolCallRecord>, widgets: Record<string, unknown>): Block[] {
  const out: Block[] = []
  let stretch: { kind: 'stretch'; key: string; items: StretchItem[] } | null = null
  const open = (index: number) => {
    if (!stretch) {
      stretch = { kind: 'stretch', key: `s${index}`, items: [] }
      out.push(stretch)
    }
    return stretch
  }
  parts.forEach((part, index) => {
    if (part.type === 'text') {
      if (!(part.text ?? '').trim()) return
      stretch = null
      out.push({ kind: 'text', part, index })
      return
    }
    if (part.type === 'thinking') {
      open(index).items.push({ kind: 'thought', part, index })
      return
    }
    if (part.type === 'subagent' && part.subagentId) {
      const items = open(index).items
      const last = items[items.length - 1]
      if (last?.kind === 'helpers') last.ids.push(part.subagentId)
      else items.push({ kind: 'helpers', ids: [part.subagentId] })
      return
    }
    if (part.type === 'tool_call' && part.toolCallId) {
      const id = part.toolCallId
      const tc = toolCalls[id]
      if (tc && SPAWN_TOOLS.has(tc.name)) return
      // Generative-UI widgets are the artifact itself: they stand on their
      // own between paragraphs. An errored call (no widget) is a step.
      if (tc?.name === 'show_widget' && (widgets[id] || tc.status === 'running')) {
        stretch = null
        out.push({ kind: 'widget', toolCallId: id, index })
        return
      }
      const items = open(index).items
      const last = items[items.length - 1]
      // Calls that ran together share a groupId. Transcripts saved before
      // the store kept sequential calls apart can share one across separate
      // responses, so a call also has to start within moments of the one
      // before it.
      const prevId = last?.kind === 'call' ? last.id : last?.kind === 'calls' ? last.ids[last.ids.length - 1] : undefined
      const prev = prevId ? toolCalls[prevId] : undefined
      const together = !!tc?.groupId && !!prev && prev.groupId === tc.groupId && tc.startedAt - prev.startedAt <= TOGETHER_MS
      if (together && last?.kind === 'calls') {
        last.ids.push(id)
        return
      }
      if (together && last?.kind === 'call') {
        items[items.length - 1] = { kind: 'calls', ids: [last.id, id], groupId: tc!.groupId }
        return
      }
      items.push({ kind: 'call', id })
      return
    }
    stretch = null
    out.push({ kind: 'other', part, index })
  })
  return out
}
