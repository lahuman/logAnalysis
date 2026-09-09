from __future__ import annotations

import unittest

from log_analyzer.analysis.models import AnalysisRequest, AnalysisResult, ErrorPriority
from log_analyzer.analysis.redact import RedactionError, RedactionLimits, SecretRedactor


def request_with(**changes: object) -> AnalysisRequest:
    values: dict[str, object] = {
        "service": "orders",
        "environment": "production",
        "version": "1.2.3",
        "fingerprint": "abc123",
        "error_type": "java.lang.IllegalStateException",
        "message": "request failed",
        "stack_trace": "at com.example.OrderService.run(OrderService.java:42)",
        "parse_warnings": (),
        "git_commit": "a" * 40,
        "source_path": "src/main/java/com/example/OrderService.java",
        "line_number": 42,
        "function_name": "run",
        "class_name": "com.example.OrderService",
        "source_code": "public void run() { throw new IllegalStateException(); }",
        "context_start_line": 40,
        "context_end_line": 45,
    }
    values.update(changes)
    return AnalysisRequest(**values)


class SecretRedactorTests(unittest.TestCase):
    def test_redacts_common_secrets_and_personal_data(self) -> None:
        secret = "super-secret-token"
        raw = (
            "Authorization: Bearer eyJabcde.eyJfghij.abcdefghij\n"
            f"password={secret} api_key=sk-abcdefghijklmnopqrstuvwxyz\n"
            "Cookie: session_id=customer-session\n"
            "email=person@example.com client=192.168.10.4\n"
            "customer_id=customer-42 phone=010-1234-5678\n"
            "resident=900101-1234567 key=AKIAABCDEFGHIJKLMNOP\n"
            "jdbc:postgresql://dbuser:dbpass@db.internal/orders"
        )

        redacted = SecretRedactor().redact_text(raw)

        for sensitive in (
            secret,
            "eyJabcde.eyJfghij.abcdefghij",
            "sk-abcdefghijklmnopqrstuvwxyz",
            "customer-session",
            "person@example.com",
            "192.168.10.4",
            "customer-42",
            "010-1234-5678",
            "900101-1234567",
            "AKIAABCDEFGHIJKLMNOP",
            "dbuser:dbpass",
        ):
            self.assertNotIn(sensitive, redacted)
        self.assertIn("[REDACTED_AUTHORIZATION]", redacted)
        self.assertIn("[REDACTED_SECRET]", redacted)

    def test_redacts_private_key_blocks(self) -> None:
        raw = (
            "before\n-----BEGIN PRIVATE KEY-----\nabc123\n"
            "-----END PRIVATE KEY-----\nafter"
        )
        redacted = SecretRedactor().redact_text(raw)
        self.assertNotIn("abc123", redacted)
        self.assertIn("[REDACTED_PRIVATE_KEY]", redacted)

    def test_redacts_git_change_context_before_serialization(self) -> None:
        request = request_with(
            revision_source="repository_ref",
            git_reference="main",
            git_change_context="+ password=history-secret\n+ api_key=sk-abcdefghijklmnop",
        )

        redacted = SecretRedactor().redact_request(request)

        self.assertEqual(redacted.revision_source, "repository_ref")
        self.assertEqual(redacted.git_reference, "main")
        self.assertNotIn("history-secret", redacted.git_change_context)
        self.assertNotIn("sk-abcdefghijklmnop", redacted.git_change_context)

    def test_request_is_redacted_before_use_and_context_is_bounded(self) -> None:
        limits = RedactionLimits(
            message_chars=40,
            stack_trace_chars=50,
            source_code_chars=60,
            warning_chars=32,
            warning_count=1,
        )
        request = request_with(
            message="password=hidden " + "m" * 100,
            stack_trace="Authorization: Bearer hidden-token " + "s" * 100,
            source_code="api_key=sk-abcdefghijklmnop " + "c" * 10,
            parse_warnings=("person@example.com " + "w" * 100, "discarded"),
        )

        safe = SecretRedactor(limits).redact_request(request)

        serialized = safe.to_prompt_json()
        for sensitive in (
            "hidden-token",
            "sk-abcdefghijklmnop",
            "person@example.com",
            "password=hidden",
            "discarded",
        ):
            self.assertNotIn(sensitive, serialized)
        self.assertLessEqual(len(safe.message), limits.message_chars)
        self.assertLessEqual(len(safe.stack_trace), limits.stack_trace_chars)
        self.assertLessEqual(len(safe.source_code), limits.source_code_chars)
        self.assertEqual(1, len(safe.parse_warnings))
        self.assertEqual("api_key=[REDACTED_SECRET] " + "c" * 10, safe.source_code)

    def test_oversized_method_is_rejected_instead_of_silently_cut(self) -> None:
        request = request_with(source_code="public void run() {\n" + "doWork();\n" * 100 + "}")
        with self.assertRaisesRegex(RedactionError, "refusing to truncate the method"):
            SecretRedactor(RedactionLimits(source_code_chars=60)).redact_request(request)

    def test_model_result_is_redacted_before_persistence(self) -> None:
        result = AnalysisResult(
            summary="password=model-secret",
            error_priority=ErrorPriority.unassessed().model_copy(update={
                "rationale": "password=priority-secret",
                "impact": "password=impact-secret",
                "response_action": "password=response-secret",
                "escalation_condition": "password=escalation-secret",
            }),
            root_causes=[],
            recommended_fixes=[],
            validation_steps=["contact person@example.com"],
            unknowns=[],
        )

        safe = SecretRedactor().redact_result(result)
        serialized = safe.model_dump_json()

        self.assertNotIn("model-secret", serialized)
        for secret in ("priority-secret", "impact-secret", "response-secret", "escalation-secret"):
            self.assertNotIn(secret, serialized)
        self.assertEqual("중간", safe.error_priority.level)
        self.assertNotIn("person@example.com", serialized)
        self.assertIn("[REDACTED_SECRET]", serialized)


if __name__ == "__main__":
    unittest.main()
