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
_UID_ATTR_RE = re.compile(rb"\bUID (\d+)")
_SIZE_RE = re.compile(rb"RFC822\.SIZE (\d+)")
_INTERNALDATE_RE = re.compile(rb'INTERNALDATE "([^"]+)"')


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
        if not isinstance(item, tuple) or len(item) < 2:
            continue
        prefix, header_blob = item[0], item[1]

        uid = None
        m = _UIDFETCH_PREFIX_RE.match(prefix)
        if m:
            uid = int(m.group(1))
        else:
            m = _UID_ATTR_RE.search(prefix)
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

    if imap.message_limit:
        batch_size = min(batch_size, max(1, imap.message_limit - 1))

    ckpt = store.get_checkpoint(account, folder)
    if ckpt is not None and ckpt["uidvalidity"] != uidvalidity:
        logger.warning(
            "UIDVALIDITY changed for %s/%s (%s -> %s); restarting scan",
            account, folder, ckpt["uidvalidity"], uidvalidity,
        )
        store.clear_checkpoint(account, folder)
        ckpt = None

    scanned_before = ckpt["scanned"] if ckpt else 0

    if ckpt is not None and ckpt["done"]:
        # Folder fully scanned before: only fetch new mail above the old high mark.
        low_bound = int(ckpt["high_uid"]) + 1
        high = uidnext - 1
        if high < low_bound:
            result.done = True
            result.skipped = scanned_before
            return result
        prior_high = int(ckpt["high_uid"])
        prior_low = ckpt["low_uid"]
    elif ckpt is not None:
        # Resume interrupted walk downward from the last low-water mark.
        low_bound = 1
        high = int(ckpt["low_uid"]) - 1
        prior_high = int(ckpt["high_uid"])
        prior_low = ckpt["low_uid"]
        if high < 1:
            store.save_checkpoint(account, folder, uidvalidity, 1, prior_high, True, scanned_before)
            result.done = True
            result.skipped = scanned_before
            return result
    else:
        low_bound = 1
        high = uidnext - 1
        prior_high = uidnext - 1
        prior_low = None
        if high < 1:
            store.save_checkpoint(account, folder, uidvalidity, 1, 0, True, 0)
            result.done = True
            return result

    scanned_this_run = 0
    current_low = prior_low
    high_mark = max(prior_high, uidnext - 1)

    while high >= low_bound:
        uid_range = f"{low_bound}:{high}"
        if uidonly:
            fetch = lambda r=uid_range: _raw_uid_fetch(imap, r, partial=f"-1:-{batch_size}")
        else:
            window_low = max(low_bound, high - batch_size + 1)
            fetch = lambda r=f"{window_low}:{high}": _raw_uid_fetch(imap, r)
        responses = imap.with_retry(f"fetch {folder} {uid_range}", fetch)
        records = _parse_fetch_responses(responses)

        if records:
            _persist_batch(store, account, folder, uidvalidity, records)
            scanned_this_run += len(records)
            batch_low = min(r["uid"] for r in records)
        elif uidonly:
            # Empty response in PARTIAL mode means nothing left below `high`.
            batch_low = low_bound
        else:
            batch_low = max(low_bound, high - batch_size + 1)

        current_low = batch_low if current_low is None else min(current_low, batch_low)
        finished = batch_low <= low_bound or (uidonly and not records)
        store.save_checkpoint(
            account,
            folder,
            uidvalidity,
            current_low,
            high_mark,
            finished,
            scanned_before + scanned_this_run,
        )
        if finished:
            result.done = True
            break
        high = batch_low - 1
        if max_messages is not None and scanned_this_run >= max_messages:
            break

    result.scanned = scanned_this_run
    result.skipped = scanned_before
    if high < low_bound and not result.done:
        store.save_checkpoint(
            account, folder, uidvalidity, current_low or low_bound, high_mark, True,
            scanned_before + scanned_this_run,
        )
        result.done = True
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

    if folders:
        targets = folders
    else:
        targets = []
        for info in imap.list_folders():
            if info.special_use in DEFAULT_SKIP_SPECIAL or info.special_use == "\\All":
                continue
            targets.append(info.name)

    for folder in targets:
        try:
            folder_result = scan_folder(imap, store, account, folder, batch_size, max_messages)
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
