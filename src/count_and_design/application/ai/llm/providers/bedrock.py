"""AWS Bedrock LLM provider using the Converse API."""

from __future__ import annotations

import base64
from collections.abc import Sequence
from typing import Any

import requests

from count_and_design.application.config import BedrockLlmConfig

from ..types import ChatMessage, LlmCompletionResult, LlmServiceError

_IMAGE_FORMATS = {
    "image/png": "png",
    "image/jpeg": "jpeg",
    "image/jpg": "jpeg",
    "image/gif": "gif",
    "image/webp": "webp",
}


class BedrockLlmProvider:
    """Client for Anthropic Claude and other models on Amazon Bedrock."""

    def __init__(self, config: BedrockLlmConfig) -> None:
        self._config = config

    @property
    def config(self) -> BedrockLlmConfig:
        return self._config

    def list_models(self, *, refresh: bool = False) -> list[str]:
        return [self._config.model]

    def resolve_model(self, *, refresh: bool = False) -> str:
        return self._config.model

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
        # Claude Opus 4.7 rejects ``temperature``. Keep the common provider
        # signature, but let Bedrock choose all sampling defaults.
        del temperature, reasoning_effort, include_reasoning
        if not messages:
            raise LlmServiceError("No chat messages were provided.")
        if stream:
            raise LlmServiceError(
                "Streaming chat completions are not supported yet."
            )

        system_blocks: list[dict[str, str]] = []
        bedrock_messages: list[dict[str, Any]] = []
        for message in messages:
            if message.role == "system":
                system_blocks.append({"text": message.content})
                continue
            bedrock_messages.append(
                {
                    "role": message.role,
                    "content": [{"text": message.content}],
                }
            )
        if not bedrock_messages:
            raise LlmServiceError(
                "Bedrock requests require at least one user or assistant message."
            )

        body: dict[str, Any] = {"messages": bedrock_messages}
        if system_blocks:
            body["system"] = system_blocks

        resolved_max_tokens = (
            self._config.max_tokens
            if max_completion_tokens is None
            else max_completion_tokens
        )
        if resolved_max_tokens is not None:
            body["inferenceConfig"] = {"maxTokens": resolved_max_tokens}

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

        # Claude Opus 4.7 rejects ``temperature``. Keep the common provider
        # signature, but let Bedrock choose its sampling default.
        del temperature
        if not prompt.strip():
            raise LlmServiceError("Prompt is empty.")
        if not images:
            raise LlmServiceError("No images were provided.")
        content: list[dict[str, Any]] = []
        for image, mime_type in images:
            if not image:
                raise LlmServiceError("Image bytes are empty.")
            image_format = _IMAGE_FORMATS.get(mime_type.lower())
            if image_format is None:
                raise LlmServiceError(
                    f"Unsupported Bedrock image mime type: {mime_type}"
                )
            content.append(
                {
                    "image": {
                        "format": image_format,
                        "source": {
                            "bytes": base64.b64encode(image).decode("ascii"),
                        },
                    }
                }
            )
        content.append({"text": prompt})
        bedrock_messages = [{"role": "user", "content": content}]
        body: dict[str, Any] = {"messages": bedrock_messages}
        if system_prompt:
            body["system"] = [{"text": system_prompt}]
        resolved_max_tokens = (
            self._config.max_tokens
            if max_completion_tokens is None
            else max_completion_tokens
        )
        if resolved_max_tokens is not None:
            body["inferenceConfig"] = {"maxTokens": resolved_max_tokens}
        payload = self._post(body)
        return self._parse_completion(payload)

    def _auth_headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._config.api_key}",
            "Content-Type": "application/json",
        }

    def _post(self, body: dict[str, Any]) -> dict[str, Any]:
        try:
            response = requests.post(
                self._config.converse_url,
                json=body,
                headers=self._auth_headers(),
                timeout=self._config.timeout,
            )
        except requests.RequestException as exc:
            raise LlmServiceError(f"Bedrock request failed: {exc}") from exc
        self._raise_for_status(response, operation="request")
        try:
            data = response.json()
        except ValueError as exc:
            raise LlmServiceError(
                "Bedrock server returned invalid JSON."
            ) from exc
        if not isinstance(data, dict):
            raise LlmServiceError(
                "Bedrock server returned an unsupported response."
            )
        return data

    @staticmethod
    def _raise_for_status(response: Any, *, operation: str) -> None:
        if 200 <= response.status_code < 300:
            return
        detail = response.text.strip()[:300]
        message = (
            f"Bedrock server {operation} returned HTTP "
            f"{response.status_code}"
        )
        if detail:
            message = f"{message}: {detail}"
        raise LlmServiceError(message)

    def _parse_completion(self, payload: dict[str, Any]) -> LlmCompletionResult:
        output = payload.get("output")
        if not isinstance(output, dict):
            raise LlmServiceError(
                "Bedrock response did not contain output."
            )
        message = output.get("message")
        if not isinstance(message, dict):
            raise LlmServiceError(
                "Bedrock response did not contain output.message."
            )
        content_blocks = message.get("content")
        if not isinstance(content_blocks, list):
            raise LlmServiceError(
                "Bedrock response did not contain message content."
            )
        content = "".join(
            block.get("text", "")
            for block in content_blocks
            if isinstance(block, dict)
        )
        usage = payload.get("usage")
        return LlmCompletionResult(
            content=content,
            model=self._config.model,
            finish_reason=str(payload.get("stopReason") or ""),
            usage=usage if isinstance(usage, dict) else {},
            raw=payload,
        )
