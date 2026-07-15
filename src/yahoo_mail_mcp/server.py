"""MCP server entrypoint (stdio transport)."""

from __future__ import annotations

import logging
import os
import sys

from mcp.server.fastmcp import FastMCP

from .app import AppContext
from .tools import browse, execute, review, scan, triage


def build_server(ctx: AppContext | None = None) -> FastMCP:
    ctx = ctx or AppContext()
    mcp = FastMCP(
        "yahoo-mail-mcp",
        instructions=(
            "Tools for auditing and cleaning up Yahoo Mail accounts over IMAP. "
            "Typical flow: list_accounts -> scan_mailbox -> list_recent_messages "
            "or search_messages -> list_sender_groups -> "
            "set_decisions (or export/import CSV) -> preview_cleanup -> execute_decisions. "
            "Nothing is deleted or unsubscribed until execute_decisions runs on "
            "explicitly tagged domains."
        ),
    )
    scan.register(mcp, ctx)
    browse.register(mcp, ctx)
    review.register(mcp, ctx)
    execute.register(mcp, ctx)
    triage.register(mcp, ctx)
    return mcp


def main() -> None:
    level_name = os.environ.get("YAHOO_MAIL_MCP_LOG_LEVEL", "WARNING").upper()
    level = getattr(logging, level_name, logging.WARNING)
    logging.basicConfig(
        level=level,
        stream=sys.stderr,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    build_server().run()


if __name__ == "__main__":
    main()
