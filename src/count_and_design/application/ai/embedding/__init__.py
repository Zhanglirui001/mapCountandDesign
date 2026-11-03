"""Public embedding API with pluggable provider support."""

from .client import EmbeddingClient, EmbeddingProgressCallback
from .drawing import (
    DRAWING_CLASSIFICATION_PROMPT,
    create_embedding_client,
    embed_drawing_image,
    embed_drawing_images,
    embed_text,
)
from .providers import OpenAIEmbeddingProvider, image_to_data_uri
from .types import (
    EmbeddingProvider,
    EmbeddingResult,
    EmbeddingServiceError,
    ImageInput,
)

__all__ = [
    "DRAWING_CLASSIFICATION_PROMPT",
    "EmbeddingClient",
    "EmbeddingProgressCallback",
    "EmbeddingProvider",
    "EmbeddingResult",
    "EmbeddingServiceError",
    "ImageInput",
    "OpenAIEmbeddingProvider",
    "create_embedding_client",
    "embed_drawing_image",
    "embed_drawing_images",
    "embed_text",
    "image_to_data_uri",
]
