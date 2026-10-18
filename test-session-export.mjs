// Session exports record what happened: a `call_tool` call is
// call_tool({tool_name, arguments}) as the model made it, with the tool that
// ran beside it, in the sidebar download, the /export trace, and the bundle's
// per-model-call telemetry. Run from the repo root:
//   npx tsx test-session-export.mjs

import assert from 'node:assert/strict'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'

// The main-process modules resolve ~/.freyja from HOME when they load.
const home = fs.mkdtempSync(path.join(os.tmpdir(), 'freyja-export-test-'))
process.env.HOME = home

globalThis.window = globalThis.window ?? {}
window.harness = {
  sendCommand: async () => {},
  sessionSave: async () => ({ ok: true }),
  sessionIndexSave: async () => ({ ok: true }),
  sessionLoad: async () => ({ ok: false }),
}
const { useHarness } = await import('./src/renderer/state/store.ts')
const { exportToolCall, exportToolStats } = await import('./src/renderer/lib/toolCall.ts')
const persistence = await import('./src/main/persistence.ts')

const emit = (ev) => useHarness.getState().handleEvent(ev)
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

emit({ type: 'ready', sessionId: S, mode: 'live', capabilities: {} })

// The event sequence the bridge emits for one call_tool call (see the
// bridge's traced registry): the wrapper's args, then the same id again for
// the inner tool, with the inner tool's name and its own args.
const WRAPPER = (tool, args) => ({ tool_name: tool, arguments: args })
function callTool(id, tool, args, { groupId } = {}) {
  emit({ type: 'tool_use_start', sessionId: S, id, name: 'call_tool' })
  emit({ type: 'tool_input_end', sessionId: S, id, toolName: 'call_tool', arguments: WRAPPER(tool, args) })
  emit({ type: 'tool_input_end', sessionId: S, id, toolName: tool, arguments: args })
  emit({ type: 'tool_result', sessionId: S, id, preview: 'ok', isError: false, durationMs: 12 })
  if (groupId) {
    const tc = useHarness.getState().toolCalls[id]
    useHarness.setState({ toolCalls: { ...useHarness.getState().toolCalls, [id]: { ...tc, groupId } } })
  }
}
const rec = (id) => useHarness.getState().toolCalls[id]

console.log('\nthe store keeps what the model sent')
await check('the wrapper args survive the inner event', () => {
  callTool('w1', 'bash', { command: 'ls', summary: 'list' })
  assert.deepEqual(rec('w1').wrapperArguments, WRAPPER('bash', { command: 'ls', summary: 'list' }))
  assert.deepEqual(rec('w1').arguments, { command: 'ls', summary: 'list' })
  assert.equal(rec('w1').invokes, 'bash')
})
await check('a repeated inner event keeps the first wrapper args', () => {
  emit({ type: 'tool_input_end', sessionId: S, id: 'w1', toolName: 'bash', arguments: { command: 'ls', summary: 'list' } })
  assert.deepEqual(rec('w1').wrapperArguments, WRAPPER('bash', { command: 'ls', summary: 'list' }))
})
await check('a direct call has no wrapper args', () => {
  emit({ type: 'tool_use_start', sessionId: S, id: 'd1', name: 'bash' })
  emit({ type: 'tool_input_end', sessionId: S, id: 'd1', toolName: 'bash', arguments: { command: 'pwd' } })
  emit({ type: 'tool_result', sessionId: S, id: 'd1', preview: '/tmp', isError: false, durationMs: 4 })
  assert.equal(rec('d1').wrapperArguments, undefined)
  assert.equal(rec('d1').invokes, undefined)
})

console.log('\nthe sidebar download')
await check('a call_tool call reads as the model made it, with the tool that ran', () => {
  const out = exportToolCall(rec('w1'))
  assert.equal(out.name, 'call_tool')
  assert.deepEqual(out.arguments, WRAPPER('bash', { command: 'ls', summary: 'list' }))
  assert.equal(out.invokes, 'bash')
  assert.deepEqual(out.invokedArguments, { command: 'ls', summary: 'list' })
  assert.equal(out.argumentsRebuilt, undefined)
})
await check('a direct call is unchanged: no invokes, no invokedArguments', () => {
  const out = exportToolCall(rec('d1'))
  assert.equal(out.name, 'bash')
  assert.deepEqual(out.arguments, { command: 'pwd' })
  assert.ok(!('invokes' in out) && !('invokedArguments' in out) && !('argumentsRebuilt' in out))
})
await check('a call whose wrapper event was missed is rebuilt, and says so', () => {
  const out = exportToolCall({
    id: 'r1', name: 'call_tool', invokes: 'read_file', arguments: { path: '/a' },
    status: 'success', startedAt: 1,
  })
  assert.deepEqual(out.arguments, WRAPPER('read_file', { path: '/a' }))
  assert.equal(out.argumentsRebuilt, true)
  assert.deepEqual(out.invokedArguments, { path: '/a' })
})
await check('a call_tool record from before the fix is left as stored', () => {
  const out = exportToolCall({
    id: 'o1', name: 'call_tool', arguments: { path: '/a' }, status: 'success', startedAt: 1,
  })
  assert.deepEqual(out.arguments, { path: '/a' })
  assert.ok(!('invokes' in out) && !('argumentsRebuilt' in out))
})
await check('the parallel group, partial JSON and file changes are kept', () => {
  callTool('g1', 'read_file', { path: '/b' }, { groupId: 'grp-1' })
  assert.equal(exportToolCall(rec('g1')).groupId, 'grp-1')
  const mid = exportToolCall({ id: 'm1', name: 'bash', status: 'running', startedAt: 1, partialJson: '{"comm' })
  assert.equal(mid.partialJson, '{"comm')
  const changed = { id: 'cs1', files: [{ path: '/b' }] }
  assert.deepEqual(exportToolCall({ ...rec('g1'), fileChangeSet: changed }).fileChangeSet, changed)
})
await check('image bytes stay out; size and kind stay in', () => {
  const b64 = 'A'.repeat(400)
  const out = exportToolCall({
    id: 'i1', name: 'screenshot', status: 'success', startedAt: 1,
    frame: { pngBase64: b64, mimeType: 'image/png', width: 10, height: 5, takenAt: 2 },
    resultImages: [{ frameId: 'f1', mimeType: 'image/png', width: 1, height: 1, takenAt: 3, byteSize: 77, label: 'x' }],
  })
  assert.ok(!('pngBase64' in out.frame))
  assert.equal(out.frame.byteSize, 300)
  assert.deepEqual([out.frame.width, out.frame.height, out.frame.mimeType], [10, 5, 'image/png'])
  assert.equal(out.resultImages[0].byteSize, 77)
  assert.equal(out.resultImages[0].label, 'x')
  assert.ok(!JSON.stringify(out).includes(b64))
})
await check('stats count by the tool that ran', () => {
  const stats = exportToolStats(Object.values(useHarness.getState().toolCalls))
  assert.ok(!('call_tool' in stats), JSON.stringify(stats))
  assert.deepEqual(stats.bash, { count: 2, success: 2, failure: 0 })
  assert.deepEqual(stats.read_file, { count: 1, success: 1, failure: 0 })
})
await check('stats count a wrapped call that never ran under the tool it asked for', () => {
  const stats = exportToolStats([
    { id: 'e1', name: 'call_tool', status: 'error', isError: true, startedAt: 1,
      arguments: WRAPPER('no_such_tool', {}) },
  ])
  assert.deepEqual(stats, { no_such_tool: { count: 1, success: 0, failure: 1 } })
})
await check('the whole export survives JSON', () => {
  const out = JSON.parse(JSON.stringify(Object.values(useHarness.getState().toolCalls).map(exportToolCall)))
  assert.equal(out.find((c) => c.id === 'w1').invokes, 'bash')
})

console.log('\n/export: trace and bundle')
const sid = 'session-export-test'
const sessionsDir = path.join(home, '.freyja', 'sessions')
fs.mkdirSync(sessionsDir, { recursive: true })
const wrapped = rec('w1')
fs.writeFileSync(path.join(sessionsDir, `${sid}.json`), JSON.stringify({
  version: 1, id: sid, title: 't', model: 'm', workspace: '~/', createdAt: 1, updatedAt: 2,
  messageCount: 2, totalInputTokens: 0, totalOutputTokens: 0, cacheReadTokens: 0,
  slice: {
    messages: [
      { id: 'a1', role: 'assistant', createdAt: 1, parts: [
        { type: 'tool_call', toolCallId: 'w1' },
        { type: 'tool_call', toolCallId: 'd1' },
        { type: 'tool_call', toolCallId: 'o1' },
        { type: 'tool_call', toolCallId: 'o2' },
      ] },
    ],
    toolCalls: {
      w1: wrapped,
      d1: rec('d1'),
      o1: { id: 'o1', name: 'call_tool', arguments: { path: '/a' }, status: 'success', startedAt: 1, durationMs: 3 },
      o2: { id: 'o2', name: 'call_tool', arguments: { path: '/z' }, status: 'success', startedAt: 1, durationMs: 5 },
    },
  },
}))
// The raw transcript holds the calls as the model sent them. o1 is a record
// from before the app kept the wrapper args; o2 has no raw entry.
const projectDir = path.join(home, '.freyja', 'projects', sid)
fs.mkdirSync(projectDir, { recursive: true })
fs.writeFileSync(path.join(projectDir, 'raw_messages.jsonl'), [
  { ts: 1, session_id: sid, turn_id: 'turn-1', message: { role: 'assistant', content: '', tool_calls: [
    { id: 'o1', name: 'call_tool', arguments: WRAPPER('read_file', { path: '/a' }) },
  ] } },
  { ts: 2, session_id: sid, turn_id: 'turn-1', message: { role: 'tool_result', tool_call_id: 'o1', content: 'x' } },
].map((r) => JSON.stringify(r)).join('\n') + '\n')
const telemetryDir = path.join(home, '.freyja', 'telemetry')
fs.mkdirSync(telemetryDir, { recursive: true })
const rows = [
  { type: 'llm_call_metric', session_id: sid, model: 'm1', input_tokens: 10, output_tokens: 2, cache_read_tokens: 5, cache_write_tokens: 1, cost_usd: 0.1, duration_ms: 900, ts: 1 },
  { type: 'llm_call_metric', session_id: 'someone-else', model: 'm2', ts: 2 },
  { type: 'tool_call_metric', session_id: sid, tool_call_id: 'w1', tool_name: 'call_tool', result_bytes: 450945, ok: true, ts: 3 },
  { type: 'llm_call_metric', session_id: sid, model: 'm1', input_tokens: 20, output_tokens: 4, ts: 4 },
]
fs.writeFileSync(path.join(telemetryDir, 'compaction.jsonl'), rows.map((r) => JSON.stringify(r)).join('\n') + '\n')

const dest = path.join(home, 'out.json')
const res = await persistence.exportSessionToFile(sid, dest)
await check('the export succeeds and writes three files', () => {
  assert.equal(res.ok, true, JSON.stringify(res))
  for (const p of [res.jsonPath, res.bundlePath, res.tracePath]) assert.ok(fs.existsSync(p), p)
})
await check('the .json keeps the wrapper args, the tool that ran and its args', () => {
  const saved = JSON.parse(fs.readFileSync(res.jsonPath, 'utf8')).slice.toolCalls.w1
  assert.equal(saved.invokes, 'bash')
  assert.deepEqual(saved.wrapperArguments, WRAPPER('bash', { command: 'ls', summary: 'list' }))
  assert.deepEqual(saved.arguments, { command: 'ls', summary: 'list' })
})
await check('the trace names the tool behind call_tool and shows what the model sent', () => {
  const trace = fs.readFileSync(res.tracePath, 'utf8')
  assert.match(trace, /TOOL call_tool -> bash \(12ms\) args=\{"tool_name":"bash","arguments":\{"command":"ls"/)
  assert.match(trace, /TOOL bash \(\d+ms\)|TOOL bash \(\?ms\)/)
  assert.ok(!/TOOL bash -> /.test(trace), 'a direct call names no second tool')
})
await check('the trace recovers an older call_tool record from the raw transcript', () => {
  const trace = fs.readFileSync(res.tracePath, 'utf8')
  assert.match(trace, /TOOL call_tool -> read_file \(3ms\) args=\{"tool_name":"read_file","arguments":\{"path":"\/a"\}\}/)
})
await check('an older record with no raw entry is shown as stored', () => {
  const trace = fs.readFileSync(res.tracePath, 'utf8')
  assert.match(trace, /TOOL call_tool \(5ms\) args=\{"path":"\/z"\}/)
})
await check('the bundle carries this session\'s telemetry by row type, in order', () => {
  const bundle = JSON.parse(fs.readFileSync(res.bundlePath, 'utf8'))
  assert.equal(bundle.version, 3)
  assert.deepEqual(Object.keys(bundle.telemetry).sort(), ['llm_call_metric', 'tool_call_metric'])
  assert.deepEqual(bundle.telemetry.llm_call_metric.map((r) => r.input_tokens), [10, 20])
  assert.equal(bundle.telemetry.llm_call_metric[0].cache_read_tokens, 5)
  assert.equal(bundle.telemetry.llm_call_metric[0].cost_usd, 0.1)
  assert.equal(bundle.telemetry.tool_call_metric[0].result_bytes, 450945)
  assert.ok(!JSON.stringify(bundle.telemetry).includes('someone-else'))
})
await check('with no telemetry file the bundle still writes, with empty telemetry', async () => {
  fs.rmSync(path.join(telemetryDir, 'compaction.jsonl'))
  // The reader caches rows by file, so a new session id keeps the check honest.
  const sid2 = 'session-export-test-2'
  fs.copyFileSync(path.join(sessionsDir, `${sid}.json`), path.join(sessionsDir, `${sid2}.json`))
  const s = JSON.parse(fs.readFileSync(path.join(sessionsDir, `${sid2}.json`), 'utf8'))
  s.id = sid2
  fs.writeFileSync(path.join(sessionsDir, `${sid2}.json`), JSON.stringify(s))
  const r2 = await persistence.exportSessionToFile(sid2, path.join(home, 'out2.json'))
  assert.equal(r2.ok, true, JSON.stringify(r2))
  assert.deepEqual(JSON.parse(fs.readFileSync(r2.bundlePath, 'utf8')).telemetry, {})
})
await check('an unknown session fails cleanly and writes a v3 stub', async () => {
  const r3 = await persistence.exportSessionToFile('nope', path.join(home, 'out3.json'))
  assert.equal(r3.ok, false)
})

fs.rmSync(home, { recursive: true, force: true })
if (failures) {
  console.log(`\n${failures} failed`)
  process.exit(1)
}
console.log('\nall passed')
process.exit(0)
