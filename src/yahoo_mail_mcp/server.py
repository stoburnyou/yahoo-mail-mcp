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
    decision_step = "set_decisions" if remote else "set_decisions (or export/import CSV)"
    mcp = FastMCP(
        "yahoo-mail-mcp",
        instructions=(
            "Tools for auditing and cleaning up Yahoo Mail accounts over IMAP. "
            "Typical flow: list_accounts -> scan_mailbox (or start_scan_job for "
            "remote, long-running scans) -> list_recent_messages "
            "or search_messages -> list_sender_groups -> "
            f"{decision_step} -> preview_cleanup -> execute_decisions. "
            "Nothing is archived, deleted, or unsubscribed until execute_decisions runs on "
            "explicitly tagged account/domain pairs. Always name the account before "
            "previewing or executing a mutation."
        ),
        stateless_http=remote,
        json_response=remote,
        # The HTTP entrypoint enforces an explicit Host allowlist and bearer
        # token before requests reach MCP.
        transport_security=(
            TransportSecuritySettings(enable_dns_rebinding_protection=False) if remote else None
        ),
    )
    scan.register(mcp, ctx)
    browse.register(mcp, ctx)
    review.register(mcp, ctx, include_file_tools=not remote)
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
