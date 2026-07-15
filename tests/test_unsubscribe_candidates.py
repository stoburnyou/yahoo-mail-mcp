import json

import pytest
from conftest import make_message

from yahoo_mail_mcp.analysis import unsubscribe_candidates


def seed(store):
    store.upsert_messages(
        [
            make_message(
                uid=1,
                account="personal",
                subject="Big Store offers",
                one_click=1,
                unsub_http="https://bigstore.com/unsubscribe",
            ),
            make_message(
                uid=2,
                account="personal",
                subject="Big Store receipt",
                list_unsub_raw=None,
                unsub_mailto=None,
                unsub_http=None,
                one_click=0,
            ),
            make_message(uid=1, account="work", subject="Work store offers"),
            make_message(
                uid=3,
                account="personal",
                sender_email="news@manual.example",
                sender_domain="manual.example",
                subject="Manual newsletter",
                list_unsub_raw="<https://manual.example/unsubscribe>",
                unsub_mailto=None,
                unsub_http="https://manual.example/unsubscribe",
                one_click=0,
            ),
            make_message(
                uid=4,
                account="personal",
                sender_email="news@insecure.example",
                sender_domain="insecure.example",
                subject="Insecure newsletter",
                list_unsub_raw="<http://insecure.example/unsubscribe>",
                unsub_mailto=None,
                unsub_http="http://insecure.example/unsubscribe",
                one_click=1,
            ),
            make_message(
                uid=5,
                account="personal",
                sender_email="news@broken.example",
                sender_domain="broken.example",
                subject="Broken newsletter",
                list_unsub_raw="not a usable URI",
                unsub_mailto=None,
                unsub_http=None,
                one_click=0,
            ),
        ]
    )
    store.set_decision("personal", "bigstore.com", "archive")


def test_lists_classified_candidates_without_raw_urls(store):
    seed(store)

    rows, total, limit, offset = unsubscribe_candidates.list_unsubscribe_candidates(
        store.conn,
        account="personal",
    )

    assert total == 3
    assert limit == 50
    assert offset == 0
    bigstore = next(row for row in rows if row["sender_domain"] == "bigstore.com")
    assert bigstore["message_count"] == 2
    assert bigstore["candidate_message_count"] == 1
    assert bigstore["coverage_pct"] == 50
    assert bigstore["methods"] == ["one_click", "mailto"]
    assert bigstore["preferred_method"] == "one_click"
    assert bigstore["method_message_counts"] == {
        "one_click": 1,
        "mailto": 1,
        "manual": 0,
    }
    assert bigstore["automatic_candidate"] is True
    assert bigstore["decision"] == "archive"

    serialized = json.dumps(rows)
    assert "https://" not in serialized
    assert "http://" not in serialized
    assert "mailto:" not in serialized
    assert "@" not in serialized


def test_one_click_requires_post_header_and_https_endpoint(store):
    seed(store)

    rows, total, *_ = unsubscribe_candidates.list_unsubscribe_candidates(
        store.conn,
        account="personal",
        method="one_click",
    )

    assert total == 1
    assert [row["sender_domain"] for row in rows] == ["bigstore.com"]

    manual, manual_total, *_ = unsubscribe_candidates.list_unsubscribe_candidates(
        store.conn,
        account="personal",
        method="manual",
        sort="domain",
    )
    assert manual_total == 2
    assert [row["sender_domain"] for row in manual] == [
        "insecure.example",
        "manual.example",
    ]


def test_candidates_are_isolated_by_account(store):
    seed(store)

    personal, *_ = unsubscribe_candidates.list_unsubscribe_candidates(
        store.conn,
        account="personal",
        method="one_click",
    )
    work, work_total, *_ = unsubscribe_candidates.list_unsubscribe_candidates(
        store.conn,
        account="work",
        method="one_click",
    )
    work_mailto, mailto_total, *_ = unsubscribe_candidates.list_unsubscribe_candidates(
        store.conn,
        account="work",
        method="mailto",
    )

    assert [row["sender_domain"] for row in personal] == ["bigstore.com"]
    assert work == []
    assert work_total == 0
    assert mailto_total == 1
    assert work_mailto[0]["decision"] == "needs_review"


def test_deleted_candidates_pagination_and_validation(store):
    seed(store)
    store.mark_deleted("personal", "Inbox", [3])

    page, total, limit, offset = unsubscribe_candidates.list_unsubscribe_candidates(
        store.conn,
        account="personal",
        limit=1,
        offset=0,
    )

    assert total == 2
    assert limit == 1
    assert offset == 0
    assert page[0]["sender_domain"] == "bigstore.com"

    with pytest.raises(ValueError, match="method must be"):
        unsubscribe_candidates.list_unsubscribe_candidates(
            store.conn,
            account="personal",
            method="unsafe",
        )
