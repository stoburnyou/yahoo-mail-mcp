"""Read-only tools for browsing scanned messages."""

from __future__ import annotations

from email import policy
from email.parser import BytesParser
from html import unescape
from html.parser import HTMLParser

from mcp.server.fastmcp import FastMCP

from ..analysis import messages, unsubscribe_candidates
from ..app import AppContext
from ..store.db import VALID_DECISIONS
from .account_scope import resolve_account
from .annotations import READ_ONLY_LOCAL, READ_ONLY_REMOTE


class _TextHTMLParser(HTMLParser):
    """Small dependency-free HTML-to-text helper for email bodies."""

    BLOCK_TAGS = {
        "p",
        "div",
        "br",
        "li",
        "tr",
        "table",
        "section",
        "article",
        "header",
        "footer",
        "blockquote",
    }

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag.lower() == "br":
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in self.BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        self.parts.append(data)

    def text(self) -> str:
        lines = [" ".join(line.split()) for line in "".join(self.parts).splitlines()]
        return "\n".join(line for line in lines if line).strip()


def _decode_part(part) -> str:
    payload = part.get_payload(decode=True)
    if payload is None:
        return ""
    charset = part.get_content_charset() or "utf-8"
    try:
        return payload.decode(charset, errors="replace")
    except LookupError:
        return payload.decode("utf-8", errors="replace")


def _extract_message(raw: bytes, max_chars: int) -> dict:
    message = BytesParser(policy=policy.default).parsebytes(raw)
    plain_parts: list[str] = []
    html_parts: list[str] = []
    attachments: list[dict] = []

    parts = message.walk() if message.is_multipart() else [message]
    for part in parts:
        if part.is_multipart():
            continue

        content_type = part.get_content_type()
        filename = part.get_filename()
        disposition = (part.get_content_disposition() or "").lower()

        if disposition == "attachment" or filename:
            attachments.append(
                {
                    "filename": filename,
                    "content_type": content_type,
                    "size_bytes": len(part.get_payload(decode=True) or b""),
                }
            )
            continue

        if content_type == "text/plain":
            text = _decode_part(part).strip()
            if text:
                plain_parts.append(text)
        elif content_type == "text/html":
            html = _decode_part(part).strip()
            if html:
                html_parts.append(html)

    body = "\n\n".join(plain_parts).strip()
    source = "text/plain"
    if not body and html_parts:
        parser = _TextHTMLParser()
        parser.feed("\n".join(html_parts))
        parser.close()
        body = unescape(parser.text())
        source = "text/html"

    truncated = len(body) > max_chars
    body = body[:max_chars]

    return {
        "subject": str(message.get("subject", "")),
        "from": str(message.get("from", "")),
        "to": str(message.get("to", "")),
        "cc": str(message.get("cc", "")),
        "date": str(message.get("date", "")),
        "message_id": str(message.get("message-id", "")),
        "body_text": body,
        "body_source": source if body else None,
        "body_truncated": truncated,
        "attachments": attachments,
    }


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
        message bodies are not returned by this list operation.
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
        """Search cached message headers without connecting to IMAP."""
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
        """List sender domains with cached unsubscribe methods for one account."""
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
        }

    @mcp.tool(annotations=READ_ONLY_LOCAL)
    def get_message_headers(account: str, folder: str, uid: int) -> dict:
        """Get cached headers for one exact account/folder/UID reference."""
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

    @mcp.tool(annotations=READ_ONLY_REMOTE)
    def get_message_body(
        account: str,
        folder: str,
        uid: int,
        max_chars: int = 50000,
    ) -> dict:
        """Read one Yahoo message body without modifying the mailbox.

        The message must already exist in the local scan index. The tool checks
        UIDVALIDITY, selects the Yahoo folder read-only, and fetches with
        BODY.PEEK[] so the Seen flag is not set. Attachment contents are never
        downloaded into the result; only filename/type/size metadata is returned.
        """
        if max_chars < 1000 or max_chars > 100000:
            return {"error": "max_chars must be between 1000 and 100000"}

        account_name = ctx.account(account).name
        detail = messages.get_message_headers(
            ctx.store.conn, account=account_name, folder=folder, uid=uid
        )
        if detail is None:
            return {
                "error": (
                    f"No scanned message for account {account_name!r}, folder {folder!r}, UID {uid}. "
                    "Scan the folder first."
                )
            }
        if detail.get("staleness") != "current":
            return {
                "error": (
                    "The cached message reference is stale. Rescan this folder before reading."
                ),
                "staleness": detail.get("staleness"),
            }

        raw = ctx.imap(account_name).fetch_message_peek(
            folder,
            uid,
            expected_uidvalidity=int(detail["uidvalidity"]),
        )
        parsed = _extract_message(raw, max_chars)
        return {
            "account": account_name,
            "folder": folder,
            "uid": uid,
            "uidvalidity": detail["uidvalidity"],
            **parsed,
        }
