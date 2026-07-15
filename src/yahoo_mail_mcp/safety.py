"""Safeguards for destructive operations: previews and confirm tokens."""

from __future__ import annotations

import hashlib
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone

from .store.db import Store

# If the live count drifts more than this fraction from the previewed count,
# the confirm token is rejected and a fresh preview is required.
DRIFT_TOLERANCE = 0.05
CONFIRM_TOKEN_TTL = timedelta(minutes=15)


def _snapshot_hash(rows: list[sqlite3.Row]) -> str:
    digest = hashlib.sha256()
    for row in rows:
        digest.update(
            (
                f"{row['account']}\0{row['folder']}\0{row['uid']}\0"
                f"{row['uidvalidity']}\0{row['sender_domain']}\n"
            ).encode()
        )
    return digest.hexdigest()


def pending_messages(
    conn: sqlite3.Connection,
    account: str,
    decision: str,
) -> list[sqlite3.Row]:
    """Pending messages for one account whose sender domain has `decision`."""
    return conn.execute(
        """
        SELECT m.account, m.folder, m.uid, m.uidvalidity, m.sender_domain
        FROM messages m
        JOIN decisions d
          ON d.account = m.account AND d.sender_domain = m.sender_domain
        WHERE m.account = ? AND d.decision = ? AND m.deleted_at IS NULL
        ORDER BY m.account, m.folder, m.uid
        """,
        (account, decision),
    ).fetchall()


def preview(store: Store, account: str, decision: str) -> dict:
    rows = pending_messages(store.conn, account, decision)
    per_domain: dict[str, int] = {}
    per_account: dict[str, int] = {}
    for row in rows:
        per_domain[row["sender_domain"]] = per_domain.get(row["sender_domain"], 0) + 1
        per_account[row["account"]] = per_account.get(row["account"], 0) + 1

    total = len(rows)
    token = secrets.token_hex(8)
    store.save_confirm_token(token, decision, total, _snapshot_hash(rows))

    return {
        "account": account,
        "decision": decision,
        "total_messages": total,
        "domains": dict(sorted(per_domain.items(), key=lambda kv: -kv[1])),
        "per_account": per_account,
        "confirm_token": token,
    }


def validate_token(
    store: Store, token: str, decision: str, live_rows: list[sqlite3.Row]
) -> str | None:
    """Return an error string if the token is invalid, else None (token consumed)."""
    row = store.pop_confirm_token(token)
    if row is None:
        return "Unknown or already-used confirm_token. Run preview_cleanup to get a fresh one."
    try:
        created_at = datetime.fromisoformat(row["created_at"])
    except (TypeError, ValueError):
        return "confirm_token has an invalid timestamp. Run preview_cleanup again."
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=timezone.utc)
    if datetime.now(timezone.utc) - created_at > CONFIRM_TOKEN_TTL:
        return "confirm_token expired after 15 minutes. Run preview_cleanup again."
    if row["decision"] != decision:
        return (
            f"confirm_token was issued for decision {row['decision']!r}, not {decision!r}. "
            "Run preview_cleanup again."
        )
    previewed = int(row["total"])
    live_total = len(live_rows)
    allowed_drift = max(1, int(previewed * DRIFT_TOLERANCE))
    if abs(live_total - previewed) > allowed_drift:
        return (
            f"Affected message count changed since preview ({previewed} -> {live_total}, "
            f"more than {DRIFT_TOLERANCE:.0%} drift). Run preview_cleanup again."
        )
    if not row["snapshot_hash"] or row["snapshot_hash"] != _snapshot_hash(live_rows):
        return (
            "Affected messages or sender domains changed since preview. Run preview_cleanup again."
        )
    return None
