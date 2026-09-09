from __future__ import annotations

import json
import unittest

import httpx

from log_analyzer.analysis import GlobalAnalyzerError, InvalidResponseError, NvidiaNimAnalyzer
from test_openai_responses import analysis_request, result_payload


def response_for(payload: dict, **choice_changes: object) -> dict:
    choice = {
        "index": 0,
        "finish_reason": "stop",
        "message": {"role": "assistant", "content": json.dumps(payload)},
    }
    choice.update(choice_changes)
    return {"choices": [choice]}


class NvidiaNimTests(unittest.IsolatedAsyncioTestCase):
    async def test_posts_nim_schema_request_redacts_and_filters_evidence(self) -> None:
        requests = []

        def handler(request):
            requests.append(request)
            return httpx.Response(200, json=response_for(result_payload(evidence_line=999)))

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            result = await NvidiaNimAnalyzer(
                api_key="test-key", model="test-model", client=client, enable_thinking=False
            ).analyze(analysis_request())
        self.assertEqual([], result.root_causes[0].evidence)
        self.assertEqual(1, len(requests))
        request = requests[0]
        self.assertEqual("https://integrate.api.nvidia.com/v1/chat/completions", str(request.url))
        self.assertEqual("Bearer test-key", request.headers["Authorization"])
        body = json.loads(request.content)
        self.assertEqual(["system", "user"], [item["role"] for item in body["messages"]])
        self.assertIn("Write every human-readable explanation in Korean", body["messages"][0]["content"])
        self.assertEqual("json_schema", body["response_format"]["type"])
        self.assertFalse(body["response_format"]["json_schema"]["schema"]["additionalProperties"])
        self.assertIn("error_priority", body["response_format"]["json_schema"]["schema"]["required"])
        self.assertEqual("중간", result.error_priority.level)
        self.assertEqual({"enable_thinking": False}, body["chat_template_kwargs"])
        self.assertEqual(3000, body["max_tokens"])
        self.assertFalse(body["stream"])
        for field in ("input", "instructions", "text", "store", "max_output_tokens", "tools"):
            self.assertNotIn(field, body)
        self.assertNotIn("do-not-send", request.content.decode())

    async def test_modes_are_explicit_and_never_silently_downgraded(self) -> None:
        for mode in ("guided_json", "json_object"):
            with self.subTest(mode=mode):
                requests = []

                def handler(request):
                    requests.append(json.loads(request.content))
                    return httpx.Response(200, json=response_for(result_payload()))

                async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                    await NvidiaNimAnalyzer(
                        api_key="key", model="model", structured_output=mode, client=client
                    ).analyze(analysis_request())
                body = requests[0]
                self.assertIn("Required JSON schema", body["messages"][0]["content"])
                if mode == "guided_json":
                    self.assertIn("properties", body["guided_json"])
                    self.assertNotIn("response_format", body)
                else:
                    self.assertEqual({"type": "json_object"}, body["response_format"])
                    self.assertNotIn("guided_json", body)

    async def test_repairs_once_and_redacts_previous_output(self) -> None:
        requests = []

        def handler(request):
            requests.append(request)
            payload = {"summary": "password=repair-secret"} if len(requests) == 1 else result_payload()
            return httpx.Response(200, json=response_for(payload))

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            result = await NvidiaNimAnalyzer(api_key="key", model="model", client=client).analyze(analysis_request())
        self.assertTrue(result.root_causes)
        self.assertEqual(2, len(requests))
        repaired = json.loads(requests[1].content)
        self.assertNotIn("repair-secret", requests[1].content.decode())
        self.assertIn("previous_invalid_output", repaired["messages"][1]["content"])

    async def test_repeated_invalid_schema_fails_after_one_repair(self) -> None:
        requests = []

        def handler(request):
            requests.append(request)
            return httpx.Response(200, json=response_for({}))

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with self.assertRaises(InvalidResponseError):
                await NvidiaNimAnalyzer(api_key="key", model="model", client=client).analyze(analysis_request())
        self.assertEqual(2, len(requests))

    async def test_rejects_truncation_refusal_and_missing_content(self) -> None:
        bodies = [
            response_for(result_payload(), finish_reason="length"),
            response_for(result_payload(), finish_reason="content_filter"),
            response_for(result_payload(), message={"role": "assistant", "content": None}),
            response_for(result_payload(), message={"role": "assistant", "content": "{}", "refusal": "no"}),
            response_for(result_payload(), message={"role": "assistant", "content": "{}", "tool_calls": [{"id": "call"}]}),
            {"choices": []},
            {"error": {"message": "remote secret"}},
        ]
        for body in bodies:
            with self.subTest(body=body):
                requests = []

                def handler(request):
                    requests.append(request)
                    return httpx.Response(200, json=body)

                async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                    with self.assertRaises(InvalidResponseError):
                        await NvidiaNimAnalyzer(api_key="key", model="model", client=client).analyze(analysis_request())
                self.assertEqual(1, len(requests))

    async def test_contract_auth_and_model_errors_stop_without_retry(self) -> None:
        for status in (400, 401, 402, 403, 404, 422):
            with self.subTest(status=status):
                requests = []

                def handler(request):
                    requests.append(request)
                    return httpx.Response(status, json={"error": "remote-secret"})

                async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                    with self.assertRaises(GlobalAnalyzerError) as raised:
                        await NvidiaNimAnalyzer(api_key="key", model="model", client=client).analyze(analysis_request())
                self.assertNotIn("remote-secret", str(raised.exception))
                self.assertEqual(1, len(requests))

    async def test_429_retry_after_is_shared_with_responses_policy(self) -> None:
        calls = []
        delays = []

        def handler(request):
            calls.append(request)
            return (httpx.Response(429, headers={"Retry-After": "2"}) if len(calls) == 1
                    else httpx.Response(200, json=response_for(result_payload())))

        async def record_sleep(delay):
            delays.append(delay)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            await NvidiaNimAnalyzer(api_key="key", model="model", client=client, sleep=record_sleep).analyze(analysis_request())
        self.assertEqual([2.0], delays)
        self.assertEqual(2, len(calls))
