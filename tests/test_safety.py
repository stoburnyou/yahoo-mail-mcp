from conftest import make_message

from yahoo_mail_mcp import safety
from yahoo_mail_mcp.imap.actions import _chunk_uid_set, _expand_chunk


def seed_tagged(store, n=10):
    store.upsert_messages([make_message(uid=i) for i in range(1, n + 1)])
    store.set_decision("bigstore.com", "delete")


def test_preview_counts_and_token(store):
    seed_tagged(store, 10)
    result = safety.preview(store, "delete")
    assert result["total_messages"] == 10
    assert result["domains"] == {"bigstore.com": 10}
    assert result["per_account"] == {"personal": 10}
    assert result["confirm_token"]


def test_token_single_use(store):
    seed_tagged(store, 10)
    token = safety.preview(store, "delete")["confirm_token"]
    assert safety.validate_token(store, token, "delete", 10) is None
    err = safety.validate_token(store, token, "delete", 10)
    assert err and "already-used" in err


def test_token_wrong_decision(store):
    seed_tagged(store, 10)
    token = safety.preview(store, "delete")["confirm_token"]
    err = safety.validate_token(store, token, "unsubscribe", 10)
    assert err and "issued for decision" in err


def test_token_drift_rejected(store):
    seed_tagged(store, 100)
    token = safety.preview(store, "delete")["confirm_token"]
    err = safety.validate_token(store, token, "delete", 200)
    assert err and "changed since preview" in err


def test_pending_excludes_untagged_and_deleted(store):
    store.upsert_messages(
        [
            make_message(uid=1),
            make_message(uid=2, sender_email="x@other.com", sender_domain="other.com"),
        ]
    )
    store.set_decision("bigstore.com", "delete")
    assert len(safety.pending_messages(store.conn, "delete")) == 1
    store.mark_deleted("personal", "Inbox", [1])
    assert len(safety.pending_messages(store.conn, "delete")) == 0


def test_chunk_uid_set_ranges():
    chunks = _chunk_uid_set([1, 2, 3, 7, 9, 10], size=100)
    assert chunks == ["1:3,7,9:10"]
    assert _expand_chunk(chunks[0]) == [1, 2, 3, 7, 9, 10]


def test_chunk_uid_set_splits_by_size():
    chunks = _chunk_uid_set(list(range(1, 501)), size=200)
    assert len(chunks) == 3
    assert sum(len(_expand_chunk(c)) for c in chunks) == 500
