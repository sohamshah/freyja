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
 */

const DAEMON_LOG = path.join(os.homedir(), '.freyja', 'logs', 'daemon.log')

const POLL_INTERVAL_MS = 1000
const SCHEDULER_EVENT = /^scheduler_(run|job)_/
// The daemon's sessions stream into the same log; a batch is capped so a
// busy hour is not parsed in one go.
const MAX_BATCH_BYTES = 1 * 1024 * 1024

export class SchedulerDaemonLogTailer {
  private offset = 0
  private partial = '' // The trailing partial line.
  private timer: NodeJS.Timeout | null = null

  constructor(private readonly forward: BridgeEventForwarder) {}

  start(): void {
    this.stop()
    try {
      this.offset = fs.statSync(DAEMON_LOG).size
    } catch {
      this.offset = 0 // Not written yet: read it from its start.
    }
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
    let size: number
    try {
      size = fs.statSync(DAEMON_LOG).size
    } catch {
      return
    }
    if (size < this.offset) {
      // Truncated: read it again from its start.
      this.offset = 0
      this.partial = ''
    }
    if (size === this.offset) return
    const buf = Buffer.alloc(Math.min(size - this.offset, MAX_BATCH_BYTES))
    const fd = fs.openSync(DAEMON_LOG, 'r')
    let read = 0
    try {
      read = fs.readSync(fd, buf, 0, buf.length, this.offset)
    } finally {
      fs.closeSync(fd)
    }
    if (read <= 0) return
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
  }
}
