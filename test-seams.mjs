// Tool calls as quiet lines that fold into a seam (components/seam/).
// Drives the real store with the bridge's event sequence, then renders the
// real stretch. Run from the repo root:
//   npx tsx test-seams.mjs

import assert from 'node:assert/strict'
import { createElement } from 'react'
import { flushSync } from 'react-dom'
import { createRoot } from 'react-dom/client'

// Needs a DOM, so install jsdom first (it is not a project dependency):
//   npm i --no-save jsdom
let JSDOM
try {
  ({ JSDOM } = await import('jsdom'))
} catch {
  console.error('jsdom not found — run:  npm i --no-save jsdom')
  process.exit(2)
}
const dom = new JSDOM('<!doctype html><body></body>', { pretendToBeVisual: true })
globalThis.window = dom.window
globalThis.document = dom.window.document
globalThis.requestAnimationFrame = dom.window.requestAnimationFrame.bind(dom.window)
globalThis.cancelAnimationFrame = dom.window.cancelAnimationFrame.bind(dom.window)
globalThis.IS_REACT_ACT_ENVIRONMENT = false
window.harness = {
  sendCommand: async () => {},
  sessionSave: async () => ({ ok: true }),
  sessionIndexSave: async () => ({ ok: true }),
  sessionLoad: async () => ({ ok: false }),
}
const { useHarness } = await import('./src/renderer/state/store.ts')
const { layoutSeam } = await import('./src/renderer/lib/seam.ts')
const { stepWords, wordsText, stretchPhrase, humanTool } = await import('./src/renderer/components/seam/stepWords.ts')
const { WorkStretch } = await import('./src/renderer/components/seam/WorkStretch.tsx')

const h = () => useHarness.getState()
const emit = (ev) => h().handleEvent({ sessionId: S, ...ev })
const S = 'session-local'

let failures = 0
async function check(name, fn) {
  try {
    await fn()
    console.log(`ok   ${name}`)
  } catch (err) {
    failures++
    console.log(`FAIL ${name}\n     ${err.message.split('\n').join('\n     ')}`)
  }
}
const sleep = (ms) => new Promise((r) => setTimeout(r, ms))

h().handleEvent({ type: 'ready', sessionId: S, mode: 'live', capabilities: {} })
emit({ type: 'turn_start', turnId: 't1' })

await check('calls issued together share a group; a later call starts its own', () => {
  emit({ type: 'tool_use_start', id: 'a', name: 'bash' })
  emit({ type: 'tool_use_start', id: 'b', name: 'bash' })
  emit({ type: 'tool_input_end', id: 'a', arguments: { command: 'ls', summary: 'List the folder' } })
  emit({ type: 'tool_input_end', id: 'b', arguments: { command: 'npm test', summary: 'Run the tests' } })
  emit({ type: 'tool_result', id: 'a', preview: 'ok', isError: false, durationMs: 40 })
  emit({ type: 'tool_result', id: 'b', preview: 'Error: 1 failed', isError: true, durationMs: 900 })
  emit({ type: 'tool_use_start', id: 'c', name: 'edit_file' })
  emit({ type: 'tool_input_end', id: 'c', arguments: { path: 'src/app/main.ts' } })
  emit({ type: 'tool_result', id: 'c', preview: 'edited', isError: false, durationMs: 12 })
  const tc = h().toolCalls
  assert.equal(tc.a.groupId, tc.b.groupId)
  assert.notEqual(tc.c.groupId, tc.b.groupId)
})

await check('a step reads in plain words', () => {
  const call = (name, args) => ({ id: 'x', name, status: 'success', startedAt: 0, arguments: args })
  assert.equal(wordsText(stepWords(call('bash', { command: 'ls -la', summary: 'List the folder' }))), 'List the folder')
  assert.equal(wordsText(stepWords(call('read_file', { path: 'src/a/b.ts', start_line: 3, end_line: 9 }))), 'Read src/a/b.ts, lines 3–9')
  assert.equal(wordsText(stepWords(call('tasks', { action: 'create', title: 'Ship it' }))), 'Add task “Ship it”')
  assert.equal(wordsText(stepWords(call('artifacts', { action: 'read', ref: '/p/artifacts/notes.md' }))), 'Read artifact /p/artifacts/notes.md')
  assert.equal(wordsText(stepWords(call('web_search', { queries: ['one', 'two'] }))), 'Search the web for one · two')
  assert.equal(humanTool('mcp__slack__slack_read_thread'), 'slack read thread')
  // An unknown tool never shows its list of argument names.
  assert.ok(!wordsText(stepWords(call('mystery', { action: 'go', target: 'x' }))).includes(','))
})

await check('a step carries a drawing of what it does, and unknown tools keep the ring', async () => {
  const { markKind } = await import('./src/renderer/components/seam/marks.tsx')
  const call = (name, args = {}) => ({ id: 'x', name, status: 'success', startedAt: 0, arguments: args })
  assert.equal(markKind(call('bash', { command: 'ls' })), 'run')
  assert.equal(markKind(call('read_file', { path: 'a.ts' })), 'read')
  assert.equal(markKind(call('grep', { pattern: 'x' })), 'look')
  assert.equal(markKind(call('web_search', { query: 'x' })), 'web')
  assert.equal(markKind(call('edit_file', { path: 'a.ts' })), 'edit')
  assert.equal(markKind(call('artifacts', { action: 'save', ref: 'n.md' })), 'edit')
  assert.equal(markKind(call('artifacts', { action: 'read', ref: 'n.md' })), 'read')
  assert.equal(markKind(call('talk', { to: 'Store' })), 'talk')
  assert.equal(markKind(call('mcp__slack__slack_read_thread')), 'step')
})

await check('a seam draws each kind of step, and squeezes to fit', () => {
  const units = [
    { k: 'think', secs: 4 },
    { k: 'up', ms: 900 },
    { k: 'down', ms: 20 },
    { k: 'fail' },
    { k: 'calls', n: 3, strands: ['ok', 'err', 'ok'] },
    { k: 'helpers', n: 2, strands: ['ok', 'out'] },
  ]
  const g = layoutSeam(units)
  assert.equal(g.units.length, units.length)
  const cls = g.pieces.flatMap((p) => p.paths.map((q) => q.cls))
  assert.ok(cls.includes('think'))
  assert.ok(cls.some((c) => c.includes('err')), 'a failure is drawn in red')
  assert.ok(cls.some((c) => c.includes('fray')), 'a snapped strand has frayed ends')
  assert.ok(cls.some((c) => c.includes('out')), 'a helper still out keeps a blue strand')
  assert.ok(layoutSeam(units, 120).width <= 121)
})

function mount(element) {
  const host = document.createElement('div')
  document.body.appendChild(host)
  const root = createRoot(host)
  flushSync(() => root.render(element))
  return { host, root, render: (el) => flushSync(() => root.render(el)) }
}

const items = [
  { kind: 'calls', ids: ['a', 'b'], groupId: h().toolCalls.a.groupId },
  { kind: 'call', id: 'c' },
]

await check('a finished stretch folds into a seam with its summary', () => {
  const { host, root } = mount(createElement(WorkStretch, { items, closed: true, liveIndex: -1 }))
  const stretch = host.querySelector('.wk-stretch')
  assert.equal(stretch.dataset.mode, 'folded')
  assert.ok(host.querySelectorAll('.wk-seam-svg path').length > 3)
  assert.equal(host.querySelector('.wk-seam-what').textContent, 'ran 2 commands and edited main.ts')
  assert.equal(host.querySelector('.wk-seam-trouble').textContent, '1 failed')
  assert.equal(host.querySelectorAll('.wk-step').length, 3)
  root.unmount()
  host.remove()
})

await check('the summary keeps trouble apart from the work', () => {
  const p = stretchPhrase(items, { calls: h().toolCalls, helpers: {} })
  assert.deepEqual(p.trouble, ['1 failed'])
})

await check('a stretch shows lines while the agent works, then gathers when the next paragraph starts', async () => {
  const { host, root, render } = mount(createElement(WorkStretch, { items, closed: false, liveIndex: -1 }))
  assert.equal(host.querySelector('.wk-stretch').dataset.mode, 'live')
  render(createElement(WorkStretch, { items, closed: true, liveIndex: -1 }))
  const stretch = host.querySelector('.wk-stretch')
  assert.equal(stretch.dataset.mode, 'folded')
  assert.ok(stretch.classList.contains('wk-gathering'), 'the fold animates')
  await sleep(2800)
  assert.ok(!stretch.classList.contains('wk-gathering'), 'and leaves nothing behind')
  assert.equal(host.querySelectorAll('.wk-ghost').length, 0)
  assert.equal(host.querySelector('.wk-seam-svg path').style.strokeDashoffset, '')
  root.unmount()
  host.remove()
})

await check('clicking the seam opens the lines under it', async () => {
  const { host, root } = mount(createElement(WorkStretch, { items, closed: true, liveIndex: -1 }))
  host.querySelector('.wk-seam-row').dispatchEvent(new window.MouseEvent('click', { bubbles: true }))
  await sleep(50)
  assert.equal(host.querySelector('.wk-stretch').dataset.mode, 'open')
  root.unmount()
  host.remove()
})

await check('a helper shows its task while it works, then its reply in its place', async () => {
  const { Helpers } = await import('./src/renderer/components/seam/Helpers.tsx')
  h().handleEvent({ type: 'session_spawned', sessionId: 'kid', parentSessionId: S, title: 'Store', model: 'm', task: 'Find the selectors', createdAt: Date.now() })
  emit({ type: 'subagent_spawn', record: { id: 'kid', label: 'Store', mode: 'background', state: 'running', task: 'Find the selectors', startedAt: Date.now(), elapsedMs: 0, tokensIn: 0, tokensOut: 0, toolsCalled: 0 } })
  h().handleEvent({ type: 'turn_start', turnId: 'k1', sessionId: 'kid' })
  h().handleEvent({ type: 'tool_use_start', id: 'k-a', name: 'read_file', sessionId: 'kid' })
  h().handleEvent({ type: 'tool_input_end', id: 'k-a', arguments: { path: 'src/state/store.ts' }, sessionId: 'kid' })
  h().handleEvent({ type: 'tool_result', id: 'k-a', preview: 'ok', isError: false, durationMs: 30, sessionId: 'kid' })
  const { host, root, render } = mount(createElement(Helpers, { ids: ['kid'], u: 0 }))
  const row = () => host.querySelector('.wk-helper')
  assert.equal(row().dataset.state, 'running')
  assert.equal(host.querySelector('.wk-helper-task').textContent, 'Find the selectors')
  assert.equal(host.querySelector('.wk-helper-step').textContent, 'Read src/state/store.ts')
  assert.ok(host.querySelectorAll('.wk-helper-seam path').length > 0, 'its own seam grows as it works')
  h().handleEvent({ type: 'session_completed', sessionId: 'kid', success: true, elapsedMs: 26000 })
  emit({ type: 'subagent_done', id: 'kid', result: 'Three selectors scan every message. More detail follows.', elapsedMs: 26000 })
  render(createElement(Helpers, { ids: ['kid'], u: 0 }))
  assert.equal(row().dataset.state, 'done')
  assert.equal(host.querySelector('.wk-helper-say').textContent, 'Three selectors scan every message')
  assert.equal(host.querySelector('.wk-helper-task'), null)
  root.unmount()
  host.remove()
})

console.log(failures ? `\n${failures} failed` : '\nall passed')
process.exit(failures ? 1 : 0)
