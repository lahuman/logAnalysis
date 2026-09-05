from __future__ import annotations

import unittest

from pydantic import ValidationError

from log_analyzer.analysis.models import (
    AnalysisRequest,
    AnalysisResult,
    Evidence,
    RecommendedFix,
    RootCause,
    validated_evidence,
)


def valid_request(**changes: object) -> AnalysisRequest:
    values: dict[str, object] = {
        "service": "orders",
        "environment": "production",
        "version": "1.2.3",
        "fingerprint": "fingerprint",
        "error_type": "java.lang.NullPointerException",
        "message": "value was null",
        "stack_trace": "stack",
        "parse_warnings": (),
        "git_commit": "b" * 40,
        "source_path": "src/main/java/com/example/OrderService.java",
        "line_number": 42,
        "function_name": "load",
        "class_name": "com.example.OrderService",
        "source_code": "41: value = repo.get();\n42: return value.id();",
        "context_start_line": 41,
        "context_end_line": 42,
    }
    values.update(changes)
    return AnalysisRequest(**values)


class AnalysisValidationTests(unittest.TestCase):
    def test_request_rejects_traversal_and_out_of_context_line(self) -> None:
        with self.assertRaises(ValidationError):
            valid_request(source_path="../../etc/passwd")
        with self.assertRaises(ValidationError):
            valid_request(line_number=100)

    def test_result_rejects_extra_fields_and_invalid_confidence(self) -> None:
        with self.assertRaises(ValidationError):
            AnalysisResult.model_validate(
                {
                    "summary": "summary",
                    "root_causes": [
                        {
                            "cause": "cause",
                            "confidence": 2.0,
                            "evidence": [],
                            "unexpected": True,
                        }
                    ],
                    "recommended_fixes": [],
                    "validation_steps": [],
                    "unknowns": [],
                }
            )

    def test_evidence_outside_supplied_file_or_lines_is_removed(self) -> None:
        path = "src/main/java/com/example/OrderService.java"
        result = AnalysisResult(
            summary="summary",
            root_causes=[
                RootCause(
                    cause="cause",
                    confidence=0.8,
                    evidence=[
                        Evidence(file=path, line=42, description="valid"),
                        Evidence(file=path, line=100, description="outside"),
                        Evidence(file="src/Other.java", line=42, description="other"),
                        Evidence(
                            file="src/main/java/com/example/../example/OrderService.java",
                            line=42,
                            description="traversal",
                        ),
                    ],
                )
            ],
            recommended_fixes=[
                RecommendedFix(
                    description="fix",
                    files=[path, "src/Other.java"],
                    risk="low",
                )
            ],
            validation_steps=["test"],
            unknowns=[],
        )

        validated = validated_evidence(result, valid_request())

        self.assertEqual(["valid"], [item.description for item in validated.root_causes[0].evidence])
        self.assertEqual([path], validated.recommended_fixes[0].files)

    def test_json_schema_forbids_unknown_properties(self) -> None:
        schema = AnalysisResult.model_json_schema()
        self.assertFalse(schema["additionalProperties"])
        for definition in schema["$defs"].values():
            self.assertFalse(definition["additionalProperties"])


if __name__ == "__main__":
    unittest.main()
