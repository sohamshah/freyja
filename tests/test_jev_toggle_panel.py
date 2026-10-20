"""A control whose second click closes the panel its first click opened.

On a token form Jev clicked "Add permissions", the permission list opened, and
on the next step Jev clicked "Add permissions" again, which closed the list. It
did so twice and the run stopped with no permission added.
"""

from __future__ import annotations

import json
import re
from types import SimpleNamespace

from tests.test_jev_dom_surface import FakePage, action, dom_operator, run, snap
from tests.test_jev_operator import ScriptedProvider

OPENER = "Add permissions"


class PermissionForm(FakePage):
    """A form whose "Add permissions" button opens a list placed before the form
    in the page, so opening it renumbers the form's controls. A second click on
    the button closes the list. "Storage objects" adds the permission and closes
    the list; "Secrets" is marked in the list and leaves it open."""

    def __init__(self):
        super().__init__([])
        self.open = False
        self.added: list[str] = []
        self.marked: list[str] = []
        self._render()

    def _render(self):
        form = [action(1, "fill", "Key name"), action(2, label=OPENER, expanded=self.open),
                action(3, label="Generate key")]
        panel = [action(10, "fill", "Search", focused=True), action(11, label="Secrets"),
                 action(12, label="Storage objects")]
        self.actions = (panel if self.open else []) + form

    def __call__(self, bundle, js):
        if js.endswith("snapshot();})()"):
            text = "\n".join(
                ["New access key", *(["Secrets", "Storage objects"] if self.open else []),
                 "Permissions", *(self.added or ["No permissions added yet"]),
                 *(f"{m} (marked)" for m in self.marked)]
            )
            return json.dumps(snap(self.actions, text=text))
        m = re.search(r"act\((.*)\);\}\)\(\)$", js, re.S)
        if m:
            target = json.loads("[" + m.group(1) + "]")[0]
            label = next(a["label"] for a in self.actions if a["id"] == target)
            if label == OPENER:
                self.open = not self.open
            elif label == "Storage objects":
                self.added.append(label)
                self.open = False
            elif label == "Secrets":
                self.marked.append(label)
            out = super().__call__(bundle, js)
            self._render()
            return out
        return super().__call__(bundle, js)


def _clicks(page: PermissionForm) -> list[str]:
    return [a[0] for a in page.calls]


def test_a_second_click_on_the_control_that_just_opened_its_list_is_refused(tmp_path, monkeypatch):
    monkeypatch.setattr("bridge.tools.jev_operator.loop.RUN_DIR", tmp_path)
    page = PermissionForm()
    jev = ScriptedProvider(
        [("click", OPENER), ("click", OPENER), ("click", "Storage objects"), ("done", None)]
    )
    op = dom_operator(page, provider=jev, goal="add the Storage objects permission")
    res = run(op.run())
    assert res.status == "done", res.summary
    assert page.added == ["Storage objects"]
    assert _clicks(page) == [2, 12], "the opener was clicked once, then the permission"
    refused = [h for h in res.history if h.get("kind") == "refused"]
    assert len(refused) == 1 and "would close what it opened" in refused[0]["error"]


def test_the_control_can_close_its_list_once_the_run_has_used_the_list(tmp_path, monkeypatch):
    monkeypatch.setattr("bridge.tools.jev_operator.loop.RUN_DIR", tmp_path)
    page = PermissionForm()
    jev = ScriptedProvider(
        [("click", OPENER), ("click", "Secrets"), ("click", OPENER), ("done", None)]
    )
    op = dom_operator(page, provider=jev, goal="mark Secrets in the permission list")
    res = run(op.run())
    assert res.status == "done", res.summary
    assert _clicks(page) == [2, 11, 2] and not page.open
    assert not [h for h in res.history if h.get("kind") == "refused"]


def test_a_goal_or_sub_goal_that_asks_to_close_the_list_lets_the_click_through(
    tmp_path, monkeypatch
):
    monkeypatch.setattr("bridge.tools.jev_operator.loop.RUN_DIR", tmp_path)
    op = dom_operator(PermissionForm(), provider=ScriptedProvider([]), goal="add a permission")
    opener = SimpleNamespace(window="w", role="AXButton", label=OPENER, expanded=True,
                             toggle_state=lambda: None)
    op._opened_by_last = ("w", "AXButton", OPENER)
    assert op._reshuts(opener)
    op._subgoal = "Click Add permissions [2] again to close the list."
    assert not op._reshuts(opener)
    op._subgoal = None
    op.cfg.goal = "open the list, read the permissions, and close it"
    assert not op._reshuts(opener)
    # Shut already, or opened by an earlier action: nothing to protect.
    op.cfg.goal = "add a permission"
    assert not op._reshuts(SimpleNamespace(**{**vars(opener), "expanded": False}))
    op._opened_by_last = None
    assert not op._reshuts(opener)
