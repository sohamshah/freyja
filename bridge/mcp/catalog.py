"""Curated MCP catalog: shipped ``mcp-catalog/<name>/manifest.yaml`` entries
plus per-user overrides, with list / info / search / install handlers.

Modeled on hermes-agent's ``hermes_cli/mcp_catalog.py`` + ``optional-mcps/``
(Nous Research, MIT). Schema, dataclass layout and the parse/validate
structure are adapted from there; the install path is rewritten against
Freyja's ``mcp.json`` (bridge/mcp/config.py) instead of hermes's
config.yaml. Attribution is kept in the manifests that were copied.

Catalog policy (same as hermes, adapted):
- Presence under ``mcp-catalog/`` = reviewed + approved. Entries are added
  by PR only; there is no community tier.
- Every shipped manifest records ``verified: YYYY-MM-DD`` — the date the
  package pin / remote URL was checked against the vendor. The lint
  rejects manifests without it.
- Supply-chain pins: ``npx -y pkg@X.Y.Z`` / ``uvx pkg==X.Y.Z`` / docker
  images with an explicit tag. Never ``@latest``, never a bare package.
  The lint rejects floating versions.
- Secrets are NEVER in manifests and never written to mcp.json. A
  manifest declares the env var *names* it needs (``auth.env``); install
  writes ``${VAR}`` references and reports which vars are unset in the
  environment (presence only — values are never read). The operator sets
  them in ``~/.freyja/.env``.
- Install is idempotent and preserves operator edits (``enabled``,
  ``trust``, ``tier``, ``tools``, ``timeouts``, extra ``env`` keys, OAuth
  state). Re-installing after a catalog update applies the new pin.

User overrides: ``~/.freyja/mcp-catalog/<name>/manifest.yaml`` (or
``<name>.yaml``) takes precedence over the shipped entry of the same name,
so an operator can bump a pin or point at a self-hosted URL without editing
the repo.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Iterable

import yaml

from bridge.mcp.config import (
    McpConfigError,
    McpServerSpec,
    is_secret_key,
    load_catalog,
    save_catalog,
)

logger = logging.getLogger(__name__)

MANIFEST_VERSION = 1
MANIFEST_FILENAME = "manifest.yaml"
CATALOG_DIRNAME = "mcp-catalog"

VALID_AUTH_TYPES = ("api_key", "oauth", "none")
VALID_TRANSPORT_TYPES = ("stdio", "http")

_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_ENV_REF_RE = re.compile(r"\$\{(?P<name>[A-Za-z_][A-Za-z0-9_]*)(?::-(?P<default>[^}]*))?\}")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_TAG_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")

# Literal credential shapes that must never appear anywhere in a manifest.
_SECRET_LITERAL_RE = re.compile(
    r"(xox[abprs]-[A-Za-z0-9-]{8,}"  # slack
    r"|gh[pousr]_[A-Za-z0-9]{20,}"  # github
    r"|github_pat_[A-Za-z0-9_]{20,}"
    r"|sk-[A-Za-z0-9_-]{16,}"  # openai-ish
    r"|sk_(?:live|test)_[A-Za-z0-9]{8,}"  # stripe
    r"|rk_(?:live|test)_[A-Za-z0-9]{8,}"
    r"|AKIA[0-9A-Z]{16}"  # aws access key id
    r"|lin_api_[A-Za-z0-9]{16,}"  # linear
    r"|ntn_[A-Za-z0-9]{20,}"  # notion
    r"|figd_[A-Za-z0-9_-]{16,}"  # figma
    r"|eyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{10,})"  # jwt
)

# Package-launcher commands whose first package argument must carry a pin.
_NPM_LAUNCHERS = frozenset({"npx", "bunx", "pnpx", "pnpm"})
_PY_LAUNCHERS = frozenset({"uvx", "pipx"})
_FLOATING_NPM_TAGS = frozenset(
    {"latest", "next", "canary", "beta", "alpha", "rc", "dev", "nightly", "*"}
)
_SEMVER_PIN_RE = re.compile(r"^\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?$")
_PY_PIN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*==[0-9][0-9A-Za-z.+!-]*$")
_DOCKER_TAG_RE = re.compile(
    r"^[^\s:@/]+(?:[:.][^\s:@/]+)*(?:/[^\s:@]+)*"
    r"(?::(?P<tag>[^\s@]+))?(?:@(?P<digest>sha256:[0-9a-f]{64}))?$"
)


class CatalogError(ValueError):
    """Manifest parse/validation failure or install error."""


# ─── Data classes ────────────────────────────────────────────────────────────


@dataclass
class EnvVarSpec:
    """One environment variable a server needs. ``secret`` vars are never
    stored anywhere but ``~/.freyja/.env``; the catalog only ever writes
    ``${NAME}`` references into mcp.json."""

    name: str
    description: str = ""
    required: bool = True
    secret: bool = True
    default: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "required": self.required,
            "secret": self.secret,
            "default": self.default,
        }


@dataclass
class AuthSpec:
    type: str = "none"  # api_key | oauth | none
    env: list[EnvVarSpec] = field(default_factory=list)
    # http + api_key: which header carries the credential and how the
    # value is formatted. ``header_format`` may reference declared env
    # vars as ${NAME}; default is ``Bearer ${<first secret env>}``.
    header: str = "Authorization"
    header_format: str = ""
    # oauth: hints merged into the mcp.json ``oauth`` block (client_name,
    # scope, callback_port, ...). Never credentials.
    oauth: dict[str, Any] = field(default_factory=dict)
    # third-party-provider oauth (hermes "case 2"); informational here.
    provider: str | None = None
    scopes: list[str] = field(default_factory=list)

    @property
    def secrets(self) -> list[EnvVarSpec]:
        return [e for e in self.env if e.secret]

    @property
    def required_env(self) -> list[EnvVarSpec]:
        return [e for e in self.env if e.required and not e.default]


@dataclass
class TransportSpec:
    type: str  # stdio | http
    command: str | None = None
    args: list[str] = field(default_factory=list)
    url: str | None = None
    # Static, NON-secret env for the stdio subprocess (telemetry opt-outs,
    # mode flags). Secret-shaped keys must be ${VAR} references.
    env: dict[str, str] = field(default_factory=dict)
    # Informational pin metadata (the lint checks args carry the pin).
    package: str | None = None
    version: str | None = None


@dataclass
class ToolsSpec:
    """Manifest-side default tool filter, written to mcp.json ``tools``
    on first install only (operator edits win on re-install)."""

    default_enabled: list[str] | None = None
    default_excluded: list[str] | None = None


@dataclass
class SuggestSpec:
    keywords: list[str] = field(default_factory=list)
    hosts: list[str] = field(default_factory=list)


@dataclass
class CatalogEntry:
    name: str
    description: str
    homepage: str
    transport: TransportSpec
    auth: AuthSpec = field(default_factory=AuthSpec)
    tools: ToolsSpec = field(default_factory=ToolsSpec)
    suggest: SuggestSpec | None = None
    tags: list[str] = field(default_factory=list)
    license: str = ""
    verified: str = ""
    post_install: str = ""
    manifest_path: Path = field(default_factory=Path)
    origin: str = "shipped"  # shipped | user

    @property
    def auth_kind(self) -> str:
        """oauth | api_key | env | none — ``env`` means no credential
        exchange but the server still needs non-auth env vars set."""
        if self.auth.type in ("oauth", "api_key"):
            return self.auth.type
        if self.auth.env:
            return "env"
        return "none"

    @property
    def catalog_ref(self) -> str:
        return f"{self.name}@{self.verified}" if self.verified else self.name


@dataclass
class CatalogDiagnostic:
    """A manifest that could not be loaded. Never fatal for the catalog."""

    name: str
    path: str
    kind: str  # invalid | future_manifest | duplicate
    message: str

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "path": self.path, "kind": self.kind, "message": self.message}


@dataclass
class LintIssue:
    path: str
    code: str
    message: str
    severity: str = "error"  # error | warning

    def __str__(self) -> str:
        return f"{self.path}: [{self.code}] {self.message}"


@dataclass
class InstallReport:
    """Outcome of :func:`catalog_install`. Secret VALUES never appear here."""

    name: str
    server_name: str
    mcp_json_path: str
    auth: str
    created: bool
    changed: bool
    enabled: bool
    missing_env: list[str] = field(default_factory=list)
    missing_optional_env: list[str] = field(default_factory=list)
    next_steps: list[str] = field(default_factory=list)
    post_install: str = ""
    server: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "server_name": self.server_name,
            "mcp_json_path": self.mcp_json_path,
            "auth": self.auth,
            "created": self.created,
            "changed": self.changed,
            "enabled": self.enabled,
            "missing_env": list(self.missing_env),
            "missing_optional_env": list(self.missing_optional_env),
            "next_steps": list(self.next_steps),
            "post_install": self.post_install,
            "server": dict(self.server),
        }


# ─── Paths ───────────────────────────────────────────────────────────────────


def shipped_catalog_dir() -> Path:
    """``<repo>/mcp-catalog`` (override with ``FREYJA_MCP_CATALOG_DIR``)."""
    override = os.environ.get("FREYJA_MCP_CATALOG_DIR")
    if override:
        return Path(override).expanduser()
    return Path(__file__).resolve().parents[2] / CATALOG_DIRNAME


def freyja_home() -> Path:
    return Path(os.environ.get("FREYJA_HOME") or os.path.expanduser("~/.freyja"))


def user_catalog_dir() -> Path:
    """``~/.freyja/mcp-catalog`` — per-user overrides/additions."""
    return freyja_home() / CATALOG_DIRNAME


def iter_manifest_paths(root: Path | str) -> list[Path]:
    """All manifests under *root*: ``<root>/<name>/manifest.yaml`` and the
    flat ``<root>/<name>.yaml`` form. Sorted for deterministic output."""
    root = Path(root)
    if not root.is_dir():
        return []
    found: list[Path] = []
    for child in sorted(root.iterdir()):
        if child.name.startswith("."):
            continue
        if child.is_dir():
            manifest = child / MANIFEST_FILENAME
            if manifest.is_file():
                found.append(manifest)
        elif child.suffix in (".yaml", ".yml") and child.is_file():
            found.append(child)
    return found


def expected_name_for(path: Path) -> str:
    """The catalog name a manifest at *path* must declare (dir name or
    file stem)."""
    path = Path(path)
    if path.name == MANIFEST_FILENAME:
        return path.parent.name
    return path.stem


# ─── Manifest parsing ────────────────────────────────────────────────────────


def _err(path: Path, msg: str) -> CatalogError:
    return CatalogError(f"{path}: {msg}")


def _parse_env_spec(path: Path, raw: Any) -> EnvVarSpec:
    if not isinstance(raw, dict):
        raise _err(path, f"auth.env entry must be a mapping, got {type(raw).__name__}")
    name = str(raw.get("name") or "")
    if not _ENV_NAME_RE.match(name):
        raise _err(path, f"invalid env var name: {name!r}")
    description = raw.get("description") or raw.get("prompt") or ""
    default = raw.get("default")
    default = "" if default is None else str(default)
    return EnvVarSpec(
        name=name,
        description=str(description).strip(),
        required=bool(raw.get("required", True)),
        secret=bool(raw.get("secret", True)),
        default=default,
    )


def _str_list(path: Path, value: Any, where: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise _err(path, f"{where} must be a list of strings")
    return list(value)


def parse_manifest(path: Path | str, *, origin: str = "shipped") -> CatalogEntry:
    """Read and validate one manifest.yaml. Raises CatalogError."""
    path = Path(path)
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception as exc:  # noqa: BLE001
        raise CatalogError(f"failed to read {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise _err(path, "manifest must be a mapping")

    mv = data.get("manifest_version")
    if mv is None:
        raise _err(path, f"missing manifest_version (expected {MANIFEST_VERSION})")
    if isinstance(mv, int) and not isinstance(mv, bool) and mv > MANIFEST_VERSION:
        raise _err(
            path,
            f"manifest_version {mv} is newer than this Freyja understands "
            f"(max {MANIFEST_VERSION}); update Freyja to use this entry",
        )
    if mv != MANIFEST_VERSION:
        raise _err(path, f"manifest_version {mv!r} unsupported (expected {MANIFEST_VERSION})")

    name = str(data.get("name") or "")
    if not _NAME_RE.match(name):
        raise _err(path, f"invalid or missing 'name' {name!r} (lowercase [a-z0-9_-])")

    description = str(data.get("description") or "").strip()
    if not description:
        raise _err(path, "'description' required")

    homepage = str(data.get("homepage") or data.get("source") or "").strip()

    # transport
    t_raw = data.get("transport") or {}
    if not isinstance(t_raw, dict):
        raise _err(path, "'transport' must be a mapping")
    t_type = t_raw.get("type")
    if t_type not in VALID_TRANSPORT_TYPES:
        raise _err(path, "transport.type must be 'stdio' or 'http'")
    args = t_raw.get("args") or []
    if not isinstance(args, list):
        raise _err(path, "transport.args must be a list")
    env_raw = t_raw.get("env") or {}
    if not isinstance(env_raw, dict) or not all(isinstance(k, str) for k in env_raw):
        raise _err(path, "transport.env must be a mapping of string to string")
    transport = TransportSpec(
        type=t_type,
        command=(str(t_raw["command"]) if t_raw.get("command") is not None else None),
        args=[str(a) for a in args],
        url=(str(t_raw["url"]).strip() if t_raw.get("url") is not None else None),
        env={str(k): str(v) for k, v in env_raw.items()},
        package=(str(t_raw["package"]) if t_raw.get("package") is not None else None),
        version=(str(t_raw["version"]) if t_raw.get("version") is not None else None),
    )
    if t_type == "stdio" and not transport.command:
        raise _err(path, "stdio transport requires 'command'")
    if t_type == "http" and not transport.url:
        raise _err(path, "http transport requires 'url'")
    if t_type == "http" and (transport.command or transport.args):
        raise _err(path, "http transport must not set command/args")
    if t_type == "stdio" and transport.url:
        raise _err(path, "stdio transport must not set url")

    # auth
    a_raw = data.get("auth") or {"type": "none"}
    if not isinstance(a_raw, dict):
        raise _err(path, "'auth' must be a mapping")
    a_type = str(a_raw.get("type") or "none")
    if a_type not in VALID_AUTH_TYPES:
        raise _err(path, "auth.type must be 'api_key'|'oauth'|'none'")
    env_list_raw = a_raw.get("env") or []
    if not isinstance(env_list_raw, list):
        raise _err(path, "auth.env must be a list")
    env_list = [_parse_env_spec(path, e) for e in env_list_raw]
    seen: set[str] = set()
    for e in env_list:
        if e.name in seen:
            raise _err(path, f"auth.env declares {e.name} twice")
        seen.add(e.name)
    oauth_hints = a_raw.get("oauth") or {}
    if not isinstance(oauth_hints, dict):
        raise _err(path, "auth.oauth must be a mapping")
    auth = AuthSpec(
        type=a_type,
        env=env_list,
        header=str(a_raw.get("header") or "Authorization"),
        header_format=str(a_raw.get("header_format") or ""),
        oauth=dict(oauth_hints),
        provider=(str(a_raw["provider"]) if a_raw.get("provider") else None),
        scopes=_str_list(path, a_raw.get("scopes"), "auth.scopes"),
    )
    if a_type == "oauth" and t_type != "http":
        raise _err(path, "auth.type oauth requires transport.type http")
    if a_type == "api_key" and not auth.secrets:
        raise _err(path, "auth.type api_key requires at least one secret entry in auth.env")
    if a_type != "oauth" and oauth_hints:
        raise _err(path, "auth.oauth hints only make sense with auth.type oauth")

    # tools
    tools_raw = data.get("tools") or {}
    if not isinstance(tools_raw, dict):
        raise _err(path, "'tools' must be a mapping")
    default_enabled = tools_raw.get("default_enabled")
    default_excluded = tools_raw.get("default_excluded")
    if default_enabled is not None:
        default_enabled = _str_list(path, default_enabled, "tools.default_enabled")
    if default_excluded is not None:
        default_excluded = _str_list(path, default_excluded, "tools.default_excluded")
    if default_enabled is not None and default_excluded is not None:
        raise _err(path, "tools.default_enabled and tools.default_excluded are mutually exclusive")
    tools = ToolsSpec(default_enabled=default_enabled, default_excluded=default_excluded)

    # suggest
    suggest: SuggestSpec | None = None
    s_raw = data.get("suggest")
    if s_raw is not None:
        if not isinstance(s_raw, dict):
            raise _err(path, "'suggest' must be a mapping")
        kws = _str_list(path, s_raw.get("keywords"), "suggest.keywords")
        hosts = _str_list(path, s_raw.get("hosts"), "suggest.hosts")
        if not kws and not hosts:
            raise _err(path, "'suggest' requires at least one keyword or host")
        suggest = SuggestSpec(
            keywords=[k.strip().lower() for k in kws if k.strip()],
            hosts=[h.strip().lower().lstrip(".") for h in hosts if h.strip()],
        )

    tags = _str_list(path, data.get("tags"), "tags")
    verified_raw = data.get("verified")
    if isinstance(verified_raw, date):
        verified = verified_raw.isoformat()
    else:
        verified = str(verified_raw).strip() if verified_raw is not None else ""

    return CatalogEntry(
        name=name,
        description=description,
        homepage=homepage,
        transport=transport,
        auth=auth,
        tools=tools,
        suggest=suggest,
        tags=tags,
        license=str(data.get("license") or "").strip(),
        verified=verified,
        post_install=str(data.get("post_install") or "").strip(),
        manifest_path=path,
        origin=origin,
    )


# ─── Loading ─────────────────────────────────────────────────────────────────

_LAST_DIAGNOSTICS: list[CatalogDiagnostic] = []


def catalog_diagnostics() -> list[CatalogDiagnostic]:
    """Diagnostics from the most recent :func:`load_catalog_dir` call."""
    return list(_LAST_DIAGNOSTICS)


def _load_root(
    root: Path, *, origin: str, into: dict[str, CatalogEntry], diagnostics: list[CatalogDiagnostic]
) -> None:
    for manifest in iter_manifest_paths(root):
        expected = expected_name_for(manifest)
        try:
            entry = parse_manifest(manifest, origin=origin)
        except CatalogError as exc:
            msg = str(exc)
            kind = "future_manifest" if "newer than this Freyja" in msg else "invalid"
            diagnostics.append(CatalogDiagnostic(expected, str(manifest), kind, msg))
            logger.warning("mcp catalog: skipped %s: %s", manifest, msg)
            continue
        if entry.name != expected:
            msg = f"manifest name {entry.name!r} does not match its location ({expected!r})"
            diagnostics.append(CatalogDiagnostic(expected, str(manifest), "invalid", msg))
            logger.warning("mcp catalog: skipped %s: %s", manifest, msg)
            continue
        if entry.name in into and into[entry.name].origin == origin:
            diagnostics.append(
                CatalogDiagnostic(
                    entry.name, str(manifest), "duplicate",
                    f"duplicate entry {entry.name!r} in {root}; "
                    f"keeping {into[entry.name].manifest_path}",
                )
            )
            continue
        into[entry.name] = entry


def load_catalog_dir(
    path: Path | str | None = None,
    *,
    user_dir: Path | str | None = None,
    include_user: bool = True,
    diagnostics: list[CatalogDiagnostic] | None = None,
) -> dict[str, CatalogEntry]:
    """Load the shipped catalog at *path* (default ``<repo>/mcp-catalog``)
    and overlay per-user entries from *user_dir* (default
    ``~/.freyja/mcp-catalog``; skipped when ``include_user=False``).

    A user entry with the same name as a shipped one REPLACES it. Broken
    manifests are skipped and reported via *diagnostics* /
    :func:`catalog_diagnostics`; they never take the catalog down.
    """
    diags: list[CatalogDiagnostic] = [] if diagnostics is None else diagnostics
    entries: dict[str, CatalogEntry] = {}
    root = shipped_catalog_dir() if path is None else Path(path).expanduser()
    _load_root(root, origin="shipped", into=entries, diagnostics=diags)
    if include_user:
        udir = user_catalog_dir() if user_dir is None else Path(user_dir).expanduser()
        same_dir = udir.exists() and root.exists() and udir.resolve() == root.resolve()
        if not same_dir:
            _load_root(udir, origin="user", into=entries, diagnostics=diags)
    _LAST_DIAGNOSTICS[:] = diags
    return dict(sorted(entries.items()))


def get_entry(name: str, entries: dict[str, CatalogEntry] | None = None) -> CatalogEntry | None:
    if entries is None:
        entries = load_catalog_dir()
    return entries.get(name.strip())


# ─── list / info / search ────────────────────────────────────────────────────


def _row(entry: CatalogEntry) -> dict[str, Any]:
    return {
        "name": entry.name,
        "description": entry.description,
        "transport": entry.transport.type,
        "auth": entry.auth_kind,
        "tags": list(entry.tags),
        "verified": entry.verified,
        "origin": entry.origin,
    }


def catalog_list(
    entries: dict[str, CatalogEntry] | None = None,
    *,
    tags: Iterable[str] | None = None,
) -> list[dict[str, Any]]:
    """Summary rows (name, description, transport, auth kind, tags), sorted
    by name. With *tags*, only entries carrying ALL of them."""
    if entries is None:
        entries = load_catalog_dir()
    wanted = {t.strip().lower() for t in (tags or []) if t.strip()}
    rows = []
    for name in sorted(entries):
        entry = entries[name]
        if wanted and not wanted.issubset({t.lower() for t in entry.tags}):
            continue
        rows.append(_row(entry))
    return rows


def catalog_info(
    name: str, entries: dict[str, CatalogEntry] | None = None
) -> dict[str, Any] | None:
    """Full detail for one entry: transport, required secrets/env, OAuth
    hints, default tool filter, setup notes, and a preview of the mcp.json
    entry install would write. None if unknown."""
    entry = get_entry(name, entries)
    if entry is None:
        return None
    info = _row(entry)
    info.update({
        "homepage": entry.homepage,
        "license": entry.license,
        "manifest_path": str(entry.manifest_path),
        "transport_detail": {
            "type": entry.transport.type,
            "url": entry.transport.url,
            "command": entry.transport.command,
            "args": list(entry.transport.args),
            "env": dict(entry.transport.env),
            "package": entry.transport.package,
            "version": entry.transport.version,
        },
        "auth_detail": {
            "type": entry.auth.type,
            "header": entry.auth.header if entry.auth.type == "api_key" else None,
            "oauth": dict(entry.auth.oauth),
            "provider": entry.auth.provider,
            "scopes": list(entry.auth.scopes),
        },
        "env": [e.to_dict() for e in entry.auth.env],
        "required_secrets": [e.name for e in entry.auth.secrets if e.required],
        "required_env": [e.name for e in entry.auth.required_env],
        "tools": {
            "default_enabled": entry.tools.default_enabled,
            "default_excluded": entry.tools.default_excluded,
        },
        "suggest": (
            {"keywords": list(entry.suggest.keywords), "hosts": list(entry.suggest.hosts)}
            if entry.suggest else None
        ),
        "setup_notes": entry.post_install,
        "server_preview": build_server_dict(entry),
    })
    return info


def catalog_search(
    query: str, entries: dict[str, CatalogEntry] | None = None
) -> list[dict[str, Any]]:
    """Case-insensitive substring match over name, description, tags,
    homepage and suggest keywords/hosts. Empty query → everything."""
    if entries is None:
        entries = load_catalog_dir()
    q = (query or "").strip().lower()
    if not q:
        return catalog_list(entries)
    terms = q.split()
    rows = []
    for name in sorted(entries):
        entry = entries[name]
        hay = [entry.name, entry.description, entry.homepage, *entry.tags]
        if entry.suggest:
            hay.extend(entry.suggest.keywords)
            hay.extend(entry.suggest.hosts)
        blob = " ".join(hay).lower()
        if all(term in blob for term in terms):
            rows.append(_row(entry))
    return rows


# ─── mcp.json entry construction ─────────────────────────────────────────────


def _env_ref(name: str) -> str:
    return "${" + name + "}"


def _header_value(entry: CatalogEntry) -> str:
    if entry.auth.header_format:
        return entry.auth.header_format
    secrets = entry.auth.secrets
    primary = secrets[0].name if secrets else (entry.auth.env[0].name if entry.auth.env else "")
    return f"Bearer {_env_ref(primary)}"


def build_server_dict(entry: CatalogEntry) -> dict[str, Any]:
    """Translate a manifest into an McpServerSpec-compatible dict.

    Env values are ALWAYS ``${VAR}`` references named per the manifest —
    never inline values. OAuth servers get ``auth: "oauth"`` plus an
    ``oauth`` block seeded with the manifest's hints. Trust is
    ``standard``; ``source.catalog`` records ``<name>@<verified>`` so a
    later ``/mcp catalog`` can tell which manifest revision an entry came
    from. ``enabled`` is left to the caller.
    """
    t = entry.transport
    out: dict[str, Any] = {"transport": t.type}
    if t.type == "stdio":
        out["command"] = t.command
        if t.args:
            out["args"] = list(t.args)
        env: dict[str, str] = dict(t.env)
        for spec in entry.auth.env:
            if spec.default:
                env[spec.name] = "${" + spec.name + ":-" + spec.default + "}"
            else:
                env[spec.name] = _env_ref(spec.name)
        if env:
            out["env"] = env
    else:
        out["url"] = t.url
        if entry.auth.type == "api_key":
            out["headers"] = {entry.auth.header: _header_value(entry)}
    if entry.auth.type == "oauth":
        out["auth"] = "oauth"
        out["oauth"] = dict(entry.auth.oauth)
    elif entry.auth.type == "api_key":
        out["auth"] = "api_key"
    out["trust"] = "standard"
    tools_block: dict[str, Any] = {}
    if entry.tools.default_enabled is not None:
        tools_block["include"] = list(entry.tools.default_enabled)
    if entry.tools.default_excluded is not None:
        tools_block["exclude"] = list(entry.tools.default_excluded)
    if tools_block:
        out["tools"] = tools_block
    out["source"] = {"catalog": entry.catalog_ref}
    return out


def _referenced_env(values: Iterable[str]) -> tuple[set[str], set[str]]:
    """(names without a default, names with a default) referenced as
    ${VAR} across *values*."""
    hard: set[str] = set()
    soft: set[str] = set()
    for value in values:
        for m in _ENV_REF_RE.finditer(value or ""):
            (soft if m.group("default") is not None else hard).add(m.group("name"))
    return hard, soft


def _spec_values(raw: dict[str, Any]) -> list[str]:
    values = [str(raw.get("command") or ""), str(raw.get("url") or "")]
    values.extend(str(a) for a in raw.get("args") or [])
    values.extend(str(v) for v in (raw.get("env") or {}).values())
    values.extend(str(v) for v in (raw.get("headers") or {}).values())
    return values


# manifest-owned keys: overwritten on every install so pin bumps apply.
_MANIFEST_OWNED = ("transport", "command", "args", "url", "auth", "source")
# operator-owned keys: never touched on re-install.
_OPERATOR_OWNED = ("enabled", "scope", "trust", "tier", "tools", "timeouts")


def _merge_existing(old: dict[str, Any], new: dict[str, Any], *, enable: bool) -> dict[str, Any]:
    merged = dict(old)
    for key in _MANIFEST_OWNED:
        if key in new:
            merged[key] = new[key]
        else:
            merged.pop(key, None)
    # env / headers: manifest keys win, operator-added keys survive.
    for block in ("env", "headers"):
        combined = dict(old.get(block) or {})
        combined.update(new.get(block) or {})
        if combined:
            merged[block] = combined
        else:
            merged.pop(block, None)
    # oauth: manifest hints fill gaps; anything the OAuth flow or the
    # operator already recorded (client_id, callback_port, ...) is kept.
    if "oauth" in new:
        merged["oauth"] = {**new["oauth"], **(old.get("oauth") or {})}
    else:
        merged.pop("oauth", None)
    # enabled/scope/trust/tier/tools/timeouts are operator-owned: the
    # manifest's defaults (tools filter, trust=standard) apply on FIRST
    # install only and are never re-applied — an operator who cleared the
    # exclude list or tightened trust keeps that on every re-install.
    for key in _OPERATOR_OWNED:
        if key in old:
            merged[key] = old[key]
        else:
            merged.pop(key, None)
    if enable:
        merged["enabled"] = True
    return merged


def catalog_install(
    name: str,
    *,
    mcp_json_path: Path | str,
    server_name: str | None = None,
    enable: bool = False,
    entries: dict[str, CatalogEntry] | None = None,
    catalog_dir: Path | str | None = None,
    user_dir: Path | str | None = None,
    environ: dict[str, str] | None = None,
) -> InstallReport:
    """Install catalog entry *name* into *mcp_json_path*.

    - Builds the server dict via :func:`build_server_dict` and merges it
      into mcp.json through ``config.load_catalog`` / ``save_catalog``.
    - Idempotent: a second install with the same manifest is a no-op
      (``changed=False``, file untouched) and operator edits to
      ``enabled``/``trust``/``tier``/``tools``/``timeouts`` survive.
    - Fresh installs are ``enabled=False`` unless ``enable=True`` — the
      operator (or ``/mcp enable``) flips it after secrets are in place.
    - Reports env vars the entry needs that are absent from *environ*
      (default ``os.environ``). Only presence is checked; values are
      never read or returned.
    """
    if entries is None:
        entries = load_catalog_dir(catalog_dir, user_dir=user_dir)
    entry = entries.get(name.strip())
    if entry is None:
        raise CatalogError(f"unknown catalog entry {name!r}")
    server_name = (server_name or entry.name).strip()
    if not server_name:
        raise CatalogError("server_name must not be empty")

    mcp_json_path = Path(mcp_json_path).expanduser()
    catalog = load_catalog(mcp_json_path)
    existing = catalog.specs.get(server_name)
    new_raw = build_server_dict(entry)

    if existing is None:
        merged = dict(new_raw)
        merged["enabled"] = bool(enable)
        created = True
        before: dict[str, Any] | None = None
    else:
        before = existing.to_dict()
        merged = _merge_existing(before, new_raw, enable=enable)
        created = False

    try:
        spec = McpServerSpec.from_dict(server_name, merged)
        spec.validate()
    except McpConfigError as exc:
        raise CatalogError(
            f"catalog entry {entry.name!r} produced an invalid mcp.json entry: {exc}"
        ) from exc

    after = spec.to_dict()
    changed = created or after != before
    if changed:
        catalog.specs[server_name] = spec
        save_catalog(catalog, mcp_json_path)

    # Missing env: declared requirements + every ${VAR} the entry references.
    env = os.environ if environ is None else environ
    declared_required = {e.name for e in entry.auth.required_env}
    declared_optional = {e.name for e in entry.auth.env} - declared_required
    hard_refs, _soft_refs = _referenced_env(_spec_values(after))
    # A ${VAR} the entry references is required unless the manifest
    # explicitly declared it optional (it expands to "" when unset).
    required = declared_required | (hard_refs - declared_optional)
    optional = declared_optional - required
    missing_env = sorted(v for v in required if v not in env)
    missing_optional = sorted(v for v in optional if v not in env)

    env_path = mcp_json_path.parent / ".env"
    steps: list[str] = []
    is_oauth = entry.auth.type == "oauth"
    if missing_env:
        steps.append(
            f"set {', '.join(missing_env)} in {env_path} then /mcp enable {server_name}"
        )
    if is_oauth:
        steps.append(f"/mcp login {server_name}")
        if not spec.enabled:
            steps.append(f"/mcp enable {server_name}")
    elif not missing_env:
        if not spec.enabled:
            steps.append(f"/mcp enable {server_name}")
        elif changed:
            steps.append("/mcp reload")
    if missing_optional:
        steps.append(
            f"optional: set {', '.join(missing_optional)} in {env_path} to customize {server_name}"
        )

    return InstallReport(
        name=entry.name,
        server_name=server_name,
        mcp_json_path=str(mcp_json_path),
        auth=entry.auth_kind,
        created=created,
        changed=changed,
        enabled=spec.enabled,
        missing_env=missing_env,
        missing_optional_env=missing_optional,
        next_steps=steps,
        post_install=entry.post_install,
        server=after,
    )


# ─── Lint ────────────────────────────────────────────────────────────────────


def _npm_package_pin_issue(spec: str) -> str | None:
    """None if ``pkg@X.Y.Z`` (scoped or not); else the reason."""
    if spec.startswith("@"):
        scope_sep = spec.find("/")
        if scope_sep < 0:
            return f"malformed scoped package {spec!r}"
        at = spec.find("@", scope_sep)
    else:
        at = spec.find("@")
    if at < 0:
        return f"npm package {spec!r} has no version pin (use pkg@X.Y.Z)"
    version = spec[at + 1:]
    if version.lower() in _FLOATING_NPM_TAGS:
        return f"npm package {spec!r} uses floating tag {version!r}"
    if not _SEMVER_PIN_RE.match(version):
        return f"npm package {spec!r} version {version!r} is not an exact X.Y.Z pin"
    return None


def _first_package_arg(args: list[str]) -> str | None:
    """First positional (non-flag) arg, skipping ``-y``/``--yes``/``-q``
    and ``-p <pkg>``/``--package <pkg>`` (in which case the package operand
    is the pinned thing)."""
    skip_next = False
    for a in args:
        if skip_next:
            return a
        if a in ("-p", "--package", "--from"):
            skip_next = True
            continue
        if a.startswith("-"):
            continue
        return a
    return None


def _check_pins(entry: CatalogEntry) -> list[str]:
    t = entry.transport
    if t.type != "stdio" or not t.command:
        return []
    problems: list[str] = []
    base = Path(t.command).name
    if base in _NPM_LAUNCHERS:
        args = list(t.args)
        if base == "pnpm" and args[:1] == ["dlx"]:
            args = args[1:]
        pkg = _first_package_arg(args)
        if pkg is None:
            problems.append("no package operand found for npm launcher")
        else:
            reason = _npm_package_pin_issue(pkg)
            if reason:
                problems.append(reason)
    elif base in _PY_LAUNCHERS:
        args = list(t.args)
        if base == "pipx" and args[:1] == ["run"]:
            args = args[1:]
        pkg = _first_package_arg(args)
        if pkg is None:
            problems.append("no package operand found for python launcher")
        elif not _PY_PIN_RE.match(pkg):
            problems.append(f"python package {pkg!r} must be pinned as pkg==X.Y.Z")
    elif base == "docker":
        image = None
        for a in t.args:
            if a in ("run", "-i", "--rm", "--init") or a.startswith("-"):
                continue
            image = a
            break
        if image is None:
            problems.append("no docker image found in args")
        else:
            m = _DOCKER_TAG_RE.match(image)
            tag = m.group("tag") if m else None
            digest = m.group("digest") if m else None
            if not digest and (not tag or tag == "latest"):
                problems.append(f"docker image {image!r} must carry an explicit tag or digest")
    if base in _NPM_LAUNCHERS | _PY_LAUNCHERS | {"docker"} and not t.version:
        problems.append(
            "transport.version (informational pin) is required for launcher-based entries"
        )
    for a in t.args:
        if a.endswith("@latest") or a.endswith("@next") or a.endswith("@canary"):
            problems.append(f"floating version in args: {a!r}")
    return problems


def lint_manifest(path: Path | str, *, today: date | None = None) -> list[LintIssue]:
    """Validate one manifest. Returns issues (empty = clean)."""
    path = Path(path)
    issues: list[LintIssue] = []

    def err(code: str, msg: str) -> None:
        issues.append(LintIssue(str(path), code, msg))

    try:
        entry = parse_manifest(path)
    except CatalogError as exc:
        err("schema", str(exc))
        return issues

    expected = expected_name_for(path)
    if entry.name != expected:
        err("name-mismatch", f"name {entry.name!r} must match location {expected!r}")

    if not entry.verified:
        err("verified-missing", "shipped manifests must record `verified: YYYY-MM-DD`")
    elif not _DATE_RE.match(entry.verified):
        err("verified-format", f"verified {entry.verified!r} is not YYYY-MM-DD")
    else:
        try:
            v = date.fromisoformat(entry.verified)
            if v > (today or date.today()):
                err("verified-future", f"verified {entry.verified} is in the future")
        except ValueError:
            err("verified-format", f"verified {entry.verified!r} is not a real date")

    if not entry.homepage.startswith("https://"):
        err("homepage", "homepage must be an https:// URL")
    if not entry.tags:
        err("tags", "at least one tag is required")
    for tag in entry.tags:
        if not _TAG_RE.match(tag):
            err("tags", f"tag {tag!r} must be lowercase [a-z0-9-]")
    if not entry.license:
        err(
            "license",
            "license (SPDX id of the server software, or 'proprietary' for hosted services) "
            "is required",
        )

    t = entry.transport
    if t.type == "http":
        if not (t.url or "").startswith("https://"):
            err("url-scheme", "http transport url must be https://")
        if re.search(r"[?&](token|key|api_key|apikey|secret|password)=", t.url or "", re.I):
            err("inline-secret", "url carries a credential-shaped query parameter")

    for reason in _check_pins(entry):
        err("floating-version", reason)

    # inline secrets: static env for secret-shaped keys must be ${VAR};
    # secret env specs must not ship a default; nothing may look like a token.
    for key, value in t.env.items():
        if is_secret_key(key) and not _ENV_REF_RE.search(value):
            err(
                "inline-secret",
                f"transport.env.{key} is secret-shaped but not a ${{VAR}} reference",
            )
    for spec in entry.auth.env:
        if spec.secret and spec.default:
            err("inline-secret", f"auth.env.{spec.name} is secret but ships a default value")
        if spec.secret and not is_secret_key(spec.name):
            issues.append(LintIssue(
                str(path), "secret-name",
                f"auth.env.{spec.name} is marked secret but its name has no "
                "TOKEN/KEY/SECRET/PASSWORD marker; config.py redaction keys off the name",
                severity="warning",
            ))
    for value in [t.command or "", t.url or "", *t.args, *t.env.values(), entry.post_install,
                  entry.description, entry.auth.header_format]:
        if _SECRET_LITERAL_RE.search(value):
            err("inline-secret", "a credential-shaped literal appears in the manifest")

    # every ${VAR} referenced must be declared in auth.env (or carry a default)
    declared = {e.name for e in entry.auth.env}
    hard, _soft = _referenced_env([t.command or "", t.url or "", *t.args, *t.env.values(),
                                   entry.auth.header_format])
    for var in sorted(hard - declared):
        err("undeclared-env", f"${{{var}}} is referenced but not declared in auth.env")

    if entry.auth.type == "api_key":
        for spec in entry.auth.secrets:
            if not spec.required:
                issues.append(LintIssue(
                    str(path), "api-key-optional",
                    f"api_key secret {spec.name} is marked optional", "warning",
                ))
    if entry.auth.type == "oauth" and entry.auth.oauth:
        for key, value in entry.auth.oauth.items():
            if is_secret_key(str(key)):
                err("inline-secret", f"auth.oauth.{key} must not be in a manifest")
            if isinstance(value, str) and _SECRET_LITERAL_RE.search(value):
                err("inline-secret", f"auth.oauth.{key} looks like a credential")

    # the entry must round-trip through Freyja's real mcp.json schema
    try:
        spec_dict = build_server_dict(entry)
        spec_dict["enabled"] = False
        spec = McpServerSpec.from_dict(entry.name, spec_dict)
        spec.validate()
    except (McpConfigError, Exception) as exc:  # noqa: BLE001
        err("spec-invalid", f"generated mcp.json entry rejected by McpServerSpec: {exc}")

    if not entry.post_install:
        issues.append(
            LintIssue(str(path), "post-install", "no post_install setup notes", "warning")
        )
    return issues


def manifest_lint(root: Path | str | None = None, *, today: date | None = None) -> list[LintIssue]:
    """Lint every manifest under *root* (default: the shipped catalog).
    Also flags an empty catalog and duplicate names across dir/flat forms."""
    root = shipped_catalog_dir() if root is None else Path(root).expanduser()
    issues: list[LintIssue] = []
    paths = iter_manifest_paths(root)
    if not paths:
        issues.append(LintIssue(str(root), "empty", "no manifests found"))
        return issues
    seen: dict[str, Path] = {}
    for p in paths:
        issues.extend(lint_manifest(p, today=today))
        name = expected_name_for(p)
        if name in seen:
            issues.append(LintIssue(str(p), "duplicate", f"{name!r} also defined at {seen[name]}"))
        seen[name] = p
    return issues


def lint_errors(issues: Iterable[LintIssue]) -> list[LintIssue]:
    return [i for i in issues if i.severity == "error"]


__all__ = [
    "AuthSpec",
    "CatalogDiagnostic",
    "CatalogEntry",
    "CatalogError",
    "EnvVarSpec",
    "InstallReport",
    "LintIssue",
    "SuggestSpec",
    "ToolsSpec",
    "TransportSpec",
    "build_server_dict",
    "catalog_diagnostics",
    "catalog_info",
    "catalog_install",
    "catalog_list",
    "catalog_search",
    "expected_name_for",
    "get_entry",
    "iter_manifest_paths",
    "lint_errors",
    "lint_manifest",
    "load_catalog_dir",
    "manifest_lint",
    "parse_manifest",
    "shipped_catalog_dir",
    "user_catalog_dir",
]
