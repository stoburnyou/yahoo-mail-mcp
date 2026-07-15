from yahoo_mail_mcp.analysis.headers import (
    parse_headers,
    parse_list_unsubscribe,
    registrable_domain,
)

RAW = b"""From: "Big Store" <deals@e.bigstore.com>\r
Subject: =?utf-8?q?50=25_off_everything!?=\r
Date: Mon, 01 Jul 2024 10:30:00 +0000\r
List-Unsubscribe: <mailto:unsub@bigstore.com?subject=stop>, <https://bigstore.com/unsub?u=1>\r
List-Unsubscribe-Post: List-Unsubscribe=One-Click\r
\r
"""


def test_parse_headers_full():
    p = parse_headers(RAW)
    assert p.sender_email == "deals@e.bigstore.com"
    assert p.sender_domain == "bigstore.com"
    assert p.sender_name == "Big Store"
    assert p.subject == "50% off everything!"
    assert p.date.startswith("2024-07-01T10:30:00")
    assert p.unsub_mailto == "mailto:unsub@bigstore.com?subject=stop"
    assert p.unsub_http == "https://bigstore.com/unsub?u=1"
    assert p.one_click is True


def test_parse_headers_minimal():
    p = parse_headers(b"From: someone@example.com\r\n\r\n")
    assert p.sender_email == "someone@example.com"
    assert p.sender_domain == "example.com"
    assert p.subject is None
    assert p.list_unsub_raw is None
    assert p.one_click is False


def test_parse_headers_garbage():
    p = parse_headers(b"")
    assert p.sender_email is None
    assert p.sender_domain is None


def test_registrable_domain():
    assert registrable_domain("a@news.mailer.example.co.uk") == "example.co.uk"
    assert registrable_domain("a@example.com") == "example.com"
    assert registrable_domain("not-an-email") is None


def test_parse_list_unsubscribe_order_independent():
    mailto, http = parse_list_unsubscribe(
        "<https://x.com/u>, <mailto:stop@x.com>"
    )
    assert mailto == "mailto:stop@x.com"
    assert http == "https://x.com/u"
