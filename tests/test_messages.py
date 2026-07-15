import pytest
from conftest import make_message

from yahoo_mail_mcp.analysis import messages


def seed(store):
    store.upsert_messages(
        [
            make_message(
                uid=1,
                subject="Older sale",
                date="2024-01-01T10:00:00+00:00",
            ),
            make_message(
                uid=2,
                subject="Newest ticket update",
                sender_email="news@email.ticketmaster.com",
                sender_domain="email.ticketmaster.com",
                date="2024-03-01T10:00:00+00:00",
                one_click=1,
                unsub_http="https://email.ticketmaster.com/unsubscribe",
            ),
            make_message(
                uid=3,
                subject="Middle message",
                sender_email="friend@example.net",
                sender_domain="example.net",
                date="2024-02-01T10:00:00+00:00",
                list_unsub_raw=None,
                unsub_mailto=None,
            ),
            make_message(
                uid=4,
                account="work",
                folder="Archive",
                subject="Work notice",
                sender_email="alerts@work.example",
                sender_domain="work.example",
                date="2024-04-01T10:00:00+00:00",
            ),
        ]
    )
    store.save_checkpoint(
        "personal",
        "Inbox",
        uidvalidity=100,
        low_uid=1,
        high_uid=3,
        done=True,
        scanned=3,
        phase="complete",
    )
    store.save_checkpoint(
        "work",
        "Archive",
        uidvalidity=999,
        low_uid=1,
        high_uid=4,
        done=True,
        scanned=1,
        phase="complete",
    )


def test_recent_orders_filters_and_hides_email(store):
    seed(store)
    page, total, limit, offset = messages.list_recent_messages(
        store.conn, account="personal", folder="Inbox"
    )

    assert [row["uid"] for row in page] == [2, 3, 1]
    assert total == 3
    assert limit == 50
    assert offset == 0
    assert "from_email" not in page[0]
    assert page[0]["from_domain"] == "email.ticketmaster.com"
    assert page[0]["staleness"] == "current"


def test_recent_paginates_and_optionally_includes_email(store):
    seed(store)
    page, total, limit, offset = messages.list_recent_messages(
        store.conn,
        account="personal",
        limit=1,
        offset=1,
        include_sender_email=True,
    )

    assert total == 3
    assert limit == 1
    assert offset == 1
    assert page[0]["uid"] == 3
    assert page[0]["from_email"] == "friend@example.net"


def test_recent_since_and_deleted_filter(store):
    seed(store)
    store.mark_deleted("personal", "Inbox", [2])
    page, total, *_ = messages.list_recent_messages(
        store.conn,
        account="personal",
        since="2024-01-15T00:00:00+00:00",
    )

    assert total == 1
    assert [row["uid"] for row in page] == [3]


def test_recent_truncates_subject_and_caps_limit(store):
    seed(store)
    store.upsert_messages([make_message(uid=10, subject="x" * 300)])
    page, _total, limit, _offset = messages.list_recent_messages(
        store.conn, account="personal", limit=10_000
    )

    row = next(item for item in page if item["uid"] == 10)
    assert len(row["subject"]) == messages.MAX_SUBJECT_PREVIEW
    assert row["subject"].endswith("…")
    assert limit == messages.MAX_LIMIT


def test_search_combines_filters_and_decisions(store):
    seed(store)
    store.set_decision("email.ticketmaster.com", "delete")

    page, total, *_ = messages.search_messages(
        store.conn,
        query="ticket",
        decision="delete",
        has_unsubscribe=True,
    )

    assert total == 1
    assert page[0]["uid"] == 2
    assert page[0]["decision"] == "delete"


def test_search_exact_sender_and_date_range(store):
    seed(store)
    page, total, *_ = messages.search_messages(
        store.conn,
        sender_domain="EXAMPLE.NET",
        since="2024-01-15T00:00:00+00:00",
        until="2024-02-15T00:00:00+00:00",
    )

    assert total == 1
    assert page[0]["uid"] == 3


def test_search_escapes_like_wildcards(store):
    seed(store)
    store.upsert_messages([make_message(uid=10, subject="Save 50% today")])

    page, total, *_ = messages.search_messages(store.conn, query="50%")
    assert total == 1
    assert page[0]["uid"] == 10


def test_search_staleness_and_exclusion(store):
    seed(store)
    store.conn.execute(
        "UPDATE messages SET uidvalidity = 101 WHERE account = 'personal' AND uid = 3"
    )
    store.conn.commit()

    page, total, *_ = messages.search_messages(store.conn, sender_domain="example.net")
    assert total == 1
    assert page[0]["staleness"] == "uidvalidity_mismatch"

    page, total, *_ = messages.search_messages(
        store.conn, sender_domain="example.net", exclude_stale=True
    )
    assert page == []
    assert total == 0


def test_message_without_checkpoint_is_marked_stale(store):
    store.upsert_messages([make_message(uid=1)])
    page, _total, *_ = messages.list_recent_messages(store.conn)
    assert page[0]["staleness"] == "folder_not_checkpointed"


def test_get_headers_returns_detail_without_raw_unsubscribe_urls(store):
    seed(store)
    detail = messages.get_message_headers(store.conn, account="personal", folder="Inbox", uid=2)

    assert detail is not None
    assert detail["from"]["email"] == "news@email.ticketmaster.com"
    assert detail["unsubscribe"] == {
        "available": True,
        "methods": ["one_click", "mailto", "http"],
    }
    serialized_keys = repr(detail)
    assert "unsub_http" not in serialized_keys
    assert "list_unsub_raw" not in serialized_keys


def test_get_headers_not_found(store):
    seed(store)
    assert (
        messages.get_message_headers(store.conn, account="personal", folder="Inbox", uid=999)
        is None
    )


def test_invalid_date_and_sort_are_rejected(store):
    seed(store)
    with pytest.raises(ValueError, match="ISO 8601"):
        messages.list_recent_messages(store.conn, since="yesterday")
    with pytest.raises(ValueError, match="Invalid sort"):
        messages.search_messages(store.conn, sort="random")
