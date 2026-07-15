"""Shared application context: settings, store, and per-account IMAP sessions."""

from __future__ import annotations

from .config import Account, Settings, load_settings
from .imap.client import YahooImap
from .store.db import Store


class AppContext:
    def __init__(self, settings: Settings | None = None):
        self.settings = settings or load_settings()
        self.store = Store(self.settings.db_path)
        self._imaps: dict[str, YahooImap] = {}

    def account(self, name_or_email: str) -> Account:
        return self.settings.account(name_or_email)

    def imap(self, name_or_email: str) -> YahooImap:
        acct = self.account(name_or_email)
        session = self._imaps.get(acct.name)
        if session is None:
            session = YahooImap(acct)
            session.connect()
            self._imaps[acct.name] = session
        return session

    @property
    def accounts_by_name(self) -> dict[str, Account]:
        return {a.name: a for a in self.settings.accounts}

    def close(self) -> None:
        for session in self._imaps.values():
            session.close()
        self._imaps.clear()
        self.store.close()
