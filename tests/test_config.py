import pytest

from yahoo_mail_mcp.config import load_settings


def test_rejects_duplicate_account_names(monkeypatch, tmp_path):
    monkeypatch.setenv(
        "YAHOO_ACCOUNTS",
        """[
          {"name":"same","email":"one@yahoo.com","app_password":"first"},
          {"name":"same","email":"two@yahoo.com","app_password":"second"}
        ]""",
    )

    with pytest.raises(ValueError, match="names must be unique"):
        load_settings(tmp_path / "missing.env")


def test_rejects_duplicate_account_emails(monkeypatch, tmp_path):
    monkeypatch.setenv(
        "YAHOO_ACCOUNTS",
        """[
          {"name":"one","email":"same@yahoo.com","app_password":"first"},
          {"name":"two","email":"SAME@yahoo.com","app_password":"second"}
        ]""",
    )

    with pytest.raises(ValueError, match="email addresses must be unique"):
        load_settings(tmp_path / "missing.env")


def test_rejects_non_positive_batch_size(monkeypatch, tmp_path):
    monkeypatch.setenv(
        "YAHOO_ACCOUNTS",
        '[{"name":"one","email":"one@yahoo.com","app_password":"password"}]',
    )
    monkeypatch.setenv("YAHOO_MAIL_MCP_BATCH_SIZE", "0")

    with pytest.raises(ValueError, match="BATCH_SIZE"):
        load_settings(tmp_path / "missing.env")
