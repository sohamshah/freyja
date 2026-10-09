// Checks that the computer STOP buttons go away when nothing is on the screen.
// The Activity panel's STOP shows while the session's computer entry is
// running; the floating panic button shows while `computerActive` is set.
// Run from the repo root:
//   npx tsx test-computer-sessions.mjs

import assert from 'node:assert/strict'

globalThis.window = globalThis.window ?? {}
window.harness = {
  sendCommand: async () => {},
  sessionSave: async () => ({ ok: true }),
  sessionIndexSave: async () => ({ ok: true }),
  sessionLoad: async () => ({ ok: false }),
}
const { useHarness } = await import('./src/renderer/state/store.ts')
const h = () => useHarness.getState()
const emit = (ev) => h().handleEvent(ev)

let failures = 0
function check(name, fn) {
  try {
    fn()
    console.log(`ok   ${name}`)
  } catch (err) {
    failures++
    console.log(`FAIL ${name}\n     ${err.message.split('\n').join('\n     ')}`)
  }
}

const MAIN = 'session-local'
const PNG = 'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=='
let clock = 1_000
const frame = (sessionId) =>
  emit({ type: 'screenshot_frame', sessionId, pngBase64: PNG, mimeType: 'image/png', width: 1, height: 1, takenAt: clock++, reason: 'after-click' })
const click = (sessionId) => {
  emit({ type: 'action_planned', sessionId, action: 'click', description: 'click', x: 1, y: 1 })
  emit({ type: 'action_executed', sessionId, action: 'click', success: true, durationMs: 5 })
}
const spawn = (sessionId, extra = {}) =>
  emit({
    type: 'session_spawned', sessionId, parentSessionId: MAIN, title: sessionId, model: 'm',
    reasoningLevel: 'off', task: 'g', mode: 'background', workspace: '/tmp', createdAt: Date.now(), ...extra,
  })
const running = () => Object.values(h().computerSessions).filter((s) => s.status === 'running').map((s) => s.sessionId)
// What EmergencyPanic renders: nothing unless computerActive and something runs.
const panicCount = () => (h().computerActive ? running().length : 0)

emit({ type: 'ready', sessionId: MAIN, mode: 'live', capabilities: {} })

// The reported case: the agent clicks and types on the screen itself during
// its turn and starts a jev run; both finish, and the STOP buttons stayed.
emit({ type: 'turn_start', sessionId: MAIN, turnId: 't1' })
frame(MAIN)
click(MAIN)
frame(MAIN)
spawn('jev_a', { agentType: 'computer' })
emit({ type: 'computer_session_start', sessionId: 'jev_a', parentSessionId: MAIN, goal: 'g', targetApp: 'Arc' })

check("the agent's own screen use runs during its turn, and a jev run raises the panic button", () => {
  assert.equal(h().computerSessions[MAIN].status, 'running')
  assert.equal(h().computerSessions[MAIN].direct, true)
  assert.equal(h().computerSessions[MAIN].history.length, 1)
  assert.equal(h().computerActive, true)
  assert.equal(panicCount(), 2)
})

emit({ type: 'computer_session_end', sessionId: 'jev_a', outcome: 'done', summary: 'ok' })

check('the panic button goes when the jev run ends, though the turn goes on', () => {
  assert.equal(h().computerSessions.jev_a.status, 'done')
  assert.equal(h().computerActive, false)
  assert.equal(panicCount(), 0)
  assert.deepEqual(running(), [MAIN])
})

emit({ type: 'turn_complete', sessionId: MAIN, turnId: 't1', success: true })

check("the agent's own screen use ends with its turn: no STOP anywhere", () => {
  assert.equal(h().computerSessions[MAIN].status, 'done')
  assert.equal(h().computerSessions[MAIN].frameCount, 2)
  assert.deepEqual(running(), [])
  assert.equal(h().computerActive, false)
})

// A later turn uses the screen again; a stale turn_complete from an earlier
// turn must not end it.
emit({ type: 'turn_start', sessionId: MAIN, turnId: 't2' })
click(MAIN)
check('a later turn that acts on the screen runs again', () => {
  assert.equal(h().computerSessions[MAIN].status, 'running')
})
emit({ type: 'turn_complete', sessionId: MAIN, turnId: 't1', success: true })
check("a stale turn_complete does not end the current turn's use", () => {
  assert.equal(h().computerSessions[MAIN].status, 'running')
})
emit({ type: 'turn_complete', sessionId: MAIN, turnId: 't2', success: true })
check('and that turn ending ends it', () => {
  assert.equal(h().computerSessions[MAIN].status, 'done')
})

// A general sub-agent acts with the parent's computer tools, so its frames
// carry the parent's id, also after the parent's turn has ended.
spawn('sub_b')
frame(MAIN)
check("a background sub-agent on the screen runs the parent's entry", () => {
  assert.equal(h().computerSessions[MAIN].status, 'running')
})
emit({ type: 'session_completed', sessionId: 'sub_b', success: true })
check('it ends when the sub-agent finishes and the parent is between turns', () => {
  assert.equal(h().computerSessions[MAIN].status, 'done')
})
spawn('sub_c')
emit({ type: 'turn_start', sessionId: MAIN, turnId: 't3' })
frame(MAIN)
emit({ type: 'session_completed', sessionId: 'sub_c', success: true })
check("while the parent's turn runs, the parent's turn decides", () => {
  assert.equal(h().computerSessions[MAIN].status, 'running')
  emit({ type: 'turn_complete', sessionId: MAIN, turnId: 't3', success: true })
  assert.equal(h().computerSessions[MAIN].status, 'done')
})

// A run's late frame does not bring an ended run back.
spawn('jev_d', { agentType: 'computer' })
emit({ type: 'computer_session_start', sessionId: 'jev_d', parentSessionId: MAIN, goal: 'g' })
emit({ type: 'computer_session_end', sessionId: 'jev_d', outcome: 'stuck', summary: 'blocked' })
frame('jev_d')
click('jev_d')
check('an ended run stays ended', () => {
  assert.equal(h().computerSessions.jev_d.status, 'stopped')
  assert.equal(h().computerActive, false)
})

// The bridge restarts mid-run: the new process sends no end for the old
// process's runs.
spawn('jev_e', { agentType: 'computer' })
emit({ type: 'computer_session_start', sessionId: 'jev_e', parentSessionId: MAIN, goal: 'g' })
emit({ type: 'turn_start', sessionId: MAIN, turnId: 't4' })
frame(MAIN)
emit({ type: 'ready', sessionId: 'session-new', mode: 'live', capabilities: {} })
check('a new bridge ends what the old one was doing on the screen', () => {
  assert.equal(h().computerSessions.jev_e.status, 'cancelled')
  assert.equal(h().computerSessions[MAIN].status, 'done')
  assert.deepEqual(running(), [])
  assert.equal(h().computerActive, false)
})

if (failures) {
  console.log(`\n${failures} failed`)
  process.exit(1)
}
console.log('\nall passed')
