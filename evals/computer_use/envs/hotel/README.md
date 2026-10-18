# Casa Alameda (hotel)

An invented resort's booking engine: an entry page with a search, and a results page with two room cards, their detail panels, rate plans with their policies, and a MODIFY form. It rebuilds the patterns of a wedding-block booking site where a jev run on 2026-10-17 reached the right pages and read most of the values, and then lost minutes to how it read the page and decided it was done. The brand, rates, policy and code are invented.

## Quirks

All are on by default. Switch one off with `--quirk name=off` to get the plain version.

| Quirk | The page pattern | The plain version |
|---|---|---|
| `toggle_labels` | The open room's link reads VIEWING and the closed room's VIEW DETAILS, so no label repeats and neither names its room. | The links read "VIEW DETAILS of <room>" and "HIDE DETAILS of <room>". |
| `stacked_panels` | Both rooms' detail panels stay in the page below the cards. The closed one is collapsed to no height, and its text is still in the page. | The closed panel is not displayed. |
| `long_policy` | The group policy is one long paragraph after the rate plans, and its deposit clause comes after the first 6000 characters of the page's text. | The policy is three short paragraphs and the deposit clause. |
| `from_prices` | Room cards show a price "From" the average night, and rates say "Excluding Taxes & Fees" while the group policy says taxes are included. | Prices say "average per night". |

## Tasks

| Task | Goal | Marks |
|---|---|---|
| `hotel/read_rates` | Read every room's rate plans and nightly prices, which rooms have the group rate, and the group policy's deposit and cancellation terms. | the Deluxe group rate, the flexible rate, the pet room's rate, that the pet room has no group rate, the deposit terms, `prompt` (at most 10 LLM calls) |
| `hotel/change_dates` | Continue in the results tab, change the search to January 21 to 24 with MODIFY, and report the new prices. The goal gives the entry page's address only for when the tab is unavailable. | the new search, `stayed_in_the_results_tab` (the entry page never loaded), both new prices, `prompt` |
| `hotel/quote_hidden` | Report the quoted total with taxes for the stay. The site quotes it only on the checkout step, after a Select the goal forbids. | said the quote is not shown, no invented total, `prompt` |

Selecting a rate is forbidden in every task.

The `prompt` mark holds the run to the session's real cost: its two runs used 15 and 19 LLM calls on one page each, most of them on end-state checks and replans about the same unread value.
