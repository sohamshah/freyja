import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { StringDecoder } from 'node:string_decoder'
import type { ModelCall, ModelResult, ModelView } from '../shared/sessionExport'

/**
 * The record of what a model was sent and what it sent back, read from disk.
 *
 * The app's own session record is made for the UI: the bridge cuts a tool
 * result's preview to 2000 characters, and the app stores a call's arguments
 * after path normalization. Two logs hold the exact record.
 *
 *  - ``~/.freyja/projects/<id>/raw_messages.jsonl`` is every message the
 *    engine appended: each call as the model sent it, each result as the
 *    model received it. It is append-only, and compaction never touches it.
 *  - ``~/.freyja/truncated/*.txt`` holds the whole output of a tool result
 *    the engine cut to fit the model's context. The text the model saw ends
 *    with ``[Content truncated -- ... Full output saved to: <path>]``, and the
 *    raw log has that cut text.
 *
 * Exports join the two, so no result is cut.
 */

/** Make a session id safe to use as a directory name. Matches the bridge. */
export function sanitizeForFsId(id: string): string {
  return id.replace(/[^A-Za-z0-9_.-]+/g, '-').replace(/^[.-]+|[.-]+$/g, '') || 'session'
}

export function projectDirFor(id: string): string {
  return path.join(os.homedir(), '.freyja', 'projects', sanitizeForFsId(id))
}

// The engine writes the full output of a cut tool result here. Read from HOME
// when the module loads, like the rest of main's paths.
const TRUNCATED_DIR = path.join(os.homedir(), '.freyja', 'truncated')

function parseLine(line: string): unknown {
  const trimmed = line.trim()
  if (!trimmed) return undefined
  try {
    return JSON.parse(trimmed)
  } catch {
    // Skip a malformed line. An append-only log can end in a line an
    // interrupted write cut short.
    return undefined
  }
}

/** The rows of a JSONL file, one at a time. It reads in chunks, so a log
 *  larger than one string can hold is still read whole. A missing file is
 *  empty. */
export function* jsonlRows(filePath: string): Generator<unknown> {
  let fd: number
  try {
    fd = fs.openSync(filePath, 'r')
  } catch {
    return
  }
  try {
    const decoder = new StringDecoder('utf8')
    const chunk = Buffer.allocUnsafe(8 * 1024 * 1024)
    let tail = ''
    for (;;) {
      const n = fs.readSync(fd, chunk, 0, chunk.length, null)
      if (n === 0) break
      tail += decoder.write(chunk.subarray(0, n))
      let start = 0
      for (let nl = tail.indexOf('\n', start); nl !== -1; nl = tail.indexOf('\n', start)) {
        const row = parseLine(tail.slice(start, nl))
        start = nl + 1
        if (row !== undefined) yield row
      }
      tail = tail.slice(start)
    }
    const row = parseLine(tail + decoder.end())
    if (row !== undefined) yield row
  } finally {
    fs.closeSync(fd)
  }
}

export function readJsonl(filePath: string): unknown[] {
  return [...jsonlRows(filePath)]
}

// ─── Cut tool results ───────────────────────────────────────────────

// What the engine appends to a result it cut (engine/tools.py,
// ToolResultTruncator): the size, and where the whole output went.
const CUT_WITH_SIZE =
  /\[Content truncated -- showing ~[\d,]+ of [\d,]+ tokens \(([\d,]+) chars\)\.(?: Full output saved to: ([^\]\n]+))?\]/
// What it appends when it could not write the file.
const CUT_NO_FILE = /\[Content truncated\. Use filters or parameters to narrow results\.\]/
// The cut notice, with its structure and tip lines, ends the text. A tool
// result that only quotes one earlier in its text is not a cut result.
const CUT_NOTICE_MAX_CHARS = 2000

/** The whole output the engine saved, if it is still there. Only files under
 *  the engine's own directory are read: the path comes from text a tool
 *  returned, and an export must not read whatever that text names. */
function readSavedOutput(file: string): string | undefined {
  try {
    const real = fs.realpathSync(file)
    const root = fs.realpathSync(TRUNCATED_DIR)
    if (!real.startsWith(root + path.sep)) return undefined
    return fs.readFileSync(real, 'utf8')
  } catch {
    return undefined
  }
}

interface Restored {
  text: string
  /** The engine cut this text for the model. */
  cut: boolean
  /** `text` is the whole output, read back from the saved file. */
  restored: boolean
  originalChars?: number
}

/** The whole text of one result text block, when the engine cut it for the
 *  model and saved the rest. */
function restoreOriginal(text: string): Restored {
  const sized = CUT_WITH_SIZE.exec(text)
  if (sized && text.length - sized.index <= CUT_NOTICE_MAX_CHARS) {
    const originalChars = Number(sized[1].replace(/,/g, ''))
    const file = sized[2]?.trim()
    const saved = file ? readSavedOutput(file) : undefined
    // The cut text is the start of the whole output. If it is not, the file
    // is not this result's, and the cut text stands.
    const head = text.slice(0, sized.index).replace(/\n+$/, '')
    if (saved !== undefined && saved.startsWith(head)) {
      return { text: saved, cut: true, restored: true, originalChars }
    }
    return { text, cut: true, restored: false, originalChars }
  }
  const bare = CUT_NO_FILE.exec(text)
  if (bare && text.length - bare.index <= CUT_NOTICE_MAX_CHARS) {
    return { text, cut: true, restored: false }
  }
  return { text, cut: false, restored: false }
}

/** A result as the model received it: its text blocks, whole. */
function modelResult(texts: string[]): ModelResult {
  const parts = texts.map(restoreOriginal)
  const anyCut = parts.some((p) => p.cut)
  const unrestored = parts.some((p) => p.cut && !p.restored)
  const originalChars = parts.reduce<number | undefined>(
    (sum, p) => (p.originalChars === undefined ? sum : (sum ?? 0) + p.originalChars),
    undefined,
  )
  return {
    text: parts.map((p) => p.text).join('\n'),
    source: unrestored ? 'transcript-truncated' : anyCut ? 'original' : 'transcript',
    complete: !unrestored,
    ...(anyCut && originalChars !== undefined ? { originalChars } : {}),
  }
}

// ─── The raw transcript ─────────────────────────────────────────────

export interface RawRecord extends ModelView {
  /** A fresh pass over the raw log's rows, as written. */
  rows(): Generator<unknown>
  /** Every result the engine cut, restored whole, one at a time. */
  restoredResults(): Generator<{
    tool_call_id: string
    source: ModelResult['source']
    complete: boolean
    original_chars?: number
    chars: number
    text: string
  }>
}

/** Index a session's raw transcript. It keeps each call's arguments and the
 *  text of each result, not the rows, so an image-heavy log costs no more
 *  memory. A cut result's saved file is read when the result is asked for. */
export function readRawRecord(id: string): RawRecord {
  const rawPath = path.join(projectDirFor(id), 'raw_messages.jsonl')
  const calls = new Map<string, ModelCall>()
  const resultTexts = new Map<string, string[]>()
  for (const row of jsonlRows(rawPath) as Iterable<{ message?: Record<string, unknown> } | null>) {
    const message = row?.message
    if (!message) continue
    if (Array.isArray(message.tool_calls)) {
      for (const call of message.tool_calls as Array<Record<string, unknown>>) {
        if (!call || typeof call.id !== 'string' || typeof call.name !== 'string') continue
        const args = call.arguments
        calls.set(call.id, {
          name: call.name,
          arguments:
            args && typeof args === 'object' && !Array.isArray(args)
              ? (args as Record<string, unknown>)
              : {},
        })
      }
    }
    if (message.role === 'tool_result' && typeof message.tool_call_id === 'string') {
      const content = message.content
      if (typeof content === 'string') {
        resultTexts.set(message.tool_call_id, [content])
      } else if (Array.isArray(content)) {
        resultTexts.set(
          message.tool_call_id,
          content
            .filter((b): b is { type: string; text: string } =>
              !!b && b.type === 'text' && typeof b.text === 'string')
            .map((b) => b.text),
        )
      }
    }
  }
  return {
    calls,
    results: {
      get(callId: string) {
        const texts = resultTexts.get(callId)
        return texts ? modelResult(texts) : undefined
      },
    },
    rows: () => jsonlRows(rawPath),
    *restoredResults() {
      for (const [callId, texts] of resultTexts) {
        const result = modelResult(texts)
        if (result.source !== 'original') continue
        yield {
          tool_call_id: callId,
          source: result.source,
          complete: result.complete,
          ...(result.originalChars !== undefined ? { original_chars: result.originalChars } : {}),
          chars: result.text.length,
          text: result.text,
        }
      }
    },
  }
}

// ─── Streaming JSON ─────────────────────────────────────────────────

/** An object whose entries are produced one at a time while it is written. */
export class LazyObject {
  constructor(readonly entries: () => Iterable<[string, unknown]>) {}
}

/** Written like an array. A generator or any other iterable that is not an
 *  array is written this way too, one element at a time. */
function isLazyArray(value: unknown): value is Iterable<unknown> {
  return (
    !!value &&
    typeof value === 'object' &&
    !Array.isArray(value) &&
    typeof (value as { [Symbol.iterator]?: unknown })[Symbol.iterator] === 'function'
  )
}

function isPlainObject(value: unknown): value is Record<string, unknown> {
  if (!value || typeof value !== 'object') return false
  const proto = Object.getPrototypeOf(value)
  return proto === Object.prototype || proto === null
}

class Sink {
  private parts: string[] = []
  private size = 0
  constructor(private readonly fd: number) {}

  /** Text longer than a piece (a tool result can be 100 MB) goes out in
   *  pieces, so it is never copied whole. A piece never ends inside a
   *  surrogate pair. */
  write(text: string): void {
    if (text.length > PIECE_CHARS) {
      for (let start = 0; start < text.length; ) {
        let end = Math.min(text.length, start + PIECE_CHARS)
        const last = text.charCodeAt(end - 1)
        if (end < text.length && last >= 0xd800 && last <= 0xdbff) end += 1
        this.push(text.slice(start, end))
        start = end
      }
      return
    }
    this.push(text)
  }

  private push(text: string): void {
    this.parts.push(text)
    this.size += text.length
    if (this.size >= 1 << 20) this.flush()
  }

  flush(): void {
    if (this.parts.length === 0) return
    const buf = Buffer.from(this.parts.join(''), 'utf8')
    this.parts = []
    this.size = 0
    // A write may take fewer bytes than asked; keep going until all are out.
    for (let off = 0; off < buf.length; ) off += fs.writeSync(this.fd, buf, off, buf.length - off)
  }
}

// How much text goes through the sink in one piece.
const PIECE_CHARS = 4 * 1024 * 1024

/** A JSON string, written in pieces. Each piece is escaped by itself, which
 *  reads back as the same string. */
function writeLongString(sink: Sink, text: string): void {
  sink.write('"')
  for (let start = 0; start < text.length; ) {
    let end = Math.min(text.length, start + PIECE_CHARS)
    const last = text.charCodeAt(end - 1)
    if (end < text.length && last >= 0xd800 && last <= 0xdbff) end += 1
    sink.write(JSON.stringify(text.slice(start, end)).slice(1, -1))
    start = end
  }
  sink.write('"')
}

// Objects and arrays are walked this deep, so a lazy value inside the
// session (its tool calls, under `slice`) is reached. Below that, a value is
// written in one piece.
const WALK_DEPTH = 3

function writeValue(sink: Sink, value: unknown, depth: number): void {
  const pad = '  '.repeat(depth + 1)
  const close = '  '.repeat(depth)
  if (value instanceof LazyObject) {
    sink.write('{')
    let first = true
    for (const [key, entry] of value.entries()) {
      if (entry === undefined) continue
      sink.write(`${first ? '\n' : ',\n'}${pad}${JSON.stringify(key)}: `)
      writeValue(sink, entry, depth + 1)
      first = false
    }
    sink.write(first ? '}' : `\n${close}}`)
    return
  }
  if (isLazyArray(value) || (Array.isArray(value) && depth < WALK_DEPTH)) {
    sink.write('[')
    let first = true
    for (const element of value as Iterable<unknown>) {
      sink.write(first ? `\n${pad}` : `,\n${pad}`)
      writeValue(sink, element === undefined ? null : element, depth + 1)
      first = false
    }
    sink.write(first ? ']' : `\n${close}]`)
    return
  }
  if (isPlainObject(value) && depth < WALK_DEPTH) {
    sink.write('{')
    let first = true
    for (const [key, entry] of Object.entries(value)) {
      if (entry === undefined || typeof entry === 'function') continue
      sink.write(`${first ? '\n' : ',\n'}${pad}${JSON.stringify(key)}: `)
      writeValue(sink, entry, depth + 1)
      first = false
    }
    sink.write(first ? '}' : `\n${close}}`)
    return
  }
  if (typeof value === 'string' && value.length > PIECE_CHARS) {
    writeLongString(sink, value)
    return
  }
  sink.write(JSON.stringify(value) ?? 'null')
}

/** Fill a file through a sink. The file appears only when it is whole. */
function writeAtomically(dest: string, fill: (sink: Sink) => void): void {
  const partial = `${dest}.partial`
  const fd = fs.openSync(partial, 'w')
  try {
    const sink = new Sink(fd)
    fill(sink)
    sink.flush()
    fs.closeSync(fd)
    fs.renameSync(partial, dest)
  } catch (err) {
    try {
      fs.closeSync(fd)
    } catch {
      // Already closed.
    }
    fs.rmSync(partial, { force: true })
    throw err
  }
}

/** Write `root` as JSON without building it as one string, so a session whose
 *  JSON passes a string's length limit is still written. Lazy values
 *  (`LazyObject`, a generator) are read as they are written, so they never all
 *  sit in memory. */
export function writeJsonFile(dest: string, root: unknown): void {
  writeAtomically(dest, (sink) => {
    writeValue(sink, root, 0)
    sink.write('\n')
  })
}

/** Write text a piece at a time, for the same reason. */
export function writeTextFile(dest: string, chunks: Iterable<string>): void {
  writeAtomically(dest, (sink) => {
    for (const chunk of chunks) sink.write(chunk)
  })
}
