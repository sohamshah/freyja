"""MCP server catalog: parse/validate/save ~/.freyja/mcp.json.

Design doc section 2. The catalog is a standalone JSON file (never
gateway.yaml) so the desktop bridge and gateway daemon read the same
servers. ``${VAR}`` / ``${VAR:-default}`` references are stored verbatim
and expanded at CONNECT time (connection.py), never at load time, so
``.env`` edits take effect on reconnect.

Security posture (design doc section 8):
- config never holds secret values: inline secret-shaped strings are
  rejected by validation and the operator is pointed at ~/.freyja/.env;
- stdio commands are exec'd from an argv array, never shell-parsed, and
  shell-egress-shaped entries (``sh -c``, pipes, redirects) are rejected.
"""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

VALID_TRANSPORTS = ("stdio", "http", "sse")
VALID_TRUST = ("trusted", "standard", "untrusted")
VALID_TIERS = ("hot", "warm", "cold")

DEFAULT_CONNECT_TIMEOUT_S = 30.0
DEFAULT_CALL_TIMEOUT_S = 120.0
# Hard cap on a single tool result (chars) applied BEFORE any budget layer.
DEFAULT_MAX_RESULT_CHARS = 100_000
# Schema cache TTL hint: 0 = refresh only via tools/list_changed.
DEFAULT_SCHEMA_TTL_S = 0.0

# Keys we understand on a server entry. Anything else is preserved
# verbatim in ``extra`` and written back on save (forward compat).
_KNOWN_SERVER_KEYS = frozenset({
    "transport", "command", "args", "env", "url", "headers",
    "enabled", "scope", "trust", "tier", "tools", "timeouts",
    "auth", "oauth", "source", "limits",
})

_KNOWN_TOOLS_KEYS = frozenset({
    "include", "exclude", "permissions", "schema_ttl_s", "allow_quarantined",
})
_KNOWN_LIMITS_KEYS = frozenset({"max_result_chars"})


class McpConfigError(ValueError):
    """A server entry failed validation."""


# Claude Code plugin `.mcp.json` files spell the oauth block in camelCase
# ({"clientId": ..., "callbackPort": 3118}); Freyja's schema is snake_case.
_OAUTH_KEY_ALIASES = {
    "clientId": "client_id",
    "clientSecret": "client_secret",
    "callbackPort": "redirect_port",
    "redirectPort": "redirect_port",
    "redirectUri": "redirect_uri",
    "redirectHost": "redirect_host",
    "clientName": "client_name",
    "clientMetadataUrl": "client_metadata_url",
    "tokenEndpointAuthMethod": "token_endpoint_auth_method",
    "userAgent": "user_agent",
    "timeoutS": "timeout_s",
}


def normalize_oauth_block(raw: Any) -> Any:
    if not isinstance(raw, dict):
        return raw
    out: dict[str, Any] = {}
    for key, value in raw.items():
        canonical = _OAUTH_KEY_ALIASES.get(str(key), str(key))
        if canonical == "scopes" and isinstance(value, (list, tuple)):
            canonical, value = "scope", " ".join(str(s) for s in value)
        out[canonical] = value
    # A pre-registered client from a Claude Code plugin was registered with
    # Claude Code's redirect URI shape, http://localhost:<port>/callback.
    if "callbackPort" in raw and "redirect_host" not in out and "redirect_uri" not in out:
        out["redirect_host"] = "localhost"
    return out


def _infer_auth(raw: dict[str, Any]) -> Any:
    if raw.get("auth") is not None:
        return raw.get("auth")
    return "oauth" if raw.get("oauth") else None


# ---------------------------------------------------------------------------
# Secret heuristics (mirrors env_summary_redacted() in
# bridge/gateway/setup/env_writer.py — TOKEN/KEY/SECRET/PASSWORD).
# ---------------------------------------------------------------------------

_SECRET_NEEDLES = ("TOKEN", "KEY", "SECRET", "PASSWORD")


def is_secret_key(key: str) -> bool:
    """True if an env/header name looks like it holds a credential."""
    upper = key.upper()
    return any(needle in upper for needle in _SECRET_NEEDLES)


# ${VAR} or ${VAR:-default}
_ENV_REF_RE = re.compile(r"\$\{(?P<name>[A-Za-z_][A-Za-z0-9_]*)(?::-(?P<default>[^}]*))?\}")


def is_env_reference(value: str) -> bool:
    """True if the string is purely a ``${VAR}`` style reference (possibly
    with a default), i.e. contains no literal secret material."""
    return bool(_ENV_REF_RE.fullmatch(value.strip()))


def looks_like_inline_secret(key: str, value: str) -> bool:
    """A literal (non-${VAR}) value assigned to a secret-shaped key."""
    if not is_secret_key(key):
        return False
    if not value:
        return False
    # A value that *contains* env references is treated as a reference,
    # not an inline secret ("Bearer ${SLACK_TOKEN}" is fine).
    if _ENV_REF_RE.search(value):
        return False
    return True


# ---------------------------------------------------------------------------
# ${VAR} expansion — resolved at CONNECT time against os.environ.
# ---------------------------------------------------------------------------

@dataclass
class ExpandResult:
    """Result of expanding env references in one string."""

    text: str
    missing: list[str] = field(default_factory=list)
    """Referenced vars that were unset and had no default."""


def expand_env_refs(value: str, environ: dict[str, str] | None = None) -> ExpandResult:
    """Expand ``${VAR}`` and ``${VAR:-default}`` against *environ*
    (default ``os.environ``). Unset vars without a default expand to the
    empty string and are reported in ``missing`` so the caller can decide
    whether that means needs-auth (secret-shaped name) or a warning."""
    env = os.environ if environ is None else environ
    missing: list[str] = []

    def _sub(match: re.Match[str]) -> str:
        name = match.group("name")
        default = match.group("default")
        current = env.get(name)
        if current is not None:
            return current
        if default is not None:
            return default
        missing.append(name)
        return ""

    return ExpandResult(text=_ENV_REF_RE.sub(_sub, value), missing=missing)


# ---------------------------------------------------------------------------
# Shell-egress detection for stdio commands.
# ---------------------------------------------------------------------------

_SHELL_BINARIES = frozenset({"sh", "bash", "zsh", "dash", "ksh", "csh", "fish"})
# Metacharacters that only make sense under a shell. The command array is
# exec'd, never shell-parsed, so their presence means the entry is either
# broken or trying to smuggle a pipeline/redirect through us.
_SHELL_METACHAR_RE = re.compile(r"[|<>;`]|&&|\|\||\$\(")


def _shell_egress_reason(command: str, args: list[str]) -> str | None:
    """Return a human-readable rejection reason, or None if clean."""
    basename = Path(command).name
    if basename in _SHELL_BINARIES and any(a == "-c" for a in args):
        return (
            f"'{basename} -c' shell execution is not allowed; "
            "list the program and its args directly"
        )
    for token in [command, *args]:
        if _SHELL_METACHAR_RE.search(token):
            return (
                f"shell metacharacter in {token!r}: the command array is exec'd, "
                "never shell-parsed — pipes/redirects/substitution are rejected"
            )
    return None


# ---------------------------------------------------------------------------
# Spec
# ---------------------------------------------------------------------------

@dataclass
class McpServerSpec:
    """One server entry from mcp.json. Values are stored RAW (with
    ``${VAR}`` references intact); expansion happens at connect time."""

    name: str
    transport: str = "stdio"
    # stdio transport
    command: str | None = None
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    # http / sse transports. ``headers`` values may hold ${VAR} references
    # (expanded at connect time); literal secret-shaped values are rejected.
    url: str | None = None
    headers: dict[str, str] = field(default_factory=dict)
    # policy
    enabled: bool = True
    scope: str = "user"
    trust: str = "standard"
    tier: str = "warm"
    tools_include: list[str] = field(default_factory=lambda: ["*"])
    tools_exclude: list[str] = field(default_factory=list)
    # tools.allow_quarantined: remote tool names the operator reviewed and
    # approved despite matching the description injection scan.
    tools_allow_quarantined: list[str] = field(default_factory=list)
    tool_permissions: dict[str, str] = field(default_factory=dict)
    connect_timeout_s: float = DEFAULT_CONNECT_TIMEOUT_S
    call_timeout_s: float = DEFAULT_CALL_TIMEOUT_S
    # limits block
    max_result_chars: int = DEFAULT_MAX_RESULT_CHARS
    # tools.schema_ttl_s: periodic diff refresh of the tool list (0 = only
    # when the server sends tools/list_changed)
    schema_ttl_s: float = DEFAULT_SCHEMA_TTL_S
    # passthrough blocks
    auth: Any = None
    oauth: dict[str, Any] | None = None
    source: dict[str, Any] | None = None
    # unknown keys preserved verbatim for rewrite
    extra: dict[str, Any] = field(default_factory=dict)
    # extra keys inside the "tools" block
    tools_extra: dict[str, Any] = field(default_factory=dict)
    # extra keys inside the "limits" block
    limits_extra: dict[str, Any] = field(default_factory=dict)

    # -- parse / serialize --------------------------------------------------

    @classmethod
    def from_dict(cls, name: str, raw: dict[str, Any]) -> "McpServerSpec":
        tools_block = raw.get("tools") or {}
        timeouts = raw.get("timeouts") or {}
        limits = raw.get("limits") or {}
        if not isinstance(limits, dict):
            raise McpConfigError(f"server '{name}': 'limits' must be an object")
        spec = cls(
            name=name,
            transport=str(raw.get("transport", "stdio")),
            command=raw.get("command"),
            args=[str(a) for a in (raw.get("args") or [])],
            env={str(k): str(v) for k, v in (raw.get("env") or {}).items()},
            url=raw.get("url"),
            headers={str(k): str(v) for k, v in (raw.get("headers") or {}).items()},
            enabled=bool(raw.get("enabled", True)),
            scope=str(raw.get("scope", "user")),
            trust=str(raw.get("trust", "standard")),
            tier=str(raw.get("tier", "warm")),
            tools_include=[str(p) for p in (tools_block.get("include") or ["*"])],
            tools_exclude=[str(p) for p in (tools_block.get("exclude") or [])],
            tools_allow_quarantined=[str(p) for p in (tools_block.get("allow_quarantined") or [])],
            tool_permissions={
                str(k): str(v)
                for k, v in (tools_block.get("permissions") or {}).items()
            },
            connect_timeout_s=float(timeouts.get("connect_s", DEFAULT_CONNECT_TIMEOUT_S)),
            call_timeout_s=float(timeouts.get("call_s", DEFAULT_CALL_TIMEOUT_S)),
            max_result_chars=int(limits.get("max_result_chars", DEFAULT_MAX_RESULT_CHARS)),
            schema_ttl_s=float(tools_block.get("schema_ttl_s", DEFAULT_SCHEMA_TTL_S)),
            auth=_infer_auth(raw),
            oauth=normalize_oauth_block(raw.get("oauth")),
            source=raw.get("source"),
            extra={k: v for k, v in raw.items() if k not in _KNOWN_SERVER_KEYS},
            tools_extra={
                k: v for k, v in tools_block.items() if k not in _KNOWN_TOOLS_KEYS
            },
            limits_extra={k: v for k, v in limits.items() if k not in _KNOWN_LIMITS_KEYS},
        )
        return spec

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"transport": self.transport}
        if self.transport == "stdio":
            if self.command is not None:
                out["command"] = self.command
            if self.args:
                out["args"] = list(self.args)
            if self.env:
                out["env"] = dict(self.env)
        else:
            if self.url is not None:
                out["url"] = self.url
            if self.headers:
                out["headers"] = dict(self.headers)
        out["enabled"] = self.enabled
        out["scope"] = self.scope
        out["trust"] = self.trust
        out["tier"] = self.tier
        tools_block: dict[str, Any] = {}
        if self.tools_include != ["*"]:
            tools_block["include"] = list(self.tools_include)
        if self.tools_exclude:
            tools_block["exclude"] = list(self.tools_exclude)
        if self.tools_allow_quarantined:
            tools_block["allow_quarantined"] = list(self.tools_allow_quarantined)
        if self.tool_permissions:
            tools_block["permissions"] = dict(self.tool_permissions)
        if self.schema_ttl_s != DEFAULT_SCHEMA_TTL_S:
            tools_block["schema_ttl_s"] = self.schema_ttl_s
        tools_block.update(self.tools_extra)
        if tools_block:
            out["tools"] = tools_block
        if (
            self.connect_timeout_s != DEFAULT_CONNECT_TIMEOUT_S
            or self.call_timeout_s != DEFAULT_CALL_TIMEOUT_S
        ):
            out["timeouts"] = {
                "connect_s": self.connect_timeout_s,
                "call_s": self.call_timeout_s,
            }
        limits_block: dict[str, Any] = {}
        if self.max_result_chars != DEFAULT_MAX_RESULT_CHARS:
            limits_block["max_result_chars"] = self.max_result_chars
        limits_block.update(self.limits_extra)
        if limits_block:
            out["limits"] = limits_block
        if self.auth is not None:
            out["auth"] = self.auth
        if self.oauth is not None:
            out["oauth"] = self.oauth
        if self.source is not None:
            out["source"] = self.source
        out.update(self.extra)
        return out

    # -- validation ----------------------------------------------------------

    def validate(self) -> None:
        """Raise McpConfigError if the entry is unsafe or malformed."""
        if self.transport not in VALID_TRANSPORTS:
            raise McpConfigError(
                f"server '{self.name}': unknown transport {self.transport!r} "
                f"(expected one of {VALID_TRANSPORTS})"
            )
        if self.trust not in VALID_TRUST:
            raise McpConfigError(
                f"server '{self.name}': unknown trust {self.trust!r} "
                f"(expected one of {VALID_TRUST})"
            )
        if self.tier not in VALID_TIERS:
            raise McpConfigError(
                f"server '{self.name}': unknown tier {self.tier!r} "
                f"(expected one of {VALID_TIERS})"
            )
        if self.transport == "stdio":
            if not self.command:
                raise McpConfigError(f"server '{self.name}': stdio transport requires 'command'")
            reason = _shell_egress_reason(self.command, self.args)
            if reason is not None:
                raise McpConfigError(f"server '{self.name}': {reason}")
        else:
            if not self.url:
                raise McpConfigError(
                    f"server '{self.name}': {self.transport} transport requires 'url'"
                )
        if self.max_result_chars < 1:
            raise McpConfigError(
                f"server '{self.name}': limits.max_result_chars must be a positive integer"
            )
        if self.schema_ttl_s < 0:
            raise McpConfigError(f"server '{self.name}': tools.schema_ttl_s must be >= 0")
        # Inline secrets: env values and header values assigned to
        # secret-shaped keys must be ${VAR} references, never literals.
        for block_name, block in (("env", self.env), ("headers", self.headers)):
            for key, value in block.items():
                if looks_like_inline_secret(key, value):
                    raise McpConfigError(
                        f"server '{self.name}': {block_name}.{key} looks like an inline "
                        f"secret; put the value in ~/.freyja/.env and reference it as "
                        f"${{{key}}} instead"
                    )


# ---------------------------------------------------------------------------
# Catalog
# ---------------------------------------------------------------------------

@dataclass
class McpCatalog:
    """Parsed mcp.json: server specs + top-level passthrough."""

    specs: dict[str, McpServerSpec] = field(default_factory=dict)
    version: int = 1
    extra: dict[str, Any] = field(default_factory=dict)
    """Unknown top-level keys, preserved on save."""

    def to_dict(self) -> dict[str, Any]:
        doc: dict[str, Any] = {"version": self.version}
        doc.update(self.extra)
        doc["servers"] = {name: spec.to_dict() for name, spec in self.specs.items()}
        return doc


def _parse_document(doc: dict[str, Any], *, origin: str) -> McpCatalog:
    # "servers" is the native key; "mcpServers" is accepted as an alias
    # because it is the ecosystem-standard spelling (Claude Code, plugin
    # .mcp.json files, most vendor READMEs) and a silent zero-server load
    # from a pasted standard snippet is a nasty trap. Native key wins if
    # both are present. save_catalog always writes "servers".
    servers_raw = doc.get("servers") or doc.get("mcpServers") or {}
    catalog = McpCatalog(
        version=int(doc.get("version", 1)),
        extra={k: v for k, v in doc.items() if k not in ("version", "servers", "mcpServers")},
    )
    for name, raw in servers_raw.items():
        if not isinstance(raw, dict):
            logger.warning("mcp catalog %s: server '%s' is not an object; skipped", origin, name)
            continue
        try:
            spec = McpServerSpec.from_dict(str(name), raw)
            spec.validate()
        except McpConfigError as exc:
            # A bad entry never takes the bridge down; it is skipped with
            # a log line pointing at the exact problem.
            logger.warning("mcp catalog %s: rejected server '%s': %s", origin, name, exc)
            continue
        except Exception as exc:  # noqa: BLE001
            logger.warning("mcp catalog %s: failed to parse server '%s': %s", origin, name, exc)
            continue
        catalog.specs[spec.name] = spec
    return catalog


def load_catalog(
    user_path: Path | str,
    workspace: Path | str | None = None,
    *,
    include_project: bool = False,
) -> McpCatalog:
    """Load the user catalog, optionally overlaying the project catalog.

    A missing file is a silent no-op (empty catalog): the live bridge must
    behave exactly as before when the operator has no mcp.json. Malformed
    JSON logs a warning and yields an empty catalog rather than raising.

    Project scope (<workspace>/.freyja/mcp.json) wins on name collision but
    is only consulted when ``include_project=True`` — default False for v0
    (the one-time project-trust confirmation lands in v1).
    """
    catalog = McpCatalog()
    user_path = Path(user_path).expanduser()
    if user_path.is_file():
        try:
            doc = json.loads(user_path.read_text(encoding="utf-8"))
            if isinstance(doc, dict):
                catalog = _parse_document(doc, origin=str(user_path))
            else:
                logger.warning("mcp catalog %s: top level is not an object; ignored", user_path)
        except Exception as exc:  # noqa: BLE001
            logger.warning("mcp catalog %s: unreadable (%s); ignored", user_path, exc)

    if include_project and workspace is not None:
        project_path = Path(workspace).expanduser() / ".freyja" / "mcp.json"
        if project_path.is_file():
            try:
                doc = json.loads(project_path.read_text(encoding="utf-8"))
                if isinstance(doc, dict):
                    project = _parse_document(doc, origin=str(project_path))
                    for name, spec in project.specs.items():
                        spec.scope = "project"
                        catalog.specs[name] = spec  # project overrides user
            except Exception as exc:  # noqa: BLE001
                logger.warning("mcp catalog %s: unreadable (%s); ignored", project_path, exc)

    return catalog


def save_catalog(catalog: McpCatalog, path: Path | str) -> Path:
    """Atomically write the catalog (tmp file + rename, same pattern as
    save_env_values in bridge/gateway/setup/env_writer.py). Unknown keys
    captured at load time are preserved."""
    path = Path(path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(catalog.to_dict(), indent=2, ensure_ascii=False) + "\n"
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=".mcp-json-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(payload)
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
    return path
