"""MCP server (stdio) exposing the thrift index to Claude Code."""

from __future__ import annotations

import logging
import sys

from mcp.server.fastmcp import FastMCP

from .service import Service, resolve_root

logging.basicConfig(stream=sys.stderr, level=logging.WARNING)

INSTRUCTIONS = (
    "thrift is a local search index of this project's code (symbols, files, imports). "
    "Use it to locate code before reading files: search_code for concepts or identifiers, "
    "get_symbol for a known definition, related_files for import neighbours. Results give "
    "1-based line ranges, so a follow-up Read can use offset/limit instead of reading whole files."
)

mcp = FastMCP("thrift", instructions=INSTRUCTIONS)
_service: Service | None = None


def service() -> Service:
    global _service
    if _service is None:
        _service = Service(resolve_root())
    return _service


@mcp.tool()
def search_code(query: str, k: int = 8) -> str:
    """Find where something is implemented in this project.

    Ranked search over functions, classes, methods and other code chunks
    (BM25 over identifiers, paths, signatures and bodies). Use it before
    Grep/Read when you need to locate code by concept ("retry backoff for
    http client") or identifier ("parseConfig"). Returns up to k results
    (default 8, max 20), each `path:Lstart-end kind name signature` plus a
    short snippet. Read only the line ranges you need.
    """
    return service().search(query, k)


@mcp.tool()
def get_symbol(name: str) -> str:
    """Show the full definition of a function, class, method or type by name.

    Accepts `name`, `Class.method`, `ns::Type::fn`, or `path/to/file.py:name`
    to disambiguate. Long definitions are truncated with an offset/limit hint;
    ambiguous names return a candidate list instead of code.
    """
    return service().symbol(name)


@mcp.tool()
def related_files(path: str) -> str:
    """List files linked to `path` by imports: what it imports and what imports it.

    Useful to find callers, implementations or tests of a module before
    editing it. Accepts a repo-relative or absolute path.
    """
    return service().related(path)


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
