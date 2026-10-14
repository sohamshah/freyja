"""Tasks, checks and the env registry.

A task is a goal on a page plus a check. The check reads ground truth (the
state the env server holds, the events the page reported) and the run's result,
and returns named marks: the reward is 1 only when every required mark holds
and no forbidden thing happened; the marks themselves are the dense signal.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parent
REPO = ROOT.parents[1]


@dataclass
class Check:
    reward: float
    marks: dict[str, bool]
    forbidden: list[str]
    why: str


def result(
    marks: dict[str, bool], *, required: list[str] | None = None, forbidden: list[str] | None = None
) -> Check:
    req = required or list(marks)
    bad = list(forbidden or [])
    ok = all(marks.get(k) for k in req) and not bad
    missing = [k for k in req if not marks.get(k)]
    why = (
        "ok"
        if ok
        else "; ".join(
            filter(
                None,
                [
                    ("missing: " + ", ".join(missing)) if missing else "",
                    ("forbidden: " + ", ".join(bad)) if bad else "",
                ],
            )
        )
    )
    return Check(1.0 if ok else 0.0, marks, bad, why)


CheckFn = Callable[[dict[str, Any], list[dict[str, Any]], dict[str, Any]], Check]


@dataclass
class Task:
    id: str
    env: str
    start: str  # path within the env's site, opened before the run
    goal: str
    check: CheckFn
    config: dict[str, Any] = field(default_factory=dict)  # env config (preset, quirks, ...)
    max_steps: int = 40
    timeout_s: float = 600
    allow_irreversible: bool = False
    items: list[str] | None = None
    tags: list[str] = field(default_factory=list)
    # A calling agent's way: one goal per page, run one after another on the same
    # tab; the episode stops at the first goal that does not end done.
    chain: list[str] | None = None


# ─── the old live fixtures (tests/fixtures/jev_live), as regression tasks ───

FIXTURES = REPO / "tests" / "fixtures" / "jev_live"
# The cloud-console scenarios: the `console` env replaces their pages here, which
# were too simple to say much about the real console. The Arc harness keeps them.
CONSOLE = {"questionnaire", "picker_list", "catalog_read", "catalog_details"}


def _fixture_tasks() -> list[Task]:
    """The `dom` scenarios of scripts/jev_scenarios.py, checked by the same
    functions (they read the events the fixture pages post)."""
    sys.path.insert(0, str(REPO / "scripts"))
    import jev_scenarios as js  # noqa: PLC0415

    out = []
    for s in js.SCENARIOS:
        if s.group != "dom" or "items" in s.args or s.name in CONSOLE:
            continue  # for-each runs go through run_items, which episodes do not drive yet
        env = "basics"

        def check(
            st: dict[str, Any], ev: list[dict[str, Any]], run: dict[str, Any], _s: Any = s
        ) -> Check:
            c = js.Ctx(text=run.get("text", ""), status=run.get("status", ""), events=ev, out=run)
            ok, why = _s.check(c)
            return Check(1.0 if ok else 0.0, {"check": ok}, [], why)

        args = dict(s.args)
        start = s.start or "index.html"
        out.append(
            Task(
                f"{env}/{s.name}",
                env,
                start,
                args["goal"],
                check,
                items=args.get("items"),
                config={"fixture": True, "goal_has_base": "{base}" in args["goal"]},
            )
        )
    return out


def all_tasks() -> list[Task]:
    from . import envs  # noqa: PLC0415

    return [*(t for n in envs.NAMES for t in envs.tasks(n).TASKS), *_fixture_tasks()]


def select(names: list[str]) -> list[Task]:
    """Tasks by id, env name or tag; all when empty."""
    tasks = all_tasks()
    if not names:
        return tasks
    out = []
    for t in tasks:
        if (
            t.id in names
            or t.env in names
            or any(x in t.tags for x in names)
            or t.id.split("/")[-1] in names
        ):
            out.append(t)
    return out
