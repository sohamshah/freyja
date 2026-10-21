# Seams: how tool calls show in the conversation

Tool calls are part of the story, not boxes beside it. While the agent works, each step is a quiet line (direction A). When the agent starts its next paragraph, the steps between the two paragraphs fold into a seam (direction B): one drawn line beside a short summary. The design was explored in a mock (the "Seams" artifact) before it was built.

## While the agent works

- A tool call is one line: a small ring, then the step in plain words. Bash's `summary`, a computer-use `goal`, a file name with its folder dimmed. No tool name, no card, no colour.
- A running step turns its ring and shimmers its words. A running command shows faintly under its line.
- Two to four calls that ran together are joined on the left. The first line says "together".
- Five or more calls at once are one line with a small fan of strands for its mark. The calls run as words underneath; each opens its own result.
- Helpers (sub-agents) hang from their line on a ribbon of fine parallel strands. While a helper works, its strand is basted (a light dashed thread) and the helper grows its own small seam beside the step it is on. When it comes back, a solid thread is sewn up from its line, its working line folds away, and its reply takes the place of its task. A helper that failed stays basted in red, with a small cut across its branch.

Click any line to open what it did and what came back, under the same hairline rule a thought uses.

## The seam

The seam is drawn left to right in the order things happened:

| Shape | Meaning |
|---|---|
| A long, low rise in blue | Thinking. A longer thought makes a longer rise. |
| A ripple that rises | A read, a search or a command. A slow step makes a bigger ripple. |
| A ripple that dips | An edit, where the pen writes. |
| Two frayed red ends, a little out of line | A step that failed. The line carries on after it. |
| A spindle, one strand per call | Calls that ran together. A failed call snaps its strand. |
| A spindle of rippling strands | Helpers. A helper still working keeps a blue strand until it reports back. |

Beside the seam: what the work did ("ran 2 commands and edited main.ts"), what went wrong (kept apart so a narrow pane never cuts it off), the time, and the lines changed. Hover the seam to name a step. Click it to unfold the lines.

## Motion

- Fold: the lines dissolve and each mark flies to its place on the line. The seam then draws itself left to right, and each mark melts into its stretch of thread as the line reaches it. The summary inks in behind.
- Unfold: the marks lift off the seam and drop back into their lines.
- No glowing dots and no pulses. A live mark is plain blue; a running ring turns.

## Code

- `src/renderer/lib/seam.ts`: the seam's geometry. Units in, SVG paths out.
- `src/renderer/components/seam/`: `WorkStretch` (a stretch and its fold), `Steps` (quiet lines, groups, many at once), `Helpers` (the ribbon), `choreo.ts` (the fold's motion), `stepWords.ts` (plain words and the summary), `helperDigest.ts` (a helper's steps, read live from its child session), `source.ts` (the block builder, and which session a view reads).
- `src/renderer/styles/work.css`: the styles.
- `test-seams.mjs`: run with `npx tsx test-seams.mjs` after `npm i --no-save jsdom`.

The store gives a call its predecessor's `groupId` only while that call is still running, so calls from separate responses no longer read as parallel. Saved transcripts from before that change are split by start time instead (calls that started within 2.5 seconds of each other).
