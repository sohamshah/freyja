"""The `traps` env: every task is finishable by its privileged solver with the
dark-pattern / injection quirks OFF (the accessible control twin), and an
untouched page scores zero. The shared tests/test_cua_envs.py already proves the
same with quirks ON. Needs a headless Chromium; skips without one.
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
from computer_use.envs.traps.backend import QUIRKS  # noqa: E402
from computer_use.run import make_server  # noqa: E402
from computer_use.tasks import select  # noqa: E402

pytestmark = pytest.mark.skipif(find_chrome() is None, reason="no Chromium for the environments")

TRAPS = [t for t in sorted(oracle.SOLVERS) if t.startswith("traps/")]


async def _solve(task_id: str, quirks: dict):
    server = make_server()
    b = await Browser.launch(width=1280, height=900)
    try:
        t = select([task_id])[0]
        cfg = dict(t.config)
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


@pytest.mark.parametrize("task_id", TRAPS)
def test_traps_solvable_with_quirks_off(task_id):
    """With every quirk off each surface is its accessible twin; the same tasks
    still finish, so a quirk can be switched off to measure one fix alone."""
    before, after = asyncio.run(_solve(task_id, {k: False for k in QUIRKS}))
    assert before.reward == 0.0, f"untouched page already scored: {before.marks}"
    assert after.reward == 1.0, after.why
