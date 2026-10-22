# ⌘K: the generative palette

Typing a command name into ⌘K still filters the list. Typing a sentence turns
the palette into the one card that sentence needs, and the card fills in as the
sentence grows:

    create a meeting                      → event card, every slot empty
    create a meeting with nari            → With: Nari
    create a meeting with nari tomorrow   → When: Thursday 2026-10-22
    … tomorrow at 1pm                     → Time: 1:00–1:30 PM, "Create event" enables

## Two clocks

**Slots are synchronous.** `src/renderer/lib/paletteIntent.ts` parses the
query on every keystroke: attendees (`with nari`, `meet nari`), day
(`tomorrow`, `next tuesday`, `oct 27`), time (`at 1pm`, `13:00`, `afternoon`),
duration, topic, PR number and repo, model name and thinking level, period
(`last 7 days`), file types, theme, tool name, log level, swarm status. Nothing
here waits on the network, so a card never lags the typing.

**Jev is debounced.** `src/main/jevRouter.ts` sends the query to TypeSafe's
System One (`jev:classify` IPC, ~80–250 ms) with three questions: which domain
(calendar, memory, agent_task, model_picker, token_usage, artifact_explorer,
briefing, swarm, tool_inspect, diagnostics, voice_session, quick_control,
finance, palette_command), which sub-target inside it (calendar_view vs
calendar_create, appearance_toggle vs mic_toggle…), and a readiness score. Jev
only decides *what* the person means; it never generates text and never sees
store data.

## Choosing and keeping a card

`chooseWidget()` maps domain + sub-target to a card, with local verb cues
breaking ties inside a domain (`what meetings` → view, `set up a meeting` →
create). Three rules keep the palette from flickering:

1. A row whose title starts with the text wins outright (`swarm`, `morning`,
   `/goal`): no Jev call, no card.
2. A card stays mounted while Jev is still deciding on a longer version of the
   same sentence. React keys the card by type, so slot updates re-render in
   place instead of remounting.
3. The card yields to the list only when Jev is confident the text is a plain
   command, or the text shrinks below three characters.

Open-ended text that is none of these gets an "Ask Freyja" row that drops it
into the composer.

## What the cards act on

Cards read the real store and act through it: `availableModels` / `setModel`,
`sessions` (token spend over a window), `subagents`, the artifact index,
`setAppearanceTheme`, the voice store, scheduler jobs. Calendar, memory,
delegation and finance cards compose a precise instruction and `sendMessage`
it, so the agent runs the Google Calendar MCP or spawns the sub-agent; the
card is the form, the agent is the executor. ↵ fires the card's primary
action; Esc steps back to the list, a second Esc closes.

The standalone prototype this grew from is in `mockups/jev-command-palette/`.


## Deep cards (second pass)

Three structural pieces were added on top of the two-clock design.

**Candidate strips.** Jev returns the whole domain distribution, not just the
winner. While the leader is not clearly ahead (`leadP − p < 0.55`, `p ≥ 0.12`),
the runners-up render as small strips under the card (or above the list when
no card has won yet): label, probability bar, percentage. As the sentence
grows and the distribution sharpens, strips fade out (`useLeaving`, 220 ms).
Clicking a strip pins its domain: that card shows, the former leader becomes
a strip. Esc clears the pin. A local tiebreak ("how do I …" phrasing with an
unsure Jev → Skills) can change the shown card; the strips always treat the
*shown* card's domain as the leader.

**Card-to-card handoff.** Every card gets `handoff(next)`; the palette keeps a
stack. The top card renders with breadcrumbs (`CALENDAR › Reschedule`), the
query is cleared so typing refines the pushed card's slots (`mergeSlots`:
typed values fill gaps, handed-over values stay), Jev is paused while a pushed
card is up, and Esc pops the stack and restores the previous query. Current
handoffs: agenda row → reschedule (create card pre-filled, event id in
`context`), PR row → review (delegation card with profile `review`, PR, repo,
and a prompt-file focus), artifact row → preview, clipboard path → preview.

**Cards that keep morphing after render.** Data arriving after first paint
changes the card: the create card fetches the chosen day and shows conflicts
and free-slot chips (`freeSlots`, working hours 9–18, 15-min grid); the agenda
card turns a typed "at 3pm" into a busy/free verdict; the model card dims rows
whose provider key is missing and keeps them out of the slot-driven pick; PR
rows get CI, size and a `prompt` flag once `gh pr view` returns; Slack rows
filter by the person as the name is typed.

### Data layer

`src/renderer/lib/paletteData.ts`
- `mcpCall(server, tool, args)` sends `mcp_command` action `call` with the
  panel surface and resolves when the reply lands in `mcp.lastResults`. No
  agent turn, no transcript entry.
- Calendar: `google_calendar_hosted` (Google's hosted Calendar MCP,
  `https://calendarmcp.googleapis.com/mcp/v1`, same OAuth client as the Drive
  and Docs entries). `listEvents`, `freeSlots`. Cards show a connect panel
  with the setup steps until the server is active, and fall back to handing
  the request to the agent.
- Slack: `slack_search_channels` → `slack_read_channel` for a channel,
  `slack_search_public_and_private` for a person or topic. The tool output is
  markdown-ish text inside JSON; the parsers live next to the calls.

`src/main/paletteSources.ts`
- `listPalettePrs(scope, repo)`: `gh search prs` (review-requested / mine /
  involves) plus `gh pr view` for the first eight (checks, size, files;
  prompt/skill paths flagged). Cached 60 s.
- `readPaletteClipboard()`: classifies the clipboard (url, json, stack, path,
  diff, code, text) once per palette open; the empty palette proposes actions
  for it.
