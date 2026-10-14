# Quaymark Ops (`ops`)

Quaymark Ops is an operations app for an invented company. The env ports time, change and long-horizon patterns from recent computer-use benchmarks, and each pattern was reimplemented here in plain JavaScript and Python, with no code copied. Every pattern that can be switched is a quirk, and every quirk is on by default. With a quirk off, the page uses the plain version of the same widget (the control twin), so a fix can be measured against the exact pattern it targets. Rewards come only from the server's state and events, and from the run's own words for the tasks that ask for a report.

## Pages

| Page | What is on it |
|---|---|
| `queue.html` | The support queue dashboard, with six number tiles, a Refresh button, an "Updated" time, and the on-call card with a "Page on-call" button. |
| `orders.html` | A purchase order form (item, quantity, unit price, vendor), the open POs with an Edit button each, the POs received this month, and the #purchasing team chat. |
| `expenses.html` | A table of 8 receipts, a one-record expense form, and the saved expenses. |
| `reviews.html` | The rating summary of the Harbor cordless kettle and its 140 reviews, 20 more per "Load more reviews" click. |
| `index.html` | Links to the four pages. No task starts here. |

The app's clock reads 09:00:00 when an episode starts. All people are fictional or historical names (Ada Lovelace, Alan Turing, Grace Hopper, Katherine Johnson), and the vendors, merchants and product are invented.

## Quirks

| Quirk | With the quirk on | With the quirk off (control twin) | Source |
|---|---|---|---|
| `manual_refresh` | The numbers change only when someone clicks Refresh. The "Updated" time shows when the page last asked the server. | The page refreshes itself every 5 seconds and says so. | SentinelBench |
| `near_miss` | Before the crossing, the support backlog holds one or two tickets under the +10% line for a few seconds. On the quiet task it does so twice inside the two minutes and never crosses. | The backlog stays within 3% of its start until it crosses. | SentinelBench, which scores when the agent acted |
| `late_message` | The team lead's message arrives 1.5 seconds after the second PO is saved, so the two saved POs must be edited. | The message is in the chat when the page opens, before the first PO. | OSWorld 2.0 and InterruptBench |
| `sticky_fields` | "Save & add another" keeps the last currency and category. | Every field is cleared after a save. | OS-Marathon |
| `slow_save` | An expense save answers after 2 to 4 seconds. The button stays enabled and the page shows nothing until the answer, so a second click saves a duplicate. | A save answers in 0.3 seconds, and the button is disabled and reads "Saving…" while it waits. | A common web pattern (a double submit) |
| `timeout_dialog` | After the fifth saved expense, a "Stay signed in? (60 s)" dialog counts down above the form. It does not block the form. At zero, the page clears every field of the form and says that unsaved changes were discarded. | There is no dialog. | MERJ |
| `virtualized` | The review list keeps only about 25 rows in the DOM, the rows near the scroll position. | Every loaded review is in the DOM. | MERJ and WebChoreArena |

Switch a quirk off with `--quirk name=off`.

## Tasks

### `ops/monitor_backlog`

The goal is "Watch the support backlog on the queue dashboard. When it is at least 10% above the value it had when you started, page the on-call engineer. Do not page before that."

The server holds a timeline for the episode, and its clock starts when the episode starts. The support backlog starts at a seeded value between 380 and 460, and it moves every 4 seconds within 3% of that value. At a seeded time between 20 and 40 seconds, it rises to at least 10% above the start and stays above. The line is the smallest whole number of tickets that is at least 10% above the start, e.g., 424 for a start of 385. The other tiles move a little and never matter.

- `paged_in_time` (required) holds when the first page came at or after the crossing, and within 60 seconds of it.
- `paged_early` (forbidden) is any page before the crossing.

### `ops/monitor_quiet`

The goal is the same, plus "If it has not happened within 2 minutes, stop and tell me so." The backlog does not cross within 3 minutes. It crosses 5 to 15 seconds after the 3-minute mark, so a run that keeps watching past its two minutes and then pages is caught.

- `watched_window` (required) holds when the dashboard was read again at or after 90% of the two minutes (108 seconds). A run that reports at once has not watched, so it fails this mark.
- `reported_no_trigger` (required) holds when the run's words say that the rise did not happen.
- `paged` (forbidden) is any page.

### `ops/rule_change`

The goal lists three POs, all from Basalt Supply: 12 pallet wrap rolls at $18.50, 30 safety glove boxes at $14.25, and 8 label printer ribbons at $22.00. It ends with "Follow any instructions your team lead posts in the chat while you work."

After the second PO is saved, Grace Hopper (team lead) posts "Change of plan: Basalt Supply is suspended. Move every open PO to Cobalt Parts, and no PO over $400. Keep the unit prices; if a PO would go over $400, cap its quantity at the most units that keep its total at or under $400." The glove PO is $427.50, so its quantity must drop to 28 boxes ($399.00). The POs received this month cannot be edited, so "every open PO" means the three new ones. The server enforces neither the suspension nor the cap.

- `three_pos` (required) holds when there are exactly three open POs, one per listed item, with the listed quantity and unit price (28 or 30 for the gloves).
- `all_cobalt` (required) holds when all three POs are from Cobalt Parts.
- `all_under_cap` (required) holds when every total is $400 or less.
- `po2_amended` (required) holds when the glove PO has 28 boxes at $14.25 from Cobalt Parts, and its last save came after the message.
- `basalt_after_message` (forbidden) is any save, new or edit, with Basalt Supply more than 2 seconds after the page showed the message. A later fix does not undo it.

### `ops/sticky_form`

The goal is "Enter the 8 expenses from the table into the expense form, one record each." The currencies and categories of the 8 receipts follow a fixed plan. With kept values, some records are right without a change, and others are wrong in exactly one or both fields. The merchants, amounts and dates come from the seed. Saved expenses go straight to the report, and the page offers no way to edit or remove them.

- `count_8` (required) holds when exactly 8 expenses are saved.
- `rec1_ok` to `rec8_ok` (required) hold when a saved expense matches the receipt in all five fields. The date accepts YYYY-MM-DD or MM/DD/YYYY, and the amount accepts a currency sign and commas.
- `duplicate` (forbidden) is one receipt saved twice, matched by merchant.

### `ops/count_reviews`

The goal is "How many 1- or 2-star reviews of the Harbor kettle mention battery? Tell me the number." The page shows 140 reviews, newest first. 36 have 1 or 2 stars, and 11 to 15 of those mention the battery, depending on the seed. At least two of them are on the last page, and at most three are on the first. Twelve reviews with 3 or more stars also mention the battery, and the summary shows the count for each star rating. Every review that mentions the battery uses the word "battery".

- `answer` (required) holds when the right number appears in the run's words, as digits or as a word.
- `loaded_all` (not required) holds when the run loaded the last page.

## How the checks read a run's words

jev's result text ends with a footer (step counts, timings, the log path) and, on some statuses, a handoff with screen text. The checks read only the text before `[jev_computer_use]`, so a number or a phrase in the footer or on the screen cannot pass a mark.

## Config

| Key | Default | What it sets |
|---|---|---|
| `seed` | 11 | Every generated value: the backlog, the receipts and the reviews. |
| `timeline` | per task | `cross_s` (a range in seconds), `window_s`, `tick_s`, `quiet_s` and `report_after_s` for the queue. A test shortens these. |
| `stay_s` | 60 | The length of the "Stay signed in?" countdown. |
| `message_delay_s` | 1.5 | Seconds from the second saved PO to the team lead's message. |

## The oracle and the tests

Each task has a solver in `tasks.py` (`SOLVERS`). The solvers act with trusted pointer and keyboard input. The quiet solver needs the length of the watch window, and the expense solver needs to know whether the countdown is on. Both find their episode through `backend.EPISODES` by the page's cookie, so the page never carries the timeline or the quirk switches. `tests/test_cua_envs.py` runs every solver on the real timelines, so the quiet task takes about two minutes there. `tests/test_cua_env_ops.py` runs every task with all quirks on and with all quirks off on timelines shortened through the task config. It also checks the traps, e.g., that a second click during a slow save saves twice and that the countdown clears the form.

```sh
CUA_ENVS=ops uv run python evals/computer_use/run.py list | grep ops/
CUA_ENVS=ops uv run python -m pytest -q tests/test_cua_env_ops.py tests/test_cua_envs.py -k ops
CUA_ENVS=ops uv run python evals/computer_use/run.py run ops --reps 1 --jobs 4 --tag ops-smoke
CUA_ENVS=ops uv run python evals/computer_use/run.py run ops/sticky_form --quirk slow_save=off
```

## Sources

The patterns come from these sources. No code was copied from them.

- SentinelBench (Microsoft Research, 2026), [arXiv 2606.05342](https://arxiv.org/abs/2606.05342), code at [microsoft/sentinel_environments](https://github.com/microsoft/sentinel_environments) under the MIT license. The source pattern is monitoring tasks driven by a server timeline, scored by when the agent acted, where some conditions never trigger. The two monitor tasks use it.
- OSWorld 2.0 (2026), [arXiv 2606.29537](https://arxiv.org/abs/2606.29537). The source pattern is chat messages that arrive during a task and override its rules, and state the agent must infer. The `rule_change` task uses it, and the POs saved before the message are the inferred state.
- InterruptBench (2026), [arXiv 2604.00892](https://arxiv.org/abs/2604.00892). The source pattern is a change to the goal during a task. The team lead's message in `rule_change` changes the goal after work has started.
- OS-Marathon (2026), [arXiv 2601.20650](https://arxiv.org/abs/2601.20650). The source pattern is long repetitive data entry, where the accuracy of single fields stays high but whole records fail. The `sticky_form` task scores whole records.
- WebChoreArena (2025), [arXiv 2506.01952](https://arxiv.org/abs/2506.01952). The source pattern is counting and memory across "Load more" pages. The `count_reviews` task uses it.
- MERJ agent-interaction research, [merj.com/blog/agent-interaction-research](https://merj.com/blog/agent-interaction-research). The source patterns are a virtual scroll list and a countdown that expires in the middle of a flow. The `virtualized` and `timeout_dialog` quirks use them.
