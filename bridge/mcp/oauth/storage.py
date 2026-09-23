"""Persistent per-server OAuth state for MCP servers (tokens, client info, metadata).

Adapted from hermes-agent (Copyright (c) 2025 Nous Research, MIT) —
``tools/mcp_oauth.py`` lines 192-206 (dir/filename helpers), 403-449
(``_read_json`` / ``_write_json``), 457-723 (``HermesTokenStorage``) and
1739-1791 (``_invalidate_tokens_on_client_change``).

Layout (Freyja adaptation: one directory per server instead of hermes's
flat ``<server>.json`` / ``<server>.client.json`` files)::

    ~/.freyja/mcp-tokens/                 0700
        <server>/                         0700
            tokens.json                   0600  OAuthToken + absolute expires_at
            client.json                   0600  OAuthClientInformationFull
            meta.json                     0600  OAuthMetadata (AS discovery cache)
            cimd-off                            marker: AS refused our CIMD document
            client.json.bak               0600  last poisoned registration
            .refresh.lock                 0600  flock: one process refreshes at a time

Security model (hermes #19673): files are created with
``os.open(O_WRONLY|O_CREAT|O_EXCL, 0o600)`` into a per-pid/random temp name
and then ``os.replace``'d over the target, so the credential is never
observable at umask permissions and readers never see a torn write. The
parent directories are tightened to 0700. No encryption at rest (plaintext
JSON, like hermes and Claude Code) — the threat model is other local users,
not local root.

``expires_at`` (hermes Fix A): the MCP SDK serialises only the relative
``expires_in``. After a restart the SDK reloads it with no wall-clock anchor
and ``is_token_valid()`` reports True for a token that expired hours ago.
``set_tokens`` writes an absolute ``expires_at`` next to it; ``get_tokens``
rewrites ``expires_in`` to the remaining seconds (clamped at 0) so the SDK's
own expiry maths comes out right — and a token that is expired by
``expires_at`` is reported expired even if the stored ``expires_in`` says
otherwise.

SDK note (mcp 2.1.1): ``TokenStorage`` is a ``typing.Protocol`` with four
async methods; ``OAuthToken`` / ``OAuthClientInformationFull`` /
``OAuthMetadata`` live in ``mcp.shared.auth``. Imported lazily so importing
this module does not pay the SDK import cost at bridge startup.

Logging: nothing in this module logs a token, secret, code or client_secret
value. Use :func:`redact_secret` / :func:`redact_payload` when you must log
something that might contain one.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import secrets
import stat
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterable

if TYPE_CHECKING:  # pragma: no cover
    from mcp.shared.auth import OAuthClientInformationFull, OAuthMetadata, OAuthToken

logger = logging.getLogger(__name__)

TOKEN_DIR_NAME = "mcp-tokens"

TOKENS_FILE = "tokens.json"
CLIENT_FILE = "client.json"
META_FILE = "meta.json"
CIMD_OFF_FILE = "cimd-off"
# flock target for cross-process refresh serialization; never holds data.
REFRESH_LOCK_FILE = ".refresh.lock"


# ---------------------------------------------------------------------------
# Redaction helpers — the one place that decides how a secret is rendered.
# ---------------------------------------------------------------------------

# JSON keys whose values must never reach a log line.
SECRET_KEYS = frozenset({
    "access_token", "refresh_token", "id_token", "client_secret", "code",
    "code_verifier", "registration_access_token", "authorization", "token",
    "password", "secret",
})


def redact_secret(value: Any) -> str:
    """Render a secret as ``<redacted sha256:xxxxxx>`` — correlatable, not recoverable.

    The 6-hex-char digest prefix lets two log lines be recognised as the
    same credential without exposing any of its characters (unlike the
    common "last 4" pattern, which leaks entropy from short secrets).
    """
    if value is None:
        return "<none>"
    text = value if isinstance(value, str) else str(value)
    if not text:
        return "<empty>"
    digest = hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:6]
    return f"<redacted sha256:{digest}>"


def _is_secret_key(key: str) -> bool:
    lowered = key.lower()
    return lowered in SECRET_KEYS or any(
        needle in lowered for needle in ("token", "secret", "password")
    )


def redact_payload(data: Any) -> Any:
    """Deep-copy *data* with every secret-shaped value replaced by its redaction.

    Safe to hand to ``logger.debug("%s", redact_payload(payload))``. Lists and
    nested dicts are walked; scalars are returned unchanged.
    """
    if isinstance(data, dict):
        return {
            k: (redact_secret(v) if _is_secret_key(str(k)) and v is not None else redact_payload(v))
            for k, v in data.items()
        }
    if isinstance(data, list):
        return [redact_payload(v) for v in data]
    return data


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------


def default_token_root() -> Path:
    """``$FREYJA_HOME/mcp-tokens`` (falls back to ``~/.freyja/mcp-tokens``).

    Mirrors ``freyja_home()`` in bridge/gateway/pid.py without importing it
    (that helper mkdirs the home eagerly; we defer creation to first write).
    """
    home = Path(os.environ.get("FREYJA_HOME") or (Path.home() / ".freyja"))
    return home / TOKEN_DIR_NAME


def safe_filename(name: str) -> str:
    """Sanitize a server name for use as a directory name (no path separators)."""
    return re.sub(r"[^\w\-]", "_", name).strip("_")[:128] or "default"


def secure_dir(path: Path) -> None:
    """chmod 0700 on *path* if safe (refuses ``/`` and top-level dirs).

    Port of hermes ``secure_parent_dir`` (#25821, #93050): a mis-resolved
    ``FREYJA_HOME`` must never make us chmod ``/home`` or ``/tmp``. No-op on
    platforms without POSIX mode bits; failures are ignored.
    """
    try:
        resolved = path.resolve()
    except OSError:
        return
    if resolved == Path(resolved.anchor) or len(resolved.parts) < 3:
        return
    try:
        os.chmod(resolved, stat.S_IRWXU)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# JSON I/O
# ---------------------------------------------------------------------------


def read_json(path: Path) -> dict | None:
    """Read a JSON object, returning None if it doesn't exist or is invalid."""
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("mcp oauth: failed to read %s: %s", path, exc)
        return None
    if not isinstance(data, dict):
        logger.warning("mcp oauth: %s is not a JSON object; ignoring", path)
        return None
    return data


def write_json(path: Path, data: dict) -> None:
    """Write a dict as JSON with restricted permissions (0o600), atomically.

    Uses ``os.open`` with ``O_EXCL`` and an explicit mode so the file is
    created atomically at 0o600. ``write_text`` + post-write ``chmod`` would
    open a TOCTOU window where the temp file briefly inherited the process
    umask (commonly 0o644 = world-readable), exposing OAuth tokens to other
    local users between create and chmod (hermes #19673). ``os.replace``
    then swaps it into place so concurrent readers never see a torn file.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    # Tighten both the server dir and the mcp-tokens root to 0700 so
    # siblings can't traverse to the creds.
    secure_dir(path.parent)
    if path.parent.name != TOKEN_DIR_NAME and path.parent.parent.name == TOKEN_DIR_NAME:
        secure_dir(path.parent.parent)
    # Per-process random suffix avoids collisions between concurrent
    # writers and stale leftovers from a prior crashed write.
    tmp = path.with_name(f"{path.name}.tmp.{os.getpid()}.{secrets.token_hex(4)}")
    try:
        fd = os.open(
            str(tmp),
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            stat.S_IRUSR | stat.S_IWUSR,
        )
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, default=str)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except OSError:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise


# ---------------------------------------------------------------------------
# FreyjaTokenStorage
# ---------------------------------------------------------------------------


class FreyjaTokenStorage:
    """Persist OAuth tokens, client registration and AS metadata to disk.

    Implements the SDK's ``TokenStorage`` protocol (four async methods) plus
    the synchronous helpers the provider/flow layer needs. One instance per
    server; ``root`` is injectable so tests never touch ``~/.freyja``.
    """

    def __init__(self, server_name: str, *, root: str | Path | None = None):
        self.server_name = server_name
        self._safe_name = safe_filename(server_name)
        self._root = Path(root) if root is not None else None

    # -- paths -------------------------------------------------------------

    @property
    def root(self) -> Path:
        return self._root if self._root is not None else default_token_root()

    @property
    def server_dir(self) -> Path:
        return self.root / self._safe_name

    def tokens_path(self) -> Path:
        return self.server_dir / TOKENS_FILE

    def client_info_path(self) -> Path:
        return self.server_dir / CLIENT_FILE

    def meta_path(self) -> Path:
        return self.server_dir / META_FILE

    def cimd_rejected_path(self) -> Path:
        return self.server_dir / CIMD_OFF_FILE

    def refresh_lock_path(self) -> Path:
        return self.server_dir / REFRESH_LOCK_FILE

    def tokens_mtime_ns(self) -> int | None:
        try:
            return self.tokens_path().stat().st_mtime_ns
        except OSError:
            return None

    # -- cross-process refresh lock ------------------------------------------
    #
    # The desktop bridge, the gateway and the scheduler daemon each hold
    # their own in-memory copy of these tokens. Slack and Atlassian rotate
    # refresh tokens: every refresh returns a new one and revokes the old.
    # Unserialized, the first process to refresh strands the others on a
    # revoked refresh token — their next refresh fails (Slack
    # ``invalid_grant``, Atlassian 403 ``refresh_token is invalid``), the SDK
    # clears the tokens, and the server drops to needs-auth even though
    # tokens.json on disk is perfectly valid. The provider holds this flock
    # across "re-read disk → refresh → write", so one process refreshes and
    # the rest adopt its result.

    async def acquire_refresh_lock(self, *, timeout: float = 30.0) -> int | None:
        """Take the exclusive refresh flock; returns an fd for
        :meth:`release_refresh_lock`, or None if it couldn't be taken in
        ``timeout`` (callers proceed unlocked rather than hang)."""
        import fcntl

        import anyio

        try:
            self.server_dir.mkdir(parents=True, exist_ok=True)
            secure_dir(self.server_dir)
            fd = os.open(
                str(self.refresh_lock_path()),
                os.O_RDWR | os.O_CREAT,
                stat.S_IRUSR | stat.S_IWUSR,
            )
        except OSError as exc:
            logger.debug("mcp oauth '%s': refresh lock unavailable: %s", self.server_name, exc)
            return None
        deadline = time.monotonic() + timeout
        while True:
            try:
                # Non-blocking + poll: never parks an event-loop thread, and
                # two fds in the same process still exclude each other.
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return fd
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    logger.warning(
                        "mcp oauth '%s': refresh lock busy for %.0fs — refreshing unlocked",
                        self.server_name, timeout,
                    )
                    os.close(fd)
                    return None
                await anyio.sleep(0.05)
            except OSError:
                os.close(fd)
                return None

    @staticmethod
    def release_refresh_lock(fd: int | None) -> None:
        if fd is None:
            return
        import fcntl

        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        try:
            os.close(fd)
        except OSError:
            pass

    # -- tokens ------------------------------------------------------------

    async def get_tokens(self) -> "OAuthToken | None":
        data = read_json(self.tokens_path())
        if data is None:
            return None
        from mcp.shared.auth import OAuthToken

        # We record an absolute wall-clock ``expires_at`` alongside the SDK's
        # serialized token (see ``set_tokens``). On read we rewrite
        # ``expires_in`` to the remaining seconds so the SDK's downstream
        # ``update_token_expiry`` computes the correct absolute time and
        # ``is_token_valid()`` correctly reports False for tokens that
        # expired while the process was down.
        #
        # Legacy token files have ``expires_in`` but no ``expires_at``. Fall
        # back to the file's mtime as a best-effort wall-clock proxy for when
        # the token was written: if (mtime + expires_in) is in the past,
        # clamp ``expires_in`` to zero so the SDK refreshes before the first
        # request. Self-heals on the next ``set_tokens``.
        absolute_expiry = data.pop("expires_at", None)
        if absolute_expiry is not None:
            try:
                data["expires_in"] = int(max(float(absolute_expiry) - time.time(), 0))
            except (TypeError, ValueError):
                pass
        elif data.get("expires_in") is not None:
            try:
                file_mtime = self.tokens_path().stat().st_mtime
            except OSError:
                file_mtime = None
            if file_mtime is not None:
                try:
                    implied_expiry = file_mtime + int(data["expires_in"])
                    data["expires_in"] = int(max(implied_expiry - time.time(), 0))
                except (TypeError, ValueError):
                    pass
        try:
            return OAuthToken.model_validate(data)
        except (ValueError, TypeError, KeyError) as exc:
            # Pydantic's error text echoes offending *values* — only log the
            # error type + count, never the message.
            logger.warning(
                "mcp oauth '%s': corrupt tokens at %s -- ignoring (%s)",
                self.server_name, self.tokens_path(), type(exc).__name__,
            )
            return None

    async def set_tokens(self, tokens: "OAuthToken") -> None:
        payload = tokens.model_dump(mode="json", exclude_none=True)
        # Persist an absolute ``expires_at`` so a process restart can
        # reconstruct the correct remaining TTL. Without this the MCP SDK's
        # ``_initialize`` reloads a relative ``expires_in`` which has no
        # wall-clock reference, leaving ``context.token_expiry_time=None``
        # and ``is_token_valid()`` falsely reporting True. Mirrors Claude
        # Code's ``OAuthTokens.expiresAt`` persistence (auth.ts ~180).
        expires_in = payload.get("expires_in")
        if expires_in is not None:
            try:
                payload["expires_at"] = time.time() + int(expires_in)
            except (TypeError, ValueError):
                pass
        write_json(self.tokens_path(), payload)
        logger.debug("mcp oauth '%s': tokens saved", self.server_name)

    def expires_at(self) -> float | None:
        """Absolute expiry of the stored access token (None if unknown)."""
        data = read_json(self.tokens_path())
        if not data:
            return None
        value = data.get("expires_at")
        try:
            return float(value) if value is not None else None
        except (TypeError, ValueError):
            return None

    def has_cached_tokens(self) -> bool:
        """Return True if we have tokens on disk (may be expired)."""
        return self.tokens_path().exists()

    # -- client info -------------------------------------------------------

    async def get_client_info(self) -> "OAuthClientInformationFull | None":
        data = read_json(self.client_info_path())
        if data is None:
            return None
        from mcp.shared.auth import OAuthClientInformationFull

        try:
            info = OAuthClientInformationFull.model_validate(data)
            # Some dynamic registration providers (notably Supabase MCP)
            # return a client_secret but omit token_endpoint_auth_method.
            # The MCP SDK defaults that missing field to "none", which
            # causes token exchange to omit client_secret and fail with
            # "Required parameter: client_secret". If a secret is present,
            # use client_secret_post unless the provider explicitly saved a
            # different method.
            if info.client_secret and data.get("token_endpoint_auth_method") in (None, "none", ""):
                data["token_endpoint_auth_method"] = "client_secret_post"
                info = OAuthClientInformationFull.model_validate(data)
                write_json(self.client_info_path(), info.model_dump(mode="json", exclude_none=True))
            return info
        except (ValueError, TypeError, KeyError) as exc:
            logger.warning(
                "mcp oauth '%s': corrupt client info at %s -- ignoring (%s)",
                self.server_name, self.client_info_path(), type(exc).__name__,
            )
            return None

    async def set_client_info(self, client_info: "OAuthClientInformationFull") -> None:
        data = client_info.model_dump(mode="json", exclude_none=True)
        # Supabase MCP dynamic client registration returns a client_secret
        # but omits token_endpoint_auth_method. Persist the effective method
        # immediately so this flow and subsequent retries use
        # client_secret_post.
        method = data.get("token_endpoint_auth_method")
        if data.get("client_secret") and method in (None, "none", ""):
            data["token_endpoint_auth_method"] = "client_secret_post"
        write_json(self.client_info_path(), data)
        logger.debug("mcp oauth '%s': client info saved", self.server_name)

    def read_client_info_raw(self) -> dict | None:
        """The on-disk client.json as a dict (no model validation)."""
        return read_json(self.client_info_path())

    def has_cached_client_info(self) -> bool:
        return self.read_client_info_raw() is not None

    # -- oauth server metadata --------------------------------------------
    # The MCP SDK keeps discovered ``OAuthMetadata`` (token endpoint URL,
    # etc.) in memory only. Persisting it here lets a restarted process
    # refresh tokens without re-running metadata discovery. Without this,
    # cold-start refresh requests fall back to the SDK's guessed
    # ``{server_url}/token`` which returns 404 on most real providers and
    # forces a full browser re-authorization.

    def save_oauth_metadata(self, metadata: "OAuthMetadata") -> None:
        write_json(self.meta_path(), metadata.model_dump(exclude_none=True, mode="json"))
        logger.debug("mcp oauth '%s': AS metadata saved", self.server_name)

    def load_oauth_metadata(self) -> "OAuthMetadata | None":
        data = read_json(self.meta_path())
        if data is None:
            return None
        from mcp.shared.auth import OAuthMetadata

        try:
            return OAuthMetadata.model_validate(data)
        except (ValueError, TypeError, KeyError) as exc:
            logger.warning(
                "mcp oauth '%s': corrupt AS metadata at %s -- ignoring (%s)",
                self.server_name, self.meta_path(), type(exc).__name__,
            )
            return None

    # -- CIMD refusal ------------------------------------------------------

    def mark_cimd_rejected(self) -> None:
        """Record that this server refused our Client ID Metadata Document.

        Without a durable marker the in-memory fallback only holds for the
        current process, so every restart re-presents a client_id the server
        has already fetched and refused. Cleared by ``remove()``, i.e. by
        ``/mcp login`` / ``/mcp remove``, so a fixed document gets another
        chance.
        """
        path = self.cimd_rejected_path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            secure_dir(path.parent)
            path.touch()
        except OSError as exc:  # non-fatal — worst case we retry CIMD later
            logger.debug("mcp oauth '%s': could not record CIMD rejection at %s: %s",
                         self.server_name, path, exc)

    def cimd_rejected(self) -> bool:
        """True when this server has refused our metadata document before."""
        return self.cimd_rejected_path().exists()

    # -- cleanup -----------------------------------------------------------

    def _state_paths(self) -> tuple[Path, ...]:
        return (
            self.tokens_path(),
            self.client_info_path(),
            self.meta_path(),
            self.cimd_rejected_path(),
        )

    def remove(self) -> None:
        """Delete all stored OAuth state for this server (incl. the .bak)."""
        for p in self._state_paths():
            p.unlink(missing_ok=True)
        backup = self.client_info_path().with_name(CLIENT_FILE + ".bak")
        backup.unlink(missing_ok=True)
        try:
            self.server_dir.rmdir()
        except OSError:
            pass

    def snapshot(self) -> dict[str, bytes]:
        """Capture on-disk OAuth state so a failed re-auth can restore it.

        Maps filename -> bytes for whichever of the three state files exist.
        Feed back to ``restore()`` to undo an intervening ``remove()`` when a
        re-authentication attempt fails, so a still-valid token isn't destroyed.
        """
        snap: dict[str, bytes] = {}
        for p in (self.tokens_path(), self.client_info_path(), self.meta_path()):
            try:
                snap[p.name] = p.read_bytes()
            except OSError:
                pass
        return snap

    def restore(self, snapshot: dict[str, bytes], *, only_if_absent: bool = False) -> None:
        """Revert to a snapshot without overwriting a concurrent successful write.

        ``only_if_absent`` looks at ``tokens.json`` only (deviation from
        hermes, which checked all three files): a failed re-auth attempt
        typically leaves a freshly *registered* ``client.json`` behind
        without ever minting a token, and treating that as "newer state"
        would throw away a still-valid credential. Only a new token counts
        as a completed login worth protecting.
        """
        if only_if_absent and self.tokens_path().exists():
            logger.info(
                "mcp oauth '%s': skipping rollback because newer tokens exist",
                self.server_name,
            )
            return
        self.remove()
        if not snapshot:
            return
        self.server_dir.mkdir(parents=True, exist_ok=True)
        secure_dir(self.server_dir)
        for fname, data in snapshot.items():
            if fname not in (TOKENS_FILE, CLIENT_FILE, META_FILE):
                continue
            path = self.server_dir / fname
            try:
                fd = os.open(
                    str(path),
                    os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
                    stat.S_IRUSR | stat.S_IWUSR,
                )
                with os.fdopen(fd, "wb") as fh:
                    fh.write(data)
            except OSError as exc:
                logger.warning("mcp oauth '%s': failed to restore %s: %s",
                               self.server_name, fname, exc)

    def poison_client_registration(self) -> bool:
        """Discard a dead dynamically-registered client so it gets re-created.

        Called when the IdP rejects our cached ``client_id`` with
        ``invalid_client`` on the token endpoint — proof the server-side
        registration is gone (IdP redeploy / DB wipe / rebrand). Deleting
        ``client.json`` makes the MCP SDK's ``async_auth_flow`` take the
        ``if not client_info`` branch and re-run RFC 7591 dynamic client
        registration on the next flow. The stale ``meta.json`` is dropped
        too so discovery re-runs against a freshly fetched document.

        Tokens are intentionally left in place — the subsequent
        re-authorization overwrites them, and keeping them avoids losing a
        still-valid refresh token if the re-registration never completes.

        A single ``.bak`` copy of the client file is kept for recovery.
        Returns True if a client file was present and removed.
        """
        client_path = self.client_info_path()
        if not client_path.exists():
            return False
        backup = client_path.with_name(client_path.name + ".bak")
        try:
            fd = os.open(
                str(backup),
                os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
                stat.S_IRUSR | stat.S_IWUSR,
            )
            with os.fdopen(fd, "wb") as fh:
                fh.write(client_path.read_bytes())
        except OSError as exc:  # non-fatal — proceed with the removal anyway
            logger.warning("mcp oauth '%s': could not back up client info: %s",
                           self.server_name, exc)
        client_path.unlink(missing_ok=True)
        self.meta_path().unlink(missing_ok=True)
        logger.warning(
            "mcp oauth '%s': cached client registration rejected as invalid_client; "
            "removed client.json + meta.json (backup at %s) to force re-registration",
            self.server_name, backup.name,
        )
        return True

    def invalidate_on_client_change(
        self,
        new_client_id: str,
        new_client_secret: str | None,
    ) -> bool:
        """Drop cached tokens when the configured OAuth client identity changes.

        Tokens are minted for a specific ``client_id``: after the user edits
        ``oauth.client_id`` / ``oauth.client_secret`` in mcp.json (or
        switches from dynamic registration to a pre-registered client), the
        old tokens are unusable — the token endpoint rejects their refresh
        with ``invalid_client``. Pre-registered clients are deliberately
        exempt from the ``invalid_client`` auto-poison path (config-supplied
        identity can't be healed by re-registration), so without this check
        the stale tokens wedge every request until the user manually wipes
        ``~/.freyja/mcp-tokens/<server>/``.

        Compares the on-disk ``client.json`` identity against the incoming
        config identity BEFORE the new client info overwrites it. Matching
        identity is a no-op so live sessions and valid tokens are preserved.
        Port of cline/cline#12983's "invalidate tokens when OAuth client
        changes" invariant. Returns True if anything was removed.
        """
        existing = self.read_client_info_raw()
        if not isinstance(existing, dict):
            return False
        old_client_id = existing.get("client_id")
        if not old_client_id:
            return False
        old_client_secret = existing.get("client_secret") or None
        if old_client_id == new_client_id and old_client_secret == (new_client_secret or None):
            return False
        removed = False
        for path in (self.tokens_path(), self.meta_path()):
            try:
                if path.exists():
                    path.unlink()
                    removed = True
            except OSError as exc:  # non-fatal — stale tokens fail later anyway
                logger.warning(
                    "mcp oauth '%s': could not remove stale %s after client change: %s",
                    self.server_name, path.name, exc,
                )
        if removed:
            # client_id is a public identifier; the secret is never logged.
            logger.warning(
                "mcp oauth '%s': configured OAuth client changed (client_id %r -> %r); "
                "discarded tokens minted under the previous client. "
                "Re-authorize with: /mcp login %s",
                self.server_name, old_client_id, new_client_id, self.server_name,
            )
        return removed


# ---------------------------------------------------------------------------
# Root-level helpers
# ---------------------------------------------------------------------------


def list_servers(root: str | Path | None = None) -> list[str]:
    """Sanitized names of every server with any OAuth state under *root*."""
    base = Path(root) if root is not None else default_token_root()
    if not base.is_dir():
        return []
    names: list[str] = []
    for child in sorted(base.iterdir()):
        state_files = (TOKENS_FILE, CLIENT_FILE, META_FILE, CIMD_OFF_FILE)
        if child.is_dir() and any((child / fname).exists() for fname in state_files):
            names.append(child.name)
    return names


def clear(server_name: str, root: str | Path | None = None) -> None:
    """Delete stored OAuth tokens, client info, metadata and markers for a server."""
    FreyjaTokenStorage(server_name, root=root).remove()
    logger.info("mcp oauth '%s': stored OAuth state removed", server_name)


def iter_secret_values(*payloads: dict | None) -> Iterable[str]:
    """Yield every secret-shaped string value in the given dicts (test helper
    for "no secrets in logs" assertions)."""
    for payload in payloads:
        if not payload:
            continue
        for key, value in payload.items():
            if _is_secret_key(str(key)) and isinstance(value, str) and value:
                yield value


__all__ = [
    "CIMD_OFF_FILE",
    "CLIENT_FILE",
    "META_FILE",
    "TOKENS_FILE",
    "TOKEN_DIR_NAME",
    "FreyjaTokenStorage",
    "clear",
    "default_token_root",
    "iter_secret_values",
    "list_servers",
    "read_json",
    "redact_payload",
    "redact_secret",
    "safe_filename",
    "secure_dir",
    "write_json",
]
