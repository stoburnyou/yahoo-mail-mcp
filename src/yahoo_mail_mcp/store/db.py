"""SQLite persistence: scanned messages, decisions, checkpoints, audit log."""

from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

VALID_DECISIONS = ("keep", "unsubscribe", "archive", "delete", "needs_review")

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
CREATE INDEX IF NOT EXISTS idx_messages_active_date
  ON messages (account, folder, date) WHERE deleted_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_messages_active_account_date
  ON messages (account, date) WHERE deleted_at IS NULL;

CREATE TABLE IF NOT EXISTS decisions (
  sender_domain TEXT PRIMARY KEY,
  decision      TEXT NOT NULL
                CHECK (decision IN ('keep','unsubscribe','archive','delete','needs_review')),
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
  phase        TEXT NOT NULL DEFAULT 'historical',
  scanned      INTEGER NOT NULL DEFAULT 0,
  updated_at   TEXT NOT NULL,
  PRIMARY KEY (account, folder)
);

CREATE TABLE IF NOT EXISTS confirm_tokens (
  token      TEXT PRIMARY KEY,
  decision   TEXT NOT NULL,
  total      INTEGER NOT NULL,
  snapshot_hash TEXT,
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

CREATE TABLE IF NOT EXISTS scan_jobs (
  id           TEXT PRIMARY KEY,
  account      TEXT NOT NULL,
  folders_json TEXT,
  max_messages INTEGER,
  status       TEXT NOT NULL
               CHECK (status IN ('queued','running','completed','failed','interrupted')),
  result_json  TEXT,
  error        TEXT,
  created_at   TEXT NOT NULL,
  started_at   TEXT,
  completed_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_scan_jobs_created ON scan_jobs (created_at DESC);

CREATE TABLE IF NOT EXISTS schema_meta (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
"""


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Store:
    def __init__(self, db_path: Path):
        parent_existed = db_path.parent.exists()
        db_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if os.name == "posix" and not parent_existed:
            db_path.parent.chmod(0o700)
        if not db_path.exists():
            fd = os.open(db_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(fd)
        if os.name == "posix":
            db_path.chmod(0o600)
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA busy_timeout=5000")
        self.conn.execute("PRAGMA journal_mode=WAL")
        for suffix in ("-wal", "-shm"):
            sidecar = Path(f"{db_path}{suffix}")
            if os.name == "posix" and sidecar.exists():
                sidecar.chmod(0o600)
        self.conn.executescript(SCHEMA)
        self._ensure_checkpoint_phase()
        self._ensure_confirm_snapshot()
        self._ensure_archive_decision()
        self._migrate_sender_domains()

    def _ensure_checkpoint_phase(self) -> None:
        columns = {row["name"] for row in self.conn.execute("PRAGMA table_info(checkpoints)")}
        if "phase" not in columns:
            self.conn.execute(
                "ALTER TABLE checkpoints ADD COLUMN phase TEXT NOT NULL DEFAULT 'historical'"
            )
            self.conn.execute("UPDATE checkpoints SET phase = 'complete' WHERE done = 1")
            # Older code could save a capped incremental scan as done=0,
            # low_uid=1, then skip the remainder on resume. That state cannot
            # be distinguished safely from corruption, so force a rescan.
            self.conn.execute("DELETE FROM checkpoints WHERE done = 0 AND low_uid <= 1")
            self.conn.commit()

    def _ensure_confirm_snapshot(self) -> None:
        columns = {row["name"] for row in self.conn.execute("PRAGMA table_info(confirm_tokens)")}
        if "snapshot_hash" not in columns:
            self.conn.execute("ALTER TABLE confirm_tokens ADD COLUMN snapshot_hash TEXT")
            self.conn.commit()

    def _ensure_archive_decision(self) -> None:
        """Rebuild the decisions table when upgrading from the pre-Archive schema."""
        row = self.conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'decisions'"
        ).fetchone()
        if row is not None and "'archive'" in row["sql"]:
            return
        self.conn.executescript(
            """
            CREATE TABLE decisions_with_archive (
              sender_domain TEXT PRIMARY KEY,
              decision      TEXT NOT NULL
                            CHECK (decision IN (
                              'keep','unsubscribe','archive','delete','needs_review'
                            )),
              notes         TEXT,
              source        TEXT NOT NULL DEFAULT 'mcp',
              updated_at    TEXT NOT NULL
            );
            INSERT INTO decisions_with_archive
              (sender_domain, decision, notes, source, updated_at)
            SELECT sender_domain, decision, notes, source, updated_at FROM decisions;
            DROP TABLE decisions;
            ALTER TABLE decisions_with_archive RENAME TO decisions;
            """
        )
        self.conn.commit()

    def _migrate_sender_domains(self) -> None:
        """One-time migration from registrable to exact sender domains."""
        key = "sender_domain_format"
        row = self.conn.execute("SELECT value FROM schema_meta WHERE key = ?", (key,)).fetchone()
        if row is not None and row["value"] == "exact-v2":
            return
        decision_count = self.conn.execute("SELECT COUNT(*) FROM decisions").fetchone()[0]
        self.conn.execute(
            """
            UPDATE messages
            SET sender_domain = lower(
              substr(sender_email, instr(sender_email, '@') + 1)
            )
            WHERE sender_email IS NOT NULL
              AND instr(sender_email, '@') > 0
            """
        )
        self.conn.execute(
            """
            INSERT INTO schema_meta (key, value) VALUES (?, ?)
            ON CONFLICT (key) DO UPDATE SET value = excluded.value
            """,
            (key, "exact-v2"),
        )
        if decision_count:
            self.conn.execute("DELETE FROM decisions")
            self.conn.execute(
                """
                INSERT INTO action_log (ts, action, count, detail)
                VALUES (?, 'invalidate_legacy_decisions', ?, ?)
                """,
                (
                    utcnow(),
                    decision_count,
                    "Sender-domain format changed; decisions require review",
                ),
            )
        self.conn.commit()

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

    def clear_folder_messages(self, account: str, folder: str) -> None:
        self.conn.execute(
            "DELETE FROM messages WHERE account = ? AND folder = ?",
            (account, folder),
        )
        self.conn.commit()

    def message_count(self, account: str | None = None) -> int:
        sql = "SELECT COUNT(*) FROM messages WHERE deleted_at IS NULL"
        params: list = []
        if account:
            sql += " AND account = ?"
            params.append(account)
        return self.conn.execute(sql, params).fetchone()[0]

    # -- background scan jobs --------------------------------------------------

    def create_scan_job(
        self,
        job_id: str,
        account: str,
        folders_json: str | None,
        max_messages: int | None,
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO scan_jobs (
              id, account, folders_json, max_messages, status, created_at
            ) VALUES (?, ?, ?, ?, 'queued', ?)
            """,
            (job_id, account, folders_json, max_messages, utcnow()),
        )
        self.conn.commit()

    def update_scan_job(
        self,
        job_id: str,
        status: str,
        *,
        result_json: str | None = None,
        error: str | None = None,
    ) -> None:
        now = utcnow()
        started_at = now if status == "running" else None
        completed_at = now if status in {"completed", "failed", "interrupted"} else None
        self.conn.execute(
            """
            UPDATE scan_jobs
            SET status = ?,
                result_json = COALESCE(?, result_json),
                error = ?,
                started_at = COALESCE(started_at, ?),
                completed_at = ?
            WHERE id = ?
            """,
            (status, result_json, error, started_at, completed_at, job_id),
        )
        self.conn.commit()

    def get_scan_job(self, job_id: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM scan_jobs WHERE id = ?", (job_id,)).fetchone()

    def list_scan_jobs(self, limit: int = 20, status: str | None = None) -> list[sqlite3.Row]:
        if status:
            return list(
                self.conn.execute(
                    """
                    SELECT * FROM scan_jobs WHERE status = ?
                    ORDER BY created_at DESC LIMIT ?
                    """,
                    (status, limit),
                )
            )
        return list(
            self.conn.execute(
                "SELECT * FROM scan_jobs ORDER BY created_at DESC LIMIT ?",
                (limit,),
            )
        )

    def active_scan_job(self, account: str) -> sqlite3.Row | None:
        return self.conn.execute(
            """
            SELECT * FROM scan_jobs
            WHERE account = ? AND status IN ('queued', 'running')
            ORDER BY created_at DESC LIMIT 1
            """,
            (account,),
        ).fetchone()

    def interrupt_stale_scan_jobs(self) -> int:
        cursor = self.conn.execute(
            """
            UPDATE scan_jobs
            SET status = 'interrupted',
                error = 'Server restarted; call start_scan_job to resume from checkpoints.',
                completed_at = ?
            WHERE status IN ('queued', 'running')
            """,
            (utcnow(),),
        )
        self.conn.commit()
        return cursor.rowcount

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
        phase: str = "historical",
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO checkpoints (
              account, folder, uidvalidity, low_uid, high_uid, done, phase, scanned, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (account, folder) DO UPDATE SET
              uidvalidity = excluded.uidvalidity,
              low_uid = excluded.low_uid,
              high_uid = excluded.high_uid,
              done = excluded.done,
              phase = excluded.phase,
              scanned = excluded.scanned,
              updated_at = excluded.updated_at
            """,
            (
                account,
                folder,
                uidvalidity,
                low_uid,
                high_uid,
                int(done),
                phase,
                scanned,
                utcnow(),
            ),
        )
        self.conn.commit()

    def clear_checkpoint(self, account: str, folder: str) -> None:
        self.conn.execute(
            "DELETE FROM checkpoints WHERE account = ? AND folder = ?", (account, folder)
        )
        self.conn.commit()

    # -- decisions ---------------------------------------------------------------

    def set_decision(
        self, domain: str, decision: str, notes: str | None = None, source: str = "mcp"
    ) -> None:
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

    def save_confirm_token(self, token: str, decision: str, total: int, snapshot_hash: str) -> None:
        self.conn.execute(
            """
            INSERT INTO confirm_tokens (
              token, decision, total, snapshot_hash, created_at
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (token, decision, total, snapshot_hash, utcnow()),
        )
        self.conn.commit()

    def pop_confirm_token(self, token: str) -> sqlite3.Row | None:
        row = self.conn.execute("SELECT * FROM confirm_tokens WHERE token = ?", (token,)).fetchone()
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
