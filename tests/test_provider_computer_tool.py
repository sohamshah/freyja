from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from bridge.tools.base import ImageBlock, TextBlock, ToolDefinition, ToolResult
from bridge.tools.provider_computer_tool import OpenAIComputerToolAdapter
from engine.provider_native import OPENAI_COMPUTER_TOOL_NAME


@dataclass
class FakeTool:
    name: str
    calls: list[dict[str, Any]] = field(default_factory=list)

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name=self.name,
            summary=self.name,
            description=self.name,
        )

    async def execute(self, call_id: str, arguments: dict[str, Any]) -> ToolResult:
        self.calls.append({"call_id": call_id, "arguments": arguments})
        if self.name == "screenshot":
            return ToolResult(
                call_id=call_id,
                content=[
                    TextBlock(text="captured"),
                    ImageBlock.from_base64("aW1n", "image/png"),
                ],
            )
        return ToolResult(call_id=call_id, content=f"{self.name} ok")


@pytest.mark.asyncio
async def test_openai_computer_adapter_executes_actions_and_returns_screenshot() -> None:
    click = FakeTool("click")
    screenshot = FakeTool("screenshot")
    adapter = OpenAIComputerToolAdapter(
        {
            "click": click,
            "screenshot": screenshot,
        }
    )

    result = await adapter.execute(
        "cu-1",
        {
            "actions": [
                {"type": "click", "x": 10, "y": 20, "button": "left"},
                {"type": "screenshot"},
            ]
        },
    )

    assert adapter.definition.name == OPENAI_COMPUTER_TOOL_NAME
    assert result.is_error is False
    assert isinstance(result.content, list)
    assert isinstance(result.content[0], TextBlock)
    assert isinstance(result.content[-1], ImageBlock)
    assert click.calls[0]["arguments"]["x"] == 10
    assert click.calls[0]["arguments"]["y"] == 20
    assert len(screenshot.calls) == 2
