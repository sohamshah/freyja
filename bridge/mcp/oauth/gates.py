"""Interactivity gates + typed needs-auth errors for MCP OAuth.

Adapted from hermes-agent (Copyright (c) 2025 Nous Research, MIT) —
``tools/mcp_oauth.py`` lines 146-181, 325-381.

Why a ContextVar and not ``threading.local``: hermes discovered (#35927)
that background MCP discovery sets the suppression flag on one thread while
the actual connect + OAuth coroutine runs on another via
``run_coroutine_threadsafe``. asyncio copies the *calling context* into the
scheduled coroutine, so a ContextVar propagates across that boundary while a
threading.local would not. Freyja's bridge is a single asyncio loop today,
but the gateway daemon and the desktop bridge both schedule work from
non-loop threads (Slack socket-mode callbacks, Electron IPC) so the same
argument applies.

Freyja adaptation: hermes decides "interactive" by ``sys.stdin.isatty()``.
Freyja's surfaces are a desktop UI and a Slack gateway — there is never a
TTY — so interactivity is an explicit per-call decision made by the caller
of ``build_httpx_auth(..., interactive=...)`` (``/mcp login`` says True;
background connects say False). The ContextVar gates layer on top:
suppression always wins, forcing wins over the default heuristic.
"""

from __future__ import annotations

import contextvars
import sys
from contextlib import contextmanager
from typing import Iterator

# The single actionable next step, worded once so every non-interactive
# OAuth boundary agrees (hermes #57836: "hermes mcp login <server>").
LOGIN_COMMAND_TEMPLATE = "/mcp login {server}"


def login_hint(server_name: str | None) -> str:
    """The actionable "run /mcp login <server>" sentence."""
    target = server_name or "<server>"
    return (
        f"Run `{LOGIN_COMMAND_TEMPLATE.format(server=target)}` to (re)authorize, "
        "then reconnect the server."
    )


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class OAuthNeedsAuthError(RuntimeError):
    """Typed "this server needs (re)authorization" failure.

    Raised by the provider/flow layer whenever the only way forward is a
    human completing a browser authorization: no cached tokens in a
    non-interactive context, refresh rejected and no interactive session,
    a 401 that survived a freshly minted token, etc. connection.py maps it
    to the NEEDS_AUTH state without burning the retry ladder.

    ``server_name`` is carried so callers can render the login hint even
    when they only have the exception object.
    """

    def __init__(self, message: str, *, server_name: str | None = None) -> None:
        super().__init__(message)
        self.server_name = server_name


class OAuthNonInteractiveError(OAuthNeedsAuthError):
    """Raised when OAuth requires browser interaction in a non-interactive env."""


class OAuthCallbackTimeoutError(OAuthNonInteractiveError):
    """The loopback callback never arrived within the flow timeout."""


class OAuthUserSkippedError(OAuthNonInteractiveError):
    """The user typed a skip token at the paste prompt — opt out, not a failure."""


class OAuthCallbackPortInUseError(OAuthNonInteractiveError):
    """The fixed/cached loopback port could not be bound."""


# ---------------------------------------------------------------------------
# ContextVar gates
# ---------------------------------------------------------------------------

# Default True (interactive allowed) — suppression is opt-in per context.
_oauth_interactive_enabled: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "_oauth_interactive_enabled", default=True
)

# Forces is_interactive() past the explicit/stdin heuristic for flows driven
# from a GUI or chat surface: the browser + loopback callback do all the
# work there. Suppression still wins — background discovery must never
# start a browser flow.
_oauth_interactive_forced: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "_oauth_interactive_forced", default=False
)


def is_interactive(explicit: bool | None = None) -> bool:
    """Return True if we can reasonably expect a human to complete a browser flow.

    Resolution order:
      1. ``suppress_interactive_oauth()`` active -> False (always wins)
      2. ``force_interactive_oauth()`` active -> True
      3. ``explicit`` (the caller's ``interactive=`` flag) when given
      4. fallback: ``sys.stdin.isatty()`` (hermes's TTY heuristic; only
         reached by callers that pass nothing, e.g. ad-hoc scripts)
    """
    if not _oauth_interactive_enabled.get():
        return False
    if _oauth_interactive_forced.get():
        return True
    if explicit is not None:
        return bool(explicit)
    try:
        return sys.stdin.isatty()
    except (AttributeError, ValueError):
        return False


def raise_if_non_interactive(
    lead: str,
    *,
    server_name: str | None = None,
    explicit: bool | None = None,
) -> None:
    """Raise ``OAuthNonInteractiveError`` unless an interactive session exists.

    ``lead`` is the boundary-specific first sentence; this helper appends the
    shared, actionable ``/mcp login <server>`` next-step so the guidance
    wording lives in one place across every non-interactive OAuth boundary.
    """
    if not is_interactive(explicit):
        raise OAuthNonInteractiveError(
            f"{lead} {login_hint(server_name)}", server_name=server_name
        )


@contextmanager
def force_interactive_oauth() -> Iterator[None]:
    """Treat the current execution context as interactive despite no TTY.

    For GUI/chat-driven auth (``/mcp login`` from the desktop or Slack): the
    user IS present — just not on stdin. Same ContextVar propagation story
    as :func:`suppress_interactive_oauth`.
    """
    token = _oauth_interactive_forced.set(True)
    try:
        yield
    finally:
        _oauth_interactive_forced.reset(token)


@contextmanager
def suppress_interactive_oauth() -> Iterator[None]:
    """Disable browser/paste OAuth prompts for the current execution context.

    Uses a ContextVar so the suppression propagates from a background
    discovery thread onto a coroutine scheduled (via
    ``run_coroutine_threadsafe``) on the bridge loop — where the OAuth
    callback actually runs. A threading.local would not cross that boundary.
    """
    token = _oauth_interactive_enabled.set(False)
    try:
        yield
    finally:
        _oauth_interactive_enabled.reset(token)


__all__ = [
    "LOGIN_COMMAND_TEMPLATE",
    "OAuthCallbackPortInUseError",
    "OAuthCallbackTimeoutError",
    "OAuthNeedsAuthError",
    "OAuthNonInteractiveError",
    "OAuthUserSkippedError",
    "force_interactive_oauth",
    "is_interactive",
    "login_hint",
    "raise_if_non_interactive",
    "suppress_interactive_oauth",
]
