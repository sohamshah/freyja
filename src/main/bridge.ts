import { spawn, type ChildProcessWithoutNullStreams } from 'node:child_process'
import path from 'node:path'
import fs from 'node:fs'
import os from 'node:os'
import {
  type BridgeCommand,
  type BridgeEvent,
  type BridgeMode,
} from '../shared/events.js'
import { DemoBridge } from './demo.js'

/**
 * Parse a .env file into a plain object. Supports KEY=value and KEY="value"
 * formats, ignores comments and blank lines. Values are trimmed.
 */
function readDotEnv(filePath: string): Record<string, string> {
  const out: Record<string, string> = {}
  try {
    const text = fs.readFileSync(filePath, 'utf8')
    for (const rawLine of text.split(/\r?\n/)) {
      const line = rawLine.trim()
      if (!line || line.startsWith('#')) continue
      const eq = line.indexOf('=')
      if (eq === -1) continue
      let key = line.slice(0, eq).trim()
      if (key.startsWith('export ')) key = key.slice('export '.length).trim()
      if (!key) continue
      let val = line.slice(eq + 1).trim()
      if (
        (val.startsWith('"') && val.endsWith('"')) ||
        (val.startsWith("'") && val.endsWith("'"))
      ) {
        val = val.slice(1, -1)
      }
      out[key] = val
    }
  } catch {
    // ignore missing / unreadable .env
  }
  return out
}

export interface HarnessBridgeOptions {
  harnessRoot: string
  workspace: string
  onEvent: (event: BridgeEvent) => void
  /** Localhost URL of the capture proxy running in the main process.
   *  Passed to the Python bridge via FREYJA_CAPTURE_URL env var
   *  so the bridge can route screenshots through the main process
   *  (which has Screen Recording TCC grant) instead of calling the
   *  native capture path directly from Python (which does not). */
  captureProxyUrl?: string
  /** Localhost URL of the input proxy running in the main process.
   *  Passed via FREYJA_INPUT_URL so the bridge can route input
   *  injection (click, type, etc.) through the main process when
   *  the Python subprocess lacks Accessibility TCC grant. */
  inputProxyUrl?: string
}

/**
 * HarnessBridge spawns the Python `freyja_bridge.py` sidecar if possible,
 * otherwise falls back to an in-memory DemoBridge that streams realistic
 * fake events so the UI is always populated.
 */
/** Persistent diagnostic log written by the Electron main process.
 *  Captures lifecycle events (bridge spawn / exit / restart, command
 *  send failures, main-side errors) that the Python bridge cannot
 *  log itself — because when the bridge subprocess dies, it can't
 *  emit its own death. Sibling file to the Python-side
 *  `bridge-events.jsonl`. */
const MAIN_LOG_PATH = path.join(os.homedir(), '.freyja', 'main-events.jsonl')
const MAIN_LOG_PREV_PATH = path.join(os.homedir(), '.freyja', 'main-events.prev.jsonl')
const MAIN_LOG_ROLLOVER_BYTES = 20 * 1024 * 1024 // 20 MB

function appendMainLog(entry: Record<string, unknown>): void {
  try {
    const dir = path.dirname(MAIN_LOG_PATH)
    fs.mkdirSync(dir, { recursive: true })
    // Size-based rotation. Cheap stat per write; only rotates when
    // the threshold is crossed. .prev preserved for post-mortem.
    try {
      const stat = fs.statSync(MAIN_LOG_PATH)
      if (stat.size > MAIN_LOG_ROLLOVER_BYTES) {
        fs.renameSync(MAIN_LOG_PATH, MAIN_LOG_PREV_PATH)
      }
    } catch {
      // file might not exist yet — fine
    }
    const line = JSON.stringify({ _t: Date.now() / 1000, ...entry }) + '\n'
    fs.appendFileSync(MAIN_LOG_PATH, line)
  } catch {
    // never let logging take down the main process
  }
}

export class HarnessBridge {
  private mode: BridgeMode = 'error'
  private proc: ChildProcessWithoutNullStreams | null = null
  private demo: DemoBridge | null = null
  private readonly onEvent: (event: BridgeEvent) => void
  private readonly opts: HarnessBridgeOptions
  private stdoutBuffer = ''
  private ready = false
  private readonly pythonCandidates: string[]
  /** Flag set while `restart()` is tearing down the current Python
   *  child process so we can suppress the normal exit-handler path
   *  that would fall back to demo mode. During a restart we WANT the
   *  proc to die; dropping to demo mid-restart would flicker the UI
   *  into fake-event land for a second. */
  private restarting = false

  constructor(opts: HarnessBridgeOptions) {
    this.opts = opts
    this.onEvent = opts.onEvent
    // Build a list of Python interpreters to try, in priority order.
    // The first one that spawns successfully and prints `ready` wins.
    const sourceRoot = path.resolve(__dirname, '..')
    this.pythonCandidates = [
      process.env.FREYJA_PYTHON || '',
      // 1. Bundled Python inside the .app (extraResources)
      path.join(opts.harnessRoot, 'python-bundle', 'bin', 'python3'),
      // 2. Bundled Python relative to source root (dev build)
      path.join(sourceRoot, 'python-bundle', 'bin', 'python3'),
      // 3. Project venv
      path.join(opts.harnessRoot, '.venv', 'bin', 'python'),
      path.join(sourceRoot, '.venv', 'bin', 'python'),
      // 4. System-level fallbacks
      'uv',
      'python3',
      'python',
    ].filter(Boolean)
  }

  getMode(): BridgeMode {
    return this.mode
  }

  async start(): Promise<void> {
    const sourceRoot = path.resolve(__dirname, '..')
    const bridgeScript = path.resolve(this.opts.harnessRoot, 'bridge', 'freyja_bridge.py')

    // Log to BOTH the event stream (for the UI) and stderr (for the
    // terminal when running `npm run dev`). The UI might not be ready
    // when bridge.start() fires, so early events get dropped.
    const log = (msg: string) => {
      console.error(`[bridge] ${msg}`)
      this.emit({ type: 'log', level: 'info', message: msg })
    }

    log(`harnessRoot=${this.opts.harnessRoot}`)
    log(`sourceRoot=${sourceRoot}`)
    log(`bridgeScript=${bridgeScript}`)
    log(`exists=${fs.existsSync(bridgeScript)}`)
    log(`candidates=${JSON.stringify(this.pythonCandidates)}`)

    if (!fs.existsSync(bridgeScript)) {
      log(`bridge script NOT FOUND — starting demo mode`)
      this.startDemo()
      return
    }

    // Load the .env so ANTHROPIC_API_KEY etc. reach the bridge.
    // In dev it's in the project root; packaged app checks both the
    // resources dir (harnessRoot) and the source project root.
    const harnessEnv = {
      ...readDotEnv(path.join(sourceRoot, '.env')),
      ...readDotEnv(path.join(this.opts.harnessRoot, '.env')),
    }

    // PYTHONPATH: the bridge does `from engine.X` and `from bridge.tools.X`.
    // Both packages live as sibling directories under the project root
    // (or under Resources/ when packaged). We add harnessRoot (which
    // IS the resources dir when packaged) so Python's import system
    // finds them, plus the source root as a fallback for dev.
    const pythonPath = [
      this.opts.harnessRoot,
      sourceRoot,
      process.env.PYTHONPATH || '',
    ].filter(Boolean).join(':')

    const childEnv: NodeJS.ProcessEnv = {
      ...process.env,
      ...harnessEnv,
      PYTHONUNBUFFERED: '1',
      PYTHONPATH: pythonPath,
      FREYJA_WORKSPACE: this.opts.workspace,
    }
    if (this.opts.captureProxyUrl) {
      childEnv.FREYJA_CAPTURE_URL = this.opts.captureProxyUrl
    }
    if (this.opts.inputProxyUrl) {
      childEnv.FREYJA_INPUT_URL = this.opts.inputProxyUrl
    }
    if (!childEnv.HOME) childEnv.HOME = os.homedir()

    this.emit({
      type: 'log',
      level: 'info',
      message: `python candidates: ${JSON.stringify(this.pythonCandidates)}`,
    })
    this.emit({
      type: 'log',
      level: 'info',
      message: `PYTHONPATH=${childEnv.PYTHONPATH}`,
    })
    const envKeys = Object.keys(harnessEnv)
    this.emit({
      type: 'log',
      level: 'info',
      message: `.env loaded ${envKeys.length} keys: ${envKeys.join(', ')}`,
    })

    // Try to spawn the Python bridge
    for (const cmd of this.pythonCandidates) {
      try {
        const args = cmd === 'uv'
          ? ['run', '--project', this.opts.harnessRoot, 'python', bridgeScript]
          : [bridgeScript]
        log(`trying: ${cmd} ${args.join(' ')}`)
        // If the candidate is the bundled Python, set PYTHONHOME so
        // it finds its stdlib + site-packages. The bundle dir is the
        // parent of bin/ — e.g. python-bundle/bin/python3 → python-bundle/
        const spawnEnv = { ...childEnv }
        if (cmd.includes('python-bundle')) {
          const bundleDir = path.resolve(cmd, '..', '..')
          spawnEnv.PYTHONHOME = bundleDir
        }
        const proc = spawn(cmd, args, {
          cwd: this.opts.workspace,
          env: spawnEnv,
          stdio: ['pipe', 'pipe', 'pipe'],
        })
        proc.on('error', (err) => {
          this.emit({ type: 'log', level: 'error', message: `spawn error (${cmd}): ${err.message}` })
        })
        // Capture stderr for diagnosis
        let stderrBuf = ''
        proc.stderr?.on('data', (chunk: Buffer) => {
          stderrBuf += chunk.toString('utf8')
        })
        // Wait a bit for the bridge to print ready or fail
        const ok = await this.waitForReady(proc, 5000)
        if (ok) {
          this.proc = proc
          this.mode = 'live'
          this.wireProc(proc)
          this.emit({ type: 'log', level: 'info', message: `bridge live via ${cmd}` })
          return
        }
        log(`FAILED: ${cmd} (no ready in 5s). stderr: ${stderrBuf.slice(0, 500)}`)
        try {
          proc.kill()
        } catch {}
      } catch (err) {
        this.emit({
          type: 'log',
          level: 'debug',
          message: `spawn failed for ${cmd}: ${String(err)}`,
        })
      }
    }

    // Fallback
    this.emit({
      type: 'log',
      level: 'warn',
      message: 'could not spawn python bridge, running in demo mode',
    })
    this.startDemo()
  }

  private async waitForReady(proc: ChildProcessWithoutNullStreams, timeoutMs: number): Promise<boolean> {
    return new Promise((resolve) => {
      let settled = false
      let buf = ''
      const onData = (chunk: Buffer) => {
        buf += chunk.toString('utf8')
        const lines = buf.split('\n')
        buf = lines.pop() ?? ''
        for (const line of lines) {
          if (!line.trim()) continue
          try {
            const obj = JSON.parse(line)
            if (obj && obj.type === 'ready') {
              if (!settled) {
                settled = true
                proc.stdout.off('data', onData)
                proc.stderr.off('data', onErr)
                // Replay the ready event so downstream listeners see it
                this.emit(obj)
                resolve(true)
              }
              return
            }
          } catch {
            // non-json stderr-ish output -- ignore
          }
        }
      }
      const onErr = (_chunk: Buffer) => {
        // keep listening; stderr is allowed to be chatty
      }
      proc.stdout.on('data', onData)
      proc.stderr.on('data', onErr)
      proc.once('exit', () => {
        if (!settled) {
          settled = true
          resolve(false)
        }
      })
      setTimeout(() => {
        if (!settled) {
          settled = true
          proc.stdout.off('data', onData)
          proc.stderr.off('data', onErr)
          resolve(false)
        }
      }, timeoutMs)
    })
  }

  private wireProc(proc: ChildProcessWithoutNullStreams) {
    proc.stdout.on('data', (chunk: Buffer) => {
      this.stdoutBuffer += chunk.toString('utf8')
      const lines = this.stdoutBuffer.split('\n')
      this.stdoutBuffer = lines.pop() ?? ''
      for (const line of lines) {
        const trimmed = line.trim()
        if (!trimmed) continue
        try {
          const obj = JSON.parse(trimmed) as BridgeEvent
          this.emit(obj)
        } catch (err) {
          this.emit({ type: 'log', level: 'debug', message: `non-json stdout: ${trimmed.slice(0, 200)}` })
        }
      }
    })
    proc.stderr.on('data', (chunk: Buffer) => {
      const msg = chunk.toString('utf8').trim()
      if (msg) this.emit({ type: 'log', level: 'debug', message: `bridge stderr: ${msg}` })
    })
    // CRITICAL: without a stdin 'error' listener, any EPIPE / broken-pipe
    // error bubbles up as an uncaught exception in the main process and
    // crashes the whole app (the "A JavaScript error occurred in the main
    // process" dialog). Swallow it into the log stream instead.
    proc.stdin.on('error', (err) => {
      this.emit({ type: 'log', level: 'error', message: `bridge stdin error: ${err.message}` })
    })
    proc.on('exit', (code, signal) => {
      // Intentional restart: the `restart()` method killed this
      // process on purpose and is about to spawn a replacement.
      // Don't start demo mode -- just clear the proc reference and
      // let restart() drive the next step.
      if (this.restarting) {
        this.emit({
          type: 'log',
          level: 'info',
          message: `bridge exited for restart (code=${code}, signal=${signal})`,
        })
        this.proc = null
        return
      }
      this.emit({
        type: 'log',
        level: 'warn',
        message: `bridge exited (code=${code}, signal=${signal}) -- switching to demo mode`,
      })
      this.proc = null
      this.mode = 'demo'
      this.startDemo()
    })
  }

  /**
   * Kill the current Python bridge subprocess and spawn a fresh one.
   *
   * This is the "reload Python-side code changes" path -- when someone
   * edits a file under `bridge/` or `engine/`, the only way the running
   * process sees those changes is to be replaced. Before this method
   * existed the only workaround was to quit and relaunch the entire
   * Electron app, which tore down the UI unnecessarily.
   *
   * The current in-memory sessions will appear to "lose" their
   * streaming state but any persisted session metadata is untouched
   * (it's on disk). The renderer should re-hydrate via its normal
   * ready-handling path once the new bridge emits `ready`.
   */
  async restart(): Promise<void> {
    this.emit({
      type: 'log',
      level: 'info',
      message: 'bridge restart requested...',
    })

    this.restarting = true
    try {
      // Tear down the current process if it's still alive.
      if (this.proc) {
        const dying = this.proc
        try {
          dying.kill('SIGTERM')
        } catch (err) {
          this.emit({
            type: 'log',
            level: 'warn',
            message: `bridge kill failed: ${String(err)}`,
          })
        }
        // Wait for the process to actually exit (or time out and
        // force-kill). Without this we can race the spawn of the
        // replacement against a still-alive child that's holding
        // the stdin pipe.
        await new Promise<void>((resolve) => {
          const t = setTimeout(() => {
            try {
              dying.kill('SIGKILL')
            } catch {}
            resolve()
          }, 2000)
          dying.once('exit', () => {
            clearTimeout(t)
            resolve()
          })
        })
        this.proc = null
      }
      // Also clean up any demo fallback that may have been started
      // on a previous unexpected exit.
      if (this.demo) {
        try {
          this.demo.stop?.()
        } catch {}
        this.demo = null
      }
      // Reset mode + ready state so `start()` is effectively fresh.
      this.mode = 'error'
      this.ready = false
      this.stdoutBuffer = ''
    } finally {
      this.restarting = false
    }

    // Spawn the replacement. `start()` handles the candidate-python
    // fallback chain and the ready-event wait internally.
    await this.start()
    this.emit({
      type: 'log',
      level: 'info',
      message: 'bridge restarted',
    })
  }

  private startDemo() {
    if (this.demo) return
    this.demo = new DemoBridge({ onEvent: this.emit.bind(this) })
    this.mode = 'demo'
    this.demo.start()
  }

  private emit(event: BridgeEvent) {
    // Mirror diagnostic-flavored events into the persistent main-side
    // log. We intentionally filter to lifecycle / log / error events
    // and skip the high-volume streaming traffic (text_delta etc.)
    // that already lives in the bridge's own log file.
    const type = (event as { type?: string }).type
    if (type === 'log' || type === 'error' || type === 'ready' || type === 'emergency_stop') {
      appendMainLog({ source: 'main', event })
    }
    this.onEvent(event)
  }

  async sendCommand(cmd: BridgeCommand): Promise<void> {
    if (this.proc) {
      const proc = this.proc
      if (!proc.stdin.writable) {
        this.emit({
          type: 'log',
          level: 'error',
          message: 'bridge stdin not writable -- dropping command',
        })
        return
      }
      const line = JSON.stringify(cmd) + '\n'
      // Use the callback form so per-write errors (e.g. EPIPE when the
      // Python bridge has closed its stdin mid-write) are surfaced to the
      // log stream instead of bubbling up as uncaught exceptions. Wrap in
      // try/catch for the synchronous throw case too (e.g. stream already
      // destroyed between the writable check and the write).
      await new Promise<void>((resolve) => {
        try {
          proc.stdin.write(line, (err) => {
            if (err) {
              this.emit({
                type: 'log',
                level: 'error',
                message: `bridge stdin write failed: ${err.message}`,
              })
            }
            resolve()
          })
        } catch (err) {
          this.emit({
            type: 'log',
            level: 'error',
            message: `bridge stdin write threw: ${(err as Error).message}`,
          })
          resolve()
        }
      })
      return
    }
    if (this.demo) {
      this.demo.handleCommand(cmd)
      return
    }
    throw new Error('bridge not started')
  }

  triggerDemoBurst() {
    this.demo?.burst()
  }

  async stop(): Promise<void> {
    if (this.proc) {
      try {
        this.proc.stdin.end()
      } catch {}
      try {
        this.proc.kill()
      } catch {}
      this.proc = null
    }
    if (this.demo) {
      this.demo.stop()
      this.demo = null
    }
  }
}
