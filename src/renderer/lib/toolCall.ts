import type { ToolCallRecord } from '@shared/events'

/** The wrapper tool that runs a deferred tool by name. */
export const CALL_TOOL = 'call_tool'

/** What a call is, for display: the tool that runs, with its own arguments. */
export interface ResolvedToolCall {
  name: string
  arguments?: Record<string, unknown>
  partialJson?: string
}

type ResolvableCall = Pick<ToolCallRecord, 'name' | 'arguments' | 'partialJson' | 'invokes'>

/** A string value from partial JSON, once its closing quote has streamed. */
function grabClosedString(partial: string, key: string): string | undefined {
  const m = partial.match(new RegExp(`"${key}"\\s*:\\s*"((?:\\\\.|[^"\\\\])*)"`))
  if (!m) return undefined
  try {
    return JSON.parse(`"${m[1]}"`) as string
  } catch {
    return m[1]
  }
}

/** The inner `arguments` of a `call_tool` call. A model may send it as an
 *  object or as a JSON string; the bridge accepts both. */
function innerArguments(value: unknown): Record<string, unknown> | undefined {
  if (value && typeof value === 'object' && !Array.isArray(value)) {
    return value as Record<string, unknown>
  }
  if (typeof value === 'string') {
    try {
      const parsed: unknown = JSON.parse(value)
      if (parsed && typeof parsed === 'object' && !Array.isArray(parsed)) {
        return parsed as Record<string, unknown>
      }
    } catch {
      // Still streaming, or not JSON. The header falls back to the tool name.
    }
  }
  return undefined
}

/** Resolve the tool a call runs. `call_tool` is a wrapper: its header should
 *  name the downstream tool, and summarize that tool's arguments.
 *
 *  A wrapped call is in one of three states, and each carries the name
 *  somewhere else:
 *  - Streaming: `partialJson` holds `{"tool_name": ..., "arguments": {...`.
 *  - Issued: `arguments` is the wrapper's `{tool_name, arguments}`.
 *  - Running or done: the bridge re-enters the tool path for the inner tool
 *    under the same id, which replaces `arguments` with the inner tool's own
 *    and sets `invokes` to its name. */
export function resolveToolCall(call: ResolvableCall): ResolvedToolCall {
  const plain: ResolvedToolCall = {
    name: call.name,
    arguments: call.arguments,
    partialJson: call.partialJson,
  }
  if (call.invokes) return { ...plain, name: call.invokes }
  if (call.name !== CALL_TOOL) return plain

  const args = call.arguments
  if (args) {
    const target = typeof args.tool_name === 'string' ? args.tool_name.trim() : ''
    if (!target) return plain
    return { name: target, arguments: innerArguments(args.arguments) }
  }

  const partial = call.partialJson
  if (partial) {
    const target = grabClosedString(partial, 'tool_name')?.trim()
    if (!target) return plain
    const argsAt = partial.match(/"arguments"\s*:/)
    const inner = argsAt?.index != null ? partial.slice(argsAt.index + argsAt[0].length) : ''
    return { name: target, partialJson: inner || undefined }
  }
  return plain
}
