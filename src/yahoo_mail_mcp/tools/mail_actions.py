"""Remote-safe Yahoo write actions with explicit preview/confirm tokens."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import mimetypes
import os
import secrets
import time
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from mcp.server.fastmcp import FastMCP

from ..app import AppContext
from ..imap.actions import move_to_archive, move_to_trash
from ..imap.client import YahooImapError
from .annotations import DESTRUCTIVE_REMOTE, READ_ONLY_REMOTE

TOKEN_TTL = 15 * 60
MAX_ATTACHMENT_BYTES = 20 * 1024 * 1024
MAX_TOTAL_ATTACHMENT_BYTES = 25 * 1024 * 1024
DROPBOX_SUFFIXES = (".dropboxusercontent.com", ".dropbox.com")
RELAY_TIMEOUT_SECONDS = 45


def _secret() -> bytes:
    value = os.environ.get("YAHOO_MAIL_MCP_WRITE_TOKEN_SECRET", "").strip()
    value = value or os.environ.get("YAHOO_MAIL_MCP_BEARER_TOKEN", "").strip()
    if len(value) < 32:
        raise RuntimeError("Write-token secret must be at least 32 characters")
    return value.encode()


def _canonical(value: dict) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def _make_token(kind: str, payload: dict) -> str:
    env = {
        "kind": kind,
        "exp": int(time.time()) + TOKEN_TTL,
        "nonce": secrets.token_urlsafe(12),
        "hash": hashlib.sha256(_canonical(payload)).hexdigest(),
    }
    raw = _canonical(env)
    sig = hmac.new(_secret(), raw, hashlib.sha256).digest()

    def enc(value: bytes) -> str:
        return base64.urlsafe_b64encode(value).decode().rstrip("=")

    return enc(raw) + "." + enc(sig)


def _check_token(token: str, kind: str, payload: dict) -> str | None:
    try:
        a, b = token.split(".", 1)

        def dec(value: str) -> bytes:
            return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))

        raw, sig = dec(a), dec(b)
        if not hmac.compare_digest(sig, hmac.new(_secret(), raw, hashlib.sha256).digest()):
            return "Invalid confirmation token"
        env = json.loads(raw)
    except Exception:
        return "Invalid confirmation token"
    if env.get("kind") != kind:
        return "Confirmation token is for a different action"
    if int(env.get("exp", 0)) < int(time.time()):
        return "Confirmation token expired; preview again"
    digest = hashlib.sha256(_canonical(payload)).hexdigest()
    if not hmac.compare_digest(str(env.get("hash", "")), digest):
        return "Action changed after preview; preview again"
    return None


def _signature() -> str:
    return os.environ.get("YAHOO_MAIL_MCP_SIGNATURE_TEXT", "").replace("\\n", "\n").strip()


def _signed_body(body: str, enabled: bool) -> str:
    sig = _signature()
    return body.rstrip() + "\n\n" + sig if enabled and sig else body


def _send_payload(account, to, cc, bcc, subject, body_text, body_html, attachments,
                  in_reply_to, references, append_signature) -> dict:
    return {
        "account": account,
        "to": [x.strip() for x in to if x.strip()],
        "cc": [x.strip() for x in (cc or []) if x.strip()],
        "bcc": [x.strip() for x in (bcc or []) if x.strip()],
        "subject": subject.strip(),
        "body_text": body_text,
        "body_html": body_html or "",
        "attachments": [{
            "filename": str(a.get("filename") or "").strip(),
            "url": str(a.get("url") or "").strip(),
            "content_type": str(a.get("content_type") or "").strip(),
            "size_bytes": int(a.get("size_bytes") or 0),
        } for a in (attachments or [])],
        "in_reply_to": (in_reply_to or "").strip(),
        "references": (references or "").strip(),
        "append_signature": bool(append_signature),
    }


def _validate_send(p: dict) -> list[str]:
    errors = []
    if not (p["to"] or p["cc"] or p["bcc"]):
        errors.append("At least one recipient is required")
    if not p["subject"]:
        errors.append("Subject is required")
    if not (p["body_text"] or p["body_html"]):
        errors.append("Message body is empty")
    total = 0
    for i, a in enumerate(p["attachments"], 1):
        if not a["filename"] or not a["url"]:
            errors.append(f"Attachment {i} needs filename and url")
            continue
        u = urlsplit(a["url"])
        host = (u.hostname or "").lower()
        if u.scheme != "https":
            errors.append(f"Attachment {i} must use HTTPS")
        if not any(host == s[1:] or host.endswith(s) for s in DROPBOX_SUFFIXES):
            errors.append(f"Attachment {i} is not from an approved Dropbox download host")
        if a["size_bytes"] < 0 or a["size_bytes"] > MAX_ATTACHMENT_BYTES:
            errors.append(f"Attachment {i} is too large")
        total += max(0, a["size_bytes"])
    if total > MAX_TOTAL_ATTACHMENT_BYTES:
        errors.append("Combined attachments exceed 25 MiB")
    return errors


def _relay_config() -> tuple[str, str]:
    url = os.environ.get("YAHOO_MAIL_MCP_RELAY_URL", "").strip().rstrip("/")
    secret = os.environ.get("YAHOO_MAIL_MCP_RELAY_SECRET", "").strip()
    if not url:
        raise RuntimeError("YAHOO_MAIL_MCP_RELAY_URL is not configured")
    if not url.startswith("https://") and not url.startswith("http://localhost"):
        raise RuntimeError("YAHOO_MAIL_MCP_RELAY_URL must use HTTPS")
    if len(secret) < 32:
        raise RuntimeError("YAHOO_MAIL_MCP_RELAY_SECRET must be at least 32 characters")
    return url, secret


def _relay_send(payload: dict) -> dict:
    base_url, secret = _relay_config()
    req = Request(
        f"{base_url}/send",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {secret}",
            "Content-Type": "application/json",
            "User-Agent": "yahoo-mail-mcp/relay-client",
        },
        method="POST",
    )
    with urlopen(req, timeout=RELAY_TIMEOUT_SECONDS) as resp:  # noqa: S310 - URL is operator configured.
        data = resp.read(64 * 1024)
    result = json.loads(data.decode("utf-8"))
    if not isinstance(result, dict):
        raise RuntimeError("Relay returned an invalid response")
    return result


def _indexed_uidvalidity(ctx: AppContext, account: str, folder: str, uid: int) -> int:
    row = ctx.store.conn.execute(
        "SELECT uidvalidity FROM messages WHERE account=? AND folder=? AND uid=? AND deleted_at IS NULL",
        (account, folder, uid),
    ).fetchone()
    if row is None:
        raise YahooImapError("Message is not in the scan index; scan the folder first")
    return int(row["uidvalidity"])


def register(mcp: FastMCP, ctx: AppContext) -> None:
    @mcp.tool(annotations=READ_ONLY_REMOTE)
    def preview_send_email(
        account: str,
        to: list[str],
        subject: str,
        body_text: str,
        cc: list[str] | None = None,
        bcc: list[str] | None = None,
        body_html: str | None = None,
        attachments: list[dict] | None = None,
        in_reply_to: str | None = None,
        references: str | None = None,
        append_signature: bool = True,
    ) -> dict:
        """Preview an outbound email. Nothing is sent and attachment URLs are not fetched."""
        acct = ctx.account(account)
        p = _send_payload(acct.name, to, cc, bcc, subject, body_text, body_html,
                          attachments, in_reply_to, references, append_signature)
        errors = _validate_send(p)
        if errors:
            return {"ok": False, "errors": errors}
        return {
            "ok": True,
            "from": acct.email,
            "to": p["to"], "cc": p["cc"], "bcc": p["bcc"],
            "subject": p["subject"],
            "body_text": _signed_body(p["body_text"], p["append_signature"]),
            "attachments": [{
                "filename": a["filename"],
                "content_type": a["content_type"] or mimetypes.guess_type(a["filename"])[0]
                    or "application/octet-stream",
                "size_bytes": a["size_bytes"],
            } for a in p["attachments"]],
            "confirm_token": _make_token("send_email", p),
            "confirm_token_expires_in_seconds": TOKEN_TTL,
            "note": "Nothing has been sent. Send only after explicit user approval.",
        }

    @mcp.tool(annotations=DESTRUCTIVE_REMOTE)
    def send_email(
        account: str,
        to: list[str],
        subject: str,
        body_text: str,
        confirm_token: str,
        cc: list[str] | None = None,
        bcc: list[str] | None = None,
        body_html: str | None = None,
        attachments: list[dict] | None = None,
        in_reply_to: str | None = None,
        references: str | None = None,
        append_signature: bool = True,
    ) -> dict:
        """Send exactly the email that was previewed and explicitly approved."""
        acct = ctx.account(account)
        p = _send_payload(acct.name, to, cc, bcc, subject, body_text, body_html,
                          attachments, in_reply_to, references, append_signature)
        errors = _validate_send(p)
        if errors:
            return {"sent": False, "errors": errors}
        err = _check_token(confirm_token, "send_email", p)
        if err:
            return {"sent": False, "error": err}

        display = os.environ.get("YAHOO_MAIL_MCP_FROM_NAME", "").strip()
        relay_payload = {
            "account": acct.name,
            "from_email": acct.email,
            "from_name": display,
            "app_password": acct.app_password,
            "to": p["to"],
            "cc": p["cc"],
            "bcc": p["bcc"],
            "subject": p["subject"],
            "body_text": _signed_body(p["body_text"], p["append_signature"]),
            "body_html": p["body_html"],
            "attachments": p["attachments"],
            "in_reply_to": p["in_reply_to"],
            "references": p["references"],
        }
        result = _relay_send(relay_payload)
        if not result.get("sent"):
            return {"sent": False, "error": result.get("error", "Relay send failed")}

        names = [a["filename"] for a in p["attachments"]]
        ctx.store.log_action("send_email", account=acct.name, count=1,
                             detail=json.dumps({"to_count": len(p["to"]),
                                                "cc_count": len(p["cc"]),
                                                "bcc_count": len(p["bcc"]),
                                                "subject_sha256": hashlib.sha256(
                                                    p["subject"].encode("utf-8")
                                                ).hexdigest(),
                                                "attachment_count": len(names),
                                                "attachment_names_sha256": [
                                                    hashlib.sha256(
                                                        name.encode("utf-8")
                                                    ).hexdigest()
                                                    for name in names
                                                ]}, ensure_ascii=False))
        return {"sent": True, "from": acct.email, "to": p["to"], "cc": p["cc"],
                "subject": p["subject"], "attachments": names,
                "message_id": result.get("message_id")}

    @mcp.tool(annotations=READ_ONLY_REMOTE)
    def preview_message_action(
        account: str, folder: str, uid: int, action: str,
        destination_folder: str | None = None,
    ) -> dict:
        """Preview archive, trash, move, mark_read, or mark_unread for one message."""
        acct = ctx.account(account)
        action = action.strip().lower()
        if action not in {"archive", "trash", "move", "mark_read", "mark_unread"}:
            return {"ok": False, "error": "Unsupported action"}
        if action == "move" and not (destination_folder or "").strip():
            return {"ok": False, "error": "destination_folder is required"}
        row = ctx.store.conn.execute(
            "SELECT subject,sender_email,date,uidvalidity FROM messages WHERE account=? AND folder=? AND uid=? AND deleted_at IS NULL",
            (acct.name, folder, int(uid)),
        ).fetchone()
        if row is None:
            return {"ok": False, "error": "Message not found in scan index"}
        p = {"account": acct.name, "folder": folder, "uid": int(uid), "action": action,
             "destination_folder": (destination_folder or "").strip(),
             "uidvalidity": int(row["uidvalidity"])}
        return {"ok": True,
                "message": {"folder": folder, "uid": int(uid), "subject": row["subject"],
                            "from": row["sender_email"], "date": row["date"]},
                "action": action, "destination_folder": p["destination_folder"] or None,
                "confirm_token": _make_token("message_action", p),
                "confirm_token_expires_in_seconds": TOKEN_TTL,
                "note": "Nothing has been changed yet."}

    @mcp.tool(annotations=DESTRUCTIVE_REMOTE)
    def execute_message_action(
        account: str, folder: str, uid: int, action: str, confirm_token: str,
        destination_folder: str | None = None,
    ) -> dict:
        """Execute one previously previewed mailbox action."""
        acct = ctx.account(account)
        action = action.strip().lower()
        uidv = _indexed_uidvalidity(ctx, acct.name, folder, int(uid))
        p = {"account": acct.name, "folder": folder, "uid": int(uid), "action": action,
             "destination_folder": (destination_folder or "").strip(), "uidvalidity": uidv}
        err = _check_token(confirm_token, "message_action", p)
        if err:
            return {"ok": False, "error": err}
        imap = ctx.imap(acct.name)

        if action == "archive":
            changed = move_to_archive(imap, ctx.store, acct.name, folder, [int(uid)], uidv)
        elif action == "trash":
            changed = move_to_trash(imap, ctx.store, acct.name, folder, [int(uid)], uidv)
        elif action == "move":
            dest = p["destination_folder"]
            if not dest:
                return {"ok": False, "error": "destination_folder is required"}
            info = imap.with_retry(f"select {folder}", lambda: imap.select_folder(folder, readonly=False))
            if int(info[b"UIDVALIDITY"]) != uidv:
                return {"ok": False, "error": "UIDVALIDITY changed; rescan first"}
            safe = dest.replace("\\", "\\\\").replace('"', '\\"')
            typ, data = imap.client._imap.uid("MOVE", str(uid), f'"{safe}"')  # noqa: SLF001
            if typ != "OK":
                raise YahooImapError(f"UID MOVE failed: {typ} {data}")
            ctx.store.mark_deleted(acct.name, folder, [int(uid)])
            changed = 1
        elif action in {"mark_read", "mark_unread"}:
            info = imap.with_retry(f"select {folder}", lambda: imap.select_folder(folder, readonly=False))
            if int(info[b"UIDVALIDITY"]) != uidv:
                return {"ok": False, "error": "UIDVALIDITY changed; rescan first"}
            if action == "mark_read":
                imap.client.add_flags([int(uid)], [b"\\Seen"])
            else:
                imap.client.remove_flags([int(uid)], [b"\\Seen"])
            changed = 1
        else:
            return {"ok": False, "error": "Unsupported action"}

        ctx.store.log_action(action, account=acct.name, folder=folder, count=changed,
                             detail=f"uid={uid} destination={p['destination_folder'] or '-'}")
        return {"ok": True, "action": action, "changed": changed, "folder": folder,
                "uid": int(uid), "destination_folder": p["destination_folder"] or None,
                "note": "Trash is recoverable; no permanent expunge is performed."
                    if action == "trash" else None}


    @mcp.tool(annotations=DESTRUCTIVE_REMOTE)
    def bulk_move_by_sender_domains(
        account: str,
        source_folder: str,
        sender_domains: list[str],
        destination_folder: str,
        confirm_token: str | None = None,
    ) -> dict:
        """Preview or move all indexed messages from exact sender domains into one folder."""
        acct = ctx.account(account)
        source_folder = (source_folder or "").strip()
        destination_folder = (destination_folder or "").strip()
        domains = sorted({d.strip().lower() for d in (sender_domains or []) if d.strip()})
        if not source_folder or not destination_folder or not domains:
            return {"ok": False, "error": "source_folder, destination_folder and sender_domains are required"}
        if source_folder == destination_folder:
            return {"ok": False, "error": "source and destination must differ"}

        placeholders = ",".join("?" for _ in domains)
        rows = ctx.store.conn.execute(
            f"""SELECT uid,uidvalidity FROM messages
                WHERE account=? AND folder=? AND deleted_at IS NULL
                AND lower(sender_domain) IN ({placeholders}) ORDER BY uid""",
            [acct.name, source_folder, *domains],
        ).fetchall()
        if not rows:
            return {"ok": True, "preview": {"count": 0}, "note": "No matching messages remain."}
        uidvs = {int(r["uidvalidity"]) for r in rows}
        if len(uidvs) != 1:
            return {"ok": False, "error": "Multiple UIDVALIDITY values; rescan first"}
        uidv = next(iter(uidvs))
        uids = [int(r["uid"]) for r in rows]
        snap = hashlib.sha256(",".join(map(str,uids)).encode()).hexdigest()
        p = {"account":acct.name,"source_folder":source_folder,"destination_folder":destination_folder,
             "sender_domains":domains,"count":len(uids),"uidvalidity":uidv,"snapshot":snap}
        if not confirm_token:
            return {"ok": True,
                    "preview":{"account":acct.name,"action":"bulk_move_by_sender_domains",
                               "source_folder":source_folder,"destination_folder":destination_folder,
                               "sender_domains":domains,"count":len(uids)},
                    "confirm_token":_make_token("bulk_move_by_sender_domains",p),
                    "confirm_token_expires_in_seconds":TOKEN_TTL,
                    "note":"Nothing has been changed yet."}
        err = _check_token(confirm_token,"bulk_move_by_sender_domains",p)
        if err:
            return {"ok":False,"error":err}

        imap = ctx.imap(acct.name)
        info = imap.with_retry(f"select {source_folder}", lambda: imap.select_folder(source_folder, readonly=False))
        if int(info[b"UIDVALIDITY"]) != uidv:
            return {"ok":False,"error":"UIDVALIDITY changed; rescan first"}
        safe = destination_folder.replace("\\","\\\\").replace('"','\\"')
        moved = 0
        for start in range(0,len(uids),100):
            chunk = uids[start:start+100]
            seq = ",".join(map(str,chunk))
            typ,data = imap.client._imap.uid("MOVE",seq,f'"{safe}"')
            if typ != "OK":
                raise YahooImapError(f"UID MOVE failed: {typ} {data}")
            ctx.store.mark_deleted(acct.name,source_folder,chunk)
            moved += len(chunk)
        ctx.store.log_action("bulk_move_by_sender_domains",account=acct.name,folder=source_folder,
                             count=moved,detail=json.dumps({"destination":destination_folder,
                             "sender_domains":domains},sort_keys=True))
        return {"ok":True,"action":"bulk_move_by_sender_domains","changed":moved,
                "source_folder":source_folder,"destination_folder":destination_folder,
                "sender_domains":domains}
