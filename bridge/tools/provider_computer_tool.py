"""Provider-native computer tool adapters.

These are internal bridge tools. They are not meant to be advertised as normal
function tools; providers emit them after translating a native protocol item
into Freyja's shared ToolRegistry execution path.
"""

from __future__ import annotations

import logging
from typing import Any, Mapping

from bridge.tools.base import ImageBlock, TextBlock, ToolDefinition, ToolResult, ToolTier
from engine.provider_native import OPENAI_COMPUTER_TOOL_NAME

logger = logging.getLogger(__name__)


_MODIFIER_ALIASES = {
    "ALT": "alt",
    "CMD": "cmd",
    "COMMAND": "cmd",
    "CONTROL": "ctrl",
    "CTRL": "ctrl",
    "META": "cmd",
    "OPTION": "alt",
    "SHIFT": "shift",
}

_KEY_ALIASES = {
    "ARROWDOWN": "down",
    "ARROWLEFT": "left",
    "ARROWRIGHT": "right",
    "ARROWUP": "up",
    "BACKSPACE": "backspace",
    "DEL": "delete",
    "DELETE": "delete",
    "DOWN": "down",
    "END": "end",
    "ENTER": "return",
    "ESC": "escape",
    "ESCAPE": "escape",
    "HOME": "home",
    "LEFT": "left",
    "PAGEDOWN": "pagedown",
    "PAGEUP": "pageup",
    "RETURN": "return",
    "RIGHT": "right",
    "SPACE": "space",
    "TAB": "tab",
    "UP": "up",
}


def _action_to_dict(action: Any) -> dict[str, Any]:
    if isinstance(action, dict):
        return dict(action)
    if hasattr(action, "model_dump"):
        dumped = action.model_dump(exclude_none=True)
        if isinstance(dumped, dict):
            return dumped
    out: dict[str, Any] = {}
    for key in (
        "button",
        "keys",
        "path",
        "scrollX",
        "scrollY",
        "scroll_x",
        "scroll_y",
        "text",
        "type",
        "x",
        "y",
    ):
        value = getattr(action, key, None)
        if value is not None:
            out[key] = value
    return out


def _normalize_key(key: Any) -> str:
    raw = str(key)
    return _KEY_ALIASES.get(raw.upper(), raw.lower() if len(raw) > 1 else raw)


def _split_keys(keys: Any) -> tuple[list[str], list[str]]:
    modifiers: list[str] = []
    regular: list[str] = []
    for key in list(keys or []):
        raw = str(key)
        modifier = _MODIFIER_ALIASES.get(raw.upper())
        if modifier:
            if modifier not in modifiers:
                modifiers.append(modifier)
        else:
            regular.append(_normalize_key(raw))
    return modifiers, regular


def _extract_image(content: Any) -> ImageBlock | None:
    if not isinstance(content, list):
        return None
    for block in reversed(content):
        if isinstance(block, ImageBlock):
            return block
    return None


def _text_preview(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [getattr(block, "text", "") for block in content if hasattr(block, "text")]
        return " ".join(part for part in parts if part)
    return str(content)


class OpenAIComputerToolAdapter:
    """Execute OpenAI native computer actions through Freyja desktop tools."""

    def __init__(self, tools: Mapping[str, Any]) -> None:
        self._tools = dict(tools)

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name=OPENAI_COMPUTER_TOOL_NAME,
            summary="Internal OpenAI native computer call adapter",
            tier=ToolTier.COLD,
            description=(
                "Internal adapter for OpenAI computer_call actions. "
                "This tool is called by the OpenAI provider, not directly by models."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "actions": {"type": "array", "items": {"type": "object"}},
                    "action": {"type": "object"},
                },
            },
        )

    async def execute(self, call_id: str, arguments: dict[str, Any]) -> ToolResult:
        raw_actions = arguments.get("actions")
        if raw_actions is None:
            raw_actions = [arguments.get("action") or arguments]
        actions = [_action_to_dict(action) for action in list(raw_actions or [])]
        if not actions:
            actions = [{"type": "screenshot"}]

        summaries: list[str] = []
        latest_image: ImageBlock | None = None
        saw_error = False

        for index, action in enumerate(actions):
            action_type = str(action.get("type") or "").strip()
            if not action_type:
                summaries.append(f"{index + 1}. missing action type")
                saw_error = True
                continue

            result = await self._execute_action(f"{call_id}:{index}", action)
            latest_image = _extract_image(result.content) or latest_image
            saw_error = saw_error or result.is_error
            prefix = "error" if result.is_error else "ok"
            summaries.append(
                f"{index + 1}. {action_type}: {prefix} - {_text_preview(result.content)}"
            )

        final_capture = await self._execute_tool(
            "screenshot", f"{call_id}:final", {}
        )
        final_image = _extract_image(final_capture.content)
        if final_image is not None:
            latest_image = final_image
            summaries.append(f"final screenshot: {_text_preview(final_capture.content)}")
        elif final_capture.is_error:
            saw_error = True
            summaries.append(f"final screenshot failed: {_text_preview(final_capture.content)}")

        summary_text = "OpenAI computer actions executed:\n" + "\n".join(summaries)
        if latest_image is None:
            return ToolResult(
                call_id=call_id,
                content=summary_text + "\n\nNo screenshot image was available to return.",
                is_error=True,
            )

        return ToolResult(
            call_id=call_id,
            content=[TextBlock(text=summary_text), latest_image],
            is_error=saw_error,
        )

    async def _execute_action(self, call_id: str, action: dict[str, Any]) -> ToolResult:
        action_type = str(action.get("type") or "")

        if action_type == "screenshot":
            return await self._execute_tool("screenshot", call_id, {})

        if action_type == "wait":
            return await self._execute_tool("wait", call_id, {"ms": 1000})

        if action_type == "move":
            return await self._execute_tool(
                "move_mouse",
                call_id,
                {"x": int(action.get("x", 0)), "y": int(action.get("y", 0))},
            )

        if action_type == "click":
            button = str(action.get("button") or "left")
            modifiers, regular = _split_keys(action.get("keys"))
            if button == "back":
                return await self._execute_tool(
                    "press_key", call_id, {"key": "[", "modifiers": ["cmd"]}
                )
            if button == "forward":
                return await self._execute_tool(
                    "press_key", call_id, {"key": "]", "modifiers": ["cmd"]}
                )
            if button == "wheel":
                button = "middle"
            args = {
                "x": int(action.get("x", 0)),
                "y": int(action.get("y", 0)),
                "button": button,
                "modifiers": modifiers,
                "description": f"OpenAI computer click at ({action.get('x')}, {action.get('y')})",
            }
            if regular:
                logger.debug("ignoring non-modifier click keys: %s", regular)
            return await self._execute_tool("click", call_id, args)

        if action_type == "double_click":
            modifiers, _regular = _split_keys(action.get("keys"))
            return await self._execute_tool(
                "click",
                call_id,
                {
                    "x": int(action.get("x", 0)),
                    "y": int(action.get("y", 0)),
                    "button": str(action.get("button") or "left"),
                    "double": True,
                    "modifiers": modifiers,
                    "description": (
                        f"OpenAI computer double click at "
                        f"({action.get('x')}, {action.get('y')})"
                    ),
                },
            )

        if action_type == "type":
            return await self._execute_tool(
                "type_text", call_id, {"text": str(action.get("text") or "")}
            )

        if action_type == "keypress":
            return await self._execute_keypress(call_id, action.get("keys") or [])

        if action_type == "scroll":
            scroll_x = action.get("scroll_x", action.get("scrollX", 0))
            scroll_y = action.get("scroll_y", action.get("scrollY", 0))
            return await self._execute_tool(
                "scroll",
                call_id,
                {
                    "x": int(action.get("x", 0)),
                    "y": int(action.get("y", 0)),
                    "dx": int(scroll_x or 0),
                    "dy": int(scroll_y or 0),
                },
            )

        if action_type == "drag":
            return ToolResult(
                call_id=call_id,
                content=(
                    "drag is not implemented in Freyja's native computer "
                    "backend yet. Use click/move/scroll/key/type actions instead."
                ),
                is_error=True,
            )

        return ToolResult(
            call_id=call_id,
            content=f"Unsupported OpenAI computer action: {action_type}",
            is_error=True,
        )

    async def _execute_keypress(self, call_id: str, keys: Any) -> ToolResult:
        modifiers, regular = _split_keys(keys)
        if not regular and modifiers:
            regular = modifiers
            modifiers = []
        if not regular:
            return ToolResult(call_id=call_id, content="keypress had no keys", is_error=True)

        latest: ToolResult | None = None
        summaries: list[str] = []
        for index, key in enumerate(regular):
            latest = await self._execute_tool(
                "press_key",
                f"{call_id}:key:{index}",
                {"key": key, "modifiers": modifiers},
            )
            summaries.append(_text_preview(latest.content))
            if latest.is_error:
                break
        if latest is None:
            return ToolResult(
                call_id=call_id,
                content="keypress had no executable keys",
                is_error=True,
            )
        if isinstance(latest.content, list):
            return ToolResult(
                call_id=call_id,
                content=[TextBlock(text="\n".join(summaries)), *_image_tail(latest.content)],
                is_error=latest.is_error,
            )
        return ToolResult(
            call_id=call_id,
            content="\n".join(summaries) or latest.content,
            is_error=latest.is_error,
        )

    async def _execute_tool(
        self,
        tool_name: str,
        call_id: str,
        arguments: dict[str, Any],
    ) -> ToolResult:
        tool = self._tools.get(tool_name)
        if tool is None:
            return ToolResult(
                call_id=call_id,
                content=f"Internal OpenAI computer adapter missing tool: {tool_name}",
                is_error=True,
            )
        return await tool.execute(call_id, arguments)


def _image_tail(content: list[Any]) -> list[ImageBlock]:
    return [block for block in content if isinstance(block, ImageBlock)]
