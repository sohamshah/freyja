import http from 'node:http'
import { AddressInfo } from 'node:net'
import { spawn, type ChildProcess } from 'node:child_process'
import path from 'node:path'
import fs from 'node:fs'
import { hasProxyToken, newProxyToken } from './proxyToken.js'

/**
 * Localhost HTTP proxy for input injection (mouse, keyboard, scroll).
 *
 * ────────────────────────────────────────────────────────────────────────
 *
 *  Why this exists: macOS TCC Accessibility permission may not inherit
 *  to the Python subprocess when the app is unsigned or when the Python
 *  binary's code signature conflicts with the parent app's identity.
 *  Without Accessibility, CGEventPost calls in the Python bridge are
 *  silently dropped by the OS — clicks and keystrokes never land.
 *
 *  This is the input analogue of captureProxy.ts (which handles Screen
 *  Recording). The pattern is identical: the Electron main process
 *  (which DOES have TCC grants) performs the privileged operation on
 *  behalf of the Python subprocess.
 *
 *  Implementation: one long-lived Python worker runs a fixed script that
 *  imports freyja_native and calls the requested input function. The
 *  Python process inherits TCC from this (Electron main) process because
 *  it's a direct child. We use the same Python binary the bridge uses.
 *  Actions go to it one at a time, in the order they arrived, and main
 *  never waits on it: a process per action, run with execFileSync, froze
 *  every window for the interpreter start plus the action.
 *
 *  Every field is checked here, and the call reaches the script as JSON
 *  on stdin, never as Python source. When fields were pasted into the
 *  source, any local process that found the port could run its own
 *  code with Freyja's Accessibility grant.
 *
 *  Endpoints:
 *    POST /input  { action, ...params }
 *
 *  Actions:
 *    click       { x, y, button?, double?, modifiers? }
 *    move_mouse  { x, y }
 *    type_text   { text }
 *    press_key   { key, modifiers? }
 *    key_down    { key }
 *    key_up      { key }
 *    scroll      { dx, dy, x?, y? }
 *
 *  Response: 200 { ok: true } or 4xx/5xx { ok: false, error: string }
 *
 *  Bound to 127.0.0.1 on an ephemeral port. URL passed to the Python
 *  bridge via FREYJA_INPUT_URL env var, and a per-launch token via
 *  FREYJA_INPUT_TOKEN; a request without the token (X-Freyja-Token
 *  header, see proxyToken.ts) gets a 401.
 */

export interface InputProxy {
  url: string
  token: string
  close(): void
}

/**
 * Find the Python binary to use for input injection.
 * Mirrors the candidate list in bridge.ts.
 */
function findPython(harnessRoot: string): string | null {
  const sourceRoot = path.resolve(__dirname, '..')
  const candidates = [
    process.env.FREYJA_PYTHON || '',
    path.join(harnessRoot, 'python-bundle', 'bin', 'python3'),
    path.join(sourceRoot, 'python-bundle', 'bin', 'python3'),
    path.join(harnessRoot, '.venv', 'bin', 'python'),
    path.join(sourceRoot, '.venv', 'bin', 'python'),
  ].filter(Boolean)

  for (const c of candidates) {
    try {
      if (fs.existsSync(c)) return c
    } catch {}
  }
  return null
}

/**
 * Build the PYTHONPATH so `import freyja_native` works.
 */
function buildPythonEnv(
  pythonBin: string,
  harnessRoot: string,
): NodeJS.ProcessEnv {
  const sourceRoot = path.resolve(__dirname, '..')
  const env: NodeJS.ProcessEnv = {
    ...process.env,
    PYTHONUNBUFFERED: '1',
    PYTHONPATH: [harnessRoot, sourceRoot, process.env.PYTHONPATH || '']
      .filter(Boolean)
      .join(':'),
  }
  if (pythonBin.includes('python-bundle')) {
    env.PYTHONHOME = path.resolve(pythonBin, '..', '..')
  }
  return env
}

/** A request turned into the freyja_native call it asks for. */
export interface InputCall {
  action: string
  args: unknown[]
  kwargs: Record<string, unknown>
}

/** Mouse buttons, modifiers and named keys that freyja_native accepts:
 *  parse_button, parse_modifier and parse_key in
 *  native/freyja_native/src/input.rs. Matched without case, as there. */
const BUTTONS = new Set(['left', 'l', 'primary', 'right', 'r', 'secondary', 'middle', 'm'])
const MODIFIERS = new Set([
  'cmd', 'command', 'meta', 'super', 'win', 'ctrl', 'control', 'alt', 'option', 'opt', 'shift', 'fn',
])
const NAMED_KEYS = new Set([
  'return', 'enter', 'ret', 'tab', 'space', 'escape', 'esc', 'backspace', 'back', 'bs',
  'delete', 'del', 'home', 'end', 'pageup', 'page_up', 'pagedown', 'page_down',
  'up', 'arrow_up', 'up_arrow', 'down', 'arrow_down', 'down_arrow',
  'left', 'arrow_left', 'left_arrow', 'right', 'arrow_right', 'right_arrow',
  'caps', 'capslock', 'caps_lock', 'cmd', 'command', 'meta', 'super',
  'ctrl', 'control', 'alt', 'option', 'opt', 'shift',
  'f1', 'f2', 'f3', 'f4', 'f5', 'f6', 'f7', 'f8', 'f9', 'f10', 'f11', 'f12',
])
/** cmd, ctrl, alt, shift and fn. */
const MAX_MODIFIERS = 5
/** Far more than any type_text call; it only bounds one request. */
const MAX_TEXT_LENGTH = 100_000
/** freyja_native takes coordinates and scroll amounts as i32. */
const I32_MAX = 2 ** 31 - 1

class InvalidInput extends Error {}

function intField(body: Record<string, unknown>, key: string): number {
  const v = body[key]
  const n = typeof v === 'number' || (typeof v === 'string' && v.trim() !== '') ? Number(v) : NaN
  if (!Number.isFinite(n) || Math.abs(n) > I32_MAX) {
    throw new InvalidInput(`${key} must be a number`)
  }
  return Math.trunc(n)
}

function optionalIntField(body: Record<string, unknown>, key: string): number | null {
  return body[key] == null ? null : intField(body, key)
}

function keyField(body: Record<string, unknown>): string {
  const key = body.key
  if (typeof key !== 'string') throw new InvalidInput('key must be a string')
  const k = key.trim()
  // One character is typed as itself; anything longer must be a named key.
  if ([...k].length !== 1 && !NAMED_KEYS.has(k.toLowerCase())) {
    throw new InvalidInput(`unknown key: ${key.slice(0, 40)}`)
  }
  return key
}

function modifiersField(body: Record<string, unknown>): string[] {
  const mods = body.modifiers ?? []
  if (!Array.isArray(mods) || mods.length > MAX_MODIFIERS) {
    throw new InvalidInput(`modifiers must be a list of at most ${MAX_MODIFIERS}`)
  }
  for (const m of mods) {
    if (typeof m !== 'string' || !MODIFIERS.has(m.toLowerCase())) {
      throw new InvalidInput(`unknown modifier: ${String(m).slice(0, 40)}`)
    }
  }
  return mods as string[]
}

/** Check a request body and build the call it asks for. Returns an error
 *  string for anything freyja_native would not accept. */
export function parseInputRequest(body: unknown): { call: InputCall } | { error: string } {
  if (typeof body !== 'object' || body === null || Array.isArray(body)) {
    return { error: 'body must be a JSON object' }
  }
  const b = body as Record<string, unknown>
  if (!b.action) return { error: 'missing action' }
  const action = String(b.action)
  try {
    switch (action) {
      case 'click': {
        const button = b.button || 'left'
        if (typeof button !== 'string' || !BUTTONS.has(button.toLowerCase())) {
          throw new InvalidInput(`unknown button: ${String(button).slice(0, 40)}`)
        }
        const double = b.double ?? false
        if (typeof double !== 'boolean') throw new InvalidInput('double must be a boolean')
        return {
          call: {
            action,
            args: [intField(b, 'x'), intField(b, 'y')],
            kwargs: { button, double, modifiers: modifiersField(b) },
          },
        }
      }
      case 'move_mouse':
        return { call: { action, args: [intField(b, 'x'), intField(b, 'y')], kwargs: {} } }
      case 'type_text': {
        const text = b.text
        if (typeof text !== 'string') throw new InvalidInput('text must be a string')
        if (text.length > MAX_TEXT_LENGTH) {
          throw new InvalidInput(`text is longer than ${MAX_TEXT_LENGTH} characters`)
        }
        return { call: { action, args: [text], kwargs: {} } }
      }
      case 'press_key':
        return { call: { action, args: [keyField(b)], kwargs: { modifiers: modifiersField(b) } } }
      case 'key_down':
      case 'key_up':
        return { call: { action, args: [keyField(b)], kwargs: {} } }
      case 'scroll':
        return {
          call: {
            action,
            args: [intField(b, 'dx'), intField(b, 'dy')],
            kwargs: { x: optionalIntField(b, 'x'), y: optionalIntField(b, 'y') },
          },
        }
      default:
        return { error: `unknown action: ${action.slice(0, 40)}` }
    }
  } catch (err) {
    if (err instanceof InvalidInput) return { error: err.message }
    throw err
  }
}

/** The script the worker runs. Each call arrives as one line of JSON on
 *  stdin, so nothing from a request is ever read as Python source, and
 *  each gets one line of JSON back. Keyword arguments match the
 *  freyja_native wrapper, whose button, double, modifiers, x and y are
 *  keyword-only. Anything else written to stdout (freyja_native, Rust)
 *  goes to stderr, so it cannot be read as an answer. */
const INPUT_WORKER_SCRIPT = [
  'import json, os, sys',
  "answers = os.fdopen(os.dup(1), 'w')",
  'os.dup2(2, 1)',
  'import freyja_native as n',
  'for line in sys.stdin:',
  '    call = json.loads(line)',
  '    try:',
  "        getattr(n, call['action'])(*call['args'], **call['kwargs'])",
  "        answer = {'ok': True}",
  '    except Exception as exc:',
  "        answer = {'ok': False, 'error': f'{type(exc).__name__}: {exc}'}",
  "    answers.write(json.dumps(answer) + '\\n')",
  '    answers.flush()',
].join('\n')

/** How long one action may take, the worker's start included on the
 *  first, before the worker is killed. */
const ACTION_TIMEOUT_MS = 5000

/**
 * The Python process that runs input actions. Calls run one at a time,
 * in the order run() was called, so a click never overtakes the move
 * before it. The worker starts on the first call. When it crashes or an
 * action times out it is killed, and the next call starts a new one.
 */
export class InputWorker {
  private proc: ChildProcess | null = null
  private queue: Promise<unknown> = Promise.resolve()
  private current: {
    proc: ChildProcess
    timer: NodeJS.Timeout
    resolve: (error: string | null) => void
  } | null = null

  constructor(
    private pythonBin: string,
    private env: NodeJS.ProcessEnv,
    private timeoutMs = ACTION_TIMEOUT_MS,
  ) {}

  /** Run a call after every call before it. Resolves to null on success,
   *  or an error string on failure. */
  run(call: InputCall): Promise<string | null> {
    const result = this.queue.then(() => this.runNow(call))
    this.queue = result
    return result
  }

  close(): void {
    if (this.proc) this.kill(this.proc)
  }

  private runNow(call: InputCall): Promise<string | null> {
    return new Promise((resolve) => {
      try {
        const proc = this.proc ?? this.spawn()
        const timer = setTimeout(() => {
          this.finish(proc, `timed out after ${this.timeoutMs} ms`)
          this.kill(proc)
        }, this.timeoutMs)
        this.current = { proc, timer, resolve }
        proc.stdin!.write(JSON.stringify(call) + '\n')
      } catch (err) {
        resolve(String(err instanceof Error ? err.message : err).slice(0, 500))
      }
    })
  }

  private spawn(): ChildProcess {
    const proc = spawn(this.pythonBin, ['-c', INPUT_WORKER_SCRIPT], { env: this.env, stdio: 'pipe' })
    this.proc = proc
    let out = ''
    let stderr = ''
    proc.stdout!.setEncoding('utf8')
    proc.stdout!.on('data', (chunk: string) => {
      out += chunk
      for (let nl = out.indexOf('\n'); nl >= 0; nl = out.indexOf('\n')) {
        const line = out.slice(0, nl)
        out = out.slice(nl + 1)
        let answer: { ok?: boolean; error?: unknown }
        try {
          answer = JSON.parse(line)
        } catch {
          answer = { error: `bad answer from the input worker: ${line.slice(0, 200)}` }
        }
        this.finish(proc, answer.ok ? null : String(answer.error || 'input action failed').slice(0, 500))
      }
    })
    proc.stderr!.setEncoding('utf8')
    proc.stderr!.on('data', (chunk: string) => {
      stderr = (stderr + chunk).slice(-500)
    })
    // A write after it died fails here; 'close' says why.
    proc.stdin!.on('error', () => {})
    const gone = (why: string) => {
      if (this.proc === proc) this.proc = null
      this.finish(proc, stderr.trim() ? `${why}: ${stderr.trim()}` : why)
    }
    proc.on('error', (err) => gone(`input worker did not start (${err.message})`))
    proc.on('close', (code, signal) => gone(`input worker exited (${signal ?? code})`))
    return proc
  }

  /** Answer the call in flight on `proc`, if there is one. */
  private finish(proc: ChildProcess, error: string | null): void {
    const current = this.current
    if (!current || current.proc !== proc) return
    clearTimeout(current.timer)
    this.current = null
    current.resolve(error)
  }

  private kill(proc: ChildProcess): void {
    if (this.proc === proc) this.proc = null
    try {
      proc.kill('SIGKILL')
    } catch {}
  }
}

export async function startInputProxy(
  harnessRoot: string,
): Promise<InputProxy | null> {
  const pythonBin = findPython(harnessRoot)
  if (!pythonBin) {
    console.warn('[inputProxy] no Python binary found — skipping')
    return null
  }
  const worker = new InputWorker(pythonBin, buildPythonEnv(pythonBin, harnessRoot))
  const token = newProxyToken()

  const server = http.createServer(async (req, res) => {
    if (!hasProxyToken(req, token)) {
      res.writeHead(401, { 'Content-Type': 'application/json' })
      res.end(JSON.stringify({ ok: false, error: 'missing or wrong token' }))
      return
    }
    if (req.method !== 'POST' || !req.url?.startsWith('/input')) {
      res.writeHead(404, { 'Content-Type': 'application/json' })
      res.end(JSON.stringify({ ok: false, error: 'not found' }))
      return
    }

    // Read JSON body
    const chunks: Buffer[] = []
    for await (const chunk of req) chunks.push(chunk as Buffer)
    let body: unknown
    try {
      body = JSON.parse(Buffer.concat(chunks).toString('utf8'))
    } catch {
      res.writeHead(400, { 'Content-Type': 'application/json' })
      res.end(JSON.stringify({ ok: false, error: 'invalid JSON' }))
      return
    }

    const parsed = parseInputRequest(body)
    if ('error' in parsed) {
      res.writeHead(400, { 'Content-Type': 'application/json' })
      res.end(JSON.stringify({ ok: false, error: parsed.error }))
      return
    }

    const error = await worker.run(parsed.call)
    if (error) {
      res.writeHead(500, { 'Content-Type': 'application/json' })
      res.end(JSON.stringify({ ok: false, error }))
    } else {
      res.writeHead(200, { 'Content-Type': 'application/json' })
      res.end(JSON.stringify({ ok: true }))
    }
  })

  await new Promise<void>((resolve, reject) => {
    server.once('error', reject)
    server.listen(0, '127.0.0.1', () => resolve())
  })
  const addr = server.address() as AddressInfo
  const url = `http://127.0.0.1:${addr.port}`
  console.log(`[inputProxy] listening at ${url}`)

  return {
    url,
    token,
    close() {
      try {
        server.close()
      } catch {}
      worker.close()
    },
  }
}
