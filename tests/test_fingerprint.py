from __future__ import annotations

import unittest
from datetime import UTC, datetime

from log_analyzer.fingerprint import make_fingerprint, normalize_message
from log_analyzer.models import ErrorEvent, ParsedError, StackFrame


class FingerprintTests(unittest.TestCase):
    def test_normalizes_volatile_values(self) -> None:
        left = "request 6ba7b810-9dad-11d1-80b4-00c04fd430c8 failed id 123456"
        right = "request 550e8400-e29b-41d4-a716-446655440000 failed id 987654"
        self.assertEqual(normalize_message(left), normalize_message(right))

    def test_keeps_application_location_in_fingerprint(self) -> None:
        event = ErrorEvent(
            source_name="memory",
            event_id="1",
            occurred_at=datetime.now(UTC),
            service="orders",
            severity="ERROR",
            message="failed",
        )
        parsed = ParsedError(
            language="java",
            error_type="java.lang.IllegalStateException",
            message="failed",
            frames=(
                StackFrame(
                    file_path="OrderService.java",
                    line_number=42,
                    column_number=None,
                    function_name="run",
                    class_name="com.example.OrderService",
                    module_name=None,
                    in_application=True,
                ),
            ),
            cause_chain=(),
            parser_name="java",
            parse_warnings=(),
        )
        other = ParsedError(
            language=parsed.language,
            error_type=parsed.error_type,
            message=parsed.message,
            frames=(
                StackFrame(
                    file_path="OrderService.java",
                    line_number=43,
                    column_number=None,
                    function_name="run",
                    class_name="com.example.OrderService",
                    module_name=None,
                    in_application=True,
                ),
            ),
            cause_chain=parsed.cause_chain,
            parser_name=parsed.parser_name,
            parse_warnings=parsed.parse_warnings,
        )
        self.assertNotEqual(make_fingerprint(event, parsed), make_fingerprint(event, other))


if __name__ == "__main__":
    unittest.main()
