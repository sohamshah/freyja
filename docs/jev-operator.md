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
          into history; 3 consecutive unchanged non-scroll actions = stuck
```

Jev never sees a screenshot. State carries observation only (app, windows, screen text,
element table, recent actions with their diffs, current sub-goal). The goal is placed in
each question's `instructions`, following the pattern TypeSafe recommends and the cua
recipe adopted (research/R10). Every target question has a `none` option; the operation
question has `done`, `blocked`, and `need_help`.

Element rows are `[i] AXRole 'label' value='…' (state)`. The label is the element's
AXTitle, else its AXDescription (`label` in `freyja_native.read_ax_tree` output), else its
value or help text; static text contributes its AXValue to the screen text. The native
`description` key is AXRoleDescription ("button", "text") and is never used as a name,
except for window close/minimize/zoom buttons, whose role description is specific. Click
roles and type roles form separate option sets and the target is read from the set
matching the chosen operation. Menu bar items are clickable rows. Items of a menu appear
only while that menu is showing, which the AX tree reports directly: a closed menu still
lists its items, but with zero-size bounds. Menu items that open a submenu read "opens
submenu", and checkboxes, switches, and radio buttons read on/off (selected) in rows and
diffs. Elements whose center lies outside their scroll area or window are shown as
"scrolled out of view" and are not targets, because AX keeps reporting their bounds and a
click there lands on whatever is drawn at that point. A scroll operation scrolls inside the
scroll view that hides the rows the goal names (otherwise the one hiding the most rows),
not at the window center, and the next observation waits until element positions stop
moving, because smooth scrolling keeps animating after the event. Secure text fields are
never listed as type targets. The table is capped at 200 rows, focused window first,
scrolled-out elements last; row values are clipped to 120 characters for Jev, while the
replan and verify doors see up to 4000.

Typing into a text area appends at its end; typing into a text field replaces its contents
(Cmd+A, then the text). Cmd+A is always sent twice: when the text ends in an unfinished
word, the first one only makes macOS commit its pending autocorrection (measured 0/5 vs
5/5 with the second press).

## Handoff doors

| Door | Trigger | LLM receives | LLM returns | Control returns to Jev |
|---|---|---|---|---|
| text | Jev picks `type` and `text_source` = `needs_llm` (or no literal in the goal fits) | goal, field label/role/value, screen text, last 6 actions | `{"text": str \| null}` | text typed; next observation |
| replan | Jev picks `need_help` or `blocked`, or stuck rule fires | goal, history, element table, screen text, a capture of the target window (with its pixel size) if the table is empty or this is the second replan | `{"status": continue/done/give_up, "subgoal": str, "direct_action": {...} \| null, "note": str}` | sub-goal added to instructions; Jev `subgoal_complete` Noul clears it |
| verify | Jev picks `done` with confidence ≥ 0.6, the planner returns `done`, or the replan budget runs out | goal, history, focused window title, final screen text and element table | `{"satisfied": bool, "summary": str, "subgoal": str \| null}` | if not satisfied, sub-goal continues the loop; a second disagreement ends the run `blocked`, not `done` |

The replan prompt states the operator's key vocabulary (macOS shortcuts, including
cmd+down/cmd+up) and the typing rules above, so sub-goals name actions Jev can take. A
planner `done` is not final: it goes through the same end-state check as Jev's, since the
planner reads the same screen and can be just as wrong.

`direct_action` exists for screens the AX tree does not describe (custom-drawn views,
Electron apps without AX): the LLM may return one click at pixel coordinates in the window
capture, which code maps through the window's on-screen bounds, or one key press, and
code executes it once before returning to Jev. This is the existing vision loop, limited
to a single step. Only a window capture is sent: a display capture may not show the
target at all when it sits on another monitor.

The doors use `$FREYJA_JEV_OPERATOR_LLM`, default `claude-opus-5-5`, with thinking off.
Replayed door calls took 4-8 s on Opus 5.5 against about 2 s on Haiku 4.5; Opus planned
recoveries and judged end states better, and its replies (130-550 tokens) fit the 2048
token caps. Earlier caps of 300-600 tokens truncated its JSON. Every door call's raw
reply, parsed result, latency, and stop reason is written to the run log as an `llm`
event.

When the goal contains a quoted string or a number, Jev can pick it as the text to type
(`text_source` options are the literals extracted from the goal plus `needs_llm`), so
"type \"hello\" into the search field" needs no LLM call.

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
  shut down, log out, uninstall, clear history, ...) are gated. Menu-bar items, rows,
  cells, and tabs are not, since activating them only opens, selects, or toggles. The
  Delete key is gated unless a text field has focus, and commit keys are gated when the
  app exposes no accessibility tree. Default policy `ask`: the run stops and returns the
  proposed action; the parent agent or user re-invokes with `allow_irreversible=true`,
  which permits every gated control for that run. No model finalizes these alone.
- Without `app`, the run targets the frontmost app and stops if that is Freyja itself;
  it never guesses the next window in z-order.
- Secure text fields are excluded from targets; passwords are never typed.
- Budgets: `max_steps` (default 40), Jev calls ≤ 2 × steps, LLM calls ≤ 8 per run.
- Every step is logged to `~/.freyja/jev-operator/runs/<run_id>.jsonl` with the state
  hash, answers, executed action, and diff, which is the data a future calibration map
  is fit on. Confidence thresholds (operation 0.5, target 0.5, done 0.6) are starting
  points, not measured values; the benchmark showed many-way Choice runs hot on
  ill-posed questions, so a `none` pick and a low-confidence pick both route to
  `need_help` rather than to a guess.

## What this does not do

It does not read pixels except through the `direct_action` door. It does not plan
multi-app workflows on its own; the parent agent should pass one app-scoped goal at a
time, or the LLM replan door will be entered often. It does not run while any other
computer-use session is active, since both would drive the same keyboard and mouse.

## Files

- `bridge/tools/jev_operator/observe.py` — AX tree → `Observation` (table, text, dialog, fingerprint, diff)
- `bridge/tools/jev_operator/decide.py` — question battery, Jev call, answer validation
- `bridge/tools/jev_operator/act.py` — actuator over `freyja_native` with UI events
- `bridge/tools/jev_operator/handoff.py` — the three LLM doors
- `bridge/tools/jev_operator/loop.py` — `Operator.run()`
- `bridge/tools/jev_operator/__main__.py` — CLI for headless runs
- `bridge/tools/jev_computer_use_tool.py` — tool wrapper registered next to `computer_use`
- `tests/test_jev_operator.py` — table serialization, diff, gating, literal extraction, loop with fake providers
