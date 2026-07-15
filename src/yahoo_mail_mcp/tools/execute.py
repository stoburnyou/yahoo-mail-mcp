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
from ..app import AppContext
from ..imap.actions import move_to_archive, move_to_trash
from ..imap.client import YahooImapError
from ..imap.scanner import _parse_fetch_responses, _raw_uid_fetch
from ..unsubscribe import unsubscribe_domain
from .annotations import DESTRUCTIVE_REMOTE, READ_ONLY_LOCAL

logger = logging.getLogger(__name__)

SPOT_CHECK_SAMPLE = 3


def _spot_check(
    imap,
    account: str,
    folder: str,
    rows: list,
    expected_uidvalidity: int,
) -> str | None:
    """Fetch a few of the messages about to be moved and verify their From
    domain still matches what we scanned. Returns an error string on mismatch."""
    info = imap.with_retry(
        f"select {folder} for spot check",
        lambda: imap.select_folder(folder, readonly=True),
    )
    live_uidvalidity = int(info[b"UIDVALIDITY"])
    if live_uidvalidity != expected_uidvalidity:
        return (
            f"UIDVALIDITY changed for {account}/{folder} "
            f"(scanned {expected_uidvalidity}, live {live_uidvalidity}). Rescan first."
        )
    sample = rows[:: max(1, len(rows) // SPOT_CHECK_SAMPLE)][:SPOT_CHECK_SAMPLE]
    for row in sample:
        responses = imap.with_retry(
            f"spot-check {folder} uid {row['uid']}",
            lambda uid=row["uid"]: _raw_uid_fetch(imap, f"{uid}:{uid}"),
        )
        records = _parse_fetch_responses(responses)
        if not records:
            return (
                f"Spot check failed in {account}/{folder}: UID {row['uid']} "
                "is missing or unreadable. Rescan before moving messages."
            )
        live_domain = records[0]["sender_domain"]
        expected = row["sender_domain"]
        if live_domain != expected:
            return (
                f"Spot check failed in {account}/{folder}: UID {row['uid']} is now from "
                f"{live_domain!r}, expected {expected!r}. UIDs may be stale - rescan this folder."
            )
    return None


def register(mcp: FastMCP, ctx: AppContext) -> None:
    @mcp.tool(annotations=READ_ONLY_LOCAL)
    def preview_cleanup(decision: str = "delete") -> dict:
        """Dry run for execute_decisions: exact counts of affected messages
        per domain and per account, plus a confirm_token.

        Nothing in Yahoo Mail is modified. The token is required by
        execute_decisions for large Archive or Delete operations.
        """
        if decision not in ("archive", "delete", "unsubscribe"):
            return {"error": "decision must be 'archive', 'delete', or 'unsubscribe'"}
        result = safety.preview(ctx.store, decision)
        result["threshold"] = ctx.settings.delete_threshold
        result["confirm_required"] = (
            decision in ("archive", "delete")
            and result["total_messages"] > ctx.settings.delete_threshold
        )
        return result

    @mcp.tool(annotations=DESTRUCTIVE_REMOTE)
    def execute_decisions(decision: str, confirm_token: str | None = None) -> dict:
        """Execute tagged decisions. THE ONLY DESTRUCTIVE TOOL.

        decision="archive": moves messages from domains tagged 'archive' to the
        provider's Archive folder. decision="delete": moves tagged messages to
        Trash (recoverable). If either total exceeds the safety threshold, a
        confirm_token from a fresh preview_cleanup call is required.

        decision="unsubscribe": for each domain tagged 'unsubscribe', performs
        RFC 8058 one-click POST or sends a mailto unsubscribe email. Plain
        http links are never auto-fetched; they are returned as manual items.

        Domains tagged 'keep' or 'needs_review' are never touched.
        """
        if decision == "archive":
            return _execute_move("archive", confirm_token)
        if decision == "delete":
            return _execute_move("delete", confirm_token)
        if decision == "unsubscribe":
            return _execute_unsubscribe()
        return {"error": "decision must be 'archive', 'delete', or 'unsubscribe'"}

    def _execute_move(decision: str, confirm_token: str | None) -> dict:
        rows = safety.pending_messages(ctx.store.conn, decision)
        total = len(rows)
        result_key = "archived" if decision == "archive" else "deleted"
        destination = "Archive" if decision == "archive" else "Trash"
        if total == 0:
            return {
                result_key: 0,
                "detail": f"No messages pending {decision} (tag domains first).",
            }

        if total > ctx.settings.delete_threshold:
            if not confirm_token:
                return {
                    "error": (
                        f"This would move {total} messages to {destination}, above the safety "
                        f"threshold of {ctx.settings.delete_threshold}. Run preview_cleanup "
                        "to review the breakdown and pass its confirm_token to proceed."
                    ),
                    "total_messages": total,
                }
            err = safety.validate_token(ctx.store, confirm_token, decision, rows)
            if err:
                return {"error": err, "total_messages": total}

        by_target: dict[tuple[str, str, int], list] = defaultdict(list)
        for row in rows:
            by_target[(row["account"], row["folder"], row["uidvalidity"])].append(row)

        moved_total = 0
        skipped: list[dict] = []
        for (account, folder, uidvalidity), target_rows in by_target.items():
            try:
                imap = ctx.imap(account)
            except KeyError as exc:
                skipped.append({"account": account, "folder": folder, "reason": str(exc)})
                continue
            try:
                check_err = _spot_check(imap, account, folder, target_rows, uidvalidity)
                if check_err:
                    skipped.append({"account": account, "folder": folder, "reason": check_err})
                    continue
                move = move_to_archive if decision == "archive" else move_to_trash
                moved = move(
                    imap,
                    ctx.store,
                    account,
                    folder,
                    [r["uid"] for r in target_rows],
                    uidvalidity,
                )
                moved_total += moved
                ctx.store.log_action(
                    decision,
                    account=account,
                    folder=folder,
                    count=moved,
                    detail=f"moved to {destination}",
                )
            except YahooImapError as exc:
                skipped.append({"account": account, "folder": folder, "reason": str(exc)})

        return {
            result_key: moved_total,
            "requested": total,
            "skipped": skipped,
            "note": (
                "Messages were moved to Trash, not permanently deleted."
                if decision == "delete"
                else "Messages were moved out of the Inbox into Archive."
            ),
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
