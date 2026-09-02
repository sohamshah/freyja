"""Gateway (Slack/Telegram/...) sessions run yolo; skills still don't.

Two independent guarantees, easy to break separately:

1. A chat-gateway session auto-approves every permission level including
   DANGEROUS, because a prompt there stalls the turn until the operator
   looks at their phone (and hard-denies after the 10-minute timeout).
2. Skill promotion is NOT a permission-tier decision — candidates ride a
   separate ``skill_candidate`` → operator promote/discard flow. If a
   future refactor ever routes promotion through ``request_permission``,
   yolo would start silently publishing skills; that is what test 2 is
   here to catch.
"""

from __future__ import annotations

import inspect

import pytest

from bridge.freyja_bridge import (
    DesktopPermissionHandler,
    _is_gateway_session_id,
    _parse_auto_approve,
)
from engine.permissions import PermissionLevel

GATEWAY_SESSION = "freyja:slack:C0AJF21GSE8:1234567890.123456"
DESKTOP_SESSION = "session-local-1"


def test_gateway_session_ids_are_recognized():
    assert _is_gateway_session_id(GATEWAY_SESSION)
    assert _is_gateway_session_id("freyja:telegram:99887766")
    assert not _is_gateway_session_id(DESKTOP_SESSION)
    # `freyja:foo` is not gateway-shaped (needs platform + chat id)
    assert not _is_gateway_session_id("freyja:foo")


def test_yolo_tier_covers_every_level_including_dangerous():
    assert _parse_auto_approve("yolo") == {
        PermissionLevel.LOW,
        PermissionLevel.MEDIUM,
        PermissionLevel.HIGH,
        PermissionLevel.DANGEROUS,
    }
    # The tier gateway sessions used to sit on still prompts for DANGEROUS.
    assert PermissionLevel.DANGEROUS not in _parse_auto_approve("high")


@pytest.mark.parametrize(
    "level",
    [
        PermissionLevel.LOW,
        PermissionLevel.MEDIUM,
        PermissionLevel.HIGH,
        PermissionLevel.DANGEROUS,
    ],
)
def test_yolo_handler_never_prompts(level):
    """Auto-approval must return a resolved answer, not a coroutine that
    would round-trip to a UI that may never answer."""
    handler = DesktopPermissionHandler(GATEWAY_SESSION, initial_tier="yolo")
    result = handler.request_permission(action="rm -rf ./build", level=level)
    assert not inspect.iscoroutine(result), f"{level} still round-trips to a prompt"
    assert result.approved


def test_dangerous_bash_still_prompts_on_the_desktop_default():
    """Desktop sessions keep the conservative default — the yolo change is
    scoped to gateway-routed sessions only."""
    handler = DesktopPermissionHandler(DESKTOP_SESSION, initial_tier="low")
    result = handler.request_permission(
        action="sudo rm -rf /", level=PermissionLevel.DANGEROUS
    )
    assert inspect.iscoroutine(result)
    result.close()  # never awaited; don't leak the coroutine


def test_skill_promotion_is_not_gated_on_permission_tier():
    """Promotion runs through confirmation.promote, reached only by an
    explicit operator action — never through the auto-approve tier."""
    from bridge.knowledge.learning import confirmation

    source = inspect.getsource(confirmation)
    assert "request_permission" not in source
    assert "_parse_auto_approve" not in source
    # promote() takes an explicit actor — it is an operator-initiated call.
    assert "actor" in inspect.signature(confirmation.promote).parameters


def test_skill_tools_declare_no_permission_prompt():
    """A tool only reaches the auto-approve tier if it exposes
    `permission_prompt`. The skill tools deliberately do not, so no tier
    (yolo included) can approve a promotion on the operator's behalf."""
    from bridge.tools import skill_tools

    for name, obj in vars(skill_tools).items():
        if not inspect.isclass(obj) or not name.endswith("Tool"):
            continue
        assert not hasattr(obj, "permission_prompt"), (
            f"{name} grew a permission_prompt — skill actions would now be "
            "auto-approvable by the gateway's yolo tier"
        )
