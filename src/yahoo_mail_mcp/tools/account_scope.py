"""Helpers for resolving an explicit mailbox account safely."""

from ..app import AppContext


def resolve_account(ctx: AppContext, account: str | None) -> str:
    """Resolve aliases and only infer an account when exactly one is configured."""
    if account:
        return ctx.account(account).name
    if len(ctx.settings.accounts) == 1:
        return ctx.settings.accounts[0].name
    known = ", ".join(item.name for item in ctx.settings.accounts) or "(none configured)"
    raise ValueError(
        f"account is required when multiple accounts are configured. Choose one of: {known}"
    )
