"""Durable background jobs for scans that outlive an MCP request."""

from __future__ import annotations

import json
import logging
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from uuid import uuid4

from .config import Settings
from .imap.scanner import scan_mailbox
from .store.db import Store

logger = logging.getLogger(__name__)

JOB_STATUSES = ("queued", "running", "completed", "failed", "interrupted")


def scan_job_record(row) -> dict:
    return {
        "job_id": row["id"],
        "account": row["account"],
        "folders": json.loads(row["folders_json"]) if row["folders_json"] else None,
        "max_messages": row["max_messages"],
        "status": row["status"],
        "result": json.loads(row["result_json"]) if row["result_json"] else None,
        "error": row["error"],
        "created_at": row["created_at"],
        "started_at": row["started_at"],
        "completed_at": row["completed_at"],
    }


def _safe_job_error(exc: Exception) -> str:
    message = str(exc).lower()
    if "authentication" in message or "invalid credentials" in message:
        return "Yahoo authentication failed. Verify the account app password."
    if "timeout" in message or "timed out" in message:
        return "Yahoo IMAP timed out. Start another job to resume from the checkpoint."
    return "Scan failed. Review server logs, then start another job to resume from checkpoints."


class ScanJobRunner:
    """Single-worker queue to avoid overlapping large scans."""

    def __init__(self, settings: Settings, store: Store):
        self.settings = settings
        self.store = store
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="mail-scan")

    def start(
        self,
        account: str,
        folders: list[str] | None,
        max_messages: int | None,
    ) -> dict:
        active = self.store.active_scan_job(account)
        if active is not None:
            record = scan_job_record(active)
            record["detail"] = "This account already has an active scan job."
            return record

        job_id = uuid4().hex
        folders_json = json.dumps(folders) if folders is not None else None
        self.store.create_scan_job(job_id, account, folders_json, max_messages)
        self.executor.submit(self._run, job_id, account, folders, max_messages)
        row = self.store.get_scan_job(job_id)
        assert row is not None
        return scan_job_record(row)

    def _run(
        self,
        job_id: str,
        account: str,
        folders: list[str] | None,
        max_messages: int | None,
    ) -> None:
        # A separate context gives this worker its own SQLite and IMAP
        # connections; scan progress itself remains durable through checkpoints.
        from .app import AppContext

        worker = None
        try:
            worker = AppContext(self.settings)
            worker.store.update_scan_job(job_id, "running")
            imap = worker.imap(account)
            report = scan_mailbox(
                imap,
                worker.store,
                account,
                folders,
                self.settings.batch_size,
                max_messages,
            )
            result = {
                "account": account,
                "total_scanned_this_run": report.total_scanned,
                "folders": [asdict(folder) for folder in report.folders],
                "messages_in_database": worker.store.message_count(account),
            }
            worker.store.update_scan_job(
                job_id,
                "completed",
                result_json=json.dumps(result),
            )
        except Exception as exc:  # noqa: BLE001 - persist a safe failure and preserve traceback
            logger.exception("Background scan job %s failed for account %s", job_id, account)
            failure_store = worker.store if worker is not None else self.store
            failure_store.update_scan_job(job_id, "failed", error=_safe_job_error(exc))
        finally:
            if worker is not None:
                worker.close()

    def close(self) -> None:
        self.executor.shutdown(wait=False, cancel_futures=True)
