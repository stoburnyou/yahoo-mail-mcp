"""Yahoo IMAP connection handling.

Wraps IMAPClient with Yahoo-specific behavior:
- SSL connect to imap.mail.yahoo.com:993 with an app password
- IMAP ID command after login (Yahoo uses it for client identification)
- Capability probe for MESSAGELIMIT
- ENABLE UIDONLY support for folders larger than Yahoo's limited-mode window
- Reconnect with exponential backoff, since Yahoo drops long-lived
  connections under sustained load
"""

from __future__ import annotations

import logging
import platform
import re
import time
from dataclasses import dataclass

from imapclient import IMAPClient

from ..config import IMAP_HOST, IMAP_PORT, Account

logger = logging.getLogger(__name__)

MAX_RECONNECT_ATTEMPTS = 5
BACKOFF_BASE_SECONDS = 2.0


@dataclass
class FolderInfo:
    name: str
    special_use: str | None  # e.g. "\\Trash", "\\Junk", "\\All"
    messages: int | None
    uidnext: int | None
    uidvalidity: int | None


class YahooImapError(Exception):
    pass


class YahooImap:
    """A single reconnecting IMAP session for one Yahoo account."""

    def __init__(self, account: Account):
        self.account = account
        self._client: IMAPClient | None = None
        self._uidonly_enabled = False
        self.message_limit: int | None = None
        self._selected_folder: str | None = None
        self._selected_readonly = True

    # -- connection lifecycle -------------------------------------------------

    def connect(self) -> None:
        client = IMAPClient(IMAP_HOST, port=IMAP_PORT, ssl=True, timeout=120)
        client.login(self.account.email, self.account.app_password)
        try:
            client.id_({
                "name": "yahoo-mail-mcp",
                "version": "0.1.0",
                "os": platform.system(),
                "os-version": platform.release(),
            })
        except Exception:  # noqa: BLE001 - ID is best-effort
            logger.debug("IMAP ID command failed (non-fatal)", exc_info=True)

        self._client = client
        self._uidonly_enabled = False
        self._selected_folder = None
        self.message_limit = self._probe_message_limit()
        logger.info(
            "Connected to Yahoo IMAP as %s (MESSAGELIMIT=%s)",
            self.account.email,
            self.message_limit,
        )

    def _probe_message_limit(self) -> int | None:
        caps = [c.decode() if isinstance(c, bytes) else str(c) for c in self.client.capabilities()]
        for cap in caps:
            m = re.match(r"MESSAGELIMIT=(\d+)", cap, re.IGNORECASE)
            if m:
                return int(m.group(1))
        return None

    @property
    def client(self) -> IMAPClient:
        if self._client is None:
            self.connect()
        assert self._client is not None
        return self._client

    def close(self) -> None:
        if self._client is not None:
            try:
                self._client.logout()
            except Exception:  # noqa: BLE001
                pass
            self._client = None

    def _reconnect(self) -> None:
        self.close()
        self.connect()
        if self._uidonly_enabled:
            self._uidonly_enabled = False
            self.enable_uidonly()

    def with_retry(self, op_name: str, fn):
        """Run fn() against the live client, reconnecting with backoff on failure.

        On reconnect the previously selected folder is re-selected so callers
        can treat the session as stable.
        """
        last_exc: Exception | None = None
        for attempt in range(MAX_RECONNECT_ATTEMPTS):
            try:
                return fn()
            except (IMAPClient.Error, OSError) as exc:
                last_exc = exc
                wait = BACKOFF_BASE_SECONDS * (2**attempt)
                logger.warning(
                    "%s failed (%s); reconnecting in %.0fs (attempt %d/%d)",
                    op_name,
                    exc,
                    wait,
                    attempt + 1,
                    MAX_RECONNECT_ATTEMPTS,
                )
                time.sleep(wait)
                folder, readonly = self._selected_folder, self._selected_readonly
                try:
                    self._reconnect()
                    if folder:
                        self.select_folder(folder, readonly=readonly)
                except Exception:  # noqa: BLE001 - retry loop handles it
                    logger.warning("Reconnect attempt failed", exc_info=True)
        raise YahooImapError(f"{op_name} failed after {MAX_RECONNECT_ATTEMPTS} attempts: {last_exc}")

    # -- capabilities / modes -------------------------------------------------

    def enable_uidonly(self) -> bool:
        """Switch the session to Yahoo UID mode (full-folder access).

        Returns True if UIDONLY was enabled. In UID mode all FETCH responses
        arrive as UIDFETCH untagged responses and MSN-based commands fail.
        """
        if self._uidonly_enabled:
            return True
        if not self.client.has_capability("UIDONLY"):
            return False
        # Must be issued in authenticated state, before SELECT.
        enabled = self.client.enable("UIDONLY")
        self._uidonly_enabled = any(b"UIDONLY" in cap.upper() for cap in enabled)
        return self._uidonly_enabled

    @property
    def uidonly(self) -> bool:
        return self._uidonly_enabled

    # -- folders ---------------------------------------------------------------

    def list_folders(self) -> list[FolderInfo]:
        result: list[FolderInfo] = []
        special_flags = {b"\\All", b"\\Archive", b"\\Drafts", b"\\Junk", b"\\Sent", b"\\Trash"}
        for flags, _delim, name in self.client.list_folders():
            special = next((f.decode() for f in flags if f in special_flags), None)
            try:
                status = self.client.folder_status(name, ["MESSAGES", "UIDNEXT", "UIDVALIDITY"])
                result.append(
                    FolderInfo(
                        name=name,
                        special_use=special,
                        messages=status.get(b"MESSAGES"),
                        uidnext=status.get(b"UIDNEXT"),
                        uidvalidity=status.get(b"UIDVALIDITY"),
                    )
                )
            except IMAPClient.Error:
                # Some virtual folders refuse STATUS; report them without counts.
                result.append(FolderInfo(name=name, special_use=special, messages=None, uidnext=None, uidvalidity=None))
        return result

    def find_special_folder(self, special_use: str) -> str | None:
        for info in self.list_folders():
            if info.special_use == special_use:
                return info.name
        return None

    def select_folder(self, folder: str, readonly: bool = True) -> dict:
        info = self.client.select_folder(folder, readonly=readonly)
        self._selected_folder = folder
        self._selected_readonly = readonly
        return info
