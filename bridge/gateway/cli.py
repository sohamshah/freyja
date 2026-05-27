"""`freyja` CLI entry point.

Subcommand surface:

  freyja setup [slack]            run the setup wizard
  freyja gateway run              foreground daemon (dev / debug)
  freyja gateway install          install launchd plist + start it
  freyja gateway uninstall        stop + remove plist
  freyja gateway start            launchctl start (already-installed plist)
  freyja gateway stop             launchctl stop (graceful SIGTERM)
  freyja gateway status           show running pid + connected platforms
  freyja gateway logs [--follow]  tail the gateway log
  freyja slack manifest           print the manifest
  freyja slack manifest --write   write to ~/.freyja/slack-manifest.json
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from typing import Sequence

from bridge.gateway.pid import (
    freyja_home,
    gateway_err_path,
    gateway_log_path,
    get_running_pid,
)


def _cmd_setup(args: argparse.Namespace) -> int:
    from bridge.gateway.setup.wizard import run_full_setup, run_slack_setup

    target = (args.target or "").lower()
    if not target or target == "slack":
        return run_slack_setup()
    if target == "full":
        return run_full_setup()
    print(f"unknown setup target: {target}", file=sys.stderr)
    print("supported: slack, full", file=sys.stderr)
    return 2


def _cmd_gateway_run(args: argparse.Namespace) -> int:
    from bridge.gateway.run import main as gateway_main

    gateway_args: list[str] = []
    if args.replace:
        gateway_args.append("--replace")
    return gateway_main(gateway_args)


def _cmd_gateway_install(args: argparse.Namespace) -> int:
    if sys.platform != "darwin":
        print("install is macOS-only; on Linux/Windows run `freyja gateway run` "
              "under your service manager", file=sys.stderr)
        return 1
    from bridge.gateway.setup import launchd
    try:
        path = launchd.install()
    except subprocess.CalledProcessError as exc:
        print(f"launchctl load failed: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001
        print(f"install failed: {exc}", file=sys.stderr)
        return 1
    print(f"installed plist at {path}")
    # Wait briefly for the daemon to register its PID.
    for _ in range(30):
        pid = get_running_pid()
        if pid:
            print(f"gateway running (pid {pid})")
            return 0
        time.sleep(0.1)
    print("gateway didn't register a PID within 3s — check `freyja gateway logs`",
          file=sys.stderr)
    return 1


def _cmd_gateway_uninstall(args: argparse.Namespace) -> int:
    if sys.platform != "darwin":
        print("uninstall is macOS-only", file=sys.stderr)
        return 1
    from bridge.gateway.setup import launchd
    ok = launchd.uninstall()
    if ok:
        print("uninstalled")
        return 0
    print("uninstall encountered an issue (plist may already be gone)",
          file=sys.stderr)
    return 1


def _cmd_gateway_start(args: argparse.Namespace) -> int:
    if sys.platform != "darwin":
        return _cmd_gateway_run(args)
    from bridge.gateway.setup import launchd
    if not launchd.is_installed():
        print("gateway is not installed — run `freyja gateway install` first",
              file=sys.stderr)
        return 1
    ok = launchd.start_via_launchctl()
    print("started" if ok else "launchctl start returned non-zero (already running?)")
    return 0 if ok else 1


def _cmd_gateway_stop(args: argparse.Namespace) -> int:
    if sys.platform != "darwin":
        pid = get_running_pid()
        if not pid:
            print("no gateway running")
            return 0
        import os, signal
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError as exc:
            print(f"could not signal pid {pid}: {exc}", file=sys.stderr)
            return 1
        print(f"signaled pid {pid}")
        return 0
    from bridge.gateway.setup import launchd
    if not launchd.is_installed():
        # Fall back to direct signaling
        pid = get_running_pid()
        if not pid:
            print("no gateway running")
            return 0
        import os, signal
        os.kill(pid, signal.SIGTERM)
        print(f"signaled pid {pid}")
        return 0
    ok = launchd.stop_via_launchctl()
    print("stopped" if ok else "launchctl stop returned non-zero")
    return 0 if ok else 1


def _cmd_gateway_status(args: argparse.Namespace) -> int:
    from bridge.gateway.setup import launchd as launchd_mod

    pid = get_running_pid()
    info: dict[str, object] = {
        "home": str(freyja_home()),
        "running_pid": pid,
        "log_path": str(gateway_log_path()),
        "err_path": str(gateway_err_path()),
    }
    if sys.platform == "darwin":
        info["plist_installed"] = launchd_mod.is_installed()
        info["plist_path"] = str(launchd_mod.plist_path())

    if args.json:
        print(json.dumps(info, indent=2))
        return 0

    print(f"freyja home    : {info['home']}")
    if pid:
        print(f"gateway pid    : {pid}  (running)")
    else:
        print("gateway pid    : -    (not running)")
    if sys.platform == "darwin":
        print(f"plist          : {info['plist_path']}")
        print(f"plist installed: {info['plist_installed']}")
    print(f"log file       : {info['log_path']}")
    print(f"err file       : {info['err_path']}")
    return 0


def _cmd_gateway_logs(args: argparse.Namespace) -> int:
    path = gateway_log_path()
    if not path.exists():
        print(f"log file does not exist yet: {path}", file=sys.stderr)
        return 1
    if args.follow:
        # tail -F follows rotations gracefully; tail -f does too on macOS
        try:
            return subprocess.call(["tail", "-F", str(path)])
        except KeyboardInterrupt:
            return 0
    return subprocess.call(["tail", "-n", str(args.lines), str(path)])


def _cmd_slack_manifest(args: argparse.Namespace) -> int:
    from bridge.gateway.platforms.slack_manifest import (
        manifest_json,
        write_manifest,
    )
    if args.write:
        path = write_manifest()
        print(f"wrote {path}")
        return 0
    print(manifest_json())
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="freyja",
        description="Freyja gateway CLI",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    # ── setup ──
    p_setup = sub.add_parser("setup", help="Run an interactive setup wizard")
    p_setup.add_argument(
        "target", nargs="?", default="slack",
        help="What to set up (slack | full). Default: slack",
    )
    p_setup.set_defaults(func=_cmd_setup)

    # ── gateway ──
    p_gw = sub.add_parser("gateway", help="Manage the gateway daemon")
    gw_sub = p_gw.add_subparsers(dest="gw_cmd", required=True)

    g_run = gw_sub.add_parser("run", help="Run the gateway in foreground")
    g_run.add_argument("--replace", action="store_true",
                       help="Take over from any running gateway")
    g_run.set_defaults(func=_cmd_gateway_run)

    g_install = gw_sub.add_parser("install",
                                  help="Install + start the launchd service")
    g_install.set_defaults(func=_cmd_gateway_install)

    g_uninstall = gw_sub.add_parser("uninstall",
                                    help="Stop + remove the launchd service")
    g_uninstall.set_defaults(func=_cmd_gateway_uninstall)

    g_start = gw_sub.add_parser("start", help="Start the installed service")
    g_start.set_defaults(func=_cmd_gateway_start, replace=False)

    g_stop = gw_sub.add_parser("stop", help="Stop the running service")
    g_stop.set_defaults(func=_cmd_gateway_stop)

    g_status = gw_sub.add_parser("status", help="Show daemon status")
    g_status.add_argument("--json", action="store_true",
                          help="Emit status as JSON")
    g_status.set_defaults(func=_cmd_gateway_status)

    g_logs = gw_sub.add_parser("logs", help="Tail the gateway log")
    g_logs.add_argument("-f", "--follow", action="store_true",
                        help="Follow the log (tail -F)")
    g_logs.add_argument("-n", "--lines", type=int, default=200,
                        help="Lines from the tail when not following")
    g_logs.set_defaults(func=_cmd_gateway_logs)

    # ── slack ──
    p_slack = sub.add_parser("slack", help="Slack-specific helpers")
    slack_sub = p_slack.add_subparsers(dest="slack_cmd", required=True)

    s_man = slack_sub.add_parser("manifest", help="Print or write the manifest")
    s_man.add_argument("--write", action="store_true",
                       help="Write to ~/.freyja/slack-manifest.json")
    s_man.set_defaults(func=_cmd_slack_manifest)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    func = getattr(args, "func", None)
    if func is None:
        parser.print_help()
        return 2
    try:
        return func(args)
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
