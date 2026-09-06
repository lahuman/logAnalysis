"""Opt-in read-only test against a prepared Oracle fixture table/view."""
from __future__ import annotations

import os
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from log_analyzer.config import load_config, read_secret, require_secret
from log_analyzer.models import ErrorQuery
from log_analyzer.sources.oracle import OracleErrorSource


@unittest.skipUnless(os.environ.get("LOG_ANALYZER_TEST_ORACLE_CONFIG"),
                     "set LOG_ANALYZER_TEST_ORACLE_CONFIG to a prepared Oracle test configuration")
class LiveOracleTests(unittest.IsolatedAsyncioTestCase):
    async def test_readonly_healthcheck_paging_and_mapping(self):
        config = load_config(Path(os.environ["LOG_ANALYZER_TEST_ORACLE_CONFIG"]))
        self.assertEqual(config.error_source.type, "oracle")
        source = OracleErrorSource(
            config=config.error_source,
            username=require_secret(config.error_source.username_secret),
            password=require_secret(config.error_source.password_secret),
            wallet_password=read_secret(config.error_source.wallet_password_secret))
        now = datetime.now(UTC)
        query = ErrorQuery(now - timedelta(days=1), now, ("ERROR",), limit=2)
        events = []
        token = None
        try:
            await source.healthcheck()
            for _ in range(100):
                page = await source.fetch(query, token)
                events.extend(page.events)
                token = page.next_cursor
                if token is None:
                    break
            self.assertIsNone(token, "use a small fixture table with fewer than 200 matching rows")
            self.assertGreaterEqual(len(events), 3, "prepare at least 3 ERROR rows within the last 24 hours")
            self.assertEqual(len(events), len({event.event_id for event in events}))
            self.assertEqual([event.occurred_at for event in events], sorted(event.occurred_at for event in events))
            for event in events:
                self.assertFalse(event.attributes.get("mapping_error"))
                self.assertGreaterEqual(event.occurred_at, query.started_at)
                self.assertLessEqual(event.occurred_at, query.ended_at)
                self.assertEqual(event.severity, "ERROR")
        finally:
            await source.close()
