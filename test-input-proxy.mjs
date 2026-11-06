// The input proxy: what it accepts, what it refuses, that request data
// reaches Python as JSON, never as source, and that it wants the per-launch
// token. Run from the repo root:
//   npx tsx test-input-proxy.mjs
//
// No input is ever sent. spawn is swapped for a fake worker that records
// each line it is sent and answers it, and the "Python" it would run is
// the node binary, which cannot run the script anyway. The script itself
// runs at the end, with `python3 -S` against a stub freyja_native that
// only records the calls.
import cp from 'node:child_process'
import { EventEmitter } from 'node:events'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { PassThrough, Writable } from 'node:stream'

const realSpawn = cp.spawn
const spawned = []
// How the fake worker treats each line: 'ok', 'fail', 'hang', 'crash',
// or a number of milliseconds to wait before it says ok.
let workerMode = 'ok'
let inFlight = 0
let maxInFlight = 0
let realPython = null
cp.spawn = (bin, args, opts) => {
  if (realPython) return realSpawn('python3', ['-S', ...args], { ...opts, cwd: realPython })
  const proc = new EventEmitter()
  const record = { bin, args, lines: [], killed: null }
  spawned.push(record)
  const say = (answer) => {
    inFlight--
    proc.stdout.write(JSON.stringify(answer) + '\n')
  }
  proc.stdout = new PassThrough()
  proc.stderr = new PassThrough()
  proc.stdin = new Writable({
    write(chunk, _enc, done) {
      record.lines.push(JSON.parse(chunk.toString()))
      maxInFlight = Math.max(maxInFlight, ++inFlight)
      if (workerMode === 'ok') say({ ok: true })
      else if (workerMode === 'fail') say({ ok: false, error: 'RuntimeError: x is off screen' })
      else if (workerMode === 'crash') {
        inFlight--
        proc.stderr.write('Traceback (most recent call last):\nImportError: no _native\n')
        setImmediate(() => proc.emit('close', 1, null))
      } else if (typeof workerMode === 'number') setTimeout(() => say({ ok: true }), workerMode)
      done()
    },
  })
  proc.kill = (signal) => {
    record.killed = signal
    inFlight = 0
    setImmediate(() => proc.emit('close', null, signal))
  }
  return proc
}
process.env.FREYJA_PYTHON = process.execPath

const { InputWorker, parseInputRequest, startInputProxy } = await import('./src/main/inputProxy.ts')
const { hasProxyToken } = await import('./src/main/proxyToken.ts')

// A request the proxy never answers would hang the run; fail it instead.
setTimeout(() => {
  console.log('\n  FAIL a request was never answered\n')
  process.exit(1)
}, 20_000).unref()

let failures = 0
function check(name, actual, expected) {
  const a = JSON.stringify(actual)
  const e = JSON.stringify(expected)
  if (a === e) {
    console.log(`  ok   ${name}`)
  } else {
    failures++
    console.log(`  FAIL ${name}\n         expected ${e}\n         actual   ${a}`)
  }
}

const INJECT = "0);import os;os.system('touch /tmp/pwned');(0"

console.log('\nrefused bodies')
{
  const cases = [
    ['code in x', { action: 'move_mouse', x: INJECT, y: 0 }],
    ['code in y', { action: 'click', x: 0, y: INJECT }],
    ['code in dx', { action: 'scroll', dx: INJECT, dy: 0 }],
    ['code in scroll x', { action: 'scroll', dx: 0, dy: 1, x: INJECT, y: 0 }],
    ['code in button', { action: 'click', x: 0, y: 0, button: INJECT }],
    ['code in modifiers', { action: 'press_key', key: 'a', modifiers: [INJECT] }],
    ['code in key', { action: 'key_down', key: INJECT }],
    ['infinite x', { action: 'move_mouse', x: 1e400, y: 0 }],
    ['x beyond i32', { action: 'move_mouse', x: 2 ** 31, y: 0 }],
    ['missing x', { action: 'move_mouse', y: 0 }],
    ['object x', { action: 'move_mouse', x: { valueOf: 1 }, y: 0 }],
    ['non-boolean double', { action: 'click', x: 0, y: 0, double: 'yes' }],
    ['modifiers not a list', { action: 'press_key', key: 'a', modifiers: 'cmd' }],
    ['too many modifiers', { action: 'press_key', key: 'a', modifiers: ['cmd', 'cmd', 'cmd', 'cmd', 'cmd', 'cmd'] }],
    ['non-string text', { action: 'type_text', text: 5 }],
    ['text too long', { action: 'type_text', text: 'a'.repeat(100_001) }],
    ['unknown action', { action: '__import__' }],
    ['missing action', { x: 0, y: 0 }],
    ['not an object', null],
  ]
  for (const [name, body] of cases) check(name, 'error' in parseInputRequest(body), true)
}

console.log('\naccepted bodies (what freyja_native._proxy_input sends)')
{
  const call = (body) => parseInputRequest(body).call
  check('click', call({ action: 'click', x: 10, y: 20, button: 'left', double: false, modifiers: ['cmd'] }), {
    action: 'click', args: [10, 20], kwargs: { button: 'left', double: false, modifiers: ['cmd'] },
  })
  check('click defaults', call({ action: 'click', x: 1.9, y: '2' }), {
    action: 'click', args: [1, 2], kwargs: { button: 'left', double: false, modifiers: [] },
  })
  check('move_mouse', call({ action: 'move_mouse', x: -5, y: 7 }), { action: 'move_mouse', args: [-5, 7], kwargs: {} })
  check('type_text keeps quotes and newlines', call({ action: 'type_text', text: `a"b'c\n)` }), {
    action: 'type_text', args: [`a"b'c\n)`], kwargs: {},
  })
  check('press_key named key', call({ action: 'press_key', key: 'Return', modifiers: [] }), {
    action: 'press_key', args: ['Return'], kwargs: { modifiers: [] },
  })
  check('press_key one character', call({ action: 'press_key', key: 'é', modifiers: ['Shift'] }), {
    action: 'press_key', args: ['é'], kwargs: { modifiers: ['Shift'] },
  })
  check('key_up', call({ action: 'key_up', key: 'shift' }), { action: 'key_up', args: ['shift'], kwargs: {} })
  check('scroll without a point', call({ action: 'scroll', dx: 0, dy: 3, x: null, y: null }), {
    action: 'scroll', args: [0, 3], kwargs: { x: null, y: null },
  })
}

console.log('\nover HTTP')
const harnessRoot = fs.mkdtempSync(path.join(os.tmpdir(), 'freyja-input-test-'))
const proxy = await startInputProxy(harnessRoot)
async function post(body, token = proxy.token) {
  const headers = token === null ? {} : { 'X-Freyja-Token': token }
  const res = await fetch(`${proxy.url}/input`, { method: 'POST', body, headers })
  return { status: res.status, body: await res.json() }
}
{
  spawned.length = 0
  const move = JSON.stringify({ action: 'move_mouse', x: 1, y: 2 })
  check('no token is a 401', (await post(move, null)).status, 401)
  check('a wrong token is a 401', (await post(move, 'x'.repeat(proxy.token.length))).status, 401)
  check('a short token is a 401', (await post(move, proxy.token.slice(1))).status, 401)
  check('and nothing is spawned for them', spawned.length, 0)
  check('the token is per launch', proxy.token.length, 64)
  const bad = await post(JSON.stringify({ action: 'move_mouse', x: INJECT, y: 0 }))
  check('code in a field is a 400', bad.status, 400)
  check('and nothing is spawned', spawned.length, 0)
  check('bad JSON is a 400', (await post('{')).status, 400)
  check('JSON null is a 400', (await post('null')).status, 400)

  const text = `");import os;os.system('x')#`
  const ok = await post(JSON.stringify({ action: 'type_text', text }))
  check('valid input is a 200', ok.status, 200)
  check('one process', spawned.length, 1)
  check('the script holds no request data', spawned[0].args[1].includes('os.system'), false)
  check('the call goes on stdin', spawned[0].lines[0], { action: 'type_text', args: [text], kwargs: {} })
  await post(move)
  check('the next action reuses the worker', spawned.length, 1)

  workerMode = 'fail'
  check('a failed action is a 500 with its reason', await post(move), {
    status: 500, body: { ok: false, error: 'RuntimeError: x is off screen' },
  })

  // Sent together, answered slowly: one at a time, in the order sent.
  workerMode = 20
  maxInFlight = 0
  spawned[0].lines.length = 0
  const bodies = [0, 1, 2, 3, 4, 5].map((i) =>
    i % 2 ? { action: 'click', x: i, y: i } : { action: 'move_mouse', x: i, y: i },
  )
  const statuses = await Promise.all(bodies.map(async (b) => (await post(JSON.stringify(b))).status))
  check('all six are a 200', statuses, [200, 200, 200, 200, 200, 200])
  check('one action at a time', maxInFlight, 1)
  check('in the order sent', spawned[0].lines.map((l) => l.args[0]), [0, 1, 2, 3, 4, 5])

  // Main is free while an action runs.
  workerMode = 200
  let ticks = 0
  const timer = setInterval(() => ticks++, 10)
  await post(move)
  clearInterval(timer)
  check('main keeps running during an action', ticks >= 10, true)
  workerMode = 'ok'
}
proxy.close()
check('closing the proxy kills the worker', spawned[0].killed, 'SIGKILL')
fs.rmSync(harnessRoot, { recursive: true, force: true })

console.log('\nthe worker after a crash or a hang')
{
  const call = parseInputRequest({ action: 'move_mouse', x: 1, y: 2 }).call
  spawned.length = 0
  const worker = new InputWorker(process.execPath, {}, 50)
  workerMode = 'hang'
  check('a hung action times out', await worker.run(call), 'timed out after 50 ms')
  check('and its worker is killed', spawned[0].killed, 'SIGKILL')
  workerMode = 'ok'
  check('the next action starts a new worker', [await worker.run(call), spawned.length], [null, 2])
  workerMode = 'crash'
  const crashed = await worker.run(call)
  check('a crash says why', crashed, 'input worker exited (1): Traceback (most recent call last):\nImportError: no _native')
  workerMode = 'ok'
  check('and the next action starts a new worker', [await worker.run(call), spawned.length], [null, 3])
  worker.close()
}

console.log('\nthe token check (shared with the capture proxy)')
{
  const req = (headers) => ({ headers })
  check('right token', hasProxyToken(req({ 'x-freyja-token': 'abc' }), 'abc'), true)
  check('wrong token', hasProxyToken(req({ 'x-freyja-token': 'abd' }), 'abc'), false)
  check('longer token', hasProxyToken(req({ 'x-freyja-token': 'abcd' }), 'abc'), false)
  check('repeated header', hasProxyToken(req({ 'x-freyja-token': ['abc', 'abc'] }), 'abc'), false)
  check('no header', hasProxyToken(req({}), 'abc'), false)
}

console.log('\nthe script against a stub freyja_native')
{
  const stubRoot = fs.mkdtempSync(path.join(os.tmpdir(), 'freyja-input-stub-'))
  const log = path.join(stubRoot, 'calls.jsonl')
  fs.mkdirSync(path.join(stubRoot, 'freyja_native'))
  fs.writeFileSync(
    path.join(stubRoot, 'freyja_native', '__init__.py'),
    [
      'import json',
      'def __getattr__(name):',
      '    def record(*args, **kwargs):',
      "        print('stray output')",
      "        if args == ('refuse',):",
      "            raise RuntimeError('stub refused')",
      `        with open(${JSON.stringify(log)}, 'a') as f:`,
      "            f.write(json.dumps([name, list(args), kwargs]) + '\\n')",
      '    return record',
    ].join('\n'),
  )
  // -S keeps site-packages, and so the real module, off the path.
  realPython = stubRoot
  const worker = new InputWorker('python3', { PATH: process.env.PATH, PYTHONPATH: stubRoot })
  const run = (body) => worker.run(parseInputRequest(body).call)
  const text = `");import os;os.system('x')#`
  const answers = [
    await run({ action: 'click', x: 3, y: 4, double: true }),
    await run({ action: 'scroll', dx: 1, dy: -2 }),
    await run({ action: 'type_text', text }),
    await run({ action: 'type_text', text: 'refuse' }),
  ]
  worker.close()
  realPython = null
  const calls = fs.readFileSync(log, 'utf8').trim().split('\n').map((l) => JSON.parse(l))
  check('stray output does not break the answers', answers, [null, null, null, 'RuntimeError: stub refused'])
  check('click reaches the keyword arguments', calls[0], ['click', [3, 4], { button: 'left', double: true, modifiers: [] }])
  check('scroll passes None for no point', calls[1], ['scroll', [1, -2], { x: null, y: null }])
  check('text arrives as one string', calls[2], ['type_text', [text], {}])
  fs.rmSync(stubRoot, { recursive: true, force: true })
}

console.log(failures === 0 ? '\nAll input-proxy checks passed.\n' : `\n${failures} check(s) failed.\n`)
process.exit(failures === 0 ? 0 : 1)
