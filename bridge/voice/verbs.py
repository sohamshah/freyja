"""Verb registry for the Galdr voice agent (contract §3, pinned).

A verb is one atomic Mac action ("spotify.play", "system.volume", …).
The registry renders three projections of the same catalog:

- `catalog_markdown()` — the human/model-readable verb table baked into
  the realtime session instructions.
- `openai_tool_schema()` — exactly ONE function tool named `act` whose
  `verb` enum is the registered names. A single tool keeps the realtime
  session config small and lets the catalog live in the instructions
  where it is cheaper to update.
- `responses_tools()` — one typed function tool per verb (`spotify_play`)
  for the GPT-Live seat's Responses backend (docs/GALDR-LIVE.md).

Execution semantics (tiers, confirm tokens, receipts, undo retention)
live in `bridge/voice/service.py`; adapters only return `VerbResult`s.
"""

from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional


@dataclass
class VerbResult:
    ok: bool
    summary: str  # one-line human outcome for receipt + HUD
    say: Optional[str] = None  # optional spoken-hint override for the model
    data: dict[str, Any] = field(default_factory=dict)  # structured payload for the model
    undo: Optional[Callable[[], Awaitable["VerbResult"]]] = None  # closure that reverses it
    error: Optional[str] = None
    # Visual computer control (contract §12.1): only computer.* verbs set
    # these. The service copies them onto the voice_tool_result event so
    # the renderer can inject the screenshot back into the realtime
    # conversation as an input_image. The image is an api_dims PNG (the
    # SAME coordinate space ClickTool accepts) with a light coordinate
    # grid drawn on — the model looks, then clicks by pixel.
    image_b64: Optional[str] = None  # base64 PNG (no data: prefix)
    image_w: Optional[int] = None  # image width, px (== api_dims width)
    image_h: Optional[int] = None  # image height, px


@dataclass
class Verb:
    name: str  # "spotify.play"
    description: str  # one line, goes into the model's verb table
    params: dict[str, Any]  # JSON-schema "properties" fragment
    required: list[str]
    tier: str  # "auto" | "confirm"
    run: Callable[[dict[str, Any]], Awaitable[VerbResult]]


class VerbRegistry:
    def __init__(self) -> None:
        self._verbs: dict[str, Verb] = {}

    def register(self, verb: Verb) -> None:
        # A duplicate name is a wiring bug (two adapters claiming the same
        # verb) — fail loudly at boot rather than silently shadowing.
        if verb.name in self._verbs:
            raise ValueError(f"verb already registered: {verb.name}")
        self._verbs[verb.name] = verb

    def get(self, name: str) -> Optional[Verb]:
        return self._verbs.get(name)

    def all(self) -> list[Verb]:
        return list(self._verbs.values())

    def catalog_markdown(self) -> str:
        """Verb table for the system prompt: `- name(args) — description [confirm]`."""
        lines: list[str] = []
        for verb in self._verbs.values():
            parts: list[str] = []
            for pname, spec in verb.params.items():
                ptype = spec.get("type", "any")
                # Optional args carry a `?` so the model doesn't invent
                # values for parameters it can simply omit.
                suffix = "" if pname in verb.required else "?"
                parts.append(f"{pname}{suffix}: {ptype}")
            line = f"- {verb.name}({', '.join(parts)}) — {verb.description}"
            if verb.tier == "confirm":
                line += " [requires spoken confirmation]"
            lines.append(line)
        return "\n".join(lines)

    def openai_tool_schema(self) -> dict[str, Any]:
        """ONE function tool named `act`; shape pinned by contract §3."""
        return {
            "type": "function",
            "name": "act",
            "description": (
                "Execute a Mac action. Pick verb from the catalog in your "
                "instructions; put its arguments in args."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "verb": {
                        "type": "string",
                        "enum": [verb.name for verb in self._verbs.values()],
                    },
                    "args": {"type": "object"},
                    "confirm_token": {"type": "string"},
                },
                "required": ["verb"],
            },
        }

    @staticmethod
    def tool_name(verb_name: str) -> str:
        """Function-tool name for a verb. Responses tool names allow only
        [a-zA-Z0-9_-], so the namespace dot becomes an underscore:
        `spotify.play` → `spotify_play`. Namespaces never contain an
        underscore, so the first one always marks the split."""
        return verb_name.replace(".", "_", 1)

    def resolve_tool_name(self, name: str) -> Optional[str]:
        """Inverse of `tool_name`: the registered verb a per-verb function
        tool stands for, or None. Also accepts the dotted verb name itself
        (a backend that echoes the catalog spelling still resolves)."""
        if name in self._verbs:
            return name
        for verb_name in self._verbs:
            if self.tool_name(verb_name) == name:
                return verb_name
        return None

    def responses_tools(self, exclude: tuple[str, ...] = ()) -> list[dict[str, Any]]:
        """One function tool per verb, for a GPT-Live Responses backend
        (docs/GALDR-LIVE.md). A reasoning backend picks far better from
        typed tools than from one `act` enum, and 50 schemas are cheap
        once cached. Confirm-tier verbs gain an optional `confirm_token`
        so the re-call after a spoken yes has a slot for it."""
        tools: list[dict[str, Any]] = []
        for verb in self._verbs.values():
            if verb.name in exclude:
                continue
            properties = dict(verb.params)
            description = verb.description
            if verb.tier == "confirm":
                properties["confirm_token"] = {
                    "type": "string",
                    "description": (
                        "Only when a previous result said CONFIRM REQUIRED "
                        "and the operator has assented: the token it gave."
                    ),
                }
                description += " Requires spoken confirmation."
            tools.append(
                {
                    "type": "function",
                    "name": self.tool_name(verb.name),
                    "description": description,
                    "parameters": {
                        "type": "object",
                        "properties": properties,
                        "required": list(verb.required),
                    },
                }
            )
        return tools


def build_default_registry() -> VerbRegistry:
    """Registers spotify.*, system.*, app.*, timer.*, slack.*, screen.*,
    reminders.*, notes.*, messages.*, contacts.*, calendar.*, mail.*,
    shortcuts.*.

    `mission.spawn` / `mission.status` / `computer.do` are registered by
    service.py (they need bridge session access). Adapters are imported
    lazily because they import Verb/VerbResult from this module.
    """
    from bridge.voice.adapters import register_all

    registry = VerbRegistry()
    register_all(registry)
    return registry
