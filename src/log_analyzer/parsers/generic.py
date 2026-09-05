"""Fallback parser that never interprets language-specific stack syntax."""

from __future__ import annotations

from log_analyzer.models import ErrorEvent, ParsedError


class GenericErrorParser:
    name = "generic"

    def can_parse(self, event: ErrorEvent) -> bool:
        return True

    def parse(self, event: ErrorEvent) -> ParsedError:
        message = (event.message or "").strip()
        if not message:
            stack = event.stack_trace or ""
            message = next((line.strip() for line in stack.splitlines() if line.strip()), "")

        error_type = event.error_type.strip() if event.error_type else None
        cause_chain = (
            (_format_exception(error_type, message),) if error_type else ()
        )
        language = event.language_hint.strip().lower() if event.language_hint else None
        return ParsedError(
            language=language,
            error_type=error_type,
            message=message,
            frames=(),
            cause_chain=cause_chain,
            parser_name=self.name,
            parse_warnings=("language_specific_stack_not_parsed",),
        )


def _format_exception(error_type: str, message: str) -> str:
    return f"{error_type}: {message}" if message else error_type
