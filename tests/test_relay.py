import pytest
from starlette.testclient import TestClient

from yahoo_mail_mcp import relay


def _payload(**overrides):
    payload = {
        "account": "personal",
        "from_email": "a.poenaru@ymail.com",
        "from_name": "A",
        "app_password": "app-password",
        "to": ["recipient@example.com"],
        "cc": [],
        "bcc": [],
        "subject": "Hello",
        "body_text": "Body",
        "body_html": "",
        "attachments": [],
        "in_reply_to": "",
        "references": "",
    }
    payload.update(overrides)
    return payload


def test_relay_requires_bearer_secret(monkeypatch):
    monkeypatch.setenv("YAHOO_SMTP_RELAY_SECRET", "s" * 32)
    app = relay.create_relay_app()

    with TestClient(app) as client:
        response = client.post("/send", json=_payload())

    assert response.status_code == 401
    assert response.json() == {"sent": False, "error": "Unauthorized"}


def test_relay_rejects_non_allowlisted_attachment_host(monkeypatch):
    monkeypatch.setenv("YAHOO_SMTP_RELAY_SECRET", "s" * 32)
    app = relay.create_relay_app()
    payload = _payload(
        attachments=[
            {
                "filename": "invoice.pdf",
                "url": "https://example.com/invoice.pdf",
                "content_type": "application/pdf",
                "size_bytes": 100,
            }
        ]
    )

    with TestClient(app) as client:
        response = client.post(
            "/send",
            json=payload,
            headers={"Authorization": f"Bearer {'s' * 32}"},
        )

    assert response.status_code == 400
    assert response.json() == {
        "sent": False,
        "error": "Attachment URL host is not allowlisted",
    }


def test_relay_sends_with_authorized_request(monkeypatch):
    monkeypatch.setenv("YAHOO_SMTP_RELAY_SECRET", "s" * 32)
    monkeypatch.setattr(relay, "_send", lambda payload: "<msg@example.com>")
    app = relay.create_relay_app()

    with TestClient(app) as client:
        response = client.post(
            "/send",
            json=_payload(),
            headers={"Authorization": f"Bearer {'s' * 32}"},
        )

    assert response.status_code == 200
    assert response.json() == {"sent": True, "message_id": "<msg@example.com>"}


def test_relay_requires_strong_secret(monkeypatch):
    monkeypatch.setenv("YAHOO_SMTP_RELAY_SECRET", "short")

    with pytest.raises(ValueError, match="at least 32"):
        relay.create_relay_app()
