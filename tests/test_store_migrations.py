import os
import sqlite3
import stat

import pytest
from conftest import make_message

from yahoo_mail_mcp.store.db import Store


def test_legacy_sender_domains_migrate_to_exact_host(tmp_path):
    path = tmp_path / "legacy.db"
    store = Store(path)
    store.upsert_messages(
        [
            make_message(
                sender_email="news@mailer.example.com",
                sender_domain="example.com",
            )
        ]
    )
    store.set_decision("example.com", "delete")
    store.conn.execute("DELETE FROM schema_meta WHERE key = 'sender_domain_format'")
    store.conn.commit()
    store.close()

    migrated = Store(path)
    row = migrated.conn.execute("SELECT sender_domain FROM messages WHERE uid = 1").fetchone()
    assert row["sender_domain"] == "mailer.example.com"
    assert migrated.get_decisions() == []
    audit = migrated.conn.execute(
        "SELECT count FROM action_log WHERE action = 'invalidate_legacy_decisions'"
    ).fetchone()
    assert audit["count"] == 1
    migrated.close()


def test_legacy_decisions_schema_adds_archive(tmp_path):
    path = tmp_path / "legacy-decisions.db"
    conn = sqlite3.connect(path)
    conn.execute(
        """
        CREATE TABLE decisions (
          sender_domain TEXT PRIMARY KEY,
          decision TEXT NOT NULL
                   CHECK (decision IN ('keep','unsubscribe','delete','needs_review')),
          notes TEXT,
          source TEXT NOT NULL DEFAULT 'mcp',
          updated_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        "INSERT INTO decisions VALUES (?, ?, ?, ?, ?)",
        ("example.com", "keep", None, "mcp", "2026-07-15T00:00:00+00:00"),
    )
    conn.execute("CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    conn.execute("INSERT INTO schema_meta VALUES ('sender_domain_format', 'exact-v2')")
    conn.commit()
    conn.close()

    store = Store(path)
    store.set_decision("archive.example.com", "archive")

    assert {row["decision"] for row in store.get_decisions()} == {"keep", "archive"}
    store.close()


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission modes only")
def test_store_uses_owner_only_permissions(tmp_path):
    path = tmp_path / "private" / "mail.db"
    store = Store(path)
    store.close()

    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission modes only")
def test_store_does_not_chmod_existing_parent_directory(tmp_path):
    shared = tmp_path / "shared"
    shared.mkdir(mode=0o755)
    shared.chmod(0o755)

    store = Store(shared / "mail.db")
    store.close()

    assert stat.S_IMODE(shared.stat().st_mode) == 0o755
