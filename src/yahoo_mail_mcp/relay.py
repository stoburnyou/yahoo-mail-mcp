"""Private HTTPS SMTP relay for Yahoo Mail sends.

The relay intentionally exposes only a send endpoint. Mailbox actions such as
delete, expunge, archive, or move do not exist here.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import mimetypes
import os
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formataddr, make_msgid
from urllib.error import URLError
from urllib.parse import urlsplit
from urllib.request import Request as UrlRequest
from urllib.request import urlopen

import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

logger = logging.getLogger(__name__)

MIN_SECRET_LENGTH = 32
MAX_ATTACHMENT_BYTES = 20 * 1024 * 1024
MAX_TOTAL_ATTACHMENT_BYTES = 25 * 1024 * 1024
MAX_JSON_BYTES = 512 * 1024
DEFAULT_ATTACHMENT_HOST_SUFFIXES = (".dropboxusercontent.com", ".dropbox.com")


def _secret() -> str:
    value = os.environ.get("YAHOO_SMTP_RELAY_SECRET", "").strip()
    if len(value) < MIN_SECRET_LENGTH:
        raise ValueError("YAHOO_SMTP_RELAY_SECRET must be at least 32 characters")
    return value


def _smtp_host() -> str:
    return os.environ.get("YAHOO_SMTP_RELAY_SMTP_HOST", "smtp.mail.yahoo.com").strip()


def _smtp_port() -> int:
    return int(os.environ.get("YAHOO_SMTP_RELAY_SMTP_PORT", "587"))


def _allowed_attachment_suffixes() -> tuple[str, ...]:
    raw = os.environ.get("YAHOO_SMTP_RELAY_ATTACHMENT_HOST_SUFFIXES", "").strip()
    if not raw:
        return DEFAULT_ATTACHMENT_HOST_SUFFIXES
    return tuple(
        item.strip().lower()
        for item in raw.split(",")
        if item.strip()
    )


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _auth_error() -> JSONResponse:
    return JSONResponse({"sent": False, "error": "Unauthorized"}, status_code=401)


def _is_authorized(request: Request, secret: str) -> bool:
    scheme, separator, credential = request.headers.get("authorization", "").partition(" ")
    return bool(separator) and scheme.lower() == "bearer" and hmac.compare_digest(
        credential,
        secret,
    )


def _string_list(payload: dict, name: str) -> list[str]:
    value = payload.get(name) or []
    if not isinstance(value, list):
        raise ValueError(f"{name} must be a list")
    cleaned = [str(item).strip() for item in value if str(item).strip()]
    if len(cleaned) > 100:
        raise ValueError(f"{name} has too many recipients")
    return cleaned


def _validate_attachment_url(url: str) -> None:
    parsed = urlsplit(url)
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https":
        raise ValueError("Attachment URLs must use HTTPS")
    suffixes = _allowed_attachment_suffixes()
    if not any(host == suffix.lstrip(".") or host.endswith(suffix) for suffix in suffixes):
        raise ValueError("Attachment URL host is not allowlisted")


def _attachments(payload: dict) -> list[dict]:
    raw = payload.get("attachments") or []
    if not isinstance(raw, list):
        raise ValueError("attachments must be a list")
    attachments = []
    total = 0
    for index, item in enumerate(raw, 1):
        if not isinstance(item, dict):
            raise ValueError(f"Attachment {index} must be an object")
        filename = str(item.get("filename") or "").strip()
        url = str(item.get("url") or "").strip()
        content_type = str(item.get("content_type") or "").strip()
        size_bytes = int(item.get("size_bytes") or 0)
        if not filename or not url:
            raise ValueError(f"Attachment {index} needs filename and url")
        _validate_attachment_url(url)
        if size_bytes < 0 or size_bytes > MAX_ATTACHMENT_BYTES:
            raise ValueError(f"Attachment {index} is too large")
        total += max(0, size_bytes)
        attachments.append(
            {
                "filename": filename,
                "url": url,
                "content_type": content_type,
                "size_bytes": size_bytes,
            }
        )
    if total > MAX_TOTAL_ATTACHMENT_BYTES:
        raise ValueError("Combined attachments exceed 25 MiB")
    return attachments


def _validated_payload(payload: dict) -> dict:
    if not isinstance(payload, dict):
        raise ValueError("JSON body must be an object")
    from_email = str(payload.get("from_email") or "").strip()
    app_password = str(payload.get("app_password") or "").replace(" ", "")
    subject = str(payload.get("subject") or "").strip()
    body_text = str(payload.get("body_text") or "")
    body_html = str(payload.get("body_html") or "")
    to = _string_list(payload, "to")
    cc = _string_list(payload, "cc")
    bcc = _string_list(payload, "bcc")
    if not from_email:
        raise ValueError("from_email is required")
    if not app_password:
        raise ValueError("app_password is required")
    if not (to or cc or bcc):
        raise ValueError("At least one recipient is required")
    if not subject:
        raise ValueError("Subject is required")
    if not (body_text or body_html):
        raise ValueError("Message body is empty")
    return {
        "account": str(payload.get("account") or "").strip(),
        "from_email": from_email,
        "from_name": str(payload.get("from_name") or "").strip(),
        "app_password": app_password,
        "to": to,
        "cc": cc,
        "bcc": bcc,
        "subject": subject,
        "body_text": body_text,
        "body_html": body_html,
        "attachments": _attachments(payload),
        "in_reply_to": str(payload.get("in_reply_to") or "").strip(),
        "references": str(payload.get("references") or "").strip(),
    }


def _download_attachment(attachment: dict) -> bytes:
    request = UrlRequest(
        attachment["url"],
        headers={"User-Agent": "yahoo-smtp-relay/1.0"},
    )
    with urlopen(request, timeout=30) as response:  # noqa: S310 - URL host is allowlisted.
        data = response.read(MAX_ATTACHMENT_BYTES + 1)
    if len(data) > MAX_ATTACHMENT_BYTES:
        raise ValueError(f"Attachment {attachment['filename']!r} is too large")
    expected = int(attachment.get("size_bytes") or 0)
    if expected and len(data) != expected:
        raise ValueError(f"Attachment {attachment['filename']!r} size changed")
    return data


def _build_message(payload: dict) -> EmailMessage:
    message = EmailMessage()
    message["From"] = (
        formataddr((payload["from_name"], payload["from_email"]))
        if payload["from_name"]
        else payload["from_email"]
    )
    message["To"] = ", ".join(payload["to"])
    if payload["cc"]:
        message["Cc"] = ", ".join(payload["cc"])
    message["Subject"] = payload["subject"]
    message["Message-ID"] = make_msgid(domain=payload["from_email"].split("@")[-1])
    if payload["in_reply_to"]:
        message["In-Reply-To"] = payload["in_reply_to"]
    if payload["references"]:
        message["References"] = payload["references"]
    message.set_content(payload["body_text"])
    if payload["body_html"]:
        message.add_alternative(payload["body_html"], subtype="html")
    for attachment in payload["attachments"]:
        data = _download_attachment(attachment)
        ctype = (
            attachment["content_type"]
            or mimetypes.guess_type(attachment["filename"])[0]
            or "application/octet-stream"
        )
        main, sub = ctype.split("/", 1)
        message.add_attachment(
            data,
            maintype=main,
            subtype=sub,
            filename=attachment["filename"],
        )
    return message


def _send(payload: dict) -> str:
    message = _build_message(payload)
    recipients = payload["to"] + payload["cc"] + payload["bcc"]
    port = _smtp_port()
    if port == 465:
        with smtplib.SMTP_SSL(_smtp_host(), port, timeout=30) as smtp:
            smtp.login(payload["from_email"], payload["app_password"])
            smtp.send_message(message, from_addr=payload["from_email"], to_addrs=recipients)
    else:
        with smtplib.SMTP(_smtp_host(), port, timeout=30) as smtp:
            smtp.ehlo()
            smtp.starttls(context=ssl.create_default_context())
            smtp.ehlo()
            smtp.login(payload["from_email"], payload["app_password"])
            smtp.send_message(message, from_addr=payload["from_email"], to_addrs=recipients)
    return str(message["Message-ID"])


def _audit_payload(payload: dict) -> dict:
    recipients = payload["to"] + payload["cc"] + payload["bcc"]
    return {
        "account": payload["account"],
        "from_sha256": _hash(payload["from_email"]),
        "recipient_count": len(recipients),
        "recipient_domains": sorted({address.rsplit("@", 1)[-1].lower() for address in recipients}),
        "subject_sha256": _hash(payload["subject"]),
        "attachment_count": len(payload["attachments"]),
        "attachment_bytes_declared": sum(item["size_bytes"] for item in payload["attachments"]),
    }


async def _health(_request: Request) -> JSONResponse:
    return JSONResponse({"status": "ok", "service": "yahoo-smtp-relay"})


async def _send_route(request: Request) -> JSONResponse:
    if not _is_authorized(request, _secret()):
        return _auth_error()
    body = await request.body()
    if len(body) > MAX_JSON_BYTES:
        return JSONResponse({"sent": False, "error": "Request body too large"}, status_code=413)
    try:
        payload = _validated_payload(json.loads(body.decode("utf-8")))
        message_id = _send(payload)
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as exc:
        return JSONResponse({"sent": False, "error": str(exc)}, status_code=400)
    except (smtplib.SMTPException, OSError, URLError) as exc:
        logger.warning("relay_send_failed %s", json.dumps({"error_type": type(exc).__name__}))
        return JSONResponse({"sent": False, "error": "SMTP send failed"}, status_code=502)

    audit = _audit_payload(payload)
    audit["message_id_sha256"] = _hash(message_id)
    logger.info("relay_send_success %s", json.dumps(audit, sort_keys=True))
    return JSONResponse({"sent": True, "message_id": message_id})


def create_relay_app() -> Starlette:
    _secret()
    return Starlette(
        routes=[
            Route("/health", _health, methods=["GET"]),
            Route("/send", _send_route, methods=["POST"]),
        ]
    )


def main() -> None:
    level_name = os.environ.get("YAHOO_SMTP_RELAY_LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        level=getattr(logging, level_name, logging.INFO),
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    port = int(os.environ.get("PORT", "8000"))
    logger.info("Starting private Yahoo SMTP relay on port %d", port)
    uvicorn.run(create_relay_app(), host="0.0.0.0", port=port, proxy_headers=True)


if __name__ == "__main__":
    main()

