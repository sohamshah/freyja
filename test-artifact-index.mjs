// Global artifact index. Builds a fixture ~/.freyja tree, ingests it, and
// drives the query/revision/edit paths the browser depends on. Run from the
// repo root:
//   npx tsx test-artifact-index.mjs
//
// FREYJA_HOME is set before the module is imported because artifactIndex.ts
// resolves its paths once at load.
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import crypto from 'node:crypto'

const HOME = fs.mkdtempSync(path.join(os.tmpdir(), 'freyja-index-test-'))
process.env.FREYJA_HOME = HOME

const { query, stats, ingest, revisions, recordUserEdit, rowForPath, _resetForTests } =
  await import('./src/main/artifactIndex.ts')
const notes = await import('./src/main/artifactNotes.ts')

let failures = 0
function check(name, actual, expected) {
  const a = JSON.stringify(actual)
  const e = JSON.stringify(expected)
  if (a === e) {
    console.log(`  ok   ${name}`)
  } else {
    failures++
    console.log(`  FAIL ${name}\n         expected ${e}\n         actual   ${a}`)
  }
}

// ── fixture ──────────────────────────────────────────────────────────────

const PROJECTS = path.join(HOME, 'projects')
const SESSIONS = path.join(HOME, 'sessions')
fs.mkdirSync(PROJECTS, { recursive: true })
fs.mkdirSync(SESSIONS, { recursive: true })

function sha(text) {
  return crypto.createHash('sha256').update(text, 'utf8').digest('hex')
}

/** Write a real file and return a manifest row describing it. */
function artifact(projectDir, name, body, row = {}) {
  const filePath = path.join(projectDir, name)
  fs.mkdirSync(path.dirname(filePath), { recursive: true })
  fs.writeFileSync(filePath, body, 'utf8')
  return {
    id: 'art_' + crypto.randomBytes(6).toString('hex'),
    sessionId: row.sessionId ?? 'session-alpha',
    creatorId: row.creatorId ?? 'parent',
    creatorLabel: row.creatorLabel ?? 'Main agent',
    operation: row.operation ?? 'write',
    source: row.source ?? 'tool',
    path: filePath,
    filename: path.basename(name),
    fileType: name.includes('.') ? name.split('.').pop().toLowerCase() : '',
    createdAt: row.createdAt ?? 1_000,
    toolCallId: row.toolCallId ?? null,
    changeSetId: null,
    metadata: row.metadata ?? {},
    exists: true,
    bytes: Buffer.byteLength(body, 'utf8'),
    lines: body.split('\n').length,
    sha256: sha(body),
  }
}

function writeManifest(projectId, rows) {
  const dir = path.join(PROJECTS, projectId)
  fs.mkdirSync(dir, { recursive: true })
  fs.writeFileSync(
    path.join(dir, 'manifest.jsonl'),
    rows.map((r) => JSON.stringify(r)).join('\n') + '\n',
    'utf8',
  )
  return dir
}

const alphaDir = path.join(PROJECTS, 'session-alpha')
fs.mkdirSync(alphaDir, { recursive: true })
const betaDir = path.join(PROJECTS, 'session-beta')
fs.mkdirSync(betaDir, { recursive: true })

const reportBody = `# Quarterly routing report

The router now prefers Sonnet for short prompts and escalates on tool depth.

More paragraphs follow.
`
const report = artifact(alphaDir, 'report.md', reportBody, { createdAt: 1_000 })
// Three more revisions of the same path — the manifest is append-only, and a
// write_file that also produced a change set lands twice.
const reportV2 = { ...report, id: 'art_v2', operation: 'edit', createdAt: 2_000 }
const reportV3 = { ...report, id: 'art_v3', operation: 'update', createdAt: 3_000 }
const subAgentDoc = artifact(alphaDir, 'artifacts/sub_19a_3.md', '# Explore agent\n\n**Agent type:** explore\n**Task:** find the router\n\n---\n\nFound it in engine/.\n', {
  createdAt: 2_500,
  creatorId: 'sub_19a_3',
  creatorLabel: 'Explore agent',
  operation: 'subagent_artifact',
  source: 'subagent',
})
const png = artifact(alphaDir, 'diagram.png', 'not-really-a-png', { createdAt: 500 })
const ghost = {
  ...artifact(alphaDir, 'deleted.md', 'gone soon\n', { createdAt: 400 }),
}
fs.unlinkSync(ghost.path)

writeManifest('session-alpha', [png, ghost, report, subAgentDoc, reportV2, reportV3])

const betaNote = artifact(betaDir, 'notes.md', '# Beta notes\n\nSomething about caching.\n', {
  createdAt: 4_000,
  sessionId: 'session-beta',
})
writeManifest('session-beta', [betaNote])

// A project dir with no manifest at all — the common case (369 of 512 on the
// real machine). Must be an ordinary skip, not an error.
fs.mkdirSync(path.join(PROJECTS, 'session-empty'), { recursive: true })

fs.writeFileSync(
  path.join(SESSIONS, '_index.json'),
  JSON.stringify([
    { id: 'session-alpha', title: 'Router work' },
    { id: 'session-beta', title: 'Cache work' },
  ]),
  'utf8',
)

// ── ingest + fold ────────────────────────────────────────────────────────

console.log('\ningest — folding append-only rows into artifacts')
{
  await ingest(true)
  const s = stats()
  check('one row per unique path', s.artifacts, 5)
  check('every manifest row counted as a revision', s.revisions, 7)
  check('both sessions indexed', s.sessions, 2)
  check('manifest-less project dirs skipped', s.projects, 2)
  check('deleted file counted as missing', s.missing, 1)
}

console.log('\nquery — the default page')
{
  const r = await query({})
  check('total is deduped artifacts', r.total, 5)
  check('newest first', r.rows[0].filename, 'notes.md')
  const rep = r.rows.find((x) => x.filename === 'report.md')
  check('revisions folded', rep.revisions, 3)
  check('newest revision wins for createdAt', rep.createdAt, 3_000)
  check('oldest revision kept as firstSeenAt', rep.firstSeenAt, 1_000)
  check('newest revision wins for operation', rep.operation, 'update')
  check('markdown title extracted at ingest', rep.title, 'Quarterly routing report')
  check(
    'excerpt extracted at ingest',
    rep.excerpt.startsWith('The router now prefers Sonnet'),
    true,
  )
  check('session title joined', rep.sessionTitle, 'Router work')
  check('existence re-stat\'ed', r.rows.every((x) => x.exists || x.filename === 'deleted.md'), true)

  const sub = r.rows.find((x) => x.filename === 'sub_19a_3.md')
  check('sub-agent attribution preserved', sub.creatorId, 'sub_19a_3')
  check('sub-agent metadata parsed', sub.agentType, 'explore')
  check('sub-agent task parsed', sub.task, 'find the router')

  const image = r.rows.find((x) => x.filename === 'diagram.png')
  check('binary file is not read for an excerpt', image.excerpt, '')
  check('binary file falls back to a filename title', image.title, 'diagram')
}

console.log('\nquery — filters')
{
  check('type filter', (await query({ types: ['md'] })).total, 4)
  check('session filter', (await query({ sessionIds: ['session-beta'] })).total, 1)
  check('creator filter', (await query({ creators: ['sub_19a_3'] })).total, 1)
  check('onlyExisting drops dead links', (await query({ onlyExisting: true })).total, 4)
  check(
    'combined filters intersect',
    (await query({ types: ['md'], sessionIds: ['session-alpha'], onlyExisting: true })).total,
    2,
  )
}

console.log('\nquery — search')
{
  check('matches a title', (await query({ text: 'quarterly' })).total, 1)
  check('matches excerpt prose', (await query({ text: 'escalates' })).total, 1)
  check('matches the filename', (await query({ text: 'notes.md' })).total, 1)
  check('matches the session title', (await query({ text: 'cache work' })).total, 1)
  // Every term must match, so an extra word narrows rather than widens.
  check('terms are ANDed', (await query({ text: 'quarterly cache' })).total, 0)
  check('search is case-insensitive', (await query({ text: 'QUARTERLY' })).total, 1)
  check('no match is an empty page, not an error', (await query({ text: 'zzzz' })).rows.length, 0)
}

console.log('\nquery — sorting and paging')
{
  const oldest = await query({ sort: 'oldest' })
  check('oldest sorts ascending', oldest.rows[0].filename, 'deleted.md')
  const byRev = await query({ sort: 'revisions' })
  check('revisions sort puts the most-edited first', byRev.rows[0].filename, 'report.md')
  const byName = await query({ sort: 'name' })
  check('name sort is alphabetical', byName.rows[0].filename, 'deleted.md')

  const page1 = await query({ limit: 2, offset: 0 })
  const page2 = await query({ limit: 2, offset: 2 })
  check('page size honoured', page1.rows.length, 2)
  check('total is unpaged', page1.total, 5)
  check('offset advances', page2.rows[0].filename !== page1.rows[0].filename, true)
  check('offset past the end is empty', (await query({ offset: 99 })).rows.length, 0)
}

console.log('\nquery — facets describe the whole index, not the page')
{
  const narrowed = await query({ text: 'quarterly' })
  check('narrowed query still reports every type', narrowed.facets.types.length, 2)
  check('narrowed query still reports every session', narrowed.facets.sessions.length, 2)
  const md = narrowed.facets.types.find((t) => t.key === 'md')
  check('type facet counts the index, not the one match', md.count, 4)
  const creators = narrowed.facets.creators.map((c) => c.id).sort()
  check('creator facet lists both writers', creators, ['parent', 'sub_19a_3'])
}

console.log('\nrevisions — full history for one path')
{
  const rows = await revisions(report.path)
  check('every manifest row returned', rows.length, 3)
  check('newest first', rows[0].createdAt, 3_000)
  check('operations preserved', rows.map((r) => r.operation), ['update', 'edit', 'write'])
  check('unknown path returns empty', (await revisions('/nope/nope.md')).length, 0)
}

console.log('\nincremental ingest')
{
  const before = Date.now()
  await ingest(true)
  check('unchanged manifests re-ingest cheaply', Date.now() - before < 200, true)

  // Append a new artifact to beta and confirm only that project re-reads.
  const extra = artifact(betaDir, 'extra.md', '# Extra\n\nAppended later.\n', {
    createdAt: 5_000,
    sessionId: 'session-beta',
  })
  fs.appendFileSync(
    path.join(PROJECTS, 'session-beta', 'manifest.jsonl'),
    JSON.stringify(extra) + '\n',
    'utf8',
  )
  await ingest(true)
  check('new artifact picked up', (await query({})).total, 6)
  check('and it sorts newest', (await query({})).rows[0].filename, 'extra.md')
  check('its excerpt was extracted', (await query({ text: 'appended later' })).total, 1)
}

console.log('\nuser edits are real revisions')
{
  const newBody = '# Quarterly routing report\n\nRewritten by the operator.\n'
  fs.writeFileSync(report.path, newBody, 'utf8')
  await recordUserEdit(report.path, {
    bytes: Buffer.byteLength(newBody, 'utf8'),
    sha256: sha(newBody),
    lines: 3,
  })
  await ingest(true)
  const rep = (await query({ text: 'quarterly' })).rows[0]
  check('edit added a revision', rep.revisions, 4)
  check('edit is attributed to the operator', rep.creatorId, 'user')
  check('operation records the edit', rep.operation, 'user_edit')
  check('excerpt re-extracted after the hash moved', rep.excerpt, 'Rewritten by the operator.')
  check('old excerpt no longer matches', (await query({ text: 'escalates' })).total, 0)
  check('the edit shows in history', (await revisions(report.path)).length, 4)
}

console.log('\ncache survives a process restart')
{
  _resetForTests()
  const r = await query({})
  check('index rebuilt from the on-disk cache', r.total, 6)
  check('titles came back with it', r.rows.some((x) => x.title === 'Quarterly routing report'), true)
}

console.log('\ndeleting a manifest drops its artifacts')
{
  fs.rmSync(path.join(PROJECTS, 'session-beta', 'manifest.jsonl'))
  await ingest(true)
  check('beta artifacts gone', (await query({})).total, 4)
  check('alpha artifacts intact', (await query({ sessionIds: ['session-alpha'] })).total, 4)
}

console.log('\nnotes route to the session that last touched the artifact')
{
  const note = await notes.createNote({
    artifactPath: report.path,
    body: 'This paragraph contradicts the benchmark.',
    anchor: { startLine: 3, endLine: 3, quote: 'Rewritten by the operator.' },
  })
  check('target resolved from the index', note.targetSessionId, 'session-alpha')
  check('session title carried along', note.targetSessionTitle, 'Router work')
  check('starts pending', note.status, 'pending')
  check('artifact hash captured for staleness', typeof note.artifactSha256, 'string')

  await notes.markDelivered(note.id, { ok: true })
  const listed = notes.listNotes(report.path)
  check('one note after the status append', listed.length, 1)
  check('last write wins', listed[0].status, 'delivered')
  check('open note counted', notes.openNoteCounts().get(report.path), 1)
  check('the anchor round-trips', listed[0].anchor.startLine, 3)
  check('the comment body round-trips', listed[0].body, 'This paragraph contradicts the benchmark.')

  await notes.resolveNote(note.id)
  check('resolved notes stop counting', notes.openNoteCounts().get(report.path), undefined)
  check('but stay readable', notes.listNotes(report.path)[0].status, 'resolved')

  const orphan = await notes.createNote({
    artifactPath: '/tmp/not-indexed.md',
    body: 'hello',
    anchor: null,
  })
  check('unknown artifact has no target', orphan.targetSessionId, '')
  check('and still records a filename', orphan.artifactFilename, 'not-indexed.md')
}

console.log('\nempty world')
{
  // A wiped projects tree must empty the index rather than keep serving rows
  // from a cache with no manifest behind it.
  fs.rmSync(PROJECTS, { recursive: true, force: true })
  await ingest(true)
  const r = await query({})
  check('no projects dir is an empty index, not a throw', r.ok, true)
  check('and reports zero artifacts', r.total, 0)

  _resetForTests()
  const again = await query({})
  check('the emptied state persisted to the cache', again.total, 0)
}

fs.rmSync(HOME, { recursive: true, force: true })
console.log(failures === 0 ? '\nAll artifact-index checks passed.\n' : `\n${failures} check(s) failed.\n`)
process.exit(failures === 0 ? 0 : 1)
