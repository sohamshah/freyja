// The app shows a model or mode picked for a session at once, before the
// bridge has acted on it. A Slack session's thread runs in the gateway,
// which can refuse the change (a model it does not know, a sub-agent's
// pane), as the app's bridge does when no gateway runs. The app went on
// showing the refused model, and /goal on a Slack session said "goal loop
// armed" before the refusal came. Run from the repo root:
//   npx tsx test-slack-refusals.mjs
import assert from 'node:assert/strict'

const sent = []
globalThis.window = globalThis.window ?? {}
window.harness = {
  sendCommand: async (cmd) => { sent.push(cmd); return { ok: true } },
  sessionSave: async () => ({ ok: true }),
  sessionIndexSave: async () => ({ ok: true }),
}

const { useHarness } = await import('./src/renderer/state/store.ts')
const H = () => useHarness.getState()

const SLACK = 'freyja:slack:T1:channel:C1:1.0'
const OTHER = 'freyja:slack:T1:channel:C1:2.0'
const row = (id) => ({
  id, title: id, workspace: '~/', model: 'claude-sonnet-5-5', reasoningLevel: 'medium',
  coordinationStrategy: 'bus', createdAt: 1, updatedAt: 1, messageCount: 0,
  totalInputTokens: 0, totalOutputTokens: 0, cacheReadTokens: 0,
})
const refused = (sessionId, subtype, message) =>
  H().handleEvent({ type: 'system_event', sessionId, subtype, message, details: { chatVisible: true } })
const rowOf = (id) => H().sessions.find((s) => s.id === id)

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

function openSlack() {
  useHarness.setState({
    activeSessionId: SLACK,
    sessions: [row(SLACK), row(OTHER)],
    sessionArchive: {},
    model: 'claude-sonnet-5-5',
    reasoningLevel: 'medium',
    coordinationStrategy: 'bus',
    messages: [],
    toast: null,
  })
}

await check('a model the gateway refused is taken back off the screen', async () => {
  openSlack()
  await H().setModel('claude-opus-5-5', 'high')
  assert.equal(H().model, 'claude-opus-5-5')
  refused(SLACK, 'model_change_refused', 'Unknown model `claude-opus-5-5`: the session stays on its current model.')
  assert.equal(H().model, 'claude-sonnet-5-5')
  assert.equal(H().reasoningLevel, 'medium')
  assert.equal(rowOf(SLACK).model, 'claude-sonnet-5-5')
  // The refusal itself shows in the chat.
  assert.ok(H().messages.some((m) => m.parts.some((p) => p.systemSubtype === 'model_change_refused')))
})

await check('a mode the gateway refused is taken back off the screen', async () => {
  openSlack()
  await H().setCoordinationStrategy('kanban')
  assert.equal(H().coordinationStrategy, 'kanban')
  refused(SLACK, 'coordination_strategy_refused', 'Mode not changed: the Slack gateway is not running.')
  assert.equal(H().coordinationStrategy, 'bus')
  assert.equal(rowOf(SLACK).coordinationStrategy, 'bus')
})

await check('a refusal that arrives after the operator moved to another session undoes it there', async () => {
  openSlack()
  await H().setModel('claude-opus-5-5', 'high')
  // In the background now, with the model the app gave it.
  H().handleEvent({ type: 'system_event', sessionId: OTHER, subtype: 'session_switched', message: '', details: {} })
  await H().switchSession(OTHER)
  assert.equal(H().sessionArchive[SLACK].model, 'claude-opus-5-5')
  refused(SLACK, 'model_change_refused', 'Model not changed: the Slack gateway is not running.')
  assert.equal(rowOf(SLACK).model, 'claude-sonnet-5-5')
  assert.equal(H().sessionArchive[SLACK].model, 'claude-sonnet-5-5')
  assert.equal(H().sessionArchive[SLACK].reasoningLevel, 'medium')
})

await check('a model change that went through stays on screen', async () => {
  openSlack()
  await H().setModel('claude-opus-5-5', 'high')
  H().handleEvent({
    type: 'system_event', sessionId: SLACK, subtype: 'model_changed',
    message: 'model changed to claude-opus-5-5 (high)', details: { model: 'claude-opus-5-5', reasoningLevel: 'high' },
  })
  assert.equal(H().model, 'claude-opus-5-5')
})

await check('/goal on a Slack session does not say the goal loop is armed', async () => {
  openSlack()
  sent.length = 0
  H().runSlashCommand('/goal', 'ship the release')
  assert.equal(sent.at(-1).type, 'goal_control')
  assert.doesNotMatch(H().toast?.message ?? '', /armed/)
})

await check('/goal on a desktop session still says the goal loop is armed', async () => {
  useHarness.setState({ activeSessionId: 'session-desktop', sessions: [row('session-desktop')], toast: null })
  H().runSlashCommand('/goal', 'ship the release')
  assert.match(H().toast?.message ?? '', /armed/)
})

if (failures) {
  console.log(`\n${failures} failed`)
  process.exit(1)
}
console.log('\nall passed')
