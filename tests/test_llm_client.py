from __future__ import annotations

import base64
from pathlib import Path
from unittest.mock import Mock, patch

import pytest
import requests

from count_and_design.application.ai import (
    BedrockLlmConfig,
    BedrockLlmProvider,
    ChatMessage,
    LlmClient,
    LlmServiceError,
    OpenRouterLlmConfig,
    OpenRouterLlmProvider,
    QwenLlmConfig,
    QwenLlmProvider,
    create_llm_provider_from_env,
)
from count_and_design.application.config import ConfigError


_LLM_ENV_NAMES = [
    "ENV_FILE",
    "LLM_PROVIDER",
    "LLM_BASE_URL",
    "LLM_CHAT_COMPLETIONS_PATH",
    "LLM_RESPONSES_PATH",
    "LLM_MODELS_PATH",
    "LLM_MODEL",
    "LLM_API_KEY",
    "LLM_TIMEOUT",
    "LLM_MODEL_PROBE_TIMEOUT",
    "LLM_MAX_COMPLETION_TOKENS",
    "LLM_TEMPERATURE",
    "LLM_REASONING_EFFORT",
    "LLM_INCLUDE_REASONING",
    "BEDROCK_API_KEY",
    "BEDROCK_MODEL",
    "BEDROCK_REGION",
    "BEDROCK_MAX_TOKENS",
    "BEDROCK_TIMEOUT",
    "OPENROUTER_API_KEY",
    "OPENROUTER_MODEL",
    "OPENROUTER_BASE_URL",
    "OPENROUTER_CHAT_COMPLETIONS_PATH",
    "OPENROUTER_MODELS_PATH",
    "OPENROUTER_MAX_TOKENS",
    "OPENROUTER_TEMPERATURE",
    "OPENROUTER_TIMEOUT",
    "OPENROUTER_MODEL_PROBE_TIMEOUT",
    "OPENROUTER_HTTP_REFERER",
    "OPENROUTER_APP_TITLE",
]


@pytest.fixture(autouse=True)
def clear_llm_env(monkeypatch):
    for name in _LLM_ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    yield
    for name in _LLM_ENV_NAMES:
        monkeypatch.delenv(name, raising=False)


def _chat_response(
    content: str = "hello",
    *,
    model: str = "Qwen3.5-9B",
    status_code: int = 200,
) -> Mock:
    response = Mock(
        status_code=status_code,
        text="server error" if status_code >= 400 else "",
    )
    response.json.return_value = {
        "id": "chatcmpl-test",
        "object": "chat.completion",
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": content,
                },
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": 10,
            "completion_tokens": 5,
            "total_tokens": 15,
        },
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


def test_llm_config_reads_env_file(tmp_path: Path) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text(
        "\n".join(
            [
                "LLM_BASE_URL=http://llm.test",
                "LLM_CHAT_COMPLETIONS_PATH=/v1/chat/completions",
                "LLM_RESPONSES_PATH=/custom/responses",
                "LLM_MODELS_PATH=/v1/models",
                "LLM_MODEL=Qwen3.5-9B",
                "LLM_API_KEY=test-key",
                "LLM_TIMEOUT=30",
                "LLM_MODEL_PROBE_TIMEOUT=2",
                "LLM_MAX_COMPLETION_TOKENS=256",
                "LLM_TEMPERATURE=0.7",
                "LLM_REASONING_EFFORT=low",
                "LLM_INCLUDE_REASONING=true",
            ]
        ),
        encoding="utf-8",
    )

    config = QwenLlmConfig.from_env(env_path)

    assert config.chat_completions_url == (
        "http://llm.test/v1/chat/completions"
    )
    assert config.responses_url == "http://llm.test/custom/responses"
    assert config.models_url == "http://llm.test/v1/models"
    assert config.model == "Qwen3.5-9B"
    assert config.api_key == "test-key"
    assert config.timeout == 30
    assert config.model_probe_timeout == 2
    assert config.max_completion_tokens == 256
    assert config.temperature == 0.7
    assert config.reasoning_effort == "low"
    assert config.include_reasoning is True


def test_llm_config_requires_base_url(tmp_path: Path) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text("LLM_MODEL=Qwen3.5-9B\n", encoding="utf-8")

    with pytest.raises(ConfigError, match="LLM_BASE_URL"):
        QwenLlmConfig.from_env(env_path)


def test_qwen_provider_posts_chat_completion() -> None:
    config = QwenLlmConfig(
        base_url="http://llm.test",
        model="Qwen3.5-9B",
        max_completion_tokens=100,
        reasoning_effort="none",
        include_reasoning=False,
    )
    provider = QwenLlmProvider(config)
    response = _chat_response("2")

    with patch(
        "count_and_design.application.ai.llm.providers.qwen.requests.post",
        return_value=response,
    ) as mock_post:
        result = provider.chat(
            [ChatMessage(role="user", content="1+1=?")],
        )

    body = mock_post.call_args.kwargs["json"]
    assert body["model"] == "Qwen3.5-9B"
    assert body["messages"] == [
        {"role": "user", "content": "1+1=?"},
    ]
    assert body["max_completion_tokens"] == 100
    assert body["reasoning_effort"] == "none"
    assert body["include_reasoning"] is False
    assert "temperature" not in body
    assert result.content == "2"
    assert result.model == "Qwen3.5-9B"
    assert result.finish_reason == "stop"


def test_qwen_provider_posts_multimodal_chat_completion() -> None:
    config = QwenLlmConfig(
        base_url="http://llm.test",
        model="Qwen3.5-9B",
        max_completion_tokens=128,
        reasoning_effort="none",
        include_reasoning=False,
    )
    provider = QwenLlmProvider(config)
    response = _chat_response("INV11A101-CBX-01")

    with patch(
        "count_and_design.application.ai.llm.providers.qwen.requests.post",
        return_value=response,
    ) as mock_post:
        result = provider.complete_with_image(
            "Read the Node name",
            b"png-bytes",
            mime_type="image/png",
            system_prompt="Return only the name.",
        )

    body = mock_post.call_args.kwargs["json"]
    assert body["model"] == "Qwen3.5-9B"
    assert body["max_completion_tokens"] == 128
    assert body["messages"][0] == {
        "role": "system",
        "content": "Return only the name.",
    }
    user_content = body["messages"][1]["content"]
    assert user_content[0]["type"] == "image_url"
    assert user_content[0]["image_url"]["url"] == (
        "data:image/png;base64," + base64.b64encode(b"png-bytes").decode("ascii")
    )
    assert user_content[1] == {"type": "text", "text": "Read the Node name"}
    assert result.content == "INV11A101-CBX-01"


def test_qwen_provider_posts_responses_completion() -> None:
    config = QwenLlmConfig.for_responses_endpoint(
        "http://qwen.test/v1/responses",
        model="Qwen3.5-9B",
        api_key="test-key",
        timeout=30,
        max_completion_tokens=256,
    )
    client = LlmClient(config)
    response = Mock(status_code=200, text="")
    response.json.return_value = {
        "model": "Qwen3.5-9B",
        "status": "completed",
        "output": [
            {
                "content": [
                    {"type": "output_text", "text": '{"ok": true}'}
                ]
            }
        ],
        "usage": {"total_tokens": 12},
    }

    with patch(
        "count_and_design.application.ai.llm.providers.qwen.requests.post",
        return_value=response,
    ) as mock_post:
        result = client.respond(
            "extract this",
            instructions="Return JSON.",
            temperature=0,
            max_output_tokens=128,
            extra_body={
                "chat_template_kwargs": {"enable_thinking": False}
            },
        )

    assert mock_post.call_args.args[0] == "http://qwen.test/v1/responses"
    assert mock_post.call_args.kwargs["headers"]["Authorization"] == (
        "Bearer test-key"
    )
    assert mock_post.call_args.kwargs["timeout"] == 30
    body = mock_post.call_args.kwargs["json"]
    assert body == {
        "model": "Qwen3.5-9B",
        "input": "extract this",
        "instructions": "Return JSON.",
        "temperature": 0,
        "max_output_tokens": 128,
        "stream": False,
        "chat_template_kwargs": {"enable_thinking": False},
    }
    assert result.content == '{"ok": true}'
    assert result.finish_reason == "completed"
    assert result.usage == {"total_tokens": 12}


def test_qwen_provider_discovers_model_when_unset() -> None:
    config = QwenLlmConfig(base_url="http://llm.test")
    provider = QwenLlmProvider(config)
    models_response = _models_response(["Qwen3.5-9B"])
    chat_response = _chat_response("ok")

    with patch(
        "count_and_design.application.ai.llm.providers.qwen.requests.get",
        return_value=models_response,
    ) as mock_get, patch(
        "count_and_design.application.ai.llm.providers.qwen.requests.post",
        return_value=chat_response,
    ) as mock_post:
        result = provider.complete("hello")

    assert mock_get.call_args.args[0] == "http://llm.test/v1/models"
    assert mock_post.call_args.kwargs["json"]["model"] == "Qwen3.5-9B"
    assert result.content == "ok"


def test_llm_client_from_env(tmp_path: Path) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text(
        "LLM_BASE_URL=http://llm.test\nLLM_MODEL=Qwen3.5-9B\n",
        encoding="utf-8",
    )
    client = LlmClient.from_env(env_path)
    response = _chat_response("pong")

    with patch(
        "count_and_design.application.ai.llm.providers.qwen.requests.post",
        return_value=response,
    ):
        result = client.complete("ping")

    assert result.content == "pong"
    assert client.config.model == "Qwen3.5-9B"


def test_llm_client_wraps_network_errors() -> None:
    config = QwenLlmConfig(
        base_url="http://llm.test",
        model="Qwen3.5-9B",
    )
    client = LlmClient(config)

    with patch(
        "count_and_design.application.ai.llm.providers.qwen.requests.post",
        side_effect=requests.RequestException("boom"),
    ):
        with pytest.raises(LlmServiceError, match="LLM request failed"):
            client.complete("hello")


def _bedrock_response(
    content: str = "hello",
    *,
    status_code: int = 200,
) -> Mock:
    response = Mock(
        status_code=status_code,
        text="server error" if status_code >= 400 else "",
    )
    response.json.return_value = {
        "output": {
            "message": {
                "role": "assistant",
                "content": [{"text": content}],
            }
        },
        "stopReason": "end_turn",
        "usage": {
            "inputTokens": 10,
            "outputTokens": 5,
            "totalTokens": 15,
        },
    }
    return response


def test_bedrock_config_reads_env_file(tmp_path: Path) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text(
        "\n".join(
            [
                "BEDROCK_API_KEY=bedrock-key",
                "BEDROCK_MODEL=us.anthropic.claude-opus-4-7",
                "BEDROCK_REGION=us-west-2",
                "BEDROCK_MAX_TOKENS=512",
                "BEDROCK_TIMEOUT=45",
            ]
        ),
        encoding="utf-8",
    )

    config = BedrockLlmConfig.from_env(env_path)

    assert config.api_key == "bedrock-key"
    assert config.model == "us.anthropic.claude-opus-4-7"
    assert config.region == "us-west-2"
    assert config.max_tokens == 512
    assert config.timeout == 45
    assert config.converse_url == (
        "https://bedrock-runtime.us-west-2.amazonaws.com/"
        "model/us.anthropic.claude-opus-4-7/converse"
    )


def test_bedrock_config_requires_api_key(tmp_path: Path) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text("BEDROCK_MODEL=us.anthropic.claude-opus-4-7\n", encoding="utf-8")

    with pytest.raises(ConfigError, match="BEDROCK_API_KEY"):
        BedrockLlmConfig.from_env(env_path)


def test_bedrock_provider_posts_converse_request() -> None:
    config = BedrockLlmConfig(
        api_key="bedrock-key",
        model="us.anthropic.claude-opus-4-7",
        region="us-east-1",
        max_tokens=256,
    )
    provider = BedrockLlmProvider(config)
    response = _bedrock_response("2")

    with patch(
        "count_and_design.application.ai.llm.providers.bedrock.requests.post",
        return_value=response,
    ) as mock_post:
        result = provider.chat(
            [
                ChatMessage(role="system", content="You are helpful."),
                ChatMessage(role="user", content="1+1=?"),
            ],
        )

    assert mock_post.call_args.args[0] == (
        "https://bedrock-runtime.us-east-1.amazonaws.com/"
        "model/us.anthropic.claude-opus-4-7/converse"
    )
    body = mock_post.call_args.kwargs["json"]
    assert body["system"] == [{"text": "You are helpful."}]
    assert body["messages"] == [
        {"role": "user", "content": [{"text": "1+1=?"}]},
    ]
    assert body["inferenceConfig"]["maxTokens"] == 256
    assert mock_post.call_args.kwargs["headers"]["Authorization"] == (
        "Bearer bedrock-key"
    )
    assert result.content == "2"
    assert result.finish_reason == "end_turn"


def test_bedrock_provider_omits_optional_inference_defaults() -> None:
    config = BedrockLlmConfig(
        api_key="bedrock-key",
        model="us.anthropic.claude-opus-4-7",
        region="us-east-1",
    )
    provider = BedrockLlmProvider(config)
    response = _bedrock_response("node")

    with patch(
        "count_and_design.application.ai.llm.providers.bedrock.requests.post",
        return_value=response,
    ) as mock_post:
        provider.chat(
            [ChatMessage(role="user", content="Read the node")],
            temperature=0,
        )

    body = mock_post.call_args.kwargs["json"]
    assert "inferenceConfig" not in body


def test_bedrock_image_provider_omits_optional_inference_defaults() -> None:
    config = BedrockLlmConfig(
        api_key="bedrock-key",
        model="us.anthropic.claude-opus-4-7",
        region="us-east-1",
    )
    provider = BedrockLlmProvider(config)
    response = _bedrock_response("node")

    with patch(
        "count_and_design.application.ai.llm.providers.bedrock.requests.post",
        return_value=response,
    ) as mock_post:
        provider.complete_with_image(
            "Read the node",
            b"image-bytes",
            temperature=0,
        )

    body = mock_post.call_args.kwargs["json"]
    assert "inferenceConfig" not in body


def test_bedrock_provider_posts_multiple_ordered_images() -> None:
    config = BedrockLlmConfig(
        api_key="bedrock-key",
        model="us.anthropic.claude-opus-4-7",
        region="us-east-1",
    )
    provider = BedrockLlmProvider(config)
    response = _bedrock_response('{"pages": []}')

    with patch(
        "count_and_design.application.ai.llm.providers.bedrock.requests.post",
        return_value=response,
    ) as mock_post:
        provider.complete_with_images(
            "Read every page",
            (
                (b"first-image", "image/png"),
                (b"second-image", "image/jpeg"),
            ),
            system_prompt="Return JSON.",
        )

    body = mock_post.call_args.kwargs["json"]
    content = body["messages"][0]["content"]
    assert [block["image"]["format"] for block in content[:-1]] == [
        "png",
        "jpeg",
    ]
    assert content[0]["image"]["source"]["bytes"] == (
        base64.b64encode(b"first-image").decode("ascii")
    )
    assert content[1]["image"]["source"]["bytes"] == (
        base64.b64encode(b"second-image").decode("ascii")
    )
    assert content[-1] == {"text": "Read every page"}
    assert body["system"] == [{"text": "Return JSON."}]


def test_create_llm_provider_from_env_selects_bedrock(tmp_path: Path) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text(
        "\n".join(
            [
                "LLM_PROVIDER=bedrock",
                "BEDROCK_API_KEY=bedrock-key",
                "BEDROCK_MODEL=us.anthropic.claude-opus-4-7",
            ]
        ),
        encoding="utf-8",
    )

    provider = create_llm_provider_from_env(env_path)

    assert isinstance(provider, BedrockLlmProvider)
    assert provider.config.model == "us.anthropic.claude-opus-4-7"


def test_llm_client_from_env_uses_bedrock(tmp_path: Path) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text(
        "\n".join(
            [
                "LLM_PROVIDER=bedrock",
                "BEDROCK_API_KEY=bedrock-key",
                "BEDROCK_MODEL=us.anthropic.claude-opus-4-7",
            ]
        ),
        encoding="utf-8",
    )
    client = LlmClient.from_env(env_path)
    response = _bedrock_response("pong")

    with patch(
        "count_and_design.application.ai.llm.providers.bedrock.requests.post",
        return_value=response,
    ):
        result = client.complete("ping")

    assert isinstance(client.provider, BedrockLlmProvider)
    assert result.content == "pong"


def test_openrouter_config_reads_env_file(tmp_path: Path) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text(
        "\n".join(
            [
                "OPENROUTER_API_KEY=or-key",
                "OPENROUTER_MODEL=anthropic/claude-sonnet-4.5",
                "OPENROUTER_BASE_URL=https://openrouter.test/api/v1",
                "OPENROUTER_MAX_TOKENS=512",
                "OPENROUTER_TEMPERATURE=0.2",
                "OPENROUTER_TIMEOUT=45",
                "OPENROUTER_HTTP_REFERER=https://app.example",
                "OPENROUTER_APP_TITLE=count-and-design",
            ]
        ),
        encoding="utf-8",
    )

    config = OpenRouterLlmConfig.from_env(env_path)

    assert config.api_key == "or-key"
    assert config.model == "anthropic/claude-sonnet-4.5"
    assert config.max_tokens == 512
    assert config.temperature == 0.2
    assert config.timeout == 45
    assert config.http_referer == "https://app.example"
    assert config.app_title == "count-and-design"
    assert config.chat_completions_url == (
        "https://openrouter.test/api/v1/chat/completions"
    )
    assert config.models_url == "https://openrouter.test/api/v1/models"


def test_openrouter_config_requires_api_key(tmp_path: Path) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text(
        "OPENROUTER_MODEL=anthropic/claude-sonnet-4.5\n",
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="OPENROUTER_API_KEY"):
        OpenRouterLlmConfig.from_env(env_path)


def test_openrouter_provider_posts_chat_completion() -> None:
    config = OpenRouterLlmConfig(
        api_key="or-key",
        model="anthropic/claude-sonnet-4.5",
        max_tokens=256,
        http_referer="https://app.example",
        app_title="count-and-design",
    )
    provider = OpenRouterLlmProvider(config)
    response = _chat_response("2", model="anthropic/claude-sonnet-4.5")

    with patch(
        "count_and_design.application.ai.llm.providers.openrouter.requests.post",
        return_value=response,
    ) as mock_post:
        result = provider.chat(
            [
                ChatMessage(role="system", content="You are helpful."),
                ChatMessage(role="user", content="1+1=?"),
            ]
        )

    assert mock_post.call_args.args[0] == (
        "https://openrouter.ai/api/v1/chat/completions"
    )
    headers = mock_post.call_args.kwargs["headers"]
    assert headers["Authorization"] == "Bearer or-key"
    assert headers["HTTP-Referer"] == "https://app.example"
    assert headers["X-Title"] == "count-and-design"
    body = mock_post.call_args.kwargs["json"]
    assert body["model"] == "anthropic/claude-sonnet-4.5"
    assert body["messages"] == [
        {"role": "system", "content": "You are helpful."},
        {"role": "user", "content": "1+1=?"},
    ]
    assert body["max_tokens"] == 256
    assert "temperature" not in body
    assert result.content == "2"
    assert result.model == "anthropic/claude-sonnet-4.5"
    assert result.finish_reason == "stop"


def test_openrouter_provider_omits_optional_inference_defaults() -> None:
    config = OpenRouterLlmConfig(
        api_key="or-key",
        model="anthropic/claude-sonnet-4.5",
    )
    provider = OpenRouterLlmProvider(config)
    response = _chat_response("node", model="anthropic/claude-sonnet-4.5")

    with patch(
        "count_and_design.application.ai.llm.providers.openrouter.requests.post",
        return_value=response,
    ) as mock_post:
        provider.chat(
            [ChatMessage(role="user", content="Read the node")],
        )

    body = mock_post.call_args.kwargs["json"]
    assert "max_tokens" not in body
    assert "temperature" not in body


def test_openrouter_provider_posts_multiple_ordered_images() -> None:
    config = OpenRouterLlmConfig(
        api_key="or-key",
        model="anthropic/claude-sonnet-4.5",
    )
    provider = OpenRouterLlmProvider(config)
    response = _chat_response('{"pages": []}', model="anthropic/claude-sonnet-4.5")

    with patch(
        "count_and_design.application.ai.llm.providers.openrouter.requests.post",
        return_value=response,
    ) as mock_post:
        provider.complete_with_images(
            "Read every page",
            (
                (b"first-image", "image/png"),
                (b"second-image", "image/jpeg"),
            ),
            system_prompt="Return JSON.",
        )

    body = mock_post.call_args.kwargs["json"]
    assert body["messages"][0] == {
        "role": "system",
        "content": "Return JSON.",
    }
    user_content = body["messages"][1]["content"]
    assert user_content[0]["type"] == "image_url"
    assert user_content[0]["image_url"]["url"] == (
        "data:image/png;base64," + base64.b64encode(b"first-image").decode("ascii")
    )
    assert user_content[1]["image_url"]["url"] == (
        "data:image/jpeg;base64,"
        + base64.b64encode(b"second-image").decode("ascii")
    )
    assert user_content[2] == {"type": "text", "text": "Read every page"}


def test_create_llm_provider_from_env_selects_openrouter(tmp_path: Path) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text(
        "\n".join(
            [
                "LLM_PROVIDER=openrouter",
                "OPENROUTER_API_KEY=or-key",
                "OPENROUTER_MODEL=anthropic/claude-sonnet-4.5",
            ]
        ),
        encoding="utf-8",
    )

    provider = create_llm_provider_from_env(env_path)

    assert isinstance(provider, OpenRouterLlmProvider)
    assert provider.config.model == "anthropic/claude-sonnet-4.5"


def test_llm_client_from_env_uses_openrouter(tmp_path: Path) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text(
        "\n".join(
            [
                "LLM_PROVIDER=openrouter",
                "OPENROUTER_API_KEY=or-key",
                "OPENROUTER_MODEL=anthropic/claude-sonnet-4.5",
            ]
        ),
        encoding="utf-8",
    )
    client = LlmClient.from_env(env_path)
    response = _chat_response("pong", model="anthropic/claude-sonnet-4.5")

    with patch(
        "count_and_design.application.ai.llm.providers.openrouter.requests.post",
        return_value=response,
    ):
        result = client.complete("ping")

    assert isinstance(client.provider, OpenRouterLlmProvider)
    assert result.content == "pong"

