// A Slack thread reset by /reset lists none of the old conversation's
// sub-agents as its children. The bridge sets the old conversation aside and
// no longer counts the sub-agents saved before the reset as the fresh one's
// (transcript_persistence.predates_reset), but the app nested them under the
// thread by parentSessionId as before. They are hidden now; every file stays.
// Run from the repo root:
//   npx tsx test-reset-subagents-hide.mjs
import assert from 'node:assert/strict'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'

// The main-process persistence module resolves ~/.freyja from HOME when it
// loads (imported below, after this).
const home = fs.mkdtempSync(path.join(os.tmpdir(), 'freyja-reset-subagents-test-'))
process.env.HOME = home
const persistence = await import('./src/main/persistence.ts')
const { useHarness } = await import('./src/renderer/state/store.ts')
const sessionsDir = path.join(home, '.freyja', 'sessions')
fs.mkdirSync(sessionsDir, { recursive: true })

const SLACK = 'freyja:slack:T1:channel:C1:1.0'
const file = (id, suffix) => path.join(sessionsDir, `${id.replace(/[^a-zA-Z0-9._-]/g, '_')}${suffix}`)
const touch = (p, ms) => fs.utimesSync(p, new Date(ms), new Date(ms))
const writeTranscript = (id, metadata = {}) =>
  fs.writeFileSync(file(id, '.transcript.json'), JSON.stringify({
    version: 1, session_id: id, metadata,
    transcript: { entries: [{ id: 'e1', message: { role: 'user', content: 'hi' } }], head_id: 'e1' },
  }))
/** A sub-agent of ``parent`` the gateway ran, saved at ``ms``. */
const subAgent = (id, parent, ms) => {
  writeTranscript(id, { parent_session_id: parent })
  fs.writeFileSync(file(id, '.subagent.json'), JSON.stringify({ sessionId: id, parentSessionId: parent, savedAt: ms }))
  touch(file(id, '.transcript.json'), ms)
  touch(file(id, '.subagent.json'), ms)
}
const row = (id, parentSessionId) => ({ id, title: id, createdAt: 1, updatedAt: 1, parentSessionId })
const children = (parent) =>
  useHarness.getState().sessions.filter((s) => s.parentSessionId === parent).map((s) => s.id).sort()

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

globalThis.window = globalThis.window ?? {}
window.harness = { sessionList: async () => ({ ok: true, sessions: persistence.listSessions() }) }

const now = Date.now()
writeTranscript(SLACK)
subAgent('sub_old_1', SLACK, now - 60_000)
// /reset: the old conversation set aside, the reset's time in its name.
fs.writeFileSync(file(SLACK, `.transcript.json.reset-${now - 30_000}`), '{}')
subAgent('sub_new_1', SLACK, now - 10_000)

await check("the sub-agents saved before a Slack thread's reset are not its children in the app", async () => {
  useHarness.setState({
    activeSessionId: SLACK,
    sessions: [
      { ...row(SLACK), agentType: 'gateway-slack' },
      row('sub_old_1', SLACK), // seen live before the reset
    ],
  })
  await useHarness.getState().hydrateFromDisk()
  assert.deepEqual(children(SLACK), ['sub_new_1'])
  for (const p of [file('sub_old_1', '.transcript.json'), file('sub_old_1', '.subagent.json')]) {
    assert.ok(fs.existsSync(p), `${path.basename(p)} is gone`)
  }
})

await check('a reset during the thread\'s first turn, marked with no file set aside, hides them too', async () => {
  const thread = 'freyja:slack:T1:channel:C1:2.0'
  writeTranscript(thread)
  subAgent('sub_old_2', thread, now - 60_000)
  fs.writeFileSync(file(thread, `.reset-${now - 30_000}`), '')
  useHarness.setState({ activeSessionId: thread, sessions: [{ ...row(thread), agentType: 'gateway-slack' }] })
  await useHarness.getState().hydrateFromDisk()
  assert.deepEqual(children(thread), [])
})

await check('the sub-agents of a thread never reset stay its children', async () => {
  const thread = 'freyja:slack:T1:channel:C1:3.0'
  writeTranscript(thread)
  subAgent('sub_kept_3', thread, now - 60_000)
  useHarness.setState({ activeSessionId: thread, sessions: [{ ...row(thread), agentType: 'gateway-slack' }] })
  await useHarness.getState().hydrateFromDisk()
  assert.deepEqual(children(thread), ['sub_kept_3'])
})

fs.rmSync(home, { recursive: true, force: true })
if (failures) {
  console.log(`\n${failures} failed`)
  process.exit(1)
}
console.log('\nall passed')
process.exit(0)
