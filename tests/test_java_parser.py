from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import unittest

from log_analyzer.models import ErrorEvent
from log_analyzer.parsers import GenericErrorParser, JavaErrorParser, parse_event
from log_analyzer.parsers.java import normalize_java_class_name


FIXTURES = Path(__file__).parent / "fixtures" / "java"
INTERNET_FIXTURES = FIXTURES / "internet_samples"


def make_event(**overrides: object) -> ErrorEvent:
    values: dict[str, object] = {
        "source_name": "test",
        "event_id": "event-1",
        "occurred_at": datetime(2026, 1, 2, tzinfo=timezone.utc),
        "service": "order-api",
        "severity": "ERROR",
        "message": "request failed",
    }
    values.update(overrides)
    return ErrorEvent(**values)  # type: ignore[arg-type]


class JavaErrorParserTests(unittest.TestCase):
    def test_parses_internet_derived_exception_varieties(self) -> None:
        cases = (
            ("null_pointer.txt", "java.lang.NullPointerException", "MyClass", 9),
            (
                "stack_overflow_spring.txt",
                "java.lang.StackOverflowError",
                "com.spectrum.sci.osm.service.OrderDetailsService",
                76,
            ),
            (
                "bean_creation_no_such_method.txt",
                "java.lang.NoSuchMethodError",
                "com.thoughtworks.xstream.io.xml.AbstractXppDriver",
                50,
            ),
            (
                "out_of_memory.txt",
                "java.lang.OutOfMemoryError",
                "java.nio.HeapByteBuffer",
                64,
            ),
            (
                "sql_constraint.txt",
                "java.sql.SQLIntegrityConstraintViolationException",
                "com.mysql.cj.jdbc.exceptions.SQLError",
                117,
            ),
            (
                "class_not_found.txt",
                "java.lang.ClassNotFoundException",
                "jdk.internal.loader.BuiltinClassLoader",
                641,
            ),
            (
                "completion_exception.txt",
                "java.lang.IllegalStateException",
                "com.acme.payment.PaymentService",
                91,
            ),
        )
        parser = JavaErrorParser()

        for filename, error_type, first_class, first_line in cases:
            with self.subTest(filename=filename):
                stack_trace = (INTERNET_FIXTURES / filename).read_text(encoding="utf-8")
                parsed = parser.parse(
                    make_event(stack_trace=stack_trace, language_hint="java")
                )

                self.assertEqual(error_type, parsed.error_type)
                self.assertTrue(parsed.frames)
                self.assertEqual(first_class, parsed.frames[0].class_name)
                self.assertEqual(first_line, parsed.frames[0].line_number)

    def test_parses_deepest_cause_and_reconstructs_omitted_frames(self) -> None:
        stack_trace = (FIXTURES / "caused_by.txt").read_text(encoding="utf-8")
        parser = JavaErrorParser(application_packages=("com.example.order",))

        parsed = parser.parse(make_event(stack_trace=stack_trace, language_hint="java"))

        self.assertEqual("java", parsed.language)
        self.assertEqual("com.example.order.ValidationException", parsed.error_type)
        self.assertEqual("invalid order 42", parsed.message)
        self.assertEqual(
            (
                "java.lang.RuntimeException: request failed",
                "com.example.order.ValidationException: invalid order 42",
            ),
            parsed.cause_chain,
        )
        self.assertEqual(3, len(parsed.frames))
        self.assertEqual(
            "com.example.order.OrderService$Worker", parsed.frames[0].class_name
        )
        self.assertEqual("lambda$save$0", parsed.frames[0].function_name)
        self.assertEqual(42, parsed.frames[0].line_number)
        self.assertEqual("app", parsed.frames[0].module_name)
        self.assertTrue(parsed.frames[0].in_application)
        self.assertEqual(
            "com.example.order.OrderController", parsed.frames[1].class_name
        )
        self.assertFalse(parsed.frames[2].in_application)
        self.assertIn("suppressed_exceptions_ignored:1", parsed.parse_warnings)
        self.assertNotIn(
            "com.example.order.TempResource",
            {frame.class_name for frame in parsed.frames},
        )

    def test_parses_modules_cglib_and_native_locations(self) -> None:
        stack_trace = (FIXTURES / "module_and_native.txt").read_text(encoding="utf-8")
        parser = JavaErrorParser(application_packages=("com.example.order",))

        parsed = parser.parse(make_event(stack_trace=stack_trace))

        self.assertEqual("java.base", parsed.frames[0].module_name)
        self.assertFalse(parsed.frames[0].in_application)
        self.assertEqual("app", parsed.frames[1].module_name)
        self.assertEqual(
            "com.example.order.OrderService$$EnhancerBySpringCGLIB$$abc",
            parsed.frames[1].class_name,
        )
        self.assertEqual("com.example.order.OrderService", normalize_java_class_name(parsed.frames[1].class_name or ""))
        self.assertIsNone(parsed.frames[2].file_path)
        self.assertIsNone(parsed.frames[2].line_number)
        self.assertIsNone(parsed.frames[3].file_path)
        self.assertIsNone(parsed.frames[3].line_number)

    def test_uses_event_fields_when_hint_says_java_but_trace_is_incomplete(self) -> None:
        event = make_event(
            error_type="com.example.CustomProblem",
            message="broken input",
            stack_trace="not a complete stack trace",
            language_hint="JAVA",
        )
        parser = JavaErrorParser()

        self.assertTrue(parser.can_parse(event))
        parsed = parser.parse(event)

        self.assertEqual("com.example.CustomProblem", parsed.error_type)
        self.assertEqual("broken input", parsed.message)
        self.assertEqual((), parsed.frames)
        self.assertIn("no_stack_frames", parsed.parse_warnings)

    def test_framework_prefix_uses_package_boundary(self) -> None:
        parser = JavaErrorParser(
            application_packages=("com.example",),
            framework_packages=("com.example.framework",),
        )
        parsed = parser.parse(
            make_event(
                stack_trace=(
                    "java.lang.RuntimeException: failed\n"
                    " at com.example.framework.Proxy.call(Proxy.java:1)\n"
                    " at com.example.frameworkish.Real.call(Real.java:2)"
                )
            )
        )

        self.assertFalse(parsed.frames[0].in_application)
        self.assertTrue(parsed.frames[1].in_application)

    def test_common_log_indentation_does_not_hide_main_cause(self) -> None:
        parser = JavaErrorParser(application_packages=("com.example",))
        parsed = parser.parse(
            make_event(
                stack_trace=(
                    "    java.lang.RuntimeException: outer\n"
                    "        at com.example.Outer.call(Outer.java:10)\n"
                    "        Suppressed: java.io.IOException: ignored\n"
                    "            at com.example.Resource.close(Resource.java:5)\n"
                    "    Caused by: com.example.InnerException: inner\n"
                    "        at com.example.Inner.call(Inner.java:20)"
                )
            )
        )

        self.assertEqual("com.example.InnerException", parsed.error_type)
        self.assertEqual("com.example.Inner", parsed.frames[0].class_name)

    def test_parser_selection_falls_back_after_expected_parser_error(self) -> None:
        class BrokenParser:
            name = "broken"

            def can_parse(self, event: ErrorEvent) -> bool:
                return True

            def parse(self, event: ErrorEvent):
                raise RuntimeError("bad input")

        parsed = parse_event(
            make_event(error_type="Problem"),
            (BrokenParser(), GenericErrorParser()),
        )

        self.assertEqual("generic", parsed.parser_name)
        self.assertIn("parser_failed: broken: bad input", parsed.parse_warnings)


if __name__ == "__main__":
    unittest.main()
