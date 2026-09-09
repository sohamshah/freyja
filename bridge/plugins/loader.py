"""Claude-Code-style plugin install/uninstall/list for Freyja.

Design doc section 5. A plugin is a directory in the Claude Code plugin
interchange format:

    .claude-plugin/plugin.json   manifest (name, version, ...)
    skills/<name>/SKILL.md       skills
    commands/<name>.md           slash-command prompt templates
    .mcp.json                    MCP server entries

``install()`` fans the plugin out into Freyja's two existing systems:

- Skills: the plugin lands in ``<plugins_root>/<name>/`` and SkillStore
  (bridge/knowledge/skill_store.py) scans ``<plugins_root>/*/skills/**``
  as a standing scan root, prefixing every skill name with
  ``<plugin>:``. No per-install registration is needed; ``refresh()``
  picks new files up via its fingerprint check.
- Commands: Freyja has no slash-command runtime, but command ``.md``
  files are prompt templates — which is what skills are. Each
  ``commands/<x>.md`` is wrapped at install time into a synthetic skill
  written to ``<plugin>/skills/commands/<x>/SKILL.md`` (frontmatter
  ``name: <plugin>:<x>``, ``type: command``), so it is discoverable
  through the same search_skills/load_skill path.
- MCP servers: each entry of the plugin's ``.mcp.json`` is merged into
  the MCP catalog (``~/.freyja/mcp.json`` by default) via
  bridge/mcp/config.load_catalog/save_catalog with ``enabled: false``,
  ``trust: "standard"`` and ``source: {"plugin": "<name>@<source>"}``.

  ``${CLAUDE_PLUGIN_ROOT}`` in command/args/env/url/headers is expanded
  to the installed plugin directory AT INSTALL TIME and stored expanded:
  bridge/mcp/config.py resolves ``${VAR}`` references against
  ``os.environ`` at connect time, and CLAUDE_PLUGIN_ROOT is not part of
  the bridge environment, so storing the resolved path keeps mcp.json
  self-contained without any bridge/mcp changes.

Unsupported manifest sections (hooks, agents, lsp, monitors, settings,
bin — whether declared as manifest keys or shipped as conventional
top-level directories) are recorded in installed.json and returned in
the install summary, never silently dropped.

Link mode: ``install(path, link=True)`` mirrors a local source as a
"link farm" (real directories, per-file symlinks) instead of copying.
Content edits to source files show up live (SkillStore stat()s through
the symlinks), but files *added* to the source after install require a
re-install. A farm rather than one whole-directory symlink because
(a) generated command-skills must be written inside ``<plugin>/skills/``
without mutating the operator's source checkout, and (b)
``Path.rglob()`` does not follow nested directory symlinks.

Uninstall reverses everything: the plugin directory (which removes its
skills and generated command-skills from SkillStore on next refresh),
its installed.json record, and any catalog servers whose
``source.plugin`` names this plugin.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

from bridge.mcp.config import (
    McpConfigError,
    McpServerSpec,
    load_catalog,
    save_catalog,
)

logger = logging.getLogger(__name__)

DEFAULT_PLUGINS_ROOT = Path.home() / ".freyja" / "plugins"
DEFAULT_MCP_PATH = Path.home() / ".freyja" / "mcp.json"

INSTALLED_FILENAME = "installed.json"

# Claude Code manifest/layout sections Freyja has no runtime for. They
# are reported in the install summary and recorded in installed.json.
UNSUPPORTED_SECTIONS = ("hooks", "agents", "lsp", "monitors", "settings", "bin")

_PLUGIN_ROOT_VAR = "${CLAUDE_PLUGIN_ROOT}"
_GIT_URL_RE = re.compile(r"^(https?://|git@|ssh://|git://)")
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


class PluginError(RuntimeError):
    """Install/uninstall failed in a way the caller should surface."""


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def install(
    source: str | Path,
    *,
    link: bool = False,
    plugins_root: Path | str | None = None,
    mcp_path: Path | str | None = None,
) -> dict[str, Any]:
    """Install a plugin from a local path or git URL.

    Returns a summary dict: name/version/source/installed_at/path,
    ``skill_names`` and ``command_names`` (both ``<plugin>:``-prefixed),
    an ``mcp`` merge report (added / already_present / conflicts /
    invalid), and ``unsupported`` manifest sections.
    """
    root = _resolve_root(plugins_root)
    mcp = _resolve_mcp(mcp_path)
    root.mkdir(parents=True, exist_ok=True)

    source_str = str(source)
    local = Path(source_str).expanduser()
    staging: Path | None = None
    try:
        if local.exists():
            if not local.is_dir():
                raise PluginError(f"plugin source is not a directory: {source_str}")
            staged = local.resolve()
        elif _is_git_url(source_str):
            if link:
                raise PluginError("link mode requires a local path source, not a git URL")
            staging = Path(tempfile.mkdtemp(prefix="freyja-plugin-"))
            staged = _git_clone(source_str, staging)
        else:
            raise PluginError(f"plugin source not found: {source_str}")

        manifest = _read_manifest(staged)
        name = str(manifest["name"]).strip()
        if not _NAME_RE.match(name):
            raise PluginError(f"invalid plugin name {name!r} in manifest")
        version = str(manifest.get("version") or "")

        dest = root / name
        _remove_tree(dest)
        if staging is not None:
            shutil.move(str(staged), str(dest))
        elif link:
            _link_farm(staged, dest)
        else:
            shutil.copytree(
                staged, dest, ignore=shutil.ignore_patterns(".git"), symlinks=False
            )
    finally:
        if staging is not None:
            shutil.rmtree(staging, ignore_errors=True)

    # Order matters: discover the plugin's own skills BEFORE generating
    # command-skill shims into dest/skills/commands/.
    skill_names = _discover_skills(dest, name)
    command_names = _wrap_commands(dest, name)
    mcp_summary = _merge_mcp(dest, name, source_str, mcp)
    unsupported = _unsupported_sections(manifest, dest)

    record = {
        "name": name,
        "version": version,
        "source": source_str,
        "installed_at": int(time.time() * 1000),
        "link": bool(link),
        "unsupported": unsupported,
        "skills": len(skill_names),
        "commands": len(command_names),
        "servers": len(mcp_summary["added"]) + len(mcp_summary["already_present"]),
        "mcp_servers": mcp_summary["added"] + mcp_summary["already_present"],
    }
    installed = _load_installed(root)
    installed[name] = record
    _save_installed(root, installed)

    return {
        **record,
        "path": str(dest),
        "skill_names": skill_names,
        "command_names": command_names,
        "mcp": mcp_summary,
    }


def uninstall(
    name: str,
    *,
    plugins_root: Path | str | None = None,
    mcp_path: Path | str | None = None,
) -> dict[str, Any]:
    """Remove a plugin: its directory (SkillStore drops the skills on
    next refresh), its installed.json record, and any MCP catalog
    servers whose ``source.plugin`` names it. Other catalog entries are
    never touched."""
    root = _resolve_root(plugins_root)
    mcp = _resolve_mcp(mcp_path)

    dest = root / name
    removed_dir = dest.is_symlink() or dest.exists()
    _remove_tree(dest)

    installed = _load_installed(root)
    had_record = name in installed
    if had_record:
        installed.pop(name)
        _save_installed(root, installed)

    removed_servers: list[str] = []
    catalog = load_catalog(mcp)
    for server_name in list(catalog.specs):
        if _plugin_owner(catalog.specs[server_name]) == name:
            del catalog.specs[server_name]
            removed_servers.append(server_name)
    if removed_servers:
        save_catalog(catalog, mcp)

    if not (removed_dir or had_record or removed_servers):
        raise PluginError(f"plugin '{name}' is not installed")
    return {
        "name": name,
        "removed_dir": removed_dir,
        "removed_record": had_record,
        "removed_mcp_servers": removed_servers,
    }


def list_installed(
    *, plugins_root: Path | str | None = None
) -> list[dict[str, Any]]:
    """Return installed plugin records (sorted by name), each including
    skill/command/server counts recorded at install time."""
    root = _resolve_root(plugins_root)
    installed = _load_installed(root)
    return [dict(installed[key]) for key in sorted(installed)]


# ---------------------------------------------------------------------------
# Fetch / stage
# ---------------------------------------------------------------------------

def _resolve_root(plugins_root: Path | str | None) -> Path:
    return Path(plugins_root).expanduser() if plugins_root else DEFAULT_PLUGINS_ROOT


def _resolve_mcp(mcp_path: Path | str | None) -> Path:
    return Path(mcp_path).expanduser() if mcp_path else DEFAULT_MCP_PATH


def _is_git_url(source: str) -> bool:
    return bool(_GIT_URL_RE.match(source)) or source.endswith(".git")


def _git_clone(url: str, staging: Path) -> Path:
    dest = staging / "repo"
    try:
        proc = subprocess.run(
            ["git", "clone", "--depth", "1", url, str(dest)],
            capture_output=True,
            text=True,
            timeout=600,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise PluginError(f"git clone failed for {url}: {exc}") from exc
    if proc.returncode != 0:
        raise PluginError(f"git clone failed for {url}: {proc.stderr.strip()}")
    shutil.rmtree(dest / ".git", ignore_errors=True)
    return dest


def _read_manifest(plugin_dir: Path) -> dict[str, Any]:
    path = plugin_dir / ".claude-plugin" / "plugin.json"
    if not path.is_file():
        raise PluginError(
            f"not a Claude Code plugin (missing .claude-plugin/plugin.json in {plugin_dir})"
        )
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        raise PluginError(f"unreadable plugin manifest {path}: {exc}") from exc
    if not isinstance(doc, dict) or not str(doc.get("name") or "").strip():
        raise PluginError(f"plugin manifest {path} has no 'name'")
    return doc


def _remove_tree(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path)


def _link_farm(src: Path, dest: Path) -> None:
    """Mirror *src* into *dest*: real directories, per-file symlinks.
    See module docstring for why link mode is a farm rather than a
    single directory symlink."""
    dest.mkdir(parents=True, exist_ok=True)
    for entry in sorted(src.iterdir()):
        if entry.name == ".git":
            continue
        target = dest / entry.name
        if entry.is_dir() and not entry.is_symlink():
            _link_farm(entry, target)
        else:
            target.symlink_to(entry.resolve())


# ---------------------------------------------------------------------------
# Skills and command-skill shims
# ---------------------------------------------------------------------------

def _split_frontmatter(text: str) -> tuple[dict[str, str], str]:
    """Tiny frontmatter reader (scalar values only) for plugin skill and
    command files. Mirrors what skill_store._parse_frontmatter accepts."""
    if not text.startswith("---"):
        return {}, text
    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}, text
    meta: dict[str, str] = {}
    for raw_line in parts[1].splitlines():
        line = raw_line.strip()
        if not line or line.startswith("- ") or ":" not in line:
            continue
        key, _, value = line.partition(":")
        meta[key.strip()] = value.strip().strip("\"'")
    return meta, parts[2].strip()


def _discover_skills(plugin_dir: Path, plugin_name: str) -> list[str]:
    """Names (``<plugin>:``-prefixed) of the plugin's own skills, using
    the same name derivation as SkillStore (frontmatter ``name``, else
    the containing directory)."""
    skills_dir = plugin_dir / "skills"
    if not skills_dir.is_dir():
        return []
    names: list[str] = []
    for path in sorted(skills_dir.rglob("SKILL.md")):
        try:
            meta, _body = _split_frontmatter(path.read_text(encoding="utf-8"))
        except OSError:
            continue
        name = meta.get("name") or path.parent.name
        prefix = f"{plugin_name}:"
        if not name.startswith(prefix):
            name = prefix + name
        names.append(name)
    return names


def _wrap_commands(plugin_dir: Path, plugin_name: str) -> list[str]:
    """Wrap each ``commands/<x>.md`` into a synthetic skill at
    ``skills/commands/<x>/SKILL.md`` so it is discoverable via
    search_skills/load_skill. Returns the generated skill names."""
    commands_dir = plugin_dir / "commands"
    if not commands_dir.is_dir():
        return []
    generated: list[str] = []
    for path in sorted(commands_dir.glob("*.md")):
        try:
            meta, body = _split_frontmatter(path.read_text(encoding="utf-8"))
        except OSError:
            continue
        command = path.stem
        skill_name = f"{plugin_name}:{command}"
        description = meta.get("description") or f"Plugin command /{skill_name}"
        # One line, no double quotes: keeps the generated frontmatter
        # unambiguous for skill_store's line-based parser.
        description = " ".join(description.split()).replace('"', "'")
        skill_dir = plugin_dir / "skills" / "commands" / command
        skill_dir.mkdir(parents=True, exist_ok=True)
        frontmatter = "\n".join(
            [
                "---",
                f'name: "{skill_name}"',
                "type: command",
                f'description: "{description}"',
                f"tags: [command, plugin, {plugin_name}]",
                f"triggers: [/{skill_name}]",
                f"source: plugin:{plugin_name}",
                "---",
                "",
            ]
        )
        (skill_dir / "SKILL.md").write_text(frontmatter + body + "\n", encoding="utf-8")
        generated.append(skill_name)
    return generated


# ---------------------------------------------------------------------------
# MCP catalog merge
# ---------------------------------------------------------------------------

def _plugin_owner(spec: McpServerSpec) -> str:
    """Plugin name from a spec's ``source.plugin`` tag ("<name>@<source>"),
    or "" if the entry is not plugin-owned."""
    source = spec.source if isinstance(spec.source, dict) else {}
    tag = str(source.get("plugin") or "")
    return tag.split("@", 1)[0] if tag else ""


def _expand_plugin_root(value: Any, plugin_dir: Path) -> Any:
    if isinstance(value, str):
        return value.replace(_PLUGIN_ROOT_VAR, str(plugin_dir))
    if isinstance(value, list):
        return [_expand_plugin_root(v, plugin_dir) for v in value]
    if isinstance(value, dict):
        return {k: _expand_plugin_root(v, plugin_dir) for k, v in value.items()}
    return value


def _to_catalog_entry(raw: dict[str, Any], plugin_dir: Path) -> dict[str, Any]:
    """Translate a Claude Code .mcp.json server entry into catalog
    schema: ``type`` becomes ``transport`` (sse/streamable-http map to
    http) and ``${CLAUDE_PLUGIN_ROOT}`` is expanded (see module
    docstring for why expansion happens at install time)."""
    entry = dict(raw)
    transport = str(entry.pop("type", "") or entry.get("transport") or "").strip().lower()
    if not transport:
        transport = "http" if entry.get("url") else "stdio"
    if transport in ("sse", "streamable-http", "streamable_http", "streamablehttp"):
        transport = "http"
    entry["transport"] = transport
    return _expand_plugin_root(entry, plugin_dir)


def _merge_mcp(
    plugin_dir: Path, plugin_name: str, source: str, mcp_path: Path
) -> dict[str, Any]:
    summary: dict[str, Any] = {
        "added": [],
        "already_present": [],
        "conflicts": [],
        "invalid": [],
    }
    mcp_file = plugin_dir / ".mcp.json"
    if not mcp_file.is_file():
        return summary
    try:
        doc = json.loads(mcp_file.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        raise PluginError(f"unreadable {mcp_file}: {exc}") from exc
    servers = doc.get("mcpServers") if isinstance(doc, dict) else None
    if not isinstance(servers, dict):
        servers = doc.get("servers") if isinstance(doc, dict) else None
    if not isinstance(servers, dict):
        return summary

    catalog = load_catalog(mcp_path)
    changed = False
    for server_name, raw in servers.items():
        server_name = str(server_name)
        if not isinstance(raw, dict):
            summary["invalid"].append(
                {"name": server_name, "reason": "entry is not an object"}
            )
            continue
        existing = catalog.specs.get(server_name)
        if existing is not None:
            # Idempotent re-install: an entry we already own is left
            # untouched so operator edits (enabled, trust, tools)
            # survive. A same-named entry owned by someone else is a
            # conflict and is never overwritten.
            if _plugin_owner(existing) == plugin_name:
                summary["already_present"].append(server_name)
            else:
                summary["conflicts"].append(
                    {
                        "name": server_name,
                        "reason": "a server with this name already exists in the catalog",
                    }
                )
            continue
        entry = _to_catalog_entry(raw, plugin_dir)
        entry["enabled"] = False
        entry["trust"] = "standard"
        entry["source"] = {"plugin": f"{plugin_name}@{source}"}
        try:
            spec = McpServerSpec.from_dict(server_name, entry)
            spec.validate()
        except McpConfigError as exc:
            summary["invalid"].append({"name": server_name, "reason": str(exc)})
            continue
        catalog.specs[spec.name] = spec
        summary["added"].append(server_name)
        changed = True
    if changed:
        save_catalog(catalog, mcp_path)
    return summary


# ---------------------------------------------------------------------------
# Unsupported sections + installed.json
# ---------------------------------------------------------------------------

def _unsupported_sections(manifest: dict[str, Any], plugin_dir: Path) -> list[str]:
    found: list[str] = []
    for key in UNSUPPORTED_SECTIONS:
        if key in manifest or (plugin_dir / key).is_dir():
            found.append(key)
    return found


def _load_installed(root: Path) -> dict[str, Any]:
    path = root / INSTALLED_FILENAME
    if not path.is_file():
        return {}
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        logger.warning("plugins: unreadable %s (%s); treating as empty", path, exc)
        return {}
    return doc if isinstance(doc, dict) else {}


def _save_installed(root: Path, data: dict[str, Any]) -> None:
    """Atomic write (tmp + rename), same pattern as
    bridge/mcp/config.save_catalog."""
    root.mkdir(parents=True, exist_ok=True)
    path = root / INSTALLED_FILENAME
    payload = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    fd, tmp_name = tempfile.mkstemp(dir=str(root), prefix=".installed-json-")
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
