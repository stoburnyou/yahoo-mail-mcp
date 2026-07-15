"""Mailbox mutation operations using recoverable IMAP UID MOVE.

Deletes move to Trash (never STORE \\Deleted + EXPUNGE); archives move to the
provider's \\Archive folder.
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
    return _move_to_special_folder(
        imap,
        store,
        account,
        folder,
        uids,
        expected_uidvalidity,
        special_use="\\Trash",
        action_name="Trash",
    )


def move_to_archive(
    imap: YahooImap,
    store: Store,
    account: str,
    folder: str,
    uids: list[int],
    expected_uidvalidity: int,
) -> int:
    """Move the given UIDs from `folder` to Archive. Returns the number moved."""
    return _move_to_special_folder(
        imap,
        store,
        account,
        folder,
        uids,
        expected_uidvalidity,
        special_use="\\Archive",
        action_name="Archive",
    )


def _move_to_special_folder(
    imap: YahooImap,
    store: Store,
    account: str,
    folder: str,
    uids: list[int],
    expected_uidvalidity: int,
    *,
    special_use: str,
    action_name: str,
) -> int:
    if not uids:
        return 0

    destination = imap.find_special_folder(special_use)
    if destination is None:
        raise YahooImapError(f"No {special_use} folder found for {account}")
    if folder == destination:
        store.mark_deleted(account, folder, uids)
        return 0

    info = imap.with_retry(f"select {folder}", lambda: imap.select_folder(folder, readonly=False))
    live_uidvalidity = int(info[b"UIDVALIDITY"])
    if live_uidvalidity != expected_uidvalidity:
        raise YahooImapError(
            f"UIDVALIDITY changed for {account}/{folder} "
            f"(scanned {expected_uidvalidity}, live {live_uidvalidity}). "
            "Stored UIDs are stale - rescan this folder before moving messages."
        )

    moved = 0
    for chunk in _chunk_uid_set(sorted(uids), MOVE_CHUNK):
        requested = _expand_chunk(chunk)
        existing_before = set(
            imap.with_retry(
                f"verify source UIDs in {folder}",
                lambda c=chunk: imap.client.search(["UID", c]),
            )
        )
        missing_before = [uid for uid in requested if uid not in existing_before]
        if missing_before:
            store.mark_deleted(account, folder, missing_before)
        if not existing_before:
            continue
        move_chunk = _chunk_uid_set(sorted(existing_before), MOVE_CHUNK)[0]

        def do_move(c=move_chunk):
            ll = imap.client._imap  # noqa: SLF001 - imapclient.move lacks chunk control
            typ, data = ll.uid("MOVE", c, f'"{destination}"')
            if typ != "OK":
                raise YahooImapError(f"UID MOVE failed: {typ} {data}")

        imap.with_retry(f"move {folder} -> {action_name}", do_move)
        remaining = set(
            imap.with_retry(
                f"verify moved UIDs left {folder}",
                lambda c=move_chunk: imap.client.search(["UID", c]),
            )
        )
        confirmed = [uid for uid in existing_before if uid not in remaining]
        if confirmed:
            store.mark_deleted(account, folder, confirmed)
        moved += len(confirmed)
        logger.info(
            "Moved %d messages from %s/%s to %s",
            len(confirmed),
            account,
            folder,
            action_name,
        )
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
