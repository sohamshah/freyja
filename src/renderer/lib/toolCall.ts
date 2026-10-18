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

/** Image metadata for an export. The bytes live in the media store, not in
 *  the file, so an export carries size and kind instead. */
function exportMedia<T extends { pngBase64?: string; byteSize?: number }>(
  media: T,
): Omit<T, 'pngBase64'> {
  const { pngBase64, ...meta } = media
  const byteSize = meta.byteSize ?? (pngBase64 ? Math.floor((pngBase64.length * 3) / 4) : undefined)
  return byteSize === undefined ? meta : { ...meta, byteSize }
}

/** One tool call as an export records it: `name` and `arguments` are what
 *  the model sent, so a `call_tool` call is `call_tool({tool_name, arguments})`
 *  as in the transcript. A wrapped call adds `invokes` (the tool that ran) and
 *  `invokedArguments` (its arguments as it ran them, after path
 *  normalization). Without them an export pairs `call_tool` with the inner
 *  tool's arguments, which no model call ever had. */
export function exportToolCall(tc: ToolCallRecord) {
  const wrapped = Boolean(tc.invokes)
  // The first `tool_input_end` for the id, which holds the model's wrapper
  // args, may be missing. `call_tool` has one fixed shape, so rebuild it and
  // say so.
  const rebuilt =
    wrapped && !tc.wrapperArguments && tc.name === CALL_TOOL
      ? { tool_name: tc.invokes, arguments: tc.arguments ?? {} }
      : undefined
  return {
    id: tc.id,
    name: tc.name,
    arguments: tc.wrapperArguments ?? rebuilt ?? tc.arguments,
    ...(rebuilt ? { argumentsRebuilt: true } : {}),
    ...(wrapped ? { invokes: tc.invokes, invokedArguments: tc.arguments } : {}),
    status: tc.status,
    result: tc.result,
    isError: tc.isError,
    durationMs: tc.durationMs,
    startedAt: tc.startedAt,
    groupId: tc.groupId,
    partialJson: tc.partialJson,
    frame: tc.frame ? exportMedia(tc.frame) : undefined,
    resultImages: tc.resultImages?.map((image) => exportMedia(image)),
    fileChangeSet: tc.fileChangeSet,
  }
}

/** Success and failure counts by the tool that ran, so a `call_tool` call
 *  counts for the tool behind it instead of for the wrapper. */
export function exportToolStats(calls: Iterable<ToolCallRecord>) {
  const stats: Record<string, { count: number; success: number; failure: number }> = {}
  for (const tc of calls) {
    const name = resolveToolCall(tc).name
    const entry = (stats[name] ??= { count: 0, success: 0, failure: 0 })
    entry.count += 1
    if (tc.isError) entry.failure += 1
    else if (tc.status === 'success') entry.success += 1
  }
  return stats
}
