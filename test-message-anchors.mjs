// Commands anchored on a message (edit, rerun, delete, pin, fork) in the
// renderer store: what they send the bridge about that message. Drives the
// real store actions and checks the commands. Run from the repo root:
//   npx tsx test-message-anchors.mjs
import assert from 'node:assert/strict'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { useHarness } from './src/renderer/state/store.ts'

// The main-process persistence module resolves ~/.freyja from HOME when it
// loads (imported below, after this).
const home = fs.mkdtempSync(path.join(os.tmpdir(), 'freyja-anchors-test-'))
process.env.HOME = home

const sent = []
globalThis.window = globalThis.window ?? {}
window.harness = { sendCommand: async (cmd) => { sent.push(cmd) } }

let failures = 0
async function check(name, fn) {
  try {
    await fn()
    console.log(`ok   ${name}`)
  } catch (err) {
    failures++
    console.log(`FAIL ${name}\n     ${err.message}`)
  }
}

const text = (t) => [{ type: 'text', text: t }]
const long = 'Look at the logs.   ' + 'x'.repeat(300) + '\n\nThen fix it.'
const chat = () => [
  { id: 'u1', role: 'user', parts: text(long), createdAt: 1_000 },
  {
    id: 'a1', role: 'assistant', createdAt: 1_010,
    parts: [{ type: 'thinking', text: 'hm' }, ...text('Reading them.'), { type: 'tool_call', toolCallId: 'call_1' }, { type: 'tool_call', toolCallId: 'call_2' }],
  },
  { id: 'a2', role: 'assistant', parts: [{ type: 'system', text: 'Compaction complete' }], createdAt: 1_500 },
  // Slid in where it landed, so after a3 in the array but before it in time.
  { id: 'a3', role: 'assistant', parts: text('All fixed.'), createdAt: 3_000 },
  { id: 'u2', role: 'user', parts: text('thanks'), createdAt: 2_000 },
]
const reset = () => useHarness.setState({ messages: chat(), isStreaming: false })

await check('a user message sends the ends of its text and where the next user message starts', async () => {
  reset()
  await useHarness.getState().rerunUserMessage('u1')
  const fp = sent.at(-1).messageFingerprint
  const flat = long.split(/\s+/).join(' ')
  assert.deepEqual(fp, {
    role: 'user', textHead: flat.slice(0, 100), textTail: flat.slice(-100), nextUserCreatedAt: 2_000,
  })
})

await check('a reply sends its first tool call', async () => {
  reset()
  await useHarness.getState().toggleEntryPin('a1', true)
  assert.equal(sent.at(-1).messageFingerprint.toolCallId, 'call_1')
  assert.equal(sent.at(-1).messageFingerprint.role, 'assistant')
  assert.equal(sent.at(-1).messageFingerprint.nextUserCreatedAt, 2_000)
})

await check('the last message has no next user message', async () => {
  reset()
  await useHarness.getState().deleteMessagesFrom('a3')
  assert.deepEqual(sent.at(-1).messageFingerprint, {
    role: 'assistant', textHead: 'All fixed.', textTail: 'All fixed.',
  })
})

await check('edit and fork send it too', async () => {
  reset()
  await useHarness.getState().editUserMessage('u2', 'thank you')
  assert.equal(sent.at(-1).type, 'edit_user_message')
  assert.equal(sent.at(-1).messageFingerprint.textHead, 'thanks')
  reset()
  await useHarness.getState().branchSessionFrom('u2')
  assert.equal(sent.at(-1).type, 'branch_session')
  assert.equal(sent.at(-1).messageFingerprint.textHead, 'thanks')
})

const ids = () => useHarness.getState().messages.map((m) => m.id)
const sid = () => useHarness.getState().activeSessionId
const ev = (e) => useHarness.getState().handleEvent({ sessionId: sid(), ...e })

await check('a refused rerun puts the chat back as it was', async () => {
  reset()
  const before = ids()
  await useHarness.getState().rerunUserMessage('u2')
  assert.deepEqual(ids(), ['u1', 'a1', 'a2', 'a3'])
  ev({ type: 'rewind_refused', requestId: sent.at(-1).requestId })
  assert.deepEqual(ids(), before)
})

await check('a refused edit puts the chat back and the new text in the composer', async () => {
  reset()
  useHarness.setState({ inputDraft: 'half a thought' })
  const before = useHarness.getState().messages
  await useHarness.getState().editUserMessage('u1', 'Look at the other logs.')
  assert.equal(useHarness.getState().messages.length, 1)
  ev({ type: 'rewind_refused', requestId: sent.at(-1).requestId })
  assert.deepEqual(useHarness.getState().messages, before)
  assert.equal(useHarness.getState().inputDraft, 'Look at the other logs.\n\nhalf a thought')
})

await check('a refused delete keeps what arrived after the cut', async () => {
  reset()
  await useHarness.getState().deleteMessagesFrom('a2')
  const { requestId } = sent.at(-1)
  ev({ type: 'message_appended', role: 'assistant', content: 'Scheduled run done.', messageId: 'late', createdAt: 4_000 })
  ev({ type: 'rewind_refused', requestId })
  assert.deepEqual(ids(), ['u1', 'a1', 'a2', 'a3', 'u2', 'late'])
})

await check('a refusal of an earlier command does not undo the latest one', async () => {
  reset()
  await useHarness.getState().deleteMessagesFrom('a3')
  const first = sent.at(-1).requestId
  await useHarness.getState().deleteMessagesFrom('a1')
  ev({ type: 'rewind_refused', requestId: first })
  assert.deepEqual(ids(), ['u1'])
})

await check("a reply is shown from the bridge's turn_start stamp, past its user message's entry", async () => {
  reset()
  const at = Date.now() - 50.5
  ev({ type: 'turn_start', turnId: 'turn-1', at })
  const reply = useHarness.getState().messages.find((m) => m.id === useHarness.getState().currentStreamingMessageId)
  assert.equal(reply.createdAt, at)
  ev({ type: 'turn_complete', turnId: 'turn-1', success: true })
})

const pinnedIds = (messages) => messages.filter((m) => m.pinned).map((m) => m.id)

await check("a pin event marks the message in its own session, not the one on screen", async () => {
  reset()
  useHarness.setState({ sessionArchive: { other: { messages: chat() } } })
  useHarness.getState().handleEvent({ type: 'entry_pin_changed', sessionId: 'other', messageOrdinal: 1, pinned: true })
  assert.deepEqual(pinnedIds(useHarness.getState().messages), [])
  assert.deepEqual(pinnedIds(useHarness.getState().sessionArchive.other.messages), ['a1'])
  ev({ type: 'entry_pin_changed', messageOrdinal: 1, pinned: true })
  assert.deepEqual(pinnedIds(useHarness.getState().messages), ['a1'])
})

await check("an agent's pin marks the message holding the entry stamped `at`", async () => {
  // summarize_context's ordinals count every entry, tool results too, so
  // its event names the entry by its stamp: here a step of the reply a1
  // after its tool results.
  reset()
  ev({ type: 'entry_pin_changed', entryId: 'e5', at: 1_200, pinned: true, source: 'agent_summarize_context' })
  assert.deepEqual(pinnedIds(useHarness.getState().messages), ['a1'])
  reset()
  ev({ type: 'entry_pin_changed', entryId: 'e9', at: 2_000, pinned: true, source: 'agent_summarize_context' })
  assert.deepEqual(pinnedIds(useHarness.getState().messages), ['u2'])
})

// The bridge's _find_message_entry_index: the first entry holding a message
// stamped at or after the anchor.
const resolve = (entries, createdAt) =>
  entries.find((e) => e.message && e.timestamp >= createdAt / 1000)?.id

await check('a Slack session shown from its transcript puts each message where its anchor finds its own entry', async () => {
  // Real stamps from a Slack thread: a turn queued behind the one
  // running appends its user entry in the same ms as that turn's last.
  const entry = (id, role, timestamp, content = `${role} ${id}`) => ({ id, timestamp, message: { role, content } })
  const entries = [
    entry('u1', 'user', 1788996000.5),
    entry('a1', 'assistant', 1788996050.25),
    entry('t1', 'tool_result', 1788996057.99725),
    entry('u2', 'user', 1788996057.997675),
    entry('a2', 'assistant', 1788996070.1),
  ]
  const sessionsDir = path.join(home, '.freyja', 'sessions')
  fs.mkdirSync(sessionsDir, { recursive: true })
  fs.writeFileSync(
    path.join(sessionsDir, 'freyja_slack_T1_channel_C1_1.0.transcript.json'),
    JSON.stringify({ session_id: 'freyja:slack:T1:channel:C1:1.0', metadata: {}, transcript: { entries } }),
  )
  const { loadSession } = await import('./src/main/persistence.ts')
  const { messages } = loadSession('freyja:slack:T1:channel:C1:1.0').slice
  assert.deepEqual(messages.map((m) => m.id), ['u1', 'a1', 'u2', 'a2'])
  assert.deepEqual(messages.map((m) => resolve(entries, m.createdAt)), ['u1', 'a1', 'u2', 'a2'])
})

await check("a Slack session's events keep the stamps the gateway gave them", async () => {
  // `_t` on an .events.jsonl line is ms; a fork keeps the events before
  // its message by it.
  const sessionsDir = path.join(home, '.freyja', 'sessions')
  fs.writeFileSync(
    path.join(sessionsDir, 'freyja_slack_T1_channel_C1_1.0.events.jsonl'),
    JSON.stringify({ type: 'system_event', sessionId: 'freyja:slack:T1:channel:C1:1.0', subtype: 'compaction_complete', message: 'Context compacted', _t: 1788996060123 }) + '\n',
  )
  const { loadSession } = await import('./src/main/persistence.ts')
  const { systemEvents } = loadSession('freyja:slack:T1:channel:C1:1.0').slice
  assert.deepEqual(systemEvents.map((e) => e.at), [1788996060123])
})

await check("a Slack session's activity keeps its newest events", async () => {
  // It shows at most 200; a long thread has more, and the newest matter.
  const sessionsDir = path.join(home, '.freyja', 'sessions')
  const line = (i) => JSON.stringify({ type: 'system_event', sessionId: 'freyja:slack:T1:channel:C1:1.0', subtype: 'note', message: `event ${i}`, _t: 1788996000000 + i })
  fs.writeFileSync(
    path.join(sessionsDir, 'freyja_slack_T1_channel_C1_1.0.events.jsonl'),
    Array.from({ length: 250 }, (_, i) => line(i)).join('\n') + '\n',
  )
  const { loadSession } = await import('./src/main/persistence.ts')
  const { systemEvents } = loadSession('freyja:slack:T1:channel:C1:1.0').slice
  assert.equal(systemEvents.length, 200)
  assert.deepEqual([systemEvents[0].message, systemEvents.at(-1).message], ['event 50', 'event 249'])
})

await check("a Slack session shown from its transcript has the cards its kanban journal holds", async () => {
  // The gateway's journal, named the way bridge/kanban_journal.py names
  // it (invalid characters stripped). Each row holds the whole card.
  const sessionsDir = path.join(home, '.freyja', 'sessions')
  const plan = { id: 'card_001', title: 'Plan', status: 'running', parents: [], children: [], events: [{ kind: 'created' }] }
  const ship = { id: 'card_002', title: 'Ship', status: 'triage', parents: [], children: [], events: [] }
  const rows = [
    { ts: 1, kind: 'create', task: plan },
    { ts: 2, kind: 'create', task: ship },
    { ts: 3, kind: 'link', parent: 'card_001', child: 'card_002', actor: 'parent' },
    // An update snapshot is taken after the link, so it has the child.
    { ts: 4, kind: 'update', id: 'card_001', actor: 'kanban-judge', task: { ...plan, status: 'done', children: ['card_002'] } },
    { ts: 5, kind: 'restarted' },
    { ts: 6, kind: 'unblock', id: 'card_002', actor: 'operator' },
  ]
  fs.writeFileSync(
    path.join(sessionsDir, 'freyjaslackT1channelC11.0.kanban.jsonl'),
    rows.map((row) => JSON.stringify(row)).join('\n') + '\nnot json\n',
  )
  const { loadSession } = await import('./src/main/persistence.ts')
  const { kanbanCards } = loadSession('freyja:slack:T1:channel:C1:1.0').slice
  assert.deepEqual(Object.keys(kanbanCards), ['card_001', 'card_002'])
  assert.equal(kanbanCards.card_001.status, 'done')
  assert.deepEqual(kanbanCards.card_001.events, [{ kind: 'created' }], 'a card keeps its history')
  assert.deepEqual(kanbanCards.card_001.children, ['card_002'])
  assert.deepEqual(kanbanCards.card_002.parents, ['card_001'])
  assert.equal(kanbanCards.card_002.status, 'ready')
})

await check("a Slack session's events the gateway did not stamp take their neighbours' times, not the time it was loaded", async () => {
  // Lines from before the gateway stamped them (2026-09-21) have no `_t`.
  // The file is in the order things happened.
  const sessionsDir = path.join(home, '.freyja', 'sessions')
  const line = (ev) => JSON.stringify({ sessionId: 'freyja:slack:T1:channel:C1:1.0', ...ev })
  fs.writeFileSync(
    path.join(sessionsDir, 'freyja_slack_T1_channel_C1_1.0.events.jsonl'),
    [
      line({ type: 'log', level: 'info', message: 'before any stamp' }),
      line({ type: 'text_delta', text: 'hi', _t: 1788996000000 }),
      line({ type: 'system_event', subtype: 'note', message: 'after a stamp' }),
      line({ type: 'followup_queued', clientId: 'c1', at: 1788996002000 }),
      line({ type: 'error', message: 'after its own time field' }),
      line({ type: 'system_event', subtype: 'note', message: 'stamped', _t: 1788996005000 }),
    ].join('\n') + '\n',
  )
  const { loadSession } = await import('./src/main/persistence.ts')
  const { systemEvents } = loadSession('freyja:slack:T1:channel:C1:1.0').slice
  assert.deepEqual(systemEvents.map((e) => [e.message, e.at]), [
    ['before any stamp', 1788996000000],
    ['after a stamp', 1788996000000],
    ['after its own time field', 1788996002000],
    ['stamped', 1788996005000],
  ])
})

await check("a Slack session's events file with no stamp at all takes the time it was last written", async () => {
  const sessionsDir = path.join(home, '.freyja', 'sessions')
  const file = path.join(sessionsDir, 'freyja_slack_T1_channel_C1_1.0.events.jsonl')
  const line = (message) => JSON.stringify({ type: 'log', level: 'info', sessionId: 'freyja:slack:T1:channel:C1:1.0', message })
  fs.writeFileSync(file, [line('one'), line('two')].join('\n') + '\n')
  const written = new Date('2026-06-04T21:04:00Z')
  fs.utimesSync(file, written, written)
  const { loadSession } = await import('./src/main/persistence.ts')
  const { systemEvents } = loadSession('freyja:slack:T1:channel:C1:1.0').slice
  assert.deepEqual(systemEvents.map((e) => e.at), [written.getTime(), written.getTime()])
})

await check('a Slack session the app saved gets the cards its kanban journal holds and the slice lacks', async () => {
  // The app saved this slice before the gateway made card_003, and holds
  // card_002 only as the kanban tool's list sent it (no history).
  const sessionsDir = path.join(home, '.freyja', 'sessions')
  const card = (id, status, events) => ({ id, title: id, status, parents: [], children: [], ...(events ? { events } : {}) })
  const saved = { card_001: card('card_001', 'review', [{ kind: 'completed' }]), card_002: card('card_002', 'ready') }
  fs.writeFileSync(
    path.join(sessionsDir, 'freyja_slack_T1_channel_C2_2.0.json'),
    JSON.stringify({ version: 1, id: 'freyja:slack:T1:channel:C2:2.0', agentType: 'gateway-slack', slice: { messages: [], kanbanCards: saved } }),
  )
  const rows = [
    { kind: 'create', task: card('card_001', 'running', [{ kind: 'created' }]) },
    { kind: 'create', task: card('card_002', 'ready', [{ kind: 'created' }]) },
    { kind: 'update', id: 'card_002', task: card('card_002', 'running', [{ kind: 'created' }, { kind: 'claimed' }]) },
    { kind: 'create', task: card('card_003', 'ready', [{ kind: 'created' }]) },
  ]
  fs.writeFileSync(
    path.join(sessionsDir, 'freyjaslackT1channelC22.0.kanban.jsonl'),
    rows.map((row) => JSON.stringify(row)).join('\n') + '\n',
  )
  const { loadSession } = await import('./src/main/persistence.ts')
  const { kanbanCards } = loadSession('freyja:slack:T1:channel:C2:2.0').slice
  assert.deepEqual(Object.keys(kanbanCards).sort(), ['card_001', 'card_002', 'card_003'])
  assert.deepEqual(kanbanCards.card_001, saved.card_001, 'a card with history keeps the slice copy')
  assert.equal(kanbanCards.card_002.status, 'running')
  assert.deepEqual(kanbanCards.card_002.events, [{ kind: 'created' }, { kind: 'claimed' }])
  assert.equal(kanbanCards.card_003.status, 'ready')
})

if (failures) {
  console.log(`\n${failures} failed`)
  process.exit(1)
}
console.log('\nall passed')
