"""MCP server entrypoint (stdio transport)."""

from __future__ import annotations

import logging
import os
import sys

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from .app import AppContext
from .tools import browse, execute, mail_actions, review, scan, triage


def build_server(ctx: AppContext | None = None, *, remote: bool = False) -> FastMCP:
    ctx = ctx or AppContext()

    if remote:
        instructions = (
            "Yahoo Mail assistant tools. Read/search operations are safe by default. "
            "Outbound email and mailbox mutations use an explicit two-step "
            "preview -> user approval -> execute flow. Trash is recoverable and "
            "permanent expunge is not exposed. Attachments may be supplied using "
            "short-lived Dropbox download URLs."
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

    if remote:
        # Hosted write tools are deliberately narrow and require preview tokens.
        mail_actions.register(mcp, ctx)
        triage.register(mcp, ctx)
    else:
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
