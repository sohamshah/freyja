import fs from 'node:fs'
import path from 'node:path'
import os from 'node:os'

/**
 * Follows the gateway daemon's JSONL event log and forwards events for
 * gateway-routed sessions (Slack today, Telegram/Discord later) into the
 * desktop renderer's existing ``bridgeEvent`` channel.
 *
 * Architecture:
 *   · Daemon (launchd, separate Python process) writes one JSON event per
 *     line to ~/.freyja/logs/gateway.log.
 *   · Desktop (this process) tails that file, parses lines that look like
 *     JSON events with a gateway-shaped ``sessionId`` (``freyja:<platform>:...``),
 *     and forwards them to the renderer as if they had come from the
 *     local bridge subprocess.
 *   · The renderer's store already routes by sessionId, so Slack sessions
 *     light up in the sidebar and pane in real time.
 *
 * Why tail instead of using per-session ``.events.jsonl``:
 *   The daemon already writes one global log + one per-session file. The
 *   global file is the cheapest way to subscribe to ALL gateway sessions
 *   without opening N file watchers. We pay one file watcher + JSON
 *   parsing on every event, which on a busy session is still tiny.
 *
 * Offset persistence:
 *   ~/.freyja/control/desktop-tail.offset stores the last byte we've
 *   consumed. On first start (no offset file) we jump to EOF so we don't
 *   replay days of historical log lines. Across desktop restarts we
 *   resume cleanly so a brief restart doesn't miss in-flight events.
 *
 * Rotation:
 *   If the file shrinks below our offset (truncated externally, or a
 *   ``> gateway.log`` from the operator) we reset to 0 and resume. Inode
 *   change detection (e.g. logrotate moving the file) is a follow-up —
 *   launchd's stdout redirect today writes to a stable path forever.
 */

export type BridgeEventForwarder = (event: any) => void

const HOME = os.homedir()
const GATEWAY_LOG = path.join(HOME, '.freyja', 'logs', 'gateway.log')
const OFFSET_FILE = path.join(HOME, '.freyja', 'control', 'desktop-tail.offset')

const POLL_INTERVAL_MS = 250
// Cap each batch read so an idle desktop process can't suddenly try to
// parse 50MB at once after the daemon's been busy for an hour without us.
const MAX_BATCH_BYTES = 1 * 1024 * 1024

export class GatewayLogTailer {
  private offset = 0
  private timer: NodeJS.Timeout | null = null
  private stopped = false
  private partial = '' // Accumulator for the trailing partial line.

  constructor(private readonly forward: BridgeEventForwarder) {}

  start(): void {
    this.stopped = false
    this.ensureControlDir()
    this.loadOffset()
    // Schedule the first poll inline so we drain anything new on the
    // very next tick, then enter the polling cadence.
    queueMicrotask(() => this.tick())
  }

  stop(): void {
    this.stopped = true
    if (this.timer) {
      clearTimeout(this.timer)
      this.timer = null
    }
  }

  private ensureControlDir(): void {
    try {
      fs.mkdirSync(path.dirname(OFFSET_FILE), { recursive: true })
    } catch {}
  }

  private loadOffset(): void {
    try {
      const raw = fs.readFileSync(OFFSET_FILE, 'utf8').trim()
      const n = Number.parseInt(raw, 10)
      if (Number.isFinite(n) && n >= 0) {
        this.offset = n
        return
      }
    } catch {
      // No offset file. Jump to current EOF so we don't replay history.
    }
    try {
      this.offset = fs.statSync(GATEWAY_LOG).size
    } catch {
      this.offset = 0
    }
    this.persistOffset()
  }

  private persistOffset(): void {
    try {
      fs.writeFileSync(OFFSET_FILE, String(this.offset), 'utf8')
    } catch {
      // Non-fatal — at worst we replay a small window on the next start.
    }
  }

  private schedule(): void {
    if (this.stopped) return
    this.timer = setTimeout(() => this.tick(), POLL_INTERVAL_MS)
  }

  private tick(): void {
    if (this.stopped) return
    try {
      this.drain()
    } catch (err) {
      // Stay alive — a parse error or transient file system error must
      // not silently kill the tailer.
      console.error('[gatewayLogTail] drain failed:', err)
    }
    this.schedule()
  }

  private drain(): void {
    let stat: fs.Stats
    try {
      stat = fs.statSync(GATEWAY_LOG)
    } catch {
      // Log doesn't exist yet (daemon hasn't started). Wait for it.
      return
    }
    if (stat.size < this.offset) {
      // Rotation / truncation. Reset.
      this.offset = 0
      this.partial = ''
    }
    if (stat.size === this.offset) return

    const wantBytes = Math.min(stat.size - this.offset, MAX_BATCH_BYTES)
    const buf = Buffer.alloc(wantBytes)
    let fd: number
    try {
      fd = fs.openSync(GATEWAY_LOG, 'r')
    } catch {
      return
    }
    let read = 0
    try {
      read = fs.readSync(fd, buf, 0, wantBytes, this.offset)
    } finally {
      try { fs.closeSync(fd) } catch {}
    }
    if (read <= 0) return

    const chunk = buf.toString('utf8', 0, read)
    this.offset += read

    const combined = this.partial + chunk
    const lines = combined.split('\n')
    // The last element is the trailing partial — keep it for next cycle.
    this.partial = lines.pop() ?? ''

    for (const line of lines) {
      const trimmed = line.trim()
      if (!trimmed) continue
      if (trimmed[0] !== '{') continue  // skip plain-text INFO lines
      let event: any
      try {
        event = JSON.parse(trimmed)
      } catch {
        continue
      }
      if (!event || typeof event !== 'object') continue
      const sid = typeof event.sessionId === 'string' ? event.sessionId : ''
      if (!isGatewaySessionId(sid)) continue
      this.forward(event)
    }
    this.persistOffset()
  }
}

/**
 * True for session ids minted by a chat-gateway platform — Slack today,
 * other ``freyja:<platform>:...`` ids later. Desktop-owned sessions
 * (``comp_*``, ``msg_*``) don't match this and are processed via the
 * local bridge subprocess.
 */
export function isGatewaySessionId(id: string): boolean {
  if (!id || !id.startsWith('freyja:')) return false
  const rest = id.slice('freyja:'.length)
  const colonIdx = rest.indexOf(':')
  if (colonIdx <= 0) return false
  return rest.length > colonIdx + 1
}
