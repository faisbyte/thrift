"""reprouter command line.

  reprouter proxy [--port P] [--mode on|record|passthrough] [--upstream URL]
  reprouter run [--mode M] [--port P] -- claude [args...]   # proxy + Claude Code
  reprouter off | on | status                                # kill switch
  reprouter hook                                             # PostToolUse hook entry
"""

from __future__ import annotations

import argparse
import os
import socket
import subprocess
import sys
import threading
from pathlib import Path

DEFAULT_PORT = 47900


def kill_file() -> Path:
    return Path(os.environ.get("REPROUTER_KILL_FILE", os.path.expanduser("~/.reprouter/off")))


def child_env(port: int) -> dict:
    env = dict(os.environ)
    env["ANTHROPIC_BASE_URL"] = f"http://127.0.0.1:{port}"
    return env


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _proxy_config(args):
    from .config import build_policy
    from .proxy.server import ProxyConfig

    kw = {"state_file": Path(args.state_file)} if getattr(args, "state_file", None) else {}
    return ProxyConfig(upstream=args.upstream, mode=args.mode, policy=build_policy(),
                       corpus_dir=Path(args.corpus_dir), kill_file=kill_file(),
                       canary=bool(getattr(args, "canary", False)), **kw)


def _serve(cfg, port: int, ready: threading.Event | None = None) -> None:
    import asyncio

    from aiohttp import web

    from .proxy.server import make_app

    async def go():
        runner = web.AppRunner(make_app(cfg))
        await runner.setup()
        await web.TCPSite(runner, "127.0.0.1", port).start()
        if ready:
            ready.set()
        await asyncio.Event().wait()

    asyncio.run(go())


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="reprouter")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("proxy", "run"):
        p = sub.add_parser(name)
        p.add_argument("--port", type=int, default=DEFAULT_PORT if name == "proxy" else 0)
        p.add_argument("--mode", default=os.environ.get("REPROUTER_MODE", "on"),
                       choices=["off", "passthrough", "record", "on"])
        p.add_argument("--upstream", default=os.environ.get("REPROUTER_UPSTREAM", "https://api.anthropic.com"))
        p.add_argument("--corpus-dir", default=os.environ.get("REPROUTER_CORPUS", "corpus/proxy"))
        p.add_argument("--state-file", default=os.environ.get("REPROUTER_STATE"))
        p.add_argument("--canary", action="store_true",
                       help="test runs: make any history edit fail loudly (preserved-thinking binding)")
        if name == "run":
            p.add_argument("command", nargs=argparse.REMAINDER)
    for name in ("off", "on", "status", "hook"):
        sub.add_parser(name)
    return ap


def main(argv: list[str] | None = None) -> int:
    from .tokens import load_dotenv

    load_dotenv()
    ap = build_parser()
    args = ap.parse_args(argv)

    if args.cmd == "off":
        kill_file().parent.mkdir(parents=True, exist_ok=True)
        kill_file().write_text("disabled by `reprouter off`\n")
        print(f"reprouter OFF (kill file {kill_file()}); new blocks pass through untouched")
        return 0
    if args.cmd == "on":
        kill_file().unlink(missing_ok=True)
        print("reprouter ON")
        return 0
    if args.cmd == "status":
        state = "OFF" if kill_file().exists() else "ON"
        print(f"reprouter {state} (mode {os.environ.get('REPROUTER_MODE', 'on')}, kill file {kill_file()})")
        return 0
    if args.cmd == "hook":
        from .hook import main as hook_main

        return hook_main([])
    if args.cmd == "proxy":
        print(f"reprouter proxy on http://127.0.0.1:{args.port} (mode {args.mode}); "
              f"set ANTHROPIC_BASE_URL=http://127.0.0.1:{args.port}", file=sys.stderr)
        _serve(_proxy_config(args), args.port)
        return 0
    if args.cmd == "run":
        cmd = [c for c in args.command if c != "--"]
        if not cmd:
            ap.error("run needs a command, e.g. reprouter run -- claude")
        port = args.port or _free_port()
        ready = threading.Event()
        threading.Thread(target=_serve, args=(_proxy_config(args), port, ready), daemon=True).start()
        ready.wait(10)
        return subprocess.call(cmd, env=child_env(port))
    return 2


if __name__ == "__main__":
    sys.exit(main())
