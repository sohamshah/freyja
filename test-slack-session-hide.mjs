// Delete on a Slack session in the app hides it and keeps every file. The
// gateway runs the thread and saves its transcript; deleting it in the app
// removed that file under a running process, and the user never deletes
// sessions. Run from the repo root:
//   npx tsx test-slack-session-hide.mjs
import assert from 'node:assert/strict'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'

// The main-process persistence module resolves ~/.freyja from HOME when it
// loads (imported below, after this).
const home = fs.mkdtempSync(path.join(os.tmpdir(), 'freyja-slack-hide-test-'))
process.env.HOME = home
const persistence = await import('./src/main/persistence.ts')
const { useHarness } = await import('./src/renderer/state/store.ts')
const sessionsDir = path.join(home, '.freyja', 'sessions')
fs.mkdirSync(sessionsDir, { recursive: true })

const SLACK = 'freyja:slack:T1:channel:C1:1.0'
const SUB = 'sub_abc_1'
const file = (id, suffix) => path.join(sessionsDir, `${id.replace(/[^a-zA-Z0-9._-]/g, '_')}${suffix}`)
const writeTranscript = (id, metadata = {}) =>
  fs.writeFileSync(file(id, '.transcript.json'), JSON.stringify({
    version: 1, session_id: id, metadata, transcript: { entries: [], head_id: null },
  }))
const listed = () => persistence.listSessions().map((row) => row.id)
const touch = (p, ms) => fs.utimesSync(p, new Date(ms), new Date(ms))

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

writeTranscript(SLACK)
writeTranscript(SUB, { parent_session_id: SLACK })
fs.writeFileSync(file(SLACK, '.json'), JSON.stringify({ version: 1, id: SLACK, title: 't', slice: {} }))
fs.writeFileSync(file(SLACK, '.events.jsonl'), '{"type":"log"}\n')
const past = Date.now() - 60_000
for (const p of [file(SLACK, '.transcript.json'), file(SUB, '.transcript.json')]) touch(p, past)

await check('a Slack thread and its sub-agent are listed from their transcripts', () => {
  assert.deepEqual(listed().sort(), [SLACK, SUB].sort())
})

await check('hiding a Slack session keeps its files and takes it and its sub-agents off the list', () => {
  persistence.hideSession(SLACK)
  assert.deepEqual(listed(), [])
  for (const p of [file(SLACK, '.transcript.json'), file(SLACK, '.json'), file(SLACK, '.events.jsonl'), file(SUB, '.transcript.json')]) {
    assert.ok(fs.existsSync(p), `${path.basename(p)} is gone`)
  }
})

await check('a hidden Slack session is listed again once its thread has a new turn', () => {
  touch(file(SLACK, '.transcript.json'), Date.now() + 5_000)
  assert.deepEqual(listed().sort(), [SLACK, SUB].sort())
})

await check('Delete on a Slack session in the app hides it, and deletes and sends nothing', async () => {
  const calls = []
  globalThis.window = globalThis.window ?? {}
  window.harness = {
    sessionHide: async (id) => { calls.push(['hide', id]); return { ok: true } },
    sessionDelete: async (id) => { calls.push(['delete', id]); return { ok: true } },
    sendCommand: async (cmd) => { calls.push(['command', cmd.type]) },
  }
  useHarness.setState({
    activeSessionId: 'session-other',
    sessions: [
      { id: 'session-other', title: 'o', createdAt: 1, updatedAt: 1 },
      { id: SLACK, title: 's', createdAt: 1, updatedAt: 1, agentType: 'gateway-slack' },
      { id: SUB, title: 'sub', createdAt: 1, updatedAt: 1, parentSessionId: SLACK, agentType: 'gateway-subagent' },
    ],
  })
  await useHarness.getState().deleteSession(SLACK)
  assert.deepEqual(calls, [['hide', SLACK]])
  assert.deepEqual(useHarness.getState().sessions.map((s) => s.id), ['session-other'])
  assert.match(useHarness.getState().toast?.message ?? '', /stays in Slack/)
})

await check("Delete on a Slack thread's sub-agent hides it too", async () => {
  const calls = []
  window.harness = {
    sessionHide: async (id) => { calls.push(['hide', id]); return { ok: true } },
    sessionDelete: async (id) => { calls.push(['delete', id]); return { ok: true } },
    sendCommand: async (cmd) => { calls.push(['command', cmd.type]) },
  }
  useHarness.setState({
    activeSessionId: 'session-other',
    sessions: [
      { id: 'session-other', title: 'o', createdAt: 1, updatedAt: 1 },
      { id: SLACK, title: 's', createdAt: 1, updatedAt: 1, agentType: 'gateway-slack' },
      { id: SUB, title: 'sub', createdAt: 1, updatedAt: 1, parentSessionId: SLACK, agentType: 'general' },
    ],
  })
  await useHarness.getState().deleteSession(SUB)
  assert.deepEqual(calls, [['hide', SUB]])
})

await check('Delete on a desktop session still deletes it', async () => {
  const calls = []
  window.harness = {
    sessionHide: async (id) => { calls.push(['hide', id]); return { ok: true } },
    sessionDelete: async (id) => { calls.push(['delete', id]); return { ok: true } },
    sendCommand: async (cmd) => { calls.push(['command', cmd.type]) },
  }
  useHarness.setState({
    activeSessionId: 'session-other',
    sessions: [
      { id: 'session-other', title: 'o', createdAt: 1, updatedAt: 1 },
      { id: 'session-2', title: 'two', createdAt: 1, updatedAt: 1 },
    ],
  })
  await useHarness.getState().deleteSession('session-2')
  assert.deepEqual(calls, [['delete', 'session-2'], ['command', 'delete_session']])
})

await check('a Slack thread with only its settings saved, before its first message, is not listed', () => {
  // The gateway saves a mode or model set before a thread's first message
  // as transcript metadata with no entries, so a restart keeps it.
  const fresh = 'freyja:slack:T1:channel:C1:2.0'
  writeTranscript(fresh, { coordination_strategy: 'kanban' })
  touch(file(fresh, '.transcript.json'), past)
  assert.ok(!listed().includes(fresh))
  // Its first turn saves the transcript over it.
  fs.writeFileSync(file(fresh, '.transcript.json'), JSON.stringify({
    version: 1, session_id: fresh, metadata: { coordination_strategy: 'kanban' },
    transcript: { entries: [{ id: 'e1', message: { role: 'user', content: 'plan the release' } }], head_id: 'e1' },
  }))
  touch(file(fresh, '.transcript.json'), past + 1_000)
  assert.ok(listed().includes(fresh))
})

fs.rmSync(home, { recursive: true, force: true })
if (failures) {
  console.log(`\n${failures} failed`)
  process.exit(1)
}
console.log('\nall passed')
process.exit(0)
