"""Elicitation bridge: routes MCP ``elicitation/create`` requests to a UI
surface and waits for the operator's answer.

Contract (fixed; the desktop renderer and the Slack gateway both build
against it — see card_019):

    bridge -> UI : {type:'mcp_elicitation', requestId, server, message,
                    mode:'form'|'url', requestedSchema?, url?, timeoutS}
    UI -> bridge : {type:'mcp_elicitation_response', requestId,
                    action:'accept'|'decline'|'cancel', content?}

``ElicitationBridge`` is an ``ApprovalHandler`` (``ElicitRequest ->
answer``) suitable for ``McpManager(approval_handler=...)``. Calling it
emits the request through the injected ``emit`` sink (sync or async),
parks a future keyed by ``requestId`` and resolves it when
:meth:`resolve` is called with the matching id. No answer within
``timeout_s`` -> ``cancel`` (fail closed); the connection's own
``elicitation_timeout_s`` guard cancels the awaiting task, which is
handled the same way. Answers are validated/coerced against the
requested JSON schema so a Slack ``key=value`` reply and a typed desktop
form produce the same content shape.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import time
import uuid
from typing import Any, Callable

from bridge.mcp.connection import DEFAULT_ELICITATION_TIMEOUT_S, ElicitRequest

logger = logging.getLogger(__name__)

VALID_ELICITATION_ACTIONS = ("accept", "decline", "cancel")

EmitSink = Callable[[dict[str, Any]], Any]
"""Receives the ``mcp_elicitation`` event dict; may return an awaitable."""


class ElicitationBridge:
    """Pending-request registry + ApprovalHandler for one process."""

    def __init__(
        self,
        emit: EmitSink,
        *,
        timeout_s: float = DEFAULT_ELICITATION_TIMEOUT_S,
        id_factory: Callable[[], str] | None = None,
    ) -> None:
        self._emit = emit
        self.timeout_s = float(timeout_s)
        self._id_factory = id_factory or (lambda: f"elicit-{uuid.uuid4().hex[:12]}")
        self._pending: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._requests: dict[str, dict[str, Any]] = {}
        self.stats = {"emitted": 0, "accepted": 0, "declined": 0, "cancelled": 0, "timed_out": 0}

    # -- ApprovalHandler -------------------------------------------------------

    async def __call__(self, request: ElicitRequest) -> dict[str, Any]:
        request_id = self._id_factory()
        loop = asyncio.get_running_loop()
        future: asyncio.Future[dict[str, Any]] = loop.create_future()
        event = self.to_event(request, request_id)
        self._pending[request_id] = future
        self._requests[request_id] = {**event, "since": time.time()}
        self.stats["emitted"] += 1
        try:
            try:
                result = self._emit(event)
                if inspect.isawaitable(result):
                    await result
            except Exception:  # noqa: BLE001
                logger.exception(
                    "mcp elicitation '%s': emit failed; declining request %s",
                    request.server, request_id,
                )
                self.stats["declined"] += 1
                return {"action": "decline"}
            try:
                answer = await asyncio.wait_for(future, timeout=self.timeout_s)
            except (asyncio.TimeoutError, TimeoutError):
                logger.warning(
                    "mcp elicitation '%s': no answer to %s within %gs; cancelling",
                    request.server, request_id, self.timeout_s,
                )
                self.stats["timed_out"] += 1
                return {"action": "cancel"}
        finally:
            self._pending.pop(request_id, None)
            self._requests.pop(request_id, None)
        bucket = {"accept": "accepted", "cancel": "cancelled"}.get(answer["action"], "declined")
        self.stats[bucket] += 1
        return answer

    # -- resolution ------------------------------------------------------------

    def resolve(
        self,
        request_id: str,
        action: str,
        content: dict[str, Any] | None = None,
    ) -> bool:
        """Settle a pending request. Returns False for unknown/stale ids or
        an invalid action (nothing happens in that case)."""
        request_id = str(request_id or "")
        future = self._pending.get(request_id)
        if future is None or future.done():
            return False
        action = str(action or "").strip().lower()
        if action not in VALID_ELICITATION_ACTIONS:
            return False
        answer: dict[str, Any] = {"action": action}
        if action == "accept":
            answer["content"] = dict(content) if isinstance(content, dict) else {}
        future.set_result(answer)
        return True

    def pending(self) -> list[dict[str, Any]]:
        """Outstanding requests (event shape + ``since``), oldest first."""
        return [dict(v) for v in sorted(self._requests.values(), key=lambda r: r["since"])]

    def get(self, request_id: str) -> dict[str, Any] | None:
        entry = self._requests.get(str(request_id or ""))
        return dict(entry) if entry is not None else None

    # -- wire shape --------------------------------------------------------------

    def to_event(self, request: ElicitRequest, request_id: str) -> dict[str, Any]:
        mode = "url" if str(request.mode or "form").lower() == "url" else "form"
        event: dict[str, Any] = {
            "type": "mcp_elicitation",
            "requestId": request_id,
            "server": request.server,
            "message": request.message,
            "mode": mode,
            "timeoutS": self.timeout_s,
        }
        if request.requested_schema is not None:
            event["requestedSchema"] = request.requested_schema
        if request.url:
            event["url"] = request.url
        return event


# ---------------------------------------------------------------------------
# Text answers (Slack ``/mcp answer <id> key=value ...``)
# ---------------------------------------------------------------------------

_TRUE_WORDS = ("true", "yes", "y", "1", "on")
_FALSE_WORDS = ("false", "no", "n", "0", "off")


def parse_answer_tokens(tokens: list[str]) -> tuple[str, dict[str, str]]:
    """``["decline"]`` -> ``("decline", {})``; ``["confirm=yes", "n=3"]`` ->
    ``("accept", {"confirm": "yes", "n": "3"})``; ``["accept"]`` /
    ``[]`` -> accept with no fields (URL mode confirmation)."""
    action = "accept"
    fields: dict[str, str] = {}
    for tok in tokens:
        low = tok.strip().lower()
        if not low:
            continue
        if low in VALID_ELICITATION_ACTIONS and "=" not in tok:
            action = low
            continue
        if "=" not in tok:
            raise ValueError(
                f"unrecognized answer token {tok!r} (expected key=value, accept, decline or cancel)"
            )
        key, _, value = tok.partition("=")
        key = key.strip()
        if not key:
            raise ValueError(f"empty field name in {tok!r}")
        fields[key] = value.strip().strip('"').strip("'")
    return action, fields


def coerce_answer(
    schema: dict[str, Any] | None, fields: dict[str, Any]
) -> tuple[dict[str, Any], list[str]]:
    """Coerce string field values against the flat-object JSON schema the
    server requested (string / number / integer / boolean / enum). Returns
    ``(content, problems)``; problems name unknown fields, type errors
    and missing required fields so the surface can ask again."""
    props: dict[str, Any] = {}
    required: list[str] = []
    if isinstance(schema, dict):
        raw_props = schema.get("properties")
        if isinstance(raw_props, dict):
            props = raw_props
        raw_required = schema.get("required")
        if isinstance(raw_required, list):
            required = [str(r) for r in raw_required]
    content: dict[str, Any] = {}
    problems: list[str] = []
    for key, value in fields.items():
        prop = props.get(key)
        if prop is None and props:
            problems.append(f"unknown field '{key}' (expected: {', '.join(sorted(props))})")
            continue
        if not isinstance(value, str):
            content[key] = value
            continue
        ptype = (prop or {}).get("type")
        if isinstance(ptype, list):
            ptype = next((t for t in ptype if t != "null"), None)
        enum = (prop or {}).get("enum")
        try:
            if isinstance(enum, list) and enum:
                match = next(
                    (e for e in enum if str(e).lower() == value.lower()), None
                )
                if match is None:
                    raise ValueError(f"expected one of {', '.join(str(e) for e in enum)}")
                content[key] = match
            elif ptype == "boolean":
                low = value.lower()
                if low in _TRUE_WORDS:
                    content[key] = True
                elif low in _FALSE_WORDS:
                    content[key] = False
                else:
                    raise ValueError("expected yes/no")
            elif ptype == "integer":
                content[key] = int(value)
            elif ptype == "number":
                content[key] = float(value)
            else:
                content[key] = value
        except ValueError as exc:
            problems.append(f"field '{key}': {exc}")
    for key in required:
        if key not in content and key not in fields:
            problems.append(f"missing required field '{key}'")
    return content, problems


def describe_schema_fields(schema: dict[str, Any] | None) -> list[str]:
    """Human-readable one-liners per requested field (for the Slack post)."""
    if not isinstance(schema, dict):
        return []
    props = schema.get("properties")
    if not isinstance(props, dict):
        return []
    required = set(schema.get("required") or [])
    lines: list[str] = []
    for key, prop in props.items():
        prop = prop if isinstance(prop, dict) else {}
        ptype = prop.get("type", "string")
        if isinstance(ptype, list):
            ptype = "/".join(str(t) for t in ptype)
        desc = str(prop.get("description") or prop.get("title") or "").strip()
        enum = prop.get("enum")
        extra = f" one of [{', '.join(str(e) for e in enum)}]" if isinstance(enum, list) else ""
        req = " (required)" if key in required else ""
        lines.append(f"{key}: {ptype}{extra}{req}" + (f" — {desc}" if desc else ""))
    return lines


__all__ = [
    "VALID_ELICITATION_ACTIONS",
    "ElicitationBridge",
    "coerce_answer",
    "describe_schema_fields",
    "parse_answer_tokens",
]
