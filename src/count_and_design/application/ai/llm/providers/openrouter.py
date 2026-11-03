"""OpenRouter LLM provider using the OpenAI-compatible chat API."""

from __future__ import annotations

import base64
from collections.abc import Sequence
from typing import Any

import requests

from count_and_design.application.config import OpenRouterLlmConfig

from ..types import ChatMessage, LlmCompletionResult, LlmServiceError

_IMAGE_MIME_TYPES = frozenset(
    {"image/png", "image/jpeg", "image/jpg", "image/gif", "image/webp"}
)


class OpenRouterLlmProvider:
    """Client for models routed through OpenRouter."""

    def __init__(self, config: OpenRouterLlmConfig) -> None:
        self._config = config

    @property
    def config(self) -> OpenRouterLlmConfig:
        return self._config

    def list_models(self, *, refresh: bool = False) -> list[str]:
        del refresh
        try:
            response = requests.get(
                self._config.models_url,
                headers=self._auth_headers(),
                timeout=self._config.model_probe_timeout,
            )
        except requests.RequestException as exc:
            raise LlmServiceError(
                f"OpenRouter model discovery failed: {exc}"
            ) from exc
        self._raise_for_status(response, operation="model discovery")
        try:
            payload = response.json()
        except ValueError as exc:
            raise LlmServiceError(
                "OpenRouter model discovery returned invalid JSON."
            ) from exc
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, list):
            raise LlmServiceError(
                "OpenRouter model discovery response did not contain data[]."
            )
        model_ids = [
            str(item["id"])
            for item in data
            if isinstance(item, dict) and item.get("id")
        ]
        if not model_ids:
            raise LlmServiceError(
                "OpenRouter did not advertise any models."
            )
        return model_ids

    def resolve_model(self, *, refresh: bool = False) -> str:
        if self._config.model:
            return self._config.model
        return self.list_models(refresh=refresh)[0]

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
        del reasoning_effort, include_reasoning
        if not messages:
            raise LlmServiceError("No chat messages were provided.")
        if stream:
            raise LlmServiceError(
                "Streaming chat completions are not supported yet."
            )
        body = self._chat_body(
            [
                {"role": message.role, "content": message.content}
                for message in messages
            ],
            temperature=temperature,
            max_completion_tokens=max_completion_tokens,
        )
        return self._parse_completion(self._post(body))

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
    ) -> LlmCompletionResult:
        return self.complete_with_images(
            prompt,
            ((image, mime_type),),
            system_prompt=system_prompt,
            temperature=temperature,
            max_completion_tokens=max_completion_tokens,
        )

    def complete_with_images(
        self,
        prompt: str,
        images: Sequence[tuple[bytes, str]],
        *,
        system_prompt: str | None = None,
        temperature: float | None = None,
        max_completion_tokens: int | None = None,
    ) -> LlmCompletionResult:
        """Complete one prompt with multiple ordered image inputs."""

        if not prompt.strip():
            raise LlmServiceError("Prompt is empty.")
        if not images:
            raise LlmServiceError("No images were provided.")
        content: list[dict[str, Any]] = []
        for image, mime_type in images:
            if not image:
                raise LlmServiceError("Image bytes are empty.")
            normalized_mime_type = mime_type.strip().lower()
            if normalized_mime_type not in _IMAGE_MIME_TYPES:
                raise LlmServiceError(
                    f"Unsupported OpenRouter image mime type: {mime_type}"
                )
            data_url = (
                f"data:{normalized_mime_type};base64,"
                f"{base64.b64encode(image).decode('ascii')}"
            )
            content.append(
                {
                    "type": "image_url",
                    "image_url": {"url": data_url},
                }
            )
        content.append({"type": "text", "text": prompt})
        messages: list[dict[str, Any]] = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": content})
        body = self._chat_body(
            messages,
            temperature=temperature,
            max_completion_tokens=max_completion_tokens,
        )
        return self._parse_completion(self._post(body))

    def _chat_body(
        self,
        messages: list[dict[str, Any]],
        *,
        temperature: float | None,
        max_completion_tokens: int | None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": self.resolve_model(),
            "messages": messages,
        }
        resolved_max_tokens = (
            self._config.max_tokens
            if max_completion_tokens is None
            else max_completion_tokens
        )
        if resolved_max_tokens is not None:
            body["max_tokens"] = resolved_max_tokens
        resolved_temperature = (
            self._config.temperature
            if temperature is None
            else temperature
        )
        if resolved_temperature is not None:
            body["temperature"] = resolved_temperature
        return body

    def _auth_headers(self) -> dict[str, str]:
        headers = {
            "Authorization": f"Bearer {self._config.api_key}",
            "Content-Type": "application/json",
        }
        if self._config.http_referer:
            headers["HTTP-Referer"] = self._config.http_referer
        if self._config.app_title:
            headers["X-Title"] = self._config.app_title
        return headers

    def _post(self, body: dict[str, Any]) -> dict[str, Any]:
        try:
            response = requests.post(
                self._config.chat_completions_url,
                json=body,
                headers=self._auth_headers(),
                timeout=self._config.timeout,
            )
        except requests.RequestException as exc:
            raise LlmServiceError(
                f"OpenRouter request failed: {exc}"
            ) from exc
        self._raise_for_status(response, operation="request")
        try:
            data = response.json()
        except ValueError as exc:
            raise LlmServiceError(
                "OpenRouter server returned invalid JSON."
            ) from exc
        if not isinstance(data, dict):
            raise LlmServiceError(
                "OpenRouter server returned an unsupported response."
            )
        return data

    @staticmethod
    def _raise_for_status(response: Any, *, operation: str) -> None:
        if 200 <= response.status_code < 300:
            return
        detail = response.text.strip()[:300]
        message = (
            f"OpenRouter server {operation} returned HTTP "
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
                "OpenRouter response contained no choices."
            )
        choice = choices[0]
        if not isinstance(choice, dict):
            raise LlmServiceError(
                "OpenRouter returned an unsupported choice format."
            )
        message = choice.get("message")
        if not isinstance(message, dict):
            raise LlmServiceError(
                "OpenRouter response did not contain a message."
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
