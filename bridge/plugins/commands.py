"""plugin_command handling shared by the bridge stdin IPC and gateway /plugin.

Actions (design doc section 5):
  · list              — installed plugin records (name/version/counts)
  · install <source>  — local path or git URL, via bridge/plugins/loader;
                        returns a summary incl. skill/command/server
                        counts and unsupported manifest sections
  · remove <name>     — uninstall (directory + installed.json record +
                        plugin-owned MCP catalog entries)

After a successful install/remove the caller-provided SkillStore
instances are ``refresh()``ed so plugin skills and command-skill shims
appear/disappear immediately, and — when the change touched MCP catalog
entries — ``McpManager.reload()`` re-reads mcp.json so merged servers
show up in ``/mcp status`` (disabled until explicitly enabled).

Like bridge/mcp/commands.py, the handler is a plain async function so
both entry points (freyja_bridge._handle_command's ``plugin_command``
branch and the gateway's ``/plugin`` slash command) share one
implementation and unit tests can call it directly with fake
collaborators. loader.install/uninstall/list_installed are blocking
(git clone, copytree), so they run via asyncio.to_thread.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Iterable

from bridge.plugins.loader import (
    PluginError,
    install,
    list_installed,
    uninstall,
)

VALID_ACTIONS = ("list", "install", "remove")


def parse_plugin_args(text: str) -> tuple[str, str]:
    """Split ``/plugin [action] [arg]`` chat args. Empty -> list. The
    arg keeps internal whitespace so local paths with spaces work."""
    parts = (text or "").split(None, 1)
    action = parts[0].lower() if parts else "list"
    arg = parts[1].strip() if len(parts) > 1 else ""
    return action, arg


async def _refresh_surfaces(
    skill_stores: Iterable[Any],
    mcp_manager: Any,
    *,
    reload_mcp: bool,
) -> list[str]:
    """Post-change hooks: SkillStore.refresh() on every provided store,
    McpManager.reload() when the change touched catalog entries. Hook
    failures are reported as notes, never raised — the install/remove
    itself already succeeded."""
    notes: list[str] = []
    for store in skill_stores or ():
        try:
            store.refresh()
        except Exception as exc:  # noqa: BLE001
            notes.append(f"skill refresh failed: {exc}")
    if reload_mcp and mcp_manager is not None:
        try:
            await mcp_manager.reload()
        except Exception as exc:  # noqa: BLE001
            notes.append(f"mcp reload failed: {exc}")
    return notes


def _install_message(summary: dict[str, Any]) -> str:
    name = summary.get("name", "?")
    version = summary.get("version") or ""
    mcp = summary.get("mcp") or {}
    added = mcp.get("added") or []
    already = mcp.get("already_present") or []
    conflicts = mcp.get("conflicts") or []
    invalid = mcp.get("invalid") or []
    head = f"installed '{name}'" + (f" v{version}" if version else "")
    parts = [
        f"{len(summary.get('skill_names') or [])} skill(s)",
        f"{len(summary.get('command_names') or [])} command(s)",
        f"{len(added) + len(already)} MCP server(s)",
    ]
    message = f"{head} — {', '.join(parts)}"
    if added:
        message += (
            f"; MCP server(s) merged disabled: {', '.join(added)}"
            " (enable via mcp enable <server>)"
        )
    unsupported = summary.get("unsupported") or []
    if unsupported:
        message += f"; unsupported sections ignored: {', '.join(unsupported)}"
    if conflicts:
        names = ", ".join(c.get("name", "?") for c in conflicts)
        message += f"; MCP name conflict(s) skipped: {names}"
    if invalid:
        names = ", ".join(i.get("name", "?") for i in invalid)
        message += f"; invalid MCP entrie(s) skipped: {names}"
    return message


async def handle_plugin_command(
    cmd: dict[str, Any],
    *,
    plugins_root: Path | str | None = None,
    mcp_path: Path | str | None = None,
    skill_stores: Iterable[Any] = (),
    mcp_manager: Any = None,
) -> dict[str, Any]:
    """Dispatch one plugin_command. Always returns a dict with at least
    ``ok`` and ``action``; ``plugins`` carries the installed records for
    the list action."""
    action = str(cmd.get("action") or "list").strip().lower()
    if action == "uninstall":  # accepted alias
        action = "remove"
    arg = str(cmd.get("source") or cmd.get("name") or "").strip()
    if mcp_path is None and mcp_manager is not None:
        # Keep the loader's catalog writes on the same file the live
        # manager reloads from.
        mcp_path = getattr(mcp_manager, "_catalog_path", None)

    if action == "list":
        records = await asyncio.to_thread(
            list_installed, plugins_root=plugins_root
        )
        return {"ok": True, "action": "list", "plugins": records}

    if action == "install":
        if not arg:
            return {
                "ok": False,
                "action": "install",
                "message": "usage: install <path-or-git-url>",
            }
        try:
            summary = await asyncio.to_thread(
                lambda: install(
                    arg,
                    link=bool(cmd.get("link")),
                    plugins_root=plugins_root,
                    mcp_path=mcp_path,
                )
            )
        except PluginError as exc:
            return {"ok": False, "action": "install", "message": str(exc)}
        mcp_summary = summary.get("mcp") or {}
        notes = await _refresh_surfaces(
            skill_stores,
            mcp_manager,
            reload_mcp=bool(mcp_summary.get("added")),
        )
        message = _install_message(summary)
        if notes:
            message += f" [{'; '.join(notes)}]"
        return {
            "ok": True,
            "action": "install",
            "plugin": summary.get("name"),
            "message": message,
            "summary": summary,
        }

    if action == "remove":
        if not arg:
            return {
                "ok": False,
                "action": "remove",
                "message": "usage: remove <name>",
            }
        try:
            removal = await asyncio.to_thread(
                lambda: uninstall(arg, plugins_root=plugins_root, mcp_path=mcp_path)
            )
        except PluginError as exc:
            return {"ok": False, "action": "remove", "message": str(exc)}
        removed_servers = removal.get("removed_mcp_servers") or []
        notes = await _refresh_surfaces(
            skill_stores, mcp_manager, reload_mcp=bool(removed_servers)
        )
        message = f"removed plugin '{arg}'"
        if removed_servers:
            message += f"; MCP server(s) removed: {', '.join(removed_servers)}"
        if notes:
            message += f" [{'; '.join(notes)}]"
        return {
            "ok": True,
            "action": "remove",
            "plugin": arg,
            "message": message,
            "removed": removal,
        }

    return {
        "ok": False,
        "action": action,
        "message": (
            f"unknown plugin_command action '{action}' "
            f"(expected one of {', '.join(VALID_ACTIONS)})"
        ),
    }


def format_plugin_table(plugins: list[dict[str, Any]]) -> str:
    """Compact fixed-width table: name, version, skills, commands,
    servers, source (the /plugin chat surface). Mirrors
    bridge/mcp/commands.format_mcp_table."""
    if not plugins:
        return "no plugins installed"
    headers = ("name", "version", "skills", "commands", "servers", "source")
    rows: list[tuple[str, ...]] = []
    for rec in plugins:
        source = str(rec.get("source") or "-")
        if len(source) > 40:
            source = source[:39] + "…"
        rows.append((
            str(rec.get("name", "?")),
            str(rec.get("version") or "-"),
            str(rec.get("skills", 0)),
            str(rec.get("commands", 0)),
            str(rec.get("servers", 0)),
            source,
        ))
    widths = [
        max(len(headers[i]), *(len(row[i]) for row in rows))
        for i in range(len(headers))
    ]
    def _fmt(row: tuple[str, ...]) -> str:
        return "  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)).rstrip()
    lines = [_fmt(headers), _fmt(tuple("-" * w for w in widths))]
    lines.extend(_fmt(row) for row in rows)
    return "\n".join(lines)
