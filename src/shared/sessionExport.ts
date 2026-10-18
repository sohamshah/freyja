import type { Message, SubagentRecord, ToolCallRecord } from './events'
import { CALL_TOOL, resolveToolCall } from './toolCall'

// The session export: what happened, recorded as it happened.
//
// Two sources feed it. The app's own record (the renderer slice) is complete
// for the UI but not exact: the bridge cuts a tool result's preview to 2000
// characters, the app stores a tool call's arguments after path
// normalization, and a `call_tool` call kept only the inner tool's arguments
// until f71f478. The raw transcript (`raw_messages.jsonl`) holds the calls and
// results as the model sent and received them. The main process reads it and
// passes it in as a `ModelView`; without one, the export falls back to the
// app's record and says so on every field it could not check.

/** A tool call as the model sent it. */
export interface ModelCall {
  name: string
  arguments: Record<string, unknown>
}

/** A tool result as the model received it. */
export interface ModelResult {
  text: string
  /** `original`: the engine cut the result for the model, and the full output
   *  was read back from the file it saved. `transcript`: the transcript text
   *  is the whole result. `transcript-truncated`: the engine cut it and the
   *  saved file is gone, so only the cut text remains. */
  source: 'original' | 'transcript' | 'transcript-truncated'
  complete: boolean
  /** The size the engine reported for the uncut output, when it cut it. */
  originalChars?: number
}

/** Calls and results from the raw transcript, by tool call id. A result may
 *  be read from disk when it is asked for, so one is never held longer than
 *  the export needs it. */
export interface ModelView {
  calls: { get(id: string): ModelCall | undefined }
  results: { get(id: string): ModelResult | undefined }
}

/** The bridge cuts a result preview to 2000 characters and marks the cut. */
const PREVIEW_CUT = /\n\n… \[truncated \d+ chars\] …\n\n/

function sameJson(a: unknown, b: unknown): boolean {
  return JSON.stringify(a) === JSON.stringify(b)
}

/** Image metadata for an export. The bytes live in the media store; the raw
 *  transcript carries the images the model saw. */
function exportMedia<T extends { pngBase64?: string; byteSize?: number }>(
  media: T,
): Omit<T, 'pngBase64'> {
  const { pngBase64, ...meta } = media
  const byteSize = meta.byteSize ?? (pngBase64 ? Math.floor((pngBase64.length * 3) / 4) : undefined)
  return byteSize === undefined ? meta : { ...meta, byteSize }
}

/** One tool call as an export records it.
 *
 *  `name` and `arguments` are what the model sent, read from the raw
 *  transcript when there is one, so a `call_tool` call is
 *  `call_tool({tool_name, arguments})`. `argumentsSource` says where they came
 *  from: `transcript` (exact), `app` (the app's record: exact for a wrapper
 *  call, but for a direct call possibly after path normalization) or
 *  `rebuilt` (the wrapper args were lost, so they are rebuilt from `invokes`).
 *
 *  `invokes` is the tool that ran, and `executedArguments` its arguments as it
 *  ran them. They are present for a wrapped call, and for any call whose
 *  executed arguments differ from the ones the model sent.
 *
 *  `result` is the whole result as the model received it, and `resultSource`
 *  says how whole: see `ModelResult`, or `preview` for the bridge's
 *  2000-character cut when no transcript was read. */
export function exportToolCall(tc: ToolCallRecord, view?: ModelView) {
  const sent = view?.calls.get(tc.id)
  const name = sent?.name ?? tc.name
  const wrapperArgs = sent?.arguments ?? tc.wrapperArguments
  const invokes =
    tc.invokes ??
    (name === CALL_TOOL && typeof wrapperArgs?.tool_name === 'string' ? wrapperArgs.tool_name : undefined)
  // The first `tool_input_end` for the id, which held the model's wrapper
  // args, may be missing. `call_tool` has one fixed shape, so rebuild it.
  const rebuilt =
    !wrapperArgs && tc.invokes && name === CALL_TOOL
      ? { tool_name: tc.invokes, arguments: tc.arguments ?? {} }
      : undefined
  const argumentsAsSent = wrapperArgs ?? rebuilt ?? tc.arguments
  const argumentsSource = sent ? 'transcript' : rebuilt ? 'rebuilt' : 'app'
  const executedDiffers =
    tc.arguments !== undefined && (Boolean(invokes) || !sameJson(tc.arguments, argumentsAsSent))

  const full = view?.results.get(tc.id)
  const result = full ? full.text : tc.result
  const resultSource = full ? full.source : tc.result !== undefined ? 'preview' : undefined
  const resultComplete = full
    ? full.complete
    : tc.result !== undefined
      ? !PREVIEW_CUT.test(tc.result)
      : undefined

  return {
    id: tc.id,
    name,
    arguments: argumentsAsSent,
    argumentsSource,
    ...(invokes ? { invokes } : {}),
    ...(executedDiffers ? { executedArguments: tc.arguments } : {}),
    status: tc.status,
    result,
    resultSource,
    resultComplete,
    resultChars: result?.length,
    ...(full?.originalChars !== undefined ? { resultOriginalChars: full.originalChars } : {}),
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

/** The tool calls as an export records them, one at a time, so a session
 *  with large results never holds them all at once. */
export function* exportToolCalls(calls: Iterable<ToolCallRecord>, view?: ModelView) {
  for (const tc of calls) yield exportToolCall(tc, view)
}

/** Success and failure counts by the tool that ran, so a `call_tool` call
 *  counts for the tool behind it instead of for the wrapper. A record from
 *  before the app kept the tool's name is read from the transcript. */
export function exportToolStats(calls: Iterable<ToolCallRecord>, view?: ModelView) {
  const stats: Record<string, { count: number; success: number; failure: number }> = {}
  for (const tc of calls) {
    const own = resolveToolCall(tc).name
    const sent = own === CALL_TOOL ? view?.calls.get(tc.id) : undefined
    const name = sent ? resolveToolCall({ name: sent.name, arguments: sent.arguments }).name : own
    const entry = (stats[name] ??= { count: 0, success: 0, failure: 0 })
    entry.count += 1
    if (tc.isError) entry.failure += 1
    else if (tc.status === 'success') entry.success += 1
  }
  return stats
}

/** The system event fields the export reads. */
export interface ExportSystemEvent {
  at: number
  subtype?: string
  message?: string
  details?: unknown
}

/** The slice fields the export reads. The renderer's `SessionSlice` fits. */
export interface ExportSlice {
  systemPrompt?: string | null
  reasoningLevel?: string
  messages: Message[]
  toolCalls: Record<string, ToolCallRecord>
  toolCallOrder: string[]
  subagents: Record<string, SubagentRecord>
  usage: unknown
  systemEvents: ExportSystemEvent[]
}

/** The session row fields the export reads. */
export interface ExportSessionMeta {
  id: string
  title?: string
  model?: string
  reasoningLevel?: string
  workspace?: string
  createdAt?: number
  updatedAt?: number
  parentSessionId?: string
  childSessionIds?: string[]
  messageCount?: number
  totalInputTokens?: number
  totalOutputTokens?: number
  cacheReadTokens?: number
}

// EVERYTHING IS CHRONOLOGICAL (oldest → newest) — matches `verdictHistory`
// on the bridge and reads top-to-bottom like a session transcript.
// Future analyzers (including any LLM asked to summarize a goal run)
// should iterate the verdicts array in array order to follow the loop
// forward in time.
function collectGoalLoopForExport(events: ExportSystemEvent[] | undefined): {
  goalState: Record<string, unknown> | null
  brief: Record<string, unknown> | null
  // Last 12 verdicts in chronological order (matches the bridge's
  // verdict_history field). Provided as a stable per-event snapshot.
  verdictHistory: Record<string, unknown>[]
  // Every goal_judge event's full verdict, chronological. Preserved
  // even if the rolling 100-event renderer buffer dropped early ones,
  // because the bridge's verdictHistory carries them through.
  verdicts: Array<{
    at: number
    turnsUsed: number | null
    verdict: Record<string, unknown>
  }>
  // Convenience: every goal event in chronological order. Useful for
  // reconstructing the timeline (set / continue / pause / done) when
  // analyzing a session externally.
  events: Array<{ at: number; subtype: string; message: string }>
} | null {
  if (!events || events.length === 0) return null
  const goalEvents = events
    .filter((e) => (e.subtype ?? '').startsWith('goal_'))
    .sort((a, b) => a.at - b.at) // chronological
  if (goalEvents.length === 0) return null
  const latest = goalEvents[goalEvents.length - 1]
  const latestDetails = (latest.details as Record<string, unknown> | undefined) ?? {}
  const judgeEvents = goalEvents.filter((e) => e.subtype === 'goal_judge')
  return {
    goalState: (latestDetails.goalState as Record<string, unknown> | null) ?? null,
    brief: (latestDetails.judgeRules as Record<string, unknown> | null) ?? null,
    verdictHistory:
      (latestDetails.verdictHistory as Record<string, unknown>[] | undefined) ?? [],
    verdicts: judgeEvents.map((e) => {
      const d = (e.details as Record<string, unknown> | undefined) ?? {}
      const gs = d.goalState as Record<string, unknown> | undefined
      return {
        at: e.at,
        turnsUsed: typeof gs?.turnsUsed === 'number' ? (gs.turnsUsed as number) : null,
        verdict: (d.verdict as Record<string, unknown>) ?? {},
      }
    }),
    events: goalEvents.map((e) => ({
      at: e.at,
      subtype: e.subtype ?? '',
      message: e.message ?? '',
    })),
  }
}

/** The export, version 5. See docs/TRAJECTORY-TRAINING.md. `toolCalls` is a
 *  generator: write it with a streaming writer, or spread it.
 *
 *  Nothing in it is cut: messages, thinking and tool results are whole. The
 *  one cap left is `systemEvents`, which is the app's rolling buffer of the
 *  last 100; the full compaction, pressure and tool-call records are in the
 *  telemetry the main process adds beside it. */
export function buildSessionExport(
  meta: ExportSessionMeta,
  slice: ExportSlice,
  view?: ModelView,
) {
  const toolCalls = Object.values(slice.toolCalls)

  // Extract the initial user message as the "task description"
  // (used for RFT pair generation and RL reward assignment)
  const firstUserMsg = slice.messages.find((m) => m.role === 'user')
  const taskDescription =
    firstUserMsg?.parts
      .filter((p) => p.type === 'text')
      .map((p) => p.text)
      .join('\n') ?? ''

  // Extract thinking traces from message parts
  const thinkingTraces = slice.messages
    .filter((m) => m.role === 'assistant')
    .flatMap((m) =>
      m.parts
        .filter((p) => p.type === 'thinking' && p.text)
        .map((p) => ({ messageId: m.id, thinking: p.text })),
    )

  return {
    // 5: tool calls are as the model sent them and results are whole (see
    // `exportToolCall`); `invokedArguments` is now `executedArguments`.
    // 4: a `call_tool` call is `call_tool({tool_name, arguments})`.
    version: 5,
    exportedAt: new Date().toISOString(),
    app: 'freyja',
    // ─── System prompt (critical for SFT — the model needs to
    //     know what instructions it was following) ─────────────
    systemPrompt: slice.systemPrompt ?? null,
    // ─── Session metadata ────────────────────────────────────
    session: {
      id: meta.id,
      title: meta.title,
      model: meta.model,
      reasoningLevel: slice.reasoningLevel || meta.reasoningLevel,
      workspace: meta.workspace,
      createdAt: meta.createdAt,
      updatedAt: meta.updatedAt,
      parentSessionId: meta.parentSessionId,
      childSessionIds: meta.childSessionIds,
      messageCount: meta.messageCount,
      totalInputTokens: meta.totalInputTokens,
      totalOutputTokens: meta.totalOutputTokens,
      cacheReadTokens: meta.cacheReadTokens,
    },
    // ─── Training-relevant fields ────────────────────────────
    // Initial task/prompt for RFT pairing and reward assignment
    taskDescription,
    // Per-tool success/failure for quality filtering (Hermes pattern)
    toolStats: exportToolStats(toolCalls, view),
    // Thinking traces (ATLaS: training on reasoning = 3x better)
    thinkingTraces,
    // ─── Full conversation ───────────────────────────────────
    messages: slice.messages.map((m) => ({
      id: m.id,
      role: m.role,
      parts: m.parts,
      createdAt: m.createdAt,
      inputTokens: m.inputTokens,
      outputTokens: m.outputTokens,
      attachments: m.attachments,
    })),
    // ─── Tool calls (trajectory actions) ─────────────────────
    toolCalls: exportToolCalls(toolCalls, view),
    toolCallOrder: slice.toolCallOrder,
    // ─── Sub-agents (multi-agent trajectories) ───────────────
    subagents: Object.values(slice.subagents).map((sa) => ({
      id: sa.id,
      label: sa.label,
      mode: sa.mode,
      state: sa.state,
      task: sa.task,
      agentType: sa.agentType,
      artifactPath: sa.artifactPath,
      startedAt: sa.startedAt,
      elapsedMs: sa.elapsedMs,
      tokensIn: sa.tokensIn,
      tokensOut: sa.tokensOut,
      toolsCalled: sa.toolsCalled,
      result: sa.result,
    })),
    // ─── Aggregated metrics ──────────────────────────────────
    usage: slice.usage,
    systemEvents: slice.systemEvents,
    // ─── Goal loop snapshot ──────────────────────────────────
    // Lifts goal state / brief / verdict history out of the
    // event stream into a dedicated block so they survive the
    // rolling 100-event buffer and analyzers don't have to scan
    // systemEvents to find them. Pulled from the most recent
    // goal_* event's payload (every goal event includes them).
    goalLoop: collectGoalLoopForExport(slice.systemEvents),
  }
}

export type SessionExport = ReturnType<typeof buildSessionExport>
