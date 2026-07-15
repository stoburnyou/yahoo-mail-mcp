"""Privacy-conscious read models for scanned message headers."""

from __future__ import annotations

import sqlite3
from datetime import datetime

DEFAULT_LIMIT = 50
MAX_LIMIT = 200
MAX_SUBJECT_PREVIEW = 120

SORT_COLUMNS = {
    "recent": "(m.date IS NULL), datetime(m.date) DESC, m.uid DESC",
    "oldest": "(m.date IS NULL), datetime(m.date) ASC, m.uid ASC",
    "size": "(m.size_bytes IS NULL), m.size_bytes DESC, m.uid DESC",
}

_SELECT = """
SELECT
  m.*,
  d.decision,
  d.updated_at AS decision_updated_at,
  c.uidvalidity AS checkpoint_uidvalidity
FROM messages m
LEFT JOIN decisions d ON d.sender_domain = m.sender_domain
LEFT JOIN checkpoints c ON c.account = m.account AND c.folder = m.folder
"""


def _validated_limit(limit: int) -> int:
    return min(max(int(limit), 1), MAX_LIMIT)


def _validated_offset(offset: int) -> int:
    return max(int(offset), 0)


def _validated_datetime(value: str | None, field: str) -> str | None:
    if value is None:
        return None
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field} must be an ISO 8601 datetime") from exc
    return value


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _staleness(row: sqlite3.Row) -> str:
    checkpoint_uidvalidity = row["checkpoint_uidvalidity"]
    if checkpoint_uidvalidity is None:
        return "folder_not_checkpointed"
    if int(checkpoint_uidvalidity) != int(row["uidvalidity"]):
        return "uidvalidity_mismatch"
    return "current"


def _summary(row: sqlite3.Row, include_sender_email: bool) -> dict:
    subject = row["subject"]
    if subject and len(subject) > MAX_SUBJECT_PREVIEW:
        subject = f"{subject[: MAX_SUBJECT_PREVIEW - 1]}…"
    result = {
        "account": row["account"],
        "folder": row["folder"],
        "uid": row["uid"],
        "uidvalidity": row["uidvalidity"],
        "staleness": _staleness(row),
        "from_domain": row["sender_domain"],
        "subject": subject,
        "date": row["date"],
        "size_bytes": row["size_bytes"],
        "has_list_unsubscribe": row["list_unsub_raw"] is not None,
        "decision": row["decision"] or "needs_review",
    }
    if include_sender_email:
        result["from_email"] = row["sender_email"]
    return result


def _detail(row: sqlite3.Row) -> dict:
    methods: list[str] = []
    if row["one_click"]:
        methods.append("one_click")
    if row["unsub_mailto"]:
        methods.append("mailto")
    if row["unsub_http"]:
        methods.append("http")
    return {
        "account": row["account"],
        "folder": row["folder"],
        "uid": row["uid"],
        "uidvalidity": row["uidvalidity"],
        "staleness": _staleness(row),
        "from": {
            "email": row["sender_email"],
            "name": row["sender_name"],
            "domain": row["sender_domain"],
        },
        "subject": row["subject"],
        "date": row["date"],
        "size_bytes": row["size_bytes"],
        "scanned_at": row["scanned_at"],
        "deleted_at": row["deleted_at"],
        "unsubscribe": {
            "available": row["list_unsub_raw"] is not None,
            "methods": methods,
        },
        "decision": row["decision"] or "needs_review",
        "decision_updated_at": row["decision_updated_at"],
    }


def _run_page(
    conn: sqlite3.Connection,
    where: list[str],
    params: list[object],
    *,
    sort: str,
    limit: int,
    offset: int,
    include_sender_email: bool,
) -> tuple[list[dict], int, int, int]:
    if sort not in SORT_COLUMNS:
        raise ValueError(f"Invalid sort {sort!r}; must be one of {sorted(SORT_COLUMNS)}")
    limit = _validated_limit(limit)
    offset = _validated_offset(offset)
    where_sql = " WHERE " + " AND ".join(where)
    total = conn.execute(
        "SELECT COUNT(*) FROM messages m "
        "LEFT JOIN decisions d ON d.sender_domain = m.sender_domain "
        "LEFT JOIN checkpoints c ON c.account = m.account AND c.folder = m.folder" + where_sql,
        params,
    ).fetchone()[0]
    rows = conn.execute(
        _SELECT + where_sql + f" ORDER BY {SORT_COLUMNS[sort]} LIMIT ? OFFSET ?",
        [*params, limit, offset],
    ).fetchall()
    return (
        [_summary(row, include_sender_email) for row in rows],
        total,
        limit,
        offset,
    )


def list_recent_messages(
    conn: sqlite3.Connection,
    *,
    account: str | None = None,
    folder: str | None = None,
    since: str | None = None,
    limit: int = DEFAULT_LIMIT,
    offset: int = 0,
    include_sender_email: bool = False,
) -> tuple[list[dict], int, int, int]:
    where = ["m.deleted_at IS NULL"]
    params: list[object] = []
    if account:
        where.append("m.account = ?")
        params.append(account)
    if folder:
        where.append("m.folder = ?")
        params.append(folder)
    if since := _validated_datetime(since, "since"):
        where.append("datetime(m.date) >= datetime(?)")
        params.append(since)
    return _run_page(
        conn,
        where,
        params,
        sort="recent",
        limit=limit,
        offset=offset,
        include_sender_email=include_sender_email,
    )


def search_messages(
    conn: sqlite3.Connection,
    *,
    query: str | None = None,
    sender_domain: str | None = None,
    sender_email: str | None = None,
    subject_contains: str | None = None,
    account: str | None = None,
    folder: str | None = None,
    since: str | None = None,
    until: str | None = None,
    has_unsubscribe: bool | None = None,
    decision: str | None = None,
    sort: str = "recent",
    limit: int = DEFAULT_LIMIT,
    offset: int = 0,
    include_sender_email: bool = False,
    exclude_stale: bool = False,
) -> tuple[list[dict], int, int, int]:
    where = ["m.deleted_at IS NULL"]
    params: list[object] = []
    if query:
        pattern = f"%{_escape_like(query)}%"
        where.append(
            "(m.subject LIKE ? ESCAPE '\\' OR m.sender_domain LIKE ? ESCAPE '\\' "
            "OR m.sender_email LIKE ? ESCAPE '\\')"
        )
        params.extend([pattern, pattern, pattern])
    if sender_domain:
        where.append("lower(m.sender_domain) = ?")
        params.append(sender_domain.lower())
    if sender_email:
        where.append("lower(m.sender_email) = ?")
        params.append(sender_email.lower())
    if subject_contains:
        where.append("m.subject LIKE ? ESCAPE '\\'")
        params.append(f"%{_escape_like(subject_contains)}%")
    if account:
        where.append("m.account = ?")
        params.append(account)
    if folder:
        where.append("m.folder = ?")
        params.append(folder)
    if since := _validated_datetime(since, "since"):
        where.append("datetime(m.date) >= datetime(?)")
        params.append(since)
    if until := _validated_datetime(until, "until"):
        where.append("datetime(m.date) <= datetime(?)")
        params.append(until)
    if has_unsubscribe is True:
        where.append("m.list_unsub_raw IS NOT NULL")
    elif has_unsubscribe is False:
        where.append("m.list_unsub_raw IS NULL")
    if decision:
        if decision == "needs_review":
            where.append("(d.decision IS NULL OR d.decision = 'needs_review')")
        else:
            where.append("d.decision = ?")
            params.append(decision)
    if exclude_stale:
        where.append("c.uidvalidity = m.uidvalidity")
    return _run_page(
        conn,
        where,
        params,
        sort=sort,
        limit=limit,
        offset=offset,
        include_sender_email=include_sender_email,
    )


def get_message_headers(
    conn: sqlite3.Connection, *, account: str, folder: str, uid: int
) -> dict | None:
    row = conn.execute(
        _SELECT + " WHERE m.account = ? AND m.folder = ? AND m.uid = ?",
        (account, folder, uid),
    ).fetchone()
    return _detail(row) if row is not None else None
