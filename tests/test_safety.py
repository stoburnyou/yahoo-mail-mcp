from conftest import make_message

from yahoo_mail_mcp import safety
from yahoo_mail_mcp.imap.actions import _chunk_uid_set, _expand_chunk


def seed_tagged(store, n=10):
    store.upsert_messages([make_message(uid=i) for i in range(1, n + 1)])
    store.set_decision("personal", "bigstore.com", "delete")


def test_preview_counts_and_token(store):
    seed_tagged(store, 10)
    result = safety.preview(store, "personal", "delete")
    assert result["total_messages"] == 10
    assert result["domains"] == {"bigstore.com": 10}
    assert result["per_account"] == {"personal": 10}
    assert result["confirm_token"]


def test_preview_supports_archive(store):
    store.upsert_messages([make_message(uid=1)])
    store.set_decision("personal", "bigstore.com", "archive")

    result = safety.preview(store, "personal", "archive")

    assert result["total_messages"] == 1
    assert result["domains"] == {"bigstore.com": 1}


def test_token_single_use(store):
    seed_tagged(store, 10)
    token = safety.preview(store, "personal", "delete")["confirm_token"]
    rows = safety.pending_messages(store.conn, "personal", "delete")
    assert safety.validate_token(store, token, "delete", rows) is None
    err = safety.validate_token(store, token, "delete", rows)
    assert err and "already-used" in err


def test_token_wrong_decision(store):
    seed_tagged(store, 10)
    token = safety.preview(store, "personal", "delete")["confirm_token"]
    rows = safety.pending_messages(store.conn, "personal", "delete")
    err = safety.validate_token(store, token, "unsubscribe", rows)
    assert err and "issued for decision" in err


def test_token_expires(store):
    seed_tagged(store, 10)
    token = safety.preview(store, "personal", "delete")["confirm_token"]
    store.conn.execute(
        "UPDATE confirm_tokens SET created_at = ? WHERE token = ?",
        ("2000-01-01T00:00:00+00:00", token),
    )
    store.conn.commit()

    rows = safety.pending_messages(store.conn, "personal", "delete")
    err = safety.validate_token(store, token, "delete", rows)
    assert err and "expired" in err


def test_token_drift_rejected(store):
    seed_tagged(store, 100)
    token = safety.preview(store, "personal", "delete")["confirm_token"]
    rows = safety.pending_messages(store.conn, "personal", "delete")
    err = safety.validate_token(store, token, "delete", rows * 2)
    assert err and "changed since preview" in err


def test_token_rejects_changed_messages_at_same_count(store):
    seed_tagged(store, 10)
    token = safety.preview(store, "personal", "delete")["confirm_token"]
    store.mark_deleted("personal", "Inbox", [1])
    store.upsert_messages(
        [
            make_message(
                uid=11,
                sender_email="alerts@bigstore.com",
                sender_domain="bigstore.com",
            )
        ]
    )

    rows = safety.pending_messages(store.conn, "personal", "delete")
    assert len(rows) == 10
    err = safety.validate_token(store, token, "delete", rows)
    assert err and "Affected messages" in err


def test_pending_excludes_untagged_and_deleted(store):
    store.upsert_messages(
        [
            make_message(uid=1),
            make_message(uid=2, sender_email="x@other.com", sender_domain="other.com"),
        ]
    )
    store.set_decision("personal", "bigstore.com", "delete")
    assert len(safety.pending_messages(store.conn, "personal", "delete")) == 1
    store.mark_deleted("personal", "Inbox", [1])
    assert len(safety.pending_messages(store.conn, "personal", "delete")) == 0


def test_decision_only_targets_matching_account(store):
    store.upsert_messages(
        [
            make_message(uid=1, account="personal"),
            make_message(uid=1, account="work"),
        ]
    )
    store.set_decision("personal", "bigstore.com", "delete")

    personal = safety.pending_messages(store.conn, "personal", "delete")
    work = safety.pending_messages(store.conn, "work", "delete")

    assert [row["account"] for row in personal] == ["personal"]
    assert work == []


def test_chunk_uid_set_ranges():
    chunks = _chunk_uid_set([1, 2, 3, 7, 9, 10], size=100)
    assert chunks == ["1:3,7,9:10"]
    assert _expand_chunk(chunks[0]) == [1, 2, 3, 7, 9, 10]


def test_chunk_uid_set_splits_by_size():
    chunks = _chunk_uid_set(list(range(1, 501)), size=200)
    assert len(chunks) == 3
    assert sum(len(_expand_chunk(c)) for c in chunks) == 500
