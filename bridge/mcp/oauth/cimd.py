"""Client ID Metadata Document (CIMD) support for MCP OAuth.

Adapted from hermes-agent (Copyright (c) 2025 Nous Research, MIT) —
``tools/mcp_oauth.py`` lines 1293-1516.

Under CIMD the ``client_id`` IS an HTTPS URL that the authorization server
fetches to learn our app name, logo and permitted redirect URIs, replacing
the per-install RFC 7591 registration that the MCP spec deprecated in
2026-07-28. The SDK does the protocol work (``should_use_client_metadata_url``
/ ``create_client_info_from_metadata_url``); this module only decides
whether a given flow is eligible and hands the URL to
``OAuthClientProvider(client_metadata_url=...)``.

Client identification priority (MCP 2026-07-28 authorization spec):

  1. CIMD — when the AS advertises ``client_id_metadata_document_supported``
     and we have a published document URL;
  2. pre-registered ``oauth.client_id`` (+ optional ``client_secret``);
  3. RFC 7591 dynamic client registration (deprecated fallback).

**DEFAULT OFF.** ``FREYJA_CLIENT_METADATA_URL`` is ``None`` until the
operator publishes ``docs/oauth/client-metadata.json`` at a stable https
origin that does NOT redirect (the CIMD draft forbids the AS from following
redirects when fetching the document — hermes hosts theirs on github.io for
exactly that reason). Until then a server only uses CIMD when its ``oauth``
block sets ``client_metadata_url`` explicitly. Once published: set the
constant, and ``cimd: false`` per server remains the opt-out.

Rejection memory: an AS that fetches our document and refuses it returns
``invalid_client`` at the token endpoint (or aborts at the authorization
endpoint). ``FreyjaTokenStorage.mark_cimd_rejected()`` writes a ``cimd-off``
marker so the next process doesn't walk back into the same refusal; the
marker is cleared by ``/mcp login`` (``storage.remove()``).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

from bridge.mcp.oauth.callback import CIMD_PORTS, LOOPBACK_HOSTS, pick_cimd_port

if TYPE_CHECKING:  # pragma: no cover
    from bridge.mcp.oauth.settings import OAuthSettings
    from bridge.mcp.oauth.storage import FreyjaTokenStorage

logger = logging.getLogger(__name__)

# Set this to the published URL of docs/oauth/client-metadata.json once the
# operator hosts it (e.g. "https://<org>.github.io/freyja/oauth/client-metadata.json").
# None = CIMD is opt-in per server via oauth.client_metadata_url.
FREYJA_CLIENT_METADATA_URL: str | None = None

# Loopback hostnames the document lists alongside each port, so the
# ``oauth.redirect_host: localhost`` WAF workaround still works under CIMD.
CIMD_REDIRECT_HOSTS = LOOPBACK_HOSTS

# Relative path of the shipped document (for the doc <-> code consistency test).
CIMD_DOCUMENT_RELPATH = "docs/oauth/client-metadata.json"


def is_valid_cimd_url(url: str | None) -> bool:
    """True when *url* is usable as a CIMD client_id on the installed SDK.

    Delegates to the SDK's own validator so we never hand
    ``OAuthClientProvider`` a URL its constructor would reject outright.

    The SDK checks only the https-scheme and non-root-path halves of
    draft-ietf-oauth-client-id-metadata-document section 3. The rest is
    enforced here because a URL that violates it fails at the authorization
    server, mid-browser-flow, where the user sees an opaque invalid-client
    page instead of a config error.
    """
    if not url:
        return False
    try:
        from mcp.client.auth.utils import is_valid_client_metadata_url
    except ImportError:  # pragma: no cover — SDK predates CIMD
        return False
    if not is_valid_client_metadata_url(url):
        return False
    try:
        parsed = urlparse(url)
        # Accessing username/password parses the netloc, which can raise.
        has_userinfo = bool(parsed.username or parsed.password)
    except ValueError:
        return False
    if has_userinfo or parsed.fragment:
        return False
    return not any(seg in {".", ".."} for seg in parsed.path.split("/"))


def effective_cimd_url(settings: "OAuthSettings") -> str | None:
    """The CIMD URL this server would present, or None when CIMD is off.

    ``oauth.cimd: false`` always disables. An explicit
    ``oauth.client_metadata_url`` enables (self-hosted document). Otherwise
    the library default applies — which is OFF until
    ``FREYJA_CLIENT_METADATA_URL`` is set.
    """
    if settings.cimd is False:
        return None
    url = settings.client_metadata_url or FREYJA_CLIENT_METADATA_URL
    if not url:
        return None
    return url


def server_declined_cimd(storage: "FreyjaTokenStorage | None") -> bool:
    """True when cached metadata shows this server doesn't advertise CIMD.

    Pinning a callback port is only needed for a flow that actually ends up
    using CIMD, but the SDK decides that during its 401 branch — long after
    we have to fix the redirect URI. Cached authorization-server metadata
    from an earlier connection closes the gap for every server the user has
    already reached: one that never advertised
    ``client_id_metadata_document_supported`` keeps the reserved ephemeral
    port it has always used, and only a genuinely unknown server pays the
    optimistic pin.
    """
    if storage is None:
        return False
    try:
        metadata = storage.load_oauth_metadata()
    except (AttributeError, TypeError, ValueError):
        return False
    if metadata is None:
        return False
    return getattr(metadata, "client_id_metadata_document_supported", None) is not True


def maybe_use_cimd(
    settings: "OAuthSettings",
    storage: "FreyjaTokenStorage | None" = None,
    *,
    handoff_redirect: bool = False,
) -> tuple[str, int] | None:
    """Return ``(client_id URL, pinned callback port)``, or None to use DCR.

    Every early return below is a case where the redirect URI we would send
    is not one the published document declares, where the client identity
    is already settled, or where the server is known not to want a document
    — DCR remains correct in all of them. Passing a metadata URL anyway
    would make the SDK present a client_id whose registered redirect URIs
    don't match the request, and the authorization server would reject the
    flow.
    """
    url = effective_cimd_url(settings)
    if not url:
        return None
    if not is_valid_cimd_url(url):
        logger.warning(
            "mcp oauth: ignoring invalid client_metadata_url (must be https with a path)"
        )
        return None

    # A client pinned in config is the user's explicit choice, and a secret
    # means they want a confidential client — the document forbids shared
    # secrets (draft section 4.1).
    if settings.client_id or settings.client_secret:
        return None

    # The document, not the config, supplies the name and auth method the
    # server sees, so a caller that set either is asking for an identity
    # CIMD cannot present. Figma's DCR name allowlist (applied by
    # apply_provider_defaults) is the in-tree example.
    if settings.client_name:
        return None
    if (settings.token_endpoint_auth_method or "none") != "none":
        return None

    # A non-loopback (proxy) redirect is deployment-specific and can never
    # appear in a static document.
    if handoff_redirect or settings.redirect_uri or settings.redirect_port:
        return None

    if (settings.redirect_host or "127.0.0.1") not in CIMD_REDIRECT_HOSTS:
        return None

    # An existing registration is bound to the redirect URI it registered
    # with; swapping in a CIMD client_id now would invalidate stored tokens.
    if storage is not None and storage.has_cached_client_info():
        return None

    if storage is not None and storage.cimd_rejected():
        return None

    if server_declined_cimd(storage):
        return None

    port = pick_cimd_port()
    if port is None:
        return None
    return url, port


def cimd_provider_kwargs(settings: "OAuthSettings") -> dict[str, Any]:
    """``client_metadata_url=`` for ``OAuthClientProvider``, when CIMD applies.

    Returned as kwargs rather than a plain value so the argument is omitted
    entirely on a DCR flow.
    """
    url = settings.cimd_url
    return {"client_metadata_url": url} if url else {}


def expected_document_redirect_uris() -> list[str]:
    """The redirect_uris docs/oauth/client-metadata.json must declare."""
    uris: list[str] = []
    for port in CIMD_PORTS:
        for host in ("127.0.0.1", "localhost"):
            uris.append(f"http://{host}:{port}/callback")
    return uris


__all__ = [
    "CIMD_DOCUMENT_RELPATH",
    "CIMD_PORTS",
    "CIMD_REDIRECT_HOSTS",
    "FREYJA_CLIENT_METADATA_URL",
    "cimd_provider_kwargs",
    "effective_cimd_url",
    "expected_document_redirect_uris",
    "is_valid_cimd_url",
    "maybe_use_cimd",
    "server_declined_cimd",
]
