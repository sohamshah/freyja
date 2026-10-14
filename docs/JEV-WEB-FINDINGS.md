# jev on a real booking site: failures, fixes, and environments to test them

An agent used `jev_computer_use` to book two seats with a fare bundle on a US low-cost airline's site. Over 36 minutes it started 25 jev runs; 15 ended blocked and 7 ended done. The calling agent finished most of the booking with its own pointer clicks. This document records what failed and why, what changed in jev, and how the changes were tested.

The causes were found from the run logs, then confirmed by reading the airline's live pages (read-only, headless). Each pattern was rebuilt in a mock site (`evals/computer_use/`), and every fix was measured there against the unchanged code.

## The run in numbers

| | |
|---|---|
| jev runs | 25: 15 blocked, 7 done, 1 needs confirmation, 1 cancelled, 1 out of steps |
| Decisions | 158, of which 47 (30%) were "need help"; 15 of those said the needed control was not listed |
| LLM calls | 73, taking 66% of the runs' time |
| Taken over by hand | the airports, the travelers count, Search, every fare, the membership upsell, a Continue, both insurance opt-outs |
| Screenshot fallback | stopped by the person after three minutes; its screenshots failed and its clicks went to another monitor |

## What failed, and why

| What the run saw or did | Cause | Status |
|---|---|---|
| Typed the airport again and again; never committed it | The suggestions were list items with no role, no pointer cursor and a `tabindex=-1` wrapper. The page reader lists only controls its markup announces, so the suggestions never appeared. | Fixed |
| "Use up and down arrow keys to navigate", with no way to do so | Page runs offered only Return, Escape and Tab. Return in the field was gated as possibly submitting. | Fixed |
| Never opened the Travelers panel | Travelers is a read-only input. It was offered only as a typing target, and typing is refused in a read-only field. | Fixed |
| Did not find Search | Search was a link around an image with no alt text. The row was labelled with the text around it ("Search By* Promo Code Dollars Miles ..."). | Fixed |
| Saw no fares at all | Each fare is a box drawn around a zero-size radio. The reader dropped zero-size inputs, so the whole fare grid had no controls. The radios' own labels also repeat across columns. | Fixed |
| Waited, then retried a covered button | A membership upsell opened with no dialog role, appended after a long result list. Its text fell beyond the screen-text limit, and nothing marked the page as blocked. | Fixed |
| "Covered", without saying by what | A promotion in a shadow root covered the page. The calling agent guessed at a browser permission prompt. | Fixed: the refusal names the cover |
| "Close" buttons for six panels on every page | Closed slide-in panels are parked beside the window, off the right edge. The reader checked only above and below the window. | Fixed |
| Clicked Continue on add-ons, judged nothing happened, gave up | The page posts and navigates when the server answers (about 10 s later). The surface waited 3 s for the page to settle. | Fixed |
| Could not see the insurance opt-outs | They live in an embedded cross-origin frame, which a page run cannot read or click. | Open (now reported, with a handoff to `computer_use`) |
| Long passenger form ended "blocked" after five fields | Jev said "done" each time a planner sub-goal finished. A second disagreement from the end-state check ended the run, although it was still filling fields. Repeated labels ("First Name *" for each passenger) also read alike, so typing in the second passenger's field did not count as progress. | Fixed |
| Picked the wrong passenger's seat | A seat goes to the active passenger, and choosing one does not move on to the next. | Site behaviour. Jev now follows the planner's way out; a site note avoids the mistake |
| A dated day link read "29 (Prev)" | An early version of the new labels took the month's first line, which was the "Prev" link. Found on the live site. | Fixed |
| Booking lost: Continue on the passenger page sent the tab to the home page | Most likely the booking session expired: about 11 minutes passed between choosing fares and that Continue, while the agent waited for the person's details. A script click on a focusable link does move focus and fire `change`, so a missed blur is ruled out. | Open: the calling agent must gather details before checkout (tool description updated) |
| The page "did not answer" right after Search | The results page was still loading. The probe gave up after 11 s. | Fixed: it now waits about 20 s |
| The Travelers panel would not close, even after the fixes | On the live site the panel closes only on a trusted click. Script clicks are ignored, even with a click count and screen coordinates. Escape, presses outside and focus moving to another field do nothing. | Open: needs trusted input |
| The screenshot fallback failed | `window_id=0` (a model filling an optional field) produced "missing Screen Recording permission". Window captures claim to be the click coordinate space, but clicks map to the last display capture. Clicks also landed on the wrong monitor when the browser was on a secondary display. | Open: not changed here |

Two more findings came from the test runs:

- **LLM budget:** runs that spent all 8 LLM calls on replans had none left for the end-state check. They ended "blocked" with the goal met, or "done" without a check.
- **Goal literals:** extraction offered "5550100," with the sentence's comma, and once offered "1992" out of "06/23/1992".

## What changed in jev

### Page reader (`dom_snapshot.js`, `dom_surface.py`)

- **Controls the markup does not announce.** Elements with a `tabindex` or a pointer cursor that hold no control and little text are now listed; inside a popup list they read as options. Radios and checkboxes drawn over a hidden input are listed through the box a person clicks. Read-only fields are listed as clicks, with their value.
- **Names.** An unnamed control is named from its image file, icon, id or icon class, and the row says so. The surrounding text is no longer used as its label.
- **Repeated labels.** These are told apart by their section's heading, or by its leading text that is not a control's.
- **Grid cells.** A fare box reads as column header, shown price, then row, for example `'Business Bundle: $361 Standard — Biz fare (9:05 PM DEN → ...)'`.
- **Not on the page.** Controls with `opacity: 0`, and controls parked off-screen where nothing scrolls to them, are dropped.
- **Dialogs.** A modal layer over the page becomes the observation's dialog. Its text comes first, and only its controls are listed. An empty click-catching sheet does not count.
- **Frames.** Embedded frames are reported at the top of the screen text, and the handoff says to use `computer_use` for that part.
- **Suggestions.** A field whose suggestion list is open says so, and its suggestions are listed right after it.

### Acting

- **Focus.** A press moves focus the way a real press does, so the last field typed fires `change`.
- **Open panels.** A click blocked by an open panel first presses outside it, then clicks the expanded control that opened it, as a person would. If it is still blocked, the refusal names the cover and that control.
- **Keys.** Down and Up are page keys. Return in a field with an open suggestion list picks the suggestion and is not gated.
- **Retyping.** Typing the value a field already holds clears it first, so an autocomplete searches again.
- **Settling.** The surface waits for requests the action started, up to 15 s, and a little longer after typing into an autocomplete field.

### Loop (`loop.py`, `decide.py`, `observe.py`, `handoff.py`)

- **Sub-goals.** A "done" that refers to a finished planner sub-goal sends the run back to the goal, without an end-state check.
- **Disagreements.** An end-state disagreement after real progress restarts the count.
- **Oscillation.** A toggle that undoes itself (open, close, open) is not progress.
- **Budget.** The last LLM call is kept for the end-state check.
- **Planner clicks.** The planner's sub-goal can start by naming a row to click, such as "Click [14] 'Select seat for …'". When Jev picks that same row but scores the click itself between 0.3 and 0.5, the click is taken instead of asking the planner again. It is taken once per sub-goal, and the safety gates still apply.
- **Literals.** Numbers in the goal drop sentence punctuation. Dates and phone numbers stay whole.
- **Planner prompt.** It now says typing focuses a field (no "click the field first"), how to pick a suggestion, and what dialogs and frames mean.

Each change has a test that fails without it: `tests/test_jev_dom_snapshot.py` runs in headless Chrome against `tests/fixtures/jev_dom/patterns.html`, and `tests/test_jev_booking_patterns.py` drives the loop with scripted fakes.

## How it was tested

`evals/computer_use/` holds mock environments with ground truth on the server (see its README):

- **The airline.** A budget-airline booking flow whose quirks reproduce every pattern above, each behind a switch.
- **A cloud console.** The model-enablement questionnaire, project picker and catalog.
- **Basics.** The older jev fixture pages.

Episodes run headless, in parallel, through the production operator, with only the transport swapped. A scripted solver proves every task can be finished.

Results, 5 runs per task on the same mock, before and after these changes:

| Task | Before | After |
|---|---|---|
| Search: route, dates and travelers, then search | 0/5 | 5/5 |
| Fare select: two flights with the Business bundle, decline the upsell | 0/5 | 5/5 |
| Passengers: two travelers and a contact, then continue | 5/5 (one reported "blocked") | 5/5 |
| Seats: four seats on two flights | 5/5 | 5/5 |
| Whole booking, one goal per page | 0/5 | 3/5 |
| Payment page: read the total | 5/5 | 5/5 |
| Purchase: stop for confirmation | 5/5 | 5/5 |
| Whole booking as one goal | 0/5 | 0/5 |
| Known limit: opt-outs in a cross-origin frame | 0/5 | 0/5 |
| Known limit: a panel that only a trusted click closes | 0/5 | 0/5 |

The five page tasks went from 10/25 to 23/25. The regression set (cloud console and basics, 21 tasks × 2) went from 41/42 to 42/42. The one miss before was the paged project list: its run spent its last LLM call on a replan.

**A cleaner reading made Jev surer, and on one page more wrong.** In the first full measurement, seats fell from 5/5 to 0/5. The seat map gives a seat to whichever passenger is active, and choosing a seat does not move on to the next passenger. The new reader drops six off-screen Close buttons and names the logo. With that noise gone, Jev scored the second seat click 0.67–0.77 instead of 0.49–0.56, so every run gave that seat to the first passenger. The old code made the same mistake in 3 of 5 runs, but it recovered each time. After the mistake, the planner said exactly what to do: "Click [14] 'Select seat for Ada Lovelace …'". Jev picked that row but scored the click 0.36–0.43, and three such answers end a run. The planner-click rescue fixed it (seats 5/5). It also fixed a paged-list run in the regression set that stopped the same way on "Click the 'Next 10 rows' button [17]".

A site note avoids the seat-map mistake in the first place. With a two-sentence note ("click the passenger's 'Select seat for …' link before each seat"), seats went 5/5 before the rescue existed. Site behaviour like this belongs in a `jev-site-<host>` note.

The whole booking by page still fails 2 of 5, in one of two ways:

- **The seat-map mistake.** The seat goal clicked Continue before it checked the seats. A page run then has no way back: it has no browser Back, and the mock's step links do nothing.
- **Going past the goal.** With the seat note, both failures were this one. After Continue reached the Seats page, Jev clicked Continue again on Seats and on Baggage (scored 0.68 and 0.71), although the goal ended "click Continue to reach seat selection".

The two known-limit tasks fail as expected. The cross-origin opt-outs cannot be reached, and the run now names the frame and hands off. The panel ignores script clicks and focus changes, as on the live site.

On the live airline site, headless and read-only, the changed jev:

- closes the promotion
- commits both airports through the suggestions
- picks both dates from the calendar
- sets two travelers

It then stops at Search, behind the Travelers panel that ignores script clicks. The unchanged jev never committed an airport: it stopped at the first field, asking to confirm Enter.

## The LLM call budget

A run had 8 LLM calls. The number came with the first version of jev, as a guess for one-screen desktop goals, and was never measured. `OperatorConfig.llm_max_calls` was never read: the helper's own default was the real cap.

On web pages Jev hands over far more often than on the desktop, and the cap was what ended long goals. All five whole-booking runs spent their 8 calls by the second page (3 text, 4 replans, 1 end-state check), 50 s into a 1500 s limit, while every replan still led to a correct action. They then reported "blocked". Measured on the whole booking as one goal, 5 runs each:

| LLM calls per run | Whole booking | Calls used | Time |
|---|---|---|---|
| 8 (before) | 0/5 | 8 | 51 s |
| 16 | 1/5 | 16 | 98 s |
| 32 | 5/5 | 15–20 (mean 18.8) | 119 s |
| 32, and the text door names the goal's values | 5/5 | 13–15 (mean 14.0) | 107 s |

The budget is now 32. A run that stops making progress still ends at its third replan without progress, so the budget only limits runs that keep moving. A run that runs out of calls while still making progress now ends `budget_exhausted`: its hint says to continue from the last page, where "blocked" made the calling agent take over.

**A third of the calls copied a word of the goal.** Over the final runs, 95 of 304 door calls were the text door, and every one returned a word the goal already held: DEN, MCO, Ada, Lovelace, ada@example.com. Literals were only quoted text and numbers, and calling agents do not quote: none of the 25 goals in the real session did. The text door now also returns the goal's other values to type, and the single words among them become typing options for the rest of the run. The first try kept phrases too. Jev then typed "Orlando (MCO)" into the airport field, which matched no suggestion, so only single words are kept. Passengers went from 6.6 calls to 3.4 per run, and search from 7.4 to 5.8.

**Cost.** The door calls' token counts left out the prompt cache, so each call logged about 4 input tokens. Counted in full, a door call reads 1-9k tokens (median 4k) and writes 90-500 (median 240), which is $0.01-0.06 at Opus 5.5 prices. A whole booking as one goal costs $0.52 in door calls, and a run that used all 32 calls would cost about $1.20. Most regression tasks cost $0.01. The doors ask for no thinking, but Opus 5.5 cannot turn thinking off, so they run at its default (medium).

**One goal, or one per page.** With the budget raised, the whole booking as one goal finished 5/5, and one goal per page finished 5/10. Every seat run, in both forms, gives the first passenger's seat away on the second click (the seat map's active passenger). One goal's end-state check then finds the missing seat and fixes it. The page goal ends "Then click Continue", and about half the time Jev clicks it before checking, and a page run has no way back. So the tool description now says to give a flow one goal when the goal holds everything the pages ask for, with about 12 steps per page.

## Still open

- **Trusted-only controls.** Some controls ignore script events, like the live Travelers panel. A page run needs a trusted-input fallback for them: the real pointer, scoped to the run's tab and its window position. Until then, `computer_use` handles them.
- **Embedded frames.** A page run cannot enter a cross-origin frame. It now says so and hands that step to `computer_use`.
- **Going past the goal after a page change.** Jev sometimes keeps clicking Continue on the pages after the goal's last one. A stricter check on the first action after a page change may fix it, but it was not measured here.
- **Late suggestion lists.** The surface waits for suggestions only when the field shows it has them: `aria-autocomplete`, the combobox role, an autocomplete class, or `autocomplete=off`. If a field shows none of these, its list can open after the settle, and the run sees no list. The mock's airport fields show the same signs as the live site's widget.
- **Checkouts that expire.** A booking session can expire while the agent waits for the person. The tool description now says to gather everything before checkout.
- **Personal data.** Traveler names, dates of birth and contact details went into jev goals. That sends them to the decision-model provider and the LLM, and writes them in plain text to the run logs under `~/.freyja/jev-operator/runs`.
- **The screenshot tool.** `computer_use` has three problems:
  - an optional `window_id=0` reports a missing permission
  - window captures do not share the click coordinate space
  - a browser on a secondary display gets clicks on the wrong monitor
