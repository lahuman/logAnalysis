from __future__ import annotations

from datetime import datetime, timezone
import unittest

from log_analyzer.models import ErrorEvent
from log_analyzer.parsers import GenericErrorParser


class GenericErrorParserTests(unittest.TestCase):
    def test_preserves_explicit_error_fields_without_parsing_stack(self) -> None:
        event = ErrorEvent(
            source_name="test",
            event_id="1",
            occurred_at=datetime(2026, 1, 2, tzinfo=timezone.utc),
            service="order-api",
            severity="ERROR",
            error_type="RemoteFailure",
            message="downstream failed",
            raw_log="untrusted arbitrary content",
            language_hint="Custom",
        )

        parsed = GenericErrorParser().parse(event)

        self.assertEqual("custom", parsed.language)
        self.assertEqual("RemoteFailure", parsed.error_type)
        self.assertEqual("downstream failed", parsed.message)
        self.assertEqual(("RemoteFailure: downstream failed",), parsed.cause_chain)
        self.assertEqual((), parsed.frames)
        self.assertEqual("generic", parsed.parser_name)

    def test_uses_stack_trace_but_never_raw_log_when_message_is_empty(self) -> None:
        event = ErrorEvent(
            source_name="test",
            event_id="2",
            occurred_at=datetime(2026, 1, 2, tzinfo=timezone.utc),
            service="order-api",
            severity="ERROR",
            message="",
            stack_trace="\n  first useful line  \nsecond line",
            raw_log="password=must-not-be-used",
        )

        parsed = GenericErrorParser().parse(event)

        self.assertEqual("first useful line", parsed.message)
        self.assertNotIn("must-not-be-used", parsed.message)
        self.assertEqual((), parsed.cause_chain)


if __name__ == "__main__":
    unittest.main()
