// What the transcript shows when a stream goes wrong: a model call retried
// after failing mid-stream, a tool call that never ran, an error from a
// background session, a bridge that restarted mid-turn, and events for a
// session whose history is not loaded yet. Drives the real `handleEvent`
// with what the bridge sends. Run from the repo root:
//   npx tsx test-stream-recovery.mjs
import assert from 'node:assert/strict'
import { flushScheduledPersists, useHarness } from './src/renderer/state/store.ts'

const saves = []
let diskSlices = {}
let loadDelayMs = 0
globalThis.window = globalThis.window ?? {}
window.harness = {
  sendCommand: async () => {},
  sessionSave: async (payload) => { saves.push(payload); return { ok: true } },
  sessionIndexSave: async () => ({ ok: true }),
  sessionLoad: async (id) => {
    await new Promise((r) => setTimeout(r, loadDelayMs))
    return diskSlices[id] ? { ok: true, session: { slice: structuredClone(diskSlices[id]) } } : { ok: false }
  },
}

const h = () => useHarness.getState()
const emit = (ev) => h().handleEvent(ev)
const sleep = (ms) => new Promise((r) => setTimeout(r, ms))

const row = (id, extra = {}) => ({
  id, title: id, workspace: '~/', model: 'claude-sonnet-4-6', reasoningLevel: 'medium',
  coordinationStrategy: 'bus', createdAt: 1, updatedAt: 1, messageCount: 0,
  totalInputTokens: 0, totalOutputTokens: 0, cacheReadTokens: 0, ...extra,
})
const slice = (messages = []) => ({
  messages, currentStreamingMessageId: null, currentTurnId: null, thinking: '', isStreaming: false,
  toolCalls: {}, toolCallOrder: [], fileChanges: [], subagents: {}, subagentOrder: [],
  usage: {
    currentContextTokens: 0, totalInputTokens: 0, totalOutputTokens: 0, totalCacheReadTokens: 0,
    totalCacheWriteTokens: 0, totalCost: 0, lastTurnInputTokens: 0, lastTurnOutputTokens: 0,
    contextWindow: 200000,
  },
  systemEvents: [], kanbanCards: {}, busMessages: [], inboxEvents: [], artifacts: [], widgets: {},
  autoDispatchEnabled: false, model: 'claude-sonnet-4-6', reasoningLevel: 'medium',
  coordinationStrategy: 'bus', runtime: 'native',
})
const msg = (id, role, text) => ({ id, role, parts: [{ type: 'text', text }], createdAt: 1 })

function seed(extra = {}) {
  saves.length = 0
  diskSlices = {}
  loadDelayMs = 0
  useHarness.setState({
    ...slice([msg('u1', 'user', 'go')]),
    activeSessionId: 'p',
    sessionArchive: {},
    sessions: [row('p', { messageCount: 1 })],
    toast: null,
    ...extra,
  })
}
const P = 'p'
const streaming = () => h().messages.find((m) => m.id === h().currentStreamingMessageId)
const shape = (m) => m.parts.map((p) => (p.type === 'tool_call' ? `tool:${p.toolCallId}` : `${p.type}:${p.text}`))

let failures = 0
async function check(name, fn) {
  try {
    await fn()
    console.log(`ok   ${name}`)
  } catch (err) {
    failures++
    console.log(`FAIL ${name}\n     ${err.message.split('\n').join('\n     ')}`)
  }
}

// ── Retry after a mid-stream failure ─────────────────────────────────────

await check('a retried model call replaces what the failed one streamed', () => {
  seed()
  emit({ type: 'turn_start', sessionId: P, turnId: 't1' })
  emit({ type: 'stream_start', sessionId: P })
  emit({ type: 'thinking_delta', sessionId: P, thinking: 'Plan A' })
  emit({ type: 'tool_use_start', sessionId: P, id: 'tc0', name: 'bash' })
  emit({ type: 'tool_result', sessionId: P, id: 'tc0', preview: 'ok', isError: false, durationMs: 3 })
  // Second call: streams, then the connection drops.
  emit({ type: 'stream_start', sessionId: P })
  emit({ type: 'thinking_delta', sessionId: P, thinking: 'Plan B, half' })
  emit({ type: 'text_delta', sessionId: P, text: 'The answ' })
  emit({ type: 'tool_use_start', sessionId: P, id: 'tc1', name: 'bash' })
  // Retry.
  emit({ type: 'stream_reset', sessionId: P })
  emit({ type: 'stream_start', sessionId: P })
  emit({ type: 'thinking_delta', sessionId: P, thinking: 'Plan B' })
  emit({ type: 'text_delta', sessionId: P, text: 'The answer.' })
  assert.deepEqual(shape(streaming()), ['thinking:Plan A', 'tool:tc0', 'thinking:Plan B', 'text:The answer.'])
  assert.equal(h().toolCalls.tc1, undefined, 'never-run chip dropped')
  assert.ok(!h().toolCallOrder.includes('tc1'))
  assert.equal(h().toolCalls.tc0.status, 'success')
  assert.equal(h().thinking, 'Plan APlan B')
})

await check('a reset cuts text the failed call appended to an earlier part', () => {
  seed()
  emit({ type: 'turn_start', sessionId: P, turnId: 't1' })
  emit({ type: 'stream_start', sessionId: P })
  emit({ type: 'text_delta', sessionId: P, text: 'Done.' })
  // A verification step continues straight on from that text.
  emit({ type: 'stream_start', sessionId: P })
  emit({ type: 'text_delta', sessionId: P, text: ' I checked ha' })
  emit({ type: 'stream_reset', sessionId: P })
  emit({ type: 'stream_start', sessionId: P })
  emit({ type: 'text_delta', sessionId: P, text: ' I checked it.' })
  assert.deepEqual(shape(streaming()), ['text:Done. I checked it.'])
})

await check('a reset never touches an earlier message', () => {
  seed()
  emit({ type: 'turn_start', sessionId: P, turnId: 't1' })
  emit({ type: 'stream_start', sessionId: P })
  emit({ type: 'text_delta', sessionId: P, text: 'Partial before the final failure' })
  emit({ type: 'turn_complete', sessionId: P, turnId: 't1', success: false })
  const before = JSON.stringify(h().messages)
  emit({ type: 'turn_start', sessionId: P, turnId: 't2' })
  emit({ type: 'stream_reset', sessionId: P })
  assert.equal(JSON.stringify(h().messages.slice(0, -1)), before)
  assert.equal(h().messages.find((m) => m.parts.some((p) => p.text === 'Partial before the final failure')).attemptMark, undefined)
})

// ── Turn end settles chips that never ran ───────────────────────────────

await check('a tool chip still spinning at turn_complete is settled', () => {
  seed()
  emit({ type: 'turn_start', sessionId: P, turnId: 't1' })
  emit({ type: 'tool_use_start', sessionId: P, id: 'cut', name: 'write_file' })
  emit({ type: 'tool_use_start', sessionId: P, id: 'ran', name: 'bash' })
  emit({ type: 'tool_result', sessionId: P, id: 'ran', preview: 'ok', isError: false, durationMs: 1 })
  emit({ type: 'turn_complete', sessionId: P, turnId: 't1', success: true })
  assert.equal(h().toolCalls.cut.status, 'error')
  assert.match(h().toolCalls.cut.result, /Not run/)
  assert.equal(h().toolCalls.ran.status, 'success')
})

// ── Errors reach only the session they name ─────────────────────────────

await check("a background session's error leaves the reply on screen streaming", () => {
  seed({ sessionArchive: { kid: slice([msg('k1', 'user', 'x')]) }, sessions: [row('p'), row('kid', { parentSessionId: 'p' })] })
  emit({ type: 'turn_start', sessionId: P, turnId: 't1' })
  emit({ type: 'turn_start', sessionId: 'kid', turnId: 'k' })
  emit({ type: 'error', sessionId: 'kid', message: 'turn failed: boom' })
  assert.equal(h().isStreaming, true)
  emit({ type: 'text_delta', sessionId: P, text: 'still here' })
  assert.deepEqual(shape(streaming()), ['text:still here'])
  const kid = h().sessionArchive.kid
  assert.equal(kid.isStreaming, false)
  assert.equal(kid.messages.at(-1).parts.at(-1).systemSubtype, 'error')
})

await check('an error with no session is a notice, not the end of the turn', () => {
  seed()
  emit({ type: 'turn_start', sessionId: P, turnId: 't1' })
  emit({ type: 'error', message: 'Could not restart sub-agent x' })
  assert.equal(h().isStreaming, true)
  assert.equal(h().toast?.message, 'Could not restart sub-agent x')
})

await check("the active session's own error ends its turn", () => {
  seed()
  emit({ type: 'turn_start', sessionId: P, turnId: 't1' })
  emit({ type: 'error', sessionId: P, message: 'turn failed: boom' })
  assert.equal(h().isStreaming, false)
  assert.equal(h().messages.at(-1).parts.at(-1).text, 'turn failed: boom')
})

// ── A new bridge ends the old bridge's turns ────────────────────────────

await check('ready ends every turn still marked live, and settles its chips', () => {
  seed({ sessionArchive: { kid: slice([msg('k1', 'user', 'x')]) }, sessions: [row('p', { messageCount: 1 }), row('kid', { parentSessionId: 'p' })] })
  emit({ type: 'turn_start', sessionId: P, turnId: 't1' })
  emit({ type: 'tool_use_start', sessionId: P, id: 'spin', name: 'bash' })
  emit({ type: 'turn_start', sessionId: 'kid', turnId: 'k' })
  emit({ type: 'ready', sessionId: 'desktop-new', mode: 'live', capabilities: {} })
  // The real conversation is parked under its own id; a fresh one opens.
  const parked = h().sessionArchive.p
  assert.equal(parked.isStreaming, false)
  assert.equal(parked.currentStreamingMessageId, null)
  assert.equal(parked.toolCalls.spin.status, 'error')
  assert.equal(h().sessionArchive.kid.isStreaming, false)
})

await check('a transcript loaded later that a dead bridge left "live" is ended', async () => {
  // `ready` above set the bridge start time; this turn began before it.
  const stale = slice([msg('u', 'user', 'go'), { id: 'a1', role: 'assistant', parts: [{ type: 'text', text: 'half' }], createdAt: 5 }])
  stale.isStreaming = true
  stale.currentStreamingMessageId = 'a1'
  stale.currentTurnId = 't'
  diskSlices = { old: stale }
  useHarness.setState({ sessions: [...h().sessions, row('old', { messageCount: 2 })] })
  assert.equal(await h().loadPersistedSessionIntoArchive('old'), true)
  assert.equal(h().sessionArchive.old.isStreaming, false)
})

// ── Events for a session whose history is not loaded ────────────────────

await check('events for an unloaded session wait for its history, then apply on top', async () => {
  seed({ sessions: [row('p', { messageCount: 1 }), row('kid', { parentSessionId: 'p', messageCount: 3 })] })
  diskSlices = { kid: slice([msg('k1', 'user', 'a'), msg('k2', 'assistant', 'b'), msg('k3', 'user', 'c')]) }
  loadDelayMs = 30
  emit({ type: 'turn_start', sessionId: 'kid', turnId: 'k' })
  emit({ type: 'text_delta', sessionId: 'kid', text: 'woken' })
  emit({ type: 'turn_complete', sessionId: 'kid', turnId: 'k', success: true })
  assert.equal(h().sessionArchive.kid, undefined, 'nothing applied to an empty slice')
  await sleep(80)
  const kid = h().sessionArchive.kid
  assert.equal(kid.messages.length, 4)
  assert.deepEqual(shape(kid.messages.at(-1)), ['text:woken'])
  assert.equal(kid.isStreaming, false)
  flushScheduledPersists()
  await sleep(0)
  const saved = saves.filter((s) => s.id === 'kid').at(-1)
  assert.ok(saved, 'saved after the held turn_complete')
  assert.equal(saved.slice.messages.length, 4)
})

await check('an unreadable history is never overwritten by what streamed since', async () => {
  seed({ sessions: [row('p', { messageCount: 1 }), row('gone', { parentSessionId: 'p', messageCount: 9 })] })
  emit({ type: 'turn_start', sessionId: 'gone', turnId: 'g' })
  emit({ type: 'text_delta', sessionId: 'gone', text: 'x' })
  await sleep(20)
  assert.equal(h().sessionArchive.gone.messages.length, 1, 'the stream still shows')
  emit({ type: 'turn_complete', sessionId: 'gone', turnId: 'g', success: true })
  flushScheduledPersists()
  await sleep(0)
  assert.equal(saves.filter((s) => s.id === 'gone').length, 0)
})

console.log(failures ? `\n${failures} FAILED` : '\nall passed')
process.exit(failures ? 1 : 0)
