// Checks for src/renderer/lib/kanbanSnapshot.ts, which tells a worker's
// sub-agent card where its kanban card stands in review, and for the chat
// lines Conversation.tsx writes for the review loop. The event shapes
// and actor strings are the bridge's: a worker writes as
// "<label> (<session id>)", the judge as "kanban-judge" (from the kanban
// journal in ~/.freyja/sessions). Run from the repo root:
//   npx tsx test-kanban-snapshot.mjs

import assert from 'node:assert/strict'

const { deriveKanbanCardSnapshot, deriveSnapshotFromTask } = await import('./src/renderer/lib/kanbanSnapshot.ts')
const { formatNarratorEvent } = await import('./src/renderer/components/Conversation.tsx')

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

const WORKER = 'code card_002 (sub_1a17bd29613_3)'
const JUDGE = 'kanban-judge'
const ev = (actor, details = {}, kind = 'updated') => ({ kind, actor, message: `Updated card_002`, timestamp: 0, details })
const card = (status, events) => ({ id: 'card_002', title: 'Write the parser', status, events, comments: [] })
const state = (task) => {
  const s = deriveSnapshotFromTask(task)
  return { awaitingReview: s.awaitingReview, verifierActor: s.verifierActor, rejected: s.rejected }
}

const claimed = [
  ev('parent (session-test)', {}, 'created'),
  ev(WORKER, { status: 'running', workerSessionId: 'sub_1a17bd29613_3', comment: true }),
  ev(WORKER, { comment: true }),
]
const firstReview = [
  ...claimed,
  ev(WORKER, { status: 'review', reviewIteration: 1, workerSessionId: 'sub_1a17bd29613_3' }),
  ev('kanban-dispatcher', { judgeSessionId: 'sub_1a17bd8146e_4' }),
]
// The judge sends it back; the woken worker marks the card running again.
const reworking = [
  ...firstReview,
  ev(JUDGE, { status: 'running' }),
  ev(WORKER, { status: 'running', comment: true }),
  ev(WORKER, { comment: true }),
]
const secondReview = [...reworking, ev(WORKER, { status: 'review', reviewIteration: 2 })]

check('a finished worker whose card is in review is awaiting review', () => {
  assert.deepEqual(state(card('review', firstReview)), {
    awaitingReview: true, verifierActor: null, rejected: false,
  })
})

check('a card the judge sealed names the judge, so the worker reads as verified', () => {
  assert.deepEqual(state(card('done', [...firstReview, ev(JUDGE, { status: 'done' })])), {
    awaitingReview: false, verifierActor: JUDGE, rejected: false,
  })
})

check('a card the judge sent back reads as rejected while the worker reworks it', () => {
  assert.deepEqual(state(card('running', reworking)), {
    awaitingReview: false, verifierActor: JUDGE, rejected: true,
  })
})

check('reworked and back in review, the card is awaiting review again, not rejected', () => {
  assert.deepEqual(state(card('review', secondReview)), {
    awaitingReview: true, verifierActor: null, rejected: false,
  })
})

check('the third failed review blocks the card, and that reads as rejected', () => {
  const events = [
    ...secondReview,
    ev(JUDGE, { status: 'running' }),
    ev(WORKER, { status: 'running', comment: true }),
    ev(WORKER, { status: 'review', reviewIteration: 3 }),
    ev(JUDGE, { status: 'blocked' }),
  ]
  assert.deepEqual(state(card('blocked', events)), {
    awaitingReview: false, verifierActor: JUDGE, rejected: true,
  })
})

check('a card the judge requeued for a fresh worker reads as rejected', () => {
  assert.deepEqual(state(card('ready', [...firstReview, ev(JUDGE, { status: 'ready' })])), {
    awaitingReview: false, verifierActor: JUDGE, rejected: true,
  })
})

check('a legacy card the parent finished after a rejection is no longer rejected', () => {
  const events = [...reworking, ev('parent (session-test)', { status: 'done', comment: true })]
  assert.deepEqual(state(card('done', events)), {
    awaitingReview: false, verifierActor: null, rejected: false,
  })
})

check("the judge's promotion of a child card is not a verdict on the child", () => {
  const child = [
    ev('parent (session-test)', {}, 'created'),
    ev(JUDGE, {}, 'promoted'),
    ev(WORKER, { status: 'running', comment: true }),
  ]
  assert.deepEqual(state(card('running', child)), {
    awaitingReview: false, verifierActor: null, rejected: false,
  })
})

check('a legacy done_unverified card is awaiting review', () => {
  assert.equal(deriveSnapshotFromTask(card('done_unverified', claimed)).awaitingReview, true)
})

check('a legacy verify sub-agent that sealed the card still reads as the verifier', () => {
  const verifier = 'verify card_002 (sub_19e1aee19bc_26)'
  const events = [...claimed, ev(WORKER, { status: 'done_unverified' }), ev(verifier, { status: 'done' })]
  assert.deepEqual(state(card('done', events)), {
    awaitingReview: false, verifierActor: verifier, rejected: false,
  })
})

// The judge leaves its reason in the card summary, not in a comment.
const REASON = 'Rework iteration 1: the README never documents the --redact flag.'
const judgeRejected = [...firstReview, ev(JUDGE, { status: 'running', summary: true })]

check("a judge rejection shows the judge's reason from the card summary", () => {
  const events = [...judgeRejected, ev(WORKER, { status: 'running', comment: true })]
  const snapshot = deriveSnapshotFromTask({ ...card('running', events), summary: REASON })
  assert.equal(snapshot.rejected, true)
  assert.equal(snapshot.rejectionFeedback, REASON)
})

check("a summary the worker wrote after the rejection is not shown as the judge's", () => {
  const events = [...judgeRejected, ev(WORKER, { summary: true })]
  const snapshot = deriveSnapshotFromTask({ ...card('running', events), summary: 'Halfway there.' })
  assert.equal(snapshot.rejected, true)
  assert.equal(snapshot.rejectionFeedback, null)
})

check("a legacy verify sub-agent's rejection still shows its comment", () => {
  const verifier = 'verify card_002 (sub_19e1aee19bc_26)'
  const events = [...claimed, ev(WORKER, { status: 'done_unverified' }), ev(verifier, { status: 'running', comment: true })]
  const comments = [{ author: verifier, body: 'The parser drops trailing commas.', timestamp: 0 }]
  const snapshot = deriveSnapshotFromTask({ ...card('running', events), comments, summary: 'Parser written.' })
  assert.equal(snapshot.rejectionFeedback, 'The parser drops trailing commas.')
})

check('the snapshot follows the latest kanban event that carries this card', () => {
  const sys = (subtype, task) => ({ id: subtype, subtype, message: '', at: 0, details: { task } })
  const events = [
    sys('kanban_complete', card('review', firstReview)),
    sys('kanban_update', { ...card('ready', []), id: 'card_003' }),
    sys('kanban_update', card('done', [...firstReview, ev(JUDGE, { status: 'done' })])),
    { id: 'tick', subtype: 'kanban_tick', message: '', at: 0, details: {} },
  ]
  const snapshot = deriveKanbanCardSnapshot(events, 'card_002')
  assert.equal(snapshot.status, 'done')
  assert.equal(snapshot.verifierActor, JUDGE)
  assert.equal(deriveKanbanCardSnapshot(events, 'card_009'), null)
})

check('a replay that carries the card wins over an older event that carried it alone', () => {
  const events = [
    { id: 'c', subtype: 'kanban_complete', message: '', at: 0, details: { task: card('review', firstReview) } },
    {
      id: 'r', subtype: 'kanban_replay', message: '', at: 1,
      details: { tasks: [card('done', [...firstReview, ev(JUDGE, { status: 'done' })])] },
    },
  ]
  const snapshot = deriveKanbanCardSnapshot(events, 'card_002')
  assert.equal(snapshot.status, 'done')
  assert.equal(snapshot.verifierActor, JUDGE)
})

check("a newer list that sends the card without history keeps the older event's history", () => {
  const { events: _history, comments: _comments, ...listed } = card('done', [])
  const events = [
    { id: 'u', subtype: 'kanban_update', message: '', at: 0, details: { task: card('done', [...firstReview, ev(JUDGE, { status: 'done' })]) } },
    { id: 'l', subtype: 'kanban_list', message: '', at: 1, details: { tasks: [listed] } },
  ]
  const snapshot = deriveKanbanCardSnapshot(events, 'card_002')
  assert.equal(snapshot.status, 'done')
  assert.equal(snapshot.verifierActor, JUDGE)
})

// The bridge's kanban_blocked and kanban_rework_started details
// (freyja_bridge.py, _kanban_tick and _handle_kanban_verdict). The cap is
// three judge reviews.
const narrate = (subtype, details) => formatNarratorEvent({ id: subtype, subtype, message: '', at: 0, details })

check('a card parked with nothing to review says so, not that reworks ran out', () => {
  const line = narrate('kanban_blocked', { cardId: 'card_002', reason: 'empty_review_card', chatVisible: true })
  assert.equal(line, 'blocked card_002 — nothing to review')
})

check('a card blocked at the review cap names the cap of three reviews', () => {
  const line = narrate('kanban_blocked', { cardId: 'card_002', reviewIteration: 3, verdict: {}, chatVisible: true })
  assert.equal(line, 'blocked card_002 after 3 failed reviews')
})

check('a rework counts its review against the cap of three', () => {
  const line = narrate('kanban_rework_started', { cardId: 'card_002', reviewIteration: 2, chatVisible: true })
  assert.equal(line, 'reworking card_002 · iter 2/3')
})

if (failures) {
  console.log(`\n${failures} failed`)
  process.exit(1)
}
console.log('\nall passed')
