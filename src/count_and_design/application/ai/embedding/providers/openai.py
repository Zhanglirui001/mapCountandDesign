"""OpenAI-compatible internal embedding provider."""

from __future__ import annotations

import base64
from collections.abc import Sequence
import mimetypes
from pathlib import Path
from threading import Lock
from typing import Any

import requests

from count_and_design.application.config import EmbeddingConfig

from ..types import EmbeddingResult, EmbeddingServiceError, ImageInput


class OpenAIEmbeddingProvider:
    """Embedding provider backed by an OpenAI-compatible HTTP endpoint."""

    def __init__(self, config: EmbeddingConfig) -> None:
        self._config = config
        self._model_cache: tuple[str, ...] | None = None
        self._model_lock = Lock()

    @property
    def config(self) -> EmbeddingConfig:
        return self._config

    def list_models(self, *, refresh: bool = False) -> list[str]:
        with self._model_lock:
            if self._model_cache is not None and not refresh:
                return list(self._model_cache)
            try:
                response = requests.get(
                    self._config.models_url,
                    headers=self._headers(),
                    timeout=self._config.model_probe_timeout,
                )
            except requests.RequestException as exc:
                raise EmbeddingServiceError(
                    f"Embedding model discovery failed: {exc}"
                ) from exc
            self._raise_for_status(response, operation="model discovery")
            try:
                payload = response.json()
            except ValueError as exc:
                raise EmbeddingServiceError(
                    "Embedding model discovery returned invalid JSON."
                ) from exc
            data = payload.get("data") if isinstance(payload, dict) else None
            if not isinstance(data, list):
                raise EmbeddingServiceError(
                    "Embedding model discovery response did not contain data[]."
                )
            model_ids = tuple(
                str(item["id"])
                for item in data
                if isinstance(item, dict) and item.get("id")
            )
            if not model_ids:
                raise EmbeddingServiceError(
                    "Embedding service did not advertise any models."
                )
            self._model_cache = model_ids
            return list(model_ids)

    def resolve_model(self, *, refresh: bool = False) -> str:
        if self._config.model:
            return self._config.model
        return self.list_models(refresh=refresh)[0]

    def embed_text(self, text: str) -> EmbeddingResult:
        if not text.strip():
            raise EmbeddingServiceError("Text input is empty.")
        payload: dict[str, Any] = {
            "input": text,
            "encoding_format": self._config.encoding_format,
        }
        self._add_common_options(payload)
        return self._first_result(self._post(payload))

    def embed_texts(self, texts: Sequence[str]) -> list[EmbeddingResult]:
        normalized = [text for text in texts if text.strip()]
        if not normalized:
            raise EmbeddingServiceError("No text inputs were provided.")
        payload: dict[str, Any] = {
            "input": normalized,
            "encoding_format": self._config.encoding_format,
        }
        self._add_common_options(payload)
        return self._parse_results(self._post(payload))

    def embed_image(
        self,
        image: ImageInput,
        *,
        prompt: str | None = None,
        mime_type: str | None = None,
    ) -> EmbeddingResult:
        """Embed one image.

        The message layout below is not interchangeable with a plain chat
        completion request.  The embedding is the hidden state of the final
        token, so the empty assistant turn plus continue_final_message is
        what the reference category vectors were built with; changing the
        user text or swapping in add_generation_prompt moves the vector into
        a different space and cosine scores against those vectors collapse.
        """

        payload: dict[str, Any] = {
            "messages": [
                {
                    "role": "system",
                    "content": [
                        {"type": "text", "text": self._config.system_prompt}
                    ],
                },
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": image_to_data_uri(
                                    image,
                                    mime_type=mime_type,
                                ),
                            },
                        },
                        {"type": "text", "text": prompt or ""},
                    ],
                },
                {"role": "assistant", "content": [{"type": "text", "text": ""}]},
            ],
            "encoding_format": self._config.encoding_format,
            "continue_final_message": True,
            "add_special_tokens": True,
        }
        self._add_common_options(payload)
        return self._first_result(self._post(payload))

    def _add_common_options(self, payload: dict[str, Any]) -> None:
        payload["model"] = self.resolve_model()
        if self._config.dimensions is not None:
            payload["dimensions"] = self._config.dimensions

    @staticmethod
    def _headers() -> dict[str, str]:
        return {"Content-Type": "application/json"}

    def _post(self, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            response = requests.post(
                self._config.endpoint_url,
                json=payload,
                headers=self._headers(),
                timeout=self._config.timeout,
            )
        except requests.RequestException as exc:
            raise EmbeddingServiceError(
                f"Embedding request failed: {exc}"
            ) from exc
        self._raise_for_status(response, operation="request")
        try:
            data = response.json()
        except ValueError as exc:
            raise EmbeddingServiceError(
                "Embedding server returned invalid JSON."
            ) from exc
        if not isinstance(data, dict):
            raise EmbeddingServiceError(
                "Embedding server returned an unsupported response."
            )
        return data

    @staticmethod
    def _raise_for_status(response: Any, *, operation: str) -> None:
        if 200 <= response.status_code < 300:
            return
        detail = response.text.strip()[:300]
        message = (
            f"Embedding server {operation} returned HTTP "
            f"{response.status_code}"
        )
        if detail:
            message = f"{message}: {detail}"
        raise EmbeddingServiceError(message)

    def _first_result(self, payload: dict[str, Any]) -> EmbeddingResult:
        results = self._parse_results(payload)
        if not results:
            raise EmbeddingServiceError(
                "Embedding server returned no embeddings."
            )
        return results[0]

    @staticmethod
    def _parse_results(payload: dict[str, Any]) -> list[EmbeddingResult]:
        data = payload.get("data")
        if not isinstance(data, list):
            raise EmbeddingServiceError(
                "Embedding server response did not contain data[]."
            )

        model = str(payload.get("model") or "")
        results: list[EmbeddingResult] = []
        for position, item in enumerate(data):
            if not isinstance(item, dict):
                continue
            embedding = item.get("embedding")
            if not isinstance(embedding, list):
                raise EmbeddingServiceError(
                    "Embedding response was not a float array."
                )
            try:
                vector = [float(value) for value in embedding]
            except (TypeError, ValueError) as exc:
                raise EmbeddingServiceError(
                    "Embedding response contained non-numeric values."
                ) from exc
            index = item.get("index", position)
            results.append(
                EmbeddingResult(
                    embedding=vector,
                    index=int(index) if isinstance(index, int) else position,
                    model=model,
                    raw=item,
                )
            )
        return results


def image_to_data_uri(
    image: ImageInput,
    *,
    mime_type: str | None = None,
) -> str:
    if isinstance(image, bytes):
        if not image:
            raise EmbeddingServiceError("Image bytes are empty.")
        resolved_mime = mime_type or "image/png"
        data = image
    else:
        path = Path(image).expanduser()
        if not path.exists():
            raise EmbeddingServiceError(
                f"Image file does not exist: {path}"
            )
        data = path.read_bytes()
        resolved_mime = (
            mime_type
            or mimetypes.guess_type(path.name)[0]
            or "image/png"
        )
    encoded = base64.b64encode(data).decode("ascii")
    return f"data:{resolved_mime};base64,{encoded}"
