from yahoo_mail_mcp.imap.scanner import _fetch_response_count, _parse_fetch_responses

HEADERS = (
    b"From: Deals <deals@bigstore.com>\r\n"
    b"Subject: Sale\r\n"
    b"Date: Mon, 01 Jul 2024 10:30:00 +0000\r\n"
    b"List-Unsubscribe: <https://bigstore.com/u>\r\n\r\n"
)


def test_parse_normal_fetch_response():
    responses = [
        (
            b'123 FETCH (UID 4567 INTERNALDATE "01-Jul-2024 10:31:00 +0000" RFC822.SIZE 6867 '
            b"BODY[HEADER.FIELDS (FROM SUBJECT DATE LIST-UNSUBSCRIBE LIST-UNSUBSCRIBE-POST)] {%d}"
            % len(HEADERS),
            HEADERS,
        ),
        b")",
    ]
    records = _parse_fetch_responses(responses)
    assert _fetch_response_count(responses) == 1
    assert len(records) == 1
    rec = records[0]
    assert rec["uid"] == 4567
    assert rec["size_bytes"] == 6867
    assert rec["sender_domain"] == "bigstore.com"
    assert rec["unsub_http"] == "https://bigstore.com/u"


def test_parse_uidfetch_response():
    # Yahoo UIDONLY mode: leading number IS the uid, no UID attribute.
    responses = [
        (
            b'44631 UIDFETCH (INTERNALDATE "01-Dec-2021 06:38:27 +0000" RFC822.SIZE 100 '
            b"BODY[HEADER.FIELDS (FROM SUBJECT DATE LIST-UNSUBSCRIBE LIST-UNSUBSCRIBE-POST)] {%d}"
            % len(HEADERS),
            HEADERS,
        ),
        b")",
    ]
    records = _parse_fetch_responses(responses)
    assert len(records) == 1
    assert records[0]["uid"] == 44631
    assert records[0]["size_bytes"] == 100


def test_parse_skips_junk_items():
    assert _parse_fetch_responses([b")", None, b"* OK still here"]) == []


def test_missing_date_falls_back_to_internaldate():
    headers = b"From: a@b.com\r\n\r\n"
    responses = [
        (
            b'1 FETCH (UID 9 INTERNALDATE "01-Jul-2024 10:31:00 +0000" RFC822.SIZE 10 '
            b"BODY[HEADER.FIELDS (...)] {%d}" % len(headers),
            headers,
        ),
        b")",
    ]
    records = _parse_fetch_responses(responses)
    assert records[0]["date"].startswith("2024-07-01T10:31:00")


def test_parse_nil_header_response_preserves_uid():
    responses = [
        b'99 (INTERNALDATE "01-Jul-2024 10:31:00 +0000" '
        b"RFC822.SIZE 10 BODY[HEADER.FIELDS (...)] NIL)"
    ]

    records = _parse_fetch_responses(responses)
    assert _fetch_response_count(responses) == 1
    assert len(records) == 1
    assert records[0]["uid"] == 99
    assert records[0]["sender_domain"] is None
