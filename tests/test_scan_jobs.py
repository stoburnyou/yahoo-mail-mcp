import json
from types import SimpleNamespace

from yahoo_mail_mcp import app as app_module
from yahoo_mail_mcp import jobs
from yahoo_mail_mcp.config import Settings
from yahoo_mail_mcp.jobs import ScanJobRunner, scan_job_record
from yahoo_mail_mcp.store.db import Store


def test_scan_jobs_are_persisted_and_reported(store):
    store.create_scan_job("job-1", "personal", json.dumps(["Inbox"]), 100)

    active = store.active_scan_job("personal")
    assert active is not None
    assert scan_job_record(active)["folders"] == ["Inbox"]

    store.update_scan_job(
        "job-1",
        "completed",
        result_json=json.dumps({"total_scanned_this_run": 100}),
    )
    row = store.get_scan_job("job-1")
    assert row is not None
    record = scan_job_record(row)
    assert record["status"] == "completed"
    assert record["result"]["total_scanned_this_run"] == 100
    assert record["completed_at"] is not None


def test_running_scan_jobs_are_interrupted_after_restart(store):
    store.create_scan_job("job-1", "personal", None, None)
    store.update_scan_job("job-1", "running")

    assert store.interrupt_stale_scan_jobs() == 1

    row = store.get_scan_job("job-1")
    assert row is not None
    assert row["status"] == "interrupted"
    assert "resume from checkpoints" in row["error"]


def test_scan_job_runner_persists_completed_result(tmp_path, monkeypatch):
    path = tmp_path / "jobs.db"
    store = Store(path)
    settings = Settings(db_path=path)
    store.create_scan_job("job-1", "personal", None, 25)

    class FakeWorkerContext:
        def __init__(self, worker_settings):
            self.store = Store(worker_settings.db_path)

        def imap(self, _account):
            return object()

        def close(self):
            self.store.close()

    monkeypatch.setattr(app_module, "AppContext", FakeWorkerContext)
    monkeypatch.setattr(
        jobs,
        "scan_mailbox",
        lambda *_args: SimpleNamespace(total_scanned=25, folders=[]),
    )

    runner = ScanJobRunner(settings, store)
    runner._run("job-1", "personal", None, 25)
    runner.close()

    row = store.get_scan_job("job-1")
    assert row is not None
    assert row["status"] == "completed"
    assert json.loads(row["result_json"])["total_scanned_this_run"] == 25
    store.close()
