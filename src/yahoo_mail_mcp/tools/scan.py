"""Scanning tools: list_accounts, scan_mailbox, get_scan_status."""

from __future__ import annotations

from dataclasses import asdict

from mcp.server.fastmcp import FastMCP

from ..app import AppContext
from ..imap.scanner import scan_mailbox as run_scan
from ..jobs import JOB_STATUSES, scan_job_record
from .annotations import READ_ONLY_LOCAL, READ_ONLY_REMOTE


def _safe_connection_error(exc: Exception) -> str:
    message = str(exc).lower()
    if "authentication" in message or "invalid credentials" in message:
        return "Authentication failed. Verify the Yahoo email and app password."
    if "timeout" in message or "timed out" in message:
        return "Yahoo IMAP connection timed out. Try again later."
    return "Yahoo IMAP connection failed. Check the server logs for details."


def register(mcp: FastMCP, ctx: AppContext) -> None:
    @mcp.tool(annotations=READ_ONLY_REMOTE)
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
                entry["error"] = _safe_connection_error(exc)
            out.append(entry)
        return {"accounts": out}

    @mcp.tool(annotations=READ_ONLY_REMOTE)
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
        acct = ctx.account(account)
        try:
            imap = ctx.imap(account)
        except Exception as exc:  # noqa: BLE001 - return a privacy-safe tool error
            return {
                "account": acct.name,
                "error": _safe_connection_error(exc),
                "total_scanned_this_run": 0,
                "folders": [],
                "messages_in_database": ctx.store.message_count(acct.name),
            }
        report = run_scan(
            imap, ctx.store, acct.name, folders, ctx.settings.batch_size, max_messages
        )
        return {
            "account": acct.name,
            "total_scanned_this_run": report.total_scanned,
            "folders": [asdict(f) for f in report.folders],
            "messages_in_database": ctx.store.message_count(acct.name),
        }

    @mcp.tool(annotations=READ_ONLY_LOCAL)
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

    @mcp.tool(annotations=READ_ONLY_REMOTE)
    def start_scan_job(
        account: str,
        folders: list[str] | None = None,
        max_messages: int | None = None,
    ) -> dict:
        """Start a durable background header scan and return immediately.

        Use this for large scans from remote clients that impose request
        timeouts. Poll get_scan_job with the returned job_id. If the server
        restarts, starting a new job resumes from the stored IMAP checkpoints.
        """
        acct = ctx.account(account)
        if max_messages is not None and max_messages < 1:
            return {"error": "max_messages must be at least 1"}
        if folders is not None and (
            not folders or any(not isinstance(folder, str) or not folder for folder in folders)
        ):
            return {"error": "folders must be a non-empty list of folder names"}
        return ctx.scan_jobs.start(acct.name, folders, max_messages)

    @mcp.tool(annotations=READ_ONLY_LOCAL)
    def get_scan_job(job_id: str) -> dict:
        """Get status and, when complete, the result of one background scan."""
        row = ctx.store.get_scan_job(job_id)
        if row is None:
            return {"error": f"No scan job found for {job_id!r}"}
        return scan_job_record(row)

    @mcp.tool(annotations=READ_ONLY_LOCAL)
    def list_scan_jobs(status: str | None = None, limit: int = 20) -> dict:
        """List recent background scans, optionally filtered by status."""
        if status is not None and status not in JOB_STATUSES:
            return {"error": f"status must be one of {JOB_STATUSES}"}
        if limit < 1 or limit > 100:
            return {"error": "limit must be between 1 and 100"}
        jobs = [scan_job_record(row) for row in ctx.store.list_scan_jobs(limit, status)]
        return {"count": len(jobs), "jobs": jobs}
