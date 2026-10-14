# Widgets env: Tessaly Console

Tessaly Console is an invented admin console for an invented company. Each page hosts one of the widget patterns that computer-use agents fail most often in recent benchmarks, so a fix can be measured against the exact pattern it targets. Every pattern is reimplemented here in plain JavaScript from its published description, and no code was copied from the sources listed at the end.

Rewards come only from what the server saved or logged. The pages never show a success banner, an answer or a secret for an agent to find. Generated data is seeded (`seed` in the task config, 7 by default), so every episode with the same seed is the same.

## Pages

| Page | File | What it shows | Task |
|---|---|---|---|
| Products | `catalog.html`, `product.html` | 36 products, a two-thumb price slider with an Apply button, and a sort menu | `price_slider` |
| Notifications | `notifications.html` | Two settings cards, Personal and Team, built from one template | `twin_cards` |
| Release checklist | `checklist.html` | Eight cards to put in order, and a Save order button | `reorder_menu`, `reorder_keyboard`, `reorder_drag` |
| New account | `signup.html`, `account.html` | A long sign-up form for a customer account | `business_signup` |
| Members | `members.html` | A table of 2,000 members, sorted by email | `virtual_list` |
| Workspace | `workspace.html` | A menu bar with File, Edit, View and Help | `hover_menu` |

The release checklist has a mode in the task config (`mode`: `menu`, `keyboard` or `drag`), because the three reorder tasks share one page.

## Quirks

Every trap is a quirk, and every quirk is on by default. With a quirk off, the page uses the accessible version of the same widget, so one fix can be measured against the pattern it targets. Switch a quirk off with `--quirk name=off`.

| Quirk | The pattern (on) | The accessible version (off) | Source |
|---|---|---|---|
| `slider_only` | The price filter is a two-thumb slider. Each thumb is a focusable `div role=slider` with `aria-valuemin`, `aria-valuemax` and `aria-valuenow`, and it moves in $10 steps by Up and Down (also Left, Right, Page Up, Page Down, Home and End) or by a pointer drag. The thumbs cannot cross. The results change only when "Apply (n results)" posts the range. | Min and Max number inputs sit next to the slider, and typing into them moves the thumbs. | ComponentBench (range sliders, committed state) |
| `twin_labels` | The Personal and Team cards have identical "Weekly digest" switches (`role=switch`) and identical "Save" buttons. CSS shows Team first, while the DOM holds Personal first. | The controls say which card they belong to, e.g., "Team weekly digest" and "Save team settings". | ComponentBench (duplicate widget instances) |
| `no_move_buttons` | A card moves only through the mechanism of the task's mode: its "⋯" menu, the keyboard handle, or a pointer drag. | Each card also has "Move up" and "Move down" buttons named for the card, which is the single-pointer alternative that WCAG 2.5.7 asks of drag interfaces. | ComponentBench (drag lists) |
| `unnamed_icons` | The "⋯" button and the drag handle are icons with no accessible name, and the "⋯" menu has no menu roles. | The "⋯" button is named "Actions for Smoke test", the handle is named "Reorder Smoke test", and the menu has `role=menu` with `role=menuitem` items. | MERJ (unnamed icon buttons) |
| `late_errors` | Create account waits about 2 s on the server. Then the page lists the errors only in a banner at the top of the page, and the button returns to normal. | The VAT field is checked as you type, and every error, from the page or the server, shows under its field with `aria-invalid`. | UI-CUBE (real-time validation, conditional fields) |
| `no_search` | The members table has no filter box. The table is virtualized, so only about 25 rows near the visible window are in the DOM at once, and the target row exists in the DOM only when it is scrolled near the window. | A filter box above the table matches names and emails. | MERJ (virtual scroll), ComponentBench (virtual lists) |
| `hover_only` | Inside View, "Security ▶" opens its submenu only on `pointerenter`, and the submenu's items are added to the DOM only then. The submenu closes 300 ms after `pointerleave`. A click or a key on "Security ▶" does nothing. | A click on "Security ▶" opens the submenu and keeps it open until the next click. Enter, Space and Right open it too. | WebGames ("Menu Navigator") |

Some parts of each page stay the same in both versions. For example, the postcode lookup on the sign-up form fills City about 2 s after the postcode field loses focus, and the members table stays virtualized when the filter box is shown.

## Tasks and marks

A task's reward is 1 when every required mark holds and nothing forbidden happened. The marks also give partial credit. Informational marks are reported, but they are not required.

**`widgets/price_slider`** starts on `catalog.html`. The goal is "On the products page, show only items priced from $120 to $340, apply the filter, and open the cheapest item in that range."

- It tests exact values on a range slider whose results change only on Apply. The products include $119 just below the range, $120 on its lower bound, and $340 and $345 at its upper bound.
- Required marks are `filter_applied` (the last range the server got is 120 to 340) and `opened_cheapest` (the server served the $120 product, Portable SSD, 500 GB).

**`widgets/twin_cards`** starts on `notifications.html`. The goal is "Turn off the weekly digest for the Team workspace only, and save the team settings."

- It tests duplicate widget instances whose screen order differs from their DOM order.
- Required marks are `team_digest_off` (the saved Team setting) and `team_saved`. The informational mark `team_rest_unchanged` shows whether other Team switches changed.
- A save of the Personal card is forbidden, and so are Personal settings that differ from the start.

**`widgets/reorder_menu`**, **`widgets/reorder_keyboard`** and **`widgets/reorder_drag`** start on `checklist.html`, in the modes `menu`, `keyboard` and `drag`. The goal is "In the release checklist, put 'Smoke test' right after 'Deploy to staging', then click Save order."

- Smoke test starts two places below where it belongs.
- In `menu` mode, each card's "⋯" button opens Move up, Move down and Move to top.
- In `keyboard` mode there is no menu. A person focuses a card's handle, and a click on the handle focuses it. Enter picks the card up, Up and Down move it, and Enter drops it. A live region announces each move, and a visible hint on the page describes the keyboard steps.
- In `drag` mode, only a trusted pointer drag (`pointerdown`, moves and `pointerup`, each with `isTrusted`) moves a card.
- Required marks are `order` (the order the server saved is the expected one, with every other card where it was) and `saved`.
- A save is forbidden when its posted order differs from the server's own copy of the order. The server applies each move that the page's controls make, so the check catches cards that a script moved around those controls.
- `reorder_drag` is tagged `limit`, because no page surface can drag.

**`widgets/business_signup`** starts on `signup.html`. The goal is "Create a business account for Lumen Ltd with the email ada@example.com, VAT ID GB 123 4567 89 and postcode AL1 3AW. Keep the city the postcode lookup fills in."

- It tests a conditional field below the fold (Business adds a required VAT ID field at the end of the form), a lookup that changes another field about 2 s after the postcode field loses focus, and errors that arrive late and away from the field. The server rejects the VAT ID with spaces ("VAT ID: remove the spaces").
- Required marks are `created`, `business`, `vat` (stored as GB123456789), `city` (Elmsworth, the lookup's value) and `email`. The informational mark `company` shows whether the name came through as "Lumen Ltd".

**`widgets/virtual_list`** starts on `members.html`. The goal is "Suspend the member whose email is grace.hopper.1437@example.com. Do not suspend anyone else."

- The target is row 477 of 2,000, and the rows near it hold members with the same name and similar emails, e.g., grace.hopper.1347, grace.hopper.1473 and grace.hooper.1437. Every row's button reads "Suspend", and the name column alone does not tell the rows apart.
- The required mark is `suspended_target`.
- Suspending any other member is forbidden, even when the agent reactivates that member afterwards.

**`widgets/hover_menu`** starts on `workspace.html`. The goal is "Use the View menu to turn on Security > Show passwords."

- It tests a submenu that only a hovering pointer opens.
- The required mark is `show_passwords` (the server setting).
- Choosing any other menu item is forbidden. Opening menus and submenus is allowed.
- The task is tagged `limit`, because no page surface can hover.

## The oracle

`SOLVERS` in `tasks.py` solves every task with trusted input and page JavaScript. It types into the Min and Max inputs when they exist, and otherwise it presses Up and Down on the focused slider thumbs. It moves cards with the labelled buttons when they exist, and otherwise with the mode's own mechanism. For `reorder_drag` it drags with DevTools `Input.dispatchMouseEvent`, and for `hover_menu` it hovers with the same call. `tests/test_cua_envs.py` runs the oracle with the default quirks. `tests/test_cua_env_widgets.py` runs it with every quirk off, checks the forbidden side effects on the server, and checks that each trap holds against events a page script makes.

## Notes from jev's DOM surface

The following observations come from driving these pages with jev's production snapshot script (`bridge/tools/jev_operator/dom_snapshot.js`) and no LLM. They explain the failures a run is likely to show.

- A slider thumb is listed as a plain button, e.g., "Minimum price", with no value. A click on it focuses it, and jev's Up and Down keys move it, so jev can set the range only by reading the page text and pressing a key twelve and then sixteen times.
- jev refuses to press Enter when the focused element is a plain button (`no_editable_focused` in `doKey`). The keyboard handle in `reorder_keyboard` is a button, as in common drag-and-drop libraries, so jev cannot pick a card up today.
- jev scrolls the largest scroll container when the page itself cannot scroll. It moves the members table by 80% of the window height, which is 20 rows, and the target row first appears after 23 scrolls.
- jev names each row's "Suspend" button by the row's first cell, so the rows of the members who share the name Grace Hopper all read "Suspend (Grace Hopper)". Only the order of the page text, or a screenshot, tells them apart.
- jev does not read text above the visible area. After a failed submit at the bottom of the sign-up form, the error banner at the top is out of its view.
- A script click on "Security ▶" sends `pointerover` and `mouseover`, but not `pointerenter`, so the submenu stays closed. A planner click on the screenshot is a trusted pointer event that first moves the pointer over "Security ▶", so such a click can open the submenu.

## Running

```sh
CUA_ENVS=widgets uv run python evals/computer_use/run.py list
CUA_ENVS=widgets uv run python evals/computer_use/run.py run widgets --reps 1 --jobs 4 --tag widgets-smoke
CUA_ENVS=widgets uv run python evals/computer_use/run.py run widgets/price_slider --quirk slider_only=off
CUA_ENVS=widgets uv run python evals/computer_use/run.py serve widgets
CUA_ENVS=widgets uv run python -m pytest -q tests/test_cua_envs.py -k widgets
CUA_ENVS=widgets uv run python -m pytest -q tests/test_cua_env_widgets.py
```

## Sources

- ComponentBench (August 2026, MIT license), <https://arxiv.org/abs/2608.18307>. The patterns taken from it are committed state behind Apply or Save, duplicate widget instances, and exact target values. Agents had their lowest success on range sliders, drag lists and virtual lists.
- UI-CUBE (November 2025), <https://arxiv.org/abs/2511.17131>. The patterns taken from it are forms with real-time validation and conditional fields.
- WebGames by Convergence (Apache-2.0 license), <https://arxiv.org/abs/2502.18356>. The pattern taken from it is the nested menu that opens only on hover, from the "Menu Navigator" game.
- MERJ agent-interaction research, <https://merj.com/blog/agent-interaction-research>. The patterns taken from it are a virtual scroll where the target row is not in the DOM, and icon buttons with no name.
- The accessible versions follow the WAI-ARIA multi-thumb slider pattern, <https://www.w3.org/WAI/ARIA/apg/patterns/slider-multithumb/>, and WCAG 2.5.7 Dragging Movements, <https://www.w3.org/WAI/WCAG22/Understanding/dragging-movements.html>.

Tessaly and Tessaly Console are invented names, and so are the towns the postcode lookup returns. The customer, the people and the email addresses are fictional, and the email addresses use `example.com`.
