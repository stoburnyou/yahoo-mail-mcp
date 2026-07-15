"""Read-only tools for browsing scanned message headers."""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from ..analysis import messages, unsubscribe_candidates
from ..app import AppContext
from ..store.db import VALID_DECISIONS
from .account_scope import resolve_account
from .annotations import READ_ONLY_LOCAL


def _page_result(page: tuple[list[dict], int, int, int]) -> dict:
    rows, total, limit, offset = page
    return {
        "count": len(rows),
        "total": total,
        "limit": limit,
        "offset": offset,
        "messages": rows,
    }


def register(mcp: FastMCP, ctx: AppContext) -> None:
    @mcp.tool(annotations=READ_ONLY_LOCAL)
    def list_recent_messages(
        account: str | None = None,
        folder: str | None = None,
        since: str | None = None,
        limit: int = 50,
        offset: int = 0,
        include_sender_email: bool = False,
    ) -> dict:
        """List recently scanned messages using cached headers only.

        Run scan_mailbox first. Results include account, folder, UID,
        UIDVALIDITY and a staleness field. Sender email is hidden by default;
        raw unsubscribe links and message bodies are never returned.
        """
        account_name = ctx.account(account).name if account else None
        return _page_result(
            messages.list_recent_messages(
                ctx.store.conn,
                account=account_name,
                folder=folder,
                since=since,
                limit=limit,
                offset=offset,
                include_sender_email=include_sender_email,
            )
        )

    @mcp.tool(annotations=READ_ONLY_LOCAL)
    def search_messages(
        query: str | None = None,
        sender_domain: str | None = None,
        sender_email: str | None = None,
        subject_contains: str | None = None,
        account: str | None = None,
        folder: str | None = None,
        since: str | None = None,
        until: str | None = None,
        has_unsubscribe: bool | None = None,
        decision: str | None = None,
        sort: str = "recent",
        limit: int = 50,
        offset: int = 0,
        include_sender_email: bool = False,
        exclude_stale: bool = False,
    ) -> dict:
        """Search cached message headers without connecting to IMAP.

        Free-text query matches subject, sender domain and sender email.
        Additional exact filters can narrow account, folder, sender, date,
        unsubscribe availability and decision. List results never expose raw
        unsubscribe URLs or message bodies.
        """
        if decision is not None and decision not in VALID_DECISIONS:
            return {
                "error": f"decision must be one of {VALID_DECISIONS}",
                "count": 0,
                "total": 0,
                "messages": [],
            }
        account_name = ctx.account(account).name if account else None
        return _page_result(
            messages.search_messages(
                ctx.store.conn,
                query=query,
                sender_domain=sender_domain,
                sender_email=sender_email,
                subject_contains=subject_contains,
                account=account_name,
                folder=folder,
                since=since,
                until=until,
                has_unsubscribe=has_unsubscribe,
                decision=decision,
                sort=sort,
                limit=limit,
                offset=offset,
                include_sender_email=include_sender_email,
                exclude_stale=exclude_stale,
            )
        )

    @mcp.tool(annotations=READ_ONLY_LOCAL)
    def list_unsubscribe_candidates(
        account: str | None = None,
        method: str = "all",
        sort: str = "count",
        limit: int = 50,
        offset: int = 0,
    ) -> dict:
        """List sender domains with cached unsubscribe methods for one account.

        `method` is all, one_click, mailto, or manual. One-click candidates
        advertise RFC 8058 support and an HTTPS endpoint, but final DNS, domain,
        and network safety validation occurs only during execute_decisions.
        Raw unsubscribe URLs and sender addresses are never returned.
        """
        try:
            account_name = resolve_account(ctx, account)
            page, total, page_limit, page_offset = (
                unsubscribe_candidates.list_unsubscribe_candidates(
                    ctx.store.conn,
                    account=account_name,
                    method=method,
                    sort=sort,
                    limit=limit,
                    offset=offset,
                )
            )
        except (KeyError, ValueError) as exc:
            return {
                "error": str(exc),
                "count": 0,
                "total": 0,
                "candidates": [],
            }
        return {
            "account": account_name,
            "method": method,
            "count": len(page),
            "total": total,
            "limit": page_limit,
            "offset": page_offset,
            "candidates": page,
            "safety_note": (
                "One-click entries are advertised candidates. Endpoint and DNS "
                "safety are revalidated during execution."
            ),
        }

    @mcp.tool(annotations=READ_ONLY_LOCAL)
    def get_message_headers(account: str, folder: str, uid: int) -> dict:
        """Get the cached headers for one exact account/folder/UID reference.

        Includes sender, subject, date, size, decision and safe unsubscribe
        method names. It never returns raw unsubscribe links or a message body.
        """
        account_name = ctx.account(account).name
        detail = messages.get_message_headers(
            ctx.store.conn, account=account_name, folder=folder, uid=uid
        )
        if detail is None:
            return {
                "error": (
                    f"No scanned message for account {account_name!r}, folder {folder!r}, UID {uid}"
                )
            }
        return detail
