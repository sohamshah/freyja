"""``code_channel``: a Slack Code channel for a multi-step job.

Slack Code gives an agent a channel per task with a view bar (diff,
HTML report, Block Kit dashboard, canvas, pull request), a context bar
(repo, branch, PR, spend), slash commands that work during the job, a
native status, and an archive step with a summary. Freyja already
produces the artifacts those views show, so the tool is a thin layer
over ``agents.conversations.*`` through the Slack adapter.

Lifecycle, as the agent uses it:

  open     → a channel named for the task, linked to the origin thread;
             messages there reach Freyja as the session
             ``freyja:slack:<T>:code:<C>`` with no mention needed
  context  → up to five context bar items (repo, branch, PR, model)
  commands → the slash commands offered in the channel (stop, status,
             model, reset by default)
  view     → a diff (from a repo and two refs, or raw), an HTML file,
             Block Kit blocks, a canvas, or a pull request tab
  status   → processing / suspended / active / closed, with a title
  archive  → archive with a summary message, and post the summary back
             into the origin thread
  list     → the code channels this install opened

The workspace must have Slack Code turned on. Until then ``open``
answers with ``feature_disabled`` and the tool explains what to enable.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from pathlib import Path
from typing import Any, Callable

from bridge.tools.base import ToolDefinition, ToolResult

logger = logging.getLogger(__name__)

_VIEW_CONTENT_MAX = 1_000_000
_DEFAULT_COMMANDS = [
    {"name": "stop", "description": "Interrupt the current step"},
    {"name": "status", "description": "What Freyja is doing right now"},
    {"name": "model", "description": "Switch the model for this job", "argument_hint": "<model-id>"},
    {"name": "reset", "description": "Start the job's conversation fresh"},
]
_ICONS = {"folder", "branch", "hierarchy", "terminal", "link", "pull_request", "globe", "info"}


def _build_definition() -> ToolDefinition:
    description = (
        "Run a multi-step job in its own Slack Code channel. `open` creates "
        "the channel (name it for the task); the operator then talks to you "
        "there without mentions. `view` adds or replaces a tab: `diff` (give "
        "`repo` plus `base` and `head` refs, or raw `content`), `html` (a "
        "local `path` or `content`), `block_kit` (`blocks`), `canvas` "
        "(`canvas_id`) or `pull_request` (`pr_url`). `context` sets up to 5 "
        "context bar items (repo, branch, PR, model, spend). `commands` "
        "registers the channel's slash commands. `status` sets "
        "processing/suspended/active/closed and an optional title. `archive` "
        "closes the channel with a summary and posts the summary back to the "
        "thread the job started from. Use it for work that takes many steps "
        "or produces a diff or a report; a quick answer stays in the thread."
    )
    return ToolDefinition(
        name="code_channel",
        description=description,
        summary="Open, update, and archive a Slack Code channel for a multi-step job.",
        parameters={
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["open", "view", "context", "commands", "status", "archive", "list"],
                },
                "name": {"type": "string", "description": "open: channel name, lowercase words and dashes, under 80 chars. view: the tab label."},
                "channel_id": {"type": "string", "description": "The code channel id (from open). Defaults to the channel this session runs in."},
                "private": {"type": "boolean", "description": "open: make the channel private."},
                "view_type": {"type": "string", "enum": ["diff", "html", "block_kit", "canvas", "pull_request"]},
                "view_key": {"type": "string", "description": "view: stable key for html/block_kit/canvas tabs; reuse to replace."},
                "repo": {"type": "string", "description": "view diff: repository path."},
                "base": {"type": "string", "description": "view diff: base ref (default origin/main)."},
                "head": {"type": "string", "description": "view diff: head ref (default HEAD, or working tree when `working_tree` is true)."},
                "working_tree": {"type": "boolean", "description": "view diff: diff the working tree against base."},
                "content": {"type": "string", "description": "view diff/html: raw content."},
                "path": {"type": "string", "description": "view html: local HTML file."},
                "blocks": {"type": "array", "items": {"type": "object"}, "description": "view block_kit: Block Kit blocks."},
                "canvas_id": {"type": "string"},
                "access_level": {"type": "string", "enum": ["read", "write", "comment"]},
                "pr_url": {"type": "string"},
                "items": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "key": {"type": "string"},
                            "label": {"type": "string"},
                            "icon": {"type": "string", "description": "folder, branch, hierarchy, terminal, link, pull_request, globe, info"},
                            "url": {"type": "string"},
                        },
                        "required": ["key", "label"],
                    },
                    "description": "context: up to 5 items; the list replaces the whole bar.",
                },
                "commands": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string"},
                            "description": {"type": "string"},
                            "argument_hint": {"type": "string"},
                        },
                        "required": ["name", "description"],
                    },
                    "description": "commands: up to 10; omit for the default stop/status/model/reset.",
                },
                "status": {"type": "string", "enum": ["processing", "suspended", "active", "closed"]},
                "title": {"type": "string", "description": "status: session title shown in the sidebar."},
                "summary": {"type": "string", "description": "archive: the recap, posted in the channel and back in the origin thread."},
            },
            "required": ["action"],
        },
    )


class CodeChannelTool:
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

    # ── helpers ──

    def _adapter(self) -> tuple[Any | None, Any | None, str]:
        source = self._get_gateway_source()
        if source is None:
            return None, None, "no active gateway source on this session"
        from bridge.gateway.approval import get_approval_adapter

        platform_obj = getattr(source, "platform", None)
        platform_name = getattr(platform_obj, "value", None) or str(platform_obj or "")
        adapter = get_approval_adapter(platform_name)
        if adapter is None or not hasattr(adapter, "code_channel_create"):
            return None, source, f"no live Slack adapter for `{platform_name}`"
        return adapter, source, ""

    @staticmethod
    def _channel_name(name: str) -> str:
        slug = re.sub(r"[^a-z0-9-]+", "-", (name or "").strip().lower()).strip("-")
        slug = re.sub(r"-{2,}", "-", slug)
        return (slug or f"freyja-job-{int(time.time())}")[:80]

    def _target_channel(self, args: dict[str, Any], source: Any) -> str:
        channel_id = str(args.get("channel_id") or "").strip()
        if channel_id:
            return channel_id
        if getattr(source, "chat_type", None) == "code":
            return str(getattr(source, "chat_id", "") or "")
        return ""

    @staticmethod
    async def _git_diff(repo: str, base: str, head: str, *, working_tree: bool) -> tuple[str, str]:
        path = Path(repo).expanduser()
        if not path.is_dir():
            return "", f"repo path is not a directory: {path}"
        argv = ["git", "-C", str(path), "diff", "--no-color"]
        if working_tree:
            argv.append(base)
        else:
            argv.append(f"{base}...{head}")
        proc = await asyncio.create_subprocess_exec(
            *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        out, err = await proc.communicate()
        if proc.returncode != 0:
            return "", err.decode("utf-8", "replace").strip()[:500] or f"git diff exited {proc.returncode}"
        return out.decode("utf-8", "replace"), ""

    # ── actions ──

    async def execute(self, call_id: str, arguments: dict[str, Any]) -> ToolResult:
        args = arguments or {}
        action = str(args.get("action") or "").strip().lower()
        adapter, source, why = self._adapter()
        if adapter is None:
            return ToolResult(call_id=call_id, content=f"code_channel: {why}", is_error=True)
        try:
            if action == "open":
                return await self._open(call_id, args, adapter, source)
            if action == "list":
                return self._list(call_id, adapter)
            channel_id = self._target_channel(args, source)
            if not channel_id:
                return ToolResult(call_id=call_id, content="code_channel: pass `channel_id` (from `open`), or call from inside the code channel.", is_error=True)
            if action == "view":
                return await self._view(call_id, args, adapter, channel_id)
            if action == "context":
                return await self._context(call_id, args, adapter, channel_id)
            if action == "commands":
                return await self._commands(call_id, args, adapter, channel_id)
            if action == "status":
                return await self._status(call_id, args, adapter, channel_id)
            if action == "archive":
                return await self._archive(call_id, args, adapter, source, channel_id)
        except Exception as exc:  # noqa: BLE001
            logger.exception("code_channel %s raised", action)
            return ToolResult(call_id=call_id, content=f"code_channel {action}: {exc}", is_error=True)
        return ToolResult(call_id=call_id, content="code_channel: action must be open, view, context, commands, status, archive or list.", is_error=True)

    async def _open(self, call_id: str, args: dict[str, Any], adapter: Any, source: Any) -> ToolResult:
        name = self._channel_name(str(args.get("name") or ""))
        session = self._get_session()
        session_id = str(getattr(session, "id", "") or "") or f"freyja-{int(time.time())}"
        origin_ts = getattr(source, "thread_id", None) or getattr(source, "message_id", None)
        res = await adapter.code_channel_create(
            name,
            team_id=getattr(source, "workspace_id", None),
            origin_chat_id=getattr(source, "chat_id", None),
            origin_ts=origin_ts,
            session_id=f"{session_id}:{name}"[:120],
            is_private=bool(args["private"]) if "private" in args else None,
        )
        if not res.ok:
            return ToolResult(call_id=call_id, content=f"code_channel open failed: {res.error}", is_error=True)
        channel_id = str(res.message_id)
        # Working from the start, with the default commands offered.
        await adapter.set_session_status(channel_id, status="processing", title=str(args.get("name") or name)[:60])
        cmds = await adapter.code_channel_call("setCommands", channel_id=channel_id, commands=_DEFAULT_COMMANDS)
        note = "" if cmds.ok else f" (default slash commands not registered: {cmds.error})"
        workspace = getattr(source, "workspace_id", "") or ""
        return ToolResult(
            call_id=call_id,
            content=(
                f"Opened code channel #{name} ({channel_id}). Messages there reach you as session "
                f"`freyja:slack:{workspace}:code:{channel_id}`, no mention needed; reply at the top level. "
                f"Next: `context` for repo/branch/PR, `view` for a diff or report, `archive` with a summary when done.{note}"
            ),
        )

    def _list(self, call_id: str, adapter: Any) -> ToolResult:
        bindings = getattr(adapter, "bindings", None)
        channels = dict(getattr(bindings, "code_channels", {}) or {})
        if not channels:
            return ToolResult(call_id=call_id, content="No code channels open.")
        lines = [f"- {cid}: #{info.get('name', '?')} (origin {info.get('origin_chat_id', '?')})" for cid, info in channels.items()]
        return ToolResult(call_id=call_id, content="Open code channels:\n" + "\n".join(lines))

    async def _view(self, call_id: str, args: dict[str, Any], adapter: Any, channel_id: str) -> ToolResult:
        kind = str(args.get("view_type") or "").strip().lower()
        name = str(args.get("name") or "").strip() or None
        kwargs: dict[str, Any] = {"channel_id": channel_id, "type": kind}
        if name:
            kwargs["name"] = name[:40]
        if kind == "diff":
            content = str(args.get("content") or "")
            if not content:
                repo = str(args.get("repo") or "").strip()
                if not repo:
                    return ToolResult(call_id=call_id, content="code_channel view diff: pass `repo` (plus `base`/`head`) or `content`.", is_error=True)
                base = str(args.get("base") or "origin/main")
                head = str(args.get("head") or "HEAD")
                content, err = await self._git_diff(repo, base, head, working_tree=bool(args.get("working_tree")))
                if err:
                    return ToolResult(call_id=call_id, content=f"code_channel view diff: {err}", is_error=True)
                kwargs["base_branch"] = base[:80]
                kwargs["head_branch"] = ("working tree" if args.get("working_tree") else head)[:80]
            if not content.strip():
                return ToolResult(call_id=call_id, content="code_channel view diff: the diff is empty.", is_error=True)
            kwargs["content"] = content[:_VIEW_CONTENT_MAX]
        elif kind == "html":
            content = str(args.get("content") or "")
            path = str(args.get("path") or "").strip()
            if path and not content:
                p = Path(path).expanduser()
                if not p.is_file():
                    return ToolResult(call_id=call_id, content=f"code_channel view html: file not found: {p}", is_error=True)
                content = p.read_text(encoding="utf-8", errors="replace")
                kwargs.setdefault("name", p.stem[:40])
            if not content.strip():
                return ToolResult(call_id=call_id, content="code_channel view html: pass `path` or `content`.", is_error=True)
            kwargs["view_key"] = str(args.get("view_key") or (Path(path).name if path else "report.html"))[:120]
            kwargs["content"] = content[:_VIEW_CONTENT_MAX]
        elif kind == "block_kit":
            blocks = args.get("blocks")
            if isinstance(blocks, str):
                try:
                    blocks = json.loads(blocks)
                except ValueError:
                    blocks = None
            if not isinstance(blocks, list) or not blocks:
                return ToolResult(call_id=call_id, content="code_channel view block_kit: pass `blocks`.", is_error=True)
            kwargs["view_key"] = str(args.get("view_key") or "dashboard")[:120]
            kwargs["blocks"] = blocks[:50]
        elif kind == "canvas":
            canvas_id = str(args.get("canvas_id") or "").strip()
            if not canvas_id:
                return ToolResult(call_id=call_id, content="code_channel view canvas: pass `canvas_id`.", is_error=True)
            kwargs["view_key"] = str(args.get("view_key") or "plan")[:120]
            kwargs["canvas_id"] = canvas_id
            if args.get("access_level"):
                kwargs["access_level"] = str(args["access_level"])
        elif kind == "pull_request":
            pr_url = str(args.get("pr_url") or "").strip()
            if not pr_url.startswith("https://"):
                return ToolResult(call_id=call_id, content="code_channel view pull_request: pass an https `pr_url`.", is_error=True)
            kwargs["pr_url"] = pr_url
        else:
            return ToolResult(call_id=call_id, content="code_channel view: `view_type` must be diff, html, block_kit, canvas or pull_request.", is_error=True)
        res = await adapter.code_channel_call("setView", **kwargs)
        if not res.ok:
            return ToolResult(call_id=call_id, content=f"code_channel view {kind} failed: {res.error}", is_error=True)
        view_id = (res.raw or {}).get("view_id") if isinstance(res.raw, dict) else None
        return ToolResult(call_id=call_id, content=f"Set the {kind} view on {channel_id}" + (f" (view {view_id})." if view_id else "."))

    async def _context(self, call_id: str, args: dict[str, Any], adapter: Any, channel_id: str) -> ToolResult:
        items_in = args.get("items")
        if isinstance(items_in, str):
            try:
                items_in = json.loads(items_in)
            except ValueError:
                items_in = None
        if not isinstance(items_in, list) or not items_in:
            return ToolResult(call_id=call_id, content="code_channel context: pass `items` (1 to 5).", is_error=True)
        items: list[dict[str, Any]] = []
        for raw in items_in[:5]:
            if not isinstance(raw, dict) or not raw.get("key") or not raw.get("label"):
                continue
            item: dict[str, Any] = {"key": str(raw["key"])[:40], "label": str(raw["label"])[:80]}
            icon = str(raw.get("icon") or "").strip()
            if icon in _ICONS:
                item["icon"] = icon
            if raw.get("url"):
                item["url"] = str(raw["url"])[:3000]
            items.append(item)
        if not items:
            return ToolResult(call_id=call_id, content="code_channel context: every item needs `key` and `label`.", is_error=True)
        res = await adapter.code_channel_call("setProperties", channel_id=channel_id, code_channel={"context_bar_items": items})
        if not res.ok:
            return ToolResult(call_id=call_id, content=f"code_channel context failed: {res.error}", is_error=True)
        return ToolResult(call_id=call_id, content=f"Context bar on {channel_id}: " + ", ".join(i["label"] for i in items))

    async def _commands(self, call_id: str, args: dict[str, Any], adapter: Any, channel_id: str) -> ToolResult:
        cmds_in = args.get("commands")
        if isinstance(cmds_in, str):
            try:
                cmds_in = json.loads(cmds_in)
            except ValueError:
                cmds_in = None
        commands: list[dict[str, Any]] = []
        for raw in (cmds_in or _DEFAULT_COMMANDS)[:10]:
            if not isinstance(raw, dict) or not raw.get("name") or not raw.get("description"):
                continue
            cmd = {"name": re.sub(r"[^a-z0-9_-]", "", str(raw["name"]).lower().lstrip("/"))[:31], "description": str(raw["description"])[:100]}
            if raw.get("argument_hint"):
                cmd["argument_hint"] = str(raw["argument_hint"])[:60]
            if cmd["name"]:
                commands.append(cmd)
        if not commands:
            return ToolResult(call_id=call_id, content="code_channel commands: every command needs `name` and `description`.", is_error=True)
        res = await adapter.code_channel_call("setCommands", channel_id=channel_id, commands=commands)
        if not res.ok:
            return ToolResult(call_id=call_id, content=f"code_channel commands failed: {res.error}", is_error=True)
        return ToolResult(call_id=call_id, content="Slash commands on the channel: " + ", ".join("/" + c["name"] for c in commands))

    async def _status(self, call_id: str, args: dict[str, Any], adapter: Any, channel_id: str) -> ToolResult:
        status = str(args.get("status") or "").strip().lower()
        if status not in {"processing", "suspended", "active", "closed"}:
            return ToolResult(call_id=call_id, content="code_channel status: `status` must be processing, suspended, active or closed.", is_error=True)
        res = await adapter.set_session_status(channel_id, status=status, title=args.get("title") or None)
        if not res.ok:
            return ToolResult(call_id=call_id, content=f"code_channel status failed: {res.error}", is_error=True)
        return ToolResult(call_id=call_id, content=f"Session on {channel_id} is {status}.")

    async def _archive(self, call_id: str, args: dict[str, Any], adapter: Any, source: Any, channel_id: str) -> ToolResult:
        summary = str(args.get("summary") or "").strip()
        summary_ts: str | None = None
        bindings = getattr(adapter, "bindings", None)
        info = dict((getattr(bindings, "code_channels", {}) or {}).get(channel_id) or {})
        if summary:
            posted = await adapter.send(channel_id, summary)
            summary_ts = getattr(posted, "message_id", None)
        res = await adapter.code_channel_call("archive", channel_id=channel_id, summary_message_ts=summary_ts)
        if not res.ok:
            return ToolResult(call_id=call_id, content=f"code_channel archive failed: {res.error}", is_error=True)
        origin_chat = info.get("origin_chat_id") or ""
        origin_ts = info.get("origin_ts") or None
        echoed = ""
        if summary and origin_chat:
            try:
                await adapter.send(
                    origin_chat,
                    f"Finished in #{info.get('name', channel_id)} (archived).\n\n{summary}",
                    thread_id=origin_ts,
                )
                echoed = " Summary posted back in the origin thread."
            except Exception:  # noqa: BLE001
                logger.debug("archive summary echo failed", exc_info=True)
        return ToolResult(call_id=call_id, content=f"Archived {channel_id}.{echoed}")
