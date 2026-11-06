"""The mail env (evals/computer_use/envs/mail): every task is finishable by its
privileged solver with the quirks on and with them off (the plain, accessible
control twins), and an untouched page scores 0. Needs a headless Chromium;
skips without one."""

from __future__ import annotations

import pytest

from tests.cua_harness import envs, harness_fixture, oracle  # noqa: F401

pytestmark = pytest.mark.skipif("mail" not in envs.NAMES, reason="CUA_ENVS leaves out mail")
MAIL = [t for t in sorted(oracle.SOLVERS) if t.startswith("mail/")]


@pytest.mark.parametrize("quirks", ["on", "off"])
@pytest.mark.parametrize("task_id", MAIL)
def test_every_task_is_solvable_with_quirks_on_and_off(harness, task_id, quirks):
    """With every quirk off each widget is its plain version; the same solver
    still finishes, so one quirk can be switched off to test a single fix."""
    before, after = harness.solvable(task_id, quirks)
    assert before.reward == 0.0, f"an untouched page should score 0: {before.why}"
    assert after.reward == 1.0, after.why
