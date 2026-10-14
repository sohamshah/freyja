# Computer-use environments

Mock web environments for testing computer-use agents, with rewards read from the server's ground truth. They started from a real run where jev (`jev_computer_use`) tried to book two seats with a fare bundle on a US low-cost airline's site and failed or stalled on most pages. Each pattern that stopped it is rebuilt here as a switchable quirk. A fix can then be tested against the exact pattern it targets, and the other envs catch regressions.

Nothing here touches a real browser, window or account. Episodes run in headless Chromium driven over the DevTools protocol. Each episode gets a fresh browser context and a fresh server-side state, so episodes run in parallel while the Mac is in use.

## Environments

| Env | Pages | What it exercises |
|---|---|---|
| `airline` | search → flight select → passengers → seats → bags → add-ons → payment (`envs/airline/site/`) | the booking flow and its quirks, below |
| `vertex` | cloud-console model catalog, project picker, model-enablement questionnaire (`tests/fixtures/jev_live/`) | outlined fields, overlay selects, paged lists read across pages, a form that must not be submitted |
| `basics` | the other jev_live fixtures: forms, rows with repeated labels, modals, dropdowns, an editor, search, page dialogs, a login wall, a new tab, slow pages | regression coverage for the page surface |

The `vertex` and `basics` tasks are the `dom` scenarios of `scripts/jev_scenarios.py`, scored by the same checks. Those scenarios still run in Arc through `scripts/jev_harness.py`. Here they run headless.

### Airline quirks

All are on by default, except `trusted_toggle`. Switch any off with `--quirk name=off` to get the accessible version of that widget.

| Quirk | The page pattern |
|---|---|
| `promo_popup` | A promotion covers the page a moment after load. It sits in an open shadow root, and its close button has no name. Escape closes it. |
| `autocomplete` | Airport suggestions are list items with no role, no pointer cursor and a `tabindex=-1` wrapper. Only a click on one, or Down then Enter, commits the airport. Typed text alone commits nothing. |
| `readonly_travelers` | Travelers is a read-only input that opens a panel of add/remove buttons. The open panel covers Search. Escape and outside presses leave it open. Clicking the field again closes it, and so does focus moving to another field. |
| `image_search` | Search is a link around an image with no alt text. |
| `hidden_radio_fares` | Each fare is a box around a zero-size radio. The radios' labels repeat across columns ("Standard fare $186 …") or are vague ("Biz fare"). |
| `upsell_modal` | Continue at the standard price opens a membership upsell. It has no dialog role and is appended after a long result list. |
| `nested_scroll` | The flight list scrolls inside its own panel. |
| `change_commit` | Passenger fields reach the booking only on `change` (on blur), as with some form frameworks. |
| `active_passenger` | A seat goes to the active passenger, and picking a seat does not move on to the next passenger. |
| `iframe_offers` | The two insurance opt-outs live in a cross-origin frame, and both are required. |
| `slow_continue` | Continue on add-ons answers after a 6 s server wait. The address gains `#0` first. |
| `hidden_templates` | Closed slide-in panels parked off the right edge leave "Close" buttons behind. |
| `cookie_banner` | A cookie banner stays fixed at the bottom. |
| `trusted_toggle` (off) | The open Travelers panel closes only on a trusted click (a person's). Script clicks and focus moving to another field leave it open, as on the real site. |

Episodes can also expire. `session_timeout_s` sends the next request home with the booking cleared, and `session_age_s` starts the episode already idle.

## Tasks and rewards

`run.py list` shows every task. Airline tasks start on the page they are about, with the booking preset up to it, and use fictional passengers:

- `airline/search`, `airline/select`, `airline/passengers`, `airline/seats`, `airline/extras`, `airline/payment_read`, `airline/purchase_gate`
- `airline/e2e`: the whole booking as one goal.
- `airline/chain`: the same booking as six page-sized goals run one after another in the same tab, as a calling agent drives jev.
- Two known limits, tagged `limit`. `airline/extras`: no page surface can enter the cross-origin frame. `airline/search_trusted`: no page surface can close a trusted-only panel.

A check returns named marks read from the server's state and events, for example `outbound_business`, `p2_dob`, `out:1` (a seat), or `on_payment_page`. The reward is 1 only when every required mark holds and nothing forbidden happened. Forbidden: purchasing, joining a membership, choosing the Club Saver fare, or adding a paid extra. The marks also make a dense signal for partial credit.

`oracle.py` is a privileged scripted solver. It knows the pages and acts with trusted pointer and keyboard events. `tests/test_cua_envs.py` uses it to prove every task can be finished, with quirks on and off, and that an untouched page scores 0. A task the oracle cannot finish is a broken env, not a weak agent.

## Running

```sh
uv run python evals/computer_use/run.py list
uv run python evals/computer_use/run.py run airline --reps 3 --jobs 4 --tag after
uv run python evals/computer_use/run.py run airline/select --quirk upsell_modal=off
uv run python evals/computer_use/run.py run airline vertex basics --jev-root ../freyja-baseline --tag before
uv run python evals/computer_use/run.py report results/<before> results/<after>
uv run python evals/computer_use/run.py run airline/e2e --reps 5 --llm-calls 16
uv run python evals/computer_use/run.py serve                    # browse the mocks yourself
uv run python evals/computer_use/run.py serve airline/seats --port 8777
```

- `--jev-root` imports jev from another checkout, such as a baseline, while the envs stay the same.
- `--notes "..."` gives every run site notes, as a `jev-site-<host>` skill does in the app. Use it to test whether a note fixes a site behaviour before saving the note.
- `--llm-calls N` sets jev's LLM call budget per run (default: jev's own, 32).
- `CUA_ENVS=airline,widgets` limits every command to those envs (the fixture envs stay).
- `serve` prints one address per task, each a fresh episode at the page the task starts on, on a fixed port (8777 unless `--port`). Pages that find their episode by a cookie share one per host in a normal browser: the link opened last sets the episode that every tab of that env uses.
- Each episode's JSON has the run's LLM tokens (`llm_tokens`: input with the prompt cache's reads and writes, output).
- The keys (`TYPESAFE_AI_API_KEY` for Jev, and the LLM provider's key for jev's planner and check) come from the repo's `.env`.
- Results go to `results/<time>-<tag>/`: one JSON per episode, `summary.json`, the jev run logs under `runs/`, and a final screenshot per episode under `frames/`.

### How jev runs here

`jev_policy.py` runs the production operator (`bridge/tools/jev_operator`): the same snapshot script, decision questions, gates, LLM doors and stop rules. Only the bottom layer changes:

- page JavaScript goes over the DevTools protocol, in an isolated world as Arc runs it, instead of AppleScript
- the planner's screenshot is the page's own
- a click the planner places on a screenshot is a protocol mouse event
- the desktop (`freyja_native`) is a stub that fails loudly if anything reaches for it

### Step interface

`env.py` exposes `WebEnv.reset()` / `step(action)` for other policies: a screenshot agent, a scripted baseline, or a policy being trained.

- **Observations:** jev's numbered rows, the page text, the dialog over the page, and an optional screenshot.
- **Actions:** name a row: click, type, select, key, scroll, open_url.
- **Reward:** the change in the share of marks that hold after each step.

## Adding an environment

1. Put the site under `envs/<name>/site/`, and keep each quirk behind a switch.
2. Add `backend.py` with `init(episode)` and `backend(episode, method, name, query, body)` for the JSON routes.
3. Register it in `run.py:make_server`.
4. Add tasks with checks that read only server state and events.
5. Extend `oracle.py` and `tests/test_cua_envs.py`, so every task is proven solvable.

Never copy a real site's markup or branding. Rebuild the pattern.
