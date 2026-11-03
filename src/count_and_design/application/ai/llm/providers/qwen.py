"""Qwen LLM provider using an OpenAI-compatible vLLM endpoint."""

from __future__ import annotations

import base64
from collections.abc import Sequence
from threading import Lock
from typing import Any

import requests

from count_and_design.application.config import QwenLlmConfig

from ..types import ChatMessage, LlmCompletionResult, LlmServiceError

_IMAGE_MIME_TYPES = frozenset(
    {"image/png", "image/jpeg", "image/jpg", "image/gif", "image/webp"}
)


class QwenLlmProvider:
    """Client for a vLLM-hosted Qwen model via /v1/chat/completions."""

    def __init__(self, config: QwenLlmConfig) -> None:
        self._config = config
        self._model_cache: str | None = None
        self._model_lock = Lock()

    @property
    def config(self) -> QwenLlmConfig:
        return self._config

    def list_models(self, *, refresh: bool = False) -> list[str]:
        try:
            response = requests.get(
                self._config.models_url,
                headers=self._auth_headers(),
                timeout=self._config.model_probe_timeout,
            )
        except requests.RequestException as exc:
            raise LlmServiceError(
                f"LLM model discovery failed: {exc}"
            ) from exc
        self._raise_for_status(response, operation="model discovery")
        try:
            payload = response.json()
        except ValueError as exc:
            raise LlmServiceError(
                "LLM model discovery returned invalid JSON."
            ) from exc
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, list):
            raise LlmServiceError(
                "LLM model discovery response did not contain data[]."
            )
        model_ids = [
            str(item["id"])
            for item in data
            if isinstance(item, dict) and item.get("id")
        ]
        if not model_ids:
            raise LlmServiceError(
                "LLM service did not advertise any models."
            )
        return model_ids

    def resolve_model(self, *, refresh: bool = False) -> str:
        if self._config.model:
            return self._config.model
        with self._model_lock:
            if self._model_cache is not None and not refresh:
                return self._model_cache
            model = self.list_models(refresh=refresh)[0]
            self._model_cache = model
            return model

    def chat(
        self,
        messages: Sequence[ChatMessage],
        *,
        temperature: float | None = None,
        max_completion_tokens: int | None = None,
        reasoning_effort: str | None = None,
        include_reasoning: bool | None = None,
        stream: bool = False,
    ) -> LlmCompletionResult:
        if not messages:
            raise LlmServiceError("No chat messages were provided.")
        if stream:
            raise LlmServiceError(
                "Streaming chat completions are not supported yet."
            )

        body: dict[str, Any] = {
            "model": self.resolve_model(),
            "messages": [
                {"role": message.role, "content": message.content}
                for message in messages
            ],
            "max_completion_tokens": (
                self._config.max_completion_tokens
                if max_completion_tokens is None
                else max_completion_tokens
            ),
            "reasoning_effort": (
                self._config.reasoning_effort
                if reasoning_effort is None
                else reasoning_effort
            ),
            "include_reasoning": (
                self._config.include_reasoning
                if include_reasoning is None
                else include_reasoning
            ),
        }
        resolved_temperature = (
            self._config.temperature
            if temperature is None
            else temperature
        )
        if resolved_temperature is not None:
            body["temperature"] = resolved_temperature

        payload = self._post(body)
        return self._parse_completion(payload)

    def complete(
        self,
        prompt: str,
        *,
        system_prompt: str | None = None,
        **kwargs: Any,
    ) -> LlmCompletionResult:
        if not prompt.strip():
            raise LlmServiceError("Prompt is empty.")
        messages: list[ChatMessage] = []
        if system_prompt:
            messages.append(ChatMessage(role="system", content=system_prompt))
        messages.append(ChatMessage(role="user", content=prompt))
        return self.chat(messages, **kwargs)

    def complete_with_image(
        self,
        prompt: str,
        image: bytes,
        *,
        mime_type: str = "image/png",
        system_prompt: str | None = None,
        temperature: float | None = None,
        max_completion_tokens: int | None = None,
        reasoning_effort: str | None = None,
        include_reasoning: bool | None = None,
    ) -> LlmCompletionResult:
        """Send one image to a multimodal Qwen chat-completions model."""

        if not prompt.strip():
            raise LlmServiceError("Prompt is empty.")
        if not image:
            raise LlmServiceError("Image bytes are empty.")
        normalized_mime_type = mime_type.strip().lower()
        if normalized_mime_type not in _IMAGE_MIME_TYPES:
            raise LlmServiceError(
                f"Unsupported Qwen image mime type: {mime_type}"
            )

        data_url = (
            f"data:{normalized_mime_type};base64,"
            f"{base64.b64encode(image).decode('ascii')}"
        )
        messages: list[dict[str, Any]] = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append(
            {
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": data_url}},
                    {"type": "text", "text": prompt},
                ],
            }
        )
        body: dict[str, Any] = {
            "model": self.resolve_model(),
            "messages": messages,
            "max_completion_tokens": (
                self._config.max_completion_tokens
                if max_completion_tokens is None
                else max_completion_tokens
            ),
            "reasoning_effort": (
                self._config.reasoning_effort
                if reasoning_effort is None
                else reasoning_effort
            ),
            "include_reasoning": (
                self._config.include_reasoning
                if include_reasoning is None
                else include_reasoning
            ),
        }
        resolved_temperature = (
            self._config.temperature
            if temperature is None
            else temperature
        )
        if resolved_temperature is not None:
            body["temperature"] = resolved_temperature
        return self._parse_completion(self._post(body))

    def respond(
        self,
        input_text: str,
        *,
        instructions: str | None = None,
        temperature: float | None = None,
        max_output_tokens: int | None = None,
        stream: bool = False,
        extra_body: dict[str, Any] | None = None,
    ) -> LlmCompletionResult:
        """Call Qwen through the OpenAI-compatible Responses API."""

        if not input_text.strip():
            raise LlmServiceError("Responses input is empty.")
        if stream:
            raise LlmServiceError(
                "Streaming Responses completions are not supported yet."
            )
        body: dict[str, Any] = {
            "model": self.resolve_model(),
            "input": input_text,
            "max_output_tokens": (
                self._config.max_completion_tokens
                if max_output_tokens is None
                else max_output_tokens
            ),
            "stream": False,
        }
        if instructions:
            body["instructions"] = instructions
        resolved_temperature = (
            self._config.temperature
            if temperature is None
            else temperature
        )
        if resolved_temperature is not None:
            body["temperature"] = resolved_temperature
        if extra_body:
            body.update(extra_body)

        payload = self._post(body, url=self._config.responses_url)
        return self._parse_response(payload)

    def _auth_headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._config.api_key}",
            "Content-Type": "application/json",
        }

    def _post(
        self,
        body: dict[str, Any],
        *,
        url: str | None = None,
    ) -> dict[str, Any]:
        try:
            response = requests.post(
                url or self._config.chat_completions_url,
                json=body,
                headers=self._auth_headers(),
                timeout=self._config.timeout,
            )
        except requests.RequestException as exc:
            raise LlmServiceError(f"LLM request failed: {exc}") from exc
        self._raise_for_status(response, operation="request")
        try:
            data = response.json()
        except ValueError as exc:
            raise LlmServiceError(
                "LLM server returned invalid JSON."
            ) from exc
        if not isinstance(data, dict):
            raise LlmServiceError(
                "LLM server returned an unsupported response."
            )
        return data

    @staticmethod
    def _raise_for_status(response: Any, *, operation: str) -> None:
        if 200 <= response.status_code < 300:
            return
        detail = response.text.strip()[:300]
        message = (
            f"LLM server {operation} returned HTTP "
            f"{response.status_code}"
        )
        if detail:
            message = f"{message}: {detail}"
        raise LlmServiceError(message)

    @staticmethod
    def _parse_completion(payload: dict[str, Any]) -> LlmCompletionResult:
        choices = payload.get("choices")
        if not isinstance(choices, list) or not choices:
            raise LlmServiceError(
                "LLM server response contained no choices."
            )
        choice = choices[0]
        if not isinstance(choice, dict):
            raise LlmServiceError(
                "LLM server returned an unsupported choice format."
            )
        message = choice.get("message")
        if not isinstance(message, dict):
            raise LlmServiceError(
                "LLM server response did not contain a message."
            )
        content = message.get("content")
        if isinstance(content, list):
            content = "".join(
                part.get("text", "")
                for part in content
                if isinstance(part, dict)
            )
        if not isinstance(content, str):
            content = ""
        reasoning = message.get("reasoning")
        if reasoning is not None and not isinstance(reasoning, str):
            reasoning = None
        usage = payload.get("usage")
        return LlmCompletionResult(
            content=content,
            model=str(payload.get("model") or ""),
            finish_reason=str(choice.get("finish_reason") or ""),
            reasoning=reasoning,
            usage=usage if isinstance(usage, dict) else {},
            raw=payload,
        )

    @staticmethod
    def _parse_response(payload: dict[str, Any]) -> LlmCompletionResult:
        direct = payload.get("output_text")
        if isinstance(direct, str) and direct.strip():
            content = direct.strip()
        else:
            chunks: list[str] = []
            output = payload.get("output")
            if isinstance(output, list):
                for item in output:
                    if not isinstance(item, dict):
                        continue
                    parts = item.get("content")
                    if not isinstance(parts, list):
                        continue
                    for part in parts:
                        if not isinstance(part, dict):
                            continue
                        text = part.get("text")
                        if isinstance(text, str):
                            chunks.append(text)
            content = "".join(chunks).strip()
        if not content:
            raise LlmServiceError(
                "LLM Responses server returned no output text."
            )
        usage = payload.get("usage")
        return LlmCompletionResult(
            content=content,
            model=str(payload.get("model") or ""),
            finish_reason=str(payload.get("status") or ""),
            usage=usage if isinstance(usage, dict) else {},
            raw=payload,
        )
