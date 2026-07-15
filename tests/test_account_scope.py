from types import SimpleNamespace

import pytest

from yahoo_mail_mcp.config import Account, Settings
from yahoo_mail_mcp.tools.account_scope import resolve_account


def context_with(*accounts):
    settings = Settings(accounts=list(accounts))
    return SimpleNamespace(settings=settings, account=settings.account)


def test_single_account_is_inferred():
    ctx = context_with(Account("personal", "person@yahoo.com", "secret"))

    assert resolve_account(ctx, None) == "personal"


def test_multiple_accounts_require_explicit_selection():
    ctx = context_with(
        Account("personal", "one@yahoo.com", "secret"),
        Account("work", "two@yahoo.com", "secret"),
    )

    with pytest.raises(ValueError, match="account is required"):
        resolve_account(ctx, None)


def test_email_alias_resolves_to_canonical_account_name():
    ctx = context_with(Account("personal", "person@yahoo.com", "secret"))

    assert resolve_account(ctx, "person@yahoo.com") == "personal"
