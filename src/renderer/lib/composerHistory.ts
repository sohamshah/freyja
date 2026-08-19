// Shell-style history recall for the composer.
//
// The rule the arrows follow: they only leave the text once the caret is
// already parked at the edge they're heading for. ↑ recalls an older message
// only from offset 0, ↓ a newer one only from the very end. So a multiline
// draft is fully navigable with the arrows first, and stepping out of it
// takes one deliberate extra press from the top (or bottom).
//
// After each recall the caret lands on the edge the operator is travelling
// toward — start when going back, end when coming forward — so a run of ↑
// (or ↓) keeps stepping through history instead of stalling inside whatever
// it just pulled up. Reversing direction costs the one press it takes to
// cross the recalled text, which is the same rule, not an exception to it.
//
// Nothing is ever dropped: the draft in progress is stashed on the way in
// and handed back when ↓ walks past the newest message.

/** Where the operator is in history. `index` is 0-based into a newest-first
 *  list; null means they're composing their own text. */
export interface HistoryCursor {
  index: number
  total: number
}

export interface HistoryStepInput {
  key: 'ArrowUp' | 'ArrowDown'
  /** Caret offset (`selectionStart`). */
  caret: number
  /** `selectionEnd` — a ranged selection never triggers recall. */
  selectionEnd: number
  /** Current composer contents. */
  value: string
  /** Sent messages, newest first. */
  entries: string[]
  /** Current position, or null while composing. */
  cursor: HistoryCursor | null
  /** The draft stashed when history was entered. */
  stashedDraft: string
}

export interface HistoryStep {
  /** Text to put in the composer. */
  text: string
  /** Where to leave the caret. */
  caretAt: 'start' | 'end'
  /** New cursor — null when we've handed the draft back. */
  cursor: HistoryCursor | null
  /** Set when this step enters history and the caller should stash the
   *  current value first. */
  stash: boolean
}

/** Cheap precondition for `stepHistory`: the arrows can only reach history
 *  from an edge of the text, with nothing selected. Callers use this to skip
 *  building the history list on the many presses that just move the caret. */
export function couldRecall(caret: number, selectionEnd: number, length: number): boolean {
  return caret === selectionEnd && (caret === 0 || caret === length)
}

/** Returns the recall to perform, or null to let the browser move the
 *  caret normally. */
export function stepHistory(input: HistoryStepInput): HistoryStep | null {
  const { key, caret, selectionEnd, value, entries, cursor, stashedDraft } = input
  if (!couldRecall(caret, selectionEnd, value.length)) return null

  if (key === 'ArrowUp') {
    if (caret !== 0) return null
    if (entries.length === 0) return null
    // Entering history starts at the newest; already inside, walk further
    // back and hold at the oldest rather than wrapping around.
    const index = cursor === null ? 0 : Math.min(cursor.index + 1, entries.length - 1)
    return {
      text: entries[index],
      caretAt: 'start',
      cursor: { index, total: entries.length },
      stash: cursor === null,
    }
  }

  // ArrowDown only means anything once we're inside history — below the
  // newest message there is nothing but the operator's own draft.
  if (caret !== value.length || cursor === null) return null
  const index = Math.min(cursor.index - 1, entries.length - 1)
  if (index < 0) {
    // Past the newest entry, or the history changed under us. Either way
    // the draft comes back.
    return { text: stashedDraft, caretAt: 'end', cursor: null, stash: false }
  }
  return {
    text: entries[index],
    caretAt: 'end',
    cursor: { index, total: entries.length },
    stash: false,
  }
}
