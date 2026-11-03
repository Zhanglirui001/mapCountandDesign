from __future__ import annotations

from pathlib import Path
from threading import Lock
import time
from unittest.mock import Mock, patch

import pytest

from count_and_design.application.ai import (
    DRAWING_CLASSIFICATION_PROMPT,
    EmbeddingClient,
    EmbeddingConfig,
    EmbeddingResult,
    EmbeddingServiceError,
    embed_drawing_image,
)
from count_and_design.application.config import ConfigError


_EMBED_ENV_NAMES = [
    "ENV_FILE",
    "EMBEDDING_BASE_URL",
    "EMBEDDING_ENDPOINT_PATH",
    "EMBEDDING_MODELS_PATH",
    "EMBEDDING_MODEL",
    "EMBEDDING_TIMEOUT",
    "EMBEDDING_MODEL_PROBE_TIMEOUT",
    "EMBEDDING_MAX_CONCURRENCY",
    "EMBEDDING_DIMENSIONS",
    "EMBEDDING_ENCODING_FORMAT",
    "EMBEDDING_IMAGE_DETAIL",
    "EMBEDDING_ADD_GENERATION_PROMPT",
    "EMBEDDING_SYSTEM_PROMPT",
]


@pytest.fixture(autouse=True)
def clear_embedding_env(monkeypatch):
    for name in _EMBED_ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    yield
    for name in _EMBED_ENV_NAMES:
        monkeypatch.delenv(name, raising=False)


def _embedding_response(vector=None, *, model: str = "embed-model", status_code: int = 200):
    response = Mock(status_code=status_code, text="server error" if status_code >= 400 else "")
    response.json.return_value = {
        "model": model,
        "data": [
            {
                "index": 0,
                "embedding": vector or [0.1, 0.2, 0.3],
            }
        ],
    }
    return response


def _models_response(
    model_ids: list[str],
    *,
    status_code: int = 200,
) -> Mock:
    response = Mock(
        status_code=status_code,
        text="server error" if status_code >= 400 else "",
    )
    response.json.return_value = {
        "object": "list",
        "data": [{"id": model_id} for model_id in model_ids],
    }
    return response


def test_embedding_config_reads_env_file(tmp_path: Path) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text(
        "\n".join(
            [
                "EMBEDDING_BASE_URL=http://embed.test",
                "EMBEDDING_ENDPOINT_PATH=/v1/embeddings",
                "EMBEDDING_MODELS_PATH=/v1/models",
                "EMBEDDING_MODEL=vl-embedding",
                "EMBEDDING_TIMEOUT=9.5",
                "EMBEDDING_MODEL_PROBE_TIMEOUT=2.5",
                "EMBEDDING_MAX_CONCURRENCY=4",
                "EMBEDDING_DIMENSIONS=128",
                "EMBEDDING_ENCODING_FORMAT=float",
                "EMBEDDING_SYSTEM_PROMPT=Represent input.",
            ]
        ),
        encoding="utf-8",
    )

    config = EmbeddingConfig.from_env(env_path)

    assert config.endpoint_url == "http://embed.test/v1/embeddings"
    assert config.models_url == "http://embed.test/v1/models"
    assert config.model == "vl-embedding"
    assert config.timeout == 9.5
    assert config.model_probe_timeout == 2.5
    assert config.max_concurrency == 4
    assert config.dimensions == 128
    assert config.system_prompt == "Represent input."


def test_embedding_config_requires_base_url(tmp_path: Path) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text("EMBEDDING_MODEL=vl-embedding\n", encoding="utf-8")

    with pytest.raises(ConfigError, match="EMBEDDING_BASE_URL"):
        EmbeddingConfig.from_env(env_path)


def test_embed_text_sends_openai_compatible_completion_request() -> None:
    client = EmbeddingClient(
        EmbeddingConfig(
            base_url="http://embed.test",
            endpoint_path="/v1/embeddings",
            model="vl-embedding",
            dimensions=128,
            timeout=5,
        )
    )
    response = _embedding_response([0.4, 0.5])

    with patch(
        "count_and_design.application.ai.embedding.providers.openai.requests.post",
        return_value=response,
    ) as mock_post:
        result = client.embed_text("hello drawing")

    assert result.embedding == [0.4, 0.5]
    assert result.model == "embed-model"
    assert mock_post.call_args.args[0] == "http://embed.test/v1/embeddings"
    assert "Authorization" not in mock_post.call_args.kwargs["headers"]
    assert mock_post.call_args.kwargs["timeout"] == 5
    body = mock_post.call_args.kwargs["json"]
    assert body["input"] == "hello drawing"
    assert body["model"] == "vl-embedding"
    assert body["dimensions"] == 128
    assert body["encoding_format"] == "float"


def test_embed_image_sends_chat_request_with_data_uri(tmp_path: Path) -> None:
    image = tmp_path / "drawing.png"
    image.write_bytes(b"png-bytes")
    client = EmbeddingClient(
        EmbeddingConfig(
            base_url="http://embed.test",
            model="vl-embedding",
            dimensions=256,
            system_prompt="Represent input.",
        )
    )
    response = _embedding_response([0.7, 0.8])

    with patch(
        "count_and_design.application.ai.embedding.providers.openai.requests.post",
        return_value=response,
    ) as mock_post:
        result = client.embed_image(image, prompt="Classify drawing.")

    assert result.embedding == [0.7, 0.8]
    body = mock_post.call_args.kwargs["json"]
    assert body["model"] == "vl-embedding"
    assert body["dimensions"] == 256
    assert body["continue_final_message"] is True
    assert body["add_special_tokens"] is True
    assert "add_generation_prompt" not in body
    assert body["messages"][0] == {
        "role": "system",
        "content": [{"type": "text", "text": "Represent input."}],
    }
    user_content = body["messages"][1]["content"]
    assert user_content[0]["type"] == "image_url"
    assert user_content[0]["image_url"]["url"].startswith("data:image/png;base64,")
    assert user_content[1] == {"type": "text", "text": "Classify drawing."}
    assert body["messages"][2] == {
        "role": "assistant",
        "content": [{"type": "text", "text": ""}],
    }


def test_image_embedding_without_prompt_sends_empty_user_text(
    tmp_path: Path,
) -> None:
    """The packaged category vectors were built with an empty user text."""

    image = tmp_path / "drawing.png"
    image.write_bytes(b"png-bytes")
    client = EmbeddingClient(
        EmbeddingConfig(
            base_url="http://embed.test",
            model="vl-embedding",
        )
    )

    with patch(
        "count_and_design.application.ai.embedding.providers.openai.requests.post",
        return_value=_embedding_response(),
    ) as mock_post:
        client.embed_image(image)

    user_content = mock_post.call_args.kwargs["json"]["messages"][1]["content"]
    assert user_content[0]["type"] == "image_url"
    assert user_content[1] == {"type": "text", "text": ""}


def test_embedding_client_discovers_and_caches_model() -> None:
    client = EmbeddingClient(EmbeddingConfig(base_url="http://embed.test"))

    with (
        patch(
            "count_and_design.application.ai.embedding.providers.openai.requests.get",
            return_value=_models_response(["detected-model", "other-model"]),
        ) as mock_get,
        patch(
            "count_and_design.application.ai.embedding.providers.openai.requests.post",
            return_value=_embedding_response(),
        ) as mock_post,
    ):
        client.embed_text("first")
        client.embed_text("second")

    assert client.list_models() == ["detected-model", "other-model"]
    mock_get.assert_called_once_with(
        "http://embed.test/v1/models",
        headers={"Content-Type": "application/json"},
        timeout=5.0,
    )
    assert mock_post.call_count == 2
    assert all(
        call.kwargs["json"]["model"] == "detected-model"
        for call in mock_post.call_args_list
    )


def test_embed_images_runs_concurrently_and_preserves_order() -> None:
    client = EmbeddingClient(
        EmbeddingConfig(
            base_url="http://embed.test",
            model="vl-embedding",
            max_concurrency=3,
        )
    )
    lock = Lock()
    active = 0
    max_active = 0
    progress: list[tuple[int, int]] = []

    def fake_embed(image, **_kwargs):
        nonlocal active, max_active
        with lock:
            active += 1
            max_active = max(max_active, active)
        time.sleep(0.02 * (3 - int(image.decode("ascii"))))
        with lock:
            active -= 1
        return EmbeddingResult(embedding=[float(image.decode("ascii"))])

    with patch.object(client, "embed_image", side_effect=fake_embed):
        results = client.embed_images(
            [b"0", b"1", b"2"],
            on_progress=lambda completed, total: progress.append(
                (completed, total)
            ),
        )

    assert [result.embedding for result in results] == [
        [0.0],
        [1.0],
        [2.0],
    ]
    assert max_active > 1
    assert progress == [(1, 3), (2, 3), (3, 3)]


def test_embedding_client_uses_optional_fallback() -> None:
    fallback = EmbeddingClient(
        EmbeddingConfig(
            base_url="http://fallback.test",
            model="fallback-model",
        )
    )
    client = EmbeddingClient(
        EmbeddingConfig(
            base_url="http://primary.test",
            model="primary-model",
        ),
        fallback=fallback,
    )

    with patch(
        "count_and_design.application.ai.embedding.providers.openai.requests.post",
        side_effect=[
            _embedding_response(status_code=503),
            _embedding_response([0.8], model="fallback-model"),
        ],
    ) as mock_post:
        result = client.embed_text("drawing")

    assert result.embedding == [0.8]
    assert [call.args[0] for call in mock_post.call_args_list] == [
        "http://primary.test/v1/embeddings",
        "http://fallback.test/v1/embeddings",
    ]


def test_embedding_client_accepts_external_provider_fallback() -> None:
    primary = Mock()
    primary.embed_image.side_effect = EmbeddingServiceError(
        "internal unavailable"
    )
    external = Mock()
    external.embed_image.return_value = EmbeddingResult(
        embedding=[0.6],
        model="external-model",
    )
    client = EmbeddingClient(
        provider=primary,
        fallback=external,
    )

    result = client.embed_image(b"image")

    assert result.embedding == [0.6]
    assert result.model == "external-model"
    external.embed_image.assert_called_once_with(
        b"image",
        prompt=None,
        mime_type=None,
    )


def test_embed_images_falls_back_per_failed_image() -> None:
    fallback = EmbeddingClient(
        EmbeddingConfig(
            base_url="http://fallback.test",
            model="fallback-model",
        )
    )
    client = EmbeddingClient(
        EmbeddingConfig(
            base_url="http://primary.test",
            model="primary-model",
        ),
        fallback=fallback,
    )

    with patch(
        "count_and_design.application.ai.embedding.providers.openai.requests.post",
        side_effect=[
            _embedding_response(status_code=503),
            _embedding_response([1.0], model="fallback-model"),
            _embedding_response([2.0], model="primary-model"),
        ],
    ) as mock_post:
        results = client.embed_images(
            [b"first", b"second"],
            max_concurrent=1,
        )

    assert [result.embedding for result in results] == [[1.0], [2.0]]
    assert [call.args[0] for call in mock_post.call_args_list] == [
        "http://primary.test/v1/embeddings",
        "http://fallback.test/v1/embeddings",
        "http://primary.test/v1/embeddings",
    ]


def test_embed_drawing_image_uses_env_file(tmp_path: Path) -> None:
    env_path = tmp_path / ".env"
    image = tmp_path / "drawing.jpg"
    image.write_bytes(b"jpg-bytes")
    env_path.write_text(
        "\n".join(
            [
                "EMBEDDING_BASE_URL=http://embed.test",
                "EMBEDDING_MODEL=vl-embedding",
                "EMBEDDING_DIMENSIONS=64",
            ]
        ),
        encoding="utf-8",
    )
    response = _embedding_response([0.9])

    with patch(
        "count_and_design.application.ai.embedding.providers.openai.requests.post",
        return_value=response,
    ) as mock_post:
        result = embed_drawing_image(image, env_path=env_path)

    assert result.embedding == [0.9]
    body = mock_post.call_args.kwargs["json"]
    assert body["model"] == "vl-embedding"
    assert body["dimensions"] == 64
    assert body["messages"][1]["content"][0]["image_url"]["url"].startswith(
        "data:image/jpeg;base64,"
    )
    assert body["messages"][1]["content"][1] == {
        "type": "text",
        "text": DRAWING_CLASSIFICATION_PROMPT,
    }


def test_embedding_client_wraps_http_errors() -> None:
    client = EmbeddingClient(
        EmbeddingConfig(
            base_url="http://embed.test",
            model="vl-embedding",
        )
    )
    response = _embedding_response(status_code=500)

    with patch(
        "count_and_design.application.ai.embedding.providers.openai.requests.post",
        return_value=response,
    ):
        with pytest.raises(EmbeddingServiceError, match="HTTP 500"):
            client.embed_text("hello")