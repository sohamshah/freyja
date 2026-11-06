import type { SystemEventRecord } from '../state/store'

/** Snapshot of a kanban card's current state, derived from the running
 *  fold over `kanban_*` system events. Used by the subagent card to
 *  decide whether to show "awaiting review" / "verified" / "rejected"
 *  states instead of the bare worker `done`. */
export interface KanbanCardSnapshot {
  status: string
  requiresVerification: boolean
  /** True while the card waits on the judge: `review`, or the legacy
   *  `done_unverified`. */
  awaitingReview: boolean
  /** When the card's latest move was the reviewer's verdict, the
   *  reviewer's actor string. Used to render the verifier byline. */
  verifierActor: string | null
  /** True when that verdict sent the card back (to rework, to the
   *  ready queue, or blocked at the review cap) instead of sealing it. */
  rejected: boolean
  /** Verifier's most recent rejection feedback, if any. */
  rejectionFeedback: string | null
}

/** Moves that finish a card (the bridge's FINISH_STATUSES). */
const FINISH_STATUSES = new Set(['review', 'done_unverified', 'done'])

/** The bridge's judge writes as `kanban-judge`; legacy verifier
 *  sub-agents wrote with a `verify` label. */
function isReviewerActor(actor: string): boolean {
  return actor === 'kanban-judge' || actor.includes('verify')
}

export function deriveKanbanCardSnapshot(
  events: SystemEventRecord[],
  taskId: string,
): KanbanCardSnapshot | null {
  let task: Record<string, unknown> | null = null
  for (const event of events) {
    if (!event.subtype.startsWith('kanban_')) continue
    const details = event.details as Record<string, unknown> | undefined
    const eventTask = details?.task as Record<string, unknown> | undefined
    if (!eventTask) continue
    if (String(eventTask.id ?? '') !== taskId) continue
    task = eventTask
  }
  return task ? deriveSnapshotFromTask(task) : null
}

export function deriveSnapshotFromTask(task: Record<string, unknown>): KanbanCardSnapshot {
  let verifierActor: string | null = null
  let rejected = false
  let rejectionFeedback: string | null = null
  // The judge leaves its reason in the card summary. It is still the
  // judge's when the verdict wrote it and no later move wrote over it.
  let summaryRewritten = false
  let summaryIsVerdict = false
  // Walk the card's status moves newest-first. The reviewer's latest
  // verdict stands until someone else finishes the card, as the worker
  // does when it sends the card back to review. Other moves, such as
  // the woken worker marking the card running, leave it in place.
  const events = Array.isArray(task.events) ? task.events : []
  for (const inner of events.slice().reverse()) {
    if (!inner || typeof inner !== 'object') continue
    const innerDetails = (inner as Record<string, unknown>).details as
      | Record<string, unknown>
      | undefined
    const innerStatus = String(innerDetails?.status ?? '').toLowerCase()
    const actor = String((inner as Record<string, unknown>).actor ?? '').toLowerCase()
    if (innerStatus && isReviewerActor(actor)) {
      verifierActor = String((inner as Record<string, unknown>).actor)
      rejected = innerStatus !== 'done'
      summaryIsVerdict = Boolean(innerDetails?.summary) && !summaryRewritten
      break
    }
    if (FINISH_STATUSES.has(innerStatus)) break
    if (innerDetails?.summary) summaryRewritten = true
  }
  if (rejected && summaryIsVerdict) {
    rejectionFeedback = String(task.summary ?? '') || null
  } else if (rejected) {
    // Legacy verifiers left a comment when bouncing the card.
    const comments = Array.isArray(task.comments) ? task.comments : []
    for (const c of comments.slice().reverse()) {
      if (!c || typeof c !== 'object') continue
      const author = String((c as Record<string, unknown>).author ?? '').toLowerCase()
      if (!isReviewerActor(author)) continue
      rejectionFeedback = String((c as Record<string, unknown>).body ?? '') || null
      break
    }
  }
  const status = String(task.status ?? 'ready')
  return {
    status,
    requiresVerification: Boolean(task.requiresVerification),
    awaitingReview: status === 'review' || status === 'done_unverified',
    verifierActor,
    rejected,
    rejectionFeedback,
  }
}
