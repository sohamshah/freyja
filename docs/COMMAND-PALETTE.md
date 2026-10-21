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
