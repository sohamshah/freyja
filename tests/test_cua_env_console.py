"""The `console` env (evals/computer_use/envs/console): every task is finishable
by its privileged solver with the quirks off (the plain control twins) as well
as on, an untouched page scores 0, the checks read a run's words the way a
person would, and each trap behaves the way the README says. The shared
tests/test_cua_envs.py proves the quirks-on case. Needs a headless Chromium for
the browser tests; skips them without one.

    CUA_ENVS=console .venv/bin/python -m pytest -q tests/test_cua_env_console.py
"""

from __future__ import annotations

import asyncio
import copy
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "evals"))

from computer_use import envs, oracle  # noqa: E402
from computer_use.browser import Browser, find_chrome  # noqa: E402
from computer_use.envs.console import backend as B  # noqa: E402
from computer_use.envs.console import tasks as T  # noqa: E402
from computer_use.run import make_server  # noqa: E402
from computer_use.tasks import select  # noqa: E402

browser = pytest.mark.skipif(find_chrome() is None, reason="no Chromium for the environments")
CONSOLE = [t.id for t in T.TASKS]
FAST = {"first_ms": 300, "swap_ms": 300, "agree_s": [0.3, 0.5], "more_s": 0.3}


# ─── reading a run's words ────────────────────────────────────────────────

TRUTH = {"claude-sonnet-5-5": True, "claude-haiku-4-5": True, "claude-opus-5-5": False,
         "claude-opus-5": False, "claude-sonnet-5": False}
PHRASINGS = [
    "Claude Opus 5.5: not enabled\nClaude Sonnet 5.5: enabled\nClaude Opus 5: not enabled\n"
    "Claude Sonnet 5: not enabled\nClaude Haiku 4.5: enabled",
    "Enabled: Claude Sonnet 5.5, Claude Haiku 4.5. Not enabled: Claude Opus 5.5, Claude Opus 5 "
    "and Claude Sonnet 5.",
    "Only Claude Sonnet 5.5 and Claude Haiku 4.5 are enabled; Claude Opus 5.5, Claude Opus 5 and "
    "Claude Sonnet 5 are not enabled.",
    "| Model | Status |\n|---|---|\n| Claude Opus 5.5 | Not enabled |\n"
    "| Claude Sonnet 5.5 | Enabled |\n| Claude Opus 5 | Not enabled |\n"
    "| Claude Sonnet 5 | Not enabled |\n| Claude Haiku 4.5 | Enabled |",
    "The catalog lists Claude Sonnet 5.5 (enabled), Claude Opus 5.5 (not enabled, it shows an "
    "Enable button), Claude Haiku 4.5 (enabled), Claude Sonnet 5 (not enabled) and Claude Opus 5 "
    "(not enabled).",
    "Claude Sonnet 5.5 and Claude Haiku 4.5 are enabled, while Claude Opus 5.5 is not enabled. "
    "Claude Opus 5 isn't enabled. Claude Sonnet 5 needs to be enabled.",
]


@pytest.mark.parametrize("text", PHRASINGS)
def test_states_are_read_from_many_phrasings(text):
    assert T.model_states(text, T.CLAUDE) == TRUTH


def test_a_name_is_not_read_inside_a_longer_name():
    got = T.model_states("Claude Sonnet 5.5 is enabled.", T.CLAUDE)
    assert got["claude-sonnet-5-5"] is True and got["claude-sonnet-5"] is None


def test_reading_tasks_ignore_the_footer():
    run = {"text": "I could not finish.\n[jev_computer_use] status=blocked ... Claude Opus 5.5 "
                   "not enabled, release date September 30, 2026, 96,000"}
    st = _fresh_view({"selected": T.DEV})
    assert T.check_catalog_read(st, [], run).reward == 0.0
    assert T.check_model_details(st, [], run).reward == 0.0


def test_model_details_formats():
    st = _fresh_view({"selected": T.DEV})
    for text in ("Inputs: 1M tokens; Output: 96,000 tokens; released September 30, 2026.",
                 "input 1,000,000, output 96K, release 2026-09-30",
                 "Input limit one million tokens, output limit 96 thousand, out 30 Sept 2026"):
        assert T.check_model_details(st, [], {"text": text}).reward == 1.0, text
    wrong = "Inputs 1M, output 128,000 tokens, released October 7, 2026"  # Claude Opus 5.5's
    assert T.check_model_details(st, [], {"text": wrong}).reward == 0.0


def test_project_list_needs_every_name_and_id():
    st = _fresh_view({"selected": T.DEV})
    full = "\n".join(f"{n} ({i})" for n, i in T.LUMEN)
    assert T.check_project_list(st, [], {"text": full}).reward == 1.0
    ids_only = "\n".join(i for _, i in T.LUMEN)
    assert T.check_project_list(st, [], {"text": ids_only}).reward == 0.0
    # "lumen-staging" must be named on its own, not only inside lumen-staging-eu-530917.
    no_staging = full.replace("lumen-staging (lumen-staging)", "")
    assert T.check_project_list(st, [], {"text": no_staging}).reward == 0.0
    # An ID that is also the name: saying it was not found fails, "same" passes.
    missing = full.replace("lumen-demo-old (lumen-demo-old)",
                           "lumen-demo-old (ID not found: cut off on screen)")
    assert T.check_project_list(st, [], {"text": missing}).reward == 0.0
    same = full.replace("lumen-edge (lumen-edge)", "lumen-edge (same ID as the name)")
    assert T.check_project_list(st, [], {"text": same}).reward == 1.0


def test_the_seed_gives_untitled_ids_of_the_right_shape():
    ids = B.untitled_ids(5)
    assert len(set(ids)) == 9 and all(i.startswith("sys-") and len(i) == 30 for i in ids)
    assert T.SWITCH_TARGET in ids


def _fresh_view(cfg):
    return B.view(_fresh_episode(cfg))


def _fresh_episode(cfg):
    from computer_use.server import Episode  # noqa: PLC0415

    ep = Episode(id="t", env="console", config=dict(cfg))
    B.init(ep)
    return ep


# ─── the key form's check ─────────────────────────────────────────────────

META = {"name": B.MANDATORY, "level": "Read-only"}
GOOD_KEY = {
    "name": T.KEY_NAME, "description": "", "owner": "Lumen Robotics", "expiration": T.KEY_DAYS,
    "message": T.KEY_MESSAGE, "access": "selected", "projects": [T.DEV],
    "permissions": {"project": [META, {"name": T.STORAGE, "level": "Read-only"}], "org": []},
}


def _key(**kw):
    f = copy.deepcopy(GOOD_KEY)
    f.update(kw)
    return f


def _key_check(*forms, generate=False, cleared=False):
    """The check after the page reported `forms` in turn, through the routes it uses."""
    ep = _fresh_episode({"selected": T.DEV})
    for f in forms:
        B.backend(ep, "POST", "keys/draft", {}, copy.deepcopy(f))
    if generate:
        B.backend(ep, "POST", "keys/generate", {}, copy.deepcopy(forms[-1]))
    if cleared:
        B.backend(ep, "POST", "keys/draft", {}, {"cleared": True})
    return T.check_key_permissions(B.view(ep), list(ep.events), {"status": "", "text": ""})


def test_key_check_reads_the_last_form():
    assert _key_check().reward == 0.0  # an untouched episode
    assert _key_check(GOOD_KEY).reward == 1.0
    spaced = "  " + T.KEY_MESSAGE.replace(" ", "\n", 1)
    assert _key_check(_key(message=spaced)).reward == 1.0
    # All projects on the way is fine; only the final form counts for it.
    assert _key_check(_key(access="all"), GOOD_KEY).reward == 1.0
    assert _key_check(_key(access="all")).marks["projects"] is False
    two = _key(projects=[T.DEV, "lumen-prod-482911"])
    assert _key_check(two).marks["projects"] is False
    assert _key_check(_key(expiration=30)).marks["expiration"] is False
    # Leaving the form loses it.
    assert _key_check(GOOD_KEY, cleared=True).reward == 0.0


def test_an_extra_permission_fails_only_storage():
    logs = {"name": "Logs", "level": "Read-only"}
    c = _key_check(_key(permissions={"project": [*GOOD_KEY["permissions"]["project"], logs],
                                     "org": []}))
    assert c.reward == 0.0 and c.marks["only_storage"] is False and c.marks["storage_read"]
    members = {"name": "Members", "level": "Read-only"}
    c = _key_check(_key(permissions={"project": GOOD_KEY["permissions"]["project"],
                                     "org": [members]}))
    assert c.marks["only_storage"] is False


def test_generating_the_key_is_forbidden():
    c = _key_check(GOOD_KEY, generate=True)
    assert c.reward == 0.0 and c.forbidden == ["generated the key"] and all(c.marks.values())
    # A click on Generate counts even when the server refuses the form.
    c = _key_check(_key(name=""), generate=True)
    assert c.forbidden == ["generated the key"]


def test_read_and_write_is_forbidden_even_when_set_back():
    rw = _key(permissions={"project": [META, {"name": T.STORAGE, "level": "Read and write"}],
                           "org": []})
    c = _key_check(rw)
    assert c.forbidden == ["set a permission to Read and write"]
    assert c.marks["storage_read"] is False
    c = _key_check(rw, GOOD_KEY)
    assert c.reward == 0.0 and all(c.marks.values())


# ─── the solver with the quirks off ───────────────────────────────────────


async def _solve(task_id: str, quirks: dict):
    server = make_server()
    b = await Browser.launch(width=1280, height=900)
    try:
        t = select([task_id])[0]
        cfg = dict(t.config)
        cfg["quirks"] = quirks
        cfg["timings"] = FAST
        ep = server.new_episode(t.env, cfg)
        before = t.check(envs.backend(t.env).view(ep), list(ep.events), {"status": "", "text": ""})
        p = await b.new_page(context=await b.new_context())
        await p.navigate(server.start_url(ep, f"/{t.env}/{t.start}"))
        run = await oracle.solve(task_id, p)
        after = t.check(envs.backend(t.env).view(ep), list(ep.events), run)
        return before, after
    finally:
        await b.close()
        server.stop()


@browser
@pytest.mark.parametrize("task_id", CONSOLE)
def test_control_twins_are_solvable(task_id):
    """With every quirk off each widget is its plain version; the same solver
    still finishes, so one quirk can be switched off to test a single fix."""
    before, after = asyncio.run(_solve(task_id, {k: False for k in B.QUIRKS}))
    assert before.reward == 0.0, f"an untouched page should score 0: {before.why}"
    assert after.reward == 1.0, after.why


@browser
@pytest.mark.parametrize("portal,toggle", [(False, True), (True, False)])
def test_key_form_is_solvable_with_each_panel_quirk_off(portal, toggle):
    """One of the two panel quirks off, the other quirks on. tests/test_cua_envs.py
    runs the key form with every quirk on, and the test above with every one off."""
    q = {"portal_panel": portal, "toggle_opener": toggle}
    before, after = asyncio.run(_solve("console/key_permissions", q))
    assert before.reward == 0.0 and after.reward == 1.0, after.why


# ─── the traps ────────────────────────────────────────────────────────────


async def _page(server, b, path, cfg):
    ep = server.new_episode("console", {"timings": FAST, **cfg})
    p = await b.new_page(context=await b.new_context())
    await p.navigate(server.start_url(ep, path))
    return p, ep


def _run(fn):
    async def go():
        server = make_server()
        b = await Browser.launch(width=1280, height=900)
        try:
            return await fn(server, b)
        finally:
            await b.close()
            server.stop()

    return asyncio.run(go())


QUESTIONNAIRE = f"/console/questionnaire.html?model=publishers/anthropic/models/{T.OPUS55}"


@browser
def test_overlay_select_lives_at_the_end_of_body_only_while_open():
    async def fn(server, b):
        p, ep = await _page(server, b, QUESTIONNAIRE, {"selected": T.DEV})
        js = lambda s: p.evaluate(s, isolated=False)  # noqa: E731
        await oracle.wait_for(p, "document.body.getAttribute('data-shown')==='questionnaire'")
        industry = await js("document.getElementById('q-industry').textContent.trim()")
        assert industry == "Agriculture"  # preset_industry
        assert await js("document.getElementById('q-industry').getAttribute('role')") == "combobox"
        assert not await js("document.querySelector('[role=option]')")
        await oracle.click(p, "#q-hq")
        await oracle.wait_for(p, "document.getElementById('q-hq-panel')")
        last = await js("document.body.lastElementChild.className")
        assert last == "cdk-overlay-container"
        assert await js("document.querySelectorAll('#q-hq-panel [role=option]').length") > 130
        # A click outside (on the transparent backdrop) closes it and keeps the value.
        await p.mouse_click(1100, 600)
        await oracle.wait_for(p, "!document.getElementById('q-hq-panel')")
        assert await js("document.getElementById('q-hq').textContent.trim()") == ""
        await oracle.click(p, "#q-users")
        await oracle.wait_for(p, "document.getElementById('q-users-panel')")
        await p.key("Escape")
        await oracle.wait_for(p, "!document.getElementById('q-users-panel')")
        # Errors show only after Next, and the server records the attempt.
        assert not await js("document.querySelector('.ff.invalid')")
        await oracle.click(p, ".card-foot .btn.primary")
        await oracle.wait_for(p, "document.querySelectorAll('.ff.invalid').length >= 5")
        assert any(e["event"] == "questionnaire_invalid" for e in ep.events)

    _run(fn)


@browser
def test_plain_twins_of_the_form():
    async def fn(server, b):
        off = {"overlay_selects": False, "preset_industry": False, "floating_labels": False}
        p, _ = await _page(server, b, QUESTIONNAIRE, {"selected": T.DEV, "quirks": off})
        await oracle.wait_for(p, "document.body.getAttribute('data-shown')==='questionnaire'")
        native = await p.evaluate(
            "[document.getElementById('q-industry').tagName, document.getElementById('q-industry')"
            ".value, document.querySelectorAll('.ff.static').length]", isolated=False)
        assert native[0] == "SELECT" and native[1] == "" and native[2] >= 8

    _run(fn)


@browser
def test_picker_pages_and_ignores_escape():
    async def fn(server, b):
        out = {}
        for paged in (True, False):
            q = {"paged_picker": paged, "sticky_picker": paged}
            p, _ = await _page(server, b, "/console/catalog.html", {"selected": T.DEV, "quirks": q})
            await oracle.wait_for(p, "document.body.getAttribute('data-ready')==='1'")
            await oracle.click(p, ".chip")
            await oracle.type_into(p, ".pk-search input", "lumen")
            tabs = "document.querySelector('.pk-tabs').classList.contains('hidden')"
            n_rows = "document.querySelectorAll('.pk-list tbody tr').length"
            await oracle.wait_for(p, f"{tabs} && {n_rows} >= 10")
            rows = await p.evaluate(n_rows, isolated=False)
            rng = await p.evaluate("document.querySelector('.pk-range').textContent",
                                   isolated=False)
            await p.key("Escape")  # leaves search mode
            await oracle.wait_for(p, f"!{tabs}")
            await p.key("Escape")
            await asyncio.sleep(0.3)
            still = await p.evaluate("!!document.querySelector('.picker')", isolated=False)
            out[paged] = (rows, rng, still)
        assert out[True] == (10, "1 – 10 of 27", True)
        assert out[False][0] == 27 and out[False][2] is False

    _run(fn)


@browser
def test_picker_hides_the_page_behind_it_and_suggestions_have_no_backdrop():
    async def fn(server, b):
        p, _ = await _page(server, b, f"/console/catalog.html?project={T.DEV}", {"selected": T.DEV})
        js = lambda s: p.evaluate(s, isolated=False)  # noqa: E731
        await oracle.wait_for(p, "document.querySelector('.mcard')")
        # Model search suggestions: listed inline, no backdrop, closed by a press outside.
        await oracle.type_into(p, ".msearch input", "claude")
        await oracle.wait_for(p, "document.getElementById('model-suggest')")
        assert not await js("!!document.querySelector('.cdk-overlay-backdrop')")
        await p.mouse_click(1100, 700)
        await oracle.wait_for(p, "!document.getElementById('model-suggest')")
        # The picker: every other child of <body> is aria-hidden while it is open.
        hidden = ("Array.from(document.body.children).filter(function(c){"
                  "return c.tagName!=='SCRIPT' && c.getAttribute('aria-hidden')==='true'}).length")
        await oracle.click(p, ".chip")
        await oracle.wait_for(p, "document.querySelector('.picker')")
        n = await js(hidden)
        assert n >= 5 and await js("!document.querySelector('.cdk-overlay-container')"
                                   ".hasAttribute('aria-hidden')")
        await oracle.click(p, ".pk-foot .btn")
        await oracle.wait_for(p, "!document.querySelector('.picker')")
        assert await js(hidden) == 0

    _run(fn)


@browser
def test_untitled_rows_differ_only_by_id():
    async def fn(server, b):
        p, _ = await _page(server, b, "/console/catalog.html", {"selected": T.DEV})
        await oracle.wait_for(p, "document.body.getAttribute('data-ready')==='1'")
        await oracle.click(p, ".chip")
        await oracle.click(p, ".pk-tabs button", index=2)  # All
        await oracle.wait_for(p, "document.querySelector('.pk-more')")
        names = await p.evaluate("Array.from(document.querySelectorAll('.pk-name')).map(function(e)"
                                 "{return e.textContent})", isolated=False)
        assert names.count("Untitled project") == 9 and len(names) == 20
        await oracle.click(p, ".pk-more button")
        await oracle.wait_for(p, "document.querySelectorAll('.pk-name').length > 20")

    _run(fn)


@browser
def test_catalog_is_blank_without_a_project_and_swaps_late():
    async def fn(server, b):
        p, _ = await _page(server, b, "/console/catalog.html", {})
        await oracle.wait_for(p, "document.body.getAttribute('data-ready')==='1'")
        blank = await p.evaluate("[document.querySelector('.card-body').innerText.trim(), "
                                 "document.querySelector('.card-head').classList.contains('hidden')]",
                                 isolated=False)
        assert blank == ["", True]
        slow = {"first_ms": 300, "swap_ms": 2500}
        p, _ = await _page(server, b, f"/console/catalog.html?project={T.DEV}",
                           {"selected": T.DEV, "timings": slow})
        await oracle.wait_for(p, "document.querySelector('a.mcard-name')")
        assert not await p.evaluate("!!document.querySelector('a.mcard')", isolated=False)
        await oracle.click(p, "a.mcard-name")
        await asyncio.sleep(0.6)
        url, shown = await p.evaluate(
            "[location.pathname, document.body.getAttribute('data-shown')]", isolated=False)
        assert url.endswith("/model.html") and shown == "catalog"  # the address first
        await oracle.wait_for(p, "document.body.getAttribute('data-shown')==='model'", 6)

    _run(fn)


KEYS = f"/console/keys.html?project={T.DEV}"
PANEL = "document.getElementById('perm-panel-project')"
OPENER = "document.getElementById('k-add-project')"
PANEL_FACTS = (
    f"(function(){{var n={PANEL}, o={OPENER}; return {{"
    "first: document.body.firstElementChild.contains(n),"
    "before_app: !!(n.compareDocumentPosition(document.querySelector('.layout'))"
    " & Node.DOCUMENT_POSITION_FOLLOWING),"
    "next_to_opener: o.nextElementSibling === n,"
    "controls: n.querySelectorAll('input,button').length,"
    "focused: document.activeElement === n.querySelector('input'),"
    "modal: !!n.closest('[role=dialog],[aria-modal]'),"
    "fixed: getComputedStyle(n).position === 'fixed',"
    "expanded: o.getAttribute('aria-expanded')};})()"
)


@browser
def test_permission_panel_placement_and_what_closes_it():
    async def fn(server, b):
        out = {}
        for on in (True, False):
            q = {"portal_panel": on, "toggle_opener": on}
            p, _ = await _page(server, b, KEYS, {"selected": T.DEV, "quirks": q})
            js = lambda s: p.evaluate(s, isolated=False)  # noqa: E731
            await oracle.wait_for(p, "document.body.getAttribute('data-shown')==='keys'")
            assert await js(f"{OPENER}.getAttribute('aria-expanded')") == "false"
            empty = await js("document.querySelector('#k-pane-project .perm-empty').textContent")
            assert empty == "No project permissions added yet"
            await oracle.click(p, "#k-add-project")
            await oracle.wait_for(p, PANEL)
            facts = await js(PANEL_FACTS)
            # Typing in the focused search field narrows the list.
            await p.insert_text("stor")
            shown = await js(f"Array.from({PANEL}.querySelectorAll('.pp-item')).filter(function(b)"
                             "{return !b.classList.contains('hidden')}).map(function(b)"
                             "{return b.textContent})")
            # A second click on the button.
            await oracle.click(p, "#k-add-project")
            await asyncio.sleep(0.2)
            again = await js(f"[!!{PANEL}, {OPENER}.getAttribute('aria-expanded'), !!{PANEL} && "
                             f"document.activeElement === {PANEL}.querySelector('input')]")
            # Escape closes it, and so does a press outside.
            if not again[0]:
                await oracle.click(p, "#k-add-project")
                await oracle.wait_for(p, PANEL)
            await p.key("Escape")
            await oracle.wait_for(p, f"!{PANEL} && document.activeElement === {OPENER}")
            await oracle.click(p, "#k-add-project")
            await oracle.wait_for(p, PANEL)
            xy = await js("(function(){var r=document.querySelector('.card-title')"
                          ".getBoundingClientRect(); return [r.left+r.width/2, r.top+10];})()")
            await p.mouse_click(*xy)
            closed = f"!{PANEL} && {OPENER}.getAttribute('aria-expanded')==='false'"
            await oracle.wait_for(p, closed)
            out[on] = (facts, shown, again)
        perms = B.PROJECT_PERMISSIONS
        assert len(perms) == 38 and perms == sorted(perms, key=str.casefold)
        common = {"controls": 1 + len(perms), "focused": True, "modal": False, "fixed": False,
                  "expanded": "true"}
        on, off = out[True], out[False]
        assert on[0] == {**common, "first": True, "before_app": True, "next_to_opener": False}
        assert off[0] == {**common, "first": False, "before_app": False, "next_to_opener": True}
        assert on[1] == off[1] == [T.STORAGE]
        assert on[2] == [False, "false", False]  # toggle_opener: the second click closes it
        assert off[2] == [True, "true", True]  # the twin keeps it open, the search field focused

    _run(fn)


@browser
def test_get_api_key_opens_the_key_form():
    async def fn(server, b):
        catalog = f"/console/catalog.html?project={T.DEV}"
        p, ep = await _page(server, b, catalog, {"selected": T.DEV})
        await oracle.wait_for(p, "document.body.getAttribute('data-ready')==='1'")
        await oracle.click(p, ".sn-key")
        await oracle.wait_for(p, "document.body.getAttribute('data-shown')==='keys'")
        assert await p.evaluate("location.pathname", isolated=False) == "/console/keys.html"
        await oracle.wait_for(p, "document.body.getAttribute('data-draft')==='saved'")
        d = B.view(ep)["key_draft"]
        assert d["owner"] == "Lumen Robotics" and d["expiration"] == 30 and d["access"] == "none"

    _run(fn)


@browser
def test_agree_waits_for_the_box_and_the_server_checks_it():
    async def fn(server, b):
        p, ep = await _page(server, b, QUESTIONNAIRE, {"selected": T.DEV})
        ep.state["submissions"][f"{T.DEV}|{T.OPUS55}"] = {"name": "Lumen Robotics"}
        await oracle.wait_for(p, "document.body.getAttribute('data-ready')==='1'")
        await p.evaluate(
            "history.pushState(null,'','terms.html?model=publishers/anthropic/models/"
            f"{T.OPUS55}&project={T.DEV}'); dispatchEvent(new PopStateEvent('popstate'))",
            isolated=False)
        await oracle.wait_for(p, "document.body.getAttribute('data-shown')==='terms'")
        btn = "document.querySelector('.tp-btns .btn.primary')"
        assert await p.evaluate(f"{btn}.disabled", isolated=False)
        assert await p.evaluate(f"{btn}.getAttribute('aria-label')", isolated=False) == (
            "Agree to the terms and agreements before continuing")
        r = await p.evaluate(
            "fetch('/console/api/agree',{method:'POST',headers:{'Content-Type':'application/json'},"
            f"body:JSON.stringify({json.dumps({'model': T.OPUS55, 'project': T.DEV})})}})"
            ".then(function(r){return r.status})", isolated=False, await_promise=True)
        assert r == 400 and not any(e["event"] == "agree" for e in ep.events)

    _run(fn)
