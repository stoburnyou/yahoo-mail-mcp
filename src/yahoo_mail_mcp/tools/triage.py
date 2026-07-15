"""Ongoing triage: incremental scan of new mail, with suggestions and flags."""

from __future__ import annotations

from dataclasses import asdict

from mcp.server.fastmcp import FastMCP

from ..app import AppContext
from ..imap.scanner import scan_mailbox as run_scan
from ..store.db import utcnow


def register(mcp: FastMCP, ctx: AppContext) -> None:
    @mcp.tool()
    def triage_new_mail(account: str, folders: list[str] | None = None) -> dict:
        """Incrementally scan new mail since the last scan and triage it.

        Never deletes or unsubscribes anything. Returns:
        - suggestions: new messages from domains you already tagged
          (keep/delete/unsubscribe), grouped by decision
        - new_senders: domains never seen before this run, for review
        - attention: new messages that look personal (no List-Unsubscribe
          header), which usually deserve a human look
        """
        acct = ctx.account(account)
        imap = ctx.imap(account)

        known_domains = {
            row[0]
            for row in ctx.store.conn.execute(
                "SELECT DISTINCT sender_domain FROM messages WHERE sender_domain IS NOT NULL"
            )
        }

        started_at = utcnow()
        report = run_scan(imap, ctx.store, acct.name, folders, ctx.settings.batch_size)

        new_rows = ctx.store.conn.execute(
            """
            SELECT m.folder, m.uid, m.sender_email, m.sender_domain, m.sender_name,
                   m.subject, m.date, m.list_unsub_raw, d.decision
            FROM messages m
            LEFT JOIN decisions d ON d.sender_domain = m.sender_domain
            WHERE m.account = ? AND m.scanned_at >= ? AND m.deleted_at IS NULL
            ORDER BY m.date DESC
            """,
            (acct.name, started_at),
        ).fetchall()

        suggestions: dict[str, list[dict]] = {"keep": [], "delete": [], "unsubscribe": []}
        new_senders: dict[str, dict] = {}
        attention: list[dict] = []

        for row in new_rows:
            summary = {
                "folder": row["folder"],
                "from": row["sender_email"],
                "subject": row["subject"],
                "date": row["date"],
            }
            decision = row["decision"]
            domain = row["sender_domain"]

            if decision in suggestions:
                suggestions[decision].append({**summary, "domain": domain})
            elif domain and domain not in known_domains:
                entry = new_senders.setdefault(
                    domain, {"domain": domain, "count": 0, "samples": []}
                )
                entry["count"] += 1
                if len(entry["samples"]) < 3:
                    entry["samples"].append(summary)

            if not row["list_unsub_raw"]:
                attention.append(summary)

        ctx.store.log_action(
            "triage", account=acct.name, count=len(new_rows),
            detail=f"{len(new_senders)} new senders, {len(attention)} flagged",
        )

        return {
            "account": acct.name,
            "new_messages": len(new_rows),
            "scan": [asdict(f) for f in report.folders],
            "suggestions": {
                k: {"count": len(v), "messages": v[:25]} for k, v in suggestions.items()
            },
            "new_senders": sorted(new_senders.values(), key=lambda e: -e["count"]),
            "attention": attention[:50],
            "note": (
                "Suggestions are advisory only. To act on 'delete' or 'unsubscribe' "
                "suggestions, run preview_cleanup and execute_decisions."
            ),
        }
