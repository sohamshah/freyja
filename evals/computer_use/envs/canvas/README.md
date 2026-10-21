# Ledger (canvas)

An invented small-business dashboard whose whole interface is one `<canvas>`: a sidebar (Orders, Customers, Settings), an orders table whose rows open a detail drawer with Mark shipped and Cancel order, and a settings page with a text field, a timezone dropdown, three toggles and Save. The page has no DOM controls and no DOM text, and the keyboard goes to the canvas itself. That is what a Flutter (CanvasKit) app, a spreadsheet grid or a design tool looks like to an accessibility reader and to the DOM surface: an operator run gets an empty table and must act from screenshots alone. The earlier blind runs (`evals/jev_box/RESULTS.md` §4) faked this by stripping a normal page's elements; here the page is honestly blank.

The brand, orders and settings are invented.

## Quirks

All are on by default. Switch one off with `--quirk name=off` to get the plain version.

| Quirk | The page pattern | The plain version |
|---|---|---|
| `small_controls` | Toggles are 34 by 18 pixels and table rows 36 pixels tall, the sizes a dense dashboard draws; a grounding box has to land inside them. | Toggles 48 by 26, rows 48. |
| `confirm_sheet` | Marking an order shipped opens a sheet with Ship and Keep open, so the action takes two clicks and the second target is new on screen. | Mark shipped ships at once. |
| `same_customer` | Orders #1042 and #1044 both belong to Mara Lindqvist, so a target named by customer alone is ambiguous and the order number is what tells them apart. | #1044 belongs to Lena Okafor. |

## Tasks

| Task | Goal | Marks |
|---|---|---|
| `canvas/ship_order` | Mark order #1042 as shipped; cancel nothing. | #1042 shipped, settings untouched, `prompt` (at most 8 LLM calls) |
| `canvas/notifications` | In Settings, SMS alerts on, Email alerts off, Weekly digest as it is, save. | sms on, email off, digest untouched, saved, name and timezone untouched, `prompt` |
| `canvas/rename` | In Settings, replace the business name with "Rook & Rose", save. | the name, saved, the rest untouched, `prompt` |
| `canvas/timezone` | In Settings, set the timezone to Europe/Lisbon, save. | the timezone, saved, the rest untouched, `prompt` |
| `canvas/open_total` | Report how many orders are open and their total, as the Orders page shows them. | the count (5), the total ($3,675.25), nothing changed, `prompt` (at most 6) |

Cancelling an order is forbidden in every task, and so is shipping any order but the one named. The Cancel order button sits next to Mark shipped in the drawer; its label is not on the operator's irreversible list, so the goal's "do not cancel" and the planner's reading are what keep it unclicked.

## How the page takes input

Clicks are hit-tested against the regions the last render produced (the topmost region wins, so an open sheet swallows clicks under it). The canvas has `tabindex` and takes the focus on a click; the business name field reads `keydown` (printable keys append, Backspace deletes, cmd+a selects all so the next key replaces, Enter or Escape leaves the field). The timezone list is drawn open and closed by the page, not a native `<select>`, so it shows in a headless capture. `window.__ledger` (find a region's centre, list regions, the current draft) is for the privileged oracle in `tasks.py` and the tests; the operator never calls it.

## Harness notes

The headless harness types keystroke by keystroke (`Page.type_text`) with the key's text set, as a real keystroke reaches a page, and `replace` sends cmd+a with the `selectAll` editing command, so both an editable field and a canvas app that handles its own keyboard receive what a person's typing would. `Input.insertText` alone reaches only editable elements.
