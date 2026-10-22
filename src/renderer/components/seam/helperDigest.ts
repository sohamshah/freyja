import type { MessagePart, ToolCallRecord } from '@shared/events'
import type { SeamUnit } from '../../lib/seam'
import { callMs, isEdit, stepWords, wordsText } from './stepWords'
import { markKind } from './marks'
import type { MarkKind } from './marks'

/**
 * What a helper (sub-agent) has done so far, read from its own session:
 * each helper runs as a child session whose messages and tool calls stream
 * into the store live (into `sessionArchive[id]`, or the top-level fields
 * when the operator has it open).
 *
 * Returned as a JSON string so a selector can compare it by value: a
 * helper's row re-renders only when its own steps change, never on the
 * parent's text deltas. Cached on the slice's identity for the same reason.
 */

export interface HelperSteps {
  /** Finished steps, oldest first, capped to the most recent. */
  units: SeamUnit[]
  /** The step it is on now, in plain words, or '' when idle. */
  now: string
  /** The finished steps as words, for its reply. */
  steps: Array<{ kind: 'think' | 'step' | 'edit' | 'fail'; text: string; mark?: MarkKind }>
}

const MAX_UNITS = 24
const SPAWN = new Set(['sub_agent', 'subagent', 'spawn_subagent', 'subagent_spawn'])
const STEP_TITLE = /^[ \t]*\*\*([^*\n]{2,120})\*\*[ \t]*$/gm
const cache = new Map<string, { messages: unknown; toolCalls: unknown; streaming: unknown; out: string }>()

interface StoreLike {
  activeSessionId: string
  messages: Array<{ role: string; parts: MessagePart[] }>
  toolCalls: Record<string, ToolCallRecord>
  isStreaming: boolean
  sessionArchive: Record<string, { messages: Array<{ role: string; parts: MessagePart[] }>; toolCalls: Record<string, ToolCallRecord>; isStreaming?: boolean } | undefined>
}

function lastTitle(text: string): string {
  let t = ''
  for (const m of text.matchAll(STEP_TITLE)) t = m[1].trim()
  return t
}

export function helperDigest(s: StoreLike, id: string): string {
  const slice = id === s.activeSessionId ? { messages: s.messages, toolCalls: s.toolCalls, isStreaming: s.isStreaming } : s.sessionArchive[id]
  if (!slice) return ''
  const hit = cache.get(id)
  if (hit && hit.messages === slice.messages && hit.toolCalls === slice.toolCalls && hit.streaming === slice.isStreaming) return hit.out
  const units: SeamUnit[] = []
  const steps: HelperSteps['steps'] = []
  let now = ''
  const parts = slice.messages.filter((m) => m.role === 'assistant').flatMap((m) => m.parts)
  const t = Date.now()
  parts.forEach((part, i) => {
    const last = i === parts.length - 1
    if (part.type === 'thinking' && part.text) {
      const title = lastTitle(part.text)
      if (last && slice.isStreaming) {
        now = title ? `Thinking · ${title}` : 'Thinking'
        return
      }
      const secs = part.startedAt != null && part.endedAt != null ? Math.max(1, (part.endedAt - part.startedAt) / 1000) : 2
      units.push({ k: 'think', secs, label: title || 'thought' })
      steps.push({ kind: 'think', text: title || 'Thought it through', mark: 'think' })
      return
    }
    if (part.type === 'tool_call' && part.toolCallId) {
      const tc = slice.toolCalls[part.toolCallId]
      if (!tc || SPAWN.has(tc.name)) return
      const words = wordsText(stepWords(tc))
      if (tc.status === 'running') {
        now = words
        return
      }
      if (tc.status === 'error') {
        units.push({ k: 'fail', label: `${words} · failed` })
        steps.push({ kind: 'fail', text: words, mark: markKind(tc) })
      } else {
        units.push({ k: isEdit(tc) ? 'down' : 'up', ms: callMs(tc, t), label: words })
        steps.push({ kind: isEdit(tc) ? 'edit' : 'step', text: words, mark: markKind(tc) })
      }
      return
    }
    if (part.type === 'text' && last && slice.isStreaming) now = 'Writing its reply'
  })
  const out = JSON.stringify({ units: units.slice(-MAX_UNITS), now, steps: steps.slice(-40) } satisfies HelperSteps)
  cache.set(id, { messages: slice.messages, toolCalls: slice.toolCalls, streaming: slice.isStreaming, out })
  return out
}

export function parseDigest(digest: string): HelperSteps {
  if (!digest) return { units: [], now: '', steps: [] }
  try {
    return JSON.parse(digest) as HelperSteps
  } catch {
    return { units: [], now: '', steps: [] }
  }
}

/** A helper's reply in one line: its first sentence, markdown stripped. */
export function replyLine(result: string | undefined, failed: boolean): string {
  if (!result) return failed ? 'Stopped before it finished' : 'Done'
  const flat = result
    .replace(/<\/?subagent-report[^>]*>/g, '')
    .replace(/\[([^\]]+)\]\([^)]+\)/g, '$1')
    .replace(/[*_`#>]+/g, '')
    .replace(/\s+/g, ' ')
    .trim()
  const end = flat.search(/[.!?](\s|$)/)
  const first = end > 0 && end < 140 ? flat.slice(0, end) : flat.slice(0, 140)
  return first
}
