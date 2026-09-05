from __future__ import annotations

import base64
import hashlib
import json
from datetime import UTC, datetime
from typing import Any

from log_analyzer.models import ErrorEvent, ErrorPage, ErrorQuery


class ElasticsearchDataError(ValueError):
    pass


class ElasticsearchErrorSource:
    def __init__(
        self,
        *,
        url: str,
        index: str,
        username: str | None = None,
        password: str | None = None,
        api_key: str | None = None,
        ca_certs: str | None = None,
        verify_tls: bool = True,
        request_timeout: float = 30,
        pit_keep_alive: str = "2m",
        pit_refresh_interval_seconds: float = 30.0,
        source_name: str = "elasticsearch",
        client: Any | None = None,
    ) -> None:
        self._index = index
        self._pit_keep_alive = pit_keep_alive
        if pit_refresh_interval_seconds <= 0:
            raise ValueError("pit_refresh_interval_seconds must be positive")
        self.cursor_refresh_interval_seconds = pit_refresh_interval_seconds
        self._source_name = source_name
        self._owns_client = client is None
        if client is not None:
            self._client = client
            return

        from elasticsearch import AsyncElasticsearch

        options: dict[str, Any] = {
            "hosts": [url],
            "request_timeout": request_timeout,
            "verify_certs": verify_tls,
            "max_retries": 2,
            "retry_on_timeout": True,
            "retry_on_status": (429, 500, 502, 503, 504),
        }
        if api_key:
            options["api_key"] = api_key
        elif username is not None or password is not None:
            if not username or password is None:
                raise ValueError("Elasticsearch username and password must be provided together")
            options["basic_auth"] = (username, password)
        if ca_certs:
            options["ca_certs"] = ca_certs
        self._client = AsyncElasticsearch(**options)

    async def healthcheck(self) -> None:
        await self._client.info()

    async def close(self) -> None:
        if self._owns_client:
            await self._client.close()

    async def fetch(self, query: ErrorQuery, cursor: str | None = None) -> ErrorPage:
        state = _decode_cursor(cursor) if cursor else None
        if state is None:
            response = await self._client.open_point_in_time(
                index=self._index,
                keep_alive=self._pit_keep_alive,
            )
            pit_id = str(response["id"])
            search_after = None
        else:
            pit_id = state["pit_id"]
            search_after = state["search_after"]

        filters: list[dict[str, Any]] = [
            {
                "range": {
                    "@timestamp": {
                        "gte": query.started_at.isoformat(),
                        "lte": query.ended_at.isoformat(),
                    }
                }
            }
        ]
        if query.severities:
            filters.append({"terms": {"log.level": list(query.severities)}})

        parameters: dict[str, Any] = {
            "pit": {"id": pit_id, "keep_alive": self._pit_keep_alive},
            "query": {"bool": {"filter": filters}},
            "sort": [
                {
                    "@timestamp": {
                        "order": "asc",
                        "format": "strict_date_optional_time_nanos",
                        "numeric_type": "date_nanos",
                    }
                },
                {"_shard_doc": "asc"},
            ],
            "size": query.limit,
            "track_total_hits": False,
        }
        if search_after is not None:
            parameters["search_after"] = search_after

        response = await self._client.search(**parameters)
        response_pit_id = response.get("pit_id")
        if response_pit_id is not None:
            pit_id = str(response_pit_id)
        hits = list(response.get("hits", {}).get("hits", ()))
        events: list[ErrorEvent] = []
        for candidate in hits:
            hit = candidate if isinstance(candidate, dict) else {}
            try:
                events.append(self._to_event(hit))
            except (ElasticsearchDataError, TypeError, ValueError):
                events.append(self._invalid_event(hit, query))

        if len(hits) < query.limit:
            await self._close_pit(pit_id)
            return ErrorPage(events=tuple(events), next_cursor=None)

        last_sort = hits[-1].get("sort")
        if not isinstance(last_sort, list):
            await self._close_pit(pit_id)
            raise ElasticsearchDataError("Elasticsearch hit is missing the search_after sort values")
        return ErrorPage(
            events=tuple(events),
            next_cursor=_encode_cursor(pit_id, last_sort),
        )

    async def refresh_cursor(self, cursor: str) -> str:
        """Refresh an active PIT while a slow analysis page is being processed."""

        state = _decode_cursor(cursor)
        response = await self._client.search(
            pit={"id": state["pit_id"], "keep_alive": self._pit_keep_alive},
            size=0,
            track_total_hits=False,
        )
        pit_id = str(response.get("pit_id") or state["pit_id"])
        return _encode_cursor(pit_id, state["search_after"])

    async def _close_pit(self, pit_id: str) -> None:
        try:
            await self._client.close_point_in_time(id=pit_id)
        except Exception:
            # PITs expire automatically. A close failure must not discard processed data.
            return

    def _to_event(self, hit: dict[str, Any]) -> ErrorEvent:
        document = hit.get("_source")
        if not isinstance(document, dict):
            raise ElasticsearchDataError("Elasticsearch hit has no _source object")

        occurred_at = _as_datetime(_field(document, "@timestamp"))
        event_id = _optional_text(_field(document, "event.id"))
        if not event_id:
            index = _optional_text(hit.get("_index")) or "unknown-index"
            document_id = _optional_text(hit.get("_id"))
            event_id = f"{index}:{document_id}" if document_id else None
        service = _optional_text(_field(document, "service.name"))
        message = _optional_text(_field(document, "error.message")) or _optional_text(
            _field(document, "message")
        )
        if not event_id or not service or message is None:
            raise ElasticsearchDataError(
                "Elasticsearch hit requires event.id/_id, service.name and error.message/message"
            )

        attributes: dict[str, object] = {
            "index": str(hit.get("_index", "")),
        }
        code_path = _optional_text(_field(document, "code.filepath"))
        code_line = _field(document, "code.lineno")
        if code_path:
            attributes["code.filepath"] = code_path
        if isinstance(code_line, int):
            attributes["code.lineno"] = code_line

        return ErrorEvent(
            source_name=self._source_name,
            event_id=event_id,
            occurred_at=occurred_at,
            service=service,
            severity=_optional_text(_field(document, "log.level")) or "ERROR",
            message=message,
            environment=_optional_text(_field(document, "service.environment")),
            version=_optional_text(_field(document, "service.version")),
            git_commit=_optional_text(_field(document, "git.commit.id")),
            error_type=_optional_text(_field(document, "error.type")),
            stack_trace=_optional_text(_field(document, "error.stack_trace")),
            raw_log=None,
            language_hint=_optional_text(_field(document, "service.language.name")),
            runtime_hint=_optional_text(_field(document, "service.runtime.name")),
            trace_id=_optional_text(_field(document, "trace.id")),
            attributes=attributes,
        )

    def _invalid_event(self, hit: dict[str, Any], query: ErrorQuery) -> ErrorEvent:
        document = hit.get("_source")
        occurred_at = query.started_at
        if isinstance(document, dict):
            try:
                occurred_at = _as_datetime(_field(document, "@timestamp"))
            except ElasticsearchDataError:
                pass
        index = _optional_text(hit.get("_index")) or "unknown-index"
        document_id = _optional_text(hit.get("_id"))
        if document_id:
            event_id = f"{index}:{document_id}"
        else:
            stable = json.dumps(hit.get("sort", ()), separators=(",", ":"), default=str)
            digest = hashlib.sha256(stable.encode("utf-8")).hexdigest()[:20]
            event_id = f"{index}:invalid-{digest}"
        return ErrorEvent(
            source_name=self._source_name,
            event_id=event_id,
            occurred_at=occurred_at,
            service="__unmapped__",
            severity="ERROR",
            message="Elasticsearch document could not be mapped to the canonical error schema.",
            error_type="ElasticsearchDataError",
            raw_log=None,
            attributes={"index": index, "mapping_error": True},
        )


def _field(document: dict[str, Any], path: str) -> Any:
    if path in document:
        return document[path]
    value: Any = document
    for part in path.split("."):
        if not isinstance(value, dict) or part not in value:
            return None
        value = value[part]
    return value


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)


def _as_datetime(value: Any) -> datetime:
    if isinstance(value, datetime):
        result = value
    elif isinstance(value, str):
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    else:
        raise ElasticsearchDataError("Elasticsearch hit has an invalid @timestamp")
    if result.tzinfo is None:
        result = result.replace(tzinfo=UTC)
    return result.astimezone(UTC)


def _encode_cursor(pit_id: str, search_after: list[Any]) -> str:
    raw = json.dumps(
        {"pit_id": pit_id, "search_after": search_after},
        separators=(",", ":"),
    ).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii")


def _decode_cursor(cursor: str) -> dict[str, Any]:
    try:
        value = json.loads(base64.urlsafe_b64decode(cursor.encode("ascii")))
    except (ValueError, TypeError, json.JSONDecodeError) as exc:
        raise ElasticsearchDataError("Invalid Elasticsearch cursor") from exc
    if (
        not isinstance(value, dict)
        or not isinstance(value.get("pit_id"), str)
        or not isinstance(value.get("search_after"), list)
    ):
        raise ElasticsearchDataError("Invalid Elasticsearch cursor")
    return value
