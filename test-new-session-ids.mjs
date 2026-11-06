// Two sessions started in the same millisecond get different ids. The id
// was the clock alone, so Morning Room's run-all loop could start two
// sessions under one id. Run from the repo root:
//   npx tsx test-new-session-ids.mjs
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

await check('two sessions started in one millisecond get different ids', async () => {
  const now = Date.now
  Date.now = () => 1_800_000_000_000
  try {
    await H().newSession()
    const first = H().activeSessionId
    await H().newSession()
    const second = H().activeSessionId
    assert.notEqual(first, second)
    const ids = sent.filter((c) => c.type === 'new_session').map((c) => c.sessionId)
    assert.deepEqual(ids, [first, second], 'the bridge is told the same ids')
  } finally {
    Date.now = now
  }
})

await check('the id still starts with session- and the clock, in characters a file name keeps', async () => {
  await H().newSession()
  const id = H().activeSessionId
  assert.match(id, /^session-[0-9a-z]+-[0-9a-z]+$/)
  const stamp = id.split('-')[1]
  assert.ok(Math.abs(parseInt(stamp, 36) - Date.now()) < 60_000, `${stamp} is the clock`)
})

if (failures) {
  console.log(`\n${failures} failed`)
  process.exit(1)
}
console.log('\nall passed')
