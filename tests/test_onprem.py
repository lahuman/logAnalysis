from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock, patch

import httpx
from pydantic import ValidationError

from log_analyzer import cli, nim_smoke
from log_analyzer.analysis import GlobalAnalyzerError, OnPremAnalyzer
from log_analyzer.config import AppConfig, OpenAIConfig, load_config
from test_nvidia_nim import response_for
from test_openai_responses import analysis_request, result_payload


class OnPremConfigTests(unittest.TestCase):
    def test_requires_explicit_url_and_explicit_http_opt_in(self):
        with self.assertRaises(ValidationError):
            OpenAIConfig(provider="onprem", model="local")
        with self.assertRaises(ValidationError):
            OpenAIConfig(provider="onprem", base_url="http://llm.internal/v1", model="local")
        config = OpenAIConfig(provider="onprem", base_url="http://llm.internal/v1",
                              allow_http=True, auth_required=False, model="local")
        self.assertEqual("LLM_API_KEY", config.api_key_secret)
        self.assertNotEqual(config.cache_analyzer_version("1"), "1")
        other = config.model_copy(update={"base_url": "http://other.internal/v1"})
        self.assertNotEqual(config.cache_analyzer_version("1"), other.cache_analyzer_version("1"))

    def test_cloud_authentication_and_https_remain_required(self):
        for provider in ("openai", "nvidia_nim"):
            for options in ({"auth_required": False}, {"allow_http": True},
                            {"base_url": "http://llm.internal/v1"}):
                with self.subTest(provider=provider, options=options), self.assertRaises(ValidationError):
                    OpenAIConfig(provider=provider, model="local", **options)

    def test_onprem_example_and_secret_bearing_url_rejection(self):
        config = load_config(Path(__file__).resolve().parents[1] / "config/onprem.toml.example")
        self.assertEqual("onprem", config.openai.provider)
        self.assertEqual(Path("data/state.db"), config.state.path)
        for url in ("https://key@llm.internal/v1", "https://llm.internal/v1?key=secret"):
            with self.assertRaises(ValidationError):
                OpenAIConfig(provider="onprem", base_url=url, model="local")


class OnPremTransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_optional_auth_posts_only_to_internal_endpoint(self):
        for key in (None, "internal-key"):
            requests = []
            def handler(request):
                requests.append(request)
                return httpx.Response(200, json=response_for(result_payload()))
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                result = await OnPremAnalyzer(api_key=key, model="local", base_url="http://llm.internal:8000/v1",
                                              client=client).analyze(analysis_request())
            self.assertTrue(result.root_causes)
            self.assertEqual(1, len(requests))
            self.assertEqual("http://llm.internal:8000/v1/chat/completions", str(requests[0].url))
            self.assertEqual(f"Bearer {key}" if key else None, requests[0].headers.get("Authorization"))
            self.assertNotIn("do-not-send", requests[0].content.decode())

    async def test_owned_client_ignores_proxies_and_uses_custom_ca(self):
        ca = Path("internal-ca.pem")
        context = Mock()
        client = Mock(post=AsyncMock(return_value=httpx.Response(200, json=response_for(result_payload()))),
                      aclose=AsyncMock())
        with (
            patch("log_analyzer.analysis.openai_responses.ssl.create_default_context", return_value=context) as ssl_context,
            patch("log_analyzer.analysis.openai_responses.httpx.AsyncClient", return_value=client) as factory,
            patch.dict("os.environ", {"HTTPS_PROXY": "http://external-proxy.invalid:8080"}),
        ):
            await OnPremAnalyzer(api_key=None, model="local", base_url="https://llm.internal/v1",
                                 tls_ca=ca).analyze(analysis_request())
        ssl_context.assert_called_once_with(cafile=str(ca))
        factory.assert_called_once_with(verify=context, trust_env=False)
        client.aclose.assert_awaited_once()

    async def test_auth_failure_does_not_fall_back_or_retry(self):
        requests = []
        def handler(request):
            requests.append(request)
            return httpx.Response(403, json={"message": "private provider detail"})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with self.assertRaisesRegex(GlobalAnalyzerError, "On-premises LLM") as error:
                await OnPremAnalyzer(api_key="invalid", model="local", base_url="https://llm.internal/v1",
                                     client=client).analyze(analysis_request())
        self.assertEqual(1, len(requests))
        self.assertNotIn("private provider detail", str(error.exception))

    async def test_healthcheck_uses_local_model_listing_without_auth_or_proxy(self):
        config = AppConfig.model_validate({"error_source": {"url": "https://es.internal", "index": "logs"},
            "services": [{"name": "orders", "repository": "repo", "application_packages": ["com.example"]}],
            "openai": {"provider": "onprem", "base_url": "https://llm.internal/v1", "model": "local", "auth_required": False}})
        requests = []
        def handler(request):
            requests.append(request)
            return httpx.Response(200, json={"data": [{"id": "local"}]})
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        with patch.object(cli.httpx, "AsyncClient", return_value=client) as factory:
            await cli._healthcheck_openai(config, None)
        self.assertFalse(factory.call_args.kwargs["trust_env"])
        self.assertNotIn("Authorization", requests[0].headers)
        self.assertEqual("https://llm.internal/v1/models", str(requests[0].url))

    async def test_smoke_works_without_cloud_credentials(self):
        config = OpenAIConfig(provider="onprem", base_url="https://llm.internal/v1", model="local", auth_required=False)
        def handler(request):
            return httpx.Response(200, json=response_for(result_payload(evidence_line=4, evidence_file="src/main/java/com/example/smoke/OrderService.java")))
        with tempfile.TemporaryDirectory() as work:
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                analyzer = OnPremAnalyzer(api_key=None, model="local", base_url=config.base_url, client=client)
                with patch.dict("os.environ", {}, clear=True), patch.object(nim_smoke, "OnPremAnalyzer", return_value=analyzer):
                    report = await nim_smoke.run_smoke(config, Path(work))
            self.assertIn("처리 상태: 분석 완료", report.read_text(encoding="utf-8"))
