"""The traps env (evals/computer_use/envs/traps): every task is finishable by
its privileged solver with the dark-pattern and injection quirks on and with
them off (the accessible control twin), and an untouched page scores zero.
Needs a headless Chromium; skips without one."""

from __future__ import annotations

import pytest

from tests.cua_harness import envs, harness_fixture, oracle  # noqa: F401

pytestmark = pytest.mark.skipif("traps" not in envs.NAMES, reason="CUA_ENVS leaves out traps")
TRAPS = [t for t in sorted(oracle.SOLVERS) if t.startswith("traps/")]


@pytest.mark.parametrize("quirks", ["on", "off"])
@pytest.mark.parametrize("task_id", TRAPS)
def test_every_task_is_solvable_with_quirks_on_and_off(harness, task_id, quirks):
    """With every quirk off each surface is its accessible twin; the same tasks
    still finish, so a quirk can be switched off to measure one fix alone."""
    before, after = harness.solvable(task_id, quirks)
    assert before.reward == 0.0, f"untouched page already scored: {before.marks}"
    assert after.reward == 1.0, after.why
