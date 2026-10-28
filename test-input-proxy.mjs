// The input proxy: what it accepts, what it refuses, that request data
// reaches Python as JSON, never as source, and that it wants the per-launch
// token. Run from the repo root:
//   npx tsx test-input-proxy.mjs
//
// No input is ever sent. execFileSync is swapped for a recorder, and the
// "Python" it would run is the node binary, which cannot run the script
// anyway. The script itself runs once, with `python3 -S` against a stub
// freyja_native that only records the call.
import cp from 'node:child_process'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'

const realExecFileSync = cp.execFileSync
const spawned = []
cp.execFileSync = (bin, args, opts) => {
  spawned.push({ bin, args, input: opts?.input })
  return Buffer.from('')
}
process.env.FREYJA_PYTHON = process.execPath

const { parseInputRequest, startInputProxy } = await import('./src/main/inputProxy.ts')
const { hasProxyToken } = await import('./src/main/proxyToken.ts')

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
  check('the call goes on stdin', JSON.parse(spawned[0].input), { action: 'type_text', args: [text], kwargs: {} })
}
proxy.close()
fs.rmSync(harnessRoot, { recursive: true, force: true })

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
  const script = spawned[0]?.args[1]
  const stubRoot = fs.mkdtempSync(path.join(os.tmpdir(), 'freyja-input-stub-'))
  fs.mkdirSync(path.join(stubRoot, 'freyja_native'))
  fs.writeFileSync(
    path.join(stubRoot, 'freyja_native', '__init__.py'),
    [
      'import json',
      'def __getattr__(name):',
      '    def record(*args, **kwargs):',
      '        print(json.dumps([name, list(args), kwargs]))',
      '    return record',
    ].join('\n'),
  )
  const run = (call) =>
    JSON.parse(
      realExecFileSync('python3', ['-S', '-c', script], {
        // -S keeps site-packages, and so the real module, off the path.
        cwd: stubRoot,
        env: { PATH: process.env.PATH, PYTHONPATH: stubRoot },
        input: JSON.stringify(call),
      }).toString(),
    )
  check('click reaches the keyword arguments', run(parseInputRequest({ action: 'click', x: 3, y: 4, double: true }).call), [
    'click', [3, 4], { button: 'left', double: true, modifiers: [] },
  ])
  check('scroll passes None for no point', run(parseInputRequest({ action: 'scroll', dx: 1, dy: -2 }).call), [
    'scroll', [1, -2], { x: null, y: null },
  ])
  const text = `");import os;os.system('x')#`
  check('text arrives as one string', run(parseInputRequest({ action: 'type_text', text }).call), ['type_text', [text], {}])
  fs.rmSync(stubRoot, { recursive: true, force: true })
}

console.log(failures === 0 ? '\nAll input-proxy checks passed.\n' : `\n${failures} check(s) failed.\n`)
process.exit(failures === 0 ? 0 : 1)
