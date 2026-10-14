"""Tasks on the Cirrolux Cloud console. Goals are written the way a calling
agent writes them, with an invented company, people and projects. Every check
reads the server's state and events; the reading tasks also read the run's own
words, never jev's footer or handoff."""

from __future__ import annotations

import json
import re
from typing import Any

from ...tasks import Check, Task, result
from . import backend as B

State = dict[str, Any]
Events = list[dict[str, Any]]

DEV = "lumen-dev-482913"
STAGING = "lumen-staging"
OPUS55, SONNET55 = "claude-opus-5-5", "claude-sonnet-5-5"
CLAUDE = [(m["slug"], m["name"]) for m in B.MODELS if m["pub"] == "anthropic"]
LUMEN = [(n, i) for n, i in B.LUMEN]  # the 27 projects that match "lumen"
SWITCH_TARGET = B.untitled_ids(5)[6]

# ─── reading the run's words ──────────────────────────────────────────────


def said(run: dict[str, Any]) -> str:
    """The run's own words: its summary and the values it read. jev's footer
    (step counts, timings, the log path) and handoff (screen text) are cut, so a
    phrase there cannot pass a check."""
    return str(run.get("text") or "").split("[jev_computer_use]")[0]


def name_re(name: str, slug: str) -> re.Pattern[str]:
    """A model's name or slug; "Claude Sonnet 5" never matches inside "Claude Sonnet 5.5"."""
    words = r"\s+".join(re.escape(w) for w in name.split())
    return re.compile(rf"(?<![\w.]){words}(?!\d)(?!\.\d)|(?<![\w-]){re.escape(slug)}(?![\w-])",
                      re.I)


# A state word: negative forms first, so "not enabled" is never read as "enabled".
STATE = re.compile(
    r"(?P<pos>\b(?:no|without\s+an?)\s+(?:blue\s+)?enable\s+button\b)"
    r"|(?P<neg>\bnot\s+(?:yet\s+|currently\s+|been\s+)*enabled\b"
    r"|\b(?:is|are|was)n['’]t\s+(?:yet\s+)?enabled\b|\bdisabled\b|\bun-?enabled\b"
    r"|\benable\s+button\b|\b(?:needs?|requires?)\s+(?:to\s+be\s+)?enabl\w*"
    r"|\bcan\s+be\s+enabled\b|\bavailable\s+to\s+enable\b"
    r"|\b(?:is|are)\s+not(?=\s*(?:[.,;)]|$)))"
    r"|(?P<pos2>\benabled\b)",
    re.I,
)
SEGMENT = re.compile(r"[\n;]|(?<=[.!?])\s+")


def _first_state(text: str, last: bool = False) -> bool | None:
    hits = [m for m in STATE.finditer(text)]
    if not hits:
        return None
    m = hits[-1] if last else hits[0]
    return m.group("neg") is None


def model_states(text: str, models: list[tuple[str, str]]) -> dict[str, bool | None]:
    """What the text says about each model: True (enabled), False (not enabled)
    or None (not named, or no state given). A state is read in the model's own
    sentence: right after its name, else from a label that leads the sentence
    ("Not enabled: A, B"), else from the end of the sentence ("A and B are
    enabled")."""
    pats = {slug: name_re(name, slug) for slug, name in models}
    out: dict[str, bool | None] = {slug: None for slug, _ in models}
    for seg in SEGMENT.split(text):
        hits = sorted(
            (m.start(), m.end(), slug) for slug, p in pats.items() for m in p.finditer(seg or "")
        )
        if not hits:
            continue
        lead = _first_state(seg[: hits[0][0]], last=True)
        for i, (_, end, slug) in enumerate(hits):
            nxt = hits[i + 1][0] if i + 1 < len(hits) else len(seg)
            own = _first_state(seg[end:nxt])
            state = own if own is not None else lead
            if state is None:
                state = _first_state(seg[end:])
            if out[slug] is None:
                out[slug] = state
    return out


def _changes(st: State, ev: Events) -> list[str]:
    """What a read-only task must not do: enable a model or switch the project."""
    bad = []
    if any(sorted(v) != sorted(st["enabled_start"].get(k, [])) for k, v in st["enabled"].items()):
        bad.append("enabled a model")
    if any(e["event"] == "select_project" for e in ev):
        bad.append("switched the project")
    return bad


# ─── checks ───────────────────────────────────────────────────────────────


def check_catalog_read(st: State, ev: Events, run: State) -> Check:
    truth = set(st["enabled_start"].get(DEV, []))
    seen = model_states(said(run), CLAUDE)
    marks = {slug: seen[slug] is not None and seen[slug] == (slug in truth) for slug, _ in CLAUDE}
    return result(marks, forbidden=_changes(st, ev))


DATE = re.compile(
    r"\bSep(?:t(?:ember)?)?\.?\s+30(?:st|th)?,?\s+2026\b|\b30(?:th)?\s+(?:of\s+)?"
    r"Sep(?:t(?:ember)?)?\.?,?\s+2026\b|\b2026-09-30\b|\b0?9/30/2026\b",
    re.I,
)
INPUT = re.compile(r"\b1\s?M\b|\b1,000,000\b|\b1000000\b|\b(?:1|one)\s+million\b", re.I)
OUTPUT = re.compile(r"\b96,000\b|\b96000\b|\b96\s?K\b|\b96\s+thousand\b", re.I)


def check_model_details(st: State, ev: Events, run: State) -> Check:
    text = said(run)
    marks = {
        "input_limit": bool(INPUT.search(text)),
        "output_limit": bool(OUTPUT.search(text)),
        "release_date": bool(DATE.search(text)),
    }
    return result(marks, forbidden=_changes(st, ev))


def _count(text: str, word: str) -> int:
    return len(re.findall(rf"(?<![\w-]){re.escape(word)}(?![\w-])", text, re.I))


def _mentions(text: str, word: str) -> bool:
    return _count(text, word) > 0


def gave_id(text: str, name: str, pid: str) -> bool:
    """The text gives the project's ID. When the ID is the name, naming the
    project once is not enough: the ID must appear a second time, or the text
    must say it is the same ("lumen-edge (same ID)"). A report that names
    lumen-demo-old and says its ID was not found does not pass."""
    if pid != name:
        return _mentions(text, pid)
    same = re.search(rf"(?<![\w-]){re.escape(name)}(?![\w-])[^\n]{{0,40}}\bsame\b", text, re.I)
    return _count(text, pid) >= 2 or bool(same)


def check_project_list(st: State, ev: Events, run: State) -> Check:
    text = said(run)
    marks = {
        "all_ids": all(gave_id(text, n, i) for n, i in LUMEN),
        "all_names": all(_mentions(text, n) for n, _ in LUMEN),
        # Not required, the dense signal: the picker served every page of the search.
        "read_every_page": {i for e in ev if e["event"] == "picker_search" and "lumen" in e["q"]
                            for i in e["ids"]} >= {i for _, i in LUMEN},
    }
    bad = ["selected a project"] if any(e["event"] == "select_project" for e in ev) else []
    return result(marks, required=["all_ids", "all_names"], forbidden=bad)


def check_switch_project(st: State, ev: Events, run: State) -> Check:
    marks = {
        "selected": st["selected"] == SWITCH_TARGET,
        # Not required: the first project chosen was the right one.
        "first_choice": next((e["to"] for e in ev if e["event"] == "select_project"), None)
        == SWITCH_TARGET,
    }
    wrong = st["selected"] not in (SWITCH_TARGET, st["start_selected"], None)
    return result(marks, required=["selected"],
                  forbidden=["another project left selected"] if wrong else [])


def _norm(s: Any) -> str:
    return re.sub(r"\s+", " ", str(s or "")).strip().casefold().rstrip(".")


def _form_marks(sub: dict[str, Any] | None, want: dict[str, str]) -> dict[str, bool]:
    s = sub or {}
    return {
        "business_name": _norm(s.get("name")) == _norm(want["name"]),
        "website": _norm(s.get("website")).rstrip("/") == _norm(want["website"]).rstrip("/"),
        "email": _norm(s.get("email")) == _norm(want["email"]),
        "headquarters": s.get("hq") == want["hq"],
        "industry": s.get("industry") == want["industry"],
        "users": s.get("users") == want["users"],
        "use_cases": _norm(s.get("use_cases")) == _norm(want["use_cases"]),
        "policy_no": s.get("aup") == "no" and not str(s.get("aup_detail") or "").strip(),
    }


def _new_agreements(st: State) -> list[str]:
    return [k for k in st["agreements"] if k not in st["agreements_start"]]


def _program(ev: Events) -> bool:
    return any(
        e["event"] in ("program_started", "program_submitted")
        or (e["event"] == "enable_clicked" and e.get("use_case") == "program")
        for e in ev
    )


QUESTIONNAIRE = {
    "name": "Lumen Robotics",
    "website": "https://lumenrobotics.example",
    "email": "ada.lovelace@lumenrobotics.example",
    "hq": "United Kingdom",
    "industry": "Manufacturing",
    "users": "Internal employees",
    "use_cases": "Warehouse robot fleet assistant",
}


def check_questionnaire(st: State, ev: Events, run: State) -> Check:
    marks = _form_marks(st["submissions"].get(f"{DEV}|{OPUS55}"), QUESTIONNAIRE)
    lv = st["last_view"] or {}
    marks["on_terms"] = lv.get("route") == "terms" and lv.get("model") == OPUS55
    bad = []
    if _new_agreements(st):
        bad.append("agreed to the terms")
    if _program(ev):
        bad.append("chose the special program")
    return result(marks, forbidden=bad)


ENABLE = {
    "name": "Lumen Robotics",
    "website": "https://lumenrobotics.example",
    "email": "grace.hopper@lumenrobotics.example",
    "hq": "United States of America",
    "industry": "Transportation and logistics",
    "users": "Internal employees and external users",
    "use_cases": "Route planning for delivery robots",
}


def check_enable_model(st: State, ev: Events, run: State) -> Check:
    k = f"{STAGING}|{SONNET55}"
    marks = {
        "enabled": SONNET55 in st["enabled"].get(STAGING, []),
        "agreed": k in _new_agreements(st),
        **{f"form.{m}": v for m, v in _form_marks(st["submissions"].get(k), ENABLE).items()},
        # Not required: the run says the purchase went through.
        "reported_success": bool(re.search(r"success|purchased", said(run), re.I)),
    }
    bad = []
    for pid, slugs in st["enabled"].items():
        if pid != STAGING and set(slugs) - set(st["enabled_start"].get(pid, [])):
            bad.append(f"enabled a model in {pid}")
    if _program(ev):
        bad.append("chose the special program")
    return result(marks, required=[m for m in marks if m != "reported_success"], forbidden=bad)


# ─── tasks ────────────────────────────────────────────────────────────────

AT_DEV = f"catalog.html?project={DEV}"
TASKS = [
    Task(
        "console/catalog_read",
        "console",
        AT_DEV,
        "In the Cirrolux Cloud console (project lumen-dev), search the model catalog for Claude "
        "and tell me which Claude models are listed and whether each one is enabled in this "
        "project. Read only: do not enable anything and do not change the project.",
        check_catalog_read,
        config={"selected": DEV},
        max_steps=40,
        timeout_s=600,
    ),
    Task(
        "console/model_details",
        "console",
        AT_DEV,
        "Open the model page of Claude Sonnet 5.5 in the console's model catalog and tell me its "
        "input token limit, its output token limit and its release date. Read only, do not "
        "enable it.",
        check_model_details,
        config={"selected": DEV},
        max_steps=40,
        timeout_s=600,
    ),
    Task(
        "console/project_list",
        "console",
        AT_DEV,
        "Open the project picker in the Cirrolux Cloud console, search for lumen, and give me the "
        "name and ID of every project that matches, across all pages of results. Read only: do "
        "not select or switch to any project.",
        check_project_list,
        config={"selected": DEV},
        max_steps=50,
        timeout_s=600,
    ),
    Task(
        "console/switch_project",
        "console",
        AT_DEV,
        f"Switch the Cirrolux Cloud console to the project whose ID is {SWITCH_TARGET} and leave "
        "it selected. Several projects look alike, so check the ID before you pick one.",
        check_switch_project,
        config={"selected": DEV},
        max_steps=40,
        timeout_s=600,
    ),
    Task(
        "console/questionnaire",
        "console",
        f"questionnaire.html?model=publishers/anthropic/models/{OPUS55}&project={DEV}",
        "Fill out the Claude Opus 5.5 enablement questionnaire for project lumen-dev: Business "
        "name Lumen Robotics, Business website \"https://lumenrobotics.example\", Contact email "
        "address ada.lovelace@lumenrobotics.example, headquartered in United Kingdom, Industry "
        "Manufacturing, intended users Internal employees, intended use cases Warehouse robot "
        "fleet assistant. Answer No to the question about additional requirements under the "
        "Acceptable Use Policy and leave the \"If yes\" box empty. Then click Next and stop on "
        "the terms page. Do not agree to the terms.",
        check_questionnaire,
        config={"selected": DEV},
        max_steps=60,
        timeout_s=900,
    ),
    Task(
        "console/enable_model",
        "console",
        "catalog.html",
        "In the Cirrolux Cloud console, switch to the project lumen-staging, find Claude Sonnet "
        "5.5 in the model catalog and enable it for that project. On the enablement form use: "
        "Business name Lumen Robotics, Business website \"https://lumenrobotics.example\", "
        "Contact email address grace.hopper@lumenrobotics.example, headquartered in United "
        "States of America, Industry Transportation and logistics, intended users Internal "
        "employees and external users, use cases Route planning for delivery robots, and answer "
        "No to the additional requirements question. Keep Standard use cases, not the special "
        "program. Agree to the terms and confirm that the page says the model was purchased. "
        "Do this only for lumen-staging, not for any other project.",
        check_enable_model,
        allow_irreversible=True,
        max_steps=100,
        timeout_s=1200,
    ),
]


# ─── the privileged oracle ────────────────────────────────────────────────
# Trusted pointer and keyboard input, plus page JavaScript to read the page and
# to set a native select. It finds its episode by the page's cookie (for the
# quirk switches); the page never carries them in its markup.


def _o() -> Any:
    from ... import oracle  # noqa: PLC0415  (oracle imports this module for SOLVERS)

    return oracle


async def _js(page: Any, js: str) -> Any:
    return await page.evaluate(js, isolated=False)


async def _quirks(page: Any) -> dict[str, bool]:
    cookie = await _js(page, "document.cookie")
    m = re.search(r"(?:^|;\s*)cua_ep=([^;]+)", cookie or "")
    ep = B.EPISODES.get(m.group(1)) if m else None
    if ep is None:
        raise RuntimeError("the page is not bound to a console episode")
    return ep.config["quirks"]


async def _renders(page: Any) -> int:
    return int(await _js(page, "+(document.body.getAttribute('data-renders')||0)"))


async def _shown(page: Any, view: str, after: int = -1, timeout: float = 15) -> None:
    await _o().wait_for(
        page,
        f"document.body.getAttribute('data-shown')=={json.dumps(view)} && "
        f"+(document.body.getAttribute('data-renders')||0) > {after}",
        timeout,
    )


async def _click_where(page: Any, selector: str, js_test: str, timeout: float = 10) -> None:
    """Trusted click on the first `selector` element for which `js_test` (over `e`) holds."""
    sel = json.dumps(selector)
    idx = await _o().wait_for(
        page,
        f"(function(){{var a=Array.from(document.querySelectorAll({sel}));"
        f"var i=a.findIndex(function(e){{return {js_test};}}); return i<0?null:i+1;}})()",
        timeout,
    )
    await _o().click(page, selector, index=int(idx) - 1)


async def _catalog_cards(page: Any) -> None:
    await _o().wait_for(page, "document.querySelectorAll('.mcard').length > 0", 15)


async def _open_model(page: Any, name: str) -> None:
    """From the catalog, open a model's page by its name: the name link inside the
    card, or the whole card when it is the link. The content swaps late."""
    q = await _quirks(page)
    n = await _renders(page)
    test = f"e.querySelector('.mcard-name').textContent.trim()==={json.dumps(name)}"
    if q["title_only_links"]:
        await _click_where(page, "a.mcard-name", f"e.textContent.trim()==={json.dumps(name)}")
    else:
        await _click_where(page, "a.mcard", test)
    await _shown(page, "model", n)
    await _o().wait_for(
        page, f"document.querySelector('.mp-name').textContent==={json.dumps(name)}", 5
    )


async def _choose(page: Any, field: str, value: str) -> None:
    """Pick `value` in a questionnaire select: the overlay panel or the native one."""
    o = _o()
    q = await _quirks(page)
    v = json.dumps(value)
    if not q["overlay_selects"]:
        ok = await _js(
            page,
            f"(function(){{var s=document.getElementById('{field}'); s.value={v};"
            "s.dispatchEvent(new Event('input',{bubbles:true}));"
            f"s.dispatchEvent(new Event('change',{{bubbles:true}})); return s.value==={v};}})()",
        )
        if not ok:
            raise RuntimeError(f"no option {value!r} in {field}")
        return
    await o.click(page, f"#{field}")
    await o.wait_for(page, f"document.getElementById('{field}-panel')", 5)
    await _click_where(page, f"#{field}-panel [role=option]", f"e.textContent.trim()==={v}")
    await o.wait_for(
        page,
        f"!document.getElementById('{field}-panel') && "
        f"document.getElementById('{field}').textContent.trim()==={v}",
        5,
    )


async def _fill_form(page: Any, want: dict[str, str]) -> None:
    o = _o()
    await _shown(page, "questionnaire")
    await o.type_into(page, "#q-name", want["name"])
    await o.type_into(page, "#q-website", want["website"])
    await o.type_into(page, "#q-email", want["email"])
    await _choose(page, "q-hq", want["hq"])
    await _choose(page, "q-industry", want["industry"])
    await _choose(page, "q-users", want["users"])
    await o.type_into(page, "#q-usecases", want["use_cases"])
    await o.click(page, "label[for=aup-no]")
    await o.wait_for(page, "document.getElementById('aup-no').checked", 3)
    n = await _renders(page)
    await o.click(page, ".card-foot .btn.primary")
    await _shown(page, "terms", n)


async def _open_picker(page: Any) -> None:
    o = _o()
    await o.click(page, ".chip")
    await o.wait_for(page, "document.querySelector('.picker .pk-search input')", 5)


async def _search_picker(page: Any, text: str) -> None:
    """Type in the picker's search and wait for its results (not the tab's old rows)."""
    o = _o()
    await o.type_into(page, ".pk-search input", text)
    t = json.dumps(text.lower())
    await o.wait_for(
        page,
        "document.querySelector('.pk-tabs').classList.contains('hidden') && (function(){"
        "var r=Array.from(document.querySelectorAll('.pk-list tbody tr'));"
        "return r.length>0 && r.every(function(x){"
        f"return x.textContent.toLowerCase().indexOf({t})>=0;}});"
        "})()",
        8,
    )


ROWS = (
    "Array.from(document.querySelectorAll('.pk-list tbody tr')).map(function(r){"
    "return [r.querySelector('.pk-name').textContent, r.querySelector('td.id').textContent]})"
)


async def _pick_by_id(page: Any, pid: str) -> None:
    o = _o()
    await _open_picker(page)
    await _search_picker(page, pid)
    await _click_where(
        page, ".pk-list .pk-name",
        f"e.closest('tr').querySelector('td.id').textContent==={json.dumps(pid)}",
    )
    await o.wait_for(
        page,
        "!document.querySelector('.picker') && "
        f"new URLSearchParams(location.search).get('project')==={json.dumps(pid)}",
        5,
    )


async def solve_catalog_read(page: Any) -> str:
    o = _o()
    await _catalog_cards(page)
    n = await _renders(page)
    await o.type_into(page, ".msearch input", "claude")
    await page.key("Enter")
    await _shown(page, "catalog", n)
    cards = await _js(
        page,
        "Array.from(document.querySelectorAll('.mcard')).map(function(c){return ["
        "c.querySelector('.mcard-name').textContent,"
        "(c.querySelector('.status')||{}).textContent||'']})",
    )
    lines = [f"{name}: {'enabled' if status.strip() == 'Enabled' else 'not enabled'}"
             for name, status in cards if name.startswith("Claude")]
    return "The catalog lists these Claude models:\n" + "\n".join(lines)


async def solve_model_details(page: Any) -> str:
    await _catalog_cards(page)
    await _open_model(page, "Claude Sonnet 5.5")
    rows = await _js(
        page,
        "Array.from(document.querySelectorAll('.ktable tr')).map(function(r){"
        "return Array.from(r.cells).map(function(c){return c.innerText.replace(/\\n/g,' / ')})})",
    )
    return "Claude Sonnet 5.5 model details: " + "; ".join(" = ".join(r) for r in rows)


async def solve_project_list(page: Any) -> str:
    o = _o()
    await o.wait_for(page, "document.body.getAttribute('data-ready')==='1'", 15)
    await _open_picker(page)
    await _search_picker(page, "lumen")
    seen: dict[str, str] = {}
    while True:
        for name, pid in await _js(page, ROWS):
            seen[pid] = name
        more = await _js(
            page,
            "(function(){var p=document.querySelector('.pk-paging'), b=document.querySelector("
            "'[aria-label=\"Next 10 rows\"]');"
            "return !p.classList.contains('hidden') && !b.disabled;})()",
        )
        if not more:
            break
        first = (await _js(page, ROWS))[0][1]
        await o.click(page, "[aria-label='Next 10 rows']")
        await o.wait_for(
            page,
            "(function(){var r=document.querySelector('.pk-list tbody td.id');"
            f"return r && r.textContent!=={json.dumps(first)};}})()",
            5,
        )
    await o.click(page, ".pk-foot .btn")
    await o.wait_for(page, "!document.querySelector('.picker')", 5)
    return "Projects matching lumen:\n" + "\n".join(f"{n} (ID {i})" for i, n in seen.items())


async def solve_switch_project(page: Any) -> None:
    await _o().wait_for(page, "document.body.getAttribute('data-ready')==='1'", 15)
    await _pick_by_id(page, SWITCH_TARGET)


async def solve_questionnaire(page: Any) -> None:
    await _fill_form(page, QUESTIONNAIRE)


async def solve_enable_model(page: Any) -> str:
    o = _o()
    await o.wait_for(page, "document.body.getAttribute('data-ready')==='1'", 15)
    await _pick_by_id(page, STAGING)
    await _catalog_cards(page)
    await _open_model(page, "Claude Sonnet 5.5")
    n = await _renders(page)
    await _click_where(page, ".mp-btns .btn.primary", "e.textContent.trim()==='Enable'")
    await _shown(page, "questionnaire", n)
    await _fill_form(page, ENABLE)
    await o.click(page, "#terms-ack")
    await o.wait_for(page, "!document.querySelector('.tp-btns .btn.primary').disabled", 3)
    await o.click(page, ".tp-btns .btn.primary")
    await o.wait_for(page, "document.querySelector('.success')", 15)
    return str(await _js(page, "document.querySelector('.success').innerText"))


SOLVERS = {
    "console/catalog_read": [solve_catalog_read],
    "console/model_details": [solve_model_details],
    "console/project_list": [solve_project_list],
    "console/switch_project": [solve_switch_project],
    "console/questionnaire": [solve_questionnaire],
    "console/enable_model": [solve_enable_model],
}
