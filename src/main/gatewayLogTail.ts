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
 *   ~/.freyja/control/desktop-tail.offset stores the end of the last whole
 *   line we've consumed and the inode of the .1 we had read: a line the
 *   gateway is still writing, or one a batch cuts, is read again whole at
 *   the next start. On first start (no offset file) we jump to EOF so we
 *   don't replay days of historical log lines. Across desktop restarts we
 *   resume cleanly so a brief restart doesn't miss in-flight events, nor
 *   those of a rotation while the app was closed: a .1 that is not the one
 *   we had read is read on from the offset, then the log from its start. A
 *   bare offset (written before the inode was saved) takes the .1 there
 *   now as read.
 *
 * Rotation:
 *   The gateway rotates its log in place (rotate_log_if_large in
 *   bridge/gateway/pid.py): it copies the whole log to gateway.log.1, each
 *   byte at the offset it had, then empties the log. A new .1 and a log
 *   shorter than it is a rotation: the tail reads on in .1 from where it
 *   was, then reads the log from its start. Read from its start alone, the
 *   log lost the events written since the last read. A log that shrinks
 *   below our offset with no new .1 (a ``> gateway.log`` from the operator)
 *   is read again from its start.
 */

export type BridgeEventForwarder = (event: any) => void

const HOME = os.homedir()
const GATEWAY_LOG = path.join(HOME, '.freyja', 'logs', 'gateway.log')
const GATEWAY_LOG_SEGMENT = `${GATEWAY_LOG}.1`
const OFFSET_FILE = path.join(HOME, '.freyja', 'control', 'desktop-tail.offset')

const POLL_INTERVAL_MS = 250
// Cap each batch read so an idle desktop process can't suddenly try to
// parse 50MB at once after the daemon's been busy for an hour without us.
const MAX_BATCH_BYTES = 1 * 1024 * 1024

export class GatewayLogTailer {
  private offset = 0
  private timer: NodeJS.Timeout | null = null
  private stopped = false
  // The bytes of the trailing partial line, decoded once it ends: a
  // character a batch cuts in two is whole then.
  private partial = Buffer.alloc(0)
  // The inode of the .1 the tail has read to its end, or that was there
  // when it first started; saved with the offset.
  private segment: number | null = null
  // The offset and .1 at the end of the last whole line read: what is saved.
  private lineEnd: { offset: number, segment: number | null } = { offset: 0, segment: null }
  // Every sessionId we've seen emit a daemon-side event. Includes both
  // gateway root sessions (``freyja:<platform>:...``) and sub-agents
  // spawned beneath them (``sub_<hex>_<n>``). Used by the desktop main
  // process to decide whether a renderer-issued command should be
  // written to the daemon's control channel or sent to the local
  // bridge subprocess. Without this, sub-agent permission approvals
  // were getting misrouted to the local bridge — which has no idea
  // the session exists — and silently dropped.
  //
  // Bounded LRU via Map insertion order: a long-running desktop sees
  // many sub-agents come and go and we'd otherwise leak entries for
  // every one. Eviction is FIFO (oldest sid drops first). Evicted
  // entries still resolve correctly for root sessions via the
  // ``freyja:`` prefix bootstrap in isDaemonOwned.
  private static readonly OWNED_CAP = 10_000
  private daemonOwned = new Map<string, number>()

  constructor(private readonly forward: BridgeEventForwarder) {}

  /** True iff ``sid`` belongs to the gateway daemon (and therefore
   *  commands for it must go through the control channel, not the
   *  local bridge subprocess). Two acceptance paths:
   *
   *    1. We've seen at least one event with that sessionId on the
   *       gateway log — covers sub-agents whose ids are opaque
   *       hashes (``sub_19d8b53e6f0_1``) with no prefix to grep on.
   *    2. The sessionId is shaped like a gateway root session
   *       (``freyja:<platform>:...``) — bootstrap path for commands
   *       that fire before we've observed any events for that session
   *       (e.g. the operator opens a Slack session in the desktop and
   *       immediately approves something).
   */
  isDaemonOwned(sid: string): boolean {
    if (!sid) return false
    if (this.daemonOwned.has(sid)) return true
    return isGatewaySessionId(sid)
  }

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
      const saved = raw.startsWith('{') ? JSON.parse(raw) : { offset: Number.parseInt(raw, 10) }
      const n = saved.offset
      if (Number.isFinite(n) && n >= 0) {
        this.offset = n
        this.segment = 'segment' in saved ? saved.segment : statOf(GATEWAY_LOG_SEGMENT)?.ino ?? null
        this.lineEnd = { offset: this.offset, segment: this.segment }
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
    this.segment = statOf(GATEWAY_LOG_SEGMENT)?.ino ?? null
    this.lineEnd = { offset: this.offset, segment: this.segment }
    this.persistOffset()
  }

  private persistOffset(): void {
    try {
      fs.writeFileSync(OFFSET_FILE, JSON.stringify(this.lineEnd), 'utf8')
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
    const size = statOf(GATEWAY_LOG)?.size
    // Log doesn't exist yet (daemon hasn't started). Wait for it.
    if (size === undefined) return
    const segment = statOf(GATEWAY_LOG_SEGMENT)
    if (segment && segment.ino !== this.segment && size < segment.size) {
      // Rotated: the rest of .1, whatever the batch cap, then the log. Seen
      // while .1 was new but the log not yet emptied, the log was read on as
      // usual: the same bytes, at the same offsets.
      while (this.offset < segment.size) {
        if (this.read(GATEWAY_LOG_SEGMENT, segment.size) === 0) break
      }
      this.segment = segment.ino
      this.offset = 0
      if (!this.partial.length) this.lineEnd = { offset: 0, segment: this.segment }
    } else if (size < this.offset) {
      // Truncated by hand: read it again from its start.
      this.offset = 0
      this.partial = Buffer.alloc(0)
      this.lineEnd = { offset: 0, segment: this.segment }
    }
    if (size > this.offset) this.read(GATEWAY_LOG, size)
  }

  /** Read ``file`` on from the offset, up to ``size`` and at most a batch,
   *  pass on its events, and return how many bytes it read. */
  private read(file: string, size: number): number {
    const wantBytes = Math.min(size - this.offset, MAX_BATCH_BYTES)
    const buf = Buffer.alloc(wantBytes)
    let fd: number
    try {
      fd = fs.openSync(file, 'r')
    } catch {
      return 0
    }
    let read = 0
    try {
      read = fs.readSync(fd, buf, 0, wantBytes, this.offset)
    } finally {
      try { fs.closeSync(fd) } catch {}
    }
    if (read <= 0) return 0

    // Only whole lines are decoded; the bytes after the last newline wait
    // for the next read.
    const end = buf.lastIndexOf(0x0a, read - 1) + 1
    let lines: string[] = []
    if (end > 0) {
      lines = Buffer.concat([this.partial, buf.subarray(0, end)]).toString('utf8').split('\n')
      this.partial = Buffer.from(buf.subarray(end, read))
      this.lineEnd = { offset: this.offset + end, segment: this.segment }
    } else {
      this.partial = Buffer.concat([this.partial, buf.subarray(0, read)])
    }
    this.offset += read

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
      // Skip events with no sessionId (daemon boot/shutdown messages,
      // adapter status logs, etc.) — the renderer's store routes by
      // sessionId so an undirected event has nowhere meaningful to
      // land. An error is the exception: one that names no session is a
      // notice, shown as the local bridge's own are.
      if (!sid) {
        if (event.type === 'error') this.forward(event)
        continue
      }
      // Record ownership so future commands for this session route to
      // the daemon. ``session_spawned`` events for sub-agents carry
      // the child's id here and the parent's id in ``parentSessionId``;
      // tracking the child unblocks the swarm panel + permission flow.
      // ``delete``-then-``set`` moves an existing sid to the end of
      // the iteration order, giving us LRU semantics on a Map.
      this.daemonOwned.delete(sid)
      this.daemonOwned.set(sid, Date.now())
      if (this.daemonOwned.size > GatewayLogTailer.OWNED_CAP) {
        const oldest = this.daemonOwned.keys().next().value
        if (oldest !== undefined) this.daemonOwned.delete(oldest)
      }
      this.forward(event)
    }
    this.persistOffset()
    return read
  }
}

function statOf(file: string): fs.Stats | null {
  try {
    return fs.statSync(file)
  } catch {
    return null
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
