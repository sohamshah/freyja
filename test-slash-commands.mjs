// Which composer messages run as slash commands. A message that only starts
// with a slash (a pasted path) used to be taken for an unknown command: it
// was not sent and the composer was emptied. Run from the repo root:
//   npx tsx test-slash-commands.mjs
import assert from 'node:assert/strict'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { splitSlashCommand, submitAction } from './src/renderer/lib/slash.ts'

process.env.HOME = fs.mkdtempSync(path.join(os.tmpdir(), 'freyja-slash-test-'))
globalThis.window = globalThis.window ?? {}
window.harness = { sendCommand: async () => ({ ok: true }) }
const { useHarness } = await import('./src/renderer/state/store.ts')

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

/** What the composer does on Enter (InputDock's submit). */
function submit(content, held = null) {
  const action = submitAction(content, (n, a) => useHarness.getState().runSlashCommand(n, a), held)
  return { command: 'command', send: 'message', hold: 'held' }[action.kind]
}

await check('a pasted path is sent as a message', () => {
  assert.equal(splitSlashCommand('/Users/me/app/log.txt shows a crash, why?'), null)
  assert.equal(submit('/Users/me/app/log.txt shows a crash, why?'), 'message')
  assert.equal(submit('/etc/hosts'), 'message')
})

await check('a known command runs with its arguments, a newline included', () => {
  assert.deepEqual(splitSlashCommand('  /goal\nship the fix  '), { name: '/goal', args: 'ship the fix' })
  useHarness.setState({ toast: null })
  assert.equal(submit('/usage'), 'command')
  assert.match(useHarness.getState().toast?.message ?? '', /^context /)
})

await check('a mistyped command is held once with a suggestion, then sent on a second Enter', () => {
  const first = submitAction('/compcat', () => false, null)
  assert.deepEqual(first, { kind: 'hold', name: '/compcat', suggestion: '/compact' })
  assert.equal(submit('/modle gpt-6.2-sol'), 'held')
  assert.equal(submit('/modle gpt-6.2-sol', '/modle gpt-6.2-sol'), 'message')
  assert.equal(submitAction('/zzzzzz', () => false, null).suggestion, null)
})

if (failures) {
  console.log(`\n${failures} failed`)
  process.exit(1)
}
console.log('\nall passed')
