"""Atomic .env updater that preserves comments + ordering.

The setup wizard writes Slack tokens to ``~/.freyja/.env``. We don't
want to clobber any existing keys the operator has (e.g. provider API
keys, ``FREYJA_PERMISSION_AUTO``, etc.) and we want comments / blank
lines preserved through the round-trip.

Strategy: parse the file line-by-line into a list of (kind, key, value)
records (where kind ∈ {raw, kv}), apply mutations in place by replacing
matching ``kv`` rows or appending new ones, then write tmp + rename.
"""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import Path

from bridge.gateway.pid import freyja_home


def env_path() -> Path:
    return freyja_home() / ".env"


@dataclass
class _Line:
    raw: str
    key: str | None = None  # None for comment / blank lines


def _parse(text: str) -> list[_Line]:
    out: list[_Line] = []
    for raw_line in text.splitlines():
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#"):
            out.append(_Line(raw=raw_line))
            continue
        if "=" not in stripped:
            out.append(_Line(raw=raw_line))
            continue
        key = stripped.split("=", 1)[0].strip()
        # Strip a leading "export " if present (some shells use it).
        if key.startswith("export "):
            key = key[len("export "):].strip()
        out.append(_Line(raw=raw_line, key=key))
    return out


def _format(key: str, value: str) -> str:
    """Format a KEY=VALUE line. Quotes values containing whitespace,
    `#`, or shell-special chars so re-sourcing the file works."""
    needs_quote = (
        any(c in value for c in (" ", "\t", "#", "$", "`", "\"", "'", "\\"))
        or not value
    )
    if needs_quote:
        escaped = value.replace("\\", "\\\\").replace("\"", "\\\"")
        return f"{key}=\"{escaped}\""
    return f"{key}={value}"


def read_env() -> dict[str, str]:
    """Read the .env file into a dict. Missing file → empty dict."""
    path = env_path()
    if not path.exists():
        return {}
    out: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        key = key.strip()
        if key.startswith("export "):
            key = key[len("export "):].strip()
        value = value.strip()
        # Strip surrounding quotes if balanced
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("\"", "'"):
            inner = value[1:-1]
            value = inner.replace("\\\"", "\"").replace("\\\\", "\\")
        out[key] = value
    return out


def get_env_value(key: str) -> str | None:
    """Read a single key from the .env file."""
    return read_env().get(key)


def save_env_values(updates: dict[str, str]) -> Path:
    """Apply a batch of {key: value} updates to ``~/.freyja/.env``,
    preserving comments + ordering for keys that already exist.

    Atomic via tmp + rename; mode set to 0600 since this file holds
    secrets (Slack tokens, API keys).
    """
    path = env_path()
    existing_text = path.read_text(encoding="utf-8") if path.exists() else ""
    lines = _parse(existing_text)

    keys_seen: set[str] = set()
    for ln in lines:
        if ln.key is not None and ln.key in updates:
            ln.raw = _format(ln.key, updates[ln.key])
            keys_seen.add(ln.key)

    new_keys = [k for k in updates if k not in keys_seen]
    if new_keys:
        # Ensure there's a separator if the file doesn't end on a blank line
        if lines and lines[-1].raw.strip():
            lines.append(_Line(raw=""))
        for k in new_keys:
            lines.append(_Line(raw=_format(k, updates[k]), key=k))

    output = "\n".join(ln.raw for ln in lines)
    if not output.endswith("\n"):
        output += "\n"

    tmp = path.with_suffix(".tmp")
    tmp.write_text(output, encoding="utf-8")
    tmp.replace(path)

    # Tighten perms — this file holds tokens. Owner read/write only.
    try:
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass

    return path


def save_env_value(key: str, value: str) -> Path:
    """Convenience for a single key/value update."""
    return save_env_values({key: value})


def env_summary_redacted() -> dict[str, str]:
    """Return current .env values with secrets redacted, for display.
    Keys matching common secret patterns get their values replaced
    with a fingerprint (first 4 + last 4 chars)."""
    raw = read_env()
    out: dict[str, str] = {}
    for k, v in raw.items():
        if _is_secret_key(k) and len(v) > 10:
            out[k] = f"{v[:4]}…{v[-4:]}"
        else:
            out[k] = v
    return out


def _is_secret_key(key: str) -> bool:
    upper = key.upper()
    return any(needle in upper for needle in ("TOKEN", "KEY", "SECRET", "PASSWORD"))
