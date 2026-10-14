"""The computer-use environments (evals/computer_use): every airline task can be
finished (by a privileged scripted solver), a page nobody touched scores zero,
the quirk switches keep each task solvable, and the step API rewards progress.
Needs a headless Chromium; skips without one."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "evals"))

from computer_use import envs, oracle  # noqa: E402
from computer_use.browser import Browser, find_chrome  # noqa: E402
from computer_use.envs.airline.backend import QUIRKS  # noqa: E402
from computer_use.run import make_server  # noqa: E402
from computer_use.tasks import select  # noqa: E402

pytestmark = pytest.mark.skipif(find_chrome() is None, reason="no Chromium for the environments")


def _state(ep):
    return envs.backend(ep.env).view(ep)


async def _solve(task_id: str, quirks: dict | None = None):
    server = make_server()
    b = await Browser.launch(width=1280, height=900)
    try:
        t = select([task_id])[0]
        cfg = dict(t.config)
        if quirks is not None:
            cfg["quirks"] = quirks
        ep = server.new_episode(t.env, cfg)
        before = t.check(_state(ep), list(ep.events), {"status": "", "text": ""})
        p = await b.new_page(context=await b.new_context())
        await p.navigate(server.start_url(ep, f"/{t.env}/{t.start}"))
        run = await oracle.solve(task_id, p)
        after = t.check(_state(ep), list(ep.events), run)
        return before, after
    finally:
        await b.close()
        server.stop()


@pytest.mark.parametrize("task_id", sorted(oracle.SOLVERS))
def test_every_task_can_be_finished_and_nothing_done_scores_zero(task_id):
    if "slow" in select([task_id])[0].tags:
        pytest.skip("waits on a real timeline; its env's own test solves it on a shorter one")
    before, after = asyncio.run(_solve(task_id))
    assert before.reward == 0.0
    assert after.reward == 1.0, after.why


def test_the_accessible_variant_is_solvable_too():
    """With every quirk off each widget is its accessible version; the same
    tasks still finish, so a quirk can be switched off to test one fix alone."""
    _, after = asyncio.run(_solve("airline/e2e", {k: False for k in QUIRKS}))
    assert after.reward == 1.0, after.why


def test_the_step_interface_rewards_progress():
    from computer_use.env import WebEnv

    async def go():
        server = make_server()
        b = await Browser.launch(width=1280, height=900)
        env = await WebEnv.create(select(["airline/seats"])[0], server=server, browser=b)
        try:
            obs = await env.reset()
            seat = next(i for i, r in enumerate(obs.rows) if "'seat: 1D legroom" in r)
            target = int(obs.rows[seat].split("]")[0].strip("["))
            obs, reward, done, info = await env.step({"op": "click", "target": target})
            return reward, done, info
        finally:
            await env.close()
            await b.close()
            server.stop()

    reward, done, info = asyncio.run(go())
    assert info["ok"] and info["marks"]["out:0"] is True
    assert reward > 0 and not done
