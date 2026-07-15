from conftest import make_message

from yahoo_mail_mcp.analysis import grouping


def seed(store):
    rows = [
        make_message(uid=1),
        make_message(
            uid=2,
            subject="Weekly deals",
            one_click=1,
            unsub_http="https://bigstore.com/u",
            date="2024-08-01T00:00:00+00:00",
        ),
        make_message(
            uid=3,
            account="work",
            folder="Inbox",
            sender_email="news@bigstore.com",
            date="2023-01-01T00:00:00+00:00",
        ),
        make_message(
            uid=4,
            sender_email="friend@gmail.com",
            sender_domain="gmail.com",
            subject="hey",
            list_unsub_raw=None,
            unsub_mailto=None,
        ),
    ]
    store.upsert_messages(rows)


def test_group_aggregation(store):
    seed(store)
    groups = grouping.list_sender_groups(store.conn)
    assert len(groups) == 3

    big = next(
        group
        for group in groups
        if group["account"] == "personal" and group["sender_domain"] == "bigstore.com"
    )
    assert big["message_count"] == 2
    assert big["accounts"] == ["personal"]
    assert big["sender_addresses"] == ["deals@bigstore.com"]
    assert big["first_seen"] == "2024-07-01"
    assert big["last_seen"] == "2024-08-01"
    assert big["unsubscribe"]["available"] is True
    assert "one_click" in big["unsubscribe"]["methods"]
    assert big["decision"] == "needs_review"

    gmail = next(group for group in groups if group["sender_domain"] == "gmail.com")
    assert gmail["unsubscribe"]["available"] is False


def test_decision_filter_and_account_filter(store):
    seed(store)
    store.set_decision("personal", "bigstore.com", "delete")
    store.set_decision("work", "bigstore.com", "keep")

    deleted = grouping.list_sender_groups(store.conn, decision="delete")
    assert [(group["account"], group["sender_domain"]) for group in deleted] == [
        ("personal", "bigstore.com")
    ]
    needs_review = grouping.list_sender_groups(store.conn, decision="needs_review")
    assert {(group["account"], group["sender_domain"]) for group in needs_review} == {
        ("personal", "gmail.com"),
    }
    kept = grouping.list_sender_groups(store.conn, decision="keep")
    assert [(group["account"], group["sender_domain"]) for group in kept] == [
        ("work", "bigstore.com")
    ]

    work_only = grouping.list_sender_groups(store.conn, account="work")
    assert len(work_only) == 1
    assert work_only[0]["message_count"] == 1


def test_sender_detail(store):
    seed(store)
    detail = grouping.get_sender_detail(store.conn, "personal", "BIGSTORE.COM")
    assert detail is not None
    assert detail["message_count"] == 2
    assert {(f["account"], f["folder"]) for f in detail["per_folder"]} == {
        ("personal", "Inbox"),
    }
    assert grouping.get_sender_detail(store.conn, "personal", "nope.com") is None


def test_deleted_messages_excluded(store):
    seed(store)
    store.mark_deleted("personal", "Inbox", [1, 2])
    groups = grouping.list_sender_groups(store.conn)
    big = next(g for g in groups if g["sender_domain"] == "bigstore.com")
    assert big["message_count"] == 1
