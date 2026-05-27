"""Gateway daemon entry point.

Runs forever as a launchd-managed service. Hosts a single
``_BridgeState`` and one or more platform adapters (Slack today;
Telegram/Discord later). Routes inbound messages from any adapter to
the right Freyja session and streams responses back through the
originating adapter.

Lifecycle:
  · PID lock acquisition (one daemon per ``FREYJA_HOME``)
  · Signal handlers: SIGTERM → graceful drain; SIGINT → same
  · Load ``~/.freyja/.env`` so platform tokens + provider keys are
    available
  · Construct ``_BridgeState`` (existing Freyja machinery)
  · Instantiate + connect configured platform adapters
  · For each inbound message: route to a session, register a
    per-turn ``SlackStreamConsumer``, enqueue via
    ``_schedule_or_queue_turn``
  · Block on shutdown event; on shutdown, disconnect adapters,
    release PID lock, exit 0 (planned) or 1 (crash)
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
import sys
import traceback
from pathlib import Path

from bridge.gateway.pid import (
    acquire_lock,
    consume_takeover_marker,
    freyja_home,
    gateway_log_path,
    release_lock,
)
from bridge.gateway.platforms.base import IncomingMessage, Platform
from bridge.gateway.platforms.slack import SlackAdapter
from bridge.gateway.session_router import (
    gateway_source_block,
    route as route_message,
)
from bridge.gateway.setup.env_writer import read_env
from bridge.gateway.stream_consumer import SlackStreamConsumer

logger = logging.getLogger("freyja.gateway")


def _help_card_text() -> str:
    """The text returned for `/freyja` and `/freyja help`."""
    return (
        "*Freyja on Slack*\n"
        "I'm a multi-agent assistant. I can help with coding, research, "
        "writing, and analysis. A few things that make me different:\n"
        "\n"
        "• I spawn *specialized sub-agents* (explore, code, verify, judge) "
        "to work on parts of your task in parallel. They publish findings "
        "to a shared bus and I synthesize.\n"
        "• I have a *goal mode* with an autonomous judge that reviews my "
        "work each turn and decides whether to keep iterating.\n"
        "• I have persistent *memory + skills* that compound across sessions.\n"
        "\n"
        "*Slash commands*\n"
        "• `/freyja help`  — this card\n"
        "• `/status`       — show session info (model, mode, in-flight)\n"
        "• `/goal <obj>`   — arm a goal loop\n"
        "• `/mode <s>`     — switch coordination (bus / goal / kanban / isolated)\n"
        "• `/model <id>`   — switch the agent model\n"
        "• `/stop`         — interrupt the current turn\n"
        "• `/reset`        — start a fresh conversation\n"
        "\n"
        "*Channels*: @mention me to start a thread, then keep replying in "
        "the thread without re-mentioning.\n"
        "*DMs*: just talk.\n"
        "*Files*: drop in any image, code, or document — I'll use it."
    )


def _setup_logging() -> None:
    """Configure root logger to write to ~/.freyja/logs/gateway.log and
    stdout. launchd captures stdout to the same file we configure in
    the plist, but in foreground mode (`freyja gateway run`) we still
    want the operator to see live output."""
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    try:
        file_handler = logging.FileHandler(gateway_log_path(), encoding="utf-8")
        handlers.append(file_handler)
    except OSError:
        pass
    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    for h in handlers:
        h.setFormatter(fmt)
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    # Replace any existing handlers so re-running doesn't double-log.
    for h in list(root.handlers):
        root.removeHandler(h)
    for h in handlers:
        root.addHandler(h)


def _load_env_into_os_environ() -> None:
    """Merge ``~/.freyja/.env`` into ``os.environ`` so provider SDKs +
    platform adapters see their tokens. Existing env vars take
    precedence (so the operator can override per-launch)."""
    env = read_env()
    for k, v in env.items():
        if k not in os.environ:
            os.environ[k] = v


class GatewayDaemon:
    """Top-level daemon coordinator. One instance per process."""

    def __init__(self) -> None:
        self.state: object | None = None
        self.adapters: list[object] = []
        self.shutdown_event: asyncio.Event = asyncio.Event()
        self._planned_exit = False  # True on graceful SIGTERM or takeover

    async def _on_inbound(self, message: IncomingMessage) -> None:
        """Single callback every adapter feeds inbound messages into."""
        # Lazy import — _BridgeState lives in freyja_bridge which is a
        # heavy module. Importing it here means the daemon doesn't pay
        # the import cost until the first message arrives, and gateway
        # boot stays fast.
        from bridge.freyja_bridge import (
            _schedule_or_queue_turn,
            register_session_listener,
            unregister_session_listener,
        )

        if self.state is None:
            logger.warning("inbound message dropped — state not initialized")
            return

        # Find the originating adapter (so the stream consumer sends
        # back through the right surface).
        adapter = self._adapter_for_platform(message.source.platform)
        if adapter is None:
            logger.warning(
                "no adapter for platform %s", message.source.platform.value
            )
            return

        # In-gateway slash command handlers — these short-circuit the
        # agent path and reply directly. Anything not handled here
        # falls through to the agent as a regular text turn (the
        # framed text below carries the literal slash command).
        if message.is_slash_command:
            handled = await self._handle_slash_in_gateway(message, adapter)
            if handled:
                return

        try:
            key, session = await route_message(
                message,
                self.state,
                default_strategy="bus",
            )
        except Exception:
            logger.exception("failed to route inbound message")
            return

        # Slash commands without text body (just /status, /freyja help)
        # still get routed as agent turns — the slash text becomes the
        # user message. Future: shortcut some slashes to in-gateway
        # handlers without involving the agent.

        # Build a per-turn stream consumer and wire it as a listener.
        # We unregister inside on_complete so each turn gets a fresh
        # consumer + fresh Slack message anchor.
        consumer_holder: dict[str, object] = {}

        def _unregister() -> None:
            cb = consumer_holder.get("on_event")
            if cb is not None:
                unregister_session_listener(key, cb)

        consumer = SlackStreamConsumer(
            adapter,  # type: ignore[arg-type]
            message.source,
            session_key=key,
            raw_hint=message.raw,
            on_complete=_unregister,
        )
        consumer_holder["on_event"] = consumer.on_event
        register_session_listener(key, consumer.on_event)

        # Build the framed user text: a small context preamble (one
        # paragraph naming the platform + chat + sender) followed by a
        # divider + the operator's actual message. Lets the agent know
        # it's on Slack without needing system-prompt surgery.
        framed_text = (
            "[gateway context]\n"
            + gateway_source_block(message.source)
            + "\n\n[message]\n"
            + message.text
        )

        # Enqueue the user turn via the existing machinery (handles
        # the busy/queue case transparently).
        try:
            _schedule_or_queue_turn(
                session, framed_text, message.attachments or None
            )
        except Exception:
            logger.exception("schedule_or_queue_turn failed")
            unregister_session_listener(key, consumer.on_event)

    def _adapter_for_platform(self, platform: Platform) -> object | None:
        for a in self.adapters:
            if getattr(a, "name", None) == platform.value:
                return a
        return None

    async def _handle_slash_in_gateway(
        self,
        message: IncomingMessage,
        adapter: object,
    ) -> bool:
        """Reply to certain slashes directly without involving the
        agent. Returns True if handled, False to fall through."""
        cmd = (message.slash_command_name or "").lower()

        if cmd == "freyja":
            sub = (message.slash_command_args or "").strip().lower()
            if sub in {"", "help"}:
                text = _help_card_text()
            elif sub == "status":
                text = await self._render_status(message)
            else:
                text = (
                    "Unknown subcommand. Try `/freyja help` or `/freyja status`."
                )
            await adapter.send(  # type: ignore[attr-defined]
                message.source.chat_id,
                text,
                thread_id=message.source.thread_id,
                ephemeral_user_id=message.source.user_id,
                raw_hint=message.raw,
            )
            return True

        if cmd == "status":
            text = await self._render_status(message)
            await adapter.send(  # type: ignore[attr-defined]
                message.source.chat_id,
                text,
                thread_id=message.source.thread_id,
                ephemeral_user_id=message.source.user_id,
                raw_hint=message.raw,
            )
            return True

        if cmd == "stop":
            # Try to cancel the session's pending_task if any.
            from bridge.gateway.session_router import session_key_for
            key = session_key_for(message.source)
            cancelled = self._cancel_session(key)
            text = (
                "Interrupting current turn." if cancelled
                else "Nothing in flight on this session."
            )
            await adapter.send(  # type: ignore[attr-defined]
                message.source.chat_id,
                text,
                thread_id=message.source.thread_id,
                ephemeral_user_id=message.source.user_id,
                raw_hint=message.raw,
            )
            return True

        # /reset → clear the session's transcript so the next message
        # starts fresh. v1: just drop the session from the dict so the
        # next message creates a new one (transcript persists on disk
        # but the in-memory pending state resets).
        if cmd == "reset":
            from bridge.gateway.session_router import session_key_for
            key = session_key_for(message.source)
            self._reset_session(key)
            await adapter.send(  # type: ignore[attr-defined]
                message.source.chat_id,
                "Session reset. Next message starts fresh.",
                thread_id=message.source.thread_id,
                ephemeral_user_id=message.source.user_id,
                raw_hint=message.raw,
            )
            return True

        if cmd == "mode":
            return await self._handle_mode_command(message, adapter)

        if cmd == "model":
            return await self._handle_model_command(message, adapter)

        if cmd == "goal":
            return await self._handle_goal_command(message, adapter)

        if cmd == "perms":
            return await self._handle_perms_command(message, adapter)

        return False

    async def _handle_mode_command(
        self,
        message: IncomingMessage,
        adapter: object,
    ) -> bool:
        """`/mode <strategy>` — change coordination strategy on the
        active session. Creates the session if it doesn't yet exist."""
        from bridge.gateway.session_router import session_key_for
        from bridge.tools.coordination import normalize_coordination_strategy  # noqa: F401

        target = (message.slash_command_args or "").strip().lower()
        if target not in {"bus", "goal", "kanban", "isolated"}:
            await adapter.send(  # type: ignore[attr-defined]
                message.source.chat_id,
                "Usage: `/mode <bus|goal|kanban|isolated>`",
                thread_id=message.source.thread_id,
                ephemeral_user_id=message.source.user_id,
                raw_hint=message.raw,
            )
            return True

        key = session_key_for(message.source)
        if self.state is None:
            return True
        session = await self.state.ensure_session(
            session_id=key,
            coordination_strategy=target,
        )
        # If the session existed with a different strategy, ensure_session
        # already swapped it. Re-emit confirmation for the operator.
        prev = getattr(session, "coordination_strategy", "?")
        await adapter.send(  # type: ignore[attr-defined]
            message.source.chat_id,
            f"Coordination strategy set to `{prev}`.",
            thread_id=message.source.thread_id,
            ephemeral_user_id=message.source.user_id,
            raw_hint=message.raw,
        )
        return True

    async def _handle_model_command(
        self,
        message: IncomingMessage,
        adapter: object,
    ) -> bool:
        """`/model <id>` — change the model the session uses for the
        next turn."""
        from bridge.gateway.session_router import session_key_for

        target = (message.slash_command_args or "").strip()
        if not target:
            # Show current model for this session.
            key = session_key_for(message.source)
            sessions = getattr(self.state, "sessions", {}) if self.state else {}
            session = sessions.get(key)
            current = getattr(session, "model_id", None) if session else None
            await adapter.send(  # type: ignore[attr-defined]
                message.source.chat_id,
                (
                    f"Current model: `{current}`\nUsage: `/model <id>` "
                    "(e.g. `claude-opus-4-7`, `claude-sonnet-4-6`, `gpt-5.5`)"
                    if current else
                    "No active session. Usage: `/model <id>`"
                ),
                thread_id=message.source.thread_id,
                ephemeral_user_id=message.source.user_id,
                raw_hint=message.raw,
            )
            return True

        key = session_key_for(message.source)
        if self.state is None:
            return True
        session = await self.state.ensure_session(
            session_id=key, model_id=target,
        )
        applied = getattr(session, "model_id", target)
        await adapter.send(  # type: ignore[attr-defined]
            message.source.chat_id,
            f"Model set to `{applied}`.",
            thread_id=message.source.thread_id,
            ephemeral_user_id=message.source.user_id,
            raw_hint=message.raw,
        )
        return True

    async def _handle_goal_command(
        self,
        message: IncomingMessage,
        adapter: object,
    ) -> bool:
        """`/goal <objective>` — flip the session into goal mode and
        arm the goal loop with the given objective. The next turn (the
        operator's follow-up, or this one's body if non-empty) will be
        evaluated by the judge each iteration."""
        from bridge.gateway.session_router import session_key_for

        objective = (message.slash_command_args or "").strip()
        if not objective:
            await adapter.send(  # type: ignore[attr-defined]
                message.source.chat_id,
                (
                    "Usage: `/goal <objective>`\n"
                    "Example: `/goal write me an async http client with retry`"
                ),
                thread_id=message.source.thread_id,
                ephemeral_user_id=message.source.user_id,
                raw_hint=message.raw,
            )
            return True

        key = session_key_for(message.source)
        if self.state is None:
            return True
        session = await self.state.ensure_session(
            session_id=key, coordination_strategy="goal",
        )
        # Stash gateway_source so the next turn's framing carries it.
        setattr(session, "gateway_source", message.source)
        # _set_goal arms the loop + auto-fires the calibrator in parallel.
        if hasattr(session, "_set_goal"):
            session._set_goal(objective, source="gateway-slack")
        await adapter.send(  # type: ignore[attr-defined]
            message.source.chat_id,
            (
                f"Goal loop armed:\n>>> {objective}\n\n"
                "The judge will review my work each turn. Send a follow-up "
                "to kick off the first agent turn, or just wait — "
                "calibration is running."
            ),
            thread_id=message.source.thread_id,
            ephemeral_user_id=message.source.user_id,
            raw_hint=message.raw,
        )
        return True

    async def _handle_perms_command(
        self,
        message: IncomingMessage,
        adapter: object,
    ) -> bool:
        """`/perms` — show the capability set this gateway session has."""
        from bridge.gateway.capabilities import tools_allowed_for_gateway
        allowed = sorted(tools_allowed_for_gateway(message.source.platform))
        # Format as a clean list.
        lines = ["*Slack agent permissions*"]
        lines.append("Allowed tools (read-mostly):")
        for t in allowed:
            lines.append(f"  • `{t}`")
        lines.append("")
        lines.append(
            "_Not allowed over Slack:_ bash, computer, browser, mouse/keyboard, "
            "screenshot, memory mutations. To grant any of those, switch to "
            "the Freyja desktop app for this session."
        )
        await adapter.send(  # type: ignore[attr-defined]
            message.source.chat_id,
            "\n".join(lines),
            thread_id=message.source.thread_id,
            ephemeral_user_id=message.source.user_id,
            raw_hint=message.raw,
        )
        return True

    def _cancel_session(self, session_key: str) -> bool:
        if self.state is None:
            return False
        sessions = getattr(self.state, "sessions", {})
        session = sessions.get(session_key)
        if session is None:
            return False
        task = getattr(session, "pending_task", None)
        if task is not None and not task.done():
            task.cancel()
            return True
        return False

    def _reset_session(self, session_key: str) -> None:
        if self.state is None:
            return
        sessions = getattr(self.state, "sessions", {})
        if session_key in sessions:
            # Best-effort cancel before drop.
            self._cancel_session(session_key)
            sessions.pop(session_key, None)

    async def _render_status(self, message: IncomingMessage) -> str:
        from bridge.gateway.session_router import session_key_for
        key = session_key_for(message.source)
        sessions = getattr(self.state, "sessions", {}) if self.state else {}
        session = sessions.get(key)
        lines: list[str] = ["*Freyja session status*"]
        lines.append(f"• key: `{key}`")
        if session is None:
            lines.append("• status: _no active session_ (next message will create one)")
            return "\n".join(lines)
        model = getattr(session, "model_id", "?")
        strategy = getattr(session, "coordination_strategy", "?")
        pending = getattr(session, "pending_task", None)
        queued = len(getattr(session, "queued_messages", []) or [])
        in_flight = bool(pending and not pending.done())
        lines.append(f"• model: `{model}`")
        lines.append(f"• mode: `{strategy}`")
        lines.append(f"• in-flight: {'yes' if in_flight else 'no'}")
        lines.append(f"• queued messages: {queued}")
        return "\n".join(lines)

    async def start(self) -> None:
        """Stand up the bridge state + connect all configured adapters."""
        from bridge.freyja_bridge import _BridgeState

        workspace = os.environ.get("FREYJA_WORKSPACE") or str(Path.home())
        default_model = os.environ.get("FREYJA_MODEL", "claude-sonnet-4-6")
        self.state = _BridgeState(
            workspace=workspace, default_model=default_model
        )
        logger.info(
            "bridge state ready (workspace=%s, model=%s)",
            workspace, default_model,
        )

        # Slack adapter is the only one in v1. Future: read
        # ~/.freyja/gateway.yaml to enable/disable adapters.
        adapter = SlackAdapter()
        ok = await adapter.connect(self._on_inbound)
        if ok:
            self.adapters.append(adapter)
            logger.info("slack adapter connected")
        else:
            logger.warning(
                "slack adapter did not connect — set SLACK_BOT_TOKEN + "
                "SLACK_APP_TOKEN via `freyja setup slack`"
            )

        if not self.adapters:
            logger.warning(
                "no platform adapters connected — gateway running idle. "
                "Configure at least one adapter to receive messages."
            )

    async def shutdown(self) -> None:
        logger.info("gateway shutdown beginning")
        for adapter in self.adapters:
            try:
                await adapter.disconnect()  # type: ignore[attr-defined]
            except Exception:
                logger.exception("adapter disconnect raised")
        self.adapters.clear()
        logger.info("gateway shutdown complete")

    def request_shutdown(self, *, planned: bool = True) -> None:
        self._planned_exit = self._planned_exit or planned
        self.shutdown_event.set()

    @property
    def planned_exit(self) -> bool:
        return self._planned_exit


def _install_signal_handlers(loop: asyncio.AbstractEventLoop, daemon: GatewayDaemon) -> None:
    def _on_sigterm() -> None:
        # If a takeover marker addressed at us is present, this SIGTERM
        # was planned by an incoming daemon — exit 0 so launchd doesn't
        # restart us into a flap loop.
        is_takeover = consume_takeover_marker()
        if is_takeover:
            logger.info("SIGTERM acknowledged (planned takeover)")
        else:
            logger.info("SIGTERM received")
        daemon.request_shutdown(planned=True)

    def _on_sigint() -> None:
        logger.info("SIGINT received")
        daemon.request_shutdown(planned=True)

    try:
        loop.add_signal_handler(signal.SIGTERM, _on_sigterm)
        loop.add_signal_handler(signal.SIGINT, _on_sigint)
    except NotImplementedError:
        # Windows doesn't support add_signal_handler. We're macOS-first
        # but the daemon is portable enough that this fallback matters.
        signal.signal(signal.SIGTERM, lambda *_: daemon.request_shutdown(planned=True))
        signal.signal(signal.SIGINT, lambda *_: daemon.request_shutdown(planned=True))


async def _async_main(*, replace: bool) -> int:
    if not acquire_lock(replace=replace):
        return 1

    _setup_logging()
    _load_env_into_os_environ()

    logger.info(
        "freyja gateway starting (pid=%d, home=%s)",
        os.getpid(),
        freyja_home(),
    )

    daemon = GatewayDaemon()
    loop = asyncio.get_running_loop()
    _install_signal_handlers(loop, daemon)

    try:
        await daemon.start()
    except Exception:
        logger.exception("daemon start failed")
        await daemon.shutdown()
        release_lock()
        return 2

    try:
        await daemon.shutdown_event.wait()
    finally:
        await daemon.shutdown()
        release_lock()

    return 0 if daemon.planned_exit else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="freyja-gateway",
        description="Freyja messaging gateway daemon.",
    )
    parser.add_argument(
        "--replace",
        action="store_true",
        help="If another gateway is running, take over from it.",
    )
    args = parser.parse_args(argv)

    try:
        return asyncio.run(_async_main(replace=args.replace))
    except KeyboardInterrupt:
        return 0
    except Exception:
        traceback.print_exc()
        return 3


if __name__ == "__main__":
    sys.exit(main())
