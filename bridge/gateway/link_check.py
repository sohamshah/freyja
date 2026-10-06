"""Flag outbound links whose host does not exist.

2026-10-05: Freyja posted SigNoz links on hostnames it had guessed
(``demo.signoz.ema.co``) and only retracted them two messages later. A
guessed host usually fails DNS, and a DNS lookup is cheap, so we check that
before the text goes out. This does not prove a link is right: a real host
with an invented path still passes. It catches the invented-host case and
nothing more, and the footnote says so.

A host can also fail to resolve because the Mac is off the VPN, so the
wording is "could not resolve from this machine", not "this link is fake".
"""

from __future__ import annotations

import asyncio
import re
import socket
import time
from urllib.parse import urlparse

# Slack mrkdwn ``<https://host/path|label>`` and bare URLs in prose.
_URL_RE = re.compile(r"https?://[^\s<>|)\]\"']+", re.IGNORECASE)

_CACHE_TTL_SEC = 600.0
_LOOKUP_TIMEOUT_SEC = 2.0
_MAX_HOSTS = 12

# host → (resolved, checked-at monotonic)
_cache: dict[str, tuple[bool, float]] = {}


def extract_hosts(text: str) -> list[str]:
    """Distinct hostnames in ``text``, in first-seen order."""
    seen: dict[str, None] = {}
    for m in _URL_RE.finditer(text or ""):
        host = (urlparse(m.group(0).rstrip(".,;:!?")).hostname or "").lower()
        if host and host not in seen:
            seen[host] = None
    return list(seen)


async def _resolves(host: str) -> bool:
    hit = _cache.get(host)
    now = time.monotonic()
    if hit is not None and now - hit[1] < _CACHE_TTL_SEC:
        return hit[0]
    loop = asyncio.get_running_loop()
    try:
        await asyncio.wait_for(
            loop.getaddrinfo(host, 443, type=socket.SOCK_STREAM),
            timeout=_LOOKUP_TIMEOUT_SEC,
        )
        ok = True
    except socket.gaierror:
        ok = False
    except Exception:  # noqa: BLE001
        # Timeout or resolver trouble: not evidence the host is fake.
        return True
    _cache[host] = (ok, now)
    return ok


async def unresolved_hosts(text: str) -> list[str]:
    hosts = extract_hosts(text)[:_MAX_HOSTS]
    if not hosts:
        return []
    results = await asyncio.gather(*(_resolves(h) for h in hosts))
    return [h for h, ok in zip(hosts, results) if not ok]


async def annotate_unresolved(text: str) -> str:
    """Append a one-line caution when ``text`` links to unknown hosts."""
    try:
        bad = await unresolved_hosts(text)
    except Exception:  # noqa: BLE001
        return text
    if not bad:
        return text
    listed = ", ".join(f"`{h}`" for h in bad)
    return (
        f"{text}\n\n_Unverified link: could not resolve {listed} from this "
        "machine. Check it before you rely on it._"
    )
