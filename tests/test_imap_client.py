from yahoo_mail_mcp.config import Account
from yahoo_mail_mcp.imap.client import YahooImap


def test_reconnect_restores_uidonly_mode(monkeypatch):
    imap = YahooImap(Account("personal", "user@yahoo.com", "app-password"))
    imap._uidonly_enabled = True
    calls = []

    def fake_close():
        calls.append("close")

    def fake_connect():
        calls.append("connect")
        imap._uidonly_enabled = False

    def fake_enable_uidonly():
        calls.append("enable_uidonly")
        imap._uidonly_enabled = True
        return True

    monkeypatch.setattr(imap, "close", fake_close)
    monkeypatch.setattr(imap, "connect", fake_connect)
    monkeypatch.setattr(imap, "enable_uidonly", fake_enable_uidonly)

    imap._reconnect()

    assert calls == ["close", "connect", "enable_uidonly"]
    assert imap.uidonly is True


def test_enable_uidonly_reconnects_when_folder_is_selected(monkeypatch):
    imap = YahooImap(Account("personal", "user@yahoo.com", "app-password"))
    imap._selected_folder = "Inbox"
    calls = []

    class Client:
        def has_capability(self, capability):
            calls.append(("has_capability", capability))
            return True

        def enable(self, capability):
            calls.append(("enable", capability))
            return [b"UIDONLY"]

    def fake_close():
        calls.append("close")

    def fake_connect():
        calls.append("connect")
        imap._client = Client()
        imap._selected_folder = None

    monkeypatch.setattr(imap, "close", fake_close)
    monkeypatch.setattr(imap, "connect", fake_connect)

    assert imap.enable_uidonly() is True
    assert calls == [
        "close",
        "connect",
        ("has_capability", "UIDONLY"),
        ("enable", "UIDONLY"),
    ]
