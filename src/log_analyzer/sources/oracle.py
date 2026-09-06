"""Read a bounded Oracle SELECT result through a single asynchronous Thin cursor."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any
from uuid import uuid4

from ..config import OracleSourceConfig
from ..models import ErrorEvent, ErrorPage, ErrorQuery


class OracleSourceError(RuntimeError):
    """Sanitized source failure; raw database error messages are not exposed."""


_FIELDS = ("event_id", "occurred_at", "service", "severity", "message", "stack_trace",
           "error_type", "environment", "version", "git_commit", "trace_id")


class OracleErrorSource:
    def __init__(self, *, config: OracleSourceConfig, username: str, password: str,
                 wallet_password: str | None = None, max_text_characters: int = 100_000,
                 connection_factory: Any | None = None) -> None:
        if not username or not password or max_text_characters < 1:
            raise ValueError("Oracle credentials and a positive text limit are required")
        self._config = config
        self._username = username
        self._password = password
        self._wallet_password = wallet_password
        self._max_text_characters = max_text_characters
        self._connection_factory = connection_factory
        self._connection: Any | None = None
        self._cursor: Any | None = None
        self._query: ErrorQuery | None = None
        self._token: str | None = None
        self._page_number = 0
        value = config.timestamp_timezone
        minutes = (int(value[1:3]) * 60 + int(value[4:])) * (-1 if value[0] == "-" else 1)
        self._timezone = timezone(timedelta(minutes=minutes))

    async def _connect(self) -> Any:
        if self._connection is None:
            factory = self._connection_factory
            if factory is None:
                import oracledb
                factory = oracledb.connect_async
            options: dict[str, Any] = {
                "user": self._username, "password": self._password, "dsn": self._config.dsn,
                "tcp_connect_timeout": self._config.request_timeout_seconds,
                "ssl_server_dn_match": True,
            }
            if self._config.wallet_location:
                options["wallet_location"] = str(self._config.wallet_location)
            if self._wallet_password:
                options["wallet_password"] = self._wallet_password
            self._connection = await factory(**options)
            self._connection.call_timeout = int(self._config.request_timeout_seconds * 1000)
        return self._connection

    def _projection(self) -> str:
        columns = self._config.columns
        selections = []
        for name in _FIELDS:
            column = getattr(columns, name)
            expression = column or "NULL"
            if name == "occurred_at" and self._config.timestamp_type == "timestamp_tz":
                expression = f"SYS_EXTRACT_UTC({column})"
            selections.append(f"{expression} AS {name}")
        return ", ".join(selections)

    def _statement(self, query: ErrorQuery) -> tuple[str, dict[str, Any]]:
        if query.started_at.utcoffset() is None or query.ended_at.utcoffset() is None:
            raise OracleSourceError("Oracle query boundaries must include a timezone")
        columns = self._config.columns
        if self._config.timestamp_type == "timestamp_tz":
            convert = "TO_TIMESTAMP_TZ({bind}, 'YYYY-MM-DD\"T\"HH24:MI:SS.FF6TZH:TZM')"
            start = query.started_at.astimezone(UTC).isoformat(timespec="microseconds")
            end = query.ended_at.astimezone(UTC).isoformat(timespec="microseconds")
        else:
            convert = "TO_TIMESTAMP({bind}, 'YYYY-MM-DD\"T\"HH24:MI:SS.FF6')"
            start = query.started_at.astimezone(self._timezone).replace(tzinfo=None).isoformat(timespec="microseconds")
            end = query.ended_at.astimezone(self._timezone).replace(tzinfo=None).isoformat(timespec="microseconds")
        parameters: dict[str, Any] = {"started_at": start, "ended_at": end}
        where = (f"{columns.occurred_at} >= {convert.format(bind=':started_at')} AND "
                 f"{columns.occurred_at} <= {convert.format(bind=':ended_at')}")
        if query.severities:
            binds = []
            for index, severity in enumerate(query.severities):
                name = f"severity_{index}"
                binds.append(f":{name}")
                parameters[name] = severity
            where += f" AND {columns.severity} IN ({', '.join(binds)})"
        sql = (f"SELECT {self._projection()} FROM {self._config.table} WHERE {where} "
               f"ORDER BY {columns.occurred_at}, {columns.event_id}")
        return sql, parameters

    async def healthcheck(self) -> None:
        cursor = None
        try:
            connection = await self._connect()
            cursor = connection.cursor()
            await cursor.execute(f"SELECT {self._projection()} FROM {self._config.table} WHERE 1 = 0")
        except Exception as exc:
            raise OracleSourceError("Oracle connection or mapped columns could not be verified") from exc
        finally:
            if cursor is not None:
                cursor.close()

    async def fetch(self, query: ErrorQuery, cursor: str | None = None) -> ErrorPage:
        if cursor is not None and (cursor != self._token or query != self._query or self._cursor is None):
            raise OracleSourceError("Oracle page cursor is stale or belongs to a different query")
        try:
            if cursor is None:
                self._close_cursor()
                connection = await self._connect()
                self._cursor = connection.cursor()
                self._cursor.arraysize = query.limit
                self._cursor.prefetchrows = query.limit
                sql, parameters = self._statement(query)
                # Preserve Oracle statement-level read consistency across every page.
                await self._cursor.execute(sql, parameters, fetch_lobs=True, fetch_decimals=True)
                self._query = query
                self._token = uuid4().hex
                self._page_number = 0
            rows = await self._cursor.fetchmany(query.limit)
            events = tuple([await self._to_event(row, query) for row in rows])
            if len(rows) < query.limit:
                self._close_cursor()
                return ErrorPage(events=events)
            self._page_number += 1
            self._token = f"{self._token.rsplit(':', 1)[0]}:{self._page_number}"
            return ErrorPage(events=events, next_cursor=self._token)
        except BaseException as exc:
            self._close_cursor()
            if not isinstance(exc, Exception) or isinstance(exc, OracleSourceError):
                raise
            raise OracleSourceError("Oracle log query or fetch failed; checkpoint must not advance") from exc

    async def _text(self, value: Any, limit: int) -> str | None:
        if value is None:
            return None
        if hasattr(value, "read"):
            value = await value.read(offset=1, amount=limit)
        if not isinstance(value, str):
            raise ValueError("mapped text column must be VARCHAR2, CLOB or NCLOB")
        return value[:limit]

    async def _to_event(self, row: Any, query: ErrorQuery) -> ErrorEvent:
        if len(row) != len(_FIELDS):
            raise OracleSourceError("Oracle result does not match the configured projection")
        values = dict(zip(_FIELDS, row))
        identity = values.pop("event_id")
        if isinstance(identity, Decimal) and identity.is_finite() and identity == identity.to_integral_value():
            identity = int(identity)
        if isinstance(identity, bool) or not isinstance(identity, (str, int)) or not str(identity).strip():
            raise OracleSourceError("Oracle event ID must be a nonempty string or integer")
        event_id = str(identity)
        occurred_at = query.started_at
        try:
            timestamp = values.pop("occurred_at")
            if not isinstance(timestamp, datetime):
                raise ValueError("mapped occurrence must be a TIMESTAMP or DATE")
            if timestamp.utcoffset() is None:
                zone = UTC if self._config.timestamp_type == "timestamp_tz" else self._timezone
                timestamp = timestamp.replace(tzinfo=zone)
            occurred_at = timestamp.astimezone(UTC)
            mapped = {}
            for name, value in values.items():
                limit = self._max_text_characters if name in {"message", "stack_trace"} else 4096
                mapped[name] = await self._text(value, limit)
            if not mapped["service"] or not mapped["severity"] or mapped["message"] is None:
                raise ValueError("required log columns are empty")
            return ErrorEvent(source_name=self._config.name, event_id=event_id, occurred_at=occurred_at,
                              language_hint="java", **mapped)
        except (ValueError, TypeError):
            return ErrorEvent(source_name=self._config.name, event_id=event_id, occurred_at=occurred_at,
                              service="__unmapped__", severity="ERROR", message="Oracle row could not be mapped to an error event.",
                              error_type="OracleDataError", attributes={"mapping_error": True})

    def _close_cursor(self) -> None:
        active, self._cursor = self._cursor, None
        self._token = None
        self._query = None
        if active is not None:
            active.close()

    async def close(self) -> None:
        try:
            self._close_cursor()
        finally:
            connection, self._connection = self._connection, None
            if connection is not None:
                await connection.close()
