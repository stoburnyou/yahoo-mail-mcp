"""Aggregate scanned messages into sender-group records for review."""

from __future__ import annotations

import sqlite3

SORT_COLUMNS = {
    "count": "message_count DESC",
    "size": "total_size_bytes DESC",
    "recent": "last_seen DESC",
    "oldest": "first_seen ASC",
    "domain": "sender_domain ASC",
}


def _group_row_to_dict(
    conn: sqlite3.Connection, row: sqlite3.Row, sample_subjects: int = 5
) -> dict:
    domain = row["sender_domain"]
    addresses = [
        r[0]
        for r in conn.execute(
            """
            SELECT sender_email FROM messages
            WHERE sender_domain = ? AND deleted_at IS NULL AND sender_email IS NOT NULL
            GROUP BY sender_email ORDER BY COUNT(*) DESC LIMIT 10
            """,
            (domain,),
        )
    ]
    subjects = [
        r[0]
        for r in conn.execute(
            """
            SELECT subject FROM messages
            WHERE sender_domain = ? AND deleted_at IS NULL AND subject IS NOT NULL
            GROUP BY subject ORDER BY MAX(date) DESC LIMIT ?
            """,
            (domain, sample_subjects),
        )
    ]
    accounts = [
        r[0]
        for r in conn.execute(
            "SELECT DISTINCT account FROM messages WHERE sender_domain = ? AND deleted_at IS NULL",
            (domain,),
        )
    ]

    total = row["message_count"] or 0
    with_unsub = row["with_unsub"] or 0
    methods = []
    if row["one_click_count"]:
        methods.append("one_click")
    if row["mailto_count"]:
        methods.append("mailto")
    if row["http_count"]:
        methods.append("http")

    return {
        "sender_domain": domain,
        "accounts": accounts,
        "sender_addresses": addresses,
        "message_count": total,
        "total_size_mb": round((row["total_size_bytes"] or 0) / (1024 * 1024), 1),
        "first_seen": (row["first_seen"] or "")[:10] or None,
        "last_seen": (row["last_seen"] or "")[:10] or None,
        "sample_subjects": subjects,
        "unsubscribe": {
            "available": with_unsub > 0,
            "methods": methods,
            "coverage_pct": round(100 * with_unsub / total) if total else 0,
        },
        "decision": row["decision"] or "needs_review",
        "decision_updated_at": row["decision_updated_at"],
    }


_GROUP_SQL = """
SELECT
  m.sender_domain,
  COUNT(*) AS message_count,
  SUM(m.size_bytes) AS total_size_bytes,
  MIN(m.date) AS first_seen,
  MAX(m.date) AS last_seen,
  SUM(CASE WHEN m.list_unsub_raw IS NOT NULL THEN 1 ELSE 0 END) AS with_unsub,
  SUM(m.one_click) AS one_click_count,
  SUM(CASE WHEN m.unsub_mailto IS NOT NULL THEN 1 ELSE 0 END) AS mailto_count,
  SUM(CASE WHEN m.unsub_http IS NOT NULL THEN 1 ELSE 0 END) AS http_count,
  d.decision AS decision,
  d.updated_at AS decision_updated_at
FROM messages m
LEFT JOIN decisions d ON d.sender_domain = m.sender_domain
WHERE m.sender_domain IS NOT NULL AND m.deleted_at IS NULL
"""


def list_sender_groups(
    conn: sqlite3.Connection,
    account: str | None = None,
    sort: str = "count",
    min_count: int = 1,
    decision: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[dict]:
    if sort not in SORT_COLUMNS:
        raise ValueError(f"Invalid sort {sort!r}; must be one of {sorted(SORT_COLUMNS)}")

    sql = _GROUP_SQL
    params: list = []
    if account:
        sql += " AND m.account = ?"
        params.append(account)
    sql += " GROUP BY m.sender_domain HAVING COUNT(*) >= ?"
    params.append(min_count)
    if decision:
        if decision == "needs_review":
            sql += " AND (d.decision IS NULL OR d.decision = 'needs_review')"
        else:
            sql += " AND d.decision = ?"
            params.append(decision)
    sql += f" ORDER BY {SORT_COLUMNS[sort]} LIMIT ? OFFSET ?"
    params += [limit, offset]

    rows = conn.execute(sql, params).fetchall()
    return [_group_row_to_dict(conn, row) for row in rows]


def get_sender_detail(
    conn: sqlite3.Connection, domain: str, sample_subjects: int = 20
) -> dict | None:
    sql = _GROUP_SQL + " AND m.sender_domain = ? GROUP BY m.sender_domain"
    row = conn.execute(sql, (domain.lower(),)).fetchone()
    if row is None:
        return None
    detail = _group_row_to_dict(conn, row, sample_subjects=sample_subjects)

    detail["per_folder"] = [
        dict(r)
        for r in conn.execute(
            """
            SELECT account, folder, COUNT(*) AS count
            FROM messages WHERE sender_domain = ? AND deleted_at IS NULL
            GROUP BY account, folder ORDER BY count DESC
            """,
            (domain.lower(),),
        )
    ]
    return detail
