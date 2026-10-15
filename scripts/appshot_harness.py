#!/usr/bin/env python3
"""Capture an appshot from a source tree and print what the model would get.

This runs the same code as the hotkey, the `appshot` tool and the voice verb
(`bridge/appshot.py`), so use it to tune the reader on real windows: how long a
window takes, how many characters it gives, and whether the text is complete.

    # the window the user was last in (not Freyja's own)
    python scripts/appshot_harness.py

    # a named app, text only, saved to a folder
    python scripts/appshot_harness.py --app Slack --no-image --save /tmp/shots

    # list the windows the capture can choose from
    python scripts/appshot_harness.py --windows

Run it with the app's interpreter so freyja_native matches production (see
scripts/jev_harness.py). A read of a Chromium or Electron app can switch the
app's web accessibility on for a moment and back off. Do not point it at a
browser that holds a live call unless you are fine with that.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from bridge import appshot  # noqa: E402


def list_windows() -> None:
    import freyja_native as native

    for w in native.list_windows():
        mark = "freyja" if appshot.is_freyja_window(w) else "vault" if appshot.is_password_manager(w) else ""
        print(f"{w.id:7d}  {w.owner[:22]:22s}  {w.bundle[:34]:34s}  {int(w.bounds.w)}x{int(w.bounds.h)}  {w.title[:50]!r} {mark}")


async def run(args: argparse.Namespace) -> int:
    shot = await appshot.capture(
        source="harness",
        window_id=args.window_id,
        app=args.app,
        want_image=not args.no_image,
        max_chars=args.max_chars,
    )
    if shot.error:
        print(f"error: {shot.error}")
        return 1
    print(f'{shot.app} ({shot.bundle_id}) window {shot.window_id} "{shot.window_title}"')
    print(
        f"text:  {shot.text_status}"
        + (f" ({shot.text_reason})" if shot.text_reason else "")
        + f", {len(shot.text):,} chars, truncated={shot.truncated}, web tree switched on={shot.web_enabled}, {shot.ms_text} ms"
    )
    if shot.image is not None:
        print(f"image: {shot.image.width}x{shot.image.height} {shot.image.mime_type}, {len(shot.image.data):,} bytes, {shot.ms_image} ms")
    else:
        print(f"image: {shot.image_status}" + (f" ({shot.image_reason})" if shot.image_reason else ""))
    print(f"total: {shot.ms_total} ms\n")
    if args.save:
        out = Path(args.save).expanduser()
        out.mkdir(parents=True, exist_ok=True)
        stem = f"{shot.app}-{shot.window_id}".replace("/", "-")
        (out / f"{stem}.txt").write_text(appshot.block_for(shot))
        if shot.image is not None:
            ext = "jpg" if shot.image.mime_type == "image/jpeg" else "png"
            (out / f"{stem}.{ext}").write_bytes(shot.image.data)
        print(f"saved to {out}/{stem}.*")
    else:
        head = shot.text.splitlines()[: args.lines]
        print("\n".join(head))
        if len(shot.text.splitlines()) > args.lines:
            print(f"… ({len(shot.text.splitlines()) - args.lines} more lines; --lines N or --save DIR)")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--app", help="app name or bundle id (default: the window the user was last in)")
    p.add_argument("--window-id", type=int, help="a CGWindowID from --windows")
    p.add_argument("--no-image", action="store_true", help="read the text only")
    p.add_argument("--max-chars", type=int, default=appshot.DEFAULT_MAX_CHARS)
    p.add_argument("--lines", type=int, default=40, help="lines of text to print (default 40)")
    p.add_argument("--save", help="write the <appshot> block and the image to this folder")
    p.add_argument("--windows", action="store_true", help="list the open windows and exit")
    args = p.parse_args()
    if args.windows:
        list_windows()
        return 0
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
