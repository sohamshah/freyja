// Tail windowing for the conversation transcript.
//
// A long session mounts every message it ever had: on a 90-sub-agent
// research session that was ~1,700 parts, 35k DOM nodes and 47k React
// fibers, and every one of them paid for layout, paint, hit testing and a
// store subscription while the newest turn streamed. The transcript now
// mounts only its tail — the newest entries up to a budget of message
// parts — and reveals older entries a chunk at a time as the reader
// scrolls up. Entries are weighed by part count because one assistant
// turn can carry 100+ tool calls while the next carries two.

/** Parts mounted on open. */
export const TRANSCRIPT_PART_BUDGET = 150

/** Parts mounted per reveal while scrolling up — smaller, so each one
 *  fits inside a frame or two. */
export const TRANSCRIPT_REVEAL_BUDGET = 60

/** Cost of mounting one transcript entry: its parts, plus the frame. */
export function entryWeight(parts: number): number {
  return 1 + Math.max(0, parts)
}

/** First index to mount so that the entries from there to the end weigh
 *  at least `budget`, or 0 when everything fits. Always mounts at least
 *  the last entry. `weights[i]` is the weight of entry i. */
export function tailStart(weights: readonly number[], budget = TRANSCRIPT_PART_BUDGET): number {
  let total = 0
  for (let i = weights.length - 1; i >= 0; i -= 1) {
    total += weights[i]
    if (total >= budget) return i
  }
  return 0
}

/** New first index after revealing one more budget's worth of entries
 *  above `start`. */
export function revealEarlier(
  weights: readonly number[],
  start: number,
  budget = TRANSCRIPT_PART_BUDGET,
): number {
  let total = 0
  for (let i = Math.min(start, weights.length) - 1; i >= 0; i -= 1) {
    total += weights[i]
    if (total >= budget) return i
  }
  return 0
}
