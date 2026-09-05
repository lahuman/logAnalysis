from __future__ import annotations

import asyncio
import unittest
from datetime import UTC, datetime, timedelta

from log_analyzer.models import ErrorEvent, ErrorQuery
from log_analyzer.sources.memory import InMemoryErrorSource


def event(event_id: str, occurred_at: datetime, severity: str = "ERROR") -> ErrorEvent:
    return ErrorEvent(
        source_name="memory",
        event_id=event_id,
        occurred_at=occurred_at,
        service="orders",
        severity=severity,
        message="failure",
    )


class InMemoryErrorSourceTests(unittest.TestCase):
    def test_filters_and_pages_deterministically(self) -> None:
        now = datetime.now(UTC)
        source = InMemoryErrorSource(
            [
                event("2", now),
                event("1", now - timedelta(seconds=1)),
                event("3", now, "INFO"),
            ]
        )
        query = ErrorQuery(
            started_at=now - timedelta(minutes=1),
            ended_at=now + timedelta(seconds=1),
            severities=("ERROR",),
            limit=1,
        )

        first = asyncio.run(source.fetch(query))
        second = asyncio.run(source.fetch(query, first.next_cursor))

        self.assertEqual(["1"], [item.event_id for item in first.events])
        self.assertEqual(["2"], [item.event_id for item in second.events])
        self.assertIsNone(second.next_cursor)


if __name__ == "__main__":
    unittest.main()
