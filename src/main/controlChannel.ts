import fs from 'node:fs'
import path from 'node:path'
import os from 'node:os'

/**
 * Append-only JSON-line writer for desktop → daemon commands.
 *
 * Mirrors ``bridge/gateway/control_channel.py`` on the daemon side:
 * one JSON command per line in ``~/.freyja/control/commands.jsonl``.
 * The daemon tails the same file and dispatches by ``type``.
 *
 * Today we only emit ``permission_response``. Future commands
 * (``cancel_turn``, ``set_permission_policy``, ...) drop in by adding
 * a method here and a handler in ``run.py``.
 *
 * Why a file and not a socket: keeps both sides decoupled from each
 * other's lifecycle. The daemon can restart, the desktop can restart,
 * and command delivery resumes at the file's last byte offset without
 * either side needing to handshake. Append+fsync is atomic enough for
 * a single-writer (the desktop is the only writer).
 */

const HOME = os.homedir()
const CONTROL_DIR = path.join(HOME, '.freyja', 'control')
const COMMANDS_FILE = path.join(CONTROL_DIR, 'commands.jsonl')

export type ControlCommand =
  | {
      type: 'permission_response'
      sessionId: string
      requestId: string
      approved: boolean
      response?: string
    }
  | {
      type: 'set_permission_policy'
      sessionId: string
      autoApprove: string
    }
  // Add new commands here as `| { type: 'cancel_turn'; ... }` etc.

function ensureDir(): void {
  try {
    fs.mkdirSync(CONTROL_DIR, { recursive: true })
  } catch {
    // Permission errors here mean the desktop can't ever talk to the
    // daemon — let the caller see the eventual write failure rather
    // than silently swallowing the cause.
  }
}

export function sendControlCommand(cmd: ControlCommand): { ok: boolean; error?: string } {
  ensureDir()
  try {
    const line = JSON.stringify(cmd) + '\n'
    // Append atomically — open with O_APPEND so concurrent writers
    // (we don't have any today, but defensive against future renderer
    // background workers) interleave at line boundaries rather than
    // shredding each other's payloads.
    fs.appendFileSync(COMMANDS_FILE, line, { mode: 0o600 })
    return { ok: true }
  } catch (err) {
    return { ok: false, error: String((err as Error)?.message ?? err) }
  }
}
