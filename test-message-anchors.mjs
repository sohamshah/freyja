// Commands anchored on a message (edit, rerun, delete, pin, fork) in the
// renderer store: what they send the bridge about that message. Drives the
// real store actions and checks the commands. Run from the repo root:
//   npx tsx test-message-anchors.mjs
import assert from 'node:assert/strict'
import { useHarness } from './src/renderer/state/store.ts'

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

if (failures) {
  console.log(`\n${failures} failed`)
  process.exit(1)
}
console.log('\nall passed')
