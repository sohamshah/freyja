/**
 * Per-turn token, cache and cost ledger for one session, built from the
 * session's `<id>.events.jsonl`.
 *
 * The number to watch is `reread`: input the provider had already been sent,
 * sent again uncached because something earlier in the prompt changed. OpenAI
 * resumes a cached prefix only where an earlier request ended, so one edit to
 * old history makes the next call pay the cache-write rate for everything
 * after it. A healthy turn re-reads close to nothing; a turn whose system
 * prompt, tool list or old messages changed re-reads the whole context.
 */

export interface LedgerCall {
  /** ms since epoch */
  t: number
  /** `main` is the agent loop's own calls. The others ride along (compaction
   *  and working memory, cache keep-alives between turns): they count in the
   *  turn's totals but are not the prompt the next main call extends. */
  kind: 'main' | 'summarizer' | 'keepalive'
  model: string
  /** Fresh input: neither read from nor written to the cache. */
  input: number
  cacheRead: number
  cacheWrite: number
  output: number
  cost: number
  /** Input sent uncached that an earlier call had already sent. */
  reread: number
  /** Why the re-read happened, when the event log says. */
  cause: string | null
}

export interface LedgerTurn {
  /** 1-based, in log order (turn ids restart after an app restart). */
  n: number
  turnId: string
  t: number
  calls: LedgerCall[]
  input: number
  cacheRead: number
  cacheWrite: number
  output: number
  cost: number
  reread: number
  models: string[]
  /** What started the turn when it was not the operator: a sub-agent memo,
   *  a talk message (`memo from <label>`). */
  trigger: string | null
}

/** Lines worth parsing; everything else is skipped before JSON.parse. */
const NEEDLES = [
  'llm_call_metric',
  'turn_start',
  'media_pruning',
  'compaction_complete',
  'context_pruning',
  'model_changed',
  'transcript_restored',
  'loaded and ready',
  'inbox_injected',
]

/** Below this, a re-read is noise (a reminder block, a changed tail). */
const REREAD_FLOOR = 2_000

const MIN = 60_000

/** How long an idle prompt cache survives: Anthropic's default breakpoints
 *  last 5 minutes, OpenAI's GPT-5.6+ caches 30 minutes after last use. */
function cacheLifetimeMs(model: string): number {
  return model.startsWith('claude') ? 5 * MIN : 30 * MIN
}

function num(v: unknown): number {
  const n = typeof v === 'number' ? v : Number(v)
  return Number.isFinite(n) ? n : 0
}

export function buildTurnLedger(eventsJsonl: string): LedgerTurn[] {
  const turns: LedgerTurn[] = []
  let current: LedgerTurn | null = null
  let prevPrompt: number | null = null
  let prevT: number | null = null
  let mainCallsInTurn = 0
  let pendingTrigger: string | null = null
  const marks = new Set<string>()

  const openTurn = (turnId: string, t: number): LedgerTurn => {
    const turn: LedgerTurn = {
      n: turns.length + 1,
      turnId,
      t,
      calls: [],
      input: 0,
      cacheRead: 0,
      cacheWrite: 0,
      output: 0,
      cost: 0,
      reread: 0,
      models: [],
      trigger: null,
    }
    turns.push(turn)
    return turn
  }

  for (const line of eventsJsonl.split('\n')) {
    if (!line || !NEEDLES.some((n) => line.includes(n))) continue
    let ev: Record<string, any>
    try {
      ev = JSON.parse(line)
    } catch {
      continue
    }
    const type = String(ev.type ?? '')
    const t = num(ev._t)
    if (type === 'turn_start') {
      current = openTurn(String(ev.turnId ?? ''), t)
      current.trigger = pendingTrigger
      pendingTrigger = null
      mainCallsInTurn = 0
      marks.add('turn')
      continue
    }
    if (type === 'inbox_injected') {
      // Delivered at a turn boundary: it is what woke the agent, unless the
      // operator's own message started the turn (the view checks that).
      const item = Array.isArray(ev.items) ? ev.items[0] : null
      if (ev.midTurn || !item) continue
      const from = String(item.fromLabel ?? '').trim()
      const label = `${String(item.kind ?? 'message')}${from ? ` from ${from}` : ''}`
      if (current && current.calls.length === 0 && !current.trigger) current.trigger = label
      else pendingTrigger = label
      continue
    }
    if (type === 'system_event') {
      const sub = String(ev.subtype ?? '')
      if (sub === 'media_pruning') marks.add('pruning')
      else if (sub === 'compaction_complete' || sub === 'context_pruning') marks.add('compaction')
      else if (sub === 'model_changed' || sub === 'transcript_restored') marks.add('restart')
      continue
    }
    if (type === 'tool_result') {
      if (String(ev.preview ?? '').includes('loaded and ready')) marks.add('tools')
      continue
    }
    if (type !== 'llm_call_metric') continue

    const callKind = String(ev.callKind ?? 'main')
    const kind: LedgerCall['kind'] =
      callKind === 'main' ? 'main' : callKind === 'cache_keepalive' ? 'keepalive' : 'summarizer'
    const model = String(ev.model ?? '')
    const call: LedgerCall = {
      t,
      kind,
      model,
      input: num(ev.inputTokens),
      cacheRead: num(ev.cacheReadTokens),
      cacheWrite: num(ev.cacheWriteTokens),
      output: num(ev.outputTokens),
      cost: num(ev.costUsd),
      reread: 0,
      cause: null,
    }
    if (kind === 'main') {
      mainCallsInTurn += 1
      const prompt = call.input + call.cacheRead + call.cacheWrite
      if (prevPrompt !== null) {
        // Everything beyond the prompt's growth since the last call was
        // already sent once.
        const growth = Math.max(0, prompt - prevPrompt)
        call.reread = Math.max(0, call.input + call.cacheWrite - growth)
        if (call.reread >= REREAD_FLOOR) {
          const idle = prevT !== null ? t - prevT : 0
          call.cause = marks.has('restart')
            ? 'model switch or restart'
            : marks.has('compaction')
              ? 'compaction rewrote history'
              : marks.has('tools')
                ? 'tool list changed'
                : marks.has('pruning')
                  ? 'screenshot pruning edited history'
                  : idle > cacheLifetimeMs(model)
                    ? `cache expired after ${Math.round(idle / MIN)}m idle`
                    : marks.has('turn')
                      ? 'system prompt changed at turn start'
                      : mainCallsInTurn === 2
                        ? "the turn's first request was not reused"
                        : 'earlier prompt changed'
        } else {
          call.reread = 0
        }
      }
      prevPrompt = prompt
      prevT = t
      marks.clear()
    }
    const turn: LedgerTurn = current ?? openTurn('', t)
    current = turn
    turn.calls.push(call)
    turn.input += call.input
    turn.cacheRead += call.cacheRead
    turn.cacheWrite += call.cacheWrite
    turn.output += call.output
    turn.cost += call.cost
    turn.reread += call.reread
    if (model && !turn.models.includes(model)) turn.models.push(model)
  }
  return turns.filter((turn) => turn.calls.length > 0)
}
