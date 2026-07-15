import os
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


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission modes only")
def test_store_uses_owner_only_permissions(tmp_path):
    path = tmp_path / "private" / "mail.db"
    store = Store(path)
    store.close()

    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
