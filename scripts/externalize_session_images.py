"""Move inline base64 images out of existing session files into the media store.

New writes already store images by hash (engine/media_store.py); this
converts what was written before: transcripts (including sub-agents'),
compaction snapshots, raw message logs and the per-session event mirror.
Nothing is deleted: every image's bytes land in
``$FREYJA_HOME/media/images/`` and each rewritten file is checked (every
hash resolves back to the exact original base64) before it replaces the
original.

    uv run python scripts/externalize_session_images.py            # dry run
    uv run python scripts/externalize_session_images.py --apply     # rewrite

``--apply`` refuses to run while Freyja is running: the bridge keeps event
files open for appending, and replacing one under it would lose events.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import engine.media_store as media_store  # noqa: E402
from engine.media_store import canonical_bytes, externalize_images, get_image  # noqa: E402

HOME = Path(os.environ.get("FREYJA_HOME") or os.path.expanduser("~/.freyja"))

# Event-mirror payload fields (bridge/freyja_bridge.py _externalize_event_images).
_EVENT_FIELDS = (("pngBase64", "pngSha256"), ("imageB64", "imageSha256"))


def _targets() -> list[tuple[Path, str]]:
    sessions, projects = HOME / "sessions", HOME / "projects"
    out = [(p, "json") for p in sessions.glob("*.transcript.json")]
    out += [(p, "json") for p in (sessions / "compactions").glob("*.json")]
    out += [(p, "jsonl") for p in projects.glob("*/raw_messages.jsonl")]
    out += [(p, "events") for p in sessions.glob("*.events.jsonl")]
    return sorted(out)


def _externalize_event(row: dict) -> dict:
    from bridge.freyja_bridge import _externalize_event_images

    return _externalize_event_images(row)


def _convert(text: str, kind: str) -> str:
    if kind == "json":
        return json.dumps(externalize_images(json.loads(text)), separators=(",", ":"))
    lines = []
    for line in text.splitlines():
        if not line.strip() or ("base64" not in line.lower() and "B64" not in line):
            lines.append(line)
            continue
        try:
            row = json.loads(line)
        except ValueError:
            lines.append(line)  # keep a corrupt line exactly as it was
            continue
        row = _externalize_event(row) if kind == "events" else externalize_images(row)
        lines.append(json.dumps(row, ensure_ascii=False, default=str))
    return "\n".join(lines) + ("\n" if text.endswith("\n") else "")


def _resolve(obj):
    """Inverse of the conversion, for verification."""
    if isinstance(obj, list):
        return [_resolve(x) for x in obj]
    if not isinstance(obj, dict):
        return obj
    obj = {k: _resolve(v) for k, v in obj.items()}
    if obj.get("type") == "image" and "sha256" in obj and "data" not in obj:
        obj["data"] = get_image(obj.pop("sha256"), obj.get("media_type", "image/png"))
    for inline, ref in _EVENT_FIELDS:
        if ref in obj:
            obj[inline] = get_image(obj.pop(ref), obj.get("mimeType") or "image/png")
    if isinstance(obj.get("images"), list):
        for img in obj["images"]:
            if isinstance(img, dict) and "dataSha256" in img:
                mime = img.get("mimeType") or "image/png"
                img["dataBase64"] = get_image(img.pop("dataSha256"), mime)
    return obj


def _verify(original: str, converted: str, kind: str) -> bool:
    if kind == "json":
        return _resolve(json.loads(converted)) == json.loads(original)
    for a, b in zip(original.splitlines(), converted.splitlines(), strict=True):
        if a == b:
            continue
        if _resolve(json.loads(b)) != json.loads(a):
            return False
    return True


def _freyja_running() -> bool:
    out = subprocess.run(
        ["pgrep", "-f", "freyja_bridge.py|bridge.gateway.cli"], capture_output=True, text=True
    )
    return bool(out.stdout.strip())


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--apply", action="store_true", help="rewrite files (default: dry run)")
    args = ap.parse_args()
    if args.apply and _freyja_running():
        print("Freyja is running. Quit the app (and the gateway/scheduler agents) before --apply.")
        return 1
    # Unique image bytes the store holds for these files (each image once).
    unique: dict[str, int] = {}
    store = media_store.put_image

    def _counting_put(data: str, media_type: str) -> str | None:
        raw = canonical_bytes(data)
        if raw is None:
            return None
        if args.apply:
            sha = store(data, media_type)
        else:  # dry run: hash what would be stored, write nothing
            sha = hashlib.sha256(raw).hexdigest()
        if sha is not None:
            unique[sha] = len(raw)
        return sha

    media_store.put_image = _counting_put

    before = after = changed = failed = 0
    for path, kind in _targets():
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        before += len(text)
        if "base64" not in text.lower() and "B64" not in text:
            after += len(text)
            continue
        try:
            converted = _convert(text, kind)
        except (ValueError, TypeError) as exc:
            print(f"skip (unparseable): {path}: {exc}")
            after += len(text)
            continue
        after += len(converted)
        if converted == text:
            continue
        changed += 1
        if not args.apply:
            continue
        if not _verify(text, converted, kind):
            failed += 1
            print(f"skip (verification failed): {path}")
            continue
        tmp = path.with_name(path.name + ".externalize-tmp")
        tmp.write_text(converted, encoding="utf-8")
        os.replace(tmp, path)

    mode = "rewrote" if args.apply else "would rewrite"
    stored = sum(unique.values())
    print(f"{mode} {changed - failed} files: {before / 1e9:.2f} GB -> {after / 1e9:.2f} GB, "
          f"plus {len(unique)} unique images ({stored / 1e9:.2f} GB) "
          f"in {HOME / 'media' / 'images'}; "
          f"net saving {(before - after - stored) / 1e9:.2f} GB")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
