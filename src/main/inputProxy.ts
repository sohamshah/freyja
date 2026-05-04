import http from 'node:http'
import { AddressInfo } from 'node:net'
import { execFileSync } from 'node:child_process'
import path from 'node:path'
import fs from 'node:fs'

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
 *  Implementation: we shell out to a tiny Python one-liner that imports
 *  freyja_native and calls the requested input function. The Python
 *  process inherits TCC from this (Electron main) process because it's
 *  a direct child. We use the same Python binary the bridge uses.
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
 *  bridge via FREYJA_INPUT_URL env var.
 */

export interface InputProxy {
  url: string
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

/**
 * Execute an input action by calling freyja_native through Python.
 * Returns null on success, or an error string on failure.
 */
function execInput(
  pythonBin: string,
  env: NodeJS.ProcessEnv,
  action: string,
  params: Record<string, unknown>,
): string | null {
  // Build a Python one-liner that calls the right freyja_native function
  let pyCode: string
  switch (action) {
    case 'click':
      pyCode = `import freyja_native as n; n.click(${params.x},${params.y},${JSON.stringify(params.button || 'left')},${!!params.double},${JSON.stringify(params.modifiers || [])})`
      break
    case 'move_mouse':
      pyCode = `import freyja_native as n; n.move_mouse(${params.x},${params.y})`
      break
    case 'type_text':
      pyCode = `import freyja_native as n; n.type_text(${JSON.stringify(String(params.text))})`
      break
    case 'press_key':
      pyCode = `import freyja_native as n; n.press_key(${JSON.stringify(String(params.key))},${JSON.stringify(params.modifiers || [])})`
      break
    case 'key_down':
      pyCode = `import freyja_native as n; n.key_down(${JSON.stringify(String(params.key))})`
      break
    case 'key_up':
      pyCode = `import freyja_native as n; n.key_up(${JSON.stringify(String(params.key))})`
      break
    case 'scroll':
      pyCode = `import freyja_native as n; n.scroll(${params.dx},${params.dy},${params.x ?? 'None'},${params.y ?? 'None'})`
      break
    default:
      return `unknown action: ${action}`
  }
  try {
    execFileSync(pythonBin, ['-c', pyCode], {
      env,
      timeout: 5000,
      stdio: 'pipe',
    })
    return null
  } catch (err: unknown) {
    const msg = err instanceof Error ? err.message : String(err)
    return msg.slice(0, 500)
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
  const env = buildPythonEnv(pythonBin, harnessRoot)

  const server = http.createServer(async (req, res) => {
    if (req.method !== 'POST' || !req.url?.startsWith('/input')) {
      res.writeHead(404, { 'Content-Type': 'application/json' })
      res.end(JSON.stringify({ ok: false, error: 'not found' }))
      return
    }

    // Read JSON body
    const chunks: Buffer[] = []
    for await (const chunk of req) chunks.push(chunk as Buffer)
    let body: Record<string, unknown>
    try {
      body = JSON.parse(Buffer.concat(chunks).toString('utf8'))
    } catch {
      res.writeHead(400, { 'Content-Type': 'application/json' })
      res.end(JSON.stringify({ ok: false, error: 'invalid JSON' }))
      return
    }

    const action = String(body.action || '')
    if (!action) {
      res.writeHead(400, { 'Content-Type': 'application/json' })
      res.end(JSON.stringify({ ok: false, error: 'missing action' }))
      return
    }

    const error = execInput(pythonBin, env, action, body)
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
    close() {
      try {
        server.close()
      } catch {}
    },
  }
}
