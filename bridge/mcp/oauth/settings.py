"""Interpretation of the per-server ``oauth`` config block.

Adapted from hermes-agent (Copyright (c) 2025 Nous Research, MIT) — the
``oauth:`` schema documented at the top of ``tools/mcp_oauth.py`` (lines
27-43) plus ``token_request_user_agent`` (1519-1535).

bridge/mcp/config.py stores the block verbatim on ``McpServerSpec.oauth``
(it is a parallel card's file, so the parser lives here instead). Shape::

    "oauth": {
      "client_id": "pre-registered-id",           # skip dynamic registration
      "client_secret": "${ACME_MCP_CLIENT_SECRET}",  # MUST be a ${VAR} reference
      "scope": "read write",                      # default: server-provided
      "redirect_port": 0,                         # 0 = auto-pick free port
      "redirect_host": "localhost",               # loopback hostname (WAF-safe)
      "redirect_uri": "https://proxy/callback",   # non-loopback proxy callback
      "client_name": "My Custom Client",          # default: "Freyja"
      "client_metadata_url": "https://me/cimd.json",  # self-hosted CIMD (enables CIMD)
      "cimd": false,                              # force DCR for this server
      "timeout_s": 300,                           # browser round-trip budget
      "token_endpoint_auth_method": "client_secret_post",
      "user_agent": "Freyja",                     # token-endpoint UA override
      "application_type": "native"
    }

Secret posture (design doc section 8, same rule as env/headers in
config.py): ``client_secret`` must be a ``${VAR}`` reference — an inline
literal is rejected with ``McpConfigError`` pointing at ``~/.freyja/.env``.
``client_id`` may also be a reference. Expansion happens here, at build
time, against the current environment, so ``.env`` edits take effect on
the next connect.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from bridge.mcp.config import (
    McpConfigError,
    McpServerSpec,
    expand_env_refs,
    looks_like_inline_secret,
)
from bridge.mcp.oauth.gates import OAuthNeedsAuthError, login_hint

DEFAULT_CLIENT_NAME = "Freyja"
DEFAULT_TOKEN_USER_AGENT = "Freyja"
DEFAULT_TIMEOUT_S = 300.0
DEFAULT_REDIRECT_HOST = "127.0.0.1"

_VALID_AUTH_METHODS = ("none", "client_secret_post", "client_secret_basic")


@dataclass
class OAuthSettings:
    """Resolved (env-expanded) per-server OAuth settings."""

    client_id: str | None = None
    client_secret: str | None = field(default=None, repr=False)
    """Excluded from repr so an accidental ``%r`` of settings cannot leak it."""
    scope: str | None = None
    redirect_port: int = 0
    redirect_host: str = DEFAULT_REDIRECT_HOST
    redirect_uri: str | None = None
    client_name: str | None = None
    """None means "use the default" — provider defaults (Figma) may fill it."""
    client_metadata_url: str | None = None
    cimd: bool | None = None
    """None = library default (currently OFF, see cimd.py); True/False forces."""
    timeout_s: float = DEFAULT_TIMEOUT_S
    token_endpoint_auth_method: str | None = None
    user_agent: str | None = None
    application_type: str = "native"
    extra: dict[str, Any] = field(default_factory=dict)
    """Unknown keys, preserved for forward compat."""

    # Populated by the provider wiring, never from config:
    resolved_port: int | None = field(default=None, repr=False)
    cimd_url: str | None = field(default=None, repr=False)

    @property
    def effective_client_name(self) -> str:
        return self.client_name or DEFAULT_CLIENT_NAME

    @property
    def effective_user_agent(self) -> str:
        return self.user_agent or DEFAULT_TOKEN_USER_AGENT

    @property
    def preregistered(self) -> bool:
        return bool(self.client_id)

    def to_redacted_dict(self) -> dict[str, Any]:
        """Loggable view — the secret is never included."""
        from bridge.mcp.oauth.storage import redact_secret

        return {
            "client_id": self.client_id,
            "client_secret": redact_secret(self.client_secret) if self.client_secret else None,
            "scope": self.scope,
            "redirect_port": self.redirect_port,
            "redirect_host": self.redirect_host,
            "redirect_uri": self.redirect_uri,
            "client_name": self.client_name,
            "client_metadata_url": self.client_metadata_url,
            "cimd": self.cimd,
            "timeout_s": self.timeout_s,
            "token_endpoint_auth_method": self.token_endpoint_auth_method,
            "user_agent": self.user_agent,
            "application_type": self.application_type,
        }


_KNOWN_KEYS = frozenset({
    "client_id", "client_secret", "scope", "redirect_port", "redirect_host",
    "redirect_uri", "client_name", "client_metadata_url", "cimd", "timeout_s",
    "timeout", "token_endpoint_auth_method", "user_agent", "application_type",
})


def _opt_str(raw: Mapping[str, Any], key: str) -> str | None:
    value = raw.get(key)
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def validate_oauth_block(server_name: str, raw: Mapping[str, Any] | None) -> None:
    """Raise McpConfigError for a malformed/unsafe ``oauth`` block (no expansion)."""
    if raw is None:
        return
    if not isinstance(raw, Mapping):
        raise McpConfigError(f"server '{server_name}': oauth must be an object")
    secret = raw.get("client_secret")
    if isinstance(secret, str) and looks_like_inline_secret("client_secret", secret):
        raise McpConfigError(
            f"server '{server_name}': oauth.client_secret looks like an inline secret; "
            "put the value in ~/.freyja/.env and reference it as ${VAR} instead"
        )
    if secret is not None and not isinstance(secret, str):
        raise McpConfigError(f"server '{server_name}': oauth.client_secret must be a string")
    port = raw.get("redirect_port")
    if port is not None:
        try:
            port_i = int(port)
        except (TypeError, ValueError):
            raise McpConfigError(
                f"server '{server_name}': oauth.redirect_port must be an integer"
            ) from None
        if not (0 <= port_i <= 65535):
            raise McpConfigError(f"server '{server_name}': oauth.redirect_port out of range")
    method = raw.get("token_endpoint_auth_method")
    if method is not None and str(method) not in _VALID_AUTH_METHODS:
        raise McpConfigError(
            f"server '{server_name}': oauth.token_endpoint_auth_method must be one of "
            f"{_VALID_AUTH_METHODS}"
        )
    cimd_url = raw.get("client_metadata_url")
    if cimd_url is not None and (not isinstance(cimd_url, str) or not cimd_url.startswith("https://")):
        raise McpConfigError(
            f"server '{server_name}': oauth.client_metadata_url must be an https:// URL"
        )
    for key in ("timeout_s", "timeout"):
        if raw.get(key) is not None:
            try:
                if float(raw[key]) <= 0:
                    raise ValueError
            except (TypeError, ValueError):
                raise McpConfigError(
                    f"server '{server_name}': oauth.{key} must be a positive number"
                ) from None


def parse_oauth_settings(
    spec: McpServerSpec,
    environ: Mapping[str, str] | None = None,
) -> OAuthSettings:
    """Validate + env-expand ``spec.oauth`` into :class:`OAuthSettings`.

    Raises:
        McpConfigError: malformed block or inline secret.
        OAuthNeedsAuthError: ``client_secret`` references an unset variable
            (a missing credential is a needs-auth condition, not a crash —
            connection.py treats it like a missing ``${TOKEN}`` header).
    """
    raw: Mapping[str, Any] = spec.oauth or {}
    validate_oauth_block(spec.name, raw)
    env = dict(environ) if environ is not None else None

    settings = OAuthSettings()

    client_id = _opt_str(raw, "client_id")
    if client_id:
        settings.client_id = expand_env_refs(client_id, env).text.strip() or None

    secret_ref = _opt_str(raw, "client_secret")
    if secret_ref:
        expanded = expand_env_refs(secret_ref, env)
        if expanded.missing:
            raise OAuthNeedsAuthError(
                f"server '{spec.name}': oauth.client_secret references unset "
                f"${{{expanded.missing[0]}}}; add it to ~/.freyja/.env. {login_hint(spec.name)}",
                server_name=spec.name,
            )
        settings.client_secret = expanded.text or None

    settings.scope = _opt_str(raw, "scope")
    settings.redirect_port = int(raw.get("redirect_port") or 0)
    settings.redirect_host = _opt_str(raw, "redirect_host") or DEFAULT_REDIRECT_HOST
    settings.redirect_uri = _opt_str(raw, "redirect_uri")
    settings.client_name = _opt_str(raw, "client_name")
    settings.client_metadata_url = _opt_str(raw, "client_metadata_url")
    cimd = raw.get("cimd")
    settings.cimd = bool(cimd) if cimd is not None else None
    timeout = raw.get("timeout_s", raw.get("timeout"))
    settings.timeout_s = float(timeout) if timeout is not None else DEFAULT_TIMEOUT_S
    settings.token_endpoint_auth_method = _opt_str(raw, "token_endpoint_auth_method")
    settings.user_agent = _opt_str(raw, "user_agent")
    settings.application_type = _opt_str(raw, "application_type") or "native"
    settings.extra = {k: v for k, v in raw.items() if k not in _KNOWN_KEYS}
    return settings


__all__ = [
    "DEFAULT_CLIENT_NAME",
    "DEFAULT_REDIRECT_HOST",
    "DEFAULT_TIMEOUT_S",
    "DEFAULT_TOKEN_USER_AGENT",
    "OAuthSettings",
    "parse_oauth_settings",
    "validate_oauth_block",
]
