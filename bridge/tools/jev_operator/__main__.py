"""Headless runner for the Jev operator.

    .venv/bin/python -m bridge.tools.jev_operator --app Calculator --goal "compute 12 × 7"
    .venv/bin/python -m bridge.tools.jev_operator --app Finder --goal "..." --dry-run
    .venv/bin/python -m bridge.tools.jev_operator --app TextEdit --goal '...' --no-llm --max-steps 10

Reads TYPESAFE_AI_API_KEY (and the LLM provider key) from the environment or
from ~/personal/freyja/.env when present.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path


def _load_env() -> None:
    p = Path(__file__).resolve().parents[3] / ".env"
    if not p.exists():
        return
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--goal", required=True)
    ap.add_argument("--app", default=None, help="bundle id or app name; default frontmost app")
    ap.add_argument("--max-steps", type=int, default=40)
    ap.add_argument("--no-llm", action="store_true")
    ap.add_argument(
        "--llm-model", default=None, help="default: $FREYJA_JEV_OPERATOR_LLM or claude-opus-5-5"
    )
    ap.add_argument("--allow-irreversible", action="store_true")
    ap.add_argument("--dry-run", action="store_true", help="observe + decide once, act on nothing")
    ap.add_argument("--no-verify", action="store_true")
    ap.add_argument("--settle-ms", type=int, default=350)
    ap.add_argument("--json", action="store_true", help="print the RunResult as JSON")
    args = ap.parse_args()
    _load_env()

    import freyja_native as native

    from bridge.decisions.provider import TypeSafeProvider
    from bridge.tools.computer_tools import ComputerToolSpec
    from bridge.tools.jev_operator.handoff import (
        LLMHelper,
        completer_from_provider,
        default_llm_model,
    )
    from bridge.tools.jev_operator.loop import Operator, OperatorConfig

    provider = TypeSafeProvider()
    if not provider.available:
        print("TYPESAFE_AI_API_KEY is not set", file=sys.stderr)
        return 2

    llm = None
    if not args.no_llm:
        try:
            from bridge.freyja_bridge import build_provider

            model = args.llm_model or default_llm_model()
            llm = LLMHelper(completer_from_provider(build_provider(model, "off")))
        except Exception as exc:  # noqa: BLE001
            print(f"LLM helper unavailable ({exc}); running Jev-only", file=sys.stderr)

    spec = ComputerToolSpec(
        session_id="cli",
        emit_event=lambda _e: None,
        cancel_event=asyncio.Event(),
        owner="jev_operator_cli",
    )
    cfg = OperatorConfig(
        goal=args.goal,
        app=args.app,
        max_steps=args.max_steps,
        allow_irreversible=args.allow_irreversible,
        use_llm=llm is not None,
        verify_with_llm=not args.no_verify,
        dry_run=args.dry_run,
        settle_ms=args.settle_ms,
    )

    def say(t: str) -> None:
        print(t, flush=True)

    op = Operator(cfg, provider=provider, spec=spec, native=native, llm=llm, on_step=say)
    res = await op.run()
    print()
    print(res.summary)
    print(res.footer())
    if args.json:
        print(
            json.dumps(
                {k: v for k, v in res.__dict__.items() if k != "history"},
                ensure_ascii=False,
                default=str,
            )
        )
    return 0 if res.status in ("done", "dry_run") else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
