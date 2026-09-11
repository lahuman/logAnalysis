from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
import tempfile
import unittest

from log_analyzer.config import FileSourceConfig
from log_analyzer.models import ErrorQuery
from log_analyzer.parsers import JavaErrorParser
from log_analyzer.sources.file import FileErrorSource, FileSourceError


REPORT = """---

Server Instance : example-node-1
Exception Time : 2026-09-04 20:32:02.618
Exception TxID : EXAMPLE.001
Exception UUID : sample-uuid
Exception UserIP : private-test-value
Exception UserID : private-user-value
Exception Code : SAMPLE001
Exception Message : 처리할 수 없습니다.
다시 시도해 주세요.
Exception Screen ID : EXAMPLE.SCREEN
Exception RootCause : Exception:
Exception StackTrace : NexusException [code=SAMPLE001, message=처리할 수 없습니다.
다시 시도해 주세요.]
    at com.example.Handler.handle(Handler.java:1)
    at com.example.Wrapper.wrap(Wrapper.java:2)
    at com.example.Controller.execute(Controller.java:3)
    at org.example.Dispatcher.call(Dispatcher.java:4)
    at java.lang.Thread.run(Thread.java:5)
Caused by: ServiceException [code=SAMPLE001, message=처리할 수 없습니다.
다시 시도해 주세요.]
    at com.example.Service.invoke(Service.java:6)
    ... 3 more
Caused by: UIException [code=SAMPLE001, message=실패]
    at com.example.ErrorUtil.wrap(ErrorUtil.java:7)
    at [com.example.Business.send](https://example.invalid/method)(Business.java:8)
    ... 4 more
Caused by: java.lang.Exception
    ... 5 more

Exception ExtraRootCause :

"""


class NexusFileSourceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        work = tempfile.TemporaryDirectory()
        self.addCleanup(work.cleanup)
        self.path = Path(work.name) / "internal.log"
        self.path.write_text(REPORT, encoding="utf-8")
        self.query = ErrorQuery(datetime(2026, 9, 10, tzinfo=UTC), datetime(2026, 9, 11, tzinfo=UTC), ("ERROR",), 1)

    def source(self, **options):
        config = FileSourceConfig(**{"type": "file", "format": "nexus", "name": "internal",
                                     "path": self.path, "service": "example", **options})
        source = FileErrorSource(config=config)
        self.addAsyncCleanup(source.close)
        return source

    async def test_raw_report_metadata_multiline_message_and_full_cause_chain(self):
        source = self.source()
        await source.healthcheck()
        event = (await source.fetch(self.query)).events[0]
        self.assertEqual(event.occurred_at, datetime(2026, 9, 4, 11, 32, 2, 618000, tzinfo=UTC))
        self.assertEqual(event.service, "example")
        self.assertEqual(event.severity, "ERROR")
        self.assertEqual(event.trace_id, "sample-uuid")
        self.assertEqual(event.message, "처리할 수 없습니다.\n다시 시도해 주세요.")
        self.assertEqual(event.attributes["server_instance"], "example-node-1")
        self.assertEqual(event.attributes["transaction_id"], "EXAMPLE.001")
        self.assertEqual(event.attributes["exception_code"], "SAMPLE001")
        self.assertNotIn("private-test-value", str(event.attributes))
        self.assertNotIn("private-user-value", str(event.attributes))
        parsed = JavaErrorParser(("com.example",)).parse(event)
        self.assertEqual(parsed.error_type, "java.lang.Exception")
        self.assertEqual([item.split(":", 1)[0] for item in parsed.cause_chain],
                         ["NexusException", "ServiceException", "UIException", "java.lang.Exception"])
        self.assertIn("다시 시도해 주세요.", parsed.cause_chain[1])
        self.assertEqual(parsed.frames[0].class_name, "com.example.Business")
        self.assertEqual(parsed.frames[0].function_name, "send")
        self.assertEqual(parsed.frames[0].line_number, 8)
        self.assertEqual(len(parsed.frames), 5)
        self.assertFalse(parsed.parse_warnings)

    async def test_multiple_records_with_or_without_separator_and_stable_ids(self):
        for separator in ("", "---\n\n"):
            with self.subTest(separator=separator):
                second = REPORT.removeprefix("---\n").replace("example-node-1", "example-node-2")
                self.path.write_text(REPORT + separator + second, encoding="utf-8")
                source = self.source()
                first = await source.fetch(self.query)
                second_page = await source.fetch(self.query, first.next_cursor)
                final = await source.fetch(self.query, second_page.next_cursor)
                self.assertFalse(final.events)
                self.assertIsNone(final.next_cursor)
                self.assertNotEqual(first.events[0].event_id, second_page.events[0].event_id)
                self.assertEqual(second_page.events[0].attributes["server_instance"], "example-node-2")
                replay = await source.fetch(self.query)
                self.assertEqual(first.events[0].event_id, replay.events[0].event_id)

    async def test_bom_cp949_crlf_and_nonbreaking_spaces(self):
        for encoding in ("utf-8", "cp949"):
            with self.subTest(encoding=encoding):
                content = REPORT.replace("\n", "\r\n")
                if encoding == "utf-8":
                    content = "\ufeff" + content.replace("    at", "\u00a0   at")
                self.path.write_bytes(content.encode(encoding))
                event = (await self.source(encoding=encoding).fetch(self.query)).events[0]
                parsed = JavaErrorParser(("com.example",)).parse(event)
                self.assertEqual(parsed.frames[0].line_number, 8)
                self.assertFalse(parsed.parse_warnings)

    async def test_timestamp_offset_and_time_filter(self):
        self.path.write_text(REPORT.replace("20:32:02.618", "20:32:02.618+02:00"), encoding="utf-8")
        event = (await self.source().fetch(self.query)).events[0]
        self.assertEqual(event.occurred_at.hour, 18)
        self.assertFalse((await self.source(filter_time_window=True).fetch(self.query)).events)

    async def test_missing_duplicate_and_invalid_fields_fail_without_log_contents(self):
        changes = (
            ("Exception Time : 2026-09-04 20:32:02.618", "Exception Time : secret-value"),
            ("Exception Time : 2026-09-04 20:32:02.618", ""),
            ("Exception UUID : sample-uuid", "Exception UUID : secret-value\nException UUID : another"),
            ("Exception StackTrace : NexusException", "NotAStackField : NexusException"),
        )
        for before, after in changes:
            with self.subTest(before=before):
                self.path.write_text(REPORT.replace(before, after), encoding="utf-8")
                with self.assertRaises(FileSourceError) as caught:
                    await self.source().fetch(self.query)
                self.assertNotIn("secret-value", str(caught.exception))
                self.assertIn("byte", str(caught.exception))

    async def test_limits_and_invalid_preamble_are_not_silently_ignored(self):
        with self.assertRaisesRegex(FileSourceError, "max_record_bytes"):
            await self.source(max_record_bytes=1024).fetch(self.query)
        self.path.write_text("unexpected\n" + REPORT, encoding="utf-8")
        with self.assertRaisesRegex(FileSourceError, "expected Nexus Server Instance"):
            await self.source().fetch(self.query)

    async def test_standard_format_remains_explicit_and_rejects_nexus_report(self):
        self.assertEqual(FileSourceConfig(type="file", path=self.path, service="example").format, "standard")
        with self.assertRaisesRegex(FileSourceError, "header_pattern"):
            await self.source(format="standard").fetch(self.query)


if __name__ == "__main__":
    unittest.main()
