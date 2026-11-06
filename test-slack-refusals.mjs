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

// A refusal names the change it refuses by the request id the command sent.
const refusalOf = (cmd, subtype) =>
  H().handleEvent({
    type: 'system_event', sessionId: cmd.sessionId, subtype, message: 'refused',
    details: { chatVisible: true, requestId: cmd.requestId },
  })
const lastSent = (type) => sent.filter((c) => c.type === type).at(-1)

await check('a refusal of the first of two quick model changes leaves the second on screen', async () => {
  openSlack()
  await H().setModel('claude-opus-5-5', 'high')
  const first = lastSent('set_model')
  await H().setModel('claude-fable-5-1', 'low')
  const second = lastSent('set_model')
  refusalOf(first, 'model_change_refused')
  assert.equal(H().model, 'claude-fable-5-1')
  assert.equal(rowOf(SLACK).model, 'claude-fable-5-1')
  // The second refused too: the session had the model from before both.
  refusalOf(second, 'model_change_refused')
  assert.equal(H().model, 'claude-sonnet-5-5')
  assert.equal(H().reasoningLevel, 'medium')
})

await check('a refusal of the second of two quick model changes shows the first', async () => {
  openSlack()
  await H().setModel('claude-opus-5-5', 'high')
  const first = lastSent('set_model')
  await H().setModel('claude-fable-5-1', 'low')
  refusalOf(lastSent('set_model'), 'model_change_refused')
  assert.equal(H().model, 'claude-opus-5-5')
  assert.equal(H().reasoningLevel, 'high')
  refusalOf(first, 'model_change_refused')
  assert.equal(H().model, 'claude-sonnet-5-5')
})

await check('a refusal of the first of two quick mode changes leaves the second on screen', async () => {
  openSlack()
  await H().setCoordinationStrategy('kanban')
  const first = lastSent('set_coordination_strategy')
  await H().setCoordinationStrategy('goal')
  const second = lastSent('set_coordination_strategy')
  refusalOf(first, 'coordination_strategy_refused')
  assert.equal(H().coordinationStrategy, 'goal')
  refusalOf(second, 'coordination_strategy_refused')
  assert.equal(H().coordinationStrategy, 'bus')
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

const autopilotAnswer = (sessionId, subtype, message, reason) =>
  H().handleEvent({
    type: 'system_event', sessionId, subtype, message,
    details: { chatVisible: !reason, ...(reason ? { reason } : {}) },
  })
const microtasks = () => new Promise((resolve) => setTimeout(resolve, 0))

await check('/autopilot on a Slack session waits for the gateway to say what it did', async () => {
  openSlack()
  sent.length = 0
  H().runSlashCommand('/autopilot', 'on')
  assert.equal(sent.at(-1).type, 'kanban_autopilot')
  assert.equal(H().toast, null)
  // Not in kanban mode: the gateway leaves autopilot off.
  autopilotAnswer(SLACK, 'kanban_autopilot_disabled',
    'Kanban autopilot unavailable — session is not in kanban mode', 'strategy_not_kanban')
  await microtasks()
  assert.match(H().toast?.message ?? '', /not in kanban mode/)
})

await check('/autopilot on a Slack session in kanban mode says on when the gateway turned it on', async () => {
  openSlack()
  H().runSlashCommand('/autopilot', 'on')
  assert.equal(H().toast, null)
  autopilotAnswer(SLACK, 'kanban_autopilot_enabled', 'Kanban auto-dispatch enabled')
  await microtasks()
  assert.equal(H().toast?.message, 'autopilot on')
  // Later autopilot events for the thread answer no command.
  useHarness.setState({ toast: null })
  autopilotAnswer(SLACK, 'kanban_autopilot_disabled', 'Kanban auto-dispatch disabled')
  await microtasks()
  assert.equal(H().toast, null)
})

await check('/autopilot on a Slack session that no gateway can answer leaves no wait behind', async () => {
  openSlack()
  H().runSlashCommand('/autopilot', 'on')
  // The app's bridge refuses it for the thread, as the gateway refuses a
  // command for a thread it does not have.
  H().handleEvent({
    type: 'system_event', sessionId: SLACK, subtype: 'control_command_skipped',
    message: "Not done: this session's kanban board is in the Slack gateway, which is not running.",
    details: { command: 'kanban_autopilot', chatVisible: true },
  })
  assert.ok(H().messages.some((m) => m.parts.some((p) => p.systemSubtype === 'control_command_skipped')))
  // An autopilot event for the thread later, when the gateway is back,
  // answers no command.
  autopilotAnswer(SLACK, 'kanban_autopilot_enabled', 'Kanban auto-dispatch restored')
  await microtasks()
  assert.equal(H().toast, null)
})

// The dashboard's toggle shows its change at once; the app's bridge with
// no gateway, or a gateway without the thread, refuses it by request id.
const autopilotRefusal = (cmd) =>
  H().handleEvent({
    type: 'system_event', sessionId: cmd.sessionId, subtype: 'control_command_skipped', message: 'Not done.',
    details: { command: 'kanban_autopilot', chatVisible: true, requestId: cmd.requestId },
  })

await check('an autopilot toggle the gateway refused is taken back off the dashboard', async () => {
  openSlack()
  useHarness.setState({ autoDispatchEnabled: false })
  await H().setKanbanAutopilot(SLACK, true)
  assert.equal(H().autoDispatchEnabled, true)
  autopilotRefusal(lastSent('kanban_autopilot'))
  assert.equal(H().autoDispatchEnabled, false)
})

await check('a refusal of the second of two quick autopilot toggles shows the first', async () => {
  openSlack()
  useHarness.setState({ autoDispatchEnabled: false })
  await H().setKanbanAutopilot(SLACK, true)
  const first = lastSent('kanban_autopilot')
  await H().setKanbanAutopilot(SLACK, false)
  autopilotRefusal(lastSent('kanban_autopilot'))
  assert.equal(H().autoDispatchEnabled, true)
  autopilotRefusal(first)
  assert.equal(H().autoDispatchEnabled, false)
})

await check('a refusal of /autopilot leaves an earlier toggle on the dashboard', async () => {
  openSlack()
  useHarness.setState({ autoDispatchEnabled: false })
  await H().setKanbanAutopilot(SLACK, true)
  // /autopilot showed nothing at once, and its refusal names no change.
  H().runSlashCommand('/autopilot', 'off')
  autopilotRefusal(lastSent('kanban_autopilot'))
  assert.equal(H().autoDispatchEnabled, true)
})

await check('/autopilot on a desktop session still says on at once', async () => {
  useHarness.setState({ activeSessionId: 'session-desktop', sessions: [row('session-desktop')], toast: null })
  H().runSlashCommand('/autopilot', 'on')
  assert.equal(H().toast?.message, 'autopilot on')
})

if (failures) {
  console.log(`\n${failures} failed`)
  process.exit(1)
}
console.log('\nall passed')
