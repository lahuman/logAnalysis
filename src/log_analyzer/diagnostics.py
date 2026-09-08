"""Structured runtime diagnostics without source text or local variable values."""

from __future__ import annotations

from datetime import UTC, datetime
import json
from pathlib import Path
import sys
from typing import Any

from pydantic import ValidationError

from .analysis.redact import RedactionError, SecretRedactor
from .errors import ConfigError


def _safe_text(value: str, redactor: SecretRedactor) -> str:
    try:
        return redactor.redact_text(value)[:1_500]
    except Exception:
        return "[REDACTION_FAILED]"


def _message(error: BaseException, redactor: SecretRedactor) -> str:
    if isinstance(error, ValidationError):
        return "Data failed schema validation; see validation_errors."
    if isinstance(error, ConfigError):
        return _safe_text(error.diagnostic_message, redactor)
    if isinstance(error, RedactionError):
        return "Analysis data could not be safely redacted."
    if isinstance(error, UnicodeError):
        return "Text could not be decoded or encoded; check the configured encoding."
    try:
        return _safe_text(str(error), redactor)
    except Exception:
        return "[ERROR_MESSAGE_UNAVAILABLE]"


def error_details(
    error: BaseException, *, redactor: SecretRedactor | None = None
) -> dict[str, Any]:
    """Keep explicit/implicit exception chains and frame coordinates, never locals."""

    redactor = redactor or SecretRedactor()
    chain: list[dict[str, Any]] = []
    seen: set[int] = set()
    current: BaseException | None = error
    hide_message = False
    while current is not None and id(current) not in seen and len(chain) < 8:
        seen.add(id(current))
        frames = []
        trace = current.__traceback__
        while trace is not None:
            code = trace.tb_frame.f_code
            frames.append({
                "file": _safe_text(Path(code.co_filename).as_posix(), redactor),
                "line": trace.tb_lineno,
                "function": code.co_name,
            })
            trace = trace.tb_next
        item: dict[str, Any] = {
            "error_type": type(current).__name__,
            "message": "[VALIDATION_DETAIL_OMITTED]" if hide_message else _message(current, redactor),
            "location": frames[-1] if frames else None,
            "frames": frames[-30:],
        }
        if isinstance(current, ValidationError):
            item["validation_errors"] = [
                {
                    "field": _safe_text(".".join(map(str, detail["loc"])), redactor),
                    "type": detail["type"],
                }
                for detail in current.errors(include_input=False, include_context=False, include_url=False)[:20]
            ]
            hide_message = True
        if chain:
            item["relation"] = relation
        chain.append(item)
        if current.__cause__ is not None:
            current = current.__cause__
            relation = "cause"
        elif not current.__suppress_context__:
            current = current.__context__
            relation = "context"
        else:
            current = None
    return {
        "error_type": type(error).__name__,
        "error_message": chain[0]["message"],
        "error_location": chain[0]["location"],
        "exception_chain": chain,
        "exception_chain_truncated": current is not None,
    }


def error_summary(error: BaseException, redactor: SecretRedactor) -> str:
    """Compact diagnostic for the existing 2,000-character last_error column."""

    details = error_details(error, redactor=redactor)
    parts = []
    for item in details["exception_chain"]:
        location = item["location"]
        where = (
            f" at {location['file']}:{location['line']} in {location['function']}"
            if location else ""
        )
        message = item["message"]
        if "validation_errors" in item:
            message = "Schema validation failed: " + ", ".join(
                f"{detail['field']} ({detail['type']})" for detail in item["validation_errors"]
            )
        parts.append(f"{item['error_type']}{where}: {message}")
    return " <- ".join(parts)[:2_000]


def emit(
    level: str,
    event: str,
    *,
    error: BaseException | None = None,
    redactor: SecretRedactor | None = None,
    **fields: Any,
) -> None:
    redactor = redactor or SecretRedactor()

    def sanitize(value: Any) -> Any:
        if isinstance(value, (str, Path)):
            return _safe_text(str(value), redactor)
        if isinstance(value, dict):
            return {key: sanitize(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [sanitize(item) for item in value]
        return value

    record = {
        "timestamp": datetime.now(UTC).isoformat(),
        "level": level,
        "event": event,
        **sanitize(fields),
    }
    caller = sys._getframe(1)
    record["log_location"] = {
        "file": _safe_text(Path(caller.f_code.co_filename).as_posix(), redactor),
        "line": caller.f_lineno,
        "function": caller.f_code.co_name,
    }
    del caller
    if error is not None:
        record.update(error_details(error, redactor=redactor))
    print(
        json.dumps(record, ensure_ascii=True, separators=(",", ":"), sort_keys=True, default=str),
        file=sys.stderr,
        flush=True,
    )
