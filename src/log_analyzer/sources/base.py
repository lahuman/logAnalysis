from __future__ import annotations

from typing import Protocol

from log_analyzer.models import ErrorPage, ErrorQuery


class ErrorSource(Protocol):
    async def fetch(self, query: ErrorQuery, cursor: str | None = None) -> ErrorPage:
        ...

    async def healthcheck(self) -> None:
        ...

    async def close(self) -> None:
        ...
