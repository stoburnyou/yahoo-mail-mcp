"""MCP server entrypoint (stdio transport)."""

from __future__ import annotations

import logging
import sys

from mcp.server.fastmcp import FastMCP

from .app import AppContext
from .tools import execute, review, scan, triage


def build_server(ctx: AppContext | None = None) -> FastMCP:
    ctx = ctx or AppContext()
    mcp = FastMCP(
        "yahoo-mail-mcp",
        instructions=(
            "Tools for auditing and cleaning up Yahoo Mail accounts over IMAP. "
            "Typical flow: list_accounts -> scan_mailbox -> list_sender_groups -> "
            "set_decisions (or export/import CSV) -> preview_cleanup -> execute_decisions. "
            "Nothing is deleted or unsubscribed until execute_decisions runs on "
            "explicitly tagged domains."
        ),
    )
    scan.register(mcp, ctx)
    review.register(mcp, ctx)
    execute.register(mcp, ctx)
    triage.register(mcp, ctx)
    return mcp


def main() -> None:
    logging.basicConfig(level=logging.INFO, stream=sys.stderr,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")
    build_server().run()


if __name__ == "__main__":
    main()
