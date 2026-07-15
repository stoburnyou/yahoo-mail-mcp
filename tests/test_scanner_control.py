import pytest

from yahoo_mail_mcp.imap import scanner

HEADERS = b"From: Sender <news@example.com>\r\nSubject: Update\r\n\r\n"


class FakeImap:
    message_limit = 1000

    def __init__(self, folders, uidonly=True, uidvalidity=1):
        self.folders = folders
        self.selected = None
        self.uidonly = uidonly
        self.uidvalidity = uidvalidity

    def enable_uidonly(self):
        return self.uidonly

    def select_folder(self, folder, readonly=True):
        self.selected = folder
        message_count = self.folders[folder]
        return {
            b"UIDVALIDITY": self.uidvalidity,
            b"UIDNEXT": message_count + 1,
            b"EXISTS": message_count,
        }

    def with_retry(self, _op_name, fn):
        return fn()


def install_fake_fetch(monkeypatch, calls):
    def fake_fetch(imap, uid_range, partial=None):
        assert partial is not None
        fetch_size = int(partial.rsplit(":", 1)[1].lstrip("-"))
        low, high = (int(value) for value in uid_range.split(":", 1))
        calls.append((imap.selected, uid_range, fetch_size))
        if partial.startswith("-"):
            uids = range(high, max(low, high - fetch_size + 1) - 1, -1)
        else:
            uids = range(low, min(high, low + fetch_size - 1) + 1)
        return [
            (
                f'{uid} UIDFETCH (INTERNALDATE "01-Jul-2024 10:31:00 +0000" '
                f"RFC822.SIZE 100 BODY[HEADER.FIELDS (...)] {{{len(HEADERS)}}}".encode(),
                HEADERS,
            )
            for uid in uids
        ]

    monkeypatch.setattr(scanner, "_raw_uid_fetch", fake_fetch)


def test_max_messages_caps_first_fetch_and_resume(store, monkeypatch):
    calls = []
    install_fake_fetch(monkeypatch, calls)
    imap = FakeImap({"Inbox": 1000})

    first = scanner.scan_folder(imap, store, "personal", "Inbox", batch_size=500, max_messages=100)
    assert first.scanned == 100
    assert first.done is False
    assert calls == [("Inbox", "1:1000", 100)]
    assert store.message_count("personal") == 100

    second = scanner.scan_folder(imap, store, "personal", "Inbox", batch_size=500, max_messages=25)
    assert second.scanned == 25
    assert second.done is False
    assert calls[-1] == ("Inbox", "1:900", 25)
    assert store.message_count("personal") == 125


def test_max_messages_caps_whole_mailbox_not_each_folder(store, monkeypatch):
    calls = []
    install_fake_fetch(monkeypatch, calls)
    imap = FakeImap({"Inbox": 75, "Spam": 100})

    report = scanner.scan_mailbox(
        imap,
        store,
        "personal",
        folders=["Inbox", "Spam"],
        batch_size=500,
        max_messages=120,
    )

    assert report.total_scanned == 120
    assert [(f.folder, f.scanned) for f in report.folders] == [
        ("Inbox", 75),
        ("Spam", 45),
    ]
    assert calls == [
        ("Inbox", "1:75", 120),
        ("Spam", "1:100", 45),
    ]


def test_max_messages_must_be_positive(store):
    with pytest.raises(ValueError, match="positive integer"):
        scanner.scan_mailbox(
            FakeImap({"Inbox": 10}),
            store,
            "personal",
            folders=["Inbox"],
            batch_size=500,
            max_messages=0,
        )


def test_limited_mode_refuses_potentially_truncated_folder(store):
    imap = FakeImap({"Inbox": 1000}, uidonly=False)

    with pytest.raises(scanner.YahooImapError, match="incomplete scan"):
        scanner.scan_folder(
            imap,
            store,
            "personal",
            "Inbox",
            batch_size=500,
            max_messages=100,
        )


def test_incremental_scan_resumes_without_skipping_new_mail(store, monkeypatch):
    calls = []
    install_fake_fetch(monkeypatch, calls)
    imap = FakeImap({"Inbox": 10})

    initial = scanner.scan_folder(imap, store, "personal", "Inbox", batch_size=500)
    assert initial.done is True

    imap.folders["Inbox"] = 30
    first = scanner.scan_folder(imap, store, "personal", "Inbox", batch_size=500, max_messages=5)
    assert first.scanned == 5
    checkpoint = store.get_checkpoint("personal", "Inbox")
    assert checkpoint["phase"] == "incremental"
    assert checkpoint["low_uid"] == 16
    assert checkpoint["high_uid"] == 30
    assert checkpoint["done"] == 0

    second = scanner.scan_folder(imap, store, "personal", "Inbox", batch_size=500, max_messages=5)
    assert second.scanned == 5
    checkpoint = store.get_checkpoint("personal", "Inbox")
    assert checkpoint["low_uid"] == 21
    assert checkpoint["done"] == 0

    final = scanner.scan_folder(imap, store, "personal", "Inbox", batch_size=500)
    assert final.scanned == 10
    assert final.done is True
    checkpoint = store.get_checkpoint("personal", "Inbox")
    assert checkpoint["phase"] == "complete"
    assert checkpoint["high_uid"] == 30
    assert store.message_count("personal") == 30


def test_historical_resume_does_not_advance_past_new_mail(store, monkeypatch):
    calls = []
    install_fake_fetch(monkeypatch, calls)
    imap = FakeImap({"Inbox": 1000})

    scanner.scan_folder(imap, store, "personal", "Inbox", batch_size=500, max_messages=100)
    imap.folders["Inbox"] = 1100
    scanner.scan_folder(imap, store, "personal", "Inbox", batch_size=500, max_messages=100)

    checkpoint = store.get_checkpoint("personal", "Inbox")
    assert checkpoint["phase"] == "historical"
    assert checkpoint["high_uid"] == 1000
    assert calls[-1] == ("Inbox", "1:900", 100)


def test_uidvalidity_change_removes_stale_folder_rows(store, monkeypatch):
    calls = []
    install_fake_fetch(monkeypatch, calls)
    imap = FakeImap({"Inbox": 20})

    scanner.scan_folder(imap, store, "personal", "Inbox", batch_size=500, max_messages=10)
    assert store.message_count("personal") == 10

    imap.uidvalidity = 2
    scanner.scan_folder(imap, store, "personal", "Inbox", batch_size=500, max_messages=5)

    assert store.message_count("personal") == 5
    assert store.get_checkpoint("personal", "Inbox")["uidvalidity"] == 2
