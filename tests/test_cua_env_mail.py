"""The mail env: every task is finishable by its privileged solver with the
quirks off (the plain, accessible control twins) as well as on, and an untouched
page scores 0. The shared tests/test_cua_envs.py already proves the quirks-on
case for every registered env; this file adds the quirks-off case for `mail`.

Needs a headless Chromium; skips without one. Run it with the env active:

    CUA_ENVS=mail .venv/bin/python -m pytest -q tests/test_cua_env_mail.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "evals"))

from computer_use import envs, oracle  # noqa: E402
from computer_use.browser import Browser, find_chrome  # noqa: E402
from computer_use.envs.mail.backend import QUIRKS  # noqa: E402
from computer_use.run import make_server  # noqa: E402
from computer_use.tasks import select  # noqa: E402

pytestmark = pytest.mark.skipif(find_chrome() is None, reason="no Chromium for the environments")

MAIL_TASKS = [
    "mail/otp_signin",
    "mail/bulk_delete",
    "mail/icon_twins",
    "mail/refund_policy",
    "mail/meeting_from_email",
]


async def _solve(task_id: str, quirks: dict | None):
    server = make_server()
    b = await Browser.launch(width=1280, height=900)
    try:
        t = select([task_id])[0]
        cfg = dict(t.config)
        if quirks is not None:
            cfg["quirks"] = quirks
        ep = server.new_episode(t.env, cfg)
        state = envs.backend(t.env).view(ep)
        before = t.check(state, list(ep.events), {"status": "", "text": ""})
        p = await b.new_page(context=await b.new_context())
        await p.navigate(server.start_url(ep, f"/{t.env}/{t.start}"))
        run = await oracle.solve(task_id, p)
        after = t.check(envs.backend(t.env).view(ep), list(ep.events), run)
        return before, after
    finally:
        await b.close()
        server.stop()


@pytest.mark.parametrize("task_id", MAIL_TASKS)
def test_control_twins_are_solvable(task_id):
    """With every quirk off each widget is its plain version; the same solver
    still finishes, so one quirk can be switched off to test a single fix."""
    before, after = asyncio.run(_solve(task_id, {k: False for k in QUIRKS}))
    assert before.reward == 0.0, f"an untouched page should score 0: {before.why}"
    assert after.reward == 1.0, after.why
