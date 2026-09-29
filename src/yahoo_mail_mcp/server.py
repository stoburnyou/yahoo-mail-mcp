"""MCP server entrypoint (stdio transport)."""

from __future__ import annotations

import logging
import os
import sys

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from .app import AppContext
from .tools import browse, execute, review, scan, triage


def build_server(ctx: AppContext | None = None, *, remote: bool = False) -> FastMCP:
    ctx = ctx or AppContext()

    if remote:
        instructions = (
            "Read-only Yahoo Mail tools. Use list_accounts, scan tools, "
            "list_recent_messages/search_messages, get_message_headers, and "
            "get_message_body. Remote mode intentionally exposes no mailbox "
            "mutation, archive, delete, or unsubscribe execution tools."
        )
    else:
        instructions = (
            "Tools for auditing and cleaning up Yahoo Mail accounts over IMAP. "
            "Typical flow: list_accounts -> scan_mailbox -> browse/review -> "
            "preview_cleanup -> execute_decisions. Mailbox mutations require "
            "explicit local execution."
        )

    mcp = FastMCP(
        "yahoo-mail-mcp",
        instructions=instructions,
        stateless_http=remote,
        json_response=remote,
        transport_security=(
            TransportSecuritySettings(enable_dns_rebinding_protection=False) if remote else None
        ),
    )

    # Safe read-only tools are available in both local and hosted modes.
    scan.register(mcp, ctx)
    browse.register(mcp, ctx)

    # Hosted ChatGPT access is intentionally read-only. Keep all mutation
    # and decision-execution tooling local unless explicitly redesigned later.
    if not remote:
        review.register(mcp, ctx, include_file_tools=True)
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
