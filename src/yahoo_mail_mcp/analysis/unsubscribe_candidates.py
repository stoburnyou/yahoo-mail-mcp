"""Privacy-safe sender groups with cached unsubscribe capabilities."""

from __future__ import annotations

import sqlite3

DEFAULT_LIMIT = 50
MAX_LIMIT = 200
METHODS = ("all", "one_click", "mailto", "manual")
SORT_COLUMNS = {
    "count": "message_count DESC, sender_domain ASC",
    "coverage": (
        "(CAST(header_count AS REAL) / NULLIF(message_count, 0)) DESC, "
        "message_count DESC, sender_domain ASC"
    ),
    "recent": "(last_seen IS NULL), last_seen DESC, sender_domain ASC",
    "domain": "sender_domain ASC",
}

_GROUPS = """
WITH candidate_groups AS (
  SELECT
    m.account,
    m.sender_domain,
    COUNT(*) AS message_count,
    SUM(CASE WHEN m.list_unsub_raw IS NOT NULL THEN 1 ELSE 0 END) AS header_count,
    SUM(
      CASE WHEN m.one_click = 1
             AND m.unsub_http IS NOT NULL
             AND lower(m.unsub_http) LIKE 'https://%'
           THEN 1 ELSE 0 END
    ) AS one_click_count,
    SUM(CASE WHEN m.unsub_mailto IS NOT NULL THEN 1 ELSE 0 END) AS mailto_count,
    SUM(
      CASE WHEN m.unsub_http IS NOT NULL
             AND NOT (
               m.one_click = 1
               AND lower(m.unsub_http) LIKE 'https://%'
             )
           THEN 1 ELSE 0 END
    ) AS manual_count,
    SUM(
      CASE WHEN m.unsub_http IS NOT NULL OR m.unsub_mailto IS NOT NULL
           THEN 1 ELSE 0 END
    ) AS candidate_count,
    MAX(m.date) AS last_seen,
    d.decision,
    d.updated_at AS decision_updated_at
  FROM messages m
  LEFT JOIN decisions d
    ON d.account = m.account AND d.sender_domain = m.sender_domain
  WHERE m.deleted_at IS NULL
    AND m.sender_domain IS NOT NULL
    AND m.account = ?
  GROUP BY m.account, m.sender_domain
)
"""

_METHOD_FILTERS = {
    "all": "candidate_count > 0",
    "one_click": "one_click_count > 0",
    "mailto": "mailto_count > 0",
    "manual": "manual_count > 0",
}


def _candidate_dict(conn: sqlite3.Connection, row: sqlite3.Row) -> dict:
    methods = []
    if row["one_click_count"]:
        methods.append("one_click")
    if row["mailto_count"]:
        methods.append("mailto")
    if row["manual_count"]:
        methods.append("manual")

    subjects = [
        subject_row[0]
        for subject_row in conn.execute(
            """
            SELECT subject
            FROM messages
            WHERE account = ? AND sender_domain = ?
              AND deleted_at IS NULL AND subject IS NOT NULL
            GROUP BY subject
            ORDER BY MAX(date) DESC
            LIMIT 3
            """,
            (row["account"], row["sender_domain"]),
        )
    ]
    total = int(row["message_count"])
    header_count = int(row["header_count"])
    return {
        "account": row["account"],
        "sender_domain": row["sender_domain"],
        "message_count": total,
        "candidate_message_count": int(row["candidate_count"]),
        "coverage_pct": round(100 * header_count / total) if total else 0,
        "methods": methods,
        "preferred_method": methods[0],
        "method_message_counts": {
            "one_click": int(row["one_click_count"]),
            "mailto": int(row["mailto_count"]),
            "manual": int(row["manual_count"]),
        },
        "automatic_candidate": bool(row["one_click_count"] or row["mailto_count"]),
        "last_seen": row["last_seen"],
        "sample_subjects": subjects,
        "decision": row["decision"] or "needs_review",
        "decision_updated_at": row["decision_updated_at"],
        "final_validation_required": True,
    }


def list_unsubscribe_candidates(
    conn: sqlite3.Connection,
    *,
    account: str,
    method: str = "all",
    sort: str = "count",
    limit: int = DEFAULT_LIMIT,
    offset: int = 0,
) -> tuple[list[dict], int, int, int]:
    if method not in METHODS:
        raise ValueError(f"method must be one of {METHODS}")
    if sort not in SORT_COLUMNS:
        raise ValueError(f"sort must be one of {tuple(SORT_COLUMNS)}")
    limit = min(max(int(limit), 1), MAX_LIMIT)
    offset = max(int(offset), 0)
    condition = _METHOD_FILTERS[method]

    total = conn.execute(
        _GROUPS + f"SELECT COUNT(*) FROM candidate_groups WHERE {condition}",
        (account,),
    ).fetchone()[0]
    rows = conn.execute(
        _GROUPS
        + (
            f"SELECT * FROM candidate_groups WHERE {condition} "
            f"ORDER BY {SORT_COLUMNS[sort]} LIMIT ? OFFSET ?"
        ),
        (account, limit, offset),
    ).fetchall()
    return (
        [_candidate_dict(conn, row) for row in rows],
        total,
        limit,
        offset,
    )
