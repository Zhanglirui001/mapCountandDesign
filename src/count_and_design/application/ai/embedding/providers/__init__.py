"""Concrete embedding provider implementations."""

from .openai import OpenAIEmbeddingProvider, image_to_data_uri

__all__ = [
    "OpenAIEmbeddingProvider",
    "image_to_data_uri",
]
