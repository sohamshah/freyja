# Seams: how tool calls show in the conversation

Tool calls are part of the story, not boxes beside it. While the agent works, each step is a quiet line (direction A). When the agent starts its next paragraph, the steps between the two paragraphs fold into a seam (direction B): one drawn line beside a short summary. The design was explored in a mock (the "Seams" artifact) before it was built.

## While the agent works

- A tool call is one line: a small mark that shows what kind of step it is (see "Marks" below), then the step in plain words. Bash's `summary`, a computer-use `goal`, a file name with its folder dimmed. No tool name, no card, no colour.
- While a step runs, its mark is a turning ring and its words shimmer. A running command shows faintly under its line. When the step lands, the ring shrinks away and the step's drawing settles in.
- Two to four calls that ran together are joined mark to mark by a hairline. The hairline stops just short of each mark, so it never runs through one. The first line says "together".
- Five or more calls at once are one line with a small fan of strands for its mark. The calls run as words underneath; each opens its own result.
- Helpers (sub-agents) hang from their line on a ribbon of fine parallel strands. While a helper works, its strand is basted (a light dashed thread), its mark is a turning ring, and the helper grows its own small seam beside the step it is on. When it comes back, a solid thread is sewn up from its line, its mark becomes a letter, its working line folds away, and its reply takes the place of its task. A helper that failed stays basted in red, with a small cut across its branch and a red letter.

Click any line to open what it did and what came back, under the same hairline rule a thought uses. The rule runs down through the middle of the marks. Inside a group, an opened line has no rule of its own: the join runs down beside its detail, tinted blue, and fades back to grey before the next mark.

## Marks

Each mark is a hairline drawing on an 11 px grid, kept inside the same 8 px square so that every gap to a joining line is the same. A mark is grey when done, blue while live and red when its step failed.

| Mark | Steps |
|---|---|
| A coil of thread | Thinking. |
| A prompt (`>_`) | Commands. |
| An open book | Reading a file or an artifact. |
| A pencil | Editing or writing a file, saving an artifact. |
| A magnifier | Searching files: grep, glob, listing a folder. |
| A globe | Searching the web. |
| A window | Opening a page, browser scripts. |
| A pointer | Computer use: clicks, typing, keys. |
| A picture | Looking at, capturing or drawing an image. |
| A speech bubble | Messaging a helper. |
| A checked box | Tasks and the board. |
| A bookmark | Memory and findings. |
| A letter | What a helper sends back, in its row and in a background report. |
| A plain ring | Any other tool. |

Five or more calls at once keep the fan, and the helpers' line keeps its fan too.

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
- `src/renderer/components/seam/`: `WorkStretch` (a stretch and its fold), `Steps` (quiet lines, groups, many at once), `Helpers` (the ribbon), `marks.tsx` (which mark a tool gets, and the drawings), `choreo.ts` (the fold's motion), `stepWords.ts` (plain words and the summary), `helperDigest.ts` (a helper's steps, read live from its child session), `source.ts` (the block builder, and which session a view reads).
- `src/renderer/styles/work.css`: the styles.
- `test-seams.mjs`: run with `npx tsx test-seams.mjs` after `npm i --no-save jsdom`.

The store gives a call its predecessor's `groupId` only while that call is still running, so calls from separate responses no longer read as parallel. Saved transcripts from before that change are split by start time instead (calls that started within 2.5 seconds of each other).
