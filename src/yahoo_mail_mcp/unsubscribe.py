"""Unsubscribe execution.

Hierarchy of methods, safest first:
1. RFC 8058 one-click: HTTP POST with List-Unsubscribe=One-Click body.
2. mailto: links - send an unsubscribe email over SMTP from the same account.
3. Plain http(s) links: NOT fetched automatically (often interactive or
   tracking links); reported back as `manual` with the URL.
"""

from __future__ import annotations

import logging
import smtplib
import sqlite3
from dataclasses import dataclass
from email.message import EmailMessage
from urllib.parse import parse_qs, unquote, urlparse

import httpx

from .config import SMTP_HOST, SMTP_PORT, Account
from .store.db import Store

logger = logging.getLogger(__name__)

HTTP_TIMEOUT = 20.0


@dataclass
class UnsubscribeOutcome:
    sender_domain: str
    method: str  # "one_click" | "mailto" | "manual" | "none"
    ok: bool
    detail: str


def _pick_unsubscribe_row(conn: sqlite3.Connection, domain: str) -> sqlite3.Row | None:
    """Most recent message from this domain that has any unsubscribe info,
    preferring one-click, then mailto, then http."""
    return conn.execute(
        """
        SELECT account, sender_email, unsub_mailto, unsub_http, one_click
        FROM messages
        WHERE sender_domain = ? AND deleted_at IS NULL AND list_unsub_raw IS NOT NULL
        ORDER BY one_click DESC,
                 (unsub_mailto IS NOT NULL) DESC,
                 date DESC
        LIMIT 1
        """,
        (domain,),
    ).fetchone()


def one_click_unsubscribe(url: str) -> tuple[bool, str]:
    try:
        resp = httpx.post(
            url,
            content="List-Unsubscribe=One-Click",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=HTTP_TIMEOUT,
            follow_redirects=True,
        )
        ok = resp.status_code < 400
        return ok, f"POST {url} -> HTTP {resp.status_code}"
    except httpx.HTTPError as exc:
        return False, f"POST {url} failed: {exc}"


def mailto_unsubscribe(account: Account, mailto_uri: str) -> tuple[bool, str]:
    parsed = urlparse(mailto_uri)
    to_addr = unquote(parsed.path)
    params = parse_qs(parsed.query)
    subject = params.get("subject", ["unsubscribe"])[0]
    body = params.get("body", ["unsubscribe"])[0]

    msg = EmailMessage()
    msg["From"] = account.email
    msg["To"] = to_addr
    msg["Subject"] = subject
    msg.set_content(body)

    try:
        with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=30) as smtp:
            smtp.login(account.email, account.app_password)
            smtp.send_message(msg)
        return True, f"Sent unsubscribe email to {to_addr} from {account.email}"
    except (smtplib.SMTPException, OSError) as exc:
        return False, f"SMTP send to {to_addr} failed: {exc}"


def unsubscribe_domain(
    store: Store, accounts_by_name: dict[str, Account], domain: str
) -> UnsubscribeOutcome:
    row = _pick_unsubscribe_row(store.conn, domain)
    if row is None:
        return UnsubscribeOutcome(domain, "none", False, "No List-Unsubscribe header on record")

    if row["one_click"] and row["unsub_http"]:
        ok, detail = one_click_unsubscribe(row["unsub_http"])
        outcome = UnsubscribeOutcome(domain, "one_click", ok, detail)
    elif row["unsub_mailto"]:
        account = accounts_by_name.get(row["account"])
        if account is None:
            outcome = UnsubscribeOutcome(
                domain, "mailto", False, f"Account {row['account']} not configured"
            )
        else:
            ok, detail = mailto_unsubscribe(account, row["unsub_mailto"])
            outcome = UnsubscribeOutcome(domain, "mailto", ok, detail)
    elif row["unsub_http"]:
        outcome = UnsubscribeOutcome(
            domain,
            "manual",
            False,
            f"Only a non-one-click link is available; open manually: {row['unsub_http']}",
        )
    else:
        outcome = UnsubscribeOutcome(domain, "none", False, "Unsubscribe header present but unusable")

    store.log_action(
        "unsubscribe",
        account=row["account"],
        sender_domain=domain,
        detail=f"{outcome.method}: {outcome.detail}",
    )
    return outcome
