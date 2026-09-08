from __future__ import annotations

import contextlib
import io
import json
import unittest
from unittest.mock import patch

from pydantic import BaseModel, ValidationError, field_validator

from log_analyzer.analysis.redact import SecretRedactor
from log_analyzer.diagnostics import emit, error_details, error_summary


class DiagnosticsTests(unittest.TestCase):
    def test_frames_have_exact_coordinates_without_source_or_locals(self) -> None:
        try:
            confidential = "not-for-diagnostics"
            raise RuntimeError("operation failed")
        except RuntimeError as exc:
            details = error_details(exc)
            expected_line = exc.__traceback__.tb_lineno
        serialized = json.dumps(details)
        self.assertNotIn(confidential, serialized)
        self.assertEqual(expected_line, details["error_location"]["line"])
        self.assertEqual("test_frames_have_exact_coordinates_without_source_or_locals", details["error_location"]["function"])
        self.assertEqual({"file", "line", "function"}, set(details["error_location"]))

    def test_implicit_context_and_suppressed_context(self) -> None:
        for suppress in (False, True):
            try:
                try:
                    raise OSError("root failure")
                except OSError:
                    if suppress:
                        raise RuntimeError("wrapper") from None
                    raise RuntimeError("wrapper")
            except RuntimeError as exc:
                chain = error_details(exc)["exception_chain"]
            self.assertEqual(1 if suppress else 2, len(chain))
            if not suppress:
                self.assertEqual("context", chain[1]["relation"])

    def test_exception_chain_cycles_are_bounded(self) -> None:
        error = RuntimeError("cycle")
        error.__cause__ = error
        details = error_details(error)
        self.assertEqual(1, len(details["exception_chain"]))
        self.assertTrue(details["exception_chain_truncated"])

    def test_validation_omits_input(self) -> None:
        class Input(BaseModel):
            count: int

        try:
            Input(count="private-input")
        except ValidationError as exc:
            details = error_details(exc)
        self.assertNotIn("private-input", json.dumps(details))
        self.assertEqual([{"field": "count", "type": "int_parsing"}], details["exception_chain"][0]["validation_errors"])

    def test_custom_validation_message_and_context_do_not_leak_into_logs_or_db(self) -> None:
        class Input(BaseModel):
            value: str

            @field_validator("value")
            @classmethod
            def reject(cls, value: str) -> str:
                raise ValueError(f"invalid input: {value}")

        try:
            Input(value="private-value")
        except ValidationError as exc:
            details = error_details(exc)
            summary = error_summary(exc, SecretRedactor())
        self.assertNotIn("private-value", json.dumps(details))
        self.assertNotIn("private-value", summary)
        self.assertIn("value (value_error)", summary)

    def test_redaction_applies_to_causes_and_context_fields_and_json_stays_one_line(self) -> None:
        output = io.StringIO()
        try:
            try:
                raise OSError("password=hidden-password\nAuthorization: Bearer hidden-token")
            except OSError as exc:
                raise RuntimeError("failed") from exc
        except RuntimeError as exc:
            with contextlib.redirect_stderr(output):
                emit("error", "operation_failed", error=exc, event_id="api_key=hidden-id")
        self.assertEqual(1, len(output.getvalue().splitlines()))
        for secret in ("hidden-password", "hidden-token", "hidden-id"):
            self.assertNotIn(secret, output.getvalue())
        record = json.loads(output.getvalue())
        self.assertIn("timestamp", record)
        self.assertTrue(record["timestamp"].endswith("+00:00"))
        self.assertEqual("test_redaction_applies_to_causes_and_context_fields_and_json_stays_one_line", record["log_location"]["function"])

    def test_redaction_failure_does_not_hide_original_type_or_leak_message(self) -> None:
        redactor = SecretRedactor()
        with patch.object(redactor, "redact_text", side_effect=RuntimeError("failed")):
            details = error_details(OSError("private-value"), redactor=redactor)
        self.assertEqual("OSError", details["error_type"])
        self.assertNotIn("private-value", json.dumps(details))

    def test_stored_error_retains_location_and_cause(self) -> None:
        try:
            try:
                raise OSError("password=private-value")
            except OSError as exc:
                raise RuntimeError("request failed") from exc
        except RuntimeError as exc:
            summary = error_summary(exc, SecretRedactor())
        self.assertIn("test_diagnostics.py:", summary)
        self.assertIn("OSError", summary)
        self.assertNotIn("private-value", summary)
        self.assertLessEqual(len(summary), 2_000)


if __name__ == "__main__":
    unittest.main()
