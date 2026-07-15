"""SQLite persistence: scanned messages, decisions, checkpoints, audit log."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

VALID_DECISIONS = ("keep", "unsubscribe", "delete", "needs_review")

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
  account        TEXT NOT NULL,
  folder         TEXT NOT NULL,
  uid            INTEGER NOT NULL,
  uidvalidity    INTEGER NOT NULL,
  sender_email   TEXT,
  sender_domain  TEXT,
  sender_name    TEXT,
  subject        TEXT,
  date           TEXT,
  size_bytes     INTEGER,
  list_unsub_raw TEXT,
  unsub_mailto   TEXT,
  unsub_http     TEXT,
  one_click      INTEGER NOT NULL DEFAULT 0,
  scanned_at     TEXT NOT NULL,
  deleted_at     TEXT,
  PRIMARY KEY (account, folder, uid)
);
CREATE INDEX IF NOT EXISTS idx_messages_domain ON messages (sender_domain);
CREATE INDEX IF NOT EXISTS idx_messages_account_domain ON messages (account, sender_domain);

CREATE TABLE IF NOT EXISTS decisions (
  sender_domain TEXT PRIMARY KEY,
  decision      TEXT NOT NULL CHECK (decision IN ('keep','unsubscribe','delete','needs_review')),
  notes         TEXT,
  source        TEXT NOT NULL DEFAULT 'mcp',
  updated_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS checkpoints (
  account      TEXT NOT NULL,
  folder       TEXT NOT NULL,
  uidvalidity  INTEGER NOT NULL,
  low_uid      INTEGER,             -- lowest UID fetched so far (scan walks downward)
  high_uid     INTEGER,             -- highest UID seen when the scan started
  done         INTEGER NOT NULL DEFAULT 0,
  scanned      INTEGER NOT NULL DEFAULT 0,
  updated_at   TEXT NOT NULL,
  PRIMARY KEY (account, folder)
);

CREATE TABLE IF NOT EXISTS confirm_tokens (
  token      TEXT PRIMARY KEY,
  decision   TEXT NOT NULL,
  total      INTEGER NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS action_log (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  ts         TEXT NOT NULL,
  action     TEXT NOT NULL,
  account    TEXT,
  folder     TEXT,
  sender_domain TEXT,
  count      INTEGER,
  detail     TEXT
);
"""


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Store:
    def __init__(self, db_path: Path):
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(db_path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(SCHEMA)

    def close(self) -> None:
        self.conn.close()

    # -- messages --------------------------------------------------------------

    def upsert_messages(self, rows: list[dict]) -> None:
        self.conn.executemany(
            """
            INSERT INTO messages (
              account, folder, uid, uidvalidity, sender_email, sender_domain,
              sender_name, subject, date, size_bytes, list_unsub_raw,
              unsub_mailto, unsub_http, one_click, scanned_at
            ) VALUES (
              :account, :folder, :uid, :uidvalidity, :sender_email, :sender_domain,
              :sender_name, :subject, :date, :size_bytes, :list_unsub_raw,
              :unsub_mailto, :unsub_http, :one_click, :scanned_at
            )
            ON CONFLICT (account, folder, uid) DO UPDATE SET
              uidvalidity = excluded.uidvalidity,
              sender_email = excluded.sender_email,
              sender_domain = excluded.sender_domain,
              sender_name = excluded.sender_name,
              subject = excluded.subject,
              date = excluded.date,
              size_bytes = excluded.size_bytes,
              list_unsub_raw = excluded.list_unsub_raw,
              unsub_mailto = excluded.unsub_mailto,
              unsub_http = excluded.unsub_http,
              one_click = excluded.one_click,
              scanned_at = excluded.scanned_at,
              deleted_at = NULL
            """,
            rows,
        )
        self.conn.commit()

    def mark_deleted(self, account: str, folder: str, uids: list[int]) -> None:
        now = utcnow()
        self.conn.executemany(
            "UPDATE messages SET deleted_at = ? WHERE account = ? AND folder = ? AND uid = ?",
            [(now, account, folder, uid) for uid in uids],
        )
        self.conn.commit()

    def message_count(self, account: str | None = None) -> int:
        sql = "SELECT COUNT(*) FROM messages WHERE deleted_at IS NULL"
        params: list = []
        if account:
            sql += " AND account = ?"
            params.append(account)
        return self.conn.execute(sql, params).fetchone()[0]

    # -- checkpoints -----------------------------------------------------------

    def get_checkpoint(self, account: str, folder: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM checkpoints WHERE account = ? AND folder = ?", (account, folder)
        ).fetchone()

    def save_checkpoint(
        self,
        account: str,
        folder: str,
        uidvalidity: int,
        low_uid: int | None,
        high_uid: int | None,
        done: bool,
        scanned: int,
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO checkpoints (account, folder, uidvalidity, low_uid, high_uid, done, scanned, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (account, folder) DO UPDATE SET
              uidvalidity = excluded.uidvalidity,
              low_uid = excluded.low_uid,
              high_uid = excluded.high_uid,
              done = excluded.done,
              scanned = excluded.scanned,
              updated_at = excluded.updated_at
            """,
            (account, folder, uidvalidity, low_uid, high_uid, int(done), scanned, utcnow()),
        )
        self.conn.commit()

    def clear_checkpoint(self, account: str, folder: str) -> None:
        self.conn.execute(
            "DELETE FROM checkpoints WHERE account = ? AND folder = ?", (account, folder)
        )
        self.conn.commit()

    # -- decisions ---------------------------------------------------------------

    def set_decision(self, domain: str, decision: str, notes: str | None = None, source: str = "mcp") -> None:
        if decision not in VALID_DECISIONS:
            raise ValueError(f"Invalid decision {decision!r}; must be one of {VALID_DECISIONS}")
        self.conn.execute(
            """
            INSERT INTO decisions (sender_domain, decision, notes, source, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT (sender_domain) DO UPDATE SET
              decision = excluded.decision,
              notes = excluded.notes,
              source = excluded.source,
              updated_at = excluded.updated_at
            """,
            (domain.lower(), decision, notes, source, utcnow()),
        )
        self.conn.commit()

    def get_decisions(self, decision: str | None = None) -> list[sqlite3.Row]:
        if decision:
            return self.conn.execute(
                "SELECT * FROM decisions WHERE decision = ? ORDER BY sender_domain", (decision,)
            ).fetchall()
        return self.conn.execute("SELECT * FROM decisions ORDER BY sender_domain").fetchall()

    # -- confirm tokens ----------------------------------------------------------

    def save_confirm_token(self, token: str, decision: str, total: int) -> None:
        self.conn.execute(
            "INSERT INTO confirm_tokens (token, decision, total, created_at) VALUES (?, ?, ?, ?)",
            (token, decision, total, utcnow()),
        )
        self.conn.commit()

    def pop_confirm_token(self, token: str) -> sqlite3.Row | None:
        row = self.conn.execute(
            "SELECT * FROM confirm_tokens WHERE token = ?", (token,)
        ).fetchone()
        if row is not None:
            self.conn.execute("DELETE FROM confirm_tokens WHERE token = ?", (token,))
            self.conn.commit()
        return row

    # -- audit log -----------------------------------------------------------------

    def log_action(
        self,
        action: str,
        account: str | None = None,
        folder: str | None = None,
        sender_domain: str | None = None,
        count: int | None = None,
        detail: str | None = None,
    ) -> None:
        self.conn.execute(
            "INSERT INTO action_log (ts, action, account, folder, sender_domain, count, detail)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (utcnow(), action, account, folder, sender_domain, count, detail),
        )
        self.conn.commit()
