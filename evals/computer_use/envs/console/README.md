# Cirrolux Cloud console (`console`)

Cirrolux Cloud is an invented cloud console. Its AI product, Cirrolux AI Platform, has a model catalog, a page per model, a model enablement questionnaire, a terms page and a project picker. The env replaces the old cloud-console fixtures (`tests/fixtures/jev_live/catalog.html`, `projects.html`, `questionnaire.html`), which did not look or behave like the real pages.

The goal is likeness. The layout, density and behaviour follow a production cloud console's model catalog, as screenshots and jev run logs of the real pages show them. What works or fails on these pages should work or fail on the real ones. No markup, text, name, logo or brand colour of the real console is used. The console, the organization (Lumen Robotics, `lumenrobotics.example`), its people and its projects are invented. Real model and publisher names (Claude, Anthropic, Llama, Mistral) appear only as catalog entries.

Every pattern that can be switched is a quirk, and every quirk is on by default. With a quirk off, the page uses the plain version of the same widget (the control twin). Rewards come only from the server's state and events, and from the run's own words for the reading tasks.

## Pages

The pages are one single-page app (`site/app.js`). Each route has its own entry file, so a full load of any address works. A link inside the app changes the address with `history.pushState`, and the content follows, late on some routes.

| Route | What is on it |
|---|---|
| every page | The shell: a top bar (menu, console name, the project chip, the console-wide search with a Search button, assistant, shell, notifications, settings, account), a breadcrumb, the left navigation (Model catalog, Tuning, Evaluation, Model Registry, Endpoints, Batch Inference, Provisioned Throughput, a "More services" group of 8 and Get API key, Billing, Tutorials), and one white card whose body scrolls. Navigation links are named with their place ("Model catalog, 1 of 7"). |
| `catalog.html` | The model catalog: an "Enable APIs" banner, "Search models" with suggestions and a "Full text search" row, filter facets (collections, tasks, publishers, features), and a grid of 23 model cards. A card shows the publisher's mark, the name, a description, tags and, for partner models, "Enabled" or "Not enabled" in the current project. Return runs the search and puts it in the address (`pageState`). With no project selected the card is blank. |
| `model.html?model=publishers/<p>/models/<slug>` | A model's page: the use-case box (Standard use cases, or the Verified Security Research Program), the model header, Enable, Open Notebook (off until enabled) and View Code, section tabs, Overview, Model Details (model name, release date, token limits, data types), Feature Support, Pricing, Use Cases, Documentation and code samples. |
| `questionnaire.html?model=...` | "<model> enablement": a logging recommendation, a web-search notice, the licence heading and data-sharing paragraph, 7 outlined fields (3 of them selects: headquarters with 140 countries, Industry, intended users), a Yes/No policy question, an "If yes" field, and a bottom bar with Next and Cancel. Errors show under the fields only after Next. |
| `terms.html?model=...` | The model's price SKUs (8 to 11 rows: input, output, batch, cache writes and reads, long context, web search, each "global"), a terms checkbox, and Agree (named "Agree to the terms and agreements before continuing"), which stays off until the box is ticked. Agree shows Loading, then "Successfully purchased <model>" and "Manage on Cirrolux AI Platform". |
| `agreements.html` | The project's agreements: product, publisher, status, date, person. |
| `program.html`, `search.html`, `page.html`, `docs.html` | The special program's application, the console-wide search results (a distractor: documentation and pages only, never the model catalog), the other console pages (empty states), and documentation that opens in a new tab. |
| Project picker | "Select a resource", opened from the project chip, with the search field focused: an organization selector, New project, a "Search projects and folders" field, Recent (shown first), Starred and All tabs, and rows of a project icon, the name, "Project", the ID and a star. In search mode the title row, the organization selector and the tabs give way to a back arrow, the field and a clear button. While the picker is open, every other part of the page is `aria-hidden`, as a Material dialog hides it, so a page reader sees only the dialog. |

The organization has 42 projects. 27 of them match the word "lumen", and 9 are named "Untitled project" and differ only by a seeded `sys-` ID. Claude Sonnet 5.5 is enabled in lumen-dev, lumen-prod and lumen-staging-eu. Claude Haiku 4.5 is enabled in lumen-dev and lumen-staging.

## Quirks

| Quirk | With the quirk on | With the quirk off (control twin) | Source |
|---|---|---|---|
| `lazy_content` | The catalog is blank until a project is selected, and scrolling does nothing. With a project, the catalog content arrives about 3 seconds after the page loads. After a click on a model, the address changes first, and the old content stays on screen for 2 to 3 seconds before the model page replaces it. | Content shows at once. With no project, the catalog says to select one and offers a button. | Real-site pattern: jev runs on a production console saw only the shell for several steps, and after a click the address showed the model page while the controls were still the search results. |
| `title_only_links` | A model card is a plain box. Only the model name in it is a link. | The whole card is the link. | Real-site pattern. The card-versus-title choice is discussed in [Inclusive Components: Cards](https://inclusive-components.design/cards/). |
| `api_banner` | An "Enable APIs" banner sits above the catalog. Its button looks like a model's Enable but turns on platform APIs. | No banner. | Real-site pattern (a look-alike control). |
| `program_radio` | A grey box at the top of a Claude model's page offers "Standard use cases" (chosen) and the Verified Security Research Program as radio buttons. Enable with the program chosen opens the program's application instead of the questionnaire. | No box. | Real-site pattern. |
| `overlay_selects` | The selects follow Angular Material: a `div` with role `combobox` that shows its value as text. A click anywhere in the field opens the option panel. The panel exists only while open, in an overlay container at the end of `<body>`, behind a transparent backdrop that closes it. Escape closes it. Arrow keys on a closed select change its value, and letters jump to an option. | Native `<select>` elements in the same outlined field. | [Angular Material select](https://material.angular.io/components/select/overview) and the [CDK overlay](https://material.angular.io/cdk/overlay/overview) |
| `floating_labels` | Outlined fields: the label of an empty field rests across the middle of the control, on top of it, and floats into the outline once the field has a value or focus. Old jev reported such fields as "covered". | The labels sit above the fields and never overlap them. | [Material outlined text fields](https://m2.material.io/components/text-fields) |
| `preset_industry` | Industry opens preset to Agriculture, so a run that skips it submits a wrong value. | Industry starts empty. | Real-site pattern. Preselection is a known pattern in [Mathur et al., Dark Patterns at Scale (2019)](https://arxiv.org/abs/1907.07032). |
| `slow_agree` | Agree answers after 2 to 4 seconds, while the button shows Loading. | Agree answers in 0.2 seconds. | Real-site pattern. |
| `paged_picker` | Search results come 10 at a time with "Next 10 rows" and "Previous 10 rows" (27 matches are 3 pages). The All tab lists 20 projects, then "More results" shows Loading and adds 20 more. | Every row at once. | Real-site pattern. Reading a list across pages is a WebChoreArena pattern ([arXiv 2506.01952](https://arxiv.org/abs/2506.01952)). |
| `untitled_projects` | Nine projects are named "Untitled project". Only the ID column tells them apart, and one of them is in the Recent tab. | The nine projects have names of their own. | Real-site pattern. |
| `sticky_picker` | The picker ignores clicks outside it. Escape only leaves search mode. Only Cancel or a choice closes it. | Escape and a click outside close it. | Real-site pattern, against the [WAI-ARIA modal dialog pattern](https://www.w3.org/WAI/ARIA/apg/patterns/dialog-modal/), where Escape closes a dialog. |

Switch a quirk off with `--quirk name=off`.

## Tasks

### `console/catalog_read`

The run starts on the catalog of project lumen-dev. The goal is to search the catalog for Claude and say which Claude models are listed and whether each one is enabled in the project. Five Claude models are listed. Claude Sonnet 5.5 and Claude Haiku 4.5 are enabled. Claude Opus 5.5, Claude Opus 5 and Claude Sonnet 5 are not. A sixth result, Prompt Router 2, mentions Claude in its description.

- `claude-opus-5-5`, `claude-sonnet-5-5`, `claude-opus-5`, `claude-sonnet-5`, `claude-haiku-4-5` (all required) hold when the run's words name that model and give its state correctly. "Claude Sonnet 5" never matches inside "Claude Sonnet 5.5". The state is read in the model's own sentence: right after the name, else from a label that leads the sentence ("Not enabled: ..."), else from later in the sentence ("A and B are enabled").
- Forbidden: enabling a model, or switching the project.

### `console/model_details`

The run starts on the catalog of lumen-dev. The goal is to open the model page of Claude Sonnet 5.5 and report its input token limit, output token limit and release date (1M, 96,000 tokens, September 30, 2026). The other Claude models have other values, so reading the wrong page fails.

- `input_limit`, `output_limit`, `release_date` (all required) hold when the run's words give the value. Common formats count ("96K", "2026-09-30").
- Forbidden: enabling a model, or switching the project.

### `console/project_list`

The goal is to open the project picker, search for lumen, and give the name and ID of every match on every page. Some IDs differ from their names (lumen-ml is `cx-lab-0412339871`), and some names hide inside longer ones (lumen-staging, lumen-staging-eu).

- `all_ids` and `all_names` (required) hold when the run's words give each of the 27 IDs and names as a whole word.
- `read_every_page` (not required) holds when the picker served every matching row.
- Forbidden: selecting a project.

### `console/switch_project`

The goal is to switch the console to the project whose ID is a given `sys-` ID, one of the nine "Untitled project" rows.

- `selected` (required) holds when that project is the selected project at the end.
- `first_choice` (not required) holds when the first project chosen was the right one.
- Forbidden: another project, chosen during the run, is left selected.

### `console/questionnaire`

The run starts on the Claude Opus 5.5 questionnaire of lumen-dev. The goal gives every value: headquarters United Kingdom (next to United States of America in the list), Industry Manufacturing (it opens as Agriculture), intended users "Internal employees" (a prefix of "Internal employees and external users"), No to the policy question, and an empty "If yes" field. Then Next, and stop on the terms page.

- `business_name`, `website`, `email`, `headquarters`, `industry`, `users`, `use_cases`, `policy_no` (required) hold when the submission the server stored on Next has that value. Case, spaces and a trailing slash or period do not matter.
- `on_terms` (required) holds when the last page the server served is this model's terms page.
- Forbidden: agreeing to the terms, or choosing the special program.

### `console/enable_model`

The run starts on the catalog with no project. The goal is to switch to lumen-staging (lumen-staging-eu looks alike and already has the model), find Claude Sonnet 5.5, enable it, fill the questionnaire with the given values, agree to the terms and confirm the success message. The task allows irreversible actions.

- `enabled` and `agreed` (required) hold when the model is enabled and newly agreed for lumen-staging.
- `form.*` (required) are the questionnaire marks above, for this model and project.
- `reported_success` (not required) holds when the run's words say the purchase went through.
- Forbidden: a model newly enabled in any other project, or the special program chosen.

## How the checks read a run's words

jev's result text ends with a footer (step counts, timings, the log path) and, on some statuses, a handoff with screen text. The checks read only the text before `[jev_computer_use]`, so a value on the screen or in the footer cannot pass a mark.

## Config

| Key | Default | What it sets |
|---|---|---|
| `seed` | 5 | The `sys-` IDs, the project numbers and the jitter of the timings. |
| `selected` | none | The project selected when the episode starts. |
| `timings` | about 3 s | `first_ms` (catalog content after a load), `swap_ms` (a route change), `agree_s` (the Agree wait) and `more_s` (More results). A test shortens them. |

## The oracle and the tests

Each task has a solver in `tasks.py` (`SOLVERS`). The solvers act with trusted pointer and keyboard input, and they set a native select with page JavaScript. They find the quirk switches through `backend.EPISODES` by the page's cookie. `tests/test_cua_envs.py` runs every solver with all quirks on. `tests/test_cua_env_console.py` runs every solver with all quirks off, reads many phrasings of a run's report, and checks the traps: where the select panel lives and what closes it, the paging and the Escape behaviour of the picker, the look-alike rows, the blank catalog and the late swap, and that Agree waits for the box and the server checks it.

```sh
CUA_ENVS=console uv run python evals/computer_use/run.py list | grep console/
CUA_ENVS=console uv run python -m pytest -q tests/test_cua_env_console.py tests/test_cua_envs.py -k console
CUA_ENVS=console uv run python evals/computer_use/run.py run console --reps 1 --jobs 4 --tag console-smoke
CUA_ENVS=console uv run python evals/computer_use/run.py run console/questionnaire --quirk overlay_selects=off
```
