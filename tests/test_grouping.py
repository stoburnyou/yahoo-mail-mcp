from conftest import make_message

from yahoo_mail_mcp.analysis import grouping


def seed(store):
    rows = [
        make_message(uid=1),
        make_message(uid=2, subject="Weekly deals", one_click=1,
                     unsub_http="https://bigstore.com/u", date="2024-08-01T00:00:00+00:00"),
        make_message(uid=3, account="work", folder="Inbox",
                     sender_email="news@bigstore.com", date="2023-01-01T00:00:00+00:00"),
        make_message(uid=4, sender_email="friend@gmail.com", sender_domain="gmail.com",
                     subject="hey", list_unsub_raw=None, unsub_mailto=None),
    ]
    store.upsert_messages(rows)


def test_group_aggregation(store):
    seed(store)
    groups = grouping.list_sender_groups(store.conn)
    assert [g["sender_domain"] for g in groups] == ["bigstore.com", "gmail.com"]

    big = groups[0]
    assert big["message_count"] == 3
    assert set(big["accounts"]) == {"personal", "work"}
    assert set(big["sender_addresses"]) == {"deals@bigstore.com", "news@bigstore.com"}
    assert big["first_seen"] == "2023-01-01"
    assert big["last_seen"] == "2024-08-01"
    assert big["unsubscribe"]["available"] is True
    assert "one_click" in big["unsubscribe"]["methods"]
    assert big["decision"] == "needs_review"

    gmail = groups[1]
    assert gmail["unsubscribe"]["available"] is False


def test_decision_filter_and_account_filter(store):
    seed(store)
    store.set_decision("bigstore.com", "delete")

    assert [g["sender_domain"] for g in grouping.list_sender_groups(store.conn, decision="delete")] == ["bigstore.com"]
    assert [g["sender_domain"] for g in grouping.list_sender_groups(store.conn, decision="needs_review")] == ["gmail.com"]

    work_only = grouping.list_sender_groups(store.conn, account="work")
    assert len(work_only) == 1
    assert work_only[0]["message_count"] == 1


def test_sender_detail(store):
    seed(store)
    detail = grouping.get_sender_detail(store.conn, "BIGSTORE.COM")
    assert detail is not None
    assert detail["message_count"] == 3
    assert {(f["account"], f["folder"]) for f in detail["per_folder"]} == {
        ("personal", "Inbox"),
        ("work", "Inbox"),
    }
    assert grouping.get_sender_detail(store.conn, "nope.com") is None


def test_deleted_messages_excluded(store):
    seed(store)
    store.mark_deleted("personal", "Inbox", [1, 2])
    groups = grouping.list_sender_groups(store.conn)
    big = next(g for g in groups if g["sender_domain"] == "bigstore.com")
    assert big["message_count"] == 1
