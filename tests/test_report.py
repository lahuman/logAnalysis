from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from log_analyzer.analysis.models import (
    AnalysisRequest,
    AnalysisResult,
    Evidence,
    RecommendedFix,
    RootCause,
)
from log_analyzer.report import ReportWriter


def report_request() -> AnalysisRequest:
    return AnalysisRequest(
        service="orders",
        environment="production",
        version="1.0",
        fingerprint="fp",
        error_type="java.lang.NullPointerException",
        message="password=message-secret failed",
        stack_trace=(
            "Authorization: Bearer stack-secret\n"
            "at com.example.OrderService.load(OrderService.java:42)"
        ),
        parse_warnings=(),
        git_commit="d" * 40,
        source_path="src/main/java/com/example/OrderService.java",
        line_number=42,
        function_name="load",
        class_name="com.example.OrderService",
        source_code="api_key=sk-source-must-never-be-written",
        context_start_line=40,
        context_end_line=45,
    )


def report_result() -> AnalysisResult:
    path = "src/main/java/com/example/OrderService.java"
    return AnalysisResult(
        summary="api_key=sk-result-must-be-redacted",
        root_causes=[
            RootCause(
                cause="Nullable repository result",
                confidence=0.85,
                evidence=[Evidence(file=path, line=42, description="Null dereference")],
            )
        ],
        recommended_fixes=[
            RecommendedFix(description="Handle missing value", files=[path], risk="low")
        ],
        validation_steps=["Add a missing record test"],
        unknowns=["Production input is unavailable"],
    )


class ReportWriterTests(unittest.TestCase):
    def test_writes_deterministic_atomic_report_without_raw_source_or_secrets(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            writer = ReportWriter(directory)
            timestamp = datetime(2026, 9, 4, 1, 2, 3, tzinfo=timezone.utc)

            first = writer.write(
                report_request(),
                report_result(),
                event_id="../../bad event/id",
                occurrence_count=3,
                first_seen=timestamp,
                last_seen=timestamp,
                analyzed_at=timestamp,
                model="test-model",
            )
            second = writer.write(
                report_request(),
                report_result(),
                event_id="../../bad event/id",
                occurrence_count=3,
                analyzed_at=timestamp,
                model="test-model",
            )

            self.assertEqual(first, second)
            self.assertEqual(Path(directory).resolve(), first.parent.resolve())
            self.assertNotIn("..", first.name)
            contents = first.read_text(encoding="utf-8")
            self.assertIn("Status: COMPLETED", contents)
            self.assertIn("Occurrences: 3", contents)
            self.assertIn("OrderService.java:42", contents)
            self.assertIn(r"test\-model", contents)
            for sensitive in (
                "message-secret",
                "stack-secret",
                "sk-source-must-never-be-written",
                "sk-result-must-be-redacted",
            ):
                self.assertNotIn(sensitive, contents)
            self.assertNotIn("api_key=sk-source", contents)
            self.assertEqual([], list(Path(directory).glob("*.tmp")))

    def test_no_source_report_is_written_without_llm_claims(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            report = ReportWriter(directory).write_no_source(
                event_id="event-1",
                service="orders",
                environment="production",
                version="1.0",
                git_commit="missing",
                error_type="java.lang.IllegalStateException",
                message="password=no-source-secret",
                stack_trace="at com.example.Missing.run(Missing.java:10)",
                reason="Deployment commit was not found",
            )

            contents = report.read_text(encoding="utf-8")
            self.assertIn(r"Status: NO\_SOURCE", contents)
            self.assertIn("LLM was not called", contents)
            self.assertIn("Deployment commit was not found", contents)
            self.assertNotIn("no-source-secret", contents)
            self.assertIn("Model: \\-", contents)

    def test_occurrence_count_must_be_positive(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ValueError):
                ReportWriter(directory).write(
                    report_request(),
                    report_result(),
                    event_id="event",
                    occurrence_count=0,
                )

    def test_same_event_id_from_different_sources_does_not_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            writer = ReportWriter(directory)
            first = writer.write(
                report_request(), report_result(), event_id="same", source_name="a"
            )
            second = writer.write(
                report_request(), report_result(), event_id="same", source_name="b"
            )

            self.assertNotEqual(first, second)
            self.assertTrue(first.exists())
            self.assertTrue(second.exists())


if __name__ == "__main__":
    unittest.main()
