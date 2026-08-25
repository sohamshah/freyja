import fs from 'node:fs'
import fsp from 'node:fs/promises'
import path from 'node:path'
import os from 'node:os'
import crypto from 'node:crypto'

import type { ArtifactNote } from '../shared/events.js'
import { rowForPath } from './artifactIndex.js'

/**
 * Operator notes on artifacts.
 *
 * A note is a comment pinned to a slice of a file, addressed to whichever
 * session most recently wrote that file. That routing is the whole point: it
 * turns "this paragraph is wrong" into work for the agent that produced the
 * paragraph, without the operator having to remember which of a thousand
 * sessions that was.
 *
 * Storage is one append-only JSONL, matching every other log in the app. A
 * note is written once on create and once more per status change; the reader
 * folds by id and keeps the last write, so the file is self-compacting in
 * meaning if not in size.
 */

const NOTES_FILE = path.join(
  process.env.FREYJA_HOME ?? path.join(os.homedir(), '.freyja'),
  'artifact-notes.jsonl',
)

let writeQueue: Promise<unknown> = Promise.resolve()

function readAllRows(): ArtifactNote[] {
  if (!fs.existsSync(NOTES_FILE)) return []
  try {
    const raw = fs.readFileSync(NOTES_FILE, 'utf8')
    const rows: ArtifactNote[] = []
    for (const line of raw.split('\n')) {
      const trimmed = line.trim()
      if (!trimmed) continue
      try {
        rows.push(JSON.parse(trimmed) as ArtifactNote)
      } catch {
        // Torn tail line from an interrupted append.
      }
    }
    return rows
  } catch {
    return []
  }
}

/** Fold the append-only log into current state: last write per id wins. */
function currentNotes(): ArtifactNote[] {
  const byId = new Map<string, ArtifactNote>()
  for (const row of readAllRows()) {
    if (!row?.id) continue
    byId.set(row.id, row)
  }
  return Array.from(byId.values()).sort((a, b) => b.createdAt - a.createdAt)
}

function append(note: ArtifactNote): Promise<void> {
  // The chain must never be left rejected. Serializing on a promise that can
  // settle rejected means one bad write (a full disk, a permissions blip)
  // poisons every append after it — the queue's .then() simply stops running
  // and notes silently vanish for the rest of the session. Swallow inside the
  // link, report through a separate promise.
  const done = writeQueue.then(async () => {
    try {
      await fsp.mkdir(path.dirname(NOTES_FILE), { recursive: true })
      await fsp.appendFile(NOTES_FILE, JSON.stringify(note) + '\n', 'utf8')
      return null
    } catch (err) {
      console.warn('[artifactNotes] append failed', err)
      return err
    }
  })
  writeQueue = done
  return done.then(() => undefined)
}

/** All notes, newest first. Optionally narrowed to one artifact. */
export function listNotes(artifactPath?: string): ArtifactNote[] {
  const all = currentNotes()
  if (!artifactPath) return all
  return all.filter((n) => n.artifactPath === artifactPath)
}

let countsCache: { mtimeMs: number; size: number; counts: Map<string, number> } | null = null

/**
 * Open-note counts per artifact path, for the browser's list badges.
 *
 * Joined onto every index query, so it cannot afford to re-read and re-parse
 * the whole append-only log each time — that file only grows. Cached against
 * the log's (size, mtimeMs); this process is its only writer, so the
 * fingerprint moving is exactly "someone appended".
 */
export function openNoteCounts(): Map<string, number> {
  let stat: fs.Stats | null = null
  try {
    stat = fs.statSync(NOTES_FILE)
  } catch {
    stat = null
  }
  if (!stat) {
    countsCache = null
    return new Map()
  }
  if (
    countsCache &&
    countsCache.mtimeMs === stat.mtimeMs &&
    countsCache.size === stat.size
  ) {
    return countsCache.counts
  }
  const counts = new Map<string, number>()
  for (const note of currentNotes()) {
    if (note.status === 'resolved') continue
    counts.set(note.artifactPath, (counts.get(note.artifactPath) ?? 0) + 1)
  }
  countsCache = { mtimeMs: stat.mtimeMs, size: stat.size, counts }
  return counts
}

export interface CreateNoteInput {
  artifactPath: string
  body: string
  anchor: { startLine: number; endLine: number; quote: string } | null
  /** Override the auto-resolved target. Empty means "use the index". */
  targetSessionId?: string
}

/**
 * Write a note and resolve who it is for.
 *
 * The target is the artifact's most recent writer, which the index already
 * tracks — `creatorId` on the newest manifest row is the sub-agent that wrote
 * it, `sessionId` is the session that owns the project dir. We address the
 * owning session: a sub-agent is usually finished and archived by the time the
 * operator reads its output, while the parent is the thing that can act.
 */
export async function createNote(input: CreateNoteInput): Promise<ArtifactNote> {
  const row = rowForPath(input.artifactPath)
  const note: ArtifactNote = {
    id: 'note_' + crypto.randomBytes(8).toString('hex'),
    artifactPath: input.artifactPath,
    artifactFilename: row?.filename ?? path.basename(input.artifactPath),
    createdAt: Date.now(),
    anchor: input.anchor,
    body: input.body,
    targetSessionId: (input.targetSessionId || row?.sessionId || '').trim(),
    targetSessionTitle: row?.sessionTitle,
    status: 'pending',
    artifactSha256: row?.sha256 ?? null,
  }
  await append(note)
  return note
}

/** Record the outcome of a delivery attempt. */
export async function markDelivered(
  id: string,
  outcome: { ok: boolean; error?: string },
): Promise<ArtifactNote | null> {
  const note = currentNotes().find((n) => n.id === id)
  if (!note) return null
  const next: ArtifactNote = outcome.ok
    ? { ...note, status: 'delivered', deliveredAt: Date.now(), error: undefined }
    : { ...note, status: 'failed', error: outcome.error ?? 'delivery failed' }
  await append(next)
  return next
}

/** Operator dismissed the note — it stops counting against the artifact. */
export async function resolveNote(id: string): Promise<ArtifactNote | null> {
  const note = currentNotes().find((n) => n.id === id)
  if (!note) return null
  const next: ArtifactNote = { ...note, status: 'resolved', resolvedAt: Date.now() }
  await append(next)
  return next
}
