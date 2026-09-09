"""Tests for ToolRegistry.unregister() (design doc section 1.6).

unregister(name) must remove BOTH the tool object and its catalog entry
(everything register() populates), return True if anything was removed,
and be a safe no-op (returning False) for never-registered names.
"""

from engine.tools import ToolDefinition, ToolRegistry, ToolTier
from engine.types import ToolCall, ToolResult


class _DummyTool:
    def __init__(self, name: str, tier: str = "hot") -> None:
        self.definition = ToolDefinition(
            name=name,
            description=f"{name} does things",
            summary=f"{name} summary",
            parameters={"type": "object", "properties": {}},
            tier=ToolTier(tier),
        )
        self.calls: list[dict] = []

    async def execute(self, call_id: str, arguments: dict) -> ToolResult:
        self.calls.append(arguments)
        return ToolResult(call_id=call_id, content="ran")


def _registry_with(*tools: _DummyTool) -> ToolRegistry:
    reg = ToolRegistry()
    for tool in tools:
        reg.register(tool)
    return reg


# ─── basic removal ────────────────────────────────────────────────────


def test_unregister_returns_true_for_registered_tool() -> None:
    reg = _registry_with(_DummyTool("alpha"))
    assert reg.unregister("alpha") is True


def test_unregister_returns_false_for_unknown_name() -> None:
    reg = _registry_with(_DummyTool("alpha"))
    assert reg.unregister("never_registered") is False
    # The registered tool is untouched.
    assert reg.get("alpha") is not None


def test_unregister_on_empty_registry_is_safe() -> None:
    reg = ToolRegistry()
    assert reg.unregister("anything") is False


def test_unregister_twice_returns_false_second_time() -> None:
    reg = _registry_with(_DummyTool("alpha"))
    assert reg.unregister("alpha") is True
    assert reg.unregister("alpha") is False


# ─── catalog/visibility bookkeeping ───────────────────────────────────


def test_unregistered_tool_disappears_from_list_definitions() -> None:
    reg = _registry_with(_DummyTool("alpha"), _DummyTool("beta"))
    assert {d.name for d in reg.list_definitions()} == {"alpha", "beta"}

    reg.unregister("alpha")

    assert {d.name for d in reg.list_definitions()} == {"beta"}
    assert {d.name for d in reg.list_all_definitions()} == {"beta"}


def test_unregistered_tool_disappears_from_list_summaries() -> None:
    reg = _registry_with(_DummyTool("alpha"), _DummyTool("beta", tier="warm"))
    assert set(reg.list_summaries()) == {"alpha", "beta"}

    reg.unregister("beta")

    assert set(reg.list_summaries()) == {"alpha"}
    assert set(reg.list_summary_tiers()) == {"alpha"}


def test_unregister_removes_tool_object_and_catalog_entry() -> None:
    reg = _registry_with(_DummyTool("alpha"))
    reg.unregister("alpha")

    assert reg.get("alpha") is None
    assert reg.get_catalog_entry("alpha") is None
    assert "alpha" not in reg.list_names()
    assert len(reg) == 0
    assert "alpha" not in reg


# ─── execute dispatch ─────────────────────────────────────────────────


async def test_execute_dispatch_fails_after_unregister() -> None:
    tool = _DummyTool("alpha")
    reg = _registry_with(tool)

    ok = await reg.execute(ToolCall(id="c1", name="alpha", arguments={}))
    assert ok.is_error is False
    assert tool.calls == [{}]

    reg.unregister("alpha")

    result = await reg.execute(ToolCall(id="c2", name="alpha", arguments={}))
    assert result.is_error is True
    assert "Unknown tool" in result.content
    # The removed tool was never invoked again.
    assert tool.calls == [{}]


# ─── promote interaction ──────────────────────────────────────────────


def test_register_promote_unregister_leaves_no_trace() -> None:
    reg = _registry_with(_DummyTool("cold_tool", tier="cold"))
    # Cold tools start fully hidden.
    assert reg.list_definitions() == []
    assert reg.list_summaries() == {}
    assert reg.hidden_tool_count() == 1

    promoted = reg.promote_tool("cold_tool")
    assert promoted is not None
    assert {d.name for d in reg.list_definitions()} == {"cold_tool"}
    assert set(reg.list_summaries()) == {"cold_tool"}

    assert reg.unregister("cold_tool") is True

    assert reg.list_definitions() == []
    assert reg.list_all_definitions() == []
    assert reg.list_summaries() == {}
    assert reg.list_summary_tiers() == {}
    assert reg.hidden_tool_count() == 0
    assert reg.list_names() == []
    assert reg.get("cold_tool") is None
    assert reg.get_catalog_entry("cold_tool") is None


def test_promote_after_unregister_returns_none() -> None:
    reg = _registry_with(_DummyTool("alpha"))
    reg.unregister("alpha")
    assert reg.promote_tool("alpha") is None


def test_reregister_after_unregister_restores_default_visibility() -> None:
    reg = _registry_with(_DummyTool("warm_tool", tier="warm"))
    reg.promote_tool("warm_tool")  # schema now visible
    reg.unregister("warm_tool")

    # Re-registering the same name must not inherit promoted visibility.
    reg.register(_DummyTool("warm_tool", tier="warm"))
    entry = reg.get_catalog_entry("warm_tool")
    assert entry is not None
    assert entry.summary_visible is True
    assert entry.schema_visible is False
    assert {d.name for d in reg.list_definitions()} == set()
