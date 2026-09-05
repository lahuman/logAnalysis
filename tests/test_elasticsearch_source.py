from __future__ import annotations

import asyncio
import unittest
from datetime import UTC, datetime, timedelta

from log_analyzer.models import ErrorQuery
from log_analyzer.sources.elasticsearch import ElasticsearchErrorSource


class FakeElasticsearchClient:
    def __init__(self) -> None:
        self.search_calls: list[dict] = []
        self.closed_pits: list[str] = []

    async def info(self) -> dict:
        return {"version": {"number": "8.0.0"}}

    async def open_point_in_time(self, **kwargs) -> dict:
        self.open_kwargs = kwargs
        return {"id": "pit-1"}

    async def search(self, **kwargs) -> dict:
        self.search_calls.append(kwargs)
        if kwargs.get("size") == 0:
            return {"pit_id": "pit-refreshed", "hits": {"hits": []}}
        if len(self.search_calls) == 1:
            return {
                "pit_id": "pit-2",
                "hits": {
                    "hits": [
                        {
                            "_id": "doc-1",
                            "_index": "logs-1",
                            "sort": ["2026-01-01T00:00:00Z", 1],
                            "_source": {
                                "@timestamp": "2026-01-01T00:00:00Z",
                                "event": {"id": "event-1"},
                                "service": {
                                    "name": "orders",
                                    "environment": "test",
                                },
                                "log": {
                                    "level": "ERROR",
                                    "original": "password=must-not-be-copied",
                                },
                                "error": {
                                    "type": "java.lang.IllegalStateException",
                                    "message": "failed",
                                    "stack_trace": "java.lang.IllegalStateException: failed",
                                },
                                "git": {"commit": {"id": "abc123"}},
                            },
                        }
                    ]
                }
            }
        return {"hits": {"hits": []}}

    async def close_point_in_time(self, **kwargs) -> None:
        self.closed_pits.append(kwargs["id"])


class ElasticsearchErrorSourceTests(unittest.TestCase):
    def test_pit_paging_and_mapping(self) -> None:
        client = FakeElasticsearchClient()
        source = ElasticsearchErrorSource(
            url="https://example.invalid",
            index="logs-*",
            client=client,
        )
        now = datetime(2026, 1, 1, tzinfo=UTC)
        query = ErrorQuery(
            started_at=now - timedelta(hours=1),
            ended_at=now + timedelta(hours=1),
            severities=("ERROR",),
            limit=1,
        )

        first = asyncio.run(source.fetch(query))
        second = asyncio.run(source.fetch(query, first.next_cursor))

        self.assertEqual("event-1", first.events[0].event_id)
        self.assertEqual("orders", first.events[0].service)
        self.assertEqual("abc123", first.events[0].git_commit)
        self.assertIsNone(first.events[0].raw_log)
        self.assertIsNotNone(first.next_cursor)
        self.assertIsNone(second.next_cursor)
        self.assertEqual("pit-2", client.closed_pits[-1])
        self.assertEqual("pit-2", client.search_calls[1]["pit"]["id"])
        self.assertEqual(
            ["2026-01-01T00:00:00Z", 1],
            client.search_calls[1]["search_after"],
        )

    def test_refreshes_pit_cursor_and_uses_latest_pit_id(self) -> None:
        client = FakeElasticsearchClient()
        source = ElasticsearchErrorSource(
            url="https://example.invalid", index="logs-*", client=client
        )
        now = datetime(2026, 1, 1, tzinfo=UTC)
        query = ErrorQuery(
            started_at=now - timedelta(hours=1),
            ended_at=now + timedelta(hours=1),
            limit=1,
        )

        first = asyncio.run(source.fetch(query))
        refreshed = asyncio.run(source.refresh_cursor(first.next_cursor))
        asyncio.run(source.fetch(query, refreshed))

        self.assertEqual(0, client.search_calls[1]["size"])
        self.assertEqual("pit-refreshed", client.search_calls[2]["pit"]["id"])
        self.assertEqual("pit-refreshed", client.closed_pits[-1])

    def test_invalid_document_becomes_sanitized_event_with_index_scoped_id(self) -> None:
        class InvalidClient(FakeElasticsearchClient):
            async def search(inner_self, **kwargs) -> dict:
                inner_self.search_calls.append(kwargs)
                return {
                    "pit_id": "pit-2",
                    "hits": {
                        "hits": [
                            {
                                "_id": "same-id",
                                "_index": "logs-2026.01",
                                "sort": ["2026-01-01T00:00:00Z", 1],
                                "_source": {"@timestamp": "2026-01-01T00:00:00Z"},
                            }
                        ]
                    },
                }

        client = InvalidClient()
        source = ElasticsearchErrorSource(
            url="https://example.invalid", index="logs-*", client=client
        )
        now = datetime(2026, 1, 1, tzinfo=UTC)
        page = asyncio.run(
            source.fetch(
                ErrorQuery(
                    started_at=now - timedelta(hours=1),
                    ended_at=now + timedelta(hours=1),
                    limit=2,
                )
            )
        )

        self.assertEqual("logs-2026.01:same-id", page.events[0].event_id)
        self.assertEqual("__unmapped__", page.events[0].service)
        self.assertIsNone(page.events[0].raw_log)


if __name__ == "__main__":
    unittest.main()
