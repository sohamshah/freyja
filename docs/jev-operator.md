# Jev operator: a decision-model-driven computer-use loop

`jev_computer_use` is a second computer-use tool next to `computer_use`. The existing tool
hands the whole observe → decide → act loop to an LLM sub-agent that reads screenshots and
calls the atomic tools. This one runs the loop in code, asks Jev (TypeSafe's System One
decision model) one batched question set per step, and calls an LLM only for the parts a
decision model cannot do: writing text into a field, re-planning when the loop is stuck,
resolving a screen the accessibility tree does not describe, and judging the end state.

## Problem

An LLM computer-use agent spends 2–10 s and thousands of output tokens per step deciding
which button to click, and most of those steps are recognition, not reasoning: the target
is a labelled control in the accessibility tree and the decision is "which row". Jev
answers "which row" in 100–400 ms with a calibrated probability, but it cannot produce a
string, so it cannot fill a field, summarize what happened, or invent a plan when the
visible controls do not match the goal. The design question is how to split one loop
between the two so that the fast model owns the common case and the slow model is entered
only through a small number of explicit doors.

## Loop

```
observe:  AX tree of the target app → indexed element table + screen text + dialog flag
decide:   one Jev call: operation, click_target, type_target, key_target,
          text_source, launch_target, unexpected_dialog, subgoal_complete
act:      code validates the chosen id, applies the safety gate, executes via freyja_native
verify:   re-observe; diff (window title, values, screen text) → changed flag + summary
          into history; 3 consecutive actions that changed nothing meaningful
          (for a scroll: nothing at all) = stuck
```

Jev never sees a screenshot. State carries observation only (app, windows, screen text, element table, recent actions with their diffs, current sub-goal). The goal is placed in each question's `instructions`, following the pattern TypeSafe recommends and the cua recipe adopted (research/R10). Every target question has a `none` option; the operation question has `done`, `blocked`, and `need_help`.

Element rows are `[i] AXRole 'label' value='…' (state)`. The label is the element's AXTitle, else its AXDescription (`label` in `freyja_native.read_ax_tree` output), else its value or help text; static text contributes its AXValue to the screen text. The native `description` key is AXRoleDescription ("button", "text") and is never used as a name, except for window close/minimize/zoom buttons, whose role description is specific. Click roles and type roles form separate option sets, and the target is read from the set matching the chosen operation. Sliders, progress bars and level indicators are listed with their values for reading only: a click would move a slider.

Menu bar items are clickable rows. Items of a menu appear only while that menu is showing (the reader skips closed menus). Menu items that open a submenu read "opens submenu". Checkboxes, switches and radio buttons read on/off or selected in rows and diffs; other controls read "selected" when AXSelected says so, and a change of selection counts as progress.

Elements whose center lies outside their scroll area or window read "scrolled out of view". They are targets, but choosing one scrolls toward it instead of clicking, because AX keeps reporting their bounds and a click there lands on whatever is drawn at that point; Jev chooses the control again once it is in view. A scroll goes toward the hidden rows (sideways when they are off to a side), pages the scroll area with its AXScroll<Way>ByPage action where offered, and falls back to the scroll wheel inside the visible part of that scroll view. The next observation waits until element positions stop moving, because smooth scrolling keeps animating after the event. Secure text fields are never listed as type targets.

The table is capped at 200 rows: the focused window's controls in view, then the menus, then the focused window's controls out of view, then other windows. Row values are clipped to 120 characters for Jev, while the replan and verify doors see up to 4000.

Typing into a text area appends at its end: the operator always moves to the end first, since a selection left by an earlier step would otherwise be replaced. A line break in the text is a Return key press there; in a single-line field it becomes a space, so typed text never submits a form past the Return gate. The same text is not appended to the same text area twice in a row: the second time goes to the planner instead (an item was once typed six times, since each append changed the document and so counted as progress). Quoted text in the planner's sub-goal is a typing option for Jev, so text the planner composes, such as a leading line break, reaches the field. Typing into a text field replaces its contents (Cmd+A, then the text). Cmd+A is always sent twice: when the text ends in an unfinished word, the first one only makes macOS commit its pending autocorrection (measured 0/5 vs 5/5 with the second press).

## Handoff doors

| Door | Trigger | LLM receives | LLM returns | Control returns to Jev |
|---|---|---|---|---|
| text | Jev picks `type` and `text_source` = `needs_llm` (or no literal in the goal fits) | goal, field label/role/value, screen text, last 6 actions, the planner's sub-goal and notes | `{"text": str \| null}` | text typed; next observation |
| replan | Jev picks `need_help` or `blocked`, or stuck rule fires | goal, history, element table, screen text, its own earlier notes, the run's first screen, the run time, a capture of the target window (with its pixel size) if the table is empty or this is the second replan | `{"status": continue/done/give_up, "subgoal": str, "direct_action": {...} \| null, "note": str}` | sub-goal added to instructions; Jev `subgoal_complete` Noul clears it |
| verify | Jev picks `done` with confidence ≥ 0.6, the planner returns `done` or gives up, the replan budget runs out, or the run runs out of steps or time | goal, history, focused window title, final screen text and element table, the other screens the run read, the planner's notes, the run time | `{"satisfied": bool, "summary": str, "report": str, "subgoal": str \| null, "wait_seconds": int \| null, "unresolved": [str]}` | if not satisfied, sub-goal continues the loop; a second disagreement with nothing new in between ends the run `blocked`, or `partial` when the check names values the pages do not show |

The verify door reads more than the final screen. On a web page the final screen text is the page's whole text: what is in view, then below the view, then above it, each marked, up to 24,000 characters. Jev's own decisions read only its first 3,000 characters, which end with a line saying how many more there are. The check also gets the other screens the run read, each whole and in order, under the page it was read on (up to 40,000 characters, dropping first the screens a later one contains), and the planner's notes. It returns `report`, the values the goal asked to read, list or count, complete, which the result shows under `Read:`. A list read page by page is gone from the final screen: one run paged through all 27 projects of a picker and still reported the IDs as "not visible here", and the agent grepped the run log for them. A goal that only asks to read is met once the report has everything. A run that ends without a decision of its own (out of steps or time) gets one check too, so a met read goal ends `done`, and a stuck run keeps the check's account and report instead of the list of actions it took. A planner that gives up gets the same check first, when a call is left.

Reading a booking page (2026-10-17). Two read-only searches on a wedding block's booking site reached the right pages and lost minutes to how the run read them:

- The screen text was cut at 6,000 characters in the page and at 3,000 for every reader, so a room's policy ended at "Cancella…". The run took the cut as text missing from the page and scrolled up and down for it, while the check asked again and again for the same clause.
- The run kept each line it read once for the whole run. Both rooms had a "Room Only Flexible" plan at the same price, so the pet room's plan was the Deluxe room's line, and the check said the pet room's plans were never shown. It now keeps whole screens.
- The check put the planner's notes on a par with the page. They are now the planner's notes, not evidence: a value comes from a screen, goes under the record the text shows it under, and is reported as unclear when the text does not say which record. A value worked out from the page (three nights at a per-night price) is labelled computed, and a per-night price the page calls "From" or an average is not an exact charge.
- The check names, in `unresolved`, values the goal asked for that the run looked for wherever the goal let it look (a quote shown only after selecting a room, when the goal forbids selecting one). With nothing left to look at, the run ends `partial` (code `unresolved`) with its report and the missing values, and its hint says that running again finds nothing more.

The replan prompt states the operator's key vocabulary (macOS shortcuts, including
cmd+down/cmd+up) and the typing rules above, so sub-goals name actions Jev can take. A
planner `done` is not final: it goes through the same end-state check as Jev's, since the
planner reads the same screen and can be just as wrong.

Long forms. While a planner sub-goal is active, Jev's `done` with the sub-goal marked complete means the sub-goal: the loop clears it and decides again against the goal, without an end-state check (one passenger form drew three checks this way). An end-state check that disagrees after the run made progress since the last one starts the count of disagreements again; only done-claims with nothing done in between end the run. Jev's and the planner's done-claims share that count: counted apart, one run drew six checks, five of them planner claims with nothing new read in between.

Panels. A click on a control whose panel the run's last action opened is refused, when the control now shows it open (`aria-expanded`, or an open disclosure triangle) and nothing was done in the panel since. A second click on such a control closes what the first one opened. On a token form Jev opened "Add permissions" and clicked it again on the next step, twice. The open list had come before the form in page order, so the button's row number had moved from [36] to [76]. The run stopped with no permission added. The refusal goes into the history Jev reads, and a sub-goal that starts with that click is finished, so Jev decides again against the goal. A goal or sub-goal that says to close, collapse or hide lets the click through, and so does any other action in between, e.g. a pick in the list. On the env task that rebuilds the form (`console/key_permissions`), 5 runs clicked the button 26 times before and 5 times after (one click each), with 5.2 LLM calls a run instead of 7.0.

Scrolling. A page scroll is progress only when it brings text the run had not read. Scrolling back over a page the run already has moves the screen but learns nothing, and it had reset the counts that end a stuck run. Four such scrolls in a row go to the planner. A scroll that moved nothing marks the page's edge: the same scroll on the same screen is not tried again but goes to the planner at once, saying the page is already at its top or bottom (one run tried three scrolls up at the top, three times). The last LLM call is kept for the end-state check: runs that spent every call on replans ended "blocked" with the goal met, or "done" unchecked. The same action putting the screen back where it was two actions earlier (a panel opened, closed, opened) is not progress, so the stuck rule sees a toggle undoing itself.

Waiting on the world. Jev's own `wait` is under a second, for a page that is still loading. A goal that waits for something to happen on its own (a dashboard value to cross a line, a job to finish, a code to arrive) needs more. After four waits in a row Jev hands over, and the planner can return `direct_action` `{"kind": "wait", "seconds": n}`, up to 60 s and never past the run's time limit less 20 s, with a sub-goal for what to check after it ("click Refresh, read the backlog"). A planner wait needs no screenshot. A wait during which the screen changed counts as progress, because watching is the work, and Jev's own waits count from zero again after it. Before this, a watch run replanned every few seconds and ended "stuck" 35 s in.

Jev's end-state check. `OperatorConfig.verify` (the tool's `verify` parameter;
`FREYJA_JEV_VERIFY` sets the default, `llm`) chooses who checks a "done". `llm` is the
verify door above. `jev` asks Jev two Nouls in one call, on the goal, the last eight
actions and the screen text: whether the goal is met, and whether the goal asks for
values to be reported back; a "met" at `JEV_DONE_MIN` (0.8) or more ends the run with
the template summary and no report, under a second. `auto` takes Jev's "met" the same
way when no values are to be reported, and otherwise asks the verify door, which can name
what is left: a Noul cannot, and an early version that also took Jev's "not met" as final
stalled the multi-item desk goals at the third rejected claim. On basics, hotel, canvas
and desk (34 tasks, one rep each, threshold 0.9 at the time), `auto` matched `llm` on 32
of 34 (the two differences were desk tasks passing under `auto`, an env at its limit
either way) and took the check off six basics runs whose only LLM call it had been
(rows_one 4.6 s to 2.4 s, rows_two 5.7 s to 3.0 s, long_export 4.6 s to 2.6 s). The Noul
runs cold: of 56 checks the LLM also judged, every one at 0.8 or more agreed with it (9 of
9), 10 of 12 between 0.5 and 0.8, and 6 of 19 under 0.2 were met after all; the threshold
is 0.8 on that evidence. See `evals/jev_box/RESULTS.md` section 8.

The end-state check can wait too. When the goal waits for something that has not happened yet, the check returns `wait_seconds` with its sub-goal, and the loop waits before going on. Such a check does not count as a disagreement, because Jev's done-claim was early, not wrong about the screen: two checks in a row ended a watch run "blocked" a minute into its two minutes. The LLM budget and the run's time limit still bound the run. Neither door puts the wait in the sub-goal: "after the wait, click Refresh" made Jev wait again, under a second at a time.

The planner's memory. Each replan sees only the last 10 actions, and a diff shows only text that is new. So the planner also gets its own earlier notes (the last 12, oldest first), the run's first screen, and the run time. Without its notes, a watch run's planner wrote "the starting backlog is 385, page at 424", then "treat 422 as the baseline", then "the starting value was not recorded". Jev never sees the notes, because a note goes stale one action later: given them, Jev saved an order unchanged right after opening it, under a note saying the previous order "needs saving". Instead, the planner and the check put any fact the next step depends on into the sub-goal, which they write anew each time. The sub-goal starts with the action and adds the fact briefly ("Click Page on-call [7]: the backlog is 428, at least 10% above the starting 385"). An example that put the fact first made the planner open every sub-goal with a recap of what was done; the action came last, Jev waited instead of clicking, and the rescues, which look for a sub-goal that starts with its action, stopped matching. Shown only the current backlog, Jev had answered "no click target" for Page on-call five times, because the goal said not to page before a rise it could not check. Every action in the history carries `at_s`, the run seconds when it ran, and both doors get the run time. A goal with a time limit ("if it has not happened within 2 minutes, stop and tell me so") is met only by a reading taken at or after the limit: one run refreshed at 100 s and reported "no rise within 2 minutes" at 130 s.

Indices in a sub-goal belong to the screen the planner read. When the page changes, the loop renumbers each `[N]` to the control with the same role and label, or drops the number and keeps the label when no such control is on the screen once. Picking an arrival day once inserted a RESET CALENDAR link, the departure day moved from [30] to [31], and Jev, told [30], doubted the right day.

One record per sub-goal. A sub-goal stays on one record or form and never chains a step past a save or a page change. With its notes, the planner began to plan across records ("Click Save changes [14], then click Edit PO-24072"); the next order's form had the same Save button, and Jev saved it unchanged before choosing its new vendor.

Repeated steps. The doors write a step taken before as new ("click Refresh again, then read the new backlog"), or Jev takes it as done. Telling Jev which of its recent actions came after the sub-goal was set worked for the watch tasks but made it literal elsewhere: "Click Load more reviews to show more of the 140" read as done after one click, and a count run went from 2/3 to 0/3. That change was dropped.

Literals. Numbers in the goal keep thousands commas but not the sentence's punctuation ("5550100," went into a phone field), and dates and phone numbers are one literal ("06/23/1992", "303-555-0147"), never their parts.

`direct_action` exists for screens the AX tree does not describe (custom-drawn views,
Electron apps without AX): the LLM may return one click, one key press or text to type,
and code executes it before returning to Jev. Only a window capture is sent: a display
capture may not show the target at all when it sits on another monitor.

A click carries the planner's own pixel coordinates in the capture, which code maps
through the window's on-screen bounds, and `target`, the control in words ("the blue Save
button"). On the screenshot it read, the planner's coordinates are used: Claude Opus 5.5
and GPT-6.1 Sol put them inside the control 99 to 100% of the time on the web
environments, better than any grounding model (`evals/jev_box/RESULTS.md`). The words are
for the steps after it. `direct_actions` is a list of up to four actions in order; only
the first click may carry coordinates, because the screen changes after it. Each later
click is found by Jev on a fresh capture of the window, with a Box question (an image
feature of Jev 1.14: the answer is the four edges of a box around what the instructions
describe, as fractions of the image) and a Noul for whether the target is on screen at
all (`bridge/tools/jev_operator/vision.py`). A small first box is asked again on a crop
around it at the capture's own resolution. The click is left to the planner when Jev does
not see the target (P(visible) < 0.5) or its box confidence is under 0.5: on ScreenSpot-v2
the boxes above that line are inside the target 92% of the time and the ones below it are
the look-alike rows where grounding fails. Every action carries `expect`, what the screen
shows once it has worked ("the File menu is open", "the Name field reads Ada"); after the
action, Jev answers a Noul on a fresh capture of the window (about 280 ms, 3,400 tokens),
once more after 1.5 s when it did not hold, and that verdict says whether the plan goes
on. A click that opened the wrong menu changes the window too, which is why the verdict
is not a pixel diff; the diff (more than 0.008% of the window, which a ticked checkbox
clears and a caret does not) stands in only for an action with no expectation. The plan
stops at the first expectation that does not hold or target Jev cannot place. An action
that worked counts toward the stuck rules exactly as a changed element table does, so a
run on an app with no accessibility tree can go on past two replans. A screen with no
controls and no text goes to the planner at once, without a decision call Jev could only
answer `need_help` to. When such a run ends, the end-state check gets a capture of the
final window, since it has no text to judge from. Measured on the web environments driven
blind (no elements, no text): 3 of 6 tasks before, every run ending `blocked` after about
3 planner calls; 5 of 6 after, ending `done`, the sixth a harness limit; of 60
expectation checks, the 53 that held had a median p of 0.99 and the 7 that did not, at
p=0.00, were all right (`evals/jev_box/RESULTS.md`).

A long text the goal gives to type (a manifest, a message) is never copied into the
planner's reply, which has a 4,096-token limit: the action names where the text sits in
the goal (`from_goal.after`, the words before it; `from_goal.until`, the words after it)
and code types the goal's own text between them, with `replace: true` to clear the field
first. A planner reply cut off by its limit is reported as such and ends the run. On the
DOM surface a planner `type` fills the page field that has the focus, as a page operation;
it never sends OS keystrokes, which would land in whatever app is in front.

The doors use `$FREYJA_JEV_OPERATOR_LLM`, default `claude-opus-5-5`. They ask for thinking off, but Opus 5.5 cannot turn thinking off, and the level falls back to the model's default (medium).
Replayed door calls took 4-8 s on Opus 5.5 against about 2 s on Haiku 4.5; Opus planned
recoveries and judged end states better, and its replies (130-550 tokens) fit the 2048
token caps. Earlier caps of 300-600 tokens truncated its JSON. Every door call's raw
reply, parsed result, latency, and stop reason is written to the run log as an `llm`
event.

When the goal contains a quoted string or a number, Jev can pick it as the text to type
(`text_source` options are the literals extracted from the goal plus `needs_llm`), so
"type \"hello\" into the search field" needs no LLM call.

Long text. A quoted span counts up to 1,000 characters, and a fenced block (three
backticks on their own lines) is one literal of any length up to 32,000 characters, with
the quotes inside it left alone. Jev is offered a long literal by its size and its two
ends ("the 3,412-character text of 161 lines given in the goal, which begins ... and ends
..."), not in full, and the operator types the whole text. Before this, a quoted span
over 200 characters was no literal, and a manifest pasted bare into a goal was not one
either: a 208-character string in a goal stopped a no-LLM run with "no text could be
determined", and the same manifest given to the planner stalled a run for 165 s while
the planner tried to copy all of it into a 2,048-token reply. `items` entries count up to
2,000 characters.

Door deadlines. A door call that has not answered after `DOOR_TIMEOUT_S` (90 s) is given
up, and the run's reason says so ("the replan call did not answer within 90 s"). The run
view names what the run is waiting on (the planner, the field text, the end-state check)
with a seconds counter after five seconds, instead of "reading the screen" for every wait.

Calling agents rarely quote values: none of 25 goals in a real booking session did ("first name Ada, last name Lovelace"). Each such field took a text-door call, and on the booking pages those calls were a third of all door calls, every one copying a word of the goal. The text door now also returns the goal's other values to type, one per field. The words of the goal among them become typing options for the rest of the run, so a passenger form takes one call instead of one per field. A value that is not a word of the goal is dropped, so the model cannot add a typing option of its own.

The planner also gives values that are in no goal. It reads pages the screen no longer shows and works values out from them: a refund amount from an order and a policy, a sign-in code from an email, a section number from a contract. Then it writes a sub-goal such as "Type 86.00 into the Amount field [16]", and it leaves out the quotes about half the time. Before, such a value was no typing option, and the text door saw neither the sub-goal nor the planner's notes, so it returned no text and the run went back to the planner. In one baseline round, 22 runs stopped this way. Now two changes cover it:

- One word after "type" in the sub-goal that holds a digit or an @ is a typing option (`observe.typed_values`), and the typing rescue takes it as it takes quoted text. "Type the query into" names no text, so it gives no option.
- The text door gets the sub-goal and the planner's notes, and returns the text they give for the field. The door still catches values the first change does not read, such as a name of several words.

Measured on the typing tasks (desk refunds, access and contract, mail sign-in, ops expense form), 3 runs each: correct decisions went from 54 to 77 of 126, stops for no text from 22 to 1, and text-door calls from 77 to 36. Each change alone did less: the door alone made 53 door calls, and the sub-goal values alone left 8 stops. Telling the planner to quote every value it types was also tried. It did not help (73 of 126), so the prompt is unchanged.

## Safety

- The host owns the candidate table; an id Jev returns that is not in the table is a
  refusal, not an action.
- Input only reaches the target app. Immediately before each click, keystroke, or scroll
  (after the highlight), the actuator checks that the target is the frontmost app
  according to Launch Services and, for pointer input, that the topmost window under the
  point belongs to it. It refocuses once; if another app still has focus or covers the
  point, the run stops `blocked`, and a point outside every window only refuses that one
  action. It does not refocus when someone is using the computer: keyboard or pointer
  input newer than the operator's own last action within the last 3 s stops the run
  instead. Input is also refused when another window of the same app is in front of the
  intended one, compared by window frame (titles repeat, e.g. two "Untitled" documents);
  sheets and popovers have frames of their own and still pass. A menu item whose AXPress
  fails gets no pointer click, since its menu has usually closed. Clicks are
  synthesized at screen coordinates, so without these checks a covered target sends the
  click to whatever window is on top. The frontmost app comes from `lsappinfo`, because
  NSWorkspace's frontmost application is not refreshed in the bridge process.
- Buttons are pressed with AXPress when the element supports it: the element at the point
  is hit-tested and matched by role and frame, and if something else is on top the tree is
  searched for exactly one element with that role and frame. Pointer clicks carry their
  own location (enigo posted the button events at the pointer position it read back right
  after the move, often still the old one, so clicks landed where the pointer had been).
- Irreversible controls (buttons, menu items, links, and menu or toolbar buttons whose
  label matches delete, remove, send, submit, purchase, pay, empty trash, erase, reset,
  shut down, log out, uninstall, clear history, agree, ...) are gated. "Agree" after a
  terms box accepts the terms, such as a Marketplace purchase on a cloud console. Menu-bar items, rows,
  cells, and tabs are not, since activating them only opens, selects, or toggles. The
  Delete key is gated unless a text field has focus, and commit keys are gated when the
  app exposes no accessibility tree. Default policy `ask`: the run stops and returns the
  proposed action; the parent agent or user re-invokes with `allow_irreversible=true`,
  which permits every gated control for that run. No model finalizes these alone.
- Without `app`, the run targets the frontmost app and stops if that is Freyja itself;
  it never guesses the next window in z-order.
- Secure text fields are excluded from targets; passwords are never typed.
- Budgets: `max_steps` (default 40), Jev calls ≤ 2 × steps, LLM calls ≤ 32 per run (`DEFAULT_MAX_CALLS` in `handoff.py`). The first LLM budget, 8, was a guess for one-screen desktop goals. On web pages Jev hands over more often, and a whole booking as one goal spent all 8 calls on the first two pages while each replan still led somewhere. It went 0/5 at 8 calls, 1/5 at 16 and 5/5 at 32, using 15-20. A run that stops making progress ends on the replan rule (three replans without progress), whatever the budget. A run that runs out of calls while still making progress ends `budget_exhausted`, not `blocked`, so the caller continues from that page instead of taking over.
- Every step is logged to `~/.freyja/jev-operator/runs/<run_id>.jsonl` with the state
  hash, answers, executed action, and diff, which is the data a future calibration map
  is fit on. Confidence thresholds (operation 0.5, target 0.5, done 0.6) are starting
  points, not measured values; the benchmark showed many-way Choice runs hot on
  ill-posed questions, so a `none` pick and a low-confidence pick both route to
  `need_help` rather than to a guess.
- Log rows carry `t`, the epoch seconds when the row was written, and `elapsed_s`, the run seconds then; an action's own run seconds are `at_s`. An outcome row once spread its action's `t` (run seconds) over its own, so sorting a log by `t` put outcomes before the run started. The last row, `finish` (`version` 2), holds everything the caller was sent: status, code, summary, report, page, pending action, the counts and the footer. The footer counts `actions` (what the run did on the screen, the address it opened included), `decisions` (Jev calls, some of which ask for help) and `llm_calls`; the generic sub-agent memo says the same in words, where it had said "25 tool calls, 36 steps" for 25 actions and 36 decisions.
- Three rescues let a low answer stand:
  - A click target scored at least 0.3 is taken when its label is written in the goal or in the first step of the planner's sub-goal (before any "then"). "Click Cobalt Parts [10], then click Save changes [14]" once rescued a 0.43 Save before the vendor was chosen. A label that other controls repeat ("Suspend" on every row of a members table) counts only when one of the control's own row words is in the text too, and none of its twins has that word. The rescue once matched "then click the Suspend button" and suspended the wrong member three times.
  - A "click" scored at least 0.3 is taken when the planner's sub-goal starts by naming that row ("Click [14] 'Select seat for …'"), Jev picks the same row with a target score of at least 0.5, and the run has not clicked it since the planner set the sub-goal. Without this rescue, a seat run and a paged list each stopped after Jev scored the planner's click 0.3–0.4 three times.
  - A typing step is taken when the planner's sub-goal starts with one ("Type 'Claude' into the 'Search models' field [45]"). Jev must pick the same field and the same text (quoted, or one unquoted word with a digit or an @, as in "Type 86.00 into the Amount field [16]"), each at 0.5 or more, the field must not already hold the text, and the run must not have typed it since the planner set the sub-goal. The operation's probability for "type" may be spread over the steps typing does itself: a click with no click target (text fields are not click targets, so it can only mean focusing the field) and a scroll toward a field out of view. Their sum must reach 0.5. A catalog run drew scroll up 0.42, click 0.40 and type 0.15 three times, with the field and the text at 0.95 and 0.99, and stopped there. The `type` option itself now says that typing focuses the field and scrolls it into view.
- A click on a look-alike row goes to the row the goal names exactly. When the goal or sub-goal names an identifier (an email, or a code of letters and digits such as `Q-20455`), and Jev picks a control whose row has only a near copy of it (at most two edits apart, such as `grace.hooper.1437` for `grace.hopper.1437`, or digits swapped), while exactly one twin (the same control on another row) has it exactly, the click goes to that twin and the reason says so. With every row's email in its name, Jev still chose the look-alike's Suspend at 0.65, two rows above the right one.

## Surfaces: the accessibility tree and the page DOM

The loop reads and acts through a surface (`surface.py`). `AXSurface` is everything above: the accessibility tree and synthetic input through `freyja_native`. `DOMSurface` (`dom_surface.py`, `dom_snapshot.js`) reads and drives a web page in Arc or Chrome through the browser's own JavaScript, sent with `osascript` (`execute <tab> javascript`). Both produce the same `Observation`, so Jev's questions, the gates and the doors do not know which one ran.

Selection is code: with `surface=auto`, a supported browser that answers a trivial script (`document.title`) gets the DOM surface, and other apps get AX. A browser that is slow to answer gets one longer retry. A browser that still does not answer stops the run, and so do two DOM failures in a row and a closed tab: there is no fallback to AX on a browser, because the AX surface acts on the browser's front window, whatever tabs it holds. With its test page not loaded, a run once fell back to AX and switched the person's own tabs in the sidebar. `surface=ax` still forces AX on a browser.

Why a DOM surface: the AX tree of a browser is the worst case for this loop (60-130 s per read on Arc before read budgets, unnamed fields, page content mixed with browser chrome), while the page DOM answers in about 0.2 s with named controls.

How the AX reader keeps native apps fast and complete (`native/freyja_native/src/ax.rs`):

- **One round trip per element.** All attributes of an element, its children included, come from one `AXUIElementCopyMultipleAttributeValues` call. One call per attribute meant a dozen round trips per element: Finder in column view spent the whole 8 s budget on about 900 elements and never reached the folder the goal named.
- **Closed menus are skipped.** A closed menu still lists all its items, with zero-size frames. In Finder they were 353 of 382 elements.
- **Rows out of view are capped.** Inside a window or scroll area, a parent lists at most 30 children that lie outside the visible rect; past that, a frame read decides, and children in view are still read. A folder of 600 files costs about 30 rows, not 600. Whole columns scrolled sideways out of view are few, so they are read: their file names are in the table, marked "scrolled out of view".
- **Selected state.** `AXSelected` is read. Choices with no value of their own, such as System Settings' Auto, Light and Dark tiles, show as "(selected)", as do the selected row and tab. A change of selection counts as progress.
- **Sideways scrolling.** A scroll goes toward the rows the goal names, in whichever direction hides them, at a point inside the visible part of their scroll view. A Finder column browser opens scrolled to its first columns, with the folder's own column off to the right; a vertical scroll there did nothing.
- **Controls out of view are targets.** Jev may choose a control marked "scrolled out of view". The step then scrolls toward it instead of clicking, because its center can lie on top of another control; Jev chooses it again once it is in view, and the gates apply then. A control coming into view counts as progress.
- **Icon-and-name items.** A group of one icon and one name (a file in Finder's column and icon views) is one clickable item named by its text. Its name field is listed only while it is being renamed. Text fields with no name of their own also name the row they sit in, so a Finder list row reads "roadmap.md · Today at 11:45 · …".

How the DOM surface behaves:

- **One tab.** A run is pinned to a tab by id: the tab active when it started, or the tab it opened. Tab ids are matched with one bulk `id of every tab` per window, since a window can hold a thousand tabs. A click that opens another tab (`target=_blank`, `window.open`) moves the run there, but only to a tab that did not exist when the run started: if the person brought one of their own tabs to the front meanwhile, the run stays where it was. A tab can also open after the click has returned: a file viewer's Download asks its server first and opens the virus-scan warning in a new tab about a second later. So for 15 seconds after a click, each read checks the front tab, and a new tab on the same site (the last two parts of the host name) moves the run there. A late tab on another site may be the person's own, so the run stays put and says so in the log. The person can switch tabs meanwhile.
- **Which tab a run may start in.** The process keeps the ids of the tabs runs have opened or worked in. A run whose goal gives no address starts only in one of those; otherwise it stops `blocked` (code `foreign_tab`) and names the front tab, so the caller can put the address in the goal. The front tab can be anything the person has open: a test run once attached to a live video call that had come to the front, and clicked in it.
- **Editors.** A text area or editor with no name of its own is listed by the heading,
  legend or label before it in its section, and a code editor (CodeMirror 5 and 6, Monaco,
  Ace) as "code editor (heading)". Slack's manifest editor was `(no name fill)` and Jev
  gave typing into it 0.40 confidence. A fill into a code editor dispatches the editor's
  own cmd+a before `insertText`, since selecting its hidden textarea or contenteditable
  selects nothing a person would call all, and reads the value back from the editor's
  model (CodeMirror 5's `getValue`, CodeMirror 6's `cmView`, Monaco's single model, Ace's
  `env.editor`) because the editor draws only the lines in view; with no model reachable
  the fill is not checked rather than failed.
- **URLs.** When the goal names a URL and the tab is not on it, code opens it in a new tab before the first decision, so a run never starts by acting on an unrelated tab. An address the goal gives only as a fallback, in a clause that starts with a condition ("If the owned tab is unavailable, open https://…", "otherwise open …"), is not opened when the run can continue in a tab a run already used on the same site: one continuation found the first run's results tab, then opened the entry page anyway and searched again from the start. A quoted address is text to type (a form's website field), not a page to open, unless the goal says to open it. Jev can also choose `open_url` later (the goal's URLs are its options). A tab the run opened itself is reused for later addresses; the person's own tabs are never navigated.
- **No pointer, no focus.** Page runs send no OS input and do not bring the browser forward, so they keep working behind other windows and while the screen is locked. Native-app runs cannot.
- **Snapshot.** Up to 250 controls, including those inside open shadow roots (web components; MDN's whole search lives in them), nearest to the visible area first (controls far down a long page are targets; using one scrolls it into view), with a stable id per element, accessible names, values, states, and the page's text: what is in view, then text below the fold, then up to 8,000 characters above it, up to 24,000 in all. Text a clipping ancestor hides (a panel collapsed to no height, which keeps its text in the page) and text faded to opacity 0 (a carousel's other slides) is not page text: a closed room panel's rate read as the open room's. A `<select>` lists each option as a clickable row. Password, file and hidden inputs are never listed.
- **Controls the markup does not announce.** Real booking pages draw many controls without roles. The snapshot also lists:
  - elements with a `tabindex` or a pointer cursor that hold no control of their own and little text (suggestion rows, offer cards, fare boxes); inside a popup list they read as options (`AXMenuItem`);
  - radios and checkboxes the page draws over a zero-size or transparent input, through the visible box around it; a click goes to that box, and to the input itself if the box's handler did not select it;
  - read-only fields (travelers, a date) as clicks with their value, since they open a panel or picker and cannot be typed into.
- **Names.** A control with no accessible name is named from its image file (`search_btn.svg` → `search`), its icon (`<use href="#close">`), its id (`btnSearch`) or an icon class, with the source in the row (`'search (no visible name; from image search_btn.svg)'`). The text around an unnamed control is never its label (a Search link once read "Search By* Promo Code ..."). Repeated labels ("Add (Claude Team $200 / month)") are told apart by their own part of the page: its heading ("Passenger 2 (Adult)"), else its leading text that is not a control's ("October 2026" for day links, not "Prev"). A control on a table row (`tr`, `role=row`) is named by the whole row, cell by cell, as a person reads across it: `'Suspend (Grace Hopper · grace.hopper.1437@example.com · Member · Active · 4 days ago)'`. The first cell alone named five Suspend buttons "Ada Carter", and the email that told them apart was in the next cell. Repeated controls whose names still read alike, the same or at most two letters apart with the same digits (Hooper and Hopper, not "Passenger 1" and "Passenger 2"), get all of their own part's text the same way. The text of controls that every row repeats (Suspend, Edit) is left out of these names; a row's own link, such as a person's name, is kept. A control with a short label (30 characters or fewer) inside one of several look-alike cards (an ancestor with a sibling of the same tag and class: room cards, result rows, list items) is named by its card's heading, else its leading text, even when no other control shares its label. Two room cards each had "VIEW DETAILS"; once one read "VIEWING", neither label repeated, both lost their room, and a run opened the wrong room's details. The irreversible gate reads a page control by its own name, not its card's: "Save PO" in a card headed "New purchase order" read as a purchase. Day numbers that only one month of a calendar has (January's 29 to 31 beside a 28-day February) take the month from a header that holds only the month name, when no header gives the month and the year together. Drawn grid cells (fare boxes) are named by their column header and their row: `'Business Bundle: $359 Club Saver $361 Standard — Biz fare (9:05 PM DEN → 2:48 AM MCO ...)'`. Page rows keep up to 150 characters.
- **Not on the page.** Controls with `opacity: 0`, and controls parked beside the window where nothing scrolls to them (closed slide-in panels; a real airline listed six "Close" buttons on every page), are left out.
- **Dialogs.** A modal layer over the page (a dialog role, or a fixed element with content covering most of the viewport, inside a shadow root too) becomes the observation's `dialog`: its text leads the screen text and only its controls are listed, with a count of those behind it. An upsell appended after a long result list had been cut off from the screen text, and the run waited on a page it could not see was blocked. An empty transparent sheet that only catches outside clicks is not a dialog.
- **Frames.** Embedded frames are reported at the top of the screen text (`[embedded frame from <host>, WxH at (x, y): ...]`): their content cannot be read or clicked through the page, and a run that ends short of done says to finish that part with `computer_use`.
- **Acting.** Clicks dispatch the full pointer sequence (pointerdown, mousedown, pointerup, mouseup, click), because many widgets commit on mousedown. The press moves focus the way a person's does: to the control or its focusable ancestor, or off the focused field, so a field typed last fires `change` (frameworks that bind on change never saw it after a click on a div). A click blocked by an open panel (not a modal layer) is preceded by what a person does to close it: a press on the page outside it, then, if the panel is still there, a click on the expanded control that opened it (a travelers field that ignored Escape and outside presses). If it is still blocked, the refusal names what covers it and that control. Typing the value a field already holds clears it first, because autocomplete widgets search only when the value changes. Text goes in through `insertText` like a person typing, so frameworks and rich editors register it, and is read back; a mismatch fails the action. Each action is refused as "stale" when its element changed since the snapshot, and is re-bound only when exactly one element still matches. A control is refused as "covered" only when a person's click could not reach it: when its middle is under something else, other points of the control are tried, and a part of the same field (the topmost element and the control share an ancestor at most six times the control's area) does not count. Outlined fields draw their label across the middle of the control and not inside it, so a cloud console's selects and empty text fields were refused as covered and its form could not be filled. A dialog, a backdrop or an open dropdown panel still covers what is under it. After each action the surface waits until the page stops changing (same mutation count and URL on two reads), up to 3 s (12 s after opening a URL). Requests the action started (fetch and XHR, counted by the page-world hook) and a navigation under way keep the page busy for up to 15 s: a Continue that posts and navigates when the server answers had looked settled at 3 s. After typing into a field that shows suggestions (`aria-autocomplete`, a combobox, an autocomplete class, `autocomplete=off`) it waits at least 0.8 s, since such widgets pause and then fetch.
- **Isolated world.** Arc runs AppleScript JavaScript in an isolated world: the script shares the page's DOM but not its globals (`window.H` set by a page script is undefined from the snapshot). Reading and acting through the DOM is unaffected; anything that must change page behaviour goes in through a `<script>` element and talks back with DOM events.
- **Page dialogs.** That hook makes `alert()` and `prompt()` non-blocking while a run is active (15 s after its last call), reports them in the action's record and the screen text, reports `window.open` as a new tab, and answers `confirm()` with Cancel unless the run has `allow_irreversible`. A declined confirm ends the run `needs_confirmation`, quoting the page's question; a button labelled "Clear filters" is not caught by the label gate, but its "This cannot be undone" confirm is. After the run the page's own dialogs come back. A strict Content-Security-Policy blocks the hook; on those pages dialogs behave as usual (a background tab answers them Cancel without showing them).
- **Keys.** Only Return, Escape, Tab, Down and Up are offered; browser shortcuts act on the window, which this surface does not see. Down and Up move through a suggestion list ("use up and down arrow keys to navigate", as an airport field said while the run had no way to press them). A field whose suggestion list is open reads `(suggestions open: pick one)`, its suggestions are listed right after it, and Return there picks the highlighted suggestion without submitting (hint `pick`, not gated). A search box with its suggestions open reads `(suggestions open: Return searches for the typed text; Down, then Return, picks one)` (hint `search-pick`, not gated): told to pick a suggestion there, a catalog run pressed Down and Return and opened the first model instead of searching. Return works in an editable field and on a focused combobox, listbox, option, menu item, tab or tree item, whose page script handles keys (Enter opens a focused select). The replan door is told the same, and instead of an address bar it can return `open_url` with an address it builds (a site's own search URL), which code opens only on a site the goal names or the run has been on. Return in a field is gated by what the snapshot says it does there: a search box passes, a form passes unless its submit button's label is irreversible, and a field with no form or search role (a chat box may send on Return) needs confirmation.

## Turns

Runs take turns on the screen. A run started while another is going waits for it, and the tool's reply says so; agents often start two runs in one message, and the second used to fail. A run stopped while it waits ends cancelled without opening a tab or touching the screen. A screenshot-based `computer_use` run does not take turns, so a jev run refuses to start while one is driving the screen.

## Items

`items` runs the same goal once per item, each with fresh loop state, sharing the surface, the tab and the time limit. The item text is data: it appears only in a delimited ITEM block and in the typed-literal pool, which also gets the item's parts ("Delta $412.18 — memo: Flight to NYC" offers "Flight to NYC"), so a field is not filled with the whole line. The result is one table (status, steps, seconds, evidence per item). Three items in a row ending the same non-done way stop the run; `skip_items` resumes it.

## In the app

A run writes its log as text into its own sub-agent session: one line per event (`Operator._say`: the target, each step and its outcome, replans, sub-goals, stops), a line `item N of T (#i): <item>` before each item of an items run, and at the end a `[result]` line followed by the result text the parent receives. Keep these line formats when you change a `_say` call: the renderer parses them, and a line it does not know shows as a plain note.

- **The run's pane.** `src/renderer/lib/jevLog.ts` parses the log and `src/renderer/components/JevRun.tsx` draws it as a step timeline: the operation, the target, and the outcome with what changed (URLs shortened), with replans, sub-goals and checks as callouts and repeated actions marked "again". A step number in amber had low confidence. The "timings" switch shows each step's confidence, Jev and read times, and element count. The result card below shows the status, the summary, the item table, what a run waits on, the page, the counts and the handoff. The text is the record, so runs saved before this view render the same way; a run with no `[result]` block takes its result from the parent's sub-agent record when the parent was open first. With a search query the pane shows the raw log, so matches can be highlighted.
- **The parent.** The sub-agent card, the inbox memo and the expanded `jev_computer_use` tool row draw the result: its real status (blocked, needs confirmation, partly done) instead of "finished" or "failed", the summary, the item table and the pending action. While a run is going, the card shows its latest step; page runs send no screenshots, so this line is the only live view.
- **Elsewhere.** A run that ended short of done (the bridge reports outcome `stuck`) is "stopped", in amber, in the sidebar, the swarm list, the dashboard and the Activity card; only an error is "failed". Sidebar rows for runs show "jev" and running, done or stopped.

`test-jev-log.mjs` (`npx tsx test-jev-log.mjs`) checks the parser against each line format and the store path.

## Notes and learning

Before deciding, the loop reads two kinds of skill as notes for Jev and the doors: `jev-app-<app>` (for example `jev-app-calculator`) and, on a web page, `jev-site-<host>` (for example `jev-site-console-cloud-google-com`). They are ordinary skills, so they are written the way all skills are: the main agent asks a `skill-drafter` sub-agent to propose one after a run that taught it something, and the person approves it.

## Testing against real apps

`scripts/jev_harness.py` runs the tool the way the bridge does (a real `SubAgentSpec`, background mode, the inbox memo the agent receives) from a source tree, with the app's own Python and native extension:

```sh
cd /Applications/Freyja.app/Contents/Resources
./python-bundle/bin/python3 <repo>/scripts/jev_harness.py call '{"goal": "...", "app": "Arc"}' --trace
./python-bundle/bin/python3 <repo>/scripts/jev_harness.py serve &      # fixture pages on 127.0.0.1:8765
./python-bundle/bin/python3 <repo>/scripts/jev_harness.py suite dom real ax
```

`evals/computer_use/` runs the same operator headless (DevTools protocol, an isolated world as in Arc) against mock sites with server-side ground truth: a budget-airline booking flow whose quirks reproduce the patterns above, the cloud-console fixtures, and the other fixture pages. It needs no browser of the person's and runs episodes in parallel; see its README.

`scripts/jev_scenarios.py` holds the scenarios: fixture pages (`tests/fixtures/jev_live`) that report what was done to the harness server, public sites read-only, and native apps. Checks read that ground truth, not the run's own summary. Browser scenarios open their own tab and close only the tabs they created. The `ax` group moves the real pointer and needs an unlocked, idle Mac.

The native scenarios follow three rules:

- **Fresh values per run.** A setup returns the values the goal and the check use: random operands for Calculator, a new file for TextEdit, a new folder for Finder. Calculator reopens showing its last result, so a fixed `12 × 7` once passed with no action at all.
- **Accessibility and `open` only.** Setup, checks and teardown read and press through `freyja_native`, never AppleScript. An AppleScript command to an app needs an Automation grant for the terminal, and the first one raises a consent prompt that blocks the run until someone answers it.
- **Settings stay as they were.** The Finder checks compare Finder's default view (`FXPreferredViewStyle`) before and after, and the Appearance and volume checks compare those settings. In a folder with no view of its own, Finder's view buttons change the default view for every folder: a planner that switched a window to list view to read it changed the person's default (2026-10-08).
- **Close only what the run opened.** A scenario records the id of the window it opened and closes that window: it raises it with `AXRaise` (`focus_window` only activates the app), confirms from the window server's front-to-back order that it is in front, then presses Cmd+W. Titles are not used, because a run can navigate its window elsewhere. Windows that appeared during a run are reported, not closed: one may be the person's. Left-over windows matter: when a later setup deleted their folders, Finder moved each to the parent folder (600 entries), and every Finder read grew to 1,800 elements.
- **Wait for a quiet Mac.** A native scenario starts only after 4 s without keyboard or pointer input, and the harness sends its own keys only to the app in front.

The native group has 13 scenarios: Calculator (multiply; square root, which needs Scientific mode), TextEdit (type; find and replace; a for-each run with `items`), Finder in column view (list a folder; rename a file; create a folder), System Settings (macOS version; Appearance; output volume), Dictionary, and Preview (page count of a generated PDF).

To try a change to the native reader before a rebuild, build a wheel with `maturin build --release` into a scratch folder, unzip it, and put that folder on `PYTHONPATH` when running the harness.

## What this does not do

It does not read pixels except through the replan door, which gets a screenshot on a second replan or when the table is empty, and through Jev's Box and Noul questions that place the planner's described targets on later captures and check its expectations. On web pages it does not see into embedded frames, closed shadow roots, or canvas content, and it cannot hover or drag. A page that draws its own controls (no controls listed at all, or an embedded frame in view) is the one case a page run uses the pointer and keyboard: the planner gets the window capture and may return clicks by pixel and text to type, the surface brings the browser and the run's tab forward (`bring_front`, an AppleScript tab select plus app focus; a no-op headless), and the clicks and keystrokes go through the actuator as on a native app, with Jev placing later targets and checking expectations on fresh captures. On the `canvas` environment (a dashboard drawn on one canvas, and the same app inside an iframe) that path passes 15 of 15 runs across five tasks, 4.3 planner calls a run, with all 15 of Jev's lookups placed (`evals/jev_box/RESULTS.md` §7). Otherwise its clicks are script events: a control that answers only a person's input (a real airline's Travelers panel closed only on a trusted click) cannot be operated; that needs a trusted-input fallback, or `computer_use`. It does not plan multi-app workflows on its own; the parent agent should pass one app-scoped goal at a time, or the LLM replan door will be entered often. It does not run while any other computer-use session is active, since both would drive the same keyboard and mouse.

## Files

- `bridge/tools/jev_operator/observe.py` — AX tree → `Observation` (table, text, dialog, fingerprint, diff)
- `bridge/tools/jev_operator/decide.py` — question battery, Jev call, answer validation
- `bridge/tools/jev_operator/act.py` — actuator over `freyja_native` with UI events
- `bridge/tools/jev_operator/handoff.py` — the three LLM doors
- `bridge/tools/jev_operator/vision.py` — a described control found in a window capture (Jev Box), and the frame-change measure
- `bridge/tools/jev_operator/loop.py` — `Operator.run()`
- `bridge/tools/jev_operator/surface.py` — the surface protocol and `AXSurface`
- `bridge/tools/jev_operator/dom_surface.py`, `dom_snapshot.js` — the page surface for Arc and Chrome
- `bridge/tools/jev_operator/items.py` — for-each runs over `items`
- `bridge/tools/jev_operator/notes.py` — `jev-app-*` and `jev-site-*` notes
- `scripts/jev_harness.py`, `scripts/jev_scenarios.py`, `tests/fixtures/jev_live/` — live testing
- `bridge/tools/jev_operator/__main__.py` — CLI for headless runs
- `bridge/tools/jev_computer_use_tool.py` — tool wrapper registered next to `computer_use`
- `src/renderer/lib/jevLog.ts`, `src/renderer/components/JevRun.tsx`, `test-jev-log.mjs` — how a run shows in the app
- `tests/test_jev_operator.py` — table serialization, diff, gating, literal extraction, loop with fake providers
- `tests/test_jev_vision.py` — grounding, the second look on a crop, plans of described actions, the confidence gate
- `evals/jev_box/` — grounding and blind-operation experiments behind the numbers above
- `tests/test_jev_planner_values.py` — values the planner works out reach the field it names
- `tests/test_jev_toggle_panel.py` — a second click on the control whose list the last click opened is refused
- `tests/test_jev_late_tab.py` — a tab a click opens after the click returned is followed
