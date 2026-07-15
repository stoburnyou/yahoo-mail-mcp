"""Shared application context: settings, store, and per-account IMAP sessions."""

from __future__ import annotations

from .config import Account, Settings, load_settings
from .imap.client import YahooImap
from .jobs import ScanJobRunner
from .store.db import Store


class AppContext:
    def __init__(self, settings: Settings | None = None):
        self.settings = settings or load_settings()
        self.store = Store(self.settings.db_path)
        self._imaps: dict[str, YahooImap] = {}
        self._scan_jobs: ScanJobRunner | None = None

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

    @property
    def scan_jobs(self) -> ScanJobRunner:
        if self._scan_jobs is None:
            self._scan_jobs = ScanJobRunner(self.settings, self.store)
        return self._scan_jobs

    def close(self) -> None:
        if self._scan_jobs is not None:
            self._scan_jobs.close()
            self._scan_jobs = None
        for session in self._imaps.values():
            session.close()
        self._imaps.clear()
        self.store.close()
