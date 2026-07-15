"""Parsing of raw RFC 5322 header blocks into structured message records."""

from __future__ import annotations

import email.utils
import re
from dataclasses import dataclass
from email import policy
from email.parser import BytesHeaderParser

import tldextract

# Bundled public-suffix snapshot only; never fetch the list over the network.
_extract = tldextract.TLDExtract(suffix_list_urls=())

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


def registrable_domain(email_addr: str) -> str | None:
    """news@e.marketing.example.com -> example.com (falls back to full host)."""
    if "@" not in email_addr:
        return None
    host = email_addr.rsplit("@", 1)[1].lower().strip().strip(">")
    ext = _extract(host)
    if ext.domain and ext.suffix:
        return f"{ext.domain}.{ext.suffix}"
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
    sender_domain = None
    from_value = str(msg.get("From", "") or "")
    if from_value:
        name, addr = email.utils.parseaddr(from_value)
        if addr:
            sender_email = addr.lower()
            sender_name = name or None
            sender_domain = registrable_domain(sender_email)

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
        sender_domain=sender_domain,
        sender_name=sender_name,
        subject=subject,
        date=date_iso,
        list_unsub_raw=list_unsub_raw,
        unsub_mailto=unsub_mailto,
        unsub_http=unsub_http,
        one_click=one_click,
    )
