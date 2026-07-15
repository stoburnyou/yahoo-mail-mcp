"""Account and server configuration loaded from the environment."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

IMAP_HOST = "imap.mail.yahoo.com"
IMAP_PORT = 993
SMTP_HOST = "smtp.mail.yahoo.com"
SMTP_PORT = 465

DEFAULT_DB_PATH = Path.home() / ".yahoo-mail-mcp" / "mail.db"
DEFAULT_DELETE_THRESHOLD = 1000
DEFAULT_BATCH_SIZE = 500


@dataclass(frozen=True)
class Account:
    name: str
    email: str
    app_password: str


@dataclass
class Settings:
    accounts: list[Account] = field(default_factory=list)
    db_path: Path = DEFAULT_DB_PATH
    delete_threshold: int = DEFAULT_DELETE_THRESHOLD
    batch_size: int = DEFAULT_BATCH_SIZE

    def account(self, name_or_email: str) -> Account:
        for acct in self.accounts:
            if name_or_email in (acct.name, acct.email):
                return acct
        known = ", ".join(a.name for a in self.accounts) or "(none configured)"
        raise KeyError(
            f"Unknown account {name_or_email!r}. Configured accounts: {known}. "
            "Check the YAHOO_ACCOUNTS environment variable."
        )


def load_settings(env_file: str | os.PathLike | None = None) -> Settings:
    load_dotenv(env_file)

    accounts: list[Account] = []
    raw = os.environ.get("YAHOO_ACCOUNTS", "").strip()
    if raw:
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(f"YAHOO_ACCOUNTS is not valid JSON: {exc}") from exc
        if not isinstance(parsed, list):
            raise ValueError("YAHOO_ACCOUNTS must be a JSON array of account objects")
        for i, entry in enumerate(parsed):
            missing = {"email", "app_password"} - set(entry)
            if missing:
                raise ValueError(f"YAHOO_ACCOUNTS[{i}] missing keys: {sorted(missing)}")
            accounts.append(
                Account(
                    name=entry.get("name") or entry["email"],
                    email=entry["email"],
                    app_password=entry["app_password"].replace(" ", ""),
                )
            )

    db_path = Path(os.environ.get("YAHOO_MAIL_MCP_DB") or DEFAULT_DB_PATH).expanduser()
    threshold = int(os.environ.get("YAHOO_MAIL_MCP_DELETE_THRESHOLD") or DEFAULT_DELETE_THRESHOLD)
    batch_size = int(os.environ.get("YAHOO_MAIL_MCP_BATCH_SIZE") or DEFAULT_BATCH_SIZE)

    return Settings(
        accounts=accounts,
        db_path=db_path,
        delete_threshold=threshold,
        batch_size=batch_size,
    )
