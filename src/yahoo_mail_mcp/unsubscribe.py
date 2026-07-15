"""Unsubscribe execution.

Hierarchy of methods, safest first:
1. RFC 8058 one-click: HTTP POST with List-Unsubscribe=One-Click body.
2. mailto: links - send an unsubscribe email over SMTP from the same account.
3. Plain http(s) links: NOT fetched automatically (often interactive or
   tracking links); reported back as `manual` with the URL.
"""

from __future__ import annotations

import http.client
import ipaddress
import logging
import smtplib
import socket
import sqlite3
import ssl
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import parseaddr
from urllib.parse import parse_qs, unquote, urlparse

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


def _validate_one_click_url(url: str) -> str | None:
    """Return an error for URLs that are unsafe for an automated POST.

    RFC 8058 one-click unsubscribe links must use HTTPS. Blocking local names,
    credentials, and non-public literal IPs also prevents an email header from
    turning this tool into an obvious request forgery against local services.
    """
    parsed = urlparse(url)
    if parsed.scheme.lower() != "https":
        return "RFC 8058 one-click URL must use HTTPS"
    if parsed.username or parsed.password:
        return "one-click URL must not contain credentials"
    hostname = parsed.hostname
    if not hostname:
        return "one-click URL has no hostname"
    hostname = hostname.lower().rstrip(".")
    if hostname == "localhost" or hostname.endswith((".localhost", ".local")):
        return "one-click URL points to a local hostname"
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        return None
    if not address.is_global:
        return "one-click URL points to a non-public IP address"
    return None


def _public_addresses(url: str) -> tuple[list[str], str | None]:
    hostname = urlparse(url).hostname
    if not hostname:
        return [], "one-click URL has no hostname"
    try:
        addresses = {
            str(item[4][0]) for item in socket.getaddrinfo(hostname, 443, type=socket.SOCK_STREAM)
        }
    except OSError as exc:
        return [], f"one-click hostname could not be resolved: {exc}"
    if not addresses:
        return [], "one-click hostname resolved to no addresses"
    for raw_address in addresses:
        try:
            address = ipaddress.ip_address(raw_address)
        except ValueError:
            return [], f"one-click hostname resolved to invalid address {raw_address!r}"
        if not address.is_global:
            return [], "one-click hostname resolves to a non-public IP address"
    return sorted(addresses), None


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """HTTPS connection whose TCP destination is a pre-validated IP."""

    def __init__(self, hostname: str, pinned_ip: str, port: int, timeout: float):
        self._ssl_context = ssl.create_default_context()
        super().__init__(
            hostname,
            port=port,
            timeout=timeout,
            context=self._ssl_context,
        )
        self._pinned_ip = pinned_ip

    def connect(self) -> None:
        raw_socket = socket.create_connection((self._pinned_ip, self.port), self.timeout)
        self.sock = self._ssl_context.wrap_socket(raw_socket, server_hostname=self.host)


def _is_related_endpoint(hostname: str, sender_domain: str) -> bool:
    hostname = hostname.lower().rstrip(".")
    sender_domain = sender_domain.lower().rstrip(".")
    return (
        hostname == sender_domain
        or hostname.endswith(f".{sender_domain}")
        or sender_domain.endswith(f".{hostname}")
    )


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


def one_click_unsubscribe(url: str, sender_domain: str) -> tuple[bool, str]:
    validation_error = _validate_one_click_url(url)
    if validation_error:
        return False, f"Refused unsafe one-click URL: {validation_error}"
    parsed = urlparse(url)
    assert parsed.hostname is not None
    if not _is_related_endpoint(parsed.hostname, sender_domain):
        return (
            False,
            "Refused one-click URL on a domain unrelated to the reviewed sender; "
            "handle this unsubscribe manually",
        )
    addresses, resolution_error = _public_addresses(url)
    if resolution_error:
        return False, f"Refused unsafe one-click URL: {resolution_error}"
    try:
        port = parsed.port or 443
    except ValueError as exc:
        return False, f"Refused unsafe one-click URL: invalid port ({exc})"
    path = parsed.path or "/"
    if parsed.query:
        path += f"?{parsed.query}"
    body = "List-Unsubscribe=One-Click"
    errors = []
    for address in addresses:
        connection = _PinnedHTTPSConnection(parsed.hostname, address, port, HTTP_TIMEOUT)
        try:
            connection.request(
                "POST",
                path,
                body=body,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            response = connection.getresponse()
            response.read(64 * 1024)
            ok = 200 <= response.status < 300
            return ok, f"POST {url} -> HTTP {response.status}"
        except (OSError, ssl.SSLError, http.client.HTTPException) as exc:
            errors.append(f"{address}: {exc}")
        finally:
            connection.close()
    return False, f"POST {url} failed: {'; '.join(errors)}"


def mailto_unsubscribe(account: Account, mailto_uri: str) -> tuple[bool, str]:
    try:
        parsed = urlparse(mailto_uri)
        to_addr = unquote(parsed.path)
        _name, parsed_addr = parseaddr(to_addr)
        if parsed.scheme.lower() != "mailto" or not parsed_addr or parsed_addr != to_addr:
            return False, "Invalid mailto unsubscribe address"
        if any(char in to_addr for char in "\r\n"):
            return False, "Invalid mailto unsubscribe address"
        params = parse_qs(parsed.query)
        subject = params.get("subject", ["unsubscribe"])[0].replace("\r", " ").replace("\n", " ")
        body = params.get("body", ["unsubscribe"])[0]

        msg = EmailMessage()
        msg["From"] = account.email
        msg["To"] = to_addr
        msg["Subject"] = subject
        msg.set_content(body)

        with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=30) as smtp:
            smtp.login(account.email, account.app_password)
            smtp.send_message(msg)
        return True, f"Sent unsubscribe email to {to_addr} from {account.email}"
    except (smtplib.SMTPException, OSError, TypeError, ValueError) as exc:
        return False, f"SMTP send to {to_addr} failed: {exc}"


def unsubscribe_domain(
    store: Store, accounts_by_name: dict[str, Account], domain: str
) -> UnsubscribeOutcome:
    row = _pick_unsubscribe_row(store.conn, domain)
    if row is None:
        return UnsubscribeOutcome(domain, "none", False, "No List-Unsubscribe header on record")

    if row["one_click"] and row["unsub_http"]:
        ok, detail = one_click_unsubscribe(row["unsub_http"], domain)
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
        outcome = UnsubscribeOutcome(
            domain, "none", False, "Unsubscribe header present but unusable"
        )

    store.log_action(
        "unsubscribe",
        account=row["account"],
        sender_domain=domain,
        detail=f"{outcome.method}: {outcome.detail}",
    )
    return outcome
