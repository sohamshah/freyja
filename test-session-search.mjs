// Sidebar search over every session file on disk (src/main/sessionSearch.ts).
// Builds a temp sessions dir, searches it through the real worker, and checks
// the match rule, what text counts, and that only changed files are re-read.
// Run from the repo root:
//   npx tsx test-session-search.mjs
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'

const HOME = fs.mkdtempSync(path.join(os.tmpdir(), 'freyja-session-search-'))
process.env.HOME = HOME
process.env.FREYJA_HOME = path.join(HOME, '.freyja')
const SESSIONS = path.join(HOME, '.freyja', 'sessions')
const CACHE = path.join(HOME, '.freyja', 'session-search-index.json')
fs.mkdirSync(SESSIONS, { recursive: true })

const { createSessionSearcher } = await import('./src/main/sessionSearch.ts')

let failures = 0
function check(name, actual, expected) {
  const a = JSON.stringify(actual)
  const e = JSON.stringify(expected)
  if (a === e) console.log(`  ok   ${name}`)
  else {
    failures++
    console.log(`  FAIL ${name}\n         expected ${e}\n         actual   ${a}`)
  }
}

function slice(id, title, messages) {
  fs.writeFileSync(
    path.join(SESSIONS, `${id}.json`),
    JSON.stringify({ version: 1, id, title, slice: { messages } }),
  )
}
const image = 'data:image/png;base64,' + 'QUJD'.repeat(50_000)

slice('old-one', 'Planning notes', [
  { id: 'u1', role: 'user', parts: [{ type: 'text', text: 'Where did the Zebracorn-migration land?' }], attachments: [{ previewUrl: image }] },
  {
    id: 'a1',
    role: 'assistant',
    parts: [
      { type: 'thinking', text: 'secretthought' },
      { type: 'tool_call', toolCallId: 't1' },
      { type: 'text', text: 'It landed in kubernetes_staging.' },
    ],
  },
])
slice('other', 'Groceries', [{ id: 'u1', role: 'user', parts: [{ type: 'text', text: 'eggs and milk' }] }])
// The slice is what the app shows, so this sibling transcript is not searched.
fs.writeFileSync(
  path.join(SESSIONS, 'old-one.transcript.json'),
  JSON.stringify({ session_id: 'old-one', transcript: { entries: [{ message: { role: 'user', content: 'transcriptonly' } }] } }),
)
// A Slack session has only a transcript.
fs.writeFileSync(
  path.join(SESSIONS, 'freyja_slack_C1.transcript.json'),
  JSON.stringify({
    session_id: 'freyja:slack:C1',
    transcript: {
      entries: [
        { message: { role: 'user', content: [{ type: 'text', text: '<system-reminder>hiddenword</system-reminder>deploy the walrus' }] } },
        { message: { role: 'assistant', content: [{ type: 'text', text: 'Walrus deployed.' }], thinking_blocks: [{ thinking: 'secretthought' }] } },
      ],
    },
  }),
)
fs.writeFileSync(path.join(SESSIONS, '_index.json'), JSON.stringify({ version: 1, sessions: [] }))

const searcher = createSessionSearcher({ sessionsDir: SESSIONS, cacheFile: CACHE })
const ids = async (q) => (await searcher.search(q)).ids.sort()

console.log('match rule')
check('a word prefix finds a session never loaded', await ids('zebra'), ['old-one'])
check('case and _ / - split words like the sidebar', await ids('MIGRATION staging'), ['old-one'])
check('every token must match', await ids('zebracorn milk'), [])
check('a token inside a word is not a prefix', await ids('corn'), [])
check('the title counts', await ids('grocer'), ['other'])

console.log('what text counts')
check('thinking is not searched', await ids('secretthought'), [])
check('images are not searched', await ids('qujd'), [])
check('a slack transcript is searched', await ids('walrus'), ['freyja:slack:C1'])
check('system reminders are not searched', await ids('hiddenword'), [])
check('a transcript beside its slice is not searched', await ids('transcriptonly'), [])

console.log('cache')
const warm = await searcher.search('walrus')
check('a repeat query re-reads nothing', warm.reread, 0)
check('sessions indexed', warm.sessions, 3)
await new Promise((r) => setTimeout(r, 20))
slice('other', 'Groceries', [{ id: 'u1', role: 'user', parts: [{ type: 'text', text: 'zebracorn eggs' }] }])
const changed = await searcher.search('zebracorn')
check('a changed file is re-read', changed.reread, 1)
check('and its new text is found', changed.ids.sort(), ['old-one', 'other'])
fs.unlinkSync(path.join(SESSIONS, 'old-one.json'))
check('a deleted session drops out', await ids('zebracorn'), ['other'])
check('its transcript then stands in for it', await ids('transcriptonly'), ['old-one'])

// Concurrent queries: only the newest waiting one is answered.
const [a, b, c] = await Promise.all([searcher.search('eggs'), searcher.search('walrus'), searcher.search('zebracorn')])
check('a query in flight is answered', a.ids, ['other'])
check('an older waiting query is superseded', [b.ok, b.error], [false, 'superseded'])
check('the newest waiting query is answered', c.ids, ['other'])
await searcher.close()

// The worker saved its entries right after the first reply (then at most
// once a minute). A new process re-reads only the files that changed since:
// the edited slice and the transcript that lost its slice, not the Slack one.
check('the cache was saved', fs.existsSync(CACHE), true)
const restarted = createSessionSearcher({ sessionsDir: SESSIONS, cacheFile: CACHE })
const cold = await restarted.search('walrus')
check('after a restart only changed files are re-read', [cold.reread, cold.ids], [2, ['freyja:slack:C1']])
await restarted.close()

fs.rmSync(HOME, { recursive: true, force: true })
if (failures > 0) {
  console.log(`\n${failures} failure(s)`)
  process.exit(1)
}
console.log('\nall passed')
