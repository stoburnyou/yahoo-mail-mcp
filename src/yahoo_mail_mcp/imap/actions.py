"""Destructive IMAP operations: move messages to Trash.

Deletes are always UID MOVE to the account's Trash folder (never
STORE \\Deleted + EXPUNGE), so mistakes are recoverable from Trash.
"""

from __future__ import annotations

import logging

from ..store.db import Store
from .client import YahooImap, YahooImapError

logger = logging.getLogger(__name__)

MOVE_CHUNK = 200


def _chunk_uid_set(uids: list[int], size: int) -> list[str]:
    """Compress sorted UIDs into range strings, capped at `size` UIDs per chunk."""
    chunks: list[str] = []
    for i in range(0, len(uids), size):
        window = uids[i : i + size]
        parts: list[str] = []
        start = prev = window[0]
        for uid in window[1:]:
            if uid == prev + 1:
                prev = uid
                continue
            parts.append(str(start) if start == prev else f"{start}:{prev}")
            start = prev = uid
        parts.append(str(start) if start == prev else f"{start}:{prev}")
        chunks.append(",".join(parts))
    return chunks


def move_to_trash(
    imap: YahooImap,
    store: Store,
    account: str,
    folder: str,
    uids: list[int],
    expected_uidvalidity: int,
) -> int:
    """Move the given UIDs from `folder` to Trash. Returns the number moved."""
    if not uids:
        return 0

    trash = imap.find_special_folder("\\Trash")
    if trash is None:
        raise YahooImapError(f"No \\Trash folder found for {account}")
    if folder == trash:
        return 0

    info = imap.with_retry(f"select {folder}", lambda: imap.select_folder(folder, readonly=False))
    live_uidvalidity = int(info[b"UIDVALIDITY"])
    if live_uidvalidity != expected_uidvalidity:
        raise YahooImapError(
            f"UIDVALIDITY changed for {account}/{folder} "
            f"(scanned {expected_uidvalidity}, live {live_uidvalidity}). "
            "Stored UIDs are stale - rescan this folder before deleting."
        )

    moved = 0
    for chunk in _chunk_uid_set(sorted(uids), MOVE_CHUNK):
        def do_move(c=chunk):
            ll = imap.client._imap  # noqa: SLF001 - imapclient.move lacks chunk control
            typ, data = ll.uid("MOVE", c, f'"{trash}"')
            if typ != "OK":
                raise YahooImapError(f"UID MOVE failed: {typ} {data}")

        imap.with_retry(f"move {folder} -> Trash", do_move)
        chunk_uids = _expand_chunk(chunk)
        store.mark_deleted(account, folder, chunk_uids)
        moved += len(chunk_uids)
        logger.info("Moved %d messages from %s/%s to Trash", len(chunk_uids), account, folder)
    return moved


def _expand_chunk(chunk: str) -> list[int]:
    uids: list[int] = []
    for part in chunk.split(","):
        if ":" in part:
            lo, hi = part.split(":")
            uids.extend(range(int(lo), int(hi) + 1))
        else:
            uids.append(int(part))
    return uids
