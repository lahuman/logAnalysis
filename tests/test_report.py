from __future__ import annotations

import tempfile
import unittest
from unittest.mock import patch
from datetime import datetime, timezone
from pathlib import Path

from log_analyzer.analysis.models import (
    AnalysisRequest,
    AnalysisResult,
    ErrorPriority,
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
        summary="주문 조회 결과가 없습니다 api_key=sk-result-must-be-redacted",
        root_causes=[
            RootCause(
                cause="조회 결과의 null 참조 가능성",
                confidence=0.85,
                evidence=[Evidence(file=path, line=42, description="null 객체 참조")],
            )
        ],
        recommended_fixes=[
            RecommendedFix(description="조회 결과가 없을 때의 처리를 추가하세요", files=[path], risk="low")
        ],
        validation_steps=["조회 결과가 없는 경우의 회귀 테스트를 추가하세요"],
        unknowns=["실제 운영 입력은 확인되지 않았습니다"],
    )


class ReportWriterTests(unittest.TestCase):
    def test_korean_report_finishes_with_exactly_three_single_line_summaries(self) -> None:
        result = report_result().model_copy(update={
            "summary": "첫 번째 판단\n두 번째 판단 password=summary-secret",
            "error_priority": ErrorPriority.unassessed().model_copy(update={
                "impact": "영향 확인 필요\n추가 점검 필요",
                "response_action": "즉시 점검\r\n담당자 공유 " + "장애 대응 " * 100,
            }),
        })
        with tempfile.TemporaryDirectory() as directory:
            contents = ReportWriter(directory).write(report_request(), result, event_id="summary").read_text(encoding="utf-8")
        self.assertTrue(contents.startswith("# Java 오류 분석 리포트"))
        self.assertIn("## 권장 수정 방법", contents)
        lines = contents.split("## 세 줄 요약\n\n")[-1].strip().splitlines()
        self.assertEqual(3, len(lines))
        for index, line in enumerate(lines, 1):
            self.assertTrue(line.startswith(f"{index}. "))
            self.assertLess(len(line), 300)
        self.assertNotIn("summary-secret", contents)
        self.assertIn("OrderService.java:42", contents)
        self.assertNotIn("## Recommended fixes", contents)
    def test_packaged_fallback_template_includes_priority(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with patch("log_analyzer.report.Path.read_text", side_effect=FileNotFoundError):
                writer = ReportWriter(directory)
            report = writer.write(report_request(), report_result(), event_id="fallback")
            contents = report.read_text(encoding="utf-8")
            self.assertIn("**오류 수준: 중간**", contents)
            self.assertIn("상향·긴급 재평가 조건:", contents)

    def test_each_priority_shows_timing_impact_and_response_before_stack_trace(self) -> None:
        for level, timing in (("높음", "즉시"), ("중간", "당일 업무 시간"), ("낮음", "다음 정기 개선")):
            with self.subTest(level=level), tempfile.TemporaryDirectory() as directory:
                priority = ErrorPriority(
                    level=level, provisional=False,
                    rationale="오류와 소스에서 확인한 근거 password=priority-secret",
                    impact="영향 범위 api_key=impact-secret",
                    response_action="실패 요청을 확인하세요 password=action-secret",
                    escalation_condition="실패 범위가 증가하면 재평가하세요 password=escalation-secret",
                )
                result = report_result().model_copy(update={"error_priority": priority})
                report = ReportWriter(directory).write(report_request(), result, event_id=level)
                contents = report.read_text(encoding="utf-8")
                self.assertIn(f"**오류 수준: {level}**", contents)
                self.assertIn(timing, contents)
                self.assertIn("판단 근거:", contents)
                self.assertIn("서비스·데이터 영향:", contents)
                self.assertIn("우선 대응:", contents)
                self.assertIn("상향·긴급 재평가 조건:", contents)
                self.assertLess(contents.index("오류 수준"), contents.index("대표 스택 트레이스"))
                self.assertIn("변경 위험도: 낮음", contents)
                for secret in ("priority-secret", "impact-secret", "action-secret", "escalation-secret"):
                    self.assertNotIn(secret, contents)

    def test_old_custom_template_still_displays_priority_and_response_guidance(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            template = Path(directory) / "custom.md"
            template.write_text("# Custom report\n\n${summary}\n", encoding="utf-8")
            report = ReportWriter(directory, template_path=template).write(
                report_request(), report_result(), event_id="custom",
            )
            contents = report.read_text(encoding="utf-8")
            self.assertTrue(contents.startswith("# Custom report\n"))
            self.assertIn("**오류 수준: 중간**", contents)
            self.assertIn("잠정 판단", contents)
            self.assertIn("권장 처리 시점:", contents)

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
            self.assertIn("처리 상태: 분석 완료", contents)
            self.assertIn("발생 횟수: 3", contents)
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
                reason="배포 커밋을 찾지 못했습니다",
            )

            contents = report.read_text(encoding="utf-8")
            self.assertIn(r"처리 상태: 소스 미확인", contents)
            self.assertIn("LLM 분석을 수행하지 않았습니다", contents)
            self.assertIn("**오류 수준: 중간**", contents)
            self.assertIn("잠정 판단", contents)
            self.assertIn("소스를 확인하지 못해", contents)
            self.assertIn("배포 커밋을 찾지 못했습니다", contents)
            self.assertNotIn("no-source-secret", contents)
            self.assertIn("분석 모델: \\-", contents)
            self.assertEqual(3, len(contents.split("## 세 줄 요약\n\n")[-1].strip().splitlines()))

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
