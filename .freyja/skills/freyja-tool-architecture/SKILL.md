---
name: freyja-tool-architecture
type: build
description: "How Freyja's agent-tool subsystem works and how to add a callable tool: Tool protocol, progressive disclosure, permission gating, registry wiring, and session lifecycle for background monitors. Use when adding or debugging a tool, ToolDefinition, ToolRegistry, or asynchronous monitor integration."
triggers:
- add a tool to Freyja
- ToolDefinition
- monitor lifecycle integration
tags:
- tools
- registry
- monitor
- background
confidence: experimental
---

# Freyja Tool Architecture

How Freyja's agent-tool subsystem is wired, and the exact steps to add a new tool. Applies when working in `~/personal/freyja` to add, extend, or debug a tool the agent/LLM can call — including wrapping an external HTTP API (e.g. an image/SVG generation service) as a callable tool.

## Where things live

- `engine/tools.py` — canonical types: `ToolTier`, `ToolDefinition`, `ToolCatalogEntry`, the `Tool` Protocol, and `ToolRegistry`. This is the source of truth.
- `bridge/tools/base.py` — a **type shim** that re-exports `ToolDefinition`, `ToolRegistry`, `ToolResult`, `ToolTier`, content blocks, and `PermissionLevel`/`PermissionRequest` from `engine`. Every bridge tool imports its types from here, never from `engine` directly. It's the one file coupling the bridge to the engine — keep new tools importing from `base`.
- `bridge/tools/<your_tool>.py` — one file per tool (see `bash_tool.py`, `search_tools.py`, `image_generation_tool.py`, `artifacts_tool.py` as templates).
- `bridge/tools/registry.py` — `build_desktop_registry(...)` is the single place every tool is instantiated and appended to the `tools` list, then registered in one loop. A synchronous tool starts with a file and registry entry; background tools also need session-lifecycle wiring.

## The Tool protocol (duck-typed)

A tool is any object satisfying the `Tool` Protocol — no base class to subclass:

1. A `definition` property returning a `ToolDefinition`.
2. An `async def execute(self, call_id: str, arguments: dict) -> ToolResult`.

```python
from bridge.tools.base import ToolDefinition, ToolResult, ToolTier

class QuiverSvgGenerateTool:
    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="quiver_svg_generate",          # ^[a-zA-Z0-9_-]+$ (Cerebras-safe)
            summary="Generate an SVG from a text prompt",   # one line, shown in system prompt listing
            description="Detailed schema-level description the model reads...",
            parameters={                          # JSON Schema
                "type": "object",
                "properties": {"prompt": {"type": "string"}},
                "required": ["prompt"],
            },
            tier=ToolTier.WARM,
        )

    async def execute(self, call_id: str, arguments: dict) -> ToolResult:
        ...
        return ToolResult(...)
```

`ToolDefinition.to_dict()` emits `{name, description, input_schema}` for the provider API — note `parameters` is surfaced as `input_schema`.

## Tiers (progressive disclosure)

`ToolTier` controls how much of the tool the model sees up front:

- `HOT` — full schema always in context. Use for cheap, ubiquitous tools (read_file, bash).
- `WARM` — only the one-line `summary` is listed; the model must call `tool_search` to load the full schema before first use. Use for tools that are expensive or rarely needed (image generation is WARM). **An external paid API tool should be WARM** so the model deliberately opts in before spending.
- `COLD` — hidden until explicitly surfaced.

## `strict` and schema constraints

`ToolDefinition.strict=True` enables constrained decoding on Cerebras (zai-glm) and OpenAI (gpt-4o+); Fireworks and Anthropic ignore it. When `strict=True` the `parameters` schema must obey the strict JSON Schema subset: `additionalProperties:false` on every object, every field in `required`, and no `pattern`/`minItems`/`maxItems`/recursive `$ref`. Leave `strict=False` (default) if you need looser schemas.

## Permission gating

To make a tool prompt for human approval, give it BOTH:

- `requires_permission = True`
- a `permission_prompt(...)` callable returning a `PermissionRequest(prompt, level, details)` with a `PermissionLevel` (LOW/MEDIUM/HIGH/DANGEROUS).

`ToolRegistry.register` raises `ValueError` if one is present without the other — they must be added together. Mutating/external tools (bash, file writes) follow this pattern.

## Rich returns

`ToolResult` can carry text plus `ContentBlock`s (`ImageBlock`, `DocumentBlock`, `TextBlock`) for tools that return images or files. For generated binary assets, follow `image_generation_tool.py`: write to the project output dir and return a stable ref the agent can re-view after compaction (pass the session `image_store`).

## Registering a synchronous tool

In `bridge/tools/registry.py`, inside `build_desktop_registry(...)`:

1. Import the tool at the top.
2. `tools.append(MyTool(...))` in the relevant section. Gate it behind a flag/env check if it needs a key (mirror `include_web`, which only registers web tools when `PARALLEL_API_KEY` is set and the SDK imports).
3. The trailing loop registers everything, skips names in `excluded`, and logs the final set. Registration failures are caught and warned, not fatal.

## Background monitoring and session lifetime

For choosing `monitor` versus `bash`, `schedule`, or native inbox notifications, load `freyja-monitoring`. Do not replace subagent/talk notifications with shell polling.

`monitor` is a WARM, root-session-owned tool, registered alongside `bash` under `include_bash` and excluded from default child-tool inheritance. Its implementation is in `bridge/tools/monitor_tool.py`; notification framing and unread coalescing are in `bridge/tools/monitor_events.py`.

Background execution requires more than a registry entry. `ToolContext` in `bridge/tools/background_shell.py` carries the owning session's background-command registry, event/exit callbacks, output directory, and stopping check. The bridge routes events through the inbox, uses a monitor-specific idle wake prompt, and prevents late callbacks from reviving closed sessions. Keep source output explicitly untrusted and preserve command permission checks.

Preserve cleanup across start/cancel races, deadlines, explicit stop, normal exit with surviving descendants, and session reset/deletion/shutdown. Do not create detached tasks or processes with no owner. Test busy-session delivery, idle wake-up, slow cancellation, bounded noisy output, and process-group cleanup; the focused regressions are `tests/test_monitor_tool.py` and `tests/test_monitor_delivery.py`.

## Wrapping an external HTTP API as a tool — checklist

1. Load the key from env / secret manager inside `execute` (never hardcode). Gate registration on the env var being present, like `include_web`.
2. Make the HTTP call async (httpx.AsyncClient or equivalent); `execute` is async-native.
3. Map provider errors to a clear `ToolResult` error string — the model reads it and retries.
4. Set `tier=WARM` for paid/heavy APIs so the schema loads on demand via `tool_search`.
5. Keep the `summary` action-oriented and the `description` precise about inputs, limits, and cost so the model picks the right tool.
