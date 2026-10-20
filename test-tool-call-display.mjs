// A `call_tool` header names the tool it runs, and a `summarize_context`
// header shows the agent's reason for compacting. Drives the real store with
// the event sequence the bridge emits (taken from a live session log), then
// renders the real chip and lane components. Run from the repo root:
//   npx tsx test-tool-call-display.mjs

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
const dom = new JSDOM('<!doctype html><body></body>')
globalThis.window = dom.window
globalThis.document = dom.window.document
globalThis.IS_REACT_ACT_ENVIRONMENT = false
window.harness = {
  sendCommand: async () => {},
  sessionSave: async () => ({ ok: true }),
  sessionIndexSave: async () => ({ ok: true }),
  sessionLoad: async () => ({ ok: false }),
}
const { useHarness } = await import('./src/renderer/state/store.ts')
const { resolveToolCall } = await import('./src/renderer/lib/toolCall.ts')
const { ToolCallChip, describeCall, summarizePartialJson } = await import(
  './src/renderer/components/ToolCallChip.tsx'
)
const { ParallelToolGroup } = await import('./src/renderer/components/ParallelToolGroup.tsx')

const h = () => useHarness.getState()
const emit = (ev) => h().handleEvent(ev)
const S = 'session-local'

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

// Mount the real component against the live store and return its markup.
function renderToStaticMarkup(element) {
  const host = document.createElement('div')
  document.body.appendChild(host)
  const root = createRoot(host)
  flushSync(() => root.render(element))
  const html = host.innerHTML
  root.unmount()
  host.remove()
  return html
}

// The row's text, in order, with the markup stripped.
const text = (html) => html.replace(/<[^>]+>/g, '|').replace(/\|+/g, '|')

emit({ type: 'ready', sessionId: S, mode: 'live', capabilities: {} })

const REASON = 'The Jira-comment task is complete; begin a focused recheck of the current Bugbot report.'
const INNER = { scope: 'all', level: 'chapter', reason: REASON, preserve_facts: ['a fact'] }

// What the bridge sends for one call_tool call: the wrapper's args, then the
// same id again for the inner tool, with the inner tool's own args and name.
function runCallTool(id, toolName, innerArgs) {
  emit({ type: 'tool_use_start', sessionId: S, id, name: 'call_tool' })
  const wrapped = { tool_name: toolName, arguments: innerArgs }
  const json = JSON.stringify(wrapped)
  emit({ type: 'tool_input_delta', sessionId: S, id, partialJson: json.slice(0, 40) })
  emit({ type: 'tool_input_delta', sessionId: S, id, partialJson: json.slice(40) })
  emit({ type: 'tool_input_end', sessionId: S, id, toolName: 'call_tool', arguments: wrapped })
  emit({ type: 'tool_input_end', sessionId: S, id, toolName, arguments: innerArgs })
  emit({ type: 'tool_result', sessionId: S, id, preview: 'ok', isError: false, durationMs: 760 })
}
const record = (id) => h().toolCalls[id]

console.log('\nresolving the tool a call runs')
check('a plain tool is itself', () => {
  const r = resolveToolCall({ name: 'bash', arguments: { command: 'ls' } })
  assert.equal(r.name, 'bash')
  assert.deepEqual(r.arguments, { command: 'ls' })
})
check('streaming: the name shows once its closing quote has arrived', () => {
  const partial = '{"tool_name":"summarize_context","arguments":{"scope":"all","reason":"Compact before'
  const r = resolveToolCall({ name: 'call_tool', partialJson: partial })
  assert.equal(r.name, 'summarize_context')
  assert.equal(summarizePartialJson(r.name, r.partialJson), 'Compact before')
})
check('streaming: a half-streamed name is not shown', () => {
  const r = resolveToolCall({ name: 'call_tool', partialJson: '{"tool_name":"summar' })
  assert.equal(r.name, 'call_tool')
})
check('streaming: nothing yet is call_tool', () => {
  assert.equal(resolveToolCall({ name: 'call_tool', partialJson: '' }).name, 'call_tool')
  assert.equal(resolveToolCall({ name: 'call_tool' }).name, 'call_tool')
})
check('issued: the wrapper args give the name and the inner args', () => {
  const r = resolveToolCall({ name: 'call_tool', arguments: { tool_name: ' bash ', arguments: { command: 'ls' } } })
  assert.equal(r.name, 'bash')
  assert.deepEqual(r.arguments, { command: 'ls' })
})
check('issued: arguments sent as a JSON string are parsed', () => {
  const r = resolveToolCall({ name: 'call_tool', arguments: { tool_name: 'bash', arguments: '{"command":"ls"}' } })
  assert.deepEqual(r.arguments, { command: 'ls' })
})
check('issued: arguments that are not JSON leave the name, with no args', () => {
  const r = resolveToolCall({ name: 'call_tool', arguments: { tool_name: 'bash', arguments: '{"comm' } })
  assert.equal(r.name, 'bash')
  assert.equal(r.arguments, undefined)
})
check('ran: invokes wins, and the args are already the inner tool\'s', () => {
  const r = resolveToolCall({ name: 'call_tool', invokes: 'bash', arguments: { command: 'ls', tool_name: 'x' } })
  assert.equal(r.name, 'bash')
  assert.deepEqual(r.arguments, { command: 'ls', tool_name: 'x' })
})
check('a call_tool record from before the bridge sent names stays call_tool', () => {
  assert.equal(resolveToolCall({ name: 'call_tool', arguments: INNER }).name, 'call_tool')
})
check('a tool whose own args have tool_name is not unwrapped', () => {
  assert.equal(resolveToolCall({ name: 'tool_search', arguments: { tool_name: 'bash' } }).name, 'tool_search')
})

console.log('\nthe store keeps the inner name')
check('invokes is set by the inner event, and arguments become the inner args', () => {
  runCallTool('c1', 'summarize_context', INNER)
  assert.equal(record('c1').name, 'call_tool')
  assert.equal(record('c1').invokes, 'summarize_context')
  assert.deepEqual(record('c1').arguments, INNER)
  assert.equal(record('c1').status, 'success')
})
check('the wrapper event alone does not set invokes', () => {
  emit({ type: 'tool_use_start', sessionId: S, id: 'c2', name: 'call_tool' })
  emit({ type: 'tool_input_end', sessionId: S, id: 'c2', toolName: 'call_tool', arguments: { tool_name: 'bash', arguments: {} } })
  assert.equal(record('c2').invokes, undefined)
  assert.equal(resolveToolCall(record('c2')).name, 'bash')
})
check('a direct call from an older bridge (no toolName) gets no invokes', () => {
  emit({ type: 'tool_use_start', sessionId: S, id: 'c3', name: 'bash' })
  emit({ type: 'tool_input_end', sessionId: S, id: 'c3', arguments: { command: 'ls' } })
  assert.equal(record('c3').invokes, undefined)
})
check('a direct call from the new bridge gets no invokes', () => {
  emit({ type: 'tool_use_start', sessionId: S, id: 'c4', name: 'bash' })
  emit({ type: 'tool_input_end', sessionId: S, id: 'c4', toolName: 'bash', arguments: { command: 'ls' } })
  assert.equal(record('c4').invokes, undefined)
})

console.log('\ncompaction reason')
check('describeCall returns the reason for summarize_context only', () => {
  assert.equal(describeCall('summarize_context', INNER), REASON)
  assert.equal(describeCall('summarize_context', { scope: 'all' }), '')
  assert.equal(describeCall('summarize_context', { reason: '   ' }), '')
  assert.equal(describeCall('read_file', { reason: 'x' }), '')
})
check('a summary on summarize_context does not hide the reason', () => {
  assert.equal(describeCall('summarize_context', { reason: 'why', summary: 'what' }), 'why')
})

console.log('\nrendered headers')
check('chip: a finished call_tool shows the inner tool and the reason', () => {
  const t = text(renderToStaticMarkup(createElement(ToolCallChip, { id: 'c1' })))
  assert.ok(t.includes('summarize_context'), t)
  assert.ok(t.includes(REASON), t)
  assert.ok(!t.includes('call_tool'), t)
})
check('chip: a wrapped call that has not run shows the inner tool', () => {
  const t = text(renderToStaticMarkup(createElement(ToolCallChip, { id: 'c2' })))
  assert.ok(t.includes('bash'), t)
  assert.ok(!t.includes('call_tool'), t)
})
check('chip: a streaming call_tool shows the inner tool and the reason so far', () => {
  emit({ type: 'tool_use_start', sessionId: S, id: 'c5', name: 'call_tool' })
  emit({
    type: 'tool_input_delta', sessionId: S, id: 'c5',
    partialJson: '{"tool_name":"summarize_context","arguments":{"scope":"all","reason":"Compact now becau',
  })
  const t = text(renderToStaticMarkup(createElement(ToolCallChip, { id: 'c5' })))
  assert.ok(t.includes('summarize_context'), t)
  assert.ok(t.includes('Compact now becau'), t)
  assert.ok(!t.includes('call_tool'), t)
})
check('chip: a call_tool whose name has not streamed yet reads call_tool', () => {
  emit({ type: 'tool_use_start', sessionId: S, id: 'c6', name: 'call_tool' })
  const t = text(renderToStaticMarkup(createElement(ToolCallChip, { id: 'c6' })))
  assert.ok(t.includes('call_tool'), t)
})
check('chip: a direct summarize_context shows the reason', () => {
  emit({ type: 'tool_use_start', sessionId: S, id: 'c7', name: 'summarize_context' })
  emit({ type: 'tool_input_end', sessionId: S, id: 'c7', toolName: 'summarize_context', arguments: INNER })
  // Finished, so the args panel is closed and only the header can show the reason.
  emit({ type: 'tool_result', sessionId: S, id: 'c7', preview: 'ok', isError: false, durationMs: 5 })
  const t = text(renderToStaticMarkup(createElement(ToolCallChip, { id: 'c7' })))
  assert.ok(t.includes(REASON), t)
})
check('lanes: every call_tool lane names its tool, and bash shows its summary', () => {
  runCallTool('p1', 'bash', { command: 'gh pr view 16716', summary: 'Confirm the earlier PR' })
  runCallTool('p2', 'read_file', { path: '/tmp/a.txt' })
  runCallTool('p3', 'summarize_context', INNER)
  const html = renderToStaticMarkup(createElement(ParallelToolGroup, { ids: ['p1', 'p2', 'p3'] }))
  const t = text(html)
  assert.ok(!t.includes('call_tool'), t)
  for (const want of ['bash', 'Confirm the earlier PR', 'read_file', '/tmp/a.txt', 'summarize_context', REASON]) {
    assert.ok(t.includes(want), `${want} missing from ${t}`)
  }
})
check('lanes: the category color follows the inner tool', () => {
  const html = renderToStaticMarkup(createElement(ParallelToolGroup, { ids: ['p1'] }))
  // Category hues are theme tokens (styles/globals.css): the lane carries
  // the shell token, which the light and dark shells resolve differently.
  assert.ok(html.includes('var(--tool-shell)'), `bash is the shell color: ${html.slice(0, 400)}`)
})

if (failures) {
  console.log(`\n${failures} failed`)
  process.exit(1)
}
console.log('\nall passed')
process.exit(0)
