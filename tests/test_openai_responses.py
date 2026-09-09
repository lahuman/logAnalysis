from __future__ import annotations

import json
import unittest

import httpx

from log_analyzer.analysis.models import AnalysisRequest, AnalysisResult
from log_analyzer.analysis.openai_responses import (
    GlobalAnalyzerError,
    InvalidResponseError,
    OpenAIResponsesAnalyzer,
    RetryableAnalyzerError,
)


def analysis_request(**changes: object) -> AnalysisRequest:
    values: dict[str, object] = {
        "service": "orders",
        "environment": "production",
        "version": "2026.09.04",
        "fingerprint": "fp-1",
        "error_type": "java.lang.NullPointerException",
        "message": "password=do-not-send value was null",
        "stack_trace": "at com.example.OrderService.load(OrderService.java:42)",
        "parse_warnings": (),
        "git_commit": "c" * 40,
        "source_path": "src/main/java/com/example/OrderService.java",
        "line_number": 42,
        "function_name": "load",
        "class_name": "com.example.OrderService",
        "source_code": "return order.id();",
        "context_start_line": 40,
        "context_end_line": 45,
    }
    values.update(changes)
    return AnalysisRequest(**values)


def result_payload(*, evidence_line: int = 42, evidence_file: str | None = None) -> dict:
    path = evidence_file or "src/main/java/com/example/OrderService.java"
    return {
        "summary": "주문 조회 결과가 null입니다.",
        "error_priority": {
            "level": "중간",
            "rationale": "주문 조회 결과의 null 참조가 확인되지만 전체 장애 범위는 알 수 없습니다.",
            "impact": "해당 요청은 실패할 수 있으며 다른 사용자와 데이터에 대한 영향은 확인이 필요합니다.",
            "response_action": "실패 요청 범위를 확인하고 누락된 주문 처리와 회귀 테스트를 추가하세요.",
            "escalation_condition": "핵심 주문 기능의 지속 실패 또는 데이터 손상이 확인되면 높음으로 상향하세요.",
            "provisional": True,
        },
        "root_causes": [
            {
                "cause": "null일 수 있는 조회 결과를 참조했습니다.",
                "confidence": 0.9,
                "evidence": [
                    {"file": path, "line": evidence_line, "description": "객체 참조 위치"}
                ],
            }
        ],
        "recommended_fixes": [
            {"description": "조회한 주문이 없는 경우를 처리하세요.", "files": [path], "risk": "low"}
        ],
        "validation_steps": ["주문이 없는 경우의 회귀 테스트를 추가하세요."],
        "unknowns": [],
    }


def response_for(payload: dict) -> dict:
    return {
        "output": [
            {"type": "reasoning", "summary": []},
            {
                "type": "message",
                "content": [
                    {"type": "annotation", "text": "ignore"},
                    {"type": "output_text", "text": json.dumps(payload)},
                ],
            },
        ]
    }


class OpenAIResponsesAnalyzerTests(unittest.IsolatedAsyncioTestCase):
    async def test_posts_strict_responses_request_and_redacts_before_serializing(self) -> None:
        captured: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            return httpx.Response(200, json=response_for(result_payload()))

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            analyzer = OpenAIResponsesAnalyzer(
                api_key="test-api-key",
                model="test-model",
                base_url="https://api.openai.test/v1",
                client=client,
            )
            result = await analyzer.analyze(analysis_request())

        self.assertEqual("주문 조회 결과가 null입니다.", result.summary)
        self.assertEqual(1, len(captured))
        request = captured[0]
        self.assertEqual("/v1/responses", request.url.path)
        self.assertEqual("Bearer test-api-key", request.headers["Authorization"])
        body = json.loads(request.content)
        self.assertFalse(body["store"])
        self.assertIn("Write every human-readable explanation in Korean", body["instructions"])
        self.assertEqual(3_000, body["max_output_tokens"])
        self.assertEqual("json_schema", body["text"]["format"]["type"])
        self.assertTrue(body["text"]["format"]["strict"])
        schema = body["text"]["format"]["schema"]
        self.assertIn("error_priority", schema["required"])
        self.assertEqual(set(schema["properties"]), set(schema["required"]))
        self.assertEqual("중간", result.error_priority.level)
        self.assertFalse(body["text"]["format"]["schema"]["additionalProperties"])
        self.assertNotIn("tools", body)
        self.assertNotIn("do-not-send", request.content.decode())

    async def test_missing_priority_is_repaired_in_new_provider_output(self) -> None:
        payload = result_payload()
        payload.pop("error_priority")
        responses = [payload, result_payload()]
        captured = []

        def handler(request):
            captured.append(request)
            return httpx.Response(200, json=response_for(responses.pop(0)))

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            result = await OpenAIResponsesAnalyzer(api_key="key", model="test", client=client).analyze(analysis_request())
        self.assertEqual(2, len(captured))
        self.assertEqual("중간", result.error_priority.level)

    async def test_scans_all_output_items_and_filters_unsupported_evidence(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json=response_for(
                    result_payload(evidence_line=999, evidence_file="src/Invented.java")
                ),
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            analyzer = OpenAIResponsesAnalyzer(
                api_key="key", model="model", client=client
            )
            result = await analyzer.analyze(analysis_request())

        self.assertEqual([], result.root_causes[0].evidence)
        self.assertEqual([], result.recommended_fixes[0].files)

    async def test_429_honors_retry_after_then_succeeds(self) -> None:
        calls = 0
        delays: list[float] = []

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            if calls == 1:
                return httpx.Response(429, headers={"Retry-After": "7.5"})
            return httpx.Response(200, json=response_for(result_payload()))

        async def record_sleep(delay: float) -> None:
            delays.append(delay)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            analyzer = OpenAIResponsesAnalyzer(
                api_key="key", model="model", client=client, sleep=record_sleep
            )
            await analyzer.analyze(analysis_request())

        self.assertEqual(2, calls)
        self.assertEqual([7.5], delays)

    async def test_retryable_status_stops_after_three_attempts(self) -> None:
        calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            return httpx.Response(503)

        async def no_sleep(delay: float) -> None:
            return None

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            analyzer = OpenAIResponsesAnalyzer(
                api_key="key", model="model", client=client, sleep=no_sleep
            )
            with self.assertRaises(RetryableAnalyzerError):
                await analyzer.analyze(analysis_request())

        self.assertEqual(3, calls)

    async def test_transport_errors_are_retried(self) -> None:
        calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            if calls < 3:
                raise httpx.ConnectError("unavailable", request=request)
            return httpx.Response(200, json=response_for(result_payload()))

        async def no_sleep(delay: float) -> None:
            return None

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            analyzer = OpenAIResponsesAnalyzer(
                api_key="key", model="model", client=client, sleep=no_sleep
            )
            await analyzer.analyze(analysis_request())

        self.assertEqual(3, calls)

    async def test_400_is_global_and_not_retried(self) -> None:
        calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            return httpx.Response(400, json={"error": {"message": "invalid"}})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            analyzer = OpenAIResponsesAnalyzer(
                api_key="key", model="model", client=client
            )
            with self.assertRaises(GlobalAnalyzerError):
                await analyzer.analyze(analysis_request())

        self.assertEqual(1, calls)

    async def test_invalid_schema_gets_exactly_one_correction_request(self) -> None:
        calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            if calls == 1:
                return httpx.Response(
                    200,
                    json={
                        "output": [
                            {
                                "type": "message",
                                "content": [{"type": "output_text", "text": "not-json"}],
                            }
                        ]
                    },
                )
            return httpx.Response(200, json=response_for(result_payload()))

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            analyzer = OpenAIResponsesAnalyzer(
                api_key="key", model="model", client=client
            )
            result = await analyzer.analyze(analysis_request())

        self.assertEqual(2, calls)
        self.assertIsInstance(result, AnalysisResult)

    async def test_missing_output_text_is_invalid_without_repair(self) -> None:
        calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            return httpx.Response(200, json={"output": [{"type": "reasoning"}]})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            analyzer = OpenAIResponsesAnalyzer(
                api_key="key", model="model", client=client
            )
            with self.assertRaises(InvalidResponseError):
                await analyzer.analyze(analysis_request())

        self.assertEqual(1, calls)


if __name__ == "__main__":
    unittest.main()
