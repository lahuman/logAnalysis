from __future__ import annotations

import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path

from log_analyzer.models import ErrorEvent
from log_analyzer.storage import JobStatus, SQLiteStateStore


NOW = datetime(2026, 9, 4, 1, 0, tzinfo=UTC)


def _event(event_id: str = "event-1") -> ErrorEvent:
    return ErrorEvent(
        source_name="elasticsearch",
        event_id=event_id,
        occurred_at=NOW,
        service="orders",
        severity="ERROR",
        message="safe summary",
        git_commit="a" * 40,
        raw_log="SECRET RAW LOG MUST NOT BE STORED",
    )


class StorageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.path = Path(self.temporary_directory.name) / "state.db"
        self.store = SQLiteStateStore(self.path)

    def tearDown(self) -> None:
        self.store.close()
        self.temporary_directory.cleanup()

    def test_event_registration_is_idempotent_and_omits_raw_log(self) -> None:
        event = _event()
        self.assertTrue(self.store.register_event(event, "fingerprint", now=NOW))
        self.assertFalse(self.store.register_event(event, "other", now=NOW))
        self.assertEqual(self.store.count_jobs(), 1)

        job = self.store.get_job(event.source_name, event.event_id)
        self.assertIsNotNone(job)
        self.assertEqual(job.fingerprint, "fingerprint")
        with closing(sqlite3.connect(self.path)) as connection:
            columns = {
                row[1]
                for row in connection.execute("PRAGMA table_info(analysis_job)")
            }
        self.assertNotIn("raw_log", columns)
        self.assertNotIn("message", columns)

    def test_status_and_checkpoint_round_trip(self) -> None:
        event = _event()
        self.store.register_event(event, "fingerprint", now=NOW)
        self.assertTrue(
            self.store.set_status(
                event.source_name,
                event.event_id,
                JobStatus.COMPLETED,
                report_path="/reports/event-1.md",
                now=NOW,
            )
        )
        job = self.store.get_job(event.source_name, event.event_id)
        self.assertEqual(job.status, JobStatus.COMPLETED)
        self.assertEqual(job.report_path, "/reports/event-1.md")

        self.store.save_checkpoint("elasticsearch", "cursor", NOW, now=NOW)
        checkpoint = self.store.get_checkpoint("elasticsearch")
        self.assertEqual(checkpoint.cursor, "cursor")
        self.assertEqual(checkpoint.ended_at, NOW)

    def test_retry_uses_exponential_backoff_and_stops_at_five(self) -> None:
        event = _event()
        self.store.register_event(event, "fingerprint", now=NOW)

        status = self.store.mark_retry(
            event.source_name,
            event.event_id,
            "temporary",
            request_json='{"message":"safe"}',
            now=NOW,
        )
        self.assertEqual(status, JobStatus.RETRY_WAIT)
        self.assertEqual(self.store.due_retries(now=NOW + timedelta(seconds=59)), ())
        self.assertEqual(
            len(self.store.due_retries(now=NOW + timedelta(seconds=60))),
            1,
        )

        for attempt in range(2, 6):
            status = self.store.mark_retry(
                event.source_name,
                event.event_id,
                "temporary",
                now=NOW + timedelta(hours=attempt),
            )
        self.assertEqual(status, JobStatus.PERMANENT_FAILURE)
        self.assertEqual(self.store.get_job(event.source_name, event.event_id).attempts, 5)

    def test_retry_context_survives_reopen_without_raw_event(self) -> None:
        event = _event()
        redacted_request = '{"message":"[REDACTED]","source_code":"safe"}'
        self.store.register_event(event, "fingerprint", now=NOW)
        self.store.mark_retry(
            event.source_name,
            event.event_id,
            "temporary",
            request_json=redacted_request,
            now=NOW,
        )
        self.store.close()
        self.store = SQLiteStateStore(self.path)

        jobs = self.store.due_retries(now=NOW + timedelta(minutes=1))

        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0].request_json, redacted_request)
        self.assertNotIn("SECRET RAW LOG", jobs[0].request_json)

    def test_occurrence_count_can_be_scoped_to_commit(self) -> None:
        first = _event("first")
        second = _event("second")
        third = ErrorEvent(
            source_name="elasticsearch",
            event_id="third",
            occurred_at=NOW,
            service="orders",
            severity="ERROR",
            message="safe",
            git_commit="b" * 40,
        )
        for event in (first, second, third):
            self.store.register_event(event, "same-fingerprint", now=NOW)

        self.assertEqual(self.store.count_occurrences("same-fingerprint"), 3)
        self.assertEqual(
            self.store.count_occurrences("same-fingerprint", "a" * 40),
            2,
        )

    def test_analysis_cache_requires_commit_and_uses_full_identity(self) -> None:
        self.store.save_analysis_cache(
            fingerprint="fingerprint",
            git_commit="a" * 40,
            model="model",
            prompt_version="1",
            analyzer_version="1",
            analysis_json='{"summary":"ok"}',
            now=NOW,
        )
        self.assertTrue(
            self.store.has_valid_analysis(
                "fingerprint", "a" * 40, "model", "1", "1"
            )
        )
        self.assertFalse(
            self.store.has_valid_analysis("fingerprint", None, "model", "1", "1")
        )
        self.assertFalse(
            self.store.has_valid_analysis(
                "fingerprint", "a" * 40, "another-model", "1", "1"
            )
        )

    def test_retention_deletes_only_old_terminal_jobs_and_cache(self) -> None:
        terminal = _event("terminal")
        active = _event("active")
        old = NOW - timedelta(days=31)
        self.store.register_event(terminal, "f1", now=old)
        self.store.set_status(
            terminal.source_name,
            terminal.event_id,
            JobStatus.COMPLETED,
            report_path="/reports/terminal.md",
            now=old,
        )
        self.store.register_event(active, "f2", now=old)
        self.store.save_analysis_cache(
            fingerprint="f1",
            git_commit="a" * 40,
            model="model",
            prompt_version="1",
            analyzer_version="1",
            analysis_json="{}",
            report_path="/reports/terminal.md",
            now=old,
        )
        self.store.save_checkpoint("elasticsearch", None, old, now=old)

        result = self.store.purge_older_than(NOW - timedelta(days=30))

        self.assertEqual(result.deleted_jobs, 1)
        self.assertEqual(result.deleted_cache_entries, 1)
        self.assertEqual(result.report_paths, ("/reports/terminal.md",))
        self.assertIsNotNone(self.store.get_job("elasticsearch", "active"))
        self.assertIsNotNone(self.store.get_checkpoint("elasticsearch"))

    def test_retention_does_not_delete_a_report_still_referenced_by_a_job(self) -> None:
        old_event = _event("old-shared")
        current_event = _event("current-shared")
        old = NOW - timedelta(days=31)
        shared_path = "/reports/shared.md"
        self.store.register_event(old_event, "same", now=old)
        self.store.set_status(
            old_event.source_name,
            old_event.event_id,
            JobStatus.COMPLETED,
            report_path=shared_path,
            now=old,
        )
        self.store.register_event(current_event, "same", now=NOW)
        self.store.set_status(
            current_event.source_name,
            current_event.event_id,
            JobStatus.PARSED,
            report_path=shared_path,
            now=NOW,
        )

        result = self.store.purge_older_than(NOW - timedelta(days=30))

        self.assertEqual(result.deleted_jobs, 1)
        self.assertEqual(result.report_paths, ())

    def test_report_cleanup_queue_survives_until_confirmed(self) -> None:
        old_event = _event("cleanup")
        old = NOW - timedelta(days=31)
        self.store.register_event(old_event, "cleanup", now=old)
        self.store.set_status(
            old_event.source_name,
            old_event.event_id,
            JobStatus.COMPLETED,
            report_path="/reports/cleanup.md",
            now=old,
        )

        first = self.store.purge_older_than(NOW - timedelta(days=30))
        second = self.store.purge_older_than(NOW - timedelta(days=30))
        self.assertEqual(first.report_paths, ("/reports/cleanup.md",))
        self.assertEqual(second.report_paths, ("/reports/cleanup.md",))

        self.store.confirm_report_deletions(second.report_paths)
        third = self.store.purge_older_than(NOW - timedelta(days=30))
        self.assertEqual(third.report_paths, ())

    def test_recovers_interrupted_analysis_with_redacted_request(self) -> None:
        interrupted = _event("interrupted")
        self.store.register_event(interrupted, "fingerprint", now=NOW)
        self.store.set_status(
            interrupted.source_name,
            interrupted.event_id,
            JobStatus.ANALYZING,
            request_json='{"safe":true}',
            now=NOW,
        )

        recovered = self.store.recover_interrupted_analysis(now=NOW)
        job = self.store.get_job(interrupted.source_name, interrupted.event_id)

        self.assertEqual(recovered, 1)
        self.assertEqual(job.status, JobStatus.RETRY_WAIT)
        self.assertEqual(job.next_retry_at, NOW)

    def test_healthcheck(self) -> None:
        self.store.healthcheck()


if __name__ == "__main__":
    unittest.main()
