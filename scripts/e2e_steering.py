#!/usr/bin/env python3
"""Live end-to-end check of mid-turn input and background work.

Runs the real bridge (``bridge/freyja_bridge.py``) as a subprocess, drives
it over its stdin/stdout JSON protocol the way the desktop app does, and
checks the behavior described in docs/MID-TURN-INPUT.md against a real
model. Each scenario prints PASS/FAIL with the timings that matter.

It spends real tokens (a few cents per scenario on Sonnet) and runs real
shell commands (``sleep`` / ``echo`` only). Everything is written under a
fresh temporary HOME, so nothing touches ~/.freyja except reading API keys
from ~/.freyja/.env. Permission prompts are auto-approved.

Usage (from the repo root):
    uv run python scripts/e2e_steering.py                  # every scenario
    uv run python scripts/e2e_steering.py bg_soft subagent  # just these
    uv run python scripts/e2e_steering.py --model claude-sonnet-4-6 --keep

Scenarios:
    soft          Enter mid-turn: the follow-up lands after the running
                  tool and is answered in the same turn.
    force_stream  Ctrl+Enter while the reply streams: the stream is cut at
                  once and the reply switches to the message.
    bg_soft       A follow-up during a 40s command moves the command to
                  the background after ~15s; its exit memo wakes the agent.
    bg_force      Ctrl+Enter during a command moves it to the background at
                  once instead of killing it; the memo arrives when it exits.
    after_turn    Tab mid-turn: the message waits and runs as its own turn.
    stop_note     A stop aimed at a finished turn is refused; a real stop
                  keeps the partial reply and the next turn is told.
    subagent      sub_agent returns at once, the parent keeps chatting, and
                  the child's memo wakes the parent.
    subagent_pane Opening and typing into a running child's pane reaches
                  the child (no parentless copy); typing into it after it
                  finished re-wakes it under its parent, with no replay.

Don't use claude-haiku-4-5 as --model: its `general` sub-agents fail on an
adaptive-thinking 400, so `subagent` can't pass.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PYTHON = os.environ.get("FREYJA_E2E_PYTHON", sys.executable)


_MARKERS: list[str] = []


def _marker() -> str:
    """A unique fractional part for `sleep`, so process checks (and the
    cleanup at the end) can't touch anyone else's sleep."""
    marker = f"{random.randint(100000, 999999)}"
    _MARKERS.append(marker)
    return marker


def _alive(pattern: str) -> bool:
    out = subprocess.run(["pgrep", "-f", pattern], capture_output=True, text=True).stdout
    return bool(out.strip())


def _bridge_env(home: Path) -> dict[str, str]:
    env = dict(os.environ)
    dotenv = Path.home() / ".freyja" / ".env"
    if dotenv.exists():
        for line in dotenv.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                env[key.strip()] = value.strip().strip('"').strip("'")
    workspace = home / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    env.update(
        HOME=str(home),
        FREYJA_HOME=str(home / ".freyja"),
        FREYJA_WORKSPACE=str(workspace),
        # The temporary HOME doesn't isolate launchd, which has one job
        # registry per user; never let this bridge touch the real jobs.
        FREYJA_NO_LAUNCHD="1",
        PYTHONPATH=str(REPO),
        PYTHONUNBUFFERED="1",
    )
    env.pop("PYTHONHOME", None)
    return env


class Bridge:
    """The bridge process plus every event it emitted, timestamped."""

    def __init__(self, home: Path, model: str) -> None:
        self.home = home
        self.model = model
        self.events: list[tuple[float, dict]] = []
        self.t0 = time.monotonic()
        self.proc: asyncio.subprocess.Process | None = None

    async def start(self) -> None:
        env = _bridge_env(self.home)
        self.proc = await asyncio.create_subprocess_exec(
            PYTHON,
            str(REPO / "bridge" / "freyja_bridge.py"),
            cwd=env["FREYJA_WORKSPACE"],
            env=env,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=open(self.home / "bridge.stderr.log", "w"),  # noqa: SIM115
            limit=64 * 1024 * 1024,
        )
        asyncio.create_task(self._read())
        await self.wait_for(lambda e: e.get("type") == "ready", 120, "bridge ready")

    async def _read(self) -> None:
        assert self.proc and self.proc.stdout
        while line := await self.proc.stdout.readline():
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            self.events.append((time.monotonic() - self.t0, ev))
            if ev.get("type") == "permission_request":
                await self.send(
                    {
                        "type": "permission_response",
                        "sessionId": ev.get("sessionId"),
                        "requestId": ev["requestId"],
                        "approved": True,
                    }
                )

    async def send(self, cmd: dict) -> None:
        assert self.proc and self.proc.stdin
        self.proc.stdin.write((json.dumps(cmd) + "\n").encode())
        await self.proc.stdin.drain()

    def say(self, sid: str, content: str, **extra) -> dict:
        return {
            "type": "send_message",
            "sessionId": sid,
            "content": content,
            "model": self.model,
            "reasoningLevel": "none",
            "coordinationStrategy": "bus",
            **extra,
        }

    async def wait_for(self, pred, timeout: float, what: str, start: int = 0):
        """(index after the match, time, event) of the first event from
        ``start`` on that matches."""
        deadline = time.monotonic() + timeout
        seen = start
        while time.monotonic() < deadline:
            while seen < len(self.events):
                t, ev = self.events[seen]
                seen += 1
                if pred(ev):
                    return seen, t, ev
            await asyncio.sleep(0.05)
        raise TimeoutError(f"timed out waiting for {what}")

    def text(self, sid: str, start: int = 0, end: int | None = None) -> str:
        return "".join(
            e.get("text", "")
            for _, e in self.events[start:end]
            if e.get("sessionId") == sid and e.get("type") == "text_delta"
        )

    def now(self) -> float:
        return time.monotonic() - self.t0

    async def stop(self) -> None:
        if self.proc and self.proc.returncode is None:
            self.proc.kill()
            await self.proc.wait()


def _is(sid: str, etype: str, **fields):
    def pred(e: dict) -> bool:
        return (
            e.get("sessionId") == sid
            and e.get("type") == etype
            and all(e.get(k) == v for k, v in fields.items())
        )

    return pred


def _memo(sid: str):
    return lambda e: (
        e.get("sessionId") == sid
        and e.get("type") == "inbox_event"
        and e.get("action") == "enqueued"
        and (e.get("message") or {}).get("kind") == "memo"
    )


# ─── scenarios: each returns (ok, detail) ─────────────────────────────


async def soft(b: Bridge):
    sid, m = "e2e-soft", _marker()
    i0 = len(b.events)
    await b.send(b.say(sid, f"Use bash to run exactly: sleep 8.{m} && echo TICK . Then tell me the output in one line."))
    await b.wait_for(_is(sid, "tool_use_start", name="bash"), 90, "bash start", i0)
    await asyncio.sleep(1.5)
    await b.send(b.say(sid, "Also: after that, tell me what 17+25 is.", clientId="fu-soft", followup=True))
    end, _, _ = await b.wait_for(_is(sid, "turn_complete"), 180, "turn end", i0)
    turns = sum(1 for _, e in b.events[i0:end] if _is(sid, "turn_start")(e))
    injected = any(
        _is(sid, "inbox_injected")(e) and e.get("midTurn")
        and any(i.get("clientId") == "fu-soft" for i in e["items"])
        for _, e in b.events[i0:end]
    )
    text = b.text(sid, i0, end)
    ok = turns == 1 and injected and "TICK" in text and "42" in text
    return ok, f"one turn={turns == 1}, injected mid-turn={injected}, TICK={'TICK' in text}, 42={'42' in text}"


async def force_stream(b: Bridge):
    sid = "e2e-stream"
    i0 = len(b.events)
    await b.send(b.say(sid, "Write a 700-word short story about a lighthouse keeper. No tools."))
    await b.wait_for(_is(sid, "text_delta"), 90, "first text", i0)
    await asyncio.sleep(2.0)
    sent = b.now()
    await b.send(b.say(sid, "Stop the story now. Reply with just the word OKAY.", clientId="fu-stream", followup=True, force=True))
    _, t_cut, cut = await b.wait_for(lambda e: _is(sid, "system_event")(e) and e.get("subtype") == "turn_interrupted", 30, "interrupt", i0)
    end, _, _ = await b.wait_for(_is(sid, "turn_complete"), 120, "turn end", i0)
    tail = b.text(sid, i0, end)[-40:]
    ok = cut["details"].get("phase") == "llm" and t_cut - sent < 3 and "OKAY" in tail
    return ok, f"cut after {t_cut - sent:.2f}s ({cut['details'].get('partial_chars')} chars streamed); reply tail={tail!r}"


async def bg_soft(b: Bridge):
    sid, m = "e2e-bg-soft", _marker()
    cmd = f"sleep 40.{m}"
    i0 = len(b.events)
    await b.send(b.say(sid, f"Use bash to run exactly: {cmd} && echo BUILT . Then report the output."))
    _, t_bash, _ = await b.wait_for(_is(sid, "tool_use_start", name="bash"), 90, "bash start", i0)
    await asyncio.sleep(2)
    await b.send(b.say(sid, "Also: what is 9*9? Answer that now.", clientId="fu-bg", followup=True))
    ri, t_res, res = await b.wait_for(_is(sid, "tool_result"), 60, "bash result", i0)
    end, _, _ = await b.wait_for(_is(sid, "turn_complete"), 90, "turn end", ri)
    answered = "81" in b.text(sid, i0, end)
    still_running = _alive(cmd)
    mi, t_memo, memo = await b.wait_for(_memo(sid), 90, "command memo", end)
    wi, _, _ = await b.wait_for(_is(sid, "turn_start"), 30, "wake turn", mi)
    we, _, _ = await b.wait_for(_is(sid, "turn_complete"), 90, "wake turn end", wi)
    woke_with = "built" in b.text(sid, wi, we).lower()
    backgrounded = "Moved to the background" in (res.get("preview") or "")
    ok = backgrounded and t_res - t_bash < 20 and answered and still_running and "BUILT" in memo["message"]["content"] and woke_with
    return ok, (
        f"backgrounded {t_res - t_bash:.1f}s after start={backgrounded}; 81 answered={answered}; "
        f"still running after the turn={still_running}; memo at {t_memo:.0f}s; wake reply has BUILT={woke_with}"
    )


async def bg_force(b: Bridge):
    sid, m = "e2e-bg-force", _marker()
    cmd = f"sleep 25.{m}"
    i0 = len(b.events)
    await b.send(b.say(sid, f"Use bash to run exactly: {cmd} && echo DONE25 . Then report the output."))
    await b.wait_for(_is(sid, "tool_use_start", name="bash"), 90, "bash start", i0)
    await asyncio.sleep(2)
    sent = b.now()
    await b.send(b.say(sid, "Don't wait on that. Reply with just the word PIVOT.", clientId="fu-f", followup=True, force=True))
    _, t_res, res = await b.wait_for(_is(sid, "tool_result"), 30, "bash result", i0)
    end, _, _ = await b.wait_for(_is(sid, "turn_complete"), 60, "turn end", i0)
    pivoted = "PIVOT" in b.text(sid, i0, end)
    still_running = _alive(cmd)
    _, t_memo, memo = await b.wait_for(_memo(sid), 60, "command memo", end)
    ok = t_res - sent < 3 and "cut in" in (res.get("preview") or "") and pivoted and still_running and "DONE25" in memo["message"]["content"]
    return ok, (
        f"backgrounded {t_res - sent:.1f}s after the cut-in; PIVOT={pivoted}; "
        f"still running (not killed)={still_running}; memo at {t_memo:.0f}s has DONE25={'DONE25' in memo['message']['content']}"
    )


async def after_turn(b: Bridge):
    sid, m = "e2e-after-turn", _marker()
    i0 = len(b.events)
    await b.send(b.say(sid, f"Use bash to run exactly: sleep 6.{m} && echo ONE . Then report the output and stop."))
    await b.wait_for(_is(sid, "tool_use_start", name="bash"), 90, "bash start", i0)
    await b.send(b.say(sid, "Now reply with just the word TWO.", clientId="q-1", followup=True, afterTurn=True))
    t1, _, _ = await b.wait_for(_is(sid, "turn_complete"), 90, "first turn end", i0)
    first = b.text(sid, i0, t1)
    injected_early = any(
        _is(sid, "inbox_injected")(e) and any(i.get("clientId") == "q-1" for i in e["items"])
        for _, e in b.events[i0:t1]
    )
    pi, _, _ = await b.wait_for(_is(sid, "followups_promoted"), 30, "promotion", i0)
    t2, _, _ = await b.wait_for(_is(sid, "turn_complete"), 90, "second turn end", pi)
    second = b.text(sid, pi, t2)
    ok = "ONE" in first and "TWO" not in first and not injected_early and "TWO" in second
    return ok, f"turn 1 ONE={'ONE' in first} (no TWO={'TWO' not in first}, not injected={not injected_early}); turn 2 TWO={'TWO' in second}"


async def stop_note(b: Bridge):
    sid = "e2e-stop"
    i0 = len(b.events)
    await b.send(b.say(sid, "Write a 700-word essay about rivers. No tools."))
    _, _, started = await b.wait_for(_is(sid, "turn_start"), 60, "turn start", i0)
    await b.wait_for(_is(sid, "text_delta"), 60, "text", i0)
    await asyncio.sleep(1.5)
    await b.send({"type": "force_cancel", "sessionId": sid, "scope": "turn", "turnId": "turn-999"})
    await b.wait_for(lambda e: e.get("sessionId") == sid and e.get("subtype") == "turn_cancel_stale", 10, "stale-stop refusal", i0)
    await b.send({"type": "force_cancel", "sessionId": sid, "scope": "turn", "turnId": started["turnId"]})
    await b.wait_for(_is(sid, "turn_complete"), 30, "stopped", i0)
    await asyncio.sleep(0.5)
    i1 = len(b.events)
    await b.send(b.say(sid, "In one sentence: what happened to your last reply?"))
    end, _, _ = await b.wait_for(_is(sid, "turn_complete"), 90, "next turn end", i1)
    answer = b.text(sid, i1, end).lower()
    saved = json.loads((b.home / ".freyja" / "sessions" / f"{sid}.transcript.json").read_text())
    msgs = [en["message"] for en in saved["transcript"]["entries"] if en.get("message")]

    def as_text(msg: dict) -> str:
        content = msg.get("content")
        return content if isinstance(content, str) else json.dumps(content)

    partial_kept = any(m["role"] == "assistant" and "river" in as_text(m).lower() for m in msgs[:3])
    told = any(m["role"] == "user" and "stopped your previous turn on purpose" in as_text(m) for m in msgs)
    knows = any(w in answer for w in ("stop", "interrupt", "cut"))
    return partial_kept and told and knows, f"stale stop refused; partial reply kept={partial_kept}; next turn told={told}; answer mentions it={knows}"


async def subagent(b: Bridge):
    sid, m = "e2e-subagent", _marker()
    i0 = len(b.events)
    await b.send(b.say(sid, (
        "Spawn exactly one sub_agent with label 'wordsmith', agent_type 'general', and task: "
        f"'Use bash to run: sleep 20.{m} && echo BANANA . Then reply with only the word it printed.' "
        "Do not wait for it. After launching it, tell me in one short sentence that it is running, and end your turn."
    )))
    t1, _, _ = await b.wait_for(_is(sid, "turn_complete"), 120, "first turn end", i0)
    spawned = [e for _, e in b.events[i0:t1] if e.get("type") == "session_spawned" and e.get("parentSessionId") == sid]
    child = spawned[0]["sessionId"] if spawned else None

    def child_done() -> bool:
        return any(_is(child, "session_completed")(e) for _, e in b.events[i0:])

    running_after_turn = child is not None and not child_done()
    i1 = len(b.events)
    await b.send(b.say(sid, "Quick question while that runs: what is 6*7? Answer in one line."))
    t2, _, _ = await b.wait_for(_is(sid, "turn_complete"), 90, "side question end", i1)
    side_answered = "42" in b.text(sid, i1, t2)
    running_during_chat = not child_done()
    mi, _, memo = await b.wait_for(_memo(sid), 150, "memo", i1)
    wi, _, _ = await b.wait_for(_is(sid, "turn_start"), 60, "wake turn", mi)
    we, _, _ = await b.wait_for(_is(sid, "turn_complete"), 150, "wake turn end", wi)
    woke_with = "banana" in b.text(sid, wi, we).lower()
    ok = running_after_turn and side_answered and running_during_chat and "BANANA" in memo["message"]["content"] and woke_with
    return ok, (
        f"child running after the parent's turn={running_after_turn}; side question answered={side_answered} "
        f"(child still running={running_during_chat}); memo has BANANA={'BANANA' in memo['message']['content']}; wake reply has it={woke_with}"
    )


async def subagent_pane(b: Bridge):
    """The desktop opens a running child's pane (switch_session with its id)
    and the operator types into it. That must reach the child itself, not a
    parentless copy (2026-10-07). Once the child is done, typing into its
    pane again re-wakes it under its parent, without replaying the first
    message, and the parent gets the memo."""
    sid, m = "e2e-pane", _marker()
    i0 = len(b.events)
    await b.send(b.say(sid, (
        "Spawn exactly one sub_agent with label 'counter', agent_type 'general', and task: "
        f"'Use bash to run: sleep 25.{m} && echo APPLE . Then use the talk tool with to=\"parent\" "
        "to send the single word PEAR. Then reply with the output of the command, plus any extra "
        "word the operator asked you to add.' "
        "Do not wait for it. After launching it, tell me in one short sentence that it is running, and end your turn."
    )))
    t1, _, _ = await b.wait_for(_is(sid, "turn_complete"), 120, "first turn end", i0)
    spawned = [e for _, e in b.events[i0:t1] if e.get("type") == "session_spawned" and e.get("parentSessionId") == sid]
    child = spawned[0]["sessionId"]
    await b.send({"type": "switch_session", "sessionId": child, "model": b.model})
    await asyncio.sleep(1)
    await b.send(b.say(child, "Operator here: also add the word KIWI to your final reply.", clientId="fu-pane", followup=True))
    await b.wait_for(
        lambda e: _is(child, "inbox_injected")(e) and any(i.get("clientId") == "fu-pane" for i in e["items"]),
        90, "child took the operator message", i0,
    )
    mi, _, memo = await b.wait_for(_memo(sid), 150, "memo", i0)
    report = memo["message"]["content"]
    pear = any(
        _is(sid, "inbox_event", action="enqueued")(e)
        and (e.get("message") or {}).get("fromSession") == child
        and "PEAR" in (e.get("message") or {}).get("content", "")
        for _, e in b.events[i0:]
    )
    copied = any(
        e.get("type") == "log" and f"session {child} ready" in e.get("message", "")
        for _, e in b.events[i0:]
    )
    # The memo either wakes the parent or slides into a turn the PEAR
    # message already started; either way, let that turn finish.
    di, _, _ = await b.wait_for(
        lambda e: _is(sid, "inbox_event", action="delivered")(e)
        and (e.get("message") or {}).get("id") == memo["message"]["id"],
        60, "memo read", mi,
    )
    await b.wait_for(_is(sid, "turn_complete"), 150, "parent turn end", di)

    i2 = len(b.events)
    await b.send(b.say(child, "Operator again: reply with only the word MANGO."))
    ri, _, resumed = await b.wait_for(
        lambda e: e.get("type") == "session_spawned" and e.get("sessionId") == child and e.get("resumed"),
        60, "child re-woken", i2,
    )
    _, _, drained = await b.wait_for(_is(child, "inbox_injected"), 60, "child drained", ri)
    _, _, memo2 = await b.wait_for(_memo(sid), 150, "second memo", i2)
    ok = (
        "KIWI" in report and pear and not copied
        and resumed.get("parentSessionId") == sid
        and len(drained["items"]) == 1
        and "MANGO" in memo2["message"]["content"]
    )
    return ok, (
        f"no root copy={not copied}; pane message reached the child (KIWI in report={'KIWI' in report}); "
        f"talk('parent') arrived={pear}; re-woken under parent={resumed.get('parentSessionId') == sid}; "
        f"messages replayed on re-wake={len(drained['items']) - 1}; second memo has MANGO={'MANGO' in memo2['message']['content']}"
    )


SCENARIOS = {
    "soft": soft,
    "force_stream": force_stream,
    "bg_soft": bg_soft,
    "bg_force": bg_force,
    "after_turn": after_turn,
    "stop_note": stop_note,
    "subagent": subagent,
    "subagent_pane": subagent_pane,
}


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("scenarios", nargs="*", help=f"any of: {', '.join(SCENARIOS)} (default: all)")
    parser.add_argument("--model", default="claude-sonnet-4-6")
    parser.add_argument("--keep", action="store_true", help="keep the temp HOME (logs, events) even on success")
    args = parser.parse_args()
    unknown = [n for n in args.scenarios if n not in SCENARIOS]
    if unknown:
        parser.error(f"unknown scenario(s): {', '.join(unknown)}")
    names = args.scenarios or list(SCENARIOS)

    home = Path(tempfile.mkdtemp(prefix="freyja-e2e-"))
    bridge = Bridge(home, args.model)
    results: dict[str, bool] = {}
    try:
        await bridge.start()
        for name in names:
            try:
                ok, detail = await SCENARIOS[name](bridge)
            except Exception as exc:  # noqa: BLE001
                ok, detail = False, f"{type(exc).__name__}: {exc}"
            results[name] = ok
            print(f"{'PASS' if ok else 'FAIL'}  {name}: {detail}", flush=True)
    finally:
        await bridge.stop()
        (home / "events.jsonl").write_text(
            "\n".join(json.dumps({"t": round(t, 3), **e}) for t, e in bridge.events)
        )
        # Commands the scenarios backgrounded outlive the bridge.
        for marker in _MARKERS:
            subprocess.run(["pkill", "-f", rf"sleep [0-9]+\.{marker}"], capture_output=True)

    failed = [n for n, ok in results.items() if not ok]
    if failed or args.keep:
        print(f"\nlogs and events: {home}")
    else:
        shutil.rmtree(home, ignore_errors=True)
    print(f"\n{len(results) - len(failed)}/{len(results)} passed" + (f" — failed: {', '.join(failed)}" if failed else ""))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
