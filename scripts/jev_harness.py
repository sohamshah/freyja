#!/usr/bin/env python3
"""Drive `jev_computer_use` exactly as the bridge does, from a source tree.

The tool is constructed with a real SubAgentSpec (registry, event sink, the
bridge's provider factory, the inbox-memo builder), and `execute()` is called
with the same JSON arguments a model would send. The output is what the
calling agent sees: the immediate tool result and, for a background run, the
inbox memo it receives when the run ends.

Run it with the app's own interpreter so freyja_native and every dependency
match production. The bundle's pyvenv.cfg has a relative `home`, so start it
from Contents/Resources:

    cd /Applications/Freyja.app/Contents/Resources && \\
      ./python-bundle/bin/python3 <repo>/scripts/jev_harness.py call \\
      '{"goal": "...", "app": "Arc"}' --trace

Subcommands:
  schema               the description and parameters the agent sees
  call JSON            execute the tool with these arguments
                       (--trace prints the run log; --timeout seconds)
  trace RUN            condensed run log for a run id or log path
  serve                serve tests/fixtures/jev_live on 127.0.0.1 (+ POST /event)
  suite [NAMES]        run scenarios from scripts/jev_scenarios.py
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

HARNESS_DIR = Path.home() / ".freyja" / "jev-operator" / "harness"
FIXTURES = REPO / "tests" / "fixtures" / "jev_live"
EVENTS_FILE = HARNESS_DIR / "events.jsonl"
DEFAULT_PORT = 8765


def load_env() -> None:
    """The bridge's keys: the repo's .env, else the main checkout's (a worktree has none)."""
    for p in (REPO / ".env", Path.home() / "personal" / "freyja" / ".env"):
        if not p.exists():
            continue
        for line in p.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
        return


class Harness:
    """A parent session as the bridge builds it, minus the model."""

    def __init__(self, workspace: Path) -> None:
        from bridge.freyja_bridge import build_provider
        from bridge.tools.base import ToolRegistry
        from bridge.tools.jev_computer_use_tool import JevComputerUseTool
        from bridge.tools.sub_agent_registry import SubAgentRegistry
        from bridge.tools.sub_agent_tool import SubAgentSpec, build_subagent_memo

        self.events: list[dict[str, Any]] = []
        self.memos: list[Any] = []
        self._memo = build_subagent_memo

        async def on_child_terminal(record: Any) -> None:
            self.memos.append(self._memo(record))

        self.spec = SubAgentSpec(
            parent_workspace=str(workspace),
            parent_model="harness",
            build_provider=build_provider,
            parent_registry=ToolRegistry(),
            registry=SubAgentRegistry(),
            emit_event=self.events.append,
            parent_session_id="harness",
            on_child_terminal=on_child_terminal,
        )
        self.tool = JevComputerUseTool(sub_spec=self.spec)

    async def call(self, arguments: dict[str, Any], *, timeout_s: float = 1200) -> dict[str, Any]:
        t0 = time.time()
        res = await self.tool.execute(f"harness-{int(t0)}", arguments)
        out: dict[str, Any] = {"immediate": res.content, "is_error": res.is_error, "memo": None}
        records = self.spec.registry.list_all()
        rec = records[-1] if records else None
        if rec is not None and getattr(rec, "bg_task", None) is not None:
            try:
                await asyncio.wait_for(asyncio.shield(rec.bg_task), timeout_s)
            except asyncio.TimeoutError:
                rec.cancel_event.set()
                try:
                    await asyncio.wait_for(rec.bg_task, 30)
                except Exception:  # noqa: BLE001
                    pass
                out["timed_out"] = True
            await asyncio.sleep(0.05)
            if self.memos:
                m = self.memos[-1]
                out["memo"] = getattr(m, "content", str(m))
        text = out["memo"] or out["immediate"]
        m = re.search(r"log=(\S+\.jsonl)", text or "")
        out["log"] = m.group(1) if m else None
        out["elapsed_s"] = round(time.time() - t0, 1)
        out["frames"] = self._save_frames(rec.id if rec else "none")
        return out

    def _save_frames(self, run: str) -> list[str]:
        frames = [e for e in self.events if e.get("type") == "screenshot_frame" and e.get("pngBase64")]
        if not frames:
            return []
        d = HARNESS_DIR / "frames"
        d.mkdir(parents=True, exist_ok=True)
        saved = []
        for i, e in enumerate(frames[-3:]):
            ext = "jpg" if "jpeg" in str(e.get("mimeType")) else "png"
            p = d / f"{run}-{i}.{ext}"
            p.write_bytes(base64.b64decode(e["pngBase64"]))
            saved.append(str(p))
        return saved


# ─── trace ──────────────────────────────────────────────────────────────


def _short(v: Any, n: int) -> str:
    s = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)
    s = " ".join(s.split())
    return s if len(s) <= n else s[: n - 1] + "…"


def trace_lines(path: str, *, tables: bool = False) -> list[str]:
    out = []
    p = Path(path)
    if not p.exists():
        cand = Path.home() / ".freyja" / "jev-operator" / "runs" / f"{path}.jsonl"
        p = cand if cand.exists() else p
    if not p.exists():
        return [f"(no log at {path})"]
    t0 = None
    for line in p.read_text().splitlines():
        try:
            r = json.loads(line)
        except ValueError:
            continue
        t0 = t0 or r.get("t")
        ts = f"{(r.get('t', t0) - t0):6.1f}s"
        ev = r.get("event")
        tag = f"{ts} [{r.get('surface', '?')}{'#' + str(r['item']) if 'item' in r else ''}]"
        if ev == "start":
            out.append(f"{tag} start app={r.get('app')} goal={_short(r.get('goal'), 140)}")
        elif ev == "decision":
            out.append(
                f"{tag} step {r.get('step')}: {r.get('decision')} (op {r.get('op_conf')}, "
                f"tgt {r.get('target_conf')}, read {r.get('read_ms')}ms, {r.get('elements')} el)"
                + (f" reasons={r.get('reasons')}" if r.get("reasons") else "")
            )
            if tables and r.get("table"):
                out.extend("        " + x for x in str(r["table"]).split("\n")[:80])
        elif ev == "outcome":
            out.append(
                f"{tag}   → {_short(r.get('action'), 90)} ok={r.get('ok')} "
                f"meaningful={r.get('meaningful')} diff={_short(r.get('diff'), 160)}"
                + (f" error={_short(r.get('error'), 160)}" if r.get("error") else "")
            )
        elif ev == "replan":
            out.append(f"{tag}   ↺ replan ({_short(r.get('reason'), 120)}) → {_short(r.get('out'), 220)}")
        elif ev == "verify":
            out.append(f"{tag}   ✓ verify → {_short(r.get('out'), 240)}")
        elif ev == "llm":
            out.append(f"{tag}   llm {r.get('door')} {r.get('ms')}ms")
        elif ev == "finish":
            out.append(f"{tag} FINISH {r.get('status')}: {_short(r.get('summary'), 300)}")
        else:
            rest = {k: v for k, v in r.items() if k not in ("run_id", "t", "surface", "event", "item")}
            out.append(f"{tag} {ev} {_short(rest, 220)}")
    return out


# ─── fixture server ─────────────────────────────────────────────────────


def serve(port: int) -> None:
    from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

    HARNESS_DIR.mkdir(parents=True, exist_ok=True)

    class Handler(SimpleHTTPRequestHandler):
        def __init__(self, *a: Any, **kw: Any) -> None:
            super().__init__(*a, directory=str(FIXTURES), **kw)

        def log_message(self, *a: Any) -> None:  # quiet
            pass

        def end_headers(self) -> None:
            self.send_header("Cache-Control", "no-store")
            super().end_headers()

        def do_POST(self) -> None:  # noqa: N802
            if self.path.split("?")[0] != "/event":
                self.send_error(404)
                return
            n = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(n).decode("utf-8", "replace")
            try:
                ev = json.loads(body)
            except ValueError:
                ev = {"raw": body}
            ev["_t"] = time.time()
            with EVENTS_FILE.open("a", encoding="utf-8") as f:
                f.write(json.dumps(ev, ensure_ascii=False) + "\n")
            self.send_response(204)
            self.end_headers()

    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"serving {FIXTURES} on http://127.0.0.1:{port} (events → {EVENTS_FILE})", flush=True)
    srv.serve_forever()


def page_events(run: str) -> list[dict[str, Any]]:
    if not EVENTS_FILE.exists():
        return []
    out = []
    for line in EVENTS_FILE.read_text().splitlines():
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if ev.get("run") == run:
            out.append(ev)
    return out


# ─── main ───────────────────────────────────────────────────────────────


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("schema")
    c = sub.add_parser("call")
    c.add_argument("arguments", help="tool arguments as JSON (or @file)")
    c.add_argument("--trace", action="store_true")
    c.add_argument("--tables", action="store_true")
    c.add_argument("--timeout", type=float, default=1200)
    t = sub.add_parser("trace")
    t.add_argument("run")
    t.add_argument("--tables", action="store_true")
    s = sub.add_parser("serve")
    s.add_argument("--port", type=int, default=DEFAULT_PORT)
    su = sub.add_parser("suite")
    su.add_argument("names", nargs="*")
    su.add_argument("--port", type=int, default=DEFAULT_PORT)
    su.add_argument("--repeat", type=int, default=1)
    args = ap.parse_args()

    if args.cmd == "serve":
        serve(args.port)
        return 0
    if args.cmd == "trace":
        print("\n".join(trace_lines(args.run, tables=args.tables)))
        return 0

    load_env()
    import logging

    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")

    if args.cmd == "schema":
        from bridge.tools.jev_computer_use_tool import JevComputerUseTool

        h = Harness(REPO)
        d = h.tool.definition
        print(d.description)
        print(json.dumps(d.parameters, indent=2))
        return 0

    if args.cmd == "call":
        raw = args.arguments
        if raw.startswith("@"):
            raw = Path(raw[1:]).read_text()
        arguments = json.loads(raw)
        h = Harness(REPO)
        out = asyncio.run(h.call(arguments, timeout_s=args.timeout))
        print("=== tool result (what the agent sees immediately) ===")
        print(out["immediate"])
        if out["memo"]:
            print("\n=== inbox memo (what the agent receives when the run ends) ===")
            print(out["memo"])
        print(f"\n[harness] elapsed={out['elapsed_s']}s log={out['log']} frames={out['frames']}"
              + (" TIMED OUT" if out.get("timed_out") else ""))
        if args.trace and out["log"]:
            print("\n=== run log ===")
            print("\n".join(trace_lines(out["log"], tables=args.tables)))
        return 0

    if args.cmd == "suite":
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import jev_scenarios  # noqa: PLC0415

        return asyncio.run(jev_scenarios.run_suite(args.names, port=args.port, repeat=args.repeat))
    return 1


if __name__ == "__main__":
    sys.exit(main())
