// Checks for src/renderer/lib/kanbanSnapshot.ts, which tells a worker's
// sub-agent card where its kanban card stands in review. The event shapes
// and actor strings are the bridge's: a worker writes as
// "<label> (<session id>)", the judge as "kanban-judge" (from the kanban
// journal in ~/.freyja/sessions). Run from the repo root:
//   npx tsx test-kanban-snapshot.mjs

import assert from 'node:assert/strict'

const { deriveKanbanCardSnapshot, deriveSnapshotFromTask } = await import('./src/renderer/lib/kanbanSnapshot.ts')

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

if (failures) {
  console.log(`\n${failures} failed`)
  process.exit(1)
}
console.log('\nall passed')
