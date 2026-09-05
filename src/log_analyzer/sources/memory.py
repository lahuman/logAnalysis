from __future__ import annotations

from log_analyzer.models import ErrorEvent, ErrorPage, ErrorQuery


class InMemoryErrorSource:
    def __init__(self, events: list[ErrorEvent] | tuple[ErrorEvent, ...]) -> None:
        self._events = tuple(sorted(events, key=lambda item: (item.occurred_at, item.event_id)))

    async def fetch(self, query: ErrorQuery, cursor: str | None = None) -> ErrorPage:
        offset = int(cursor) if cursor else 0
        severities = {severity.upper() for severity in query.severities}
        matching = [
            event
            for event in self._events
            if query.started_at <= event.occurred_at <= query.ended_at
            and (not severities or event.severity.upper() in severities)
        ]
        events = matching[offset : offset + query.limit]
        next_offset = offset + len(events)
        next_cursor = str(next_offset) if next_offset < len(matching) else None
        return ErrorPage(events=tuple(events), next_cursor=next_cursor)

    async def healthcheck(self) -> None:
        return None

    async def close(self) -> None:
        return None
