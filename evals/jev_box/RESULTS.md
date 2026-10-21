# Results: Jev 1.14 images and Box for the operator

Run 2026-10-20 against `jev-1.14.0`, the planner models `claude-opus-5-5` and
`gpt-6.1-sol`, and the repo at `b2f5b5c` (old code) and the commit that adds
`vision.py` (new code). Scripts and data layout are in `README.md`.

## Question

The operator reads apps through the accessibility tree or the page DOM. When
an app exposes neither (Electron without AX, canvas, custom-drawn views), its
only pixel path was the replan door: the planner gets a window capture and
returns one click at pixel coordinates, and after two replans without a change
in the element table the run ends `blocked`. Jev 1.14 can take that capture and
answer where a described control is. Three things had to be measured before
wiring it in: how often the box lands on the target, whether that beats the
planner's own coordinates, and what the change does to whole runs.

## 1. ScreenSpot-v2: how good is Box at finding a control

ScreenSpot-v2 is the public GUI-grounding benchmark: 1,272 screenshots (334
desktop, 437 web, 501 mobile) each with an instruction and the box of the
control it means. The metric is the benchmark's: the click point (the centre
of the returned box) falls inside that box. One Box question per item, the
instruction phrased as "the element in `screen` to click in order to:
{instruction}", the image fitted to 1568 px on its long side.

| Variant | All | Desktop | Web | Mobile | Icons | Text | Tokens | Latency p50 |
|---|---|---|---|---|---|---|---|---|
| one Box question | 85.9% | 86.8% | 79.9% | 90.6% | 79.0% | 91.6% | 3,553 | 244 ms |
| always a second look on a crop | 87.6% | 89.8% | 87.2% | 86.4% | 84.3% | 90.3% | 5,053 | 364 ms |
| second look only when the first box is small (< 0.5% of the image) | **89.9%** | 89.8% | 88.1% | 91.4% | 86.1% | 92.8% | 4,295 | 306 ms |
| three Box questions in parallel, median box | 86.3% | 85.9% | 80.5% | 91.6% | 79.2% | 91.3% | 10,658 | 406 ms |
| three in parallel, then three on the crop | 88.2% | 90.4% | 87.4% | 87.4% | 84.1% | 91.7% | 15,158 | 572 ms |

Box is not deterministic (389 of 1,272 repeats returned the same box), so
self-consistency was tried: the edge-wise median of three parallel answers.
It adds 0.4 points to a single question and 0.6 to the always-crop variant,
at three times the tokens and 1.5 to 1.7 times the latency, and stays below
the conditional second look. The operator keeps one question per stage.

The second look crops 40% of each side around the first box and asks again at
the capture's own resolution. It lifts targets under 0.2% of the image from
61% to 80% and the smallest (under 0.05%) from 23% to 77%, and lowers large
ones (over 1%) from 94% to 88% because the crop clips them; hence the
conditional version, which is what `vision.locate` does. For scale, published
ScreenSpot-v2 numbers are about 18% for GPT-4o asked for coordinates, 55% for
SeeClick, and 85 to 92% for the dedicated 7B grounding models (OS-Atlas,
UGround, Qwen2.5-VL, UI-TARS).

Confidence (the box's own field) is informative but not sharp: AUROC 0.76 for
hit vs miss with the conditional second look. At or above 0.5 it covers 76% of
targets at 94.6% precision; below 0.5 the remaining 24% land 75% of the time.
Those low-confidence items are mostly look-alike rows and dense tables. Cost
is $0.042 per million input tokens, so a lookup is $0.00015 to $0.00021.

Box is not deterministic: the same image and question twice gave the same
first box on 389 of 1,272 items (Jev's text answers repeat to within 1e-4).

## 2. Web environments: Box against the planner's own coordinates

The operator's mock web environments (`evals/computer_use`) give exact truth
for free: every control's DOM rectangle. On 12 start pages at 1280 x 900,
every labelled control in view was a target, described as "the 'Save' button",
"the 'Work email' text field". The planner baseline is the same screenshot the
replan door sends (JPEG, quality 70) with the same framing it uses, asked for
the control's pixel centre.

| Who places the click | Inside the target | n | Latency p50 |
|---|---|---|---|
| Jev Box, PNG, second look when small | 81.0% | 121 | 500 ms |
| Jev Box, JPEG q70 (the replan capture), second look | 73.6% | 121 | 609 ms |
| Jev Box, PNG, one question | 70.2% | 121 | 302 ms |
| Claude Opus 5.5, own coordinates | **98.8%** | 80 | 1,693 ms |
| GPT-6.1 Sol, own coordinates (second sample, 80 targets where Jev PNG scored 87.5%) | **100%** | 60 | 1,755 ms |

Jev is weakest where controls repeat: 66% on the `desk` back-office tables
(rows of identical Approve and Reject buttons), 100% on the `basics` pages.
The planners miss essentially nothing on these pages. So on the screenshot the
planner is already looking at, its coordinates are the better click, and the
operator now uses them. Jev's place is the step after: a planner's coordinates
die with the screenshot, so every further pixel action used to cost another
image call (about 2 s and a few cents with Opus 5.5); a described target can
be placed on a fresh capture in 0.3 to 0.5 s for $0.0002.

## 3. Frame change: what counts as an effect without an element table

With no elements and no text, the only sign an action did anything is the
window itself. Measured on the environments, as the share of a 1280 x 900 page
whose grayscale value moved by more than 24/255 on a 512 px thumbnail:

| Event | Share changed |
|---|---|
| unchanged page captured twice | 0 |
| pointer hovering a toggle or button | 0 |
| caret in an empty field (2 x 16 px, computed) | 0.003% |
| checkbox ticked | 0.02% |
| name typed into a field | 0.08% |
| toggle flipped | 0.12% |
| field taking focus | 0.34% |
| dialog opening (from a blind run) | 88% |

`PIXEL_CHANGE_MIN` is 0.008%, between the caret and the checkbox. A change is
progress for the stuck rules unless the window looks as it did two actions
ago (a toggle undoing itself). The first threshold tried, 1% on a 128 px
thumbnail, stopped every plan after its first step: a toggle flip was 0.08%.

## 4. Blind runs: whole tasks on a surface that exposes nothing

Six tasks from the environments run through the real operator loop with a
surface that returns no elements and no text, which is what an app with an
empty accessibility tree looks like to it. Every action then comes from the
planner (Claude Opus 5.5) reading a screenshot. Reward is the environment's
own ground truth. Three arms, 3 reps for the first two and 2 for the third:

| Arm | Passed | Ended `done` | Planner calls / run | Seconds / run | Lookups (placed) | Lookup p50 |
|---|---|---|---|---|---|---|
| old code (`b2f5b5c`) | 9/18 | 0/18 | 3.2 | 14 | 0 | |
| new code, Jev places later steps | **15/18** | 15/18 | 4.1 | 23 | 22 (16) | 473 ms |
| new code, planner places later steps (same loop, grounder swapped) | 10/12 | 10/12 | 3.7 + 19 hidden | 24 | 19 (19) | 1,710 ms |

The one task every arm fails, `basics/form_save`, fails for reasons of the
headless harness, not grounding: its team field is a native `<select>` whose
open list is not drawn in headless screenshots, and `cmd+a` cannot be sent
through the DOM surface. Without it, the new code passes 15/15 and 10/10.

What moved the result is the loop, not the grounder: the old code ends every
run `blocked` after the third planner call because a planner click never
changes the element table; the new code counts a changed window as progress,
lets a replan return up to four actions in order, and hands the end-state
check a capture of the final window so a finished run can end `done`. Jev and
the planner reach the same tasks as grounders. Jev is 3.6 times faster per
lookup and about a hundred times cheaper (86,527 tokens, $0.0036, for all 18
runs), and it declines 6 of 22 lookups through its gates (target not seen, or
box confidence under 0.5), each of which went back to the planner; the planner
as grounder declined none, and each of its lookups is a further image call
that the run's planner-call count does not show.

One grounded click in an earlier new-code run went to the wrong row: "the Add
button in the Mistral Pro row" landed on the row above at confidence 0.38,
and the item it added could not be removed. That run is why the confidence
gate exists; with it, the same step was left to the planner.

## 5. Real macOS windows

`macos_ax.py` scores Box on labelled controls of real app windows against
their accessibility frames, through the operator's own capture path (the
screen must be unlocked: macOS hides every window from the accessibility API
while it is locked). Ten Apple apps, 201 controls (buttons, rows, radio
buttons, menu buttons, text fields), the hit rule as in §1; Claude Opus 5.5
placed 120 of them from the same captures as a baseline.

| Variant | Hit | Lookup p50 | Tokens / lookup |
|---|---|---|---|
| PNG at 1568, two-stage (the operator's path) | **74.6%** | 528 ms | 4,478 |
| PNG at 1568, single Box | 70.6% | 386 ms | 4,017 |
| JPEG at 1280 | 68.2% | 526 ms | 3,246 |
| Claude Opus 5.5 (120 controls) | 93.3% | 1,725 ms | |

By app, two-stage PNG: System Settings 90%, Calculator 88%, Contacts 84%,
Activity Monitor 81%, Font Book 78%, TextEdit 75% (4 controls), Calendar 70%,
Notes 71%, Reminders 70%, Finder 43%. By control: rows 81%, radio buttons 87%,
text fields 100%, buttons 68%, menu buttons 40%. By size: controls under 0.1%
of the capture 37%, 0.1 to 0.3% 64%, 0.3 to 1% 79%, over 1% 96%. Finder is
the hard case: sidebar rows and toolbar buttons that are tiny in a large
window. Confidence at 0.5 keeps 83% of lookups at 77% precision; at 0.7, 62%
at 81%. Real windows are harder than the web pages of §2 (81 to 88%) and
easier than ScreenSpot's full desktops at single-stage (70%); the planner
baseline holds at over 90% here too.

## 6. Expectations in place of the frame diff

A replan's actions now carry an `expect` (what the screen shows once the
action has worked); after each one, Jev answers a Noul on the fresh capture
and that verdict, not the share of changed pixels, says whether the plan goes
on. On an empty, textless screen the loop also skips Jev's decision question
and asks the planner at once. The blind suite of §4, 3 reps:

| Arm | Passed | Planner calls / run | Jev decisions / run | Seconds / run |
|---|---|---|---|---|
| frame diff (§4, 18 runs) | 15/18 | 4.1 | 4.2 | 23 |
| expectations | 15/18 | 4.0 | 0 | 24 |

Every one of the 35 plans gave each action an expectation (2.8 actions a
plan). Of 60 checks (p50 276 ms, 3,362 tokens, $0.00014 each), 53 held at a
median p of 0.99 and 7 did not, every one at p=0.00 and every one on
`form_save`'s native `<select>`, whose open list the headless capture does
not draw: the checks were right both ways. The gain is not in the pass rate
(the same harness limit decides it) but in what a verdict means: a click that
opened the wrong menu changes the window too, and a frame diff cannot tell.
The removed decision calls pay for the checks in time.

## 7. A page that draws its own controls: the canvas environment

The blind runs of §4 and §6 strip a normal page's elements; `envs/canvas`
(Ledger, a dashboard drawn on one `<canvas>`, and a portal page that embeds it
in an iframe) is honestly blank: no DOM controls, no DOM text, the keyboard
handled by the canvas. On the DOM surface such a page now gets the pointer
path: the planner is offered pixel clicks and text to type when the page lists
no controls or shows an embedded frame, the surface brings the browser and its
tab forward, and the clicks and keystrokes go through the actuator. Six tasks
(ship an order through a confirmation sheet, three settings changes including
typing a name and picking from a drawn dropdown, a reading task, and the ship
task inside the iframe), 3 reps each, planner Claude Opus 5.5:

| Arm | Passed | Ended `done` | Planner calls / run | Seconds / run |
|---|---|---|---|---|
| DOM surface before (page-only planner schema) | 3/15 | 3/15 | 3.0 | 18 |
| DOM surface with the pointer path | **15/15** | 15/15 | 4.3 | 24 |
| the same task inside an iframe, no way around it | **3/3** | 3/3 | 5.0 | 29 |

Before, the planner answered every blind page with a wait and then gave up
("the operator cannot click Settings"); the three passes were the reading
task, which needs no click. After, Jev placed all 15 of its lookups (later
clicks of a plan: a 34 by 18 pixel toggle, a dropdown option, the Save button)
at confidence 0.57 to 0.97, and 48 expectation checks split 42 held and 6 not:
three were the click on Mark shipped that opened the confirmation sheet
instead of shipping, which is the right verdict and sent the planner back for
the sheet, and three were compound expectations ("the dialog is closed and
the order shows shipped") with a side panel still open, which is why the
planner is now told to expect one thing. The embedded-frame runs first ended
blocked at the sheet: two expectations in a row did not hold, and the stuck
count read that as no progress although each click had moved the run. The
stuck count now resets on any real change of the window; only the plan stops
at a failed expectation.

## What this justifies

- Planner coordinates stay primary on the screenshot the planner read.
- Jev places described targets on later captures, behind two gates
  (`VISIBLE_MIN` 0.5, `CONFIDENCE_MIN` 0.5), with the conditional second
  look (`SMALL_BOX` 0.5% of the image).
- An action's expectation, checked by Jev on a fresh capture, is progress;
  window change at 0.008% stands in when a plan gives no expectation. A plan
  stops at the first expectation that does not hold or target Jev cannot
  place.
- The end-state check gets the final capture when the screen has no text.
- A screen with no controls and no text goes to the planner without a
  decision call.
- A web page that draws its own controls, or shows an embedded frame, gets
  the pointer path: the browser is brought forward and the planner's clicks
  and keystrokes run as on a native app.

## Not measured

Native macOS apps with empty accessibility trees (the case this is for) have
not been driven end to end; §5 scores their controls one at a time, and the
blind web runs are the whole-task proxy. Jev's own rate limit and the image
feature's stability across releases ("the request format, the answers, and
the limits on this page can change between releases") are the operational
risks; `BOX_MODEL` pins `jev-1.14.0`.
