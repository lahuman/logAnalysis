from __future__ import annotations

import hashlib
import re

from log_analyzer.models import ErrorEvent, ParsedError

_UUID = re.compile(
    r"(?i)\b[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\b"
)
_LONG_HEX = re.compile(r"(?i)\b(?:0x)?[0-9a-f]{12,}\b")
_LONG_NUMBER = re.compile(r"\b\d{5,}\b")
_WHITESPACE = re.compile(r"\s+")


def normalize_message(message: str) -> str:
    normalized = _UUID.sub("<uuid>", message)
    normalized = _LONG_HEX.sub("<hex>", normalized)
    normalized = _LONG_NUMBER.sub("<number>", normalized)
    return _WHITESPACE.sub(" ", normalized).strip()


def make_fingerprint(event: ErrorEvent, parsed: ParsedError) -> str:
    frames: list[str] = []
    for frame in parsed.frames:
        if not frame.in_application:
            continue
        frames.append(
            ":".join(
                (
                    frame.class_name or "",
                    frame.function_name or "",
                    frame.file_path or "",
                    str(frame.line_number or ""),
                )
            )
        )
        if len(frames) == 3:
            break

    value = "\n".join(
        (
            event.service,
            event.environment or "",
            parsed.error_type or "",
            normalize_message(parsed.message),
            *frames,
        )
    )
    return hashlib.sha256(value.encode("utf-8")).hexdigest()
