"""Parsing of raw RFC 5322 header blocks into structured message records."""

from __future__ import annotations

import email.utils
import re
from dataclasses import dataclass
from email import policy
from email.parser import BytesHeaderParser

_parser = BytesHeaderParser(policy=policy.default)


@dataclass
class ParsedHeaders:
    sender_email: str | None
    sender_domain: str | None
    sender_name: str | None
    subject: str | None
    date: str | None  # ISO 8601
    list_unsub_raw: str | None
    unsub_mailto: str | None
    unsub_http: str | None
    one_click: bool


def sender_domain(email_addr: str) -> str | None:
    """Return the exact normalized domain after ``@``.

    Keeping subdomains distinct prevents a decision for one mail stream (for
    example, ``news.vendor.com``) from affecting unrelated streams hosted
    under the same registrable domain.
    """
    if "@" not in email_addr:
        return None
    host = email_addr.rsplit("@", 1)[1].lower().strip().strip(">")
    return host or None


def parse_list_unsubscribe(raw: str) -> tuple[str | None, str | None]:
    """Extract the first mailto: and first http(s): URI from a List-Unsubscribe value."""
    mailto = None
    http = None
    for uri in re.findall(r"<([^>]+)>", raw):
        uri = uri.strip()
        if uri.lower().startswith("mailto:") and mailto is None:
            mailto = uri
        elif uri.lower().startswith(("http://", "https://")) and http is None:
            http = uri
    return mailto, http


def parse_headers(raw: bytes) -> ParsedHeaders:
    msg = _parser.parsebytes(raw)

    sender_email = None
    sender_name = None
    domain = None
    from_value = str(msg.get("From", "") or "")
    if from_value:
        name, addr = email.utils.parseaddr(from_value)
        if addr:
            sender_email = addr.lower()
            sender_name = name or None
            domain = sender_domain(sender_email)

    subject = str(msg.get("Subject", "") or "").strip() or None

    date_iso = None
    date_value = msg.get("Date")
    if date_value:
        try:
            dt = email.utils.parsedate_to_datetime(str(date_value))
            if dt is not None:
                date_iso = dt.isoformat()
        except (TypeError, ValueError):
            pass

    list_unsub_raw = None
    unsub_mailto = None
    unsub_http = None
    lu = msg.get("List-Unsubscribe")
    if lu:
        list_unsub_raw = str(lu).strip()
        unsub_mailto, unsub_http = parse_list_unsubscribe(list_unsub_raw)

    lup = msg.get("List-Unsubscribe-Post")
    one_click = bool(lup and "one-click" in str(lup).lower())

    return ParsedHeaders(
        sender_email=sender_email,
        sender_domain=domain,
        sender_name=sender_name,
        subject=subject,
        date=date_iso,
        list_unsub_raw=list_unsub_raw,
        unsub_mailto=unsub_mailto,
        unsub_http=unsub_http,
        one_click=one_click,
    )
