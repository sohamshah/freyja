// Delete on a scheduled job's session in the app says what the bridge did.
// While another process owns the scheduler's tick, the bridge keeps the
// session's files (the job still runs on them) and only drops the app's
// copy; the toast said "Session deleted" all the same. Run from the repo
// root:
//   npx tsx test-job-session-hide.mjs
import assert from 'node:assert/strict'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'

const home = fs.mkdtempSync(path.join(os.tmpdir(), 'freyja-job-hide-test-'))
process.env.HOME = home
const { useHarness } = await import('./src/renderer/state/store.ts')

const JOB = 'scheduler:sched_19f0'
const SUB = 'sub_19f0sched_1'

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

function harness(calls) {
  globalThis.window = globalThis.window ?? {}
  window.harness = {
    sessionHide: async (id) => { calls.push(['hide', id]); return { ok: true } },
    sessionDelete: async (id) => { calls.push(['delete', id]); return { ok: true } },
    sendCommand: async (cmd) => { calls.push(['command', cmd.type]); return { ok: true } },
  }
  useHarness.setState({
    toast: null,
    activeSessionId: 'session-other',
    sessions: [
      { id: 'session-other', title: 'o', createdAt: 1, updatedAt: 1 },
      { id: JOB, title: 'digest', createdAt: 1, updatedAt: 1 },
      { id: SUB, title: 'sub', createdAt: 1, updatedAt: 1, parentSessionId: JOB },
    ],
  })
}

const toast = () => useHarness.getState().toast?.message ?? ''
const answer = (filesKept) =>
  useHarness.getState().handleEvent({ type: 'session_deleted', deletedSessionId: JOB, filesKept })
const settle = () => new Promise((resolve) => setTimeout(resolve, 0))

await check("Delete on a job's session whose files the bridge kept says it is hidden and the job keeps it", async () => {
  const calls = []
  harness(calls)
  await useHarness.getState().deleteSession(JOB)
  assert.deepEqual(calls, [['delete', JOB], ['delete', SUB], ['command', 'delete_session']])
  assert.deepEqual(useHarness.getState().sessions.map((s) => s.id), ['session-other'])
  assert.doesNotMatch(toast(), /deleted/)
  answer(true)
  await settle()
  assert.match(toast(), /^Hidden from the app\. The scheduled job keeps its session\.$/)
})

await check("Delete on a job's session the bridge deleted says so", async () => {
  harness([])
  await useHarness.getState().deleteSession(JOB)
  answer(false)
  await settle()
  assert.equal(toast(), 'Session deleted — 1 subagent also removed')
})

await check('an answer for a delete the app did not wait on shows nothing', async () => {
  harness([])
  answer(true)
  await settle()
  assert.equal(toast(), '')
})

fs.rmSync(home, { recursive: true, force: true })
if (failures) {
  console.log(`\n${failures} failed`)
  process.exit(1)
}
console.log('\nall passed')
process.exit(0)
