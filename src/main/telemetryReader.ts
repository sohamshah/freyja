import fs from 'node:fs'
import path from 'node:path'
import os from 'node:os'

/**
 * Reads the compaction telemetry JSONL the Python bridge appends to
 * (``~/.freyja/telemetry/compaction.jsonl``) for the metrics dashboard.
 *
 * Why not readFile + tail-trim:
 *   The dashboard's "all" window has to cover every row ever written.
 *   The old handler kept only the last 10k lines, which silently turned
 *   "all time" into "the last N events" — at ~10k rows a week that was
 *   one week of spend presented as the lifetime total.
 *
 * Why incremental:
 *   The file is append-only and tens of MB. Re-parsing it on every
 *   Refresh click stalls the main process for ~200ms. We keep the parsed
 *   rows plus the byte offset we've consumed, so a refresh only reads and
 *   parses what was appended since. A shrink or inode change (rotation,
 *   manual truncation) resets and re-reads from zero.
 *
 * Rotation:
 *   The bridge slides compaction.jsonl → compaction.jsonl.old once it
 *   passes 256 MB. "All time" includes the .old rows ahead of the live
 *   file's so a rotation doesn't erase history from the dashboard.
 */

const TELEMETRY_DIR = path.join(os.homedir(), '.freyja', 'telemetry')
export const TELEMETRY_FILE = path.join(TELEMETRY_DIR, 'compaction.jsonl')
export const TELEMETRY_ROTATED = `${TELEMETRY_FILE}.old`

const NEWLINE = 0x0a

/**
 * Append-only JSONL file cache. ``read()`` returns every parsed row in the
 * file, doing the minimum I/O to get there.
 */
export class JsonlFileCache {
  private offset = 0
  private ino = -1
  private rows: unknown[] = []
  private inflight: Promise<unknown[]> | null = null

  constructor(readonly filePath: string) {}

  /** Parsed rows for the whole file. The returned array is owned by the
   *  cache — callers must not mutate it. */
  read(): Promise<unknown[]> {
    // Coalesce concurrent callers (double-click on Refresh, two windows)
    // so two readers can't both consume the same tail and double-append.
    if (!this.inflight) {
      this.inflight = this.readUncoalesced().finally(() => {
        this.inflight = null
      })
    }
    return this.inflight
  }

  private reset(ino: number): void {
    this.offset = 0
    this.ino = ino
    this.rows = []
  }

  private async readUncoalesced(): Promise<unknown[]> {
    let st: fs.Stats
    try {
      st = await fs.promises.stat(this.filePath)
    } catch (err: any) {
      if (err?.code === 'ENOENT') {
        this.reset(-1)
        return this.rows
      }
      throw err
    }
    // A different inode means the path was replaced (rotation); a size
    // below our offset means it was truncated in place. Either way our
    // offset no longer refers to this content.
    if (st.ino !== this.ino || st.size < this.offset) this.reset(st.ino)
    if (st.size === this.offset) return this.rows

    const fh = await fs.promises.open(this.filePath, 'r')
    try {
      const len = st.size - this.offset
      const buf = Buffer.allocUnsafe(len)
      let got = 0
      while (got < len) {
        const { bytesRead } = await fh.read(buf, got, len - got, this.offset + got)
        if (bytesRead === 0) break
        got += bytesRead
      }
      // Only consume through the last complete line. The writer appends
      // the JSON and its newline in two writes, so we can observe a
      // half-written tail; it's picked up whole on the next read. A
      // newline byte never occurs inside a multi-byte UTF-8 sequence, so
      // cutting here can't split a character.
      const end = got === 0 ? -1 : buf.lastIndexOf(NEWLINE, got - 1)
      if (end < 0) return this.rows
      const text = buf.toString('utf8', 0, end)
      for (const line of text.split('\n')) {
        if (line.trim().length === 0) continue
        try {
          this.rows.push(JSON.parse(line))
        } catch {
          // skip malformed
        }
      }
      this.offset += end + 1
    } finally {
      await fh.close()
    }
    return this.rows
  }
}

const rotated = new JsonlFileCache(TELEMETRY_ROTATED)
const live = new JsonlFileCache(TELEMETRY_FILE)

/** Every compaction telemetry row on disk, oldest first: the rotated
 *  file (if any) followed by the live one. */
export async function readCompactionTelemetry(): Promise<unknown[]> {
  const [oldRows, liveRows] = await Promise.all([rotated.read(), live.read()])
  return oldRows.length === 0 ? liveRows : oldRows.concat(liveRows)
}
