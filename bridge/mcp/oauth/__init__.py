"""OAuth 2.1 client support for ``auth: oauth`` MCP servers.

Adapted from hermes-agent (Copyright (c) 2025 Nous Research, MIT) —
``tools/mcp_oauth.py``, ``tools/mcp_oauth_manager.py`` and
``tools/mcp_dashboard_oauth.py``. See each module's docstring for the exact
line ranges ported and the mcp 2.1.1 adaptations.

Modules:

- ``gates``     ContextVar interactivity gate + typed needs-auth errors.
- ``storage``   ``FreyjaTokenStorage``: 0600 atomic files, absolute
                ``expires_at``, AS-metadata cache, poison recovery.
- ``settings``  ``OAuthSettings`` parser for the per-server ``oauth`` block.
- ``callback``  TOCTOU-safe port reservation, cached redirect port, CIMD
                pinned-port pool, loopback listener, paste parser.
- ``cimd``      Client ID Metadata Document eligibility (default OFF).
- ``provider``  ``FreyjaOAuthClientProvider`` + ``build_httpx_auth``.
- ``flow``      ``LoginFlow`` / ``BrowserFlow`` / ``HandoffFlow`` + ``run_login``.

Import cost: ``provider`` and ``flow`` pull in the mcp SDK (+httpx2), so
they are exported lazily through module ``__getattr__``; importing this
package alone stays cheap for bridge startup.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from bridge.mcp.oauth.gates import (
    OAuthCallbackPortInUseError,
    OAuthCallbackTimeoutError,
    OAuthNeedsAuthError,
    OAuthNonInteractiveError,
    OAuthUserSkippedError,
    force_interactive_oauth,
    login_hint,
    suppress_interactive_oauth,
)
from bridge.mcp.oauth.settings import OAuthSettings, parse_oauth_settings, validate_oauth_block
from bridge.mcp.oauth.storage import (
    FreyjaTokenStorage,
    clear,
    default_token_root,
    list_servers,
    redact_payload,
    redact_secret,
)

if TYPE_CHECKING:  # pragma: no cover
    from bridge.mcp.oauth.flow import (  # noqa: F401
        BrowserFlow,
        HandoffFlow,
        LoginFlow,
        LoginResult,
        run_login,
    )
    from bridge.mcp.oauth.provider import FreyjaOAuthClientProvider, build_httpx_auth  # noqa: F401

_LAZY = {
    "build_httpx_auth": ("bridge.mcp.oauth.provider", "build_httpx_auth"),
    "FreyjaOAuthClientProvider": ("bridge.mcp.oauth.provider", "FreyjaOAuthClientProvider"),
    "run_login": ("bridge.mcp.oauth.flow", "run_login"),
    "LoginResult": ("bridge.mcp.oauth.flow", "LoginResult"),
    "LoginFlow": ("bridge.mcp.oauth.flow", "LoginFlow"),
    "BrowserFlow": ("bridge.mcp.oauth.flow", "BrowserFlow"),
    "HandoffFlow": ("bridge.mcp.oauth.flow", "HandoffFlow"),
}


def __getattr__(name: str) -> Any:
    target = _LAZY.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    import importlib

    module = importlib.import_module(target[0])
    value = getattr(module, target[1])
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(_LAZY))


__all__ = [
    "BrowserFlow",
    "FreyjaOAuthClientProvider",
    "FreyjaTokenStorage",
    "HandoffFlow",
    "LoginFlow",
    "LoginResult",
    "OAuthCallbackPortInUseError",
    "OAuthCallbackTimeoutError",
    "OAuthNeedsAuthError",
    "OAuthNonInteractiveError",
    "OAuthSettings",
    "OAuthUserSkippedError",
    "build_httpx_auth",
    "clear",
    "default_token_root",
    "force_interactive_oauth",
    "list_servers",
    "login_hint",
    "parse_oauth_settings",
    "redact_payload",
    "redact_secret",
    "run_login",
    "suppress_interactive_oauth",
    "validate_oauth_block",
]
