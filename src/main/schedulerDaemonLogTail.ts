import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import type { BridgeEventForwarder } from './gatewayLogTail.js'

/**
 * Follows the scheduler daemon's log and passes the events of the runs it
 * starts and of the jobs it changes to the renderer, as the app's bridge
 * sends its own on its stdout and the gateway's arrive through
 * gatewayLogTail.ts.
 *
 * The scheduler that owns the tick is often the daemon, a LaunchAgent
 * whose stdout is ~/.freyja/logs/daemon.log. Its runs' events there
 * (system events named scheduler_run_claimed, then scheduler_run_<status>)
 * reached no one, so the schedules dashboard and the sidebar showed its
 * runs as they were when last read: a finished run "running", a new one
 * missing. So did its jobs' (scheduler_job_<what>): a job its run made, or
 * one it stopped, stayed as the job list was when last read.
 *
 * Only those events are passed on; the daemon's sessions stay its own.
 * The tail starts at the end of the log: the renderer reads the jobs and
 * their runs when it starts.
 *
 * The daemon rotates its log in place (rotate_daemon_log_if_large in
 * bridge/scheduler/daemon.py): it copies the whole log to daemon.log.1, each
 * byte at the offset it had, then empties the log. A new .1 and a log
 * shorter than it is a rotation: the tail reads on in .1 from where it was,
 * then reads the log from its start. Read from its start alone, the log
 * lost the events written since the last read.
 */

const DAEMON_LOG = path.join(os.homedir(), '.freyja', 'logs', 'daemon.log')
const DAEMON_LOG_SEGMENT = `${DAEMON_LOG}.1`

const POLL_INTERVAL_MS = 1000
const SCHEDULER_EVENT = /^scheduler_(run|job)_/
// The daemon's sessions stream into the same log; a batch is capped so a
// busy hour is not parsed in one go.
const MAX_BATCH_BYTES = 1 * 1024 * 1024

export class SchedulerDaemonLogTailer {
  private offset = 0
  private partial = '' // The trailing partial line.
  // The inode of the .1 the tail has read to its end, or that was there
  // when it started.
  private segment: number | null = null
  private timer: NodeJS.Timeout | null = null

  constructor(private readonly forward: BridgeEventForwarder) {}

  start(): void {
    this.stop()
    try {
      this.offset = fs.statSync(DAEMON_LOG).size
    } catch {
      this.offset = 0 // Not written yet: read it from its start.
    }
    this.segment = statOf(DAEMON_LOG_SEGMENT)?.ino ?? null
    this.timer = setInterval(() => {
      try {
        this.drain()
      } catch (err) {
        console.error('[schedulerDaemonLogTail] drain failed:', err)
      }
    }, POLL_INTERVAL_MS)
  }

  stop(): void {
    if (this.timer) clearInterval(this.timer)
    this.timer = null
  }

  private drain(): void {
    const size = statOf(DAEMON_LOG)?.size
    if (size === undefined) return
    const segment = statOf(DAEMON_LOG_SEGMENT)
    if (segment && segment.ino !== this.segment && size < segment.size) {
      // Rotated: the rest of .1, whatever the batch cap, then the log. Seen
      // while .1 was new but the log not yet emptied, the log was read on as
      // usual: the same bytes, at the same offsets.
      while (this.offset < segment.size) {
        if (this.read(DAEMON_LOG_SEGMENT, segment.size) === 0) break
      }
      this.segment = segment.ino
      this.offset = 0
    } else if (size < this.offset) {
      // Emptied by hand: read it again from its start.
      this.offset = 0
      this.partial = ''
    }
    if (size > this.offset) this.read(DAEMON_LOG, size)
  }

  /** Read ``file`` on from the offset, up to ``size`` and at most a batch,
   *  pass on its scheduler events, and return how many bytes it read. */
  private read(file: string, size: number): number {
    const buf = Buffer.alloc(Math.min(size - this.offset, MAX_BATCH_BYTES))
    const fd = fs.openSync(file, 'r')
    let read = 0
    try {
      read = fs.readSync(fd, buf, 0, buf.length, this.offset)
    } finally {
      fs.closeSync(fd)
    }
    if (read <= 0) return 0
    this.offset += read
    const lines = (this.partial + buf.toString('utf8', 0, read)).split('\n')
    this.partial = lines.pop() ?? ''
    for (const line of lines) {
      // Cheap gate first: most lines are the daemon's session streams.
      if (line[0] !== '{' || !line.includes('"scheduler_')) continue
      let event: any
      try {
        event = JSON.parse(line)
      } catch {
        continue
      }
      if (
        event?.type === 'system_event'
        && typeof event.subtype === 'string'
        && SCHEDULER_EVENT.test(event.subtype)
      ) {
        this.forward(event)
      }
    }
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
