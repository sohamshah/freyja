"""The computer-use environments (evals/computer_use): the airline site's tasks
can all be finished by the privileged scripted solver, a page nobody touched
scores zero, the accessible variant (every quirk off) is solvable too, and the
step API rewards progress. The other envs prove their own tasks the same way in
tests/test_cua_env_*.py. Needs a headless Chromium; skips without one."""

from __future__ import annotations

import pytest

from tests.cua_harness import harness_fixture, oracle, quirks_off, select  # noqa: F401

AIRLINE = [t for t in sorted(oracle.SOLVERS) if t.startswith("airline/")]


@pytest.mark.parametrize("task_id", AIRLINE)
def test_every_airline_task_can_be_finished_and_nothing_done_scores_zero(harness, task_id):
    before, after = harness.solvable(task_id)
    assert before.reward == 0.0, before.why
    assert after.reward == 1.0, after.why


def test_the_accessible_variant_is_solvable_too(harness):
    """With every quirk off each widget is its accessible version; the whole
    booking still finishes, so a quirk can be switched off to test one fix alone."""
    _, after = harness.run(harness.solve("airline/e2e", quirks=quirks_off("airline")))
    assert after.reward == 1.0, after.why


def test_the_step_interface_rewards_progress(harness):
    from computer_use.env import WebEnv

    async def go():
        task = select(["airline/seats"])[0]
        env = await WebEnv.create(task, server=harness.server, browser=harness.browser)
        try:
            obs = await env.reset()
            seat = next(i for i, r in enumerate(obs.rows) if "'seat: 1D legroom" in r)
            target = int(obs.rows[seat].split("]")[0].strip("["))
            obs, reward, done, info = await env.step({"op": "click", "target": target})
            return reward, done, info
        finally:
            await env.close()

    reward, done, info = harness.run(go())
    assert info["ok"] and info["marks"]["out:0"] is True
    assert reward > 0 and not done
