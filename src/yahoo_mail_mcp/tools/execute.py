"""Execution tools: preview_cleanup and execute_decisions.

execute_decisions is the only tool in the server that mutates mailboxes,
and it only acts on domains explicitly tagged via set_decisions /
import_review_csv.
"""

from __future__ import annotations

import logging
from collections import defaultdict

from mcp.server.fastmcp import FastMCP

from .. import safety
from ..analysis.headers import registrable_domain
from ..app import AppContext
from ..imap.actions import move_to_trash
from ..imap.client import YahooImapError
from ..imap.scanner import _parse_fetch_responses, _raw_uid_fetch
from ..unsubscribe import unsubscribe_domain

logger = logging.getLogger(__name__)

SPOT_CHECK_SAMPLE = 3


def _spot_check(imap, account: str, folder: str, rows: list) -> str | None:
    """Fetch a few of the messages about to be deleted and verify their From
    domain still matches what we scanned. Returns an error string on mismatch."""
    sample = rows[:: max(1, len(rows) // SPOT_CHECK_SAMPLE)][:SPOT_CHECK_SAMPLE]
    for row in sample:
        responses = imap.with_retry(
            f"spot-check {folder} uid {row['uid']}",
            lambda uid=row["uid"]: _raw_uid_fetch(imap, f"{uid}:{uid}"),
        )
        records = _parse_fetch_responses(responses)
        if not records:
            continue  # message already gone; the MOVE will simply skip it
        live_domain = records[0]["sender_domain"]
        expected = row["sender_domain"]
        if live_domain and live_domain != expected:
            return (
                f"Spot check failed in {account}/{folder}: UID {row['uid']} is now from "
                f"{live_domain!r}, expected {expected!r}. UIDs may be stale - rescan this folder."
            )
    return None


def register(mcp: FastMCP, ctx: AppContext) -> None:
    @mcp.tool()
    def preview_cleanup(decision: str = "delete") -> dict:
        """Dry run for execute_decisions: exact counts of affected messages
        per domain and per account, plus a confirm_token.

        Nothing is modified. The token is required by execute_decisions when
        the total exceeds the configured safety threshold.
        """
        if decision not in ("delete", "unsubscribe"):
            return {"error": "decision must be 'delete' or 'unsubscribe'"}
        result = safety.preview(ctx.store, decision)
        result["threshold"] = ctx.settings.delete_threshold
        result["confirm_required"] = result["total_messages"] > ctx.settings.delete_threshold
        return result

    @mcp.tool()
    def execute_decisions(decision: str, confirm_token: str | None = None) -> dict:
        """Execute tagged decisions. THE ONLY DESTRUCTIVE TOOL.

        decision="delete": moves all messages from domains tagged 'delete' to
        Trash (recoverable). If the total exceeds the safety threshold, a
        confirm_token from a fresh preview_cleanup call is required.

        decision="unsubscribe": for each domain tagged 'unsubscribe', performs
        RFC 8058 one-click POST or sends a mailto unsubscribe email. Plain
        http links are never auto-fetched; they are returned as manual items.

        Domains tagged 'keep' or 'needs_review' are never touched.
        """
        if decision == "delete":
            return _execute_delete(confirm_token)
        if decision == "unsubscribe":
            return _execute_unsubscribe()
        return {"error": "decision must be 'delete' or 'unsubscribe'"}

    def _execute_delete(confirm_token: str | None) -> dict:
        rows = safety.pending_messages(ctx.store.conn, "delete")
        total = len(rows)
        if total == 0:
            return {"deleted": 0, "detail": "No messages pending deletion (tag domains first)."}

        if total > ctx.settings.delete_threshold:
            if not confirm_token:
                return {
                    "error": (
                        f"This would move {total} messages to Trash, above the safety "
                        f"threshold of {ctx.settings.delete_threshold}. Run preview_cleanup "
                        "to review the breakdown and pass its confirm_token to proceed."
                    ),
                    "total_messages": total,
                }
            err = safety.validate_token(ctx.store, confirm_token, "delete", total)
            if err:
                return {"error": err, "total_messages": total}

        by_target: dict[tuple[str, str, int], list] = defaultdict(list)
        for row in rows:
            by_target[(row["account"], row["folder"], row["uidvalidity"])].append(row)

        deleted = 0
        skipped: list[dict] = []
        for (account, folder, uidvalidity), target_rows in by_target.items():
            try:
                imap = ctx.imap(account)
            except KeyError as exc:
                skipped.append({"account": account, "folder": folder, "reason": str(exc)})
                continue
            try:
                check_err = _spot_check(imap, account, folder, target_rows)
                if check_err:
                    skipped.append({"account": account, "folder": folder, "reason": check_err})
                    continue
                moved = move_to_trash(
                    imap, ctx.store, account, folder,
                    [r["uid"] for r in target_rows], uidvalidity,
                )
                deleted += moved
                ctx.store.log_action(
                    "delete", account=account, folder=folder, count=moved, detail="moved to Trash"
                )
            except YahooImapError as exc:
                skipped.append({"account": account, "folder": folder, "reason": str(exc)})

        return {
            "deleted": deleted,
            "requested": total,
            "skipped": skipped,
            "note": "Messages were moved to Trash, not permanently deleted.",
        }

    def _execute_unsubscribe() -> dict:
        domains = [row["sender_domain"] for row in ctx.store.get_decisions("unsubscribe")]
        if not domains:
            return {"results": [], "detail": "No domains tagged 'unsubscribe'."}
        results = []
        for domain in domains:
            outcome = unsubscribe_domain(ctx.store, ctx.accounts_by_name, domain)
            results.append(
                {
                    "domain": outcome.sender_domain,
                    "method": outcome.method,
                    "ok": outcome.ok,
                    "detail": outcome.detail,
                }
            )
        succeeded = sum(1 for r in results if r["ok"])
        manual = [r for r in results if r["method"] == "manual"]
        return {
            "attempted": len(results),
            "succeeded": succeeded,
            "manual_action_needed": manual,
            "results": results,
        }
