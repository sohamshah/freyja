// Checks for src/renderer/lib/jevLog.ts, the parser behind the jev run view.
// Run from the repo root:
//   npx tsx test-jev-log.mjs

import assert from 'node:assert/strict'

const { parseJevLog, parseJevResult, looksLikeJevLog, unrepr, shortUrl } = await import('./src/renderer/lib/jevLog.ts')

let failures = 0
function check(name, fn) {
  try {
    fn()
    console.log(`ok   ${name}`)
  } catch (err) {
    failures++
    console.log(`FAIL ${name}\n     ${err.message.split('\n').join('\n     ')}`)
  }
}

// The shape of a real DOM run (an older one: `ax` read label, no result block).
const OLD = `target: Arc (company.thebrowser.Browser, pid 62005)
step 1: click [32] AXButton 'Open task (Add missing memo Acme Co · $11.19 Add)' (op 0.91, target 0.63, jev 200 ms, ax 141 ms, 77 rows)
  → no change: no visible change
step 2: click [32] AXButton 'Open task (Add missing memo Acme Co · $11.19 Add)' (op 0.65, target 0.54, jev 223 ms, ax 159 ms, 77 rows)
  → no change: no visible change
  → LLM: replanning (this action repeated with no effect: click: click [32] AXButton 'Open task (Add missing memo Acme Co · $11.19 Add)' (3 times in a row))
  sub-goal: Press escape to dismiss the open search/'Jump to' overlay, then click [51] 'Close details pane'.
step 3: key escape (op 0.97, target 0.91, jev 211 ms, ax 156 ms, 77 rows)
  → no change: no visible change
step 4: click [51] AXButton 'Close details pane' (op 0.96, target 0.96, jev 179 ms, ax 136 ms, 77 rows)
  → changed: window 'Tasks (https://example.com/p/inbox?entityId=abc%3D&task_id=xyz)' -> 'Tasks (https://example.com/p/inbox)'; elements +46/-77; screen text changed
step 5: need_help (op 0.74, target 0.65, jev 176 ms, ax 150 ms, 46 rows) [click_target none or unknown]
  sub-goal complete (p=0.78); back to the main goal
  → LLM: replanning (need_help: click_target none or unknown) with screenshot
  sub-goal: Click the 'Close' button [1].
step 6: type 'LLM API usage' into [60] 'Memo' (op 0.93, target 1.00, jev 235 ms, ax 144 ms, 77 rows)
  → changed: 'Memo': '' -> 'LLM API usage'; new text: Unsaved
step 7: type (text from LLM) into [61] "Don't \\"quote\\" me" (op 0.90, target 0.99, jev 201 ms, ax 140 ms, 77 rows)
  → refused: the field is read-only
step 8: done (op 0.96, target 0.00, jev 212 ms, ax 133 ms, 77 rows)
  → LLM: verifying end state
`

check('steps parse with their actions, numbers and outcomes', () => {
  const { entries, result } = parseJevLog(OLD)
  assert.equal(result, undefined)
  const steps = entries.filter((e) => e.type === 'step')
  assert.deepEqual(steps.map((s) => s.n), [1, 2, 3, 4, 5, 6, 7, 8])
  const [s1, , s3, s4, s5, s6, s7, s8] = steps
  assert.deepEqual(
    { op: s1.op, index: s1.index, role: s1.role, label: s1.label },
    { op: 'click', index: 32, role: 'AXButton', label: 'Open task (Add missing memo Acme Co · $11.19 Add)' },
  )
  assert.deepEqual(s1.conf, { op: 0.91, target: 0.63 })
  assert.deepEqual(s1.ms, { jev: 200, read: 141 })
  assert.equal(s1.rows, 77)
  assert.deepEqual(s1.outcome, { kind: 'unchanged', parts: ['no visible change'] })
  assert.deepEqual({ op: s3.op, arg: s3.arg }, { op: 'key', arg: 'escape' })
  assert.equal(s4.outcome.kind, 'changed')
  assert.equal(s4.outcome.parts.length, 3)
  assert.match(s4.outcome.parts[0], /^window 'Tasks \(https:\/\/example\.com/)
  assert.equal(s5.op, 'need_help')
  assert.equal(s5.reasons, 'click_target none or unknown')
  assert.equal(s5.outcome, undefined, 'need_help acts on nothing')
  assert.deepEqual({ op: s6.op, text: s6.text, index: s6.index, label: s6.label }, { op: 'type', text: 'LLM API usage', index: 60, label: 'Memo' })
  assert.equal(s7.text, null, 'text the LLM composed')
  assert.equal(s7.label, 'Don\'t "quote" me')
  assert.deepEqual(s7.outcome, { kind: 'refused', text: 'the field is read-only' })
  assert.equal(s8.op, 'done')
})

check('LLM turns, sub-goals and the target come through in order', () => {
  const kinds = parseJevLog(OLD).entries.map((e) => (e.type === 'llm' ? `llm:${e.what}` : e.type))
  assert.deepEqual(kinds, [
    'target', 'step', 'step', 'llm:replan', 'subgoal', 'step', 'step', 'step',
    'subgoal_done', 'llm:replan', 'subgoal', 'step', 'step', 'step', 'llm:verify',
  ])
  const { entries } = parseJevLog(OLD)
  assert.deepEqual(entries[0], { type: 'target', app: 'Arc', bundle: 'company.thebrowser.Browser', pid: 62005 })
  const replans = entries.filter((e) => e.type === 'llm' && e.what === 'replan')
  assert.match(replans[0].reason, /\(3 times in a row\)$/, 'nested parentheses stay in the reason')
  assert.equal(replans[0].screenshot, false)
  assert.equal(replans[1].screenshot, true)
  assert.equal(entries.find((e) => e.type === 'subgoal_done').p, 0.78)
})

const NEW_SINGLE = `launching Calculator
target: Calculator (com.apple.calculator, pid 501)
step 1: click [4] AXButton '7' (op 0.99, target 0.98, jev 190 ms, read 40 ms, 31 rows)
  → changed: new text: 7
step 2: done (op 0.97, target 0.00, jev 180 ms, read 38 ms, 31 rows)
  → LLM: verifying end state
[result]
The display shows 7.

[jev_computer_use] status=done surface=ax steps=1 jev_calls=2 jev_median_ms=185 llm_calls=1 llm_ms=900 elapsed=3.2s log=/Users/me/Library/Application Support/runs/r1.jsonl
`

check('a new run: read label, launch line and the result block', () => {
  const { entries, result } = parseJevLog(NEW_SINGLE)
  assert.equal(entries[0].type, 'launch')
  assert.deepEqual(entries.find((e) => e.type === 'step').ms, { jev: 190, read: 40 })
  assert.equal(result.status, 'done')
  assert.equal(result.summary, 'The display shows 7.')
  assert.equal(result.footer.steps, '1')
  assert.equal(result.footer.elapsed, '3.2s')
  assert.equal(result.footer.log, '/Users/me/Library/Application Support/runs/r1.jsonl', 'a log path with spaces stays whole')
  assert.equal(result.handoff, undefined)
})

const BLOCKED = `It could not find the Save button.

[jev_computer_use] status=blocked surface=dom steps=9 jev_calls=9 jev_median_ms=200 llm_calls=2 llm_ms=3000 elapsed=21.0s log=/x.jsonl
page: Settings — https://example.com/settings
pending_action: click [3] AXButton 'Delete'

[handoff]
surface: dom
elements (first 2 of 9):
1 AXButton 'Cancel'
2 AXButton 'Delete'
screen text: Settings | Danger zone
next: read the summary for what stopped it`

check('a blocked result: page, pending action, handoff and next step', () => {
  const r = parseJevResult(BLOCKED)
  assert.equal(r.status, 'blocked')
  assert.equal(r.summary, 'It could not find the Save button.')
  assert.equal(r.page, 'Settings — https://example.com/settings')
  assert.equal(r.pending, "click [3] AXButton 'Delete'")
  assert.ok(r.handoff.startsWith('[handoff]\nsurface: dom'))
  assert.ok(!r.handoff.includes('next:'), 'next is pulled out of the handoff')
  assert.equal(r.next, 'read the summary for what stopped it')
})

const ITEMS = `item 1 of 3 (#0): Ada Lovelace
target: Arc (company.thebrowser.Browser, pid 7)
opening https://en.wikipedia.org in a new tab
step 1: done (op 0.9, target 0.0, jev 200 ms, read 90 ms, 120 rows)
item 3 of 3 (#2): Grace Hopper
step 1: done (op 0.9, target 0.0, jev 200 ms, read 90 ms, 120 rows)
[result]
# | item | status | steps | secs | evidence
0 | Ada Lovelace | done | 4 | 9.1 | Born 1815 // screen: Ada Lovelace / Wikipedia
1 | Alan Turing | skipped | 0 | 0.0 | in skip_items (already done)
2 | Grace Hopper | blocked | 6 | 12.0 | No search box

[jev_computer_use] status=partial surface=dom steps=10 jev_calls=10 jev_median_ms=200 llm_calls=0 llm_ms=0 elapsed=21.1s log=/x.jsonl
page: Grace Hopper — Wikipedia

[handoff] item 2
surface: dom
elements: none exposed
screen text: (none)
next: fix the goal or the page, then call again with skip_items=[0, 1]`

check('an items run: item headers, the table and the last failed item', () => {
  const { entries, result } = parseJevLog(ITEMS)
  const items = entries.filter((e) => e.type === 'item')
  assert.deepEqual(items.map((i) => [i.n, i.total, i.index, i.text]), [[1, 3, 0, 'Ada Lovelace'], [3, 3, 2, 'Grace Hopper']])
  assert.deepEqual(entries.find((e) => e.type === 'open'), { type: 'open', url: 'https://en.wikipedia.org' })
  assert.equal(result.status, 'partial')
  assert.deepEqual(result.table.header, ['#', 'item', 'status', 'steps', 'secs', 'evidence'])
  assert.equal(result.table.rows.length, 3)
  assert.deepEqual(result.table.rows[2], ['2', 'Grace Hopper', 'blocked', '6', '12.0', 'No search box'])
  assert.equal(result.summary, '')
  assert.ok(result.handoff.startsWith('[handoff] item 2'))
  assert.equal(result.next, 'fix the goal or the page, then call again with skip_items=[0, 1]')
})

check('cancelled and failed runs still end with a result', () => {
  const c = parseJevLog('step 1: wait (op 0.5, target 0.0, jev 1 ms, read 1 ms, 1 rows)\n[result]\nCancelled.\n\n[jev_computer_use] status=cancelled\n').result
  assert.deepEqual({ status: c.status, summary: c.summary }, { status: 'cancelled', summary: 'Cancelled.' })
  const f = parseJevResult('jev_computer_use failed: boom\n\n[jev_computer_use] status=error')
  assert.deepEqual({ status: f.status, summary: f.summary }, { status: 'error', summary: 'jev_computer_use failed: boom' })
})

check('unknown lines become notes; an outcome with no step is a note', () => {
  const { entries } = parseJevLog('target: X (a.b, pid 1)\nsomething new\n  → changed: new text: 1\n')
  assert.deepEqual(entries.slice(1), [{ type: 'note', text: 'something new' }, { type: 'note', text: 'changed: new text: 1' }])
})

check('stops are their own entries', () => {
  const { entries } = parseJevLog(
    "step 1: click [3] AXButton 'Delete' (op 0.9, target 0.9, jev 1 ms, read 1 ms, 3 rows)\n  irreversible control 'Delete'; stopping for confirmation\n",
  )
  assert.deepEqual(entries[1], { type: 'stop', text: "irreversible control 'Delete'; stopping for confirmation" })
})

check('text that is not a run log is not taken for one', () => {
  assert.ok(looksLikeJevLog(OLD) && looksLikeJevLog(NEW_SINGLE) && looksLikeJevLog(ITEMS))
  assert.ok(!looksLikeJevLog('Here is the target: a plan.'))
  assert.equal(parseJevResult('No footer here.'), null)
})

check('repr strings and URLs', () => {
  assert.equal(unrepr("'it\\'s'"), "it's")
  assert.equal(unrepr("'tab\\there \\u2026 \\x41'"), 'tab\there … A')
  assert.equal(unrepr('plain'), 'plain')
  assert.equal(shortUrl('https://www.example.com/p/inbox?entityId=abc'), 'example.com/p/inbox?…')
  assert.equal(shortUrl('https://example.com/' + 'a'.repeat(80), 20).length, 20)
})

// Through the real store: the log reaches the run's pane as one text part
// the view can parse, and a run that ended short of done (outcome `stuck`:
// blocked, waiting for confirmation) is stopped, not failed.
globalThis.window = globalThis.window ?? {}
window.harness = {
  sendCommand: async () => {},
  sessionSave: async () => ({ ok: true }),
  sessionIndexSave: async () => ({ ok: true }),
  sessionLoad: async () => ({ ok: false }),
}
const { useHarness } = await import('./src/renderer/state/store.ts')
const h = () => useHarness.getState()
h().handleEvent({ type: 'ready', sessionId: 'session-local', mode: 'live', capabilities: {} })
h().handleEvent({
  type: 'session_spawned', sessionId: 'jev_t_1', parentSessionId: 'session-local', title: 'jev: t',
  model: 'jev-1.13.0', reasoningLevel: 'off', task: 'g', mode: 'background', agentType: 'computer',
  workspace: '/tmp', createdAt: Date.now(),
})
h().handleEvent({ type: 'computer_session_start', sessionId: 'jev_t_1', parentSessionId: 'session-local', goal: 'g', targetApp: 'Calculator', maxSteps: 40 })
h().handleEvent({ type: 'turn_start', sessionId: 'jev_t_1', turnId: 'turn-1' })
for (const line of NEW_SINGLE.split('\n').filter(Boolean)) {
  h().handleEvent({ type: 'text_delta', sessionId: 'jev_t_1', text: `${line}\n` })
}
h().handleEvent({ type: 'turn_complete', sessionId: 'jev_t_1', turnId: 'turn-1', success: true })
h().handleEvent({ type: 'computer_session_end', sessionId: 'jev_t_1', outcome: 'stuck', summary: 'The display shows 7.' })

check('the store keeps the log whole and calls a stuck run stopped', () => {
  const msgs = h().sessionArchive.jev_t_1.messages
  const parts = msgs[msgs.length - 1].parts.filter((p) => p.type === 'text')
  assert.equal(parts.length, 1)
  assert.ok(looksLikeJevLog(parts[0].text))
  const log = parseJevLog(parts[0].text)
  assert.equal(log.entries.filter((e) => e.type === 'step').length, 2)
  assert.equal(log.result.status, 'done')
  assert.equal(h().computerSessions.jev_t_1.status, 'stopped')
})

if (failures) {
  console.log(`\n${failures} failed`)
  process.exit(1)
}
console.log('\nall passed')
