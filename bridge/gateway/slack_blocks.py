"""Block Kit builders for the Slack surface.

Pure functions, no network. Each builder validates against the limits
Slack documents for the block and raises ``ValueError`` with a message
the agent can act on, so a bad table or chart fails in the tool call
instead of as an opaque ``invalid_blocks`` from the API.

Blocks built here:

- ``feedback_block``      context_actions + feedback_buttons under a reply
- ``data_table_block``    sortable, paginated table (2 to 201 rows, 20 cols)
- ``chart_block``         data_visualization: pie, bar, area, line
- ``card_blocks``         one card, or a carousel of up to 10 cards
- ``container_block``     titled, collapsible group of child blocks
- ``markdown_block``      standard markdown (tables, task lists, fences)
- ``video_block``         embedded external video
- ``home_view``           the Home tab view

Sizes come from docs.slack.dev/reference/block-kit (October 2026).
"""

from __future__ import annotations

import json
from typing import Any, Iterable

FEEDBACK_ACTION_ID = "freyja_feedback"
FEEDBACK_NOTE_CALLBACK_ID = "freyja_feedback_note"
FEEDBACK_NOTE_BLOCK_ID = "freyja_feedback_note_block"
FEEDBACK_NOTE_ACTION_ID = "freyja_feedback_note_text"

# Slack limits
_DATA_TABLE_MAX_ROWS = 201          # header + 200
_DATA_TABLE_MAX_COLS = 20
_DATA_TABLE_MAX_CHARS = 20_000      # across all cells
_CHART_MAX_SERIES = 12
_CHART_MAX_POINTS = 20
_CHART_LABEL_MAX = 20
_CHART_TITLE_MAX = 50
_CARD_TITLE_MAX = 150
_CARD_BODY_MAX = 200
_CARD_ACTIONS_MAX = 3
_CAROUSEL_MAX = 10
_CONTAINER_CHILDREN_MAX = 10
_CONTAINER_TITLE_MAX = 150
_MARKDOWN_MAX = 12_000
_FEEDBACK_VALUE_MAX = 2000


# ── feedback ──────────────────────────────────────────────────────────


def feedback_value(session_key: str, turn_ts: str | None, verdict: str) -> str:
    """The ``value`` carried by one feedback button.

    Compact JSON so one click tells the handler which session and which
    reply it refers to without a lookup. ``verdict`` is ``+`` or ``-``.
    """
    payload = {"k": session_key, "t": turn_ts or "", "v": verdict}
    raw = json.dumps(payload, separators=(",", ":"))
    if len(raw) > _FEEDBACK_VALUE_MAX:
        payload["k"] = session_key[: _FEEDBACK_VALUE_MAX - 64]
        raw = json.dumps(payload, separators=(",", ":"))
    return raw


def parse_feedback_value(value: str) -> dict[str, str]:
    """Inverse of ``feedback_value``. Tolerates the probe-era ``x:+`` shape."""
    try:
        data = json.loads(value)
        if isinstance(data, dict):
            return {
                "session_key": str(data.get("k") or ""),
                "turn_ts": str(data.get("t") or ""),
                "verdict": "+" if str(data.get("v")) == "+" else "-",
            }
    except (TypeError, ValueError):
        pass
    text = str(value or "")
    return {
        "session_key": text.rsplit(":", 1)[0] if ":" in text else "",
        "turn_ts": "",
        "verdict": "+" if text.endswith("+") else "-",
    }


def feedback_block(session_key: str, turn_ts: str | None) -> dict[str, Any]:
    """Thumbs up / thumbs down under a reply."""
    return {
        "type": "context_actions",
        "elements": [
            {
                "type": "feedback_buttons",
                "action_id": FEEDBACK_ACTION_ID,
                "positive_button": {
                    "text": {"type": "plain_text", "text": "Good"},
                    "accessibility_label": "This reply was good",
                    "value": feedback_value(session_key, turn_ts, "+"),
                },
                "negative_button": {
                    "text": {"type": "plain_text", "text": "Bad"},
                    "accessibility_label": "This reply was bad",
                    "value": feedback_value(session_key, turn_ts, "-"),
                },
            }
        ],
    }


def feedback_note_view(private_metadata: str) -> dict[str, Any]:
    """The modal opened after a thumbs down, asking for one sentence."""
    return {
        "type": "modal",
        "callback_id": FEEDBACK_NOTE_CALLBACK_ID,
        "private_metadata": private_metadata[:3000],
        "title": {"type": "plain_text", "text": "What went wrong?"},
        "submit": {"type": "plain_text", "text": "Send"},
        "close": {"type": "plain_text", "text": "Skip"},
        "blocks": [
            {
                "type": "input",
                "block_id": FEEDBACK_NOTE_BLOCK_ID,
                "optional": True,
                "label": {"type": "plain_text", "text": "One sentence is enough"},
                "element": {
                    "type": "plain_text_input",
                    "action_id": FEEDBACK_NOTE_ACTION_ID,
                    "multiline": True,
                    "max_length": 1000,
                    "placeholder": {
                        "type": "plain_text",
                        "text": "Wrong file, too long, missed the question...",
                    },
                },
            }
        ],
    }


# ── data table ────────────────────────────────────────────────────────


def _cell(value: Any) -> dict[str, Any]:
    if isinstance(value, bool):
        return {"type": "raw_text", "text": "yes" if value else "no"}
    if isinstance(value, (int, float)):
        text = f"{value:g}" if isinstance(value, float) else str(value)
        return {"type": "raw_number", "value": value, "text": text}
    if isinstance(value, dict) and value.get("type") in {"raw_text", "raw_number", "rich_text"}:
        return value
    return {"type": "raw_text", "text": str(value if value is not None else "")}


def data_table_block(
    columns: list[str],
    rows: list[list[Any]],
    *,
    caption: str = "",
    page_size: int | None = None,
) -> dict[str, Any]:
    """A sortable, paginated table. Numbers stay numbers so sorting is numeric."""
    if not columns:
        raise ValueError("table needs at least one column")
    if len(columns) > _DATA_TABLE_MAX_COLS:
        raise ValueError(f"table has {len(columns)} columns; Slack allows {_DATA_TABLE_MAX_COLS}")
    if not rows:
        raise ValueError("table needs at least one data row")
    if len(rows) + 1 > _DATA_TABLE_MAX_ROWS:
        raise ValueError(f"table has {len(rows)} rows; Slack allows 200 data rows")
    width = len(columns)
    out_rows: list[list[dict[str, Any]]] = [[{"type": "raw_text", "text": str(c)} for c in columns]]
    chars = sum(len(str(c)) for c in columns)
    for i, row in enumerate(rows):
        if not isinstance(row, (list, tuple)):
            raise ValueError(f"row {i} is not a list")
        cells = list(row)[:width]
        while len(cells) < width:
            cells.append("")
        built = [_cell(v) for v in cells]
        chars += sum(len(str(c.get("text", ""))) for c in built)
        out_rows.append(built)
    if chars > _DATA_TABLE_MAX_CHARS:
        raise ValueError(f"table text is {chars} characters; Slack allows {_DATA_TABLE_MAX_CHARS}")
    block: dict[str, Any] = {"type": "data_table", "caption": caption or "Table", "rows": out_rows}
    if page_size:
        block["page_size"] = max(1, min(100, int(page_size)))
    return block


# ── chart ─────────────────────────────────────────────────────────────


def chart_block(
    kind: str,
    title: str,
    *,
    series: list[dict[str, Any]] | None = None,
    segments: list[dict[str, Any]] | None = None,
    x_label: str | None = None,
    y_label: str | None = None,
) -> dict[str, Any]:
    """A native chart. ``kind`` is pie, bar, area or line.

    Pie takes ``segments`` as ``[{"label": str, "value": number}]``.
    The others take ``series`` as ``[{"name": str, "data": [{"label", "value"}]}]``;
    categories are the labels of the first series, in order.
    """
    kind = (kind or "").strip().lower()
    if kind not in {"pie", "bar", "area", "line"}:
        raise ValueError("chart kind must be pie, bar, area or line")
    title = (title or "").strip()
    if not title:
        raise ValueError("chart needs a title")
    if len(title) > _CHART_TITLE_MAX:
        raise ValueError(f"chart title is over {_CHART_TITLE_MAX} characters")

    def _label(s: Any) -> str:
        text = str(s if s is not None else "").strip()
        if not text:
            raise ValueError("every label needs text")
        if len(text) > _CHART_LABEL_MAX:
            raise ValueError(f"label {text!r} is over {_CHART_LABEL_MAX} characters")
        return text

    if kind == "pie":
        segs = segments or []
        if not 1 <= len(segs) <= _CHART_MAX_SERIES:
            raise ValueError(f"pie needs 1 to {_CHART_MAX_SERIES} segments")
        out_segs = []
        for s in segs:
            value = float(s.get("value", 0))
            if value <= 0:
                raise ValueError("pie segment values must be greater than zero")
            out_segs.append({"label": _label(s.get("label")), "value": value})
        return {"type": "data_visualization", "title": title, "chart": {"type": "pie", "segments": out_segs}}

    ser = series or []
    if not 1 <= len(ser) <= _CHART_MAX_SERIES:
        raise ValueError(f"chart needs 1 to {_CHART_MAX_SERIES} series")
    names: set[str] = set()
    categories: list[str] = []
    out_series = []
    for s in ser:
        name = _label(s.get("name"))
        if name in names:
            raise ValueError(f"series name {name!r} repeats")
        names.add(name)
        points = s.get("data") or []
        if not 1 <= len(points) <= _CHART_MAX_POINTS:
            raise ValueError(f"series {name!r} needs 1 to {_CHART_MAX_POINTS} points")
        out_points = []
        for p in points:
            label = _label(p.get("label"))
            if label not in categories:
                categories.append(label)
            out_points.append({"label": label, "value": float(p.get("value", 0))})
        out_series.append({"name": name, "data": out_points})
    if len(categories) > _CHART_MAX_POINTS:
        raise ValueError(f"chart has {len(categories)} categories; Slack allows {_CHART_MAX_POINTS}")
    axis: dict[str, Any] = {"categories": categories}
    if x_label:
        axis["x_label"] = str(x_label)[:50]
    if y_label:
        axis["y_label"] = str(y_label)[:50]
    return {
        "type": "data_visualization",
        "title": title,
        "chart": {"type": kind, "series": out_series, "axis_config": axis},
    }


# ── cards ─────────────────────────────────────────────────────────────


def _text(text: str, *, limit: int, markdown: bool = True) -> dict[str, Any]:
    text = str(text or "")
    if len(text) > limit:
        text = text[: limit - 1].rstrip() + "…"
    return {"type": "mrkdwn" if markdown else "plain_text", "text": text}


def card_block(card: dict[str, Any]) -> dict[str, Any]:
    """One card: title, subtitle, body, subtext, image, up to 3 link buttons."""
    out: dict[str, Any] = {"type": "card"}
    title = card.get("title")
    body = card.get("body")
    if not title and not body and not card.get("image_url"):
        raise ValueError("a card needs a title, a body or an image")
    if title:
        out["title"] = _text(title, limit=_CARD_TITLE_MAX)
    if card.get("subtitle"):
        out["subtitle"] = _text(card["subtitle"], limit=_CARD_TITLE_MAX)
    if body:
        out["body"] = _text(body, limit=_CARD_BODY_MAX)
    if card.get("subtext"):
        out["subtext"] = _text(card["subtext"], limit=_CARD_BODY_MAX)
    if card.get("image_url"):
        out["hero_image"] = {
            "type": "image",
            "image_url": str(card["image_url"])[:3000],
            "alt_text": str(card.get("image_alt") or title or "image")[:2000],
        }
    if card.get("icon_url"):
        out["icon"] = {
            "type": "image",
            "image_url": str(card["icon_url"])[:3000],
            "alt_text": str(card.get("icon_alt") or "icon")[:2000],
        }
    links = card.get("links") or []
    if links:
        buttons = []
        for i, link in enumerate(links[:_CARD_ACTIONS_MAX]):
            if not isinstance(link, dict) or not link.get("url"):
                continue
            buttons.append({
                "type": "button",
                "action_id": f"freyja_card_link_{i}",
                "text": {"type": "plain_text", "text": str(link.get("text") or "Open")[:75]},
                "url": str(link["url"])[:3000],
            })
        if buttons:
            out["actions"] = buttons
    return out


def card_blocks(cards: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One card, or a carousel when there are several."""
    if not cards:
        raise ValueError("no cards given")
    if len(cards) > _CAROUSEL_MAX:
        raise ValueError(f"{len(cards)} cards; a carousel holds {_CAROUSEL_MAX}")
    built = [card_block(c) for c in cards]
    if len(built) == 1:
        return built
    return [{"type": "carousel", "cards": built}]


# ── container ─────────────────────────────────────────────────────────


def container_block(
    title: str,
    children: list[dict[str, Any]],
    *,
    subtitle: str | None = None,
    collapsible: bool = True,
    collapsed: bool = False,
) -> dict[str, Any]:
    """A titled group. Children are section/context/divider/table/image blocks."""
    if not title:
        raise ValueError("container needs a title")
    if not 1 <= len(children) <= _CONTAINER_CHILDREN_MAX:
        raise ValueError(f"container holds 1 to {_CONTAINER_CHILDREN_MAX} blocks")
    out: dict[str, Any] = {
        "type": "container",
        "title": {"type": "plain_text", "text": str(title)[:_CONTAINER_TITLE_MAX]},
        "child_blocks": list(children),
    }
    if subtitle:
        out["subtitle"] = {"type": "plain_text", "text": str(subtitle)[:_CONTAINER_TITLE_MAX]}
    if collapsible:
        out["is_collapsible"] = True
        if collapsed:
            out["default_collapsed"] = True
    return out


def section_block(markdown: str) -> dict[str, Any]:
    """A section with mrkdwn text (Slack's own syntax, 3000 chars)."""
    return {"type": "section", "text": _text(markdown, limit=3000)}


def markdown_block(text: str) -> dict[str, Any]:
    """Standard markdown: tables, task lists, fenced code, headers."""
    text = str(text or "")
    if not text.strip():
        raise ValueError("markdown block is empty")
    if len(text) > _MARKDOWN_MAX:
        raise ValueError(f"markdown is {len(text)} characters; Slack allows {_MARKDOWN_MAX} per message")
    return {"type": "markdown", "text": text}


def video_block(
    url: str,
    *,
    title: str,
    thumbnail_url: str,
    description: str | None = None,
    provider: str | None = None,
) -> dict[str, Any]:
    """An embedded video. Needs an https embed URL and a thumbnail."""
    if not str(url).startswith("https://"):
        raise ValueError("video_url must be https")
    if not str(thumbnail_url).startswith("https://"):
        raise ValueError("thumbnail_url must be https")
    out: dict[str, Any] = {
        "type": "video",
        "video_url": str(url)[:3000],
        "thumbnail_url": str(thumbnail_url)[:3000],
        "alt_text": str(title or "video")[:2000],
        "title": {"type": "plain_text", "text": str(title or "Video")[:200]},
    }
    if description:
        out["description"] = {"type": "plain_text", "text": str(description)[:200]}
    if provider:
        out["provider_name"] = str(provider)[:50]
    return out


def image_block(file_id: str, alt_text: str) -> dict[str, Any]:
    return {"type": "image", "slack_file": {"id": file_id}, "alt_text": (alt_text or "image")[:2000]}


# ── home tab ──────────────────────────────────────────────────────────


def home_view(
    *,
    model: str,
    mode: str,
    sessions: Iterable[dict[str, Any]],
    jobs: Iterable[dict[str, Any]],
    candidates: Iterable[dict[str, Any]],
    generated_at: str,
) -> dict[str, Any]:
    """The Home tab: defaults, live sessions, scheduled jobs, skills waiting.

    Each row dict carries ``title`` and an optional ``detail`` line. Lists
    are capped so the view stays under Slack's 100 block limit.
    """
    blocks: list[dict[str, Any]] = [
        {"type": "header", "text": {"type": "plain_text", "text": "Freyja"}},
        {
            "type": "context",
            "elements": [{"type": "mrkdwn", "text": f"Default model `{model}` · mode `{mode}` · updated {generated_at}"}],
        },
        {"type": "divider"},
    ]

    def _section(title: str, rows: list[dict[str, Any]], empty: str) -> None:
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": f"*{title}*"}})
        if not rows:
            blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": f"_{empty}_"}]})
            return
        lines = []
        for r in rows[:12]:
            line = f"• {r.get('title') or '(untitled)'}"
            if r.get("detail"):
                line += f"  —  _{r['detail']}_"
            lines.append(line)
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(lines)[:3000]}})

    _section("Sessions", list(sessions), "No Slack sessions loaded. Send a DM to start one.")
    _section("Scheduled jobs", list(jobs), "No scheduled jobs. Ask Freyja to schedule something.")
    _section("Skills waiting for review", list(candidates), "Nothing to review.")
    blocks.append({"type": "divider"})
    blocks.append({
        "type": "context",
        "elements": [{
            "type": "mrkdwn",
            "text": "`/freyja help` lists commands · `/model` and `/mode` change a session · the Stop button on a reply interrupts it",
        }],
    })
    return {"type": "home", "blocks": blocks[:100]}


def suggested_prompts(items: list[tuple[str, str]]) -> list[dict[str, str]]:
    """``[(title, message)]`` into the shape setSuggestedPrompts takes, max 4."""
    out: list[dict[str, str]] = []
    for title, message in items:
        title = str(title or "").strip()[:75]
        message = str(message or "").strip()[:2000]
        if title and message:
            out.append({"title": title, "message": message})
        if len(out) == 4:
            break
    return out
