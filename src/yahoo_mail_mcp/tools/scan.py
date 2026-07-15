"""Scanning tools: list_accounts, scan_mailbox, get_scan_status."""

from __future__ import annotations

from dataclasses import asdict

from mcp.server.fastmcp import FastMCP

from ..app import AppContext
from ..imap.scanner import scan_mailbox as run_scan


def register(mcp: FastMCP, ctx: AppContext) -> None:
    @mcp.tool()
    def list_accounts() -> dict:
        """List configured Yahoo accounts with their folders and message counts.

        Connects to each account over IMAP, so this also verifies that the
        app passwords work.
        """
        out = []
        for acct in ctx.settings.accounts:
            entry: dict = {"name": acct.name, "email": acct.email}
            try:
                imap = ctx.imap(acct.name)
                entry["message_limit"] = imap.message_limit
                entry["folders"] = [
                    {
                        "name": f.name,
                        "special_use": f.special_use,
                        "messages": f.messages,
                    }
                    for f in imap.list_folders()
                ]
            except Exception as exc:  # noqa: BLE001 - report per-account failures
                entry["error"] = str(exc)
            out.append(entry)
        return {"accounts": out}

    @mcp.tool()
    def scan_mailbox(
        account: str,
        folders: list[str] | None = None,
        max_messages: int | None = None,
    ) -> dict:
        """Scan a Yahoo account's folders, pulling header-level data only.

        Does all the heavy lifting internally: fetches sender, subject, date,
        size and List-Unsubscribe headers in batches, and persists them to the
        local database. Progress is checkpointed continuously, so if the scan
        is interrupted, calling this tool again resumes where it left off.
        Once a folder has been fully scanned, later calls only pick up new mail.

        By default scans all folders except Trash, Drafts and Sent. Pass
        `folders` to scan specific ones, or `max_messages` to cap this run
        (useful for a first validation pass).
        """
        imap = ctx.imap(account)
        acct = ctx.account(account)
        report = run_scan(imap, ctx.store, acct.name, folders, ctx.settings.batch_size, max_messages)
        return {
            "account": acct.name,
            "total_scanned_this_run": report.total_scanned,
            "folders": [asdict(f) for f in report.folders],
            "messages_in_database": ctx.store.message_count(acct.name),
        }

    @mcp.tool()
    def get_scan_status(account: str | None = None) -> dict:
        """Show scan progress per folder (from checkpoints), without connecting to IMAP."""
        sql = "SELECT * FROM checkpoints"
        params: list = []
        if account:
            sql += " WHERE account = ?"
            params.append(ctx.account(account).name)
        rows = [dict(r) for r in ctx.store.conn.execute(sql + " ORDER BY account, folder", params)]
        for row in rows:
            row["done"] = bool(row["done"])
        return {
            "checkpoints": rows,
            "messages_in_database": ctx.store.message_count(
                ctx.account(account).name if account else None
            ),
        }
