import pytest

from yahoo_mail_mcp.config import Account
from yahoo_mail_mcp.unsubscribe import (
    _validate_one_click_url,
    mailto_unsubscribe,
    one_click_unsubscribe,
)


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com/unsubscribe",
        "https://localhost/unsubscribe",
        "https://service.local/unsubscribe",
        "https://127.0.0.1/unsubscribe",
        "https://10.0.0.1/unsubscribe",
        "https://user:password@example.com/unsubscribe",
    ],
)
def test_one_click_rejects_unsafe_urls(url):
    assert _validate_one_click_url(url)


def test_one_click_accepts_public_https_url():
    assert _validate_one_click_url("https://example.com/unsubscribe?token=abc") is None


def test_one_click_does_not_request_rejected_url(monkeypatch):
    def fail_if_called(*_args, **_kwargs):
        raise AssertionError("HTTP request should not be made")

    monkeypatch.setattr("yahoo_mail_mcp.unsubscribe._public_addresses", fail_if_called)

    ok, detail = one_click_unsubscribe("http://127.0.0.1/internal", "example.com")
    assert ok is False
    assert "Refused unsafe" in detail


def test_one_click_rejects_hostname_resolving_private(monkeypatch):
    monkeypatch.setattr(
        "yahoo_mail_mcp.unsubscribe.socket.getaddrinfo",
        lambda *_args, **_kwargs: [
            (2, 1, 6, "", ("127.0.0.1", 443)),
        ],
    )

    ok, detail = one_click_unsubscribe("https://example.com/unsubscribe", "example.com")
    assert ok is False
    assert "non-public IP" in detail


def test_one_click_does_not_follow_redirects(monkeypatch):
    monkeypatch.setattr(
        "yahoo_mail_mcp.unsubscribe.socket.getaddrinfo",
        lambda *_args, **_kwargs: [
            (2, 1, 6, "", ("93.184.216.34", 443)),
        ],
    )

    class Response:
        status = 302

        def read(self, _limit):
            return b""

    class Connection:
        def __init__(self, *_args):
            pass

        def request(self, method, path, body, headers):
            assert method == "POST"
            assert path == "/unsubscribe"

        def getresponse(self):
            return Response()

        def close(self):
            pass

    monkeypatch.setattr("yahoo_mail_mcp.unsubscribe._PinnedHTTPSConnection", Connection)

    ok, detail = one_click_unsubscribe("https://example.com/unsubscribe", "example.com")
    assert ok is False
    assert "HTTP 302" in detail


def test_one_click_rejects_unrelated_endpoint_domain(monkeypatch):
    def fail_if_called(*_args, **_kwargs):
        raise AssertionError("DNS should not be queried")

    monkeypatch.setattr("yahoo_mail_mcp.unsubscribe._public_addresses", fail_if_called)

    ok, detail = one_click_unsubscribe("https://attacker.example/unsubscribe", "trusted.example")
    assert ok is False
    assert "unrelated" in detail


def test_mailto_rejects_header_injection_without_smtp(monkeypatch):
    def fail_if_called(*_args, **_kwargs):
        raise AssertionError("SMTP should not be opened")

    monkeypatch.setattr("yahoo_mail_mcp.unsubscribe.smtplib.SMTP_SSL", fail_if_called)
    account = Account("personal", "user@yahoo.com", "app-password")

    ok, detail = mailto_unsubscribe(
        account,
        "mailto:unsubscribe@example.com%0ABcc:attacker@example.com",
    )
    assert ok is False
    assert "Invalid mailto" in detail
