"""NVIDIA NIM Chat Completions with the existing HTTP and validation policies."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from typing import Any, Literal
from pathlib import Path

import httpx
from pydantic import ValidationError

from .models import AnalysisRequest, AnalysisResult
from .openai_responses import (
    InvalidResponseError,
    OpenAIResponsesAnalyzer,
    Sleep,
    _INSTRUCTIONS,
    _SchemaResponseError,
)
from .redact import SecretRedactor


class NvidiaNimAnalyzer(OpenAIResponsesAnalyzer):
    """Reuse redaction, bounded retries and one repair; override the wire format."""

    _endpoint = "chat/completions"
    _provider_name = "NVIDIA NIM"
    _global_statuses = {400, 401, 402, 403, 404, 422}

    def __init__(
        self,
        *,
        api_key: str | None,
        model: str,
        base_url: str = "https://integrate.api.nvidia.com/v1",
        structured_output: Literal["json_schema", "guided_json", "json_object"] = "json_schema",
        enable_thinking: bool | None = None,
        timeout_seconds: float = 60.0,
        max_output_tokens: int = 3_000,
        max_attempts: int = 3,
        client: httpx.AsyncClient | None = None,
        redactor: SecretRedactor | None = None,
        sleep: Sleep = asyncio.sleep,
        tls_ca: Path | None = None,
    ) -> None:
        if structured_output not in {"json_schema", "guided_json", "json_object"}:
            raise ValueError("unsupported NIM structured output mode")
        self._structured_output = structured_output
        self._enable_thinking = enable_thinking
        super().__init__(
            api_key=api_key,
            model=model,
            base_url=base_url,
            timeout_seconds=timeout_seconds,
            max_output_tokens=max_output_tokens,
            max_attempts=max_attempts,
            client=client,
            redactor=redactor,
            sleep=sleep,
            tls_ca=tls_ca,
        )

    def _payload(self, request: AnalysisRequest) -> dict[str, Any]:
        schema = AnalysisResult.model_json_schema()
        payload: dict[str, Any] = {
            "model": self._model,
            "messages": [
                {
                    "role": "system",
                    "content": _INSTRUCTIONS + "\nRequired JSON schema:\n" + json.dumps(schema),
                },
                {"role": "user", "content": request.to_prompt_json()},
            ],
            "max_tokens": self._max_output_tokens,
            "stream": False,
        }
        if self._enable_thinking is not None:
            payload["chat_template_kwargs"] = {"enable_thinking": self._enable_thinking}
        if self._structured_output == "guided_json":
            payload["guided_json"] = schema
        elif self._structured_output == "json_object":
            payload["response_format"] = {"type": "json_object"}
        else:
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "incident_analysis", "schema": schema},
            }
        return payload

    def _repair_payload(
        self, request: AnalysisRequest, invalid_output: str
    ) -> dict[str, Any]:
        payload = self._payload(request)
        payload["messages"][0]["content"] += (
            "\nA previous response was invalid. Correct its JSON structure once; "
            "do not add unsupported claims."
        )
        payload["messages"][1]["content"] = json.dumps(
            {
                "incident": request.model_dump(mode="json"),
                "previous_invalid_output": self._redactor.redact_text(invalid_output[:8_000]),
            },
            ensure_ascii=False,
        )
        return payload

    @staticmethod
    def _parse_result(body: Mapping[str, Any]) -> AnalysisResult:
        choices = body.get("choices")
        if not isinstance(choices, list) or len(choices) != 1:
            raise InvalidResponseError("NVIDIA NIM response must contain one choice")
        choice = choices[0]
        if not isinstance(choice, Mapping) or choice.get("finish_reason") != "stop":
            raise InvalidResponseError("NVIDIA NIM generation did not finish normally")
        message = choice.get("message")
        if (
            not isinstance(message, Mapping)
            or message.get("role") != "assistant"
            or message.get("refusal")
            or message.get("tool_calls")
        ):
            raise InvalidResponseError("NVIDIA NIM did not return an analysis message")
        content = message.get("content")
        if not isinstance(content, str) or not content.strip():
            raise InvalidResponseError("NVIDIA NIM response had no text content")
        try:
            return AnalysisResult.model_validate_json(content)
        except (ValidationError, ValueError) as exc:
            raise _SchemaResponseError(
                "NVIDIA NIM output failed AnalysisResult validation", content
            ) from exc
