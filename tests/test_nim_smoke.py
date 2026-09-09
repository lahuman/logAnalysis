from __future__ import annotations

import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import httpx

from log_analyzer import nim_smoke
from log_analyzer.analysis import InvalidResponseError, NvidiaNimAnalyzer
from log_analyzer.config import OpenAIConfig
from test_nvidia_nim import response_for
from test_openai_responses import result_payload


class NimSmokeTests(unittest.IsolatedAsyncioTestCase):
    async def test_synthetic_context_generates_validated_markdown(self) -> None:
        request = nim_smoke.smoke_request()
        payload = result_payload(evidence_line=4, evidence_file=request.source_path)
        captured = []

        def handler(http_request):
            captured.append(json.loads(http_request.content))
            return httpx.Response(200, json=response_for(payload))

        config = OpenAIConfig(provider="nvidia_nim", model="test-model")
        with tempfile.TemporaryDirectory() as directory:
            credentials = Path(directory) / "credentials"
            credentials.mkdir()
            (credentials / "NVIDIA_API_KEY").write_text("test-secret", encoding="utf-8")
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                analyzer = NvidiaNimAnalyzer(api_key="test-secret", model="test-model", client=client)
                with (
                    patch.dict("os.environ", {}, clear=True),
                    patch.object(nim_smoke, "NvidiaNimAnalyzer", return_value=analyzer),
                ):
                    report = await nim_smoke.run_smoke(config, Path(directory) / "reports", credentials)
            contents = report.read_text(encoding="utf-8")
            self.assertIn("처리 상태: 분석 완료", contents)
            self.assertIn("**오류 수준: 중간**", contents)
            self.assertIn("권장 처리 시점:", contents)
            self.assertIn("OrderService.java:4", contents)
            self.assertIn("synthetic", contents)
            self.assertNotIn("test-secret", contents)
            self.assertFalse((Path(directory) / "state.db").exists())
        self.assertEqual(1, len(captured))
        self.assertIn("synthetic-test", captured[0]["messages"][1]["content"])

    async def test_empty_analysis_does_not_write_success_report(self) -> None:
        from log_analyzer.analysis.models import AnalysisResult
        from unittest.mock import AsyncMock
        config = OpenAIConfig(provider="nvidia_nim", model="test-model")
        result = AnalysisResult(summary="No evidence", root_causes=[], recommended_fixes=[], validation_steps=[], unknowns=[])
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "reports"
            with (
                patch.object(nim_smoke, "require_secret", return_value="test-key"),
                patch.object(nim_smoke, "NvidiaNimAnalyzer", return_value=AsyncMock(analyze=AsyncMock(return_value=result))),
            ):
                with self.assertRaises(InvalidResponseError):
                    await nim_smoke.run_smoke(config, destination)
            self.assertFalse(destination.exists())

    def test_missing_key_fails_without_network_or_secret_output(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "nim.toml"
            config.write_text('[openai]\nprovider="nvidia_nim"\nmodel="test-model"', encoding="utf-8")
            output = io.StringIO()
            with (
                patch.dict("os.environ", {}, clear=True),
                patch.object(nim_smoke, "NvidiaNimAnalyzer") as analyzer,
                contextlib.redirect_stderr(output),
            ):
                self.assertEqual(2, nim_smoke.main(["--config", str(config)]))
            analyzer.assert_not_called()
            self.assertEqual("nim_smoke_configuration_invalid", json.loads(output.getvalue())["event"])
            record = json.loads(output.getvalue())
            self.assertIn("NVIDIA_API_KEY", record["error_message"])
            self.assertEqual("require_secret", record["error_location"]["function"])

    def test_io_failure_has_reason_and_location(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = io.StringIO()
            with contextlib.redirect_stderr(output):
                status = nim_smoke.main(["--config", str(Path(directory) / "missing.toml")])
        self.assertEqual(3, status)
        record = json.loads(output.getvalue())
        self.assertEqual("nim_smoke_io_failed", record["event"])
        self.assertEqual("FileNotFoundError", record["error_type"])
        self.assertIn("missing.toml", record["error_message"])
        self.assertTrue(any(frame["function"] == "main" for frame in record["exception_chain"][0]["frames"]))
