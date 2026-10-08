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

Jev never sees a screenshot. State carries observation only (app, windows, screen text, element table, recent actions with their diffs, current sub-goal). The goal is placed in each question's `instructions`, following the pattern TypeSafe recommends and the cua recipe adopted (research/R10). Every target question has a `none` option; the operation question has `done`, `blocked`, and `need_help`.

Element rows are `[i] AXRole 'label' value='…' (state)`. The label is the element's AXTitle, else its AXDescription (`label` in `freyja_native.read_ax_tree` output), else its value or help text; static text contributes its AXValue to the screen text. The native `description` key is AXRoleDescription ("button", "text") and is never used as a name, except for window close/minimize/zoom buttons, whose role description is specific. Click roles and type roles form separate option sets, and the target is read from the set matching the chosen operation. Sliders, progress bars and level indicators are listed with their values for reading only: a click would move a slider.

Menu bar items are clickable rows. Items of a menu appear only while that menu is showing (the reader skips closed menus). Menu items that open a submenu read "opens submenu". Checkboxes, switches and radio buttons read on/off or selected in rows and diffs; other controls read "selected" when AXSelected says so, and a change of selection counts as progress.

Elements whose center lies outside their scroll area or window read "scrolled out of view". They are targets, but choosing one scrolls toward it instead of clicking, because AX keeps reporting their bounds and a click there lands on whatever is drawn at that point; Jev chooses the control again once it is in view. A scroll goes toward the hidden rows (sideways when they are off to a side), pages the scroll area with its AXScroll<Way>ByPage action where offered, and falls back to the scroll wheel inside the visible part of that scroll view. The next observation waits until element positions stop moving, because smooth scrolling keeps animating after the event. Secure text fields are never listed as type targets.

The table is capped at 200 rows: the focused window's controls in view, then the menus, then the focused window's controls out of view, then other windows. Row values are clipped to 120 characters for Jev, while the replan and verify doors see up to 4000.

Typing into a text area appends at its end: the operator always moves to the end first, since a selection left by an earlier step would otherwise be replaced. A line break in the text is a Return key press there; in a single-line field it becomes a space, so typed text never submits a form past the Return gate. The same text is not appended to the same text area twice in a row: the second time goes to the planner instead (an item was once typed six times, since each append changed the document and so counted as progress). Quoted text in the planner's sub-goal is a typing option for Jev, so text the planner composes, such as a leading line break, reaches the field. Typing into a text field replaces its contents (Cmd+A, then the text). Cmd+A is always sent twice: when the text ends in an unfinished word, the first one only makes macOS commit its pending autocorrection (measured 0/5 vs 5/5 with the second press).

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

## Surfaces: the accessibility tree and the page DOM

The loop reads and acts through a surface (`surface.py`). `AXSurface` is everything above: the accessibility tree and synthetic input through `freyja_native`. `DOMSurface` (`dom_surface.py`, `dom_snapshot.js`) reads and drives a web page in Arc or Chrome through the browser's own JavaScript, sent with `osascript` (`execute <tab> javascript`). Both produce the same `Observation`, so Jev's questions, the gates and the doors do not know which one ran.

Selection is code: with `surface=auto`, a supported browser that answers a trivial script (`document.title`) gets the DOM surface; anything else gets AX. A browser that is slow to answer gets one longer retry, because the AX fallback for a browser is far slower: Arc's tree has 3,500+ nodes and a read takes the full 8 s budget. Two DOM failures in a row switch the run to AX for good; a closed tab ends the run.

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

- **One tab.** A run is pinned to a tab by id: the tab active when it started, or the tab it opened. Tab ids are matched with one bulk `id of every tab` per window, since a window can hold a thousand tabs. A click that opens another tab (`target=_blank`, `window.open`) moves the run there. The person can switch tabs meanwhile.
- **URLs.** When the goal names a URL and the tab is not on it, code opens it in a new tab before the first decision, so a run never starts by acting on an unrelated tab. Jev can also choose `open_url` later (the goal's URLs are its options). A tab the run opened itself is reused for later addresses; the person's own tabs are never navigated.
- **No pointer, no focus.** Page runs send no OS input and do not bring the browser forward, so they keep working behind other windows and while the screen is locked. Native-app runs cannot.
- **Snapshot.** Up to 250 controls, including those inside open shadow roots (web components; MDN's whole search lives in them), nearest to the visible area first (controls far down a long page are targets; using one scrolls it into view), with a stable id per element, accessible names, values, states, row context for repeated labels ("Add (Claude Team $200 / month)"), and the visible text followed by text below the fold. A `<select>` lists each option as a clickable row. Password, file and hidden inputs are never listed.
- **Acting.** Clicks dispatch the full pointer sequence (pointerdown, mousedown, pointerup, mouseup, click), because many widgets commit on mousedown. Text goes in through `insertText` like a person typing, so frameworks and rich editors register it, and is read back; a mismatch fails the action. Each action is refused as "stale" when its element changed since the snapshot, and is re-bound only when exactly one element still matches. After each action the surface waits until the page stops changing (same mutation count and URL on two reads), up to 3 s (12 s after opening a URL).
- **Isolated world.** Arc runs AppleScript JavaScript in an isolated world: the script shares the page's DOM but not its globals (`window.H` set by a page script is undefined from the snapshot). Reading and acting through the DOM is unaffected; anything that must change page behaviour goes in through a `<script>` element and talks back with DOM events.
- **Page dialogs.** That hook makes `alert()` and `prompt()` non-blocking while a run is active (15 s after its last call), reports them in the action's record and the screen text, reports `window.open` as a new tab, and answers `confirm()` with Cancel unless the run has `allow_irreversible`. A declined confirm ends the run `needs_confirmation`, quoting the page's question; a button labelled "Clear filters" is not caught by the label gate, but its "This cannot be undone" confirm is. After the run the page's own dialogs come back. A strict Content-Security-Policy blocks the hook; on those pages dialogs behave as usual (a background tab answers them Cancel without showing them).
- **Keys.** Only Return, Escape and Tab are offered; browser shortcuts act on the window, which this surface does not see. The replan door is told the same, and instead of an address bar it can return `open_url` with an address it builds (a site's own search URL), which code opens only on a site the goal names or the run has been on. Return in a field is gated by what the snapshot says it does there: a search box passes, a form passes unless its submit button's label is irreversible, and a field with no form or search role (a chat box may send on Return) needs confirmation.

## Items

`items` runs the same goal once per item, each with fresh loop state, sharing the surface, the tab and the time limit. The item text is data: it appears only in a delimited ITEM block and in the typed-literal pool, which also gets the item's parts ("Delta $412.18 — memo: Flight to NYC" offers "Flight to NYC"), so a field is not filled with the whole line. The result is one table (status, steps, seconds, evidence per item). Three items in a row ending the same non-done way stop the run; `skip_items` resumes it.

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

It does not read pixels except through the replan door, which gets a screenshot on a second replan, or when it would otherwise give up. On web pages it does not see iframes from other origins, closed shadow roots, or canvas content, and it cannot hover or drag. It does not plan multi-app workflows on its own; the parent agent should pass one app-scoped goal at a time, or the LLM replan door will be entered often. It does not run while any other computer-use session is active, since both would drive the same keyboard and mouse.

## Files

- `bridge/tools/jev_operator/observe.py` — AX tree → `Observation` (table, text, dialog, fingerprint, diff)
- `bridge/tools/jev_operator/decide.py` — question battery, Jev call, answer validation
- `bridge/tools/jev_operator/act.py` — actuator over `freyja_native` with UI events
- `bridge/tools/jev_operator/handoff.py` — the three LLM doors
- `bridge/tools/jev_operator/loop.py` — `Operator.run()`
- `bridge/tools/jev_operator/surface.py` — the surface protocol and `AXSurface`
- `bridge/tools/jev_operator/dom_surface.py`, `dom_snapshot.js` — the page surface for Arc and Chrome
- `bridge/tools/jev_operator/items.py` — for-each runs over `items`
- `bridge/tools/jev_operator/notes.py` — `jev-app-*` and `jev-site-*` notes
- `scripts/jev_harness.py`, `scripts/jev_scenarios.py`, `tests/fixtures/jev_live/` — live testing
- `bridge/tools/jev_operator/__main__.py` — CLI for headless runs
- `bridge/tools/jev_computer_use_tool.py` — tool wrapper registered next to `computer_use`
- `tests/test_jev_operator.py` — table serialization, diff, gating, literal extraction, loop with fake providers
