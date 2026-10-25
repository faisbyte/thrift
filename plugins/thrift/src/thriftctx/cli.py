"""Command line: thriftctx index|search|symbol|related|stats|hook."""

from __future__ import annotations

import argparse
import sys

from .indexer import index_project
from .service import Service, resolve_root


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="thriftctx", description="Local code retrieval index for Claude Code")
    ap.add_argument("--root", help="project root (default: $CLAUDE_PROJECT_DIR, nearest index, or cwd)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("index", help="build or incrementally refresh the index")
    p.add_argument("path", nargs="?", help="project root to index")
    p.add_argument("--full", action="store_true", help="rebuild from scratch")
    p.add_argument("--quiet", action="store_true", help="no output unless something failed")
    p.add_argument("--wait", type=float, default=10.0, help="seconds to wait for another indexer")

    p = sub.add_parser("search", help="search the index")
    p.add_argument("query", nargs="+")
    p.add_argument("-k", type=int, default=8)

    p = sub.add_parser("symbol", help="show a symbol's definition")
    p.add_argument("name")

    p = sub.add_parser("related", help="files linked to a file by imports")
    p.add_argument("path")

    sub.add_parser("stats", help="index statistics")

    args = ap.parse_args(argv)

    if args.cmd == "index":
        root = resolve_root(args.path or args.root)
        stats = index_project(root, full=args.full, lock_wait=args.wait)
        if not args.quiet or stats.busy:
            print(stats.summary())
        return 1 if stats.busy else 0

    svc = Service(resolve_root(args.root))
    if args.cmd == "search":
        print(svc.search(" ".join(args.query), args.k))
    elif args.cmd == "symbol":
        print(svc.symbol(args.name))
    elif args.cmd == "related":
        print(svc.related(args.path))
    elif args.cmd == "stats":
        print(svc.stats())
    return 0


if __name__ == "__main__":
    sys.exit(main())
