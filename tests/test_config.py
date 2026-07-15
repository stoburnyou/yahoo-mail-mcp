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


def test_loads_simple_single_account_variables(monkeypatch, tmp_path):
    monkeypatch.delenv("YAHOO_ACCOUNTS", raising=False)
    monkeypatch.setenv("YAHOO_EMAIL", "person@yahoo.com")
    monkeypatch.setenv("YAHOO_APP_PASSWORD", "abcd efgh")
    monkeypatch.setenv("YAHOO_ACCOUNT_NAME", "personal")

    settings = load_settings(tmp_path / "missing.env")

    assert len(settings.accounts) == 1
    assert settings.accounts[0].name == "personal"
    assert settings.accounts[0].email == "person@yahoo.com"
    assert settings.accounts[0].app_password == "abcdefgh"


def test_rejects_mixed_single_and_multi_account_variables(monkeypatch, tmp_path):
    monkeypatch.setenv(
        "YAHOO_ACCOUNTS",
        '[{"name":"one","email":"one@yahoo.com","app_password":"password"}]',
    )
    monkeypatch.setenv("YAHOO_EMAIL", "other@yahoo.com")

    with pytest.raises(ValueError, match="either YAHOO_ACCOUNTS"):
        load_settings(tmp_path / "missing.env")


def test_requires_at_least_one_account(monkeypatch, tmp_path):
    for name in (
        "YAHOO_ACCOUNTS",
        "YAHOO_EMAIL",
        "YAHOO_APP_PASSWORD",
        "YAHOO_ACCOUNT_NAME",
    ):
        monkeypatch.delenv(name, raising=False)

    with pytest.raises(ValueError, match="at least one Yahoo account"):
        load_settings(tmp_path / "missing.env")
