import pytest

from yahoo_mail_mcp.store.db import Store


@pytest.fixture()
def store(tmp_path):
    s = Store(tmp_path / "test.db")
    yield s
    s.close()


def make_message(**overrides) -> dict:
    base = {
        "account": "personal",
        "folder": "Inbox",
        "uid": 1,
        "uidvalidity": 100,
        "sender_email": "deals@bigstore.com",
        "sender_domain": "bigstore.com",
        "sender_name": "Big Store",
        "subject": "50% off",
        "date": "2024-07-01T10:30:00+00:00",
        "size_bytes": 5000,
        "list_unsub_raw": "<mailto:unsub@bigstore.com>",
        "unsub_mailto": "mailto:unsub@bigstore.com",
        "unsub_http": None,
        "one_click": 0,
        "scanned_at": "2026-07-14T00:00:00+00:00",
    }
    base.update(overrides)
    return base
