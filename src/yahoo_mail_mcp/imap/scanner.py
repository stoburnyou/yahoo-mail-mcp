"""Header-only bulk scanner with checkpointed resume.

Strategy (per folder):
- Enable Yahoo's UIDONLY mode when available, so folders larger than the
  limited-mode window (~10k messages) are fully accessible, and page through
  them with the PARTIAL fetch extension.
- Walk from newest to oldest in batches below the server's MESSAGELIMIT,
  checkpointing the low-water-mark UID after every batch so an interrupted
  scan resumes instead of restarting.
- Once a folder has been fully scanned, subsequent scans only fetch UIDs
  above the recorded high-water mark (new mail).
"""

from __future__ import annotations

import email.utils
import logging
import re
from dataclasses import dataclass, field

from ..analysis.headers import parse_headers
from ..store.db import Store, utcnow
from .client import YahooImap, YahooImapError

logger = logging.getLogger(__name__)

HEADER_FIELDS = "FROM SUBJECT DATE LIST-UNSUBSCRIBE LIST-UNSUBSCRIBE-POST"
FETCH_PARTS = f"(UID INTERNALDATE RFC822.SIZE BODY.PEEK[HEADER.FIELDS ({HEADER_FIELDS})])"

_UIDFETCH_PREFIX_RE = re.compile(rb"^(\d+)\s+UIDFETCH\b", re.IGNORECASE)
_BARE_UID_PREFIX_RE = re.compile(rb"^(\d+)\s+\(")
_UID_ATTR_RE = re.compile(rb"\bUID (\d+)")
_SIZE_RE = re.compile(rb"RFC822\.SIZE (\d+)")
_INTERNALDATE_RE = re.compile(rb'INTERNALDATE "([^"]+)"')


def _looks_like_bare_fetch(item: object) -> bool:
    if not isinstance(item, bytes):
        return False
    upper = item.upper()
    return b"BODY[" in upper and bool(
        b"FETCH" in upper or _UID_ATTR_RE.search(item) or _BARE_UID_PREFIX_RE.match(item)
    )


@dataclass
class FolderScanResult:
    folder: str
    scanned: int = 0
    skipped: int = 0
    done: bool = False
    total_in_folder: int | None = None
    error: str | None = None


@dataclass
class ScanReport:
    account: str
    folders: list[FolderScanResult] = field(default_factory=list)

    @property
    def total_scanned(self) -> int:
        return sum(f.scanned for f in self.folders)


def _raw_uid_fetch(imap: YahooImap, uid_range: str, partial: str | None = None) -> list:
    """UID FETCH via imaplib, collecting both FETCH and UIDFETCH responses.

    In Yahoo's UIDONLY mode responses arrive as `* <uid> UIDFETCH (...)`,
    which imaplib leaves in untagged_responses instead of returning.
    """
    ll = imap.client._imap  # noqa: SLF001 - imapclient has no UIDFETCH support
    args = [uid_range, FETCH_PARTS]
    if partial:
        args.append(f"(PARTIAL {partial})")
    typ, data = ll.uid("FETCH", *args)
    if typ != "OK":
        raise YahooImapError(f"UID FETCH {uid_range} failed: {typ} {data}")

    responses: list = []
    if data and data != [None]:
        responses.extend(data)
    for key in ("UIDFETCH", "uidfetch"):
        responses.extend(ll.untagged_responses.pop(key, []))
    return responses


def _parse_fetch_responses(responses: list) -> list[dict]:
    """Turn raw imaplib response items into per-message dicts.

    Items with a header literal arrive as (prefix_bytes, header_bytes) tuples,
    followed by a lone b')' terminator that we skip.
    """
    out: list[dict] = []
    for item in responses:
        if isinstance(item, tuple) and len(item) >= 2:
            prefix, header_blob = item[0], item[1]
        elif _looks_like_bare_fetch(item):
            # A valid header fetch can return BODY[...] NIL instead of a
            # literal. Preserve the message metadata with empty headers rather
            # than silently skipping its UID.
            prefix, header_blob = item, b""
        else:
            continue

        uid = None
        m = _UIDFETCH_PREFIX_RE.match(prefix)
        if m:
            uid = int(m.group(1))
        else:
            m = _UID_ATTR_RE.search(prefix)
            if m:
                uid = int(m.group(1))
            else:
                m = _BARE_UID_PREFIX_RE.match(prefix)
                if m:
                    uid = int(m.group(1))
        if uid is None:
            continue

        size = None
        m = _SIZE_RE.search(prefix)
        if m:
            size = int(m.group(1))

        internaldate_iso = None
        m = _INTERNALDATE_RE.search(prefix)
        if m:
            try:
                dt = email.utils.parsedate_to_datetime(m.group(1).decode("latin-1"))
                internaldate_iso = dt.isoformat() if dt else None
            except (TypeError, ValueError):
                pass

        parsed = parse_headers(header_blob if isinstance(header_blob, bytes) else b"")
        out.append(
            {
                "uid": uid,
                "size_bytes": size,
                "sender_email": parsed.sender_email,
                "sender_domain": parsed.sender_domain,
                "sender_name": parsed.sender_name,
                "subject": parsed.subject,
                "date": parsed.date or internaldate_iso,
                "list_unsub_raw": parsed.list_unsub_raw,
                "unsub_mailto": parsed.unsub_mailto,
                "unsub_http": parsed.unsub_http,
                "one_click": int(parsed.one_click),
            }
        )
    return out


def _fetch_response_count(responses: list) -> int:
    return sum(
        1
        for item in responses
        if (isinstance(item, tuple) and len(item) >= 2 and isinstance(item[0], bytes))
        or (_looks_like_bare_fetch(item))
    )


def _persist_batch(
    store: Store, account: str, folder: str, uidvalidity: int, records: list[dict]
) -> None:
    now = utcnow()
    rows = [
        {**rec, "account": account, "folder": folder, "uidvalidity": uidvalidity, "scanned_at": now}
        for rec in records
    ]
    store.upsert_messages(rows)


def scan_folder(
    imap: YahooImap,
    store: Store,
    account: str,
    folder: str,
    batch_size: int,
    max_messages: int | None = None,
) -> FolderScanResult:
    result = FolderScanResult(folder=folder)
    if max_messages is not None and max_messages < 1:
        return result

    uidonly = False
    try:
        uidonly = imap.enable_uidonly()
    except YahooImapError:
        logger.warning("ENABLE UIDONLY failed; falling back to limited mode", exc_info=True)

    info = imap.with_retry(f"select {folder}", lambda: imap.select_folder(folder, readonly=True))
    uidvalidity = int(info[b"UIDVALIDITY"])
    uidnext = int(info[b"UIDNEXT"])
    exists = int(info.get(b"EXISTS", 0))
    result.total_in_folder = exists

    if not uidonly and imap.message_limit and exists >= imap.message_limit:
        raise YahooImapError(
            f"{folder!r} reached Yahoo's limited-mode cap of {imap.message_limit} messages, "
            "but UIDONLY could not be enabled. Refusing to mark an incomplete scan as done."
        )

    if imap.message_limit:
        batch_size = min(batch_size, max(1, imap.message_limit - 1))

    ckpt = store.get_checkpoint(account, folder)
    if ckpt is not None and ckpt["uidvalidity"] != uidvalidity:
        logger.warning(
            "UIDVALIDITY changed for %s/%s (%s -> %s); restarting scan",
            account,
            folder,
            ckpt["uidvalidity"],
            uidvalidity,
        )
        store.clear_folder_messages(account, folder)
        store.clear_checkpoint(account, folder)
        ckpt = None

    scanned_before = ckpt["scanned"] if ckpt else 0
    current_low: int | None

    if ckpt is not None and ckpt["phase"] == "incremental":
        # Resume an interrupted ascending pass through a fixed new-mail range.
        phase = "incremental"
        low_bound = int(ckpt["low_uid"])
        high = int(ckpt["high_uid"])
        current_low = low_bound
        high_mark = high
    elif ckpt is not None and ckpt["done"]:
        # Historical mail is complete. Scan new mail oldest-first so a capped
        # run can safely advance a contiguous high-water mark.
        phase = "incremental"
        low_bound = int(ckpt["high_uid"]) + 1
        high = uidnext - 1
        current_low = low_bound
        high_mark = high
        if high < low_bound:
            result.done = True
            result.skipped = scanned_before
            return result
    else:
        # Initial or resumed historical scan: walk downward, and keep the
        # original high-water mark fixed so mail arriving mid-scan is picked up
        # by the later incremental phase rather than silently skipped.
        phase = "historical"
        low_bound = 1
        high = int(ckpt["low_uid"]) - 1 if ckpt is not None else uidnext - 1
        current_low = (
            int(ckpt["low_uid"]) if ckpt is not None and ckpt["low_uid"] is not None else None
        )
        high_mark = int(ckpt["high_uid"]) if ckpt is not None else uidnext - 1
        if high < 1:
            store.save_checkpoint(
                account,
                folder,
                uidvalidity,
                1,
                high_mark,
                True,
                scanned_before,
                phase="complete",
            )
            result.done = True
            result.skipped = scanned_before
            return result

    scanned_this_run = 0

    while low_bound <= high:
        fetch_size = batch_size
        if max_messages is not None:
            remaining = max_messages - scanned_this_run
            if remaining <= 0:
                break
            fetch_size = min(fetch_size, remaining)

        uid_range = f"{low_bound}:{high}"
        if uidonly:
            fetch_range = uid_range
            fetch_partial = f"1:{fetch_size}" if phase == "incremental" else f"-1:-{fetch_size}"
        else:
            fetch_partial = None
            if phase == "incremental":
                window_high = min(high, low_bound + fetch_size - 1)
                fetch_range = f"{low_bound}:{window_high}"
            else:
                window_low = max(low_bound, high - fetch_size + 1)
                fetch_range = f"{window_low}:{high}"

        def fetch() -> list:
            return _raw_uid_fetch(imap, fetch_range, partial=fetch_partial)

        responses = imap.with_retry(f"fetch {folder} {uid_range}", fetch)
        records = _parse_fetch_responses(responses)
        response_count = _fetch_response_count(responses)
        if response_count != len(records):
            raise YahooImapError(
                f"Could not parse all FETCH responses in {folder!r} "
                f"({len(records)} of {response_count}); checkpoint not advanced"
            )

        if records:
            _persist_batch(store, account, folder, uidvalidity, records)
            scanned_this_run += len(records)
            batch_edge = (
                max(r["uid"] for r in records)
                if phase == "incremental"
                else min(r["uid"] for r in records)
            )
        else:
            batch_edge = high if phase == "incremental" else low_bound

        if phase == "incremental":
            next_low = batch_edge + 1
            finished = not records or next_low > high
            store.save_checkpoint(
                account,
                folder,
                uidvalidity,
                1 if finished else next_low,
                high_mark,
                finished,
                scanned_before + scanned_this_run,
                phase="complete" if finished else "incremental",
            )
            low_bound = next_low
        else:
            current_low = batch_edge if current_low is None else min(current_low, batch_edge)
            finished = not records or batch_edge <= low_bound
            store.save_checkpoint(
                account,
                folder,
                uidvalidity,
                current_low,
                high_mark,
                finished,
                scanned_before + scanned_this_run,
                phase="complete" if finished else "historical",
            )
            high = batch_edge - 1

        if finished:
            result.done = True
            break

    result.scanned = scanned_this_run
    result.skipped = scanned_before
    return result


DEFAULT_SKIP_SPECIAL = {"\\Trash", "\\Drafts", "\\Sent"}


def scan_mailbox(
    imap: YahooImap,
    store: Store,
    account: str,
    folders: list[str] | None,
    batch_size: int,
    max_messages: int | None = None,
) -> ScanReport:
    report = ScanReport(account=account)
    if max_messages is not None and max_messages < 1:
        raise ValueError("max_messages must be a positive integer")

    if folders:
        targets = folders
    else:
        targets = []
        for info in imap.list_folders():
            if info.special_use in DEFAULT_SKIP_SPECIAL or info.special_use == "\\All":
                continue
            targets.append(info.name)

    for folder in targets:
        remaining = None
        if max_messages is not None:
            remaining = max_messages - report.total_scanned
            if remaining <= 0:
                break
        try:
            folder_result = scan_folder(imap, store, account, folder, batch_size, remaining)
        except YahooImapError as exc:
            logger.error("Scan of %s/%s failed: %s", account, folder, exc)
            folder_result = FolderScanResult(folder=folder, error=str(exc))
        report.folders.append(folder_result)
        store.log_action(
            "scan_folder",
            account=account,
            folder=folder,
            count=folder_result.scanned,
            detail="done" if folder_result.done else (folder_result.error or "partial"),
        )
    return report
