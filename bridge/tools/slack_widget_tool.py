"""``slack_widget``: native Slack widgets inside the current reply.

Slack renders tables, charts, cards, collapsible groups, markdown and
embedded video itself, so the agent no longer draws a table in a code
fence or generates a chart image for numbers it already has. The tool
builds the Block Kit through ``bridge.gateway.slack_blocks`` (which
validates against Slack's limits), then puts the blocks into the live
streaming message at the point the agent called it. With no live
stream (the turn's stream failed, or a background sub-agent after its
parent's turn ended) the blocks go out as their own message in the
same thread.

Registered only for gateway sessions, next to ``send_attachment``.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Callable

from bridge.tools.base import ToolDefinition, ToolResult

logger = logging.getLogger(__name__)


def _build_definition() -> ToolDefinition:
    description = (
        "Show a native Slack widget in the current reply, at the point you "
        "call it. Kinds: `table` (sortable, paginated; pass `columns` and "
        "`rows`), `chart` (`chart_type` pie/bar/area/line with `series` or "
        "`segments`), `cards` (one card or a horizontal carousel of up to 10, "
        "each with title/subtitle/body/image_url/links), `container` (a titled, "
        "collapsible group of `sections` of markdown), `markdown` (standard "
        "markdown incl. tables, task lists and code fences, 12k chars) and "
        "`video` (an https embed URL with a thumbnail). Use `table` for any "
        "list of records with 3+ rows, `chart` for any numbers over time or "
        "across categories, `cards` to summarize entities (tickets, PRs, "
        "people, options). Plain prose keeps streaming before and after the "
        "widget. Limits: table 200 rows x 20 columns, chart 12 series x 20 "
        "points with labels of 20 chars, card body 200 chars."
    )
    return ToolDefinition(
        name="slack_widget",
        description=description,
        summary="Render a native Slack table, chart, card, container, markdown or video in the reply.",
        parameters={
            "type": "object",
            "properties": {
                "kind": {
                    "type": "string",
                    "enum": ["table", "chart", "cards", "container", "markdown", "video"],
                },
                "title": {"type": "string", "description": "Table caption, chart title, container title or video title."},
                "columns": {"type": "array", "items": {"type": "string"}, "description": "table: header labels."},
                "rows": {
                    "type": "array",
                    "items": {"type": "array", "items": {}},
                    "description": "table: one list per row; numbers stay numeric so sorting works.",
                },
                "page_size": {"type": "integer", "description": "table: rows per page (1 to 100, default 5 on Slack's side)."},
                "chart_type": {"type": "string", "enum": ["pie", "bar", "area", "line"]},
                "series": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string"},
                            "data": {
                                "type": "array",
                                "items": {
                                    "type": "object",
                                    "properties": {"label": {"type": "string"}, "value": {"type": "number"}},
                                    "required": ["label", "value"],
                                },
                            },
                        },
                        "required": ["name", "data"],
                    },
                    "description": "chart (bar/area/line): up to 12 series of up to 20 points.",
                },
                "segments": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {"label": {"type": "string"}, "value": {"type": "number"}},
                        "required": ["label", "value"],
                    },
                    "description": "chart (pie): 1 to 12 segments with positive values.",
                },
                "x_label": {"type": "string"},
                "y_label": {"type": "string"},
                "cards": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "title": {"type": "string"},
                            "subtitle": {"type": "string"},
                            "body": {"type": "string", "description": "200 chars max."},
                            "subtext": {"type": "string"},
                            "image_url": {"type": "string"},
                            "icon_url": {"type": "string"},
                            "links": {
                                "type": "array",
                                "items": {
                                    "type": "object",
                                    "properties": {"text": {"type": "string"}, "url": {"type": "string"}},
                                    "required": ["url"],
                                },
                                "description": "up to 3 link buttons.",
                            },
                        },
                    },
                },
                "sections": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "container: up to 10 short markdown sections (Slack mrkdwn: *bold*, _italic_, `code`).",
                },
                "subtitle": {"type": "string"},
                "collapsed": {"type": "boolean", "description": "container: start collapsed."},
                "text": {"type": "string", "description": "markdown: the standard markdown to render."},
                "video_url": {"type": "string", "description": "video: https embed URL."},
                "thumbnail_url": {"type": "string", "description": "video: https thumbnail URL."},
                "description": {"type": "string"},
            },
            "required": ["kind"],
        },
    )


class SlackWidgetTool:
    """Builds the blocks and hands them to the live stream consumer, or
    posts them in the thread when there is no live stream."""

    def __init__(
        self,
        *,
        get_gateway_source: Callable[[], Any],
        get_session: Callable[[], Any],
    ) -> None:
        self._get_gateway_source = get_gateway_source
        self._get_session = get_session
        self._defn = _build_definition()

    @property
    def definition(self) -> ToolDefinition:
        return self._defn

    @staticmethod
    def _coerce(value: Any) -> Any:
        """Models sometimes serialize a list or object as a JSON string."""
        if isinstance(value, str):
            stripped = value.strip()
            if stripped[:1] in "[{":
                try:
                    return json.loads(stripped)
                except ValueError:
                    return value
        return value

    def build_blocks(self, args: dict[str, Any]) -> list[dict[str, Any]]:
        from bridge.gateway import slack_blocks as b

        kind = str(args.get("kind") or "").strip().lower()
        title = str(args.get("title") or "")
        if kind == "table":
            columns = self._coerce(args.get("columns")) or []
            rows = self._coerce(args.get("rows")) or []
            if isinstance(rows, list) and rows and all(isinstance(r, dict) for r in rows):
                # Records: take the columns from the first one when absent.
                if not columns:
                    columns = list(rows[0].keys())
                rows = [[r.get(c) for c in columns] for r in rows]
            return [b.data_table_block(list(columns), list(rows), caption=title, page_size=args.get("page_size"))]
        if kind == "chart":
            return [b.chart_block(
                str(args.get("chart_type") or "bar"), title,
                series=self._coerce(args.get("series")),
                segments=self._coerce(args.get("segments")),
                x_label=args.get("x_label"), y_label=args.get("y_label"),
            )]
        if kind == "cards":
            cards = self._coerce(args.get("cards")) or []
            if not cards and (title or args.get("body")):
                cards = [{k: args.get(k) for k in ("title", "subtitle", "body", "subtext", "image_url", "icon_url", "links") if args.get(k)}]
            return b.card_blocks(list(cards))
        if kind == "container":
            sections = self._coerce(args.get("sections")) or []
            children = [b.section_block(str(sec)) for sec in sections if str(sec).strip()]
            return [b.container_block(
                title, children, subtitle=args.get("subtitle"),
                collapsible=True, collapsed=bool(args.get("collapsed")),
            )]
        if kind == "markdown":
            return [b.markdown_block(str(args.get("text") or ""))]
        if kind == "video":
            return [b.video_block(
                str(args.get("video_url") or ""), title=title or "Video",
                thumbnail_url=str(args.get("thumbnail_url") or ""),
                description=args.get("description"),
            )]
        raise ValueError("kind must be table, chart, cards, container, markdown or video")

    async def execute(self, call_id: str, arguments: dict[str, Any]) -> ToolResult:
        args = arguments or {}
        try:
            blocks = self.build_blocks(args)
        except ValueError as exc:
            return ToolResult(call_id=call_id, content=f"slack_widget: {exc}", is_error=True)
        except Exception as exc:  # noqa: BLE001
            logger.exception("slack_widget build failed")
            return ToolResult(call_id=call_id, content=f"slack_widget: could not build the widget: {exc}", is_error=True)

        kind = str(args.get("kind") or "")
        session = self._get_session()
        consumer = getattr(session, "_active_stream_consumer", None) if session is not None else None
        if consumer is not None and hasattr(consumer, "append_blocks"):
            try:
                if await consumer.append_blocks(blocks, label=kind):
                    return ToolResult(
                        call_id=call_id,
                        content=f"Rendered the {kind} widget in the reply. Continue the prose below it; do not repeat its contents as text.",
                    )
            except Exception:  # noqa: BLE001
                logger.exception("slack_widget append_blocks raised")

        # No live stream: post the widget as its own message in the thread.
        source = self._get_gateway_source()
        if source is None:
            return ToolResult(call_id=call_id, content="slack_widget: no active gateway source on this session.", is_error=True)
        from bridge.gateway.approval import get_approval_adapter

        platform_obj = getattr(source, "platform", None)
        platform_name = getattr(platform_obj, "value", None) or str(platform_obj or "")
        adapter = get_approval_adapter(platform_name)
        if adapter is None or not hasattr(adapter, "send_blocks"):
            return ToolResult(call_id=call_id, content=f"slack_widget: no live adapter for `{platform_name}`.", is_error=True)
        chat_id = str(getattr(source, "chat_id", "") or "")
        thread_id = getattr(source, "thread_id", None)
        if not thread_id and getattr(source, "chat_type", None) == "dm":
            thread_id = getattr(source, "message_id", None)
        if getattr(source, "chat_type", None) == "code":
            thread_id = None
        res = await adapter.send_blocks(chat_id, blocks, thread_id=thread_id, text=f"Freyja: {kind}")
        if not getattr(res, "ok", False):
            return ToolResult(call_id=call_id, content=f"slack_widget: Slack rejected the {kind}: {getattr(res, 'error', '?')}", is_error=True)
        return ToolResult(call_id=call_id, content=f"Posted the {kind} widget in the thread. Do not repeat its contents as text.")
