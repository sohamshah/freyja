// Session exports record what happened, and cut nothing: a tool call is as the
// model sent it, a result is whole (read back from the file the engine saved
// when it cut the result for the model), and large sessions are streamed to
// disk. Covers the shared export builder, the raw-transcript reader, the
// streaming writer, /export (json, bundle, trace) and the download. Run from
// the repo root:
//   npx tsx test-session-export.mjs

import assert from 'node:assert/strict'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'

// The main-process modules resolve ~/.freyja from HOME when they load.
const home = fs.mkdtempSync(path.join(os.tmpdir(), 'freyja-export-test-'))
process.env.HOME = home

globalThis.window = globalThis.window ?? {}
const harnessCalls = []
window.harness = {
  sendCommand: async () => {},
  sessionSave: async (payload) => { harnessCalls.push(['save', payload.id]); return { ok: true } },
  sessionIndexSave: async () => ({ ok: true }),
  sessionLoad: async () => ({ ok: false }),
}
const { useHarness } = await import('./src/renderer/state/store.ts')
const { exportToolCall, exportToolCalls, exportToolStats, buildSessionExport } = await import('./src/shared/sessionExport.ts')
const record = await import('./src/main/sessionRecord.ts')
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

// The event sequence the bridge emits for one call_tool call: the wrapper's
// args, then the same id again for the inner tool, with its name and args.
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

console.log('\none tool call, as the export records it (no transcript)')
await check('a call_tool call reads as the model made it, with the tool that ran', () => {
  const out = exportToolCall(rec('w1'))
  assert.equal(out.name, 'call_tool')
  assert.deepEqual(out.arguments, WRAPPER('bash', { command: 'ls', summary: 'list' }))
  assert.equal(out.argumentsSource, 'app')
  assert.equal(out.invokes, 'bash')
  assert.deepEqual(out.executedArguments, { command: 'ls', summary: 'list' })
})
await check('a direct call: no invokes, and no executedArguments when they match', () => {
  const out = exportToolCall(rec('d1'))
  assert.equal(out.name, 'bash')
  assert.deepEqual(out.arguments, { command: 'pwd' })
  assert.equal(out.argumentsSource, 'app')
  assert.ok(!('invokes' in out) && !('executedArguments' in out))
})
await check('a call whose wrapper event was missed is rebuilt, and says so', () => {
  const out = exportToolCall({
    id: 'r1', name: 'call_tool', invokes: 'read_file', arguments: { path: '/a' },
    status: 'success', startedAt: 1,
  })
  assert.deepEqual(out.arguments, WRAPPER('read_file', { path: '/a' }))
  assert.equal(out.argumentsSource, 'rebuilt')
  assert.deepEqual(out.executedArguments, { path: '/a' })
})
await check('a call_tool record from before the fix is left as stored', () => {
  const out = exportToolCall({ id: 'o1', name: 'call_tool', arguments: { path: '/a' }, status: 'success', startedAt: 1 })
  assert.deepEqual(out.arguments, { path: '/a' })
  assert.ok(!('invokes' in out))
})
await check('the preview is flagged: whole when short, cut when the bridge marked it', () => {
  const short = exportToolCall({ id: 'p1', name: 'bash', status: 'success', startedAt: 1, result: 'fine' })
  assert.deepEqual([short.resultSource, short.resultComplete, short.resultChars], ['preview', true, 4])
  const cut = exportToolCall({ id: 'p2', name: 'bash', status: 'success', startedAt: 1,
    result: 'head\n\n… [truncated 3234 chars] …\n\ntail' })
  assert.deepEqual([cut.resultSource, cut.resultComplete], ['preview', false])
})
await check('a running call has no result fields set', () => {
  const out = exportToolCall({ id: 'x', name: 'bash', status: 'running', startedAt: 1 })
  assert.equal(out.result, undefined)
  assert.equal(out.resultSource, undefined)
})
await check('the parallel group, partial JSON and file changes are kept', () => {
  callTool('g1', 'read_file', { path: '/b' }, { groupId: 'grp-1' })
  assert.equal(exportToolCall(rec('g1')).groupId, 'grp-1')
  assert.equal(exportToolCall({ id: 'm1', name: 'bash', status: 'running', startedAt: 1, partialJson: '{"comm' }).partialJson, '{"comm')
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
  assert.equal(out.resultImages[0].byteSize, 77)
  assert.ok(!JSON.stringify(out).includes(b64))
})
await check('stats count by the tool that ran, and a call that never ran under the tool it asked for', () => {
  const stats = exportToolStats(Object.values(useHarness.getState().toolCalls))
  assert.ok(!('call_tool' in stats), JSON.stringify(stats))
  assert.deepEqual(stats.bash, { count: 2, success: 2, failure: 0 })
  assert.deepEqual(
    exportToolStats([{ id: 'e1', name: 'call_tool', status: 'error', isError: true, startedAt: 1, arguments: WRAPPER('no_such_tool', {}) }]),
    { no_such_tool: { count: 1, success: 0, failure: 1 } },
  )
})

console.log('\none tool call, with the transcript')
const view = {
  calls: new Map([
    // The model sent this path; the app resolved it and added resolvedPath.
    ['d2', { name: 'read_file', arguments: { path: '/a/AGENTS.md', base: 'auto' } }],
    // A call_tool call from before the app kept the wrapper args.
    ['old1', { name: 'call_tool', arguments: WRAPPER('read_file', { path: '/old' }) }],
  ]),
  results: new Map([
    ['d2', { text: 'x'.repeat(5000), source: 'transcript', complete: true }],
    ['old1', { text: 'whole', source: 'original', complete: true, originalChars: 5 }],
  ]),
}
await check('arguments are the transcript\'s, and the app\'s are kept as executedArguments', () => {
  const out = exportToolCall({
    id: 'd2', name: 'read_file', status: 'success', startedAt: 1, result: 'preview…',
    arguments: { path: '/a/CLAUDE.md', base: 'auto', resolvedPath: '/a/CLAUDE.md' },
  }, view)
  assert.deepEqual(out.arguments, { path: '/a/AGENTS.md', base: 'auto' })
  assert.equal(out.argumentsSource, 'transcript')
  assert.deepEqual(out.executedArguments, { path: '/a/CLAUDE.md', base: 'auto', resolvedPath: '/a/CLAUDE.md' })
  assert.equal(out.result.length, 5000)
  assert.deepEqual([out.resultSource, out.resultComplete, out.resultChars], ['transcript', true, 5000])
})
await check('an older call_tool record is recovered: tool, wrapper args, and the inner args it ran with', () => {
  const out = exportToolCall({
    id: 'old1', name: 'call_tool', status: 'success', startedAt: 1, arguments: { path: '/old' },
  }, view)
  assert.equal(out.name, 'call_tool')
  assert.equal(out.invokes, 'read_file')
  assert.deepEqual(out.arguments, WRAPPER('read_file', { path: '/old' }))
  assert.deepEqual(out.executedArguments, { path: '/old' })
  assert.deepEqual([out.resultSource, out.resultOriginalChars], ['original', 5])
})
await check('a call the transcript does not have falls back to the app\'s record', () => {
  const out = exportToolCall(rec('d1'), view)
  assert.equal(out.argumentsSource, 'app')
  assert.equal(out.resultSource, 'preview')
})
await check('stats read the tool for an older call_tool record from the transcript', () => {
  const older = { id: 'old1', name: 'call_tool', status: 'success', startedAt: 1, arguments: { path: '/old' } }
  assert.deepEqual(exportToolStats([older]), { call_tool: { count: 1, success: 1, failure: 0 } })
  assert.deepEqual(exportToolStats([older], view), { read_file: { count: 1, success: 1, failure: 0 } })
})
await check('exportToolCalls yields one at a time', () => {
  const it = exportToolCalls(Object.values(useHarness.getState().toolCalls), view)
  assert.equal(typeof it.next, 'function')
  assert.equal(it.next().value.id, 'w1')
})

// ─── The raw transcript ─────────────────────────────────────────────
const sid = 'session-export-test'
const sessionsDir = path.join(home, '.freyja', 'sessions')
const projectDir = path.join(home, '.freyja', 'projects', sid)
const truncatedDir = path.join(home, '.freyja', 'truncated')
for (const d of [sessionsDir, projectDir, truncatedDir]) fs.mkdirSync(d, { recursive: true })

// What the engine writes for a result it cut: the head of the output, then a
// notice that names the file holding all of it (engine/tools.py).
const notice = (file, chars) =>
  `\n\n\n[Content truncated -- showing ~1,000 of 5,000 tokens (${chars.toLocaleString('en-US')} chars). ` +
  `Full output saved to: ${file}]\n[Tip: use grep/head/tail to extract sections from the full file, e.g.: grep -i 'keyword' ${file}]`
const WHOLE = 'line one\r\nline two with é and 🙂\n' + 'filler '.repeat(40_000) + '\nTHE END'
const savedFile = path.join(truncatedDir, 'bash_20261018_000000_000001.txt')
fs.writeFileSync(savedFile, WHOLE)
const secretFile = path.join(home, 'secret.txt')
fs.writeFileSync(secretFile, 'SECRET-CONTENT')
const otherFile = path.join(truncatedDir, 'bash_20261018_000000_000002.txt')
fs.writeFileSync(otherFile, 'something else entirely')

const asst = (calls) => ({ role: 'assistant', content: '', tool_calls: calls, input_tokens: 10, output_tokens: 2 })
const res = (id, content) => ({ role: 'tool_result', tool_call_id: id, content })
const rawRows = [
  asst([
    { id: 'c-plain', name: 'bash', arguments: { command: 'ls' } },
    { id: 'c-cut', name: 'call_tool', arguments: WRAPPER('bash', { command: 'big' }) },
    { id: 'c-gone', name: 'bash', arguments: { command: 'gone' } },
    { id: 'c-evil', name: 'bash', arguments: { command: 'evil' } },
    { id: 'c-quote', name: 'bash', arguments: { command: 'quote' } },
    { id: 'c-other', name: 'bash', arguments: { command: 'other' } },
    { id: 'c-blocks', name: 'bash', arguments: { command: 'blocks' } },
    { id: 'c-nofile', name: 'bash', arguments: { command: 'nofile' } },
  ]),
  res('c-plain', 'just output'),
  res('c-cut', WHOLE.slice(0, 1000) + notice(savedFile, WHOLE.length)),
  res('c-gone', 'head of it' + notice(path.join(truncatedDir, 'missing.txt'), 99_000)),
  res('c-evil', 'SEC' + notice(secretFile, 5)),
  // Output that quotes a notice in its middle is not a cut result.
  res('c-quote', notice(savedFile, 1).trim() + '\n' + 'and then a lot more text. '.repeat(200)),
  res('c-other', WHOLE.slice(0, 50) + notice(otherFile, 1234)),
  res('c-blocks', [
    { type: 'text', text: 'first block' },
    { type: 'image', source_type: 'base64', media_type: 'image/png', data: 'AAAA' },
    { type: 'text', text: WHOLE.slice(0, 1000) + notice(savedFile, WHOLE.length) },
  ]),
  res('c-nofile', 'cut text\n\n[Content truncated. Use filters or parameters to narrow results.]'),
].map((message, i) => ({ ts: i + 1, session_id: sid, turn_id: 'turn-1', message }))
fs.writeFileSync(path.join(projectDir, 'raw_messages.jsonl'),
  rawRows.map((r) => JSON.stringify(r)).join('\n') + '\n{"ts": 99, "message": {"role": "tool_re')   // an interrupted last write

console.log('\nthe raw transcript')
const raw = record.readRawRecord(sid)
await check('calls are indexed as the model sent them', () => {
  assert.deepEqual(raw.calls.get('c-cut'), { name: 'call_tool', arguments: WRAPPER('bash', { command: 'big' }) })
  assert.equal(raw.calls.get('nope'), undefined)
})
await check('a result that was not cut is the transcript text', () => {
  assert.deepEqual(raw.results.get('c-plain'), { text: 'just output', source: 'transcript', complete: true })
})
await check('a cut result is read back whole from the file the engine saved, byte for byte', () => {
  const r = raw.results.get('c-cut')
  assert.equal(r.source, 'original')
  assert.equal(r.complete, true)
  assert.equal(r.text, WHOLE)
  assert.ok(r.text.includes('\r\n') && r.text.endsWith('THE END'))
  assert.equal(r.originalChars, WHOLE.length)
})
await check('a cut result whose file is gone stays cut, and says how big it was', () => {
  const r = raw.results.get('c-gone')
  assert.deepEqual([r.source, r.complete, r.originalChars], ['transcript-truncated', false, 99000])
  assert.ok(r.text.startsWith('head of it'))
})
await check('a notice that names a file outside the engine\'s directory is not followed', () => {
  const r = raw.results.get('c-evil')
  assert.ok(!r.text.includes('SECRET-CONTENT'))
  assert.equal(r.source, 'transcript-truncated')
})
await check('a file that does not start with the cut text is not this result\'s, and is not used', () => {
  const r = raw.results.get('c-other')
  assert.ok(!r.text.includes('something else entirely'))
  assert.equal(r.source, 'transcript-truncated')
})
await check('a notice quoted in the middle of a result does not make it a cut result', () => {
  const r = raw.results.get('c-quote')
  assert.deepEqual([r.source, r.complete], ['transcript', true])
})
await check('a result with text blocks is restored block by block, and images are left to the bundle', () => {
  const r = raw.results.get('c-blocks')
  assert.equal(r.source, 'original')
  assert.equal(r.text, 'first block\n' + WHOLE)
})
await check('the engine\'s no-file notice marks a result cut', () => {
  const r = raw.results.get('c-nofile')
  assert.deepEqual([r.source, r.complete], ['transcript-truncated', false])
})
await check('restoredResults yields only the results read back from a file, whole', () => {
  const rows = [...raw.restoredResults()]
  assert.deepEqual(rows.map((r) => r.tool_call_id).sort(), ['c-blocks', 'c-cut'])
  assert.equal(rows.find((r) => r.tool_call_id === 'c-cut').chars, WHOLE.length)
})
await check('an interrupted last line is skipped, and the rows pass through as written', () => {
  const rows = [...raw.rows()]
  assert.equal(rows.length, rawRows.length)
  assert.equal(rows[2].message.tool_call_id, 'c-cut')
})
await check('a session with no raw transcript reads as empty', () => {
  const none = record.readRawRecord('no-such-session')
  assert.equal(none.calls.get('x'), undefined)
  assert.equal(none.results.get('x'), undefined)
  assert.deepEqual([...none.rows()], [])
})
await check('a JSONL file larger than one read chunk is read whole, across line and character splits', () => {
  const file = path.join(home, 'big.jsonl')
  // 10 rows of ~1.2 MB each with multi-byte characters: chunk edges fall inside
  // rows and inside characters.
  const rows = Array.from({ length: 10 }, (_, i) => ({ i, text: 'é🙂x'.repeat(200_000) }))
  fs.writeFileSync(file, rows.map((r) => JSON.stringify(r)).join('\n') + '\n')
  assert.ok(fs.statSync(file).size > 8 * 1024 * 1024)
  const read = record.readJsonl(file)
  assert.equal(read.length, 10)
  assert.ok(read.every((r, i) => r.i === i && r.text === rows[i].text))
})

console.log('\nthe streaming writer')
await check('lazy arrays and objects are written as JSON that reads back the same', () => {
  const dest = path.join(home, 'lazy.json')
  record.writeJsonFile(dest, {
    plain: { a: 1, nested: { b: [1, 2, { c: 'é' }] }, gone: undefined },
    list: (function* () { yield 1; yield { two: 2 }; yield undefined })(),
    lazy: new record.LazyObject(function* () { yield ['k1', 'v1']; yield ['skipped', undefined]; yield ['k2', [3]] }),
    empty: [],
    emptyLazy: (function* () {})(),
    deep: { a: { b: { c: { d: [1, 2] } } } },
    text: 'quote " and \\ and \n newline',
  })
  assert.deepEqual(JSON.parse(fs.readFileSync(dest, 'utf8')), {
    plain: { a: 1, nested: { b: [1, 2, { c: 'é' }] } },
    list: [1, { two: 2 }, null],
    lazy: { k1: 'v1', k2: [3] },
    empty: [],
    emptyLazy: [],
    deep: { a: { b: { c: { d: [1, 2] } } } },
    text: 'quote " and \\ and \n newline',
  })
})
await check('a file bigger than a JavaScript string can be is written', () => {
  const dest = path.join(home, 'huge.json')
  // 600 MB of JSON in total: one string of that length cannot exist.
  const row = 'x'.repeat(10 * 1024 * 1024)
  record.writeJsonFile(dest, { rows: (function* () { for (let i = 0; i < 60; i++) yield { i, row } })() })
  const size = fs.statSync(dest).size
  assert.ok(size > 600 * 1024 * 1024, `size ${size}`)
  // Read the head and tail back: the file is whole and ends as JSON.
  const fd = fs.openSync(dest, 'r')
  const tail = Buffer.alloc(40)
  fs.readSync(fd, tail, 0, 40, size - 40)
  fs.closeSync(fd)
  assert.match(tail.toString('utf8'), /\}\s*\]\s*\}\s*$/)
  fs.rmSync(dest)
})
await check('a string longer than one piece reads back the same, even with an emoji on the piece edge', () => {
  const piece = 4 * 1024 * 1024
  // The emoji's two halves sit on either side of the first piece edge.
  const edge = 'x'.repeat(piece - 1) + '🙂' + 'y'.repeat(50) + '"\\\n\t' + 'z'.repeat(piece) + '🙂'
  const lone = 'a'.repeat(piece - 1) + '\ud83d' + 'tail'
  const dest = path.join(home, 'long.json')
  record.writeJsonFile(dest, { edge, lone, small: 'ok' })
  const back = JSON.parse(fs.readFileSync(dest, 'utf8'))
  assert.equal(back.edge, edge)
  assert.equal(back.lone, lone)
  assert.equal(back.small, 'ok')
  const textDest = path.join(home, 'long.txt')
  record.writeTextFile(textDest, [edge, '\n'])
  assert.equal(fs.readFileSync(textDest, 'utf8'), edge + '\n')
})
await check('a failure part way leaves no file, and no partial file', () => {
  const dest = path.join(home, 'fails.json')
  assert.throws(() => record.writeJsonFile(dest, { rows: (function* () { yield 1; throw new Error('boom') })() }), /boom/)
  assert.ok(!fs.existsSync(dest) && !fs.existsSync(`${dest}.partial`))
})
await check('text is written a piece at a time', () => {
  const dest = path.join(home, 'text.txt')
  record.writeTextFile(dest, (function* () { yield 'a\n'; yield 'é\n' })())
  assert.equal(fs.readFileSync(dest, 'utf8'), 'a\né\n')
})

// ─── A saved session, for /export and the download ──────────────────
const record2 = (o) => ({ status: 'success', startedAt: 1, durationMs: 7, ...o })
const slice = {
  systemPrompt: 'You are Freyja.',
  reasoningLevel: 'high',
  messages: [
    { id: 'u1', role: 'user', createdAt: 1, parts: [{ type: 'text', text: 'do the thing' }] },
    { id: 'a1', role: 'assistant', createdAt: 2, inputTokens: 5, outputTokens: 6, parts: [
      { type: 'thinking', text: 'T'.repeat(3000) },
      { type: 'text', text: 'A'.repeat(5000) },
      { type: 'tool_call', toolCallId: 'c-plain' },
      { type: 'tool_call', toolCallId: 'c-cut' },
      { type: 'tool_call', toolCallId: 'c-gone' },
      { type: 'tool_call', toolCallId: 'c-old' },
      { type: 'tool_call', toolCallId: 'c-noraw' },
    ] },
  ],
  toolCalls: {
    // The app resolved the model's path and added resolvedPath.
    'c-plain': record2({ id: 'c-plain', name: 'bash', arguments: { command: 'ls', resolvedPath: '/x' }, result: 'just output' }),
    // A new-style wrapper record, with a cut preview.
    'c-cut': record2({ id: 'c-cut', name: 'call_tool', invokes: 'bash', arguments: { command: 'big' },
      wrapperArguments: WRAPPER('bash', { command: 'big' }),
      result: WHOLE.slice(0, 1000) + '\n\n… [truncated 99 chars] …\n\n' + WHOLE.slice(-1000),
      frame: { frameId: 'f9', mimeType: 'image/png', width: 4, height: 3, takenAt: 5, reason: 'after' } }),
    'c-gone': record2({ id: 'c-gone', name: 'bash', arguments: { command: 'gone' }, result: 'head of it' }),
    // From before the app kept the wrapper args: the name is only in the transcript.
    'c-old': record2({ id: 'c-old', name: 'call_tool', arguments: { command: 'older' }, result: 'older output' }),
    // Not in the transcript at all.
    'c-noraw': record2({ id: 'c-noraw', name: 'read_file', arguments: { path: '/n' }, result: 'noraw output' }),
  },
  toolCallOrder: ['c-plain', 'c-cut', 'c-gone', 'c-old', 'c-noraw'],
  subagents: {},
  usage: { totalInputTokens: 1 },
  systemEvents: [],
}
// One more raw call and result, for the call_tool record that lost its wrapper.
fs.appendFileSync(path.join(projectDir, 'raw_messages.jsonl'), '\n' + [
  { ts: 50, session_id: sid, turn_id: 'turn-2', message: asst([{ id: 'c-old', name: 'call_tool', arguments: WRAPPER('bash', { command: 'older' }) }]) },
  { ts: 51, session_id: sid, turn_id: 'turn-2', message: res('c-old', 'older output') },
].map((r) => JSON.stringify(r)).join('\n') + '\n')
fs.writeFileSync(path.join(sessionsDir, `${sid}.json`), JSON.stringify({
  version: 1, id: sid, title: 'Export test', model: 'm', workspace: '~/', createdAt: 1, updatedAt: 2,
  messageCount: 2, totalInputTokens: 11, totalOutputTokens: 22, cacheReadTokens: 3, slice,
}))
const telemetryDir = path.join(home, '.freyja', 'telemetry')
fs.mkdirSync(telemetryDir, { recursive: true })
fs.writeFileSync(path.join(telemetryDir, 'compaction.jsonl'), [
  { type: 'llm_call_metric', session_id: sid, model: 'm1', input_tokens: 10, output_tokens: 2, cache_read_tokens: 5, cache_write_tokens: 1, cost_usd: 0.1, duration_ms: 900, ts: 1 },
  { type: 'llm_call_metric', session_id: 'someone-else', model: 'm2', ts: 2 },
  { type: 'tool_call_metric', session_id: sid, tool_call_id: 'c-cut', tool_name: 'call_tool', result_bytes: WHOLE.length, ok: true, ts: 3 },
  { type: 'llm_call_metric', session_id: sid, model: 'm1', input_tokens: 20, output_tokens: 4, ts: 4 },
].map((r) => JSON.stringify(r)).join('\n') + '\n')
fs.writeFileSync(path.join(projectDir, 'compactions.jsonl'),
  JSON.stringify({ ts: 9, session_id: sid, subtype: 'compaction_complete', summary_text: 'S'.repeat(4000) }) + '\n')

console.log('\n/export')
const dest = path.join(home, 'out.json')
const exported = await persistence.exportSessionToFile(sid, dest)
const readJson = (p) => JSON.parse(fs.readFileSync(p, 'utf8'))
await check('it writes three files', () => {
  assert.equal(exported.ok, true, JSON.stringify(exported))
  for (const p of [exported.jsonPath, exported.bundlePath, exported.tracePath]) assert.ok(fs.existsSync(p), p)
})
await check('the .json keeps the stored record and makes each result whole', () => {
  const tcs = readJson(exported.jsonPath).slice.toolCalls
  assert.equal(tcs['c-cut'].result, WHOLE)
  assert.deepEqual([tcs['c-cut'].resultSource, tcs['c-cut'].resultComplete, tcs['c-cut'].resultChars], ['original', true, WHOLE.length])
  assert.deepEqual(tcs['c-cut'].arguments, { command: 'big' })
  assert.deepEqual(tcs['c-cut'].wrapperArguments, WRAPPER('bash', { command: 'big' }))
  assert.equal(tcs['c-cut'].frame.frameId, 'f9')
  assert.equal(tcs['c-plain'].result, 'just output')
  assert.deepEqual(tcs['c-plain'].arguments, { command: 'ls', resolvedPath: '/x' })
  assert.equal(tcs['c-noraw'].resultSource, 'preview')
})
await check('the .json recovers the tool for a call_tool record from before the fix', () => {
  const old = readJson(exported.jsonPath).slice.toolCalls['c-old']
  assert.equal(old.invokes, 'bash')
  assert.deepEqual(old.wrapperArguments, WRAPPER('bash', { command: 'older' }))
  assert.deepEqual(old.arguments, { command: 'older' })
})
await check('the .json keeps the rest of the session as stored', () => {
  const saved = readJson(exported.jsonPath)
  assert.equal(saved.title, 'Export test')
  assert.equal(saved.slice.messages[1].parts[0].text, 'T'.repeat(3000))
  assert.deepEqual(saved.slice.toolCallOrder, slice.toolCallOrder)
})
await check('the bundle has the raw transcript, whole results, telemetry and compactions', () => {
  const bundle = readJson(exported.bundlePath)
  assert.equal(bundle.version, 4)
  assert.equal(bundle.raw_transcript.length, rawRows.length + 2)
  assert.equal(bundle.raw_transcript[2].message.tool_call_id, 'c-cut')
  assert.ok(bundle.raw_transcript[2].message.content.includes('Full output saved to'), 'the raw rows stay as the model saw them')
  assert.deepEqual(bundle.full_tool_results.map((r) => r.tool_call_id).sort(), ['c-blocks', 'c-cut'])
  assert.equal(bundle.full_tool_results.find((r) => r.tool_call_id === 'c-cut').text, WHOLE)
  assert.deepEqual(Object.keys(bundle.telemetry).sort(), ['llm_call_metric', 'tool_call_metric'])
  assert.deepEqual(bundle.telemetry.llm_call_metric.map((r) => r.input_tokens), [10, 20])
  assert.equal(bundle.telemetry.tool_call_metric[0].result_bytes, WHOLE.length)
  assert.ok(!JSON.stringify(bundle.telemetry).includes('someone-else'))
  assert.equal(bundle.compactions[0].summary_text, 'S'.repeat(4000))
  assert.equal(bundle.live_transcript.length, 2)
})
await check('the trace cuts nothing', () => {
  const trace = fs.readFileSync(exported.tracePath, 'utf8')
  assert.ok(trace.includes('THINKING: ' + 'T'.repeat(3000)), 'thinking')
  assert.ok(trace.includes('TEXT: ' + 'A'.repeat(5000)), 'text')
  assert.ok(trace.includes('THE END'), 'the end of the whole result')
  assert.ok(trace.includes('filler '.repeat(40_000).split('\n')[0].slice(0, 5000)))
  assert.ok(!/\.\.\.\(\+\d+ chars\)/.test(trace), 'no cut marker')
  assert.ok(!trace.includes('… [truncated'), 'no preview cut')
})
await check('the trace names the tool behind call_tool, and shows what the model sent and what ran', () => {
  const trace = fs.readFileSync(exported.tracePath, 'utf8')
  assert.match(trace, /TOOL call_tool -> bash \(7ms\) args=\{"tool_name":"bash","arguments":\{"command":"big"\}\}/)
  // A call_tool record from before the fix is recovered from the transcript.
  assert.match(trace, /TOOL call_tool -> bash \(7ms\) args=\{"tool_name":"bash","arguments":\{"command":"older"\}\}/)
  assert.match(trace, /ran with: \{"command":"older"\}/)
  // The model sent `ls`; the app added resolvedPath.
  assert.match(trace, /TOOL bash \(7ms\) args=\{"command":"ls"\}\n    ran with: \{"command":"ls","resolvedPath":"\/x"\}/)
})
await check('the trace says where each result came from', () => {
  const trace = fs.readFileSync(exported.tracePath, 'utf8')
  assert.match(trace, /result \(\d+ chars, whole: the engine cut it for the model, and this is read back from the file it saved\)/)
  assert.match(trace, /result \(\d+ chars, CUT by the engine for the model; the file it saved is gone \(the full output was 99000 chars\)\)/)
  assert.match(trace, /\(args from the app's record: no raw transcript entry for this call\)/)
})
await check('result lines are indented, so a result cannot pass for a trace line', () => {
  const trace = fs.readFileSync(exported.tracePath, 'utf8')
  const body = trace.split('\n').filter((l) => l.startsWith('      line one'))
  assert.equal(body.length, 1)
})

console.log('\nthe download')
const dl = path.join(home, 'download.json')
const downloaded = await persistence.writeSessionDownload(sid, dl)
await check('it writes one file', () => {
  assert.equal(downloaded.ok, true, JSON.stringify(downloaded))
})
const dump = downloaded.ok ? readJson(dl) : {}
await check('it is the export builder\'s output, version 5, with messages and thinking whole', () => {
  assert.equal(dump.version, 5)
  assert.equal(dump.systemPrompt, 'You are Freyja.')
  assert.equal(dump.session.id, sid)
  assert.equal(dump.session.model, 'm')
  assert.equal(dump.session.reasoningLevel, 'high')
  assert.equal(dump.taskDescription, 'do the thing')
  assert.equal(dump.thinkingTraces[0].thinking, 'T'.repeat(3000))
  assert.equal(dump.messages[1].parts[1].text, 'A'.repeat(5000))
})
await check('a tool call is as the model sent it, and its executed arguments are beside it', () => {
  const byId = Object.fromEntries(dump.toolCalls.map((c) => [c.id, c]))
  assert.deepEqual(byId['c-plain'].arguments, { command: 'ls' })
  assert.deepEqual(byId['c-plain'].executedArguments, { command: 'ls', resolvedPath: '/x' })
  assert.equal(byId['c-plain'].argumentsSource, 'transcript')
  assert.equal(byId['c-cut'].name, 'call_tool')
  assert.deepEqual(byId['c-cut'].arguments, WRAPPER('bash', { command: 'big' }))
  assert.equal(byId['c-cut'].invokes, 'bash')
  assert.deepEqual(byId['c-old'].arguments, WRAPPER('bash', { command: 'older' }))
  assert.equal(byId['c-old'].invokes, 'bash')
  assert.equal(byId['c-noraw'].argumentsSource, 'app')
})
await check('every result is whole, and says how whole', () => {
  const byId = Object.fromEntries(dump.toolCalls.map((c) => [c.id, c]))
  assert.equal(byId['c-cut'].result, WHOLE)
  assert.deepEqual([byId['c-cut'].resultSource, byId['c-cut'].resultComplete], ['original', true])
  assert.deepEqual([byId['c-gone'].resultSource, byId['c-gone'].resultComplete, byId['c-gone'].resultOriginalChars], ['transcript-truncated', false, 99000])
  assert.deepEqual([byId['c-noraw'].resultSource, byId['c-noraw'].resultComplete], ['preview', true])
})
await check('it counts stats by the tool that ran, keeps groupId, and leaves image bytes out', () => {
  assert.equal(dump.toolStats.bash.count, 4)
  assert.ok(!('call_tool' in dump.toolStats))
  assert.equal(dump.toolCalls.find((c) => c.id === 'c-cut').frame.frameId, 'f9')
})
await check('the raw transcript, telemetry and compactions come with it', () => {
  assert.equal(dump.rawTranscript.length, rawRows.length + 2)
  assert.equal(dump.rawTranscript[1].message.content, 'just output')
  assert.deepEqual(dump.telemetry.llm_call_metric.map((r) => r.input_tokens), [10, 20])
  assert.equal(dump.compactions[0].summary_text, 'S'.repeat(4000))
})
await check('a session that does not exist fails cleanly and writes nothing', async () => {
  const out = path.join(home, 'nope.json')
  const r = await persistence.writeSessionDownload('nope', out)
  assert.equal(r.ok, false)
  assert.ok(!fs.existsSync(out))
})
await check('a session with no saved slice still downloads', async () => {
  fs.writeFileSync(path.join(sessionsDir, 'bare.json'), JSON.stringify({
    version: 1, id: 'bare', title: 'Bare', model: 'm', workspace: '~/', createdAt: 1, updatedAt: 2,
    messageCount: 0, totalInputTokens: 0, totalOutputTokens: 0, cacheReadTokens: 0,
  }))
  const out = path.join(home, 'bare-out.json')
  const r = await persistence.writeSessionDownload('bare', out)
  assert.equal(r.ok, true, JSON.stringify(r))
  const d = readJson(out)
  assert.deepEqual([d.messages, d.toolCalls, d.rawTranscript], [[], [], []])
})

console.log('\nthe download button')
await check('it saves the live session first, then asks main to write the file', async () => {
  const calls = []
  window.harness.sessionDownload = async (id) => { calls.push(['download', id]); return { ok: true, path: '/x' } }
  harnessCalls.length = 0
  useHarness.setState({
    sessions: [{ id: S, title: 't', model: 'm', workspace: '~/', reasoningLevel: 'off', createdAt: 1, updatedAt: 1, messageCount: 1 }],
  })
  const toasts = []
  const showToast = useHarness.getState().showToast
  useHarness.setState({ showToast: (m, k) => toasts.push([m, k]) })
  await useHarness.getState().downloadSession(S)
  useHarness.setState({ showToast })
  assert.deepEqual(calls, [['download', S]])
  assert.deepEqual(toasts, [['Session downloaded', 'ok']])
})
await check('it reports a failure, and says nothing when the dialog is cancelled', async () => {
  const toasts = []
  const showToast = useHarness.getState().showToast
  useHarness.setState({ showToast: (m, k) => toasts.push([m, k]) })
  window.harness.sessionDownload = async () => ({ ok: false, cancelled: true })
  await useHarness.getState().downloadSession(S)
  window.harness.sessionDownload = async () => ({ ok: false, error: 'disk full' })
  await useHarness.getState().downloadSession(S)
  useHarness.setState({ showToast })
  assert.deepEqual(toasts, [['Download failed: disk full', 'warn']])
})
await check('it says so when this build cannot download', async () => {
  const toasts = []
  const showToast = useHarness.getState().showToast
  useHarness.setState({ showToast: (m, k) => toasts.push([m, k]) })
  delete window.harness.sessionDownload
  await useHarness.getState().downloadSession(S)
  useHarness.setState({ showToast })
  assert.equal(toasts[0][1], 'warn')
})

console.log('\nbuildSessionExport on its own')
await check('it builds without a transcript, and its tool calls are a generator', () => {
  const built = buildSessionExport({ id: 'x', title: 't' }, {
    messages: [], toolCalls: { a: rec('d1') }, toolCallOrder: ['a'], subagents: {}, usage: {}, systemEvents: [],
  })
  assert.equal(built.version, 5)
  assert.equal(typeof built.toolCalls.next, 'function')
  assert.equal([...built.toolCalls].length, 1)
  assert.equal(built.goalLoop, null)
})

fs.rmSync(home, { recursive: true, force: true })
if (failures) {
  console.log(`\n${failures} failed`)
  process.exit(1)
}
console.log('\nall passed')
process.exit(0)
