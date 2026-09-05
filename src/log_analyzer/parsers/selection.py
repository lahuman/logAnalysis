"""Parser selection with a safe generic fallback."""

from __future__ import annotations

from typing import Protocol, Sequence

from log_analyzer.models import ErrorEvent, ParsedError


class ErrorParser(Protocol):
    name: str

    def can_parse(self, event: ErrorEvent) -> bool:
        ...

    def parse(self, event: ErrorEvent) -> ParsedError:
        ...


def parse_event(event: ErrorEvent, parsers: Sequence[ErrorParser]) -> ParsedError:
    """Parse an event with the first matching parser.

    A parser rejecting an event is normal. A parser that accepted the event but
    failed is skipped so that the generic parser can keep the batch moving.
    """

    errors: list[str] = []
    for parser in parsers:
        if not parser.can_parse(event):
            continue
        try:
            parsed = parser.parse(event)
        except Exception as exc:
            errors.append(f"{parser.name}: {exc}")
            continue
        if not errors:
            return parsed
        return ParsedError(
            language=parsed.language,
            error_type=parsed.error_type,
            message=parsed.message,
            frames=parsed.frames,
            cause_chain=parsed.cause_chain,
            parser_name=parsed.parser_name,
            parse_warnings=parsed.parse_warnings
            + tuple(f"parser_failed: {error}" for error in errors),
        )
    raise ValueError("no parser accepted the error event")
