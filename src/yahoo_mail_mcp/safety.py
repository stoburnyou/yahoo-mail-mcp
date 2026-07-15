"""Safeguards for destructive operations: previews and confirm tokens."""

from __future__ import annotations

import secrets
import sqlite3

from .store.db import Store

# If the live count drifts more than this fraction from the previewed count,
# the confirm token is rejected and a fresh preview is required.
DRIFT_TOLERANCE = 0.05


def pending_messages(conn: sqlite3.Connection, decision: str) -> list[sqlite3.Row]:
    """All not-yet-deleted messages whose sender domain is tagged `decision`."""
    return conn.execute(
        """
        SELECT m.account, m.folder, m.uid, m.uidvalidity, m.sender_domain
        FROM messages m
        JOIN decisions d ON d.sender_domain = m.sender_domain
        WHERE d.decision = ? AND m.deleted_at IS NULL
        ORDER BY m.account, m.folder, m.uid
        """,
        (decision,),
    ).fetchall()


def preview(store: Store, decision: str) -> dict:
    rows = pending_messages(store.conn, decision)
    per_domain: dict[str, int] = {}
    per_account: dict[str, int] = {}
    for row in rows:
        per_domain[row["sender_domain"]] = per_domain.get(row["sender_domain"], 0) + 1
        per_account[row["account"]] = per_account.get(row["account"], 0) + 1

    total = len(rows)
    token = secrets.token_hex(8)
    store.save_confirm_token(token, decision, total)

    return {
        "decision": decision,
        "total_messages": total,
        "domains": dict(sorted(per_domain.items(), key=lambda kv: -kv[1])),
        "per_account": per_account,
        "confirm_token": token,
    }


def validate_token(store: Store, token: str, decision: str, live_total: int) -> str | None:
    """Return an error string if the token is invalid, else None (token consumed)."""
    row = store.pop_confirm_token(token)
    if row is None:
        return "Unknown or already-used confirm_token. Run preview_cleanup to get a fresh one."
    if row["decision"] != decision:
        return (
            f"confirm_token was issued for decision {row['decision']!r}, not {decision!r}. "
            "Run preview_cleanup again."
        )
    previewed = int(row["total"])
    allowed_drift = max(1, int(previewed * DRIFT_TOLERANCE))
    if abs(live_total - previewed) > allowed_drift:
        return (
            f"Affected message count changed since preview ({previewed} -> {live_total}, "
            f"more than {DRIFT_TOLERANCE:.0%} drift). Run preview_cleanup again."
        )
    return None
